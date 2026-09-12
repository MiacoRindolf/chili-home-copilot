"""One read-only ordinary print-journal owner and immutable structural views.

This connects the real commit journal to the incremental reducer. Consumers do
not query/reduce their own tape. The reducer is process-local; a durable sink can
publish its views to other processes. This is not a provider-completeness
certificate or entry authority.
Crypto has a different source contract; no crypto rows enter this IQFeed owner.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import threading
import time

import sqlalchemy as sa

from scripts.iqfeed_print_publications import Cursor, capture_frontier, read_publications
from .structural_tape_prefix import (
    Limits, Prefix, PublicationFrontierReceipt, Result, Tick, rows_sha256,
)


def _hash(value):
    def encode(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError("noncanonical_source_value")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False, default=encode).encode()).hexdigest()


def _ns(value, *, stored_naive_utc=False):
    if type(value) is not datetime:
        raise ValueError("missing_source_clock")
    if value.tzinfo is None:
        if not stored_naive_utc:
            raise ValueError("naive_source_clock")
        value = value.replace(tzinfo=timezone.utc)
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def _symbols(values):
    if type(values) not in (list, tuple, frozenset, set):
        raise ValueError("invalid_demand_membership")
    if any(type(s) is not str or not s or s != s.strip().upper()
           or "/" in s or "-" in s for s in values):
        raise ValueError("ordinary_equity_symbol_required")
    return frozenset(values)


@dataclass(frozen=True)
class ScopeView:
    reference: object
    end_index: int
    phase_bounds: tuple
    phase_mass: tuple
    whole_mass: tuple


@dataclass(frozen=True)
class SymbolView:
    symbol: str
    demand_reasons: tuple[str, ...]
    prefix_sha256: str
    print_count: int
    last_print: Tick | None
    scopes: tuple[ScopeView, ...]
    events: tuple
    history_before_anchor: str = "unknown"
    selected_parent_local: str = "not_yet_derived"
    quote_freshness: str = "not_certified_by_trade_row"


@dataclass(frozen=True)
class ContextSnapshot:
    source: Cursor
    observed_frontier: Cursor
    root_sha256: str
    status: str
    reason: str | None
    symbols: tuple[SymbolView, ...]
    stale_demand_sources: tuple[str, ...]
    provider_completeness_certified: bool = False
    order_authority: bool = False


class OrdinaryStructuralContextOwner:
    """Single serialized writer, shared immutable reads, no rank-based retirement.

    Demand failures retain the previous membership; only a newer successful
    authoritative empty demand clears its own reason. All previously enrolled
    symbols remain observed until explicit owner retirement, even after their
    last reason disappears. max_symbols is a resource bound, not a trading top-N.
    A fresh owner starts from its declared cold anchor, not fictional warm history.
    """
    REASONS = frozenset({"inventory", "ranking", "held", "pending", "watch"})

    def __init__(self, *, anchor: Cursor, limits: Limits, max_symbols: int,
                 max_trade_rows: int, clock_ns=time.time_ns):
        if (type(anchor) is not Cursor or type(anchor.epoch) is not str or not anchor.epoch
                or type(anchor.revision) is not int or anchor.revision < 0
                or type(limits) is not Limits or type(max_symbols) is not int or max_symbols <= 0
                or type(max_trade_rows) is not int or max_trade_rows <= 0):
            raise ValueError("invalid_context_owner_configuration")
        self._lock = threading.RLock()
        self._limits, self._max_symbols, self._max_rows = limits, max_symbols, max_trade_rows
        self._clock_ns = clock_ns
        self._cursor = anchor
        self._identity = _hash(["ordinary_print_publications", anchor.epoch])
        self._root = _hash(["ordinary_structural_context_anchor_v1", asdict(anchor)])
        self._prefixes = {}
        self._demands = {}
        self._stale = set()
        self._failed_commit = False
        self._sink = None
        self._snapshot = ContextSnapshot(anchor, anchor, self._root, "cold", None, (), ())

    @classmethod
    def cold_start(cls, engine, **kwargs):
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            c.execute(sa.text("SET TRANSACTION READ ONLY"))
            anchor = capture_frontier(c)
        return cls(anchor=anchor, **kwargs)

    def update_demand(self, reason: str, *, revision: int, symbols=None):
        """None means source read failure, not an authoritative empty inventory."""
        with self._lock:
            if self._failed_commit:
                raise RuntimeError("context_owner_reconstruction_required")
            if reason not in self.REASONS or type(revision) is not int or revision <= 0:
                raise ValueError("invalid_demand_source")
            prior = self._demands.get(reason, (0, frozenset()))
            if revision <= prior[0]:
                raise ValueError("stale_demand_revision")
            if symbols is None:
                self._demands[reason] = (revision, prior[1])
                self._stale.add(reason)
            else:
                requested = _symbols(symbols)
                additions = requested - self._prefixes.keys()
                if len(self._prefixes) + len(additions) > self._max_symbols:
                    raise ValueError("context_symbol_resource_capacity")
                new = {s: Prefix("iqfeed:"+s, f"{self._cursor.epoch}:{self._cursor.revision}:{s}",
                                 self._limits) for s in sorted(additions)}
                self._prefixes.update(new)
                self._demands[reason] = (revision, requested)
                self._stale.discard(reason)
            self._publish(self._snapshot.observed_frontier, self._snapshot.status,
                          self._snapshot.reason, None)

    def bind_publication_sink(self, sink):
        """Bind a durable publisher while cold, before any observation is exposed.

        The supplied publisher owns its cross-process fence. Publication failure
        makes this owner non-runnable rather than exposing an uncommitted view.
        """
        with self._lock:
            if (self._failed_commit or self._sink is not None or not callable(sink)
                    or self._snapshot.status != "cold" or any(p.count for p in self._prefixes.values())):
                raise ValueError("context_sink_requires_unbound_cold_owner")
            self._sink = sink
            try:
                sink(self._snapshot)
            except BaseException:
                self._failed_commit = True
                self._snapshot = replace(self._snapshot, status="unresolved",
                                         reason="context_publication_failed")
                raise

    def read(self, consumer: str, *, after: Cursor | None = None) -> ContextSnapshot:
        if consumer not in {"selection", "entry", "exit", "audit"}:
            raise ValueError("unknown_context_consumer")
        with self._lock:
            if after is not None:
                if (type(after) is not Cursor or after.epoch != self._cursor.epoch
                        or type(after.revision) is not int
                        or not 0 <= after.revision <= self._cursor.revision):
                    raise ValueError("context_consumer_cursor_invalid")
                if self._cursor.revision - after.revision > 1:
                    raise ValueError("context_consumer_gap_requires_journal_replay")
            return self._snapshot

    def _publish(self, frontier, status, reason, results):
        views = []
        prior = {v.symbol: v for v in self._snapshot.symbols}
        for symbol, prefix in sorted(self._prefixes.items()):
            if symbol in prior and prior[symbol].print_count == prefix.count:
                # An empty source delta or demand change cannot alter geometry.
                # Reuse immutable scopes instead of re-querying every reference.
                scopes = prior[symbol].scopes
            else:
                scopes = []
                for ref in prefix.active_references():
                    value = prefix.context(ref)
                    scopes.append(ScopeView(ref, value["end_index"], value["phase_bounds"],
                                            value["phase_mass"], value["whole_mass"]))
                scopes = tuple(scopes)
            views.append(SymbolView(symbol,
                tuple(sorted(r for r, (_, members) in self._demands.items() if symbol in members)),
                prefix.prefix_sha256, prefix.count, prefix.tick(prefix.count-1) if prefix.count else None,
                scopes, (prior[symbol].events if symbol in prior else ()) if results is None
                else results[symbol].events if symbol in results else ()))
        snapshot = ContextSnapshot(self._cursor, frontier, self._root, status, reason,
                                   tuple(views), tuple(sorted(self._stale)))
        if self._sink is not None:
            try:
                self._sink(snapshot)
            except BaseException:
                self._failed_commit = True
                self._snapshot = replace(self._snapshot, status="unresolved",
                                         reason="context_publication_failed")
                raise
        self._snapshot = snapshot

    def _tick(self, row):
        if (row.get("timestamp_basis") != "iqfeed_selected_trade_date_timems_exact"
                or row.get("message_type") != "Q"):
            raise ValueError("exact_ordinary_print_required")
        for key in ("bridge_run_id", "bridge_version"):
            if type(row.get(key)) is not str or not row[key]:
                raise ValueError("ordinary_source_epoch_missing")
        for key in ("connection_generation", "source_frame_sequence"):
            if type(row.get(key)) is not int or row[key] <= 0:
                raise ValueError("ordinary_source_sequence_missing")
        digest = row.get("source_frame_sha256")
        if (type(digest) is not str or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("ordinary_source_frame_digest_missing")
        # Unknown/delayed data must not become a real-time structural authority.
        if type(row.get("provider_delay_minutes")) is not int or row["provider_delay_minutes"] != 0:
            raise ValueError("ordinary_provider_delay_unresolved")
        event = _ns(row["provider_event_at"])
        if event != _ns(row["observed_at"], stored_naive_utc=True):
            raise ValueError("ordinary_event_clock_mismatch")
        epoch = (self._cursor.epoch, row["bridge_run_id"], row["connection_generation"],
                 row["bridge_version"], row["timestamp_basis"])
        return Tick(row["id"], row["price"], row["size"], row["bid"], row["ask"], event,
                    _ns(row["received_at"]), _ns(row["available_at"]), epoch,
                    row["source_frame_sequence"], row["source_frame_sha256"])

    def advance_one(self, engine) -> ContextSnapshot:
        """Read one actual atomic release across all enrolled symbols, then commit.

        Page size1 is a transaction boundary, not a price window. Repeated calls
        catch up through the journal. No individual symbol advances on domain
        failure; a catastrophic commit failure requires owner reconstruction.
        """
        with self._lock:
            if self._failed_commit:
                raise RuntimeError("context_owner_reconstruction_required")
            if not self._prefixes:
                return self._snapshot
            frontier = self._cursor
            try:
                with engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
                    c.execute(sa.text("SET TRANSACTION READ ONLY"))
                    read = read_publications(c, symbols=frozenset(self._prefixes), after=self._cursor,
                                            max_publications=1, max_trade_rows=self._max_rows)
                frontier = read.observed_frontier
                if not read.publications:
                    return self._snapshot
                publication, = read.publications
                known_ns = self._clock_ns()
                if type(known_ns) is not int or known_ns <= 0:
                    raise ValueError("invalid_context_observation_clock")
                root = _hash([self._root, asdict(publication.cursor), publication.available_at,
                    publication.source_trade_ids, publication.source_symbols,
                    [dict(row) for row in publication.rows]])
                grouped = {s: [] for s in self._prefixes}
                for row in publication.rows:
                    grouped[row["symbol"]].append(self._tick(row))
                prepared, results = {}, {}
                for symbol, prefix in self._prefixes.items():
                    rows = tuple(sorted(grouped[symbol], key=lambda r: r.frame_sequence))
                    previous = prefix.tick(prefix.count-1) if prefix.count else None
                    for row in rows:
                        if previous and (row.epoch != previous.epoch
                                         or row.frame_sequence <= previous.frame_sequence):
                            raise ValueError("ordinary_source_order_requires_reconstruction")
                        previous = row
                    receipt = PublicationFrontierReceipt(known_ns, len(rows), rows_sha256(rows),
                        prefix.prefix_sha256, self._identity, publication.cursor.revision, root,
                        self._cursor.revision, self._root)
                    stage = prefix.prepare_frontier(rows, receipt)
                    if isinstance(stage, Result):
                        raise ValueError(stage.reason or "ordinary_frontier_not_applied")
                    prepared[symbol] = stage
                # No reducer mutation has occurred before every symbol validates.
            except (ValueError, TypeError, KeyError, sa.exc.SQLAlchemyError) as exc:
                code = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                self._publish(frontier, "unresolved", code, {})
                return self._snapshot
            try:
                for symbol, stage in prepared.items():
                    result = self._prefixes[symbol].commit_frontier(stage)
                    if result.status != "applied":
                        raise RuntimeError("prepared_context_commit_failed")
                    results[symbol] = result
                self._cursor, self._root = publication.cursor, root
                self._publish(frontier, "observed_prefix", None, results)
            except BaseException:
                self._failed_commit = True
                self._snapshot = replace(self._snapshot, status="unresolved",
                                         reason="context_owner_reconstruction_required")
                raise
            return self._snapshot
