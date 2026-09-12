"""One read-only ordinary print-journal owner and immutable structural views.

This connects the real commit journal to the incremental reducer. Consumers do
not query/reduce their own tape. The reducer is process-local; a durable sink can
publish its views to other processes. This is not a provider-completeness
certificate or entry authority.
Crypto has a different source contract; no crypto rows enter this IQFeed owner.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from types import MappingProxyType
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
from app.tick_math.wave_context import WaveContext
from app.tick_math.wave_evidence import WaveEvidence, wave_evidence
from .native_tick_enrollment import (NativeEquityIdentity, NativeEnrollmentReference,
    NativeMappingGap, NativeTickEnrollment, validate_enrollment)


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


def _symbols(values, *, bound=()):
    if type(values) not in (list, tuple, frozenset, set):
        raise ValueError("invalid_demand_membership")
    if any(type(s) is not str or not s or s != s.strip().upper()
           or "/" in s or ("-" in s and s not in bound) for s in values):
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
    wave_context: WaveContext | None = None
    wave_evidence: tuple[WaveEvidence, ...] = ()
    native_identity: NativeEquityIdentity | None = None
    native_binding_current: bool = False


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
    native_enrollment: NativeEnrollmentReference | None = None
    native_mapping_gaps: tuple[NativeMappingGap, ...] = ()


@dataclass(frozen=True)
class DemandCheckpoint:
    reason: str
    revision: int
    symbols: tuple[str, ...]
    complete: bool


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
        self._native_enrollment = None
        self._native_catalog_frontier = None
        self._native_bindings = {}
        self._current_native_symbols = frozenset()
        self._stale = set()
        self._failed_commit = False
        self._sink = None
        self._replay_sink = None
        self._snapshot = ContextSnapshot(anchor, anchor, self._root, "cold", None, (), ())

    @classmethod
    def cold_start(cls, engine, **kwargs):
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            c.execute(sa.text("SET TRANSACTION READ ONLY"))
            anchor = capture_frontier(c)
        return cls(anchor=anchor, **kwargs)

    def update_demand(self, reason: str, *, revision: int, symbols=None):
        """None means source read failure, not an authoritative empty inventory."""
        updates = {reason: {"revision": revision, "symbols": symbols,
                            "complete": symbols is not None}}
        self._update_demands(updates, legacy=True)

    def update_demands(self, updates: dict):
        """Publish related membership changes together, without partial releases.

        Each reason supplies revision, symbols and complete. An incomplete read
        can add observed symbols but cannot remove prior demand. Complete empty
        membership explicitly clears that reason. This is observation demand,
        not account atomicity, symbol mapping, or trade admission authority.
        """
        self._update_demands(updates, legacy=False)

    def update_native_enrollment(self, enrollment: NativeTickEnrollment):
        """Bind native identities and all equity demand reasons in one release.

        Failed mappings retain prior observation demand, but cannot label its
        retained identity as a current binding. A provider ticker cannot inherit
        another UUID's old wave history. Legacy ranking/watch remain separate.
        """
        with self._lock:
            validate_enrollment(enrollment)
            ref = enrollment.reference
            if ref.catalog_observed_ns is not None and self._native_catalog_frontier is not None:
                prior_ns, prior_hash = self._native_catalog_frontier
                if (ref.catalog_observed_ns < prior_ns or
                        ref.catalog_observed_ns == prior_ns and ref.catalog_sha256 != prior_hash):
                    raise ValueError('native_enrollment_catalog_frontier_regressed_or_conflicting')
            previous = self._native_enrollment
            if previous is not None:
                before, after = previous.reference, enrollment.reference
                if before.account_identity_sha256 != after.account_identity_sha256:
                    raise ValueError('native_enrollment_account_changed')
                if after.native_revision < before.native_revision:
                    raise ValueError('native_enrollment_revision_regressed')
                if (after.native_revision == before.native_revision
                        and after.native_observation_sha256 != before.native_observation_sha256):
                    raise ValueError('native_enrollment_native_revision_conflict')
                if previous == enrollment:
                    if self._failed_commit:
                        raise RuntimeError('context_owner_reconstruction_required')
                    return self._snapshot
            revision = max((self._demands.get(r, (0, ()))[0]
                            for r in ('inventory', 'held', 'pending')), default=0)+1
            updates = {r: dict(revision=revision, symbols=getattr(enrollment, r),
                               complete=r not in enrollment.incomplete_reasons)
                       for r in ('inventory', 'held', 'pending')}
            self._update_demands(updates, legacy=False, enrollment=enrollment)
            return self._snapshot

    def _update_demands(self, updates, *, legacy, enrollment=None):
        with self._lock:
            if self._failed_commit:
                raise RuntimeError("context_owner_reconstruction_required")
            if type(updates) is not dict or not updates or not set(updates) <= self.REASONS:
                raise ValueError("invalid_demand_source")
            if (enrollment is None and self._native_enrollment is not None
                    and set(updates) & {'inventory', 'held', 'pending'}):
                raise ValueError('native_demand_requires_bound_update')
            identities = dict(self._native_bindings)
            bound = frozenset()
            if enrollment is not None:
                bound = frozenset(b.provider_symbol for b in enrollment.bindings)
                for binding in enrollment.bindings:
                    prior_identity = identities.get(binding.provider_symbol)
                    if prior_identity is not None and prior_identity.asset_id != binding.asset_id:
                        raise ValueError('native_provider_identity_requires_reconstruction')
                    prior_prefix = self._prefixes.get(binding.provider_symbol)
                    if prior_identity is None and prior_prefix is not None and prior_prefix.count:
                        raise ValueError('native_binding_requires_cold_provider_prefix')
                    identities[binding.provider_symbol] = binding
            demands, stale, inputs = dict(self._demands), set(self._stale), []
            for reason, update in sorted(updates.items()):
                if type(update) is not dict or set(update) != {"revision", "symbols", "complete"}:
                    raise ValueError("invalid_demand_update")
                revision, symbols, complete = (update[k] for k in ("revision", "symbols", "complete"))
                if type(revision) is not int or revision <= 0 or type(complete) is not bool:
                    raise ValueError("invalid_demand_source")
                if symbols is None and complete:
                    raise ValueError("complete_demand_membership_required")
                prior = demands.get(reason, (0, frozenset()))
                if revision <= prior[0]:
                    raise ValueError("stale_demand_revision")
                observed = frozenset() if symbols is None else _symbols(symbols, bound=bound)
                requested = observed if complete else prior[1] | observed
                demands[reason] = (revision, requested)
                if complete:
                    stale.discard(reason)
                else:
                    stale.add(reason)
                inputs.append({"reason": reason, "revision": revision, "complete": complete,
                               "symbols": None if symbols is None else tuple(sorted(observed))})
            additions = set().union(*(members for _, members in demands.values())) - self._prefixes.keys()
            if len(self._prefixes) + len(additions) > self._max_symbols:
                raise ValueError("context_symbol_resource_capacity")
            new = {s: Prefix("iqfeed:"+s, f"{self._cursor.epoch}:{self._cursor.revision}:{s}",
                             self._limits) for s in sorted(additions)}
            if enrollment is not None:
                capsule = {'kind': 'native_enrollment', 'enrollment': enrollment}
            elif legacy:
                item = inputs[0]
                capsule = {"kind": "demand", **{k: item[k] for k in ("reason", "revision", "symbols")}}
            else:
                capsule = {"kind": "demand_batch", "updates": tuple(inputs)}
            try:
                self._prefixes.update(new)
                self._demands, self._stale = demands, stale
                if enrollment is not None:
                    self._native_enrollment = enrollment
                    if enrollment.reference.catalog_observed_ns is not None:
                        self._native_catalog_frontier = (enrollment.reference.catalog_observed_ns,
                                                        enrollment.reference.catalog_sha256)
                    self._native_bindings = identities
                    self._current_native_symbols = bound
                self._publish(self._snapshot.observed_frontier, self._snapshot.status,
                              self._snapshot.reason, None, capsule=capsule)
            except BaseException:
                # Never expose a private, partially committed membership on the
                # next pass if allocation/publication failed after staging.
                if not self._failed_commit:
                    self._failed_commit = True
                    self._snapshot = replace(self._snapshot, status="unresolved",
                                             reason="context_demand_commit_failed")
                raise

    def bind_replay_sink(self, sink):
        """Bind input/output persistence before any demand or source mutation."""
        with self._lock:
            if (self._failed_commit or self._sink is not None or self._replay_sink is not None
                    or self._demands or self._prefixes or self._snapshot.status != "cold"
                    or not callable(sink)):
                raise ValueError("replay_sink_requires_pristine_owner")
            self._replay_sink = sink
            capsule = {"kind": "init", "anchor": self._cursor, "limits": self._limits,
                       "max_symbols": self._max_symbols, "max_trade_rows": self._max_rows}
            try:
                sink(self._snapshot, capsule)
            except BaseException:
                self._failed_commit = True
                self._snapshot = replace(self._snapshot, status="unresolved",
                                         reason="context_publication_failed")
                raise

    def bind_publication_sink(self, sink):
        """Bind a durable publisher while cold, before any observation is exposed.

        The supplied publisher owns its cross-process fence. Publication failure
        makes this owner non-runnable rather than exposing an uncommitted view.
        """
        with self._lock:
            if (self._failed_commit or self._sink is not None or self._replay_sink is not None or not callable(sink)
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

    def demand_checkpoint(self) -> tuple[DemandCheckpoint, ...]:
        """Retained membership and revisions, including replayed no-op updates.

        A stale checkpoint includes retained members, not merely the most recent
        partial observation. It cannot be relabeled a complete broker inventory.
        """
        with self._lock:
            return tuple(DemandCheckpoint(reason, revision, tuple(sorted(symbols)),
                                         reason not in self._stale)
                         for reason, (revision, symbols) in sorted(self._demands.items()))

    def configuration(self):
        with self._lock:
            return MappingProxyType(dict(limits=self._limits, max_symbols=self._max_symbols,
                                         max_trade_rows=self._max_rows))

    def _publish(self, frontier, status, reason, results, *, capsule=None):
        views = []
        prior = {v.symbol: v for v in self._snapshot.symbols}
        for symbol, prefix in sorted(self._prefixes.items()):
            if symbol in prior and prior[symbol].print_count == prefix.count:
                # An empty source delta or demand change cannot alter geometry.
                # Reuse immutable scopes instead of re-querying every reference.
                scopes = prior[symbol].scopes
                evidence = prior[symbol].wave_evidence
            else:
                scopes = []
                for ref in prefix.active_references():
                    value = prefix.context(ref)
                    scopes.append(ScopeView(ref, value["end_index"], value["phase_bounds"],
                                            value["phase_mass"], value["whole_mass"]))
                scopes = tuple(scopes)
                evidence = wave_evidence(prefix)
            views.append(SymbolView(symbol,
                tuple(sorted(r for r, (_, members) in self._demands.items() if symbol in members)),
                prefix.prefix_sha256, prefix.count, prefix.tick(prefix.count-1) if prefix.count else None,
                scopes, (prior[symbol].events if symbol in prior else ()) if results is None
                else results[symbol].events if symbol in results else (),
                selected_parent_local='candidate_geometry' if prefix.wave_context is not None else 'not_yet_derived',
                wave_context=prefix.wave_context, wave_evidence=evidence,
                native_identity=self._native_bindings.get(symbol),
                native_binding_current=symbol in self._current_native_symbols))
        snapshot = ContextSnapshot(self._cursor, frontier, self._root, status, reason,
            tuple(views), tuple(sorted(self._stale)),
            native_enrollment=self._native_enrollment.reference if self._native_enrollment else None,
            native_mapping_gaps=self._native_enrollment.gaps if self._native_enrollment else ())
        if self._sink is not None or self._replay_sink is not None:
            try:
                if self._replay_sink is not None:
                    if capsule is None:
                        raise ValueError("context_replay_input_required")
                    self._replay_sink(snapshot, capsule)
                else:
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
            except (ValueError, TypeError, KeyError, sa.exc.SQLAlchemyError) as exc:
                code = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                self._publish(frontier, "unresolved", code, {}, capsule={
                    "kind": "read_failure", "frontier": frontier, "reason": code})
                return self._snapshot
            if not read.publications:
                return self._snapshot
            return self._apply_observation(read, self._clock_ns())

    def _apply_observation(self, read, known_ns):
        """Apply an actual read or its retained recovery capsule, using its clock."""
        with self._lock:
            if self._failed_commit:
                raise RuntimeError("context_owner_reconstruction_required")
            frontier = read.observed_frontier
            capsule = {"kind": "source", "read": read, "known_ns": known_ns}
            try:
                publication, = read.publications
                if (publication.cursor.epoch != self._cursor.epoch
                        or publication.cursor.revision != self._cursor.revision + 1
                        or read.consumed != publication.cursor
                        or frontier.epoch != self._cursor.epoch
                        or frontier.revision < publication.cursor.revision):
                    raise ValueError("context_source_read_cursor_invalid")
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
                self._publish(frontier, "unresolved", code, {}, capsule=capsule)
                return self._snapshot
            try:
                for symbol, stage in prepared.items():
                    result = self._prefixes[symbol].commit_frontier(stage)
                    if result.status != "applied":
                        raise RuntimeError("prepared_context_commit_failed")
                    results[symbol] = result
                self._cursor, self._root = publication.cursor, root
                self._publish(frontier, "observed_prefix", None, results, capsule=capsule)
            except BaseException:
                self._failed_commit = True
                self._snapshot = replace(self._snapshot, status="unresolved",
                                         reason="context_owner_reconstruction_required")
                raise
            return self._snapshot
