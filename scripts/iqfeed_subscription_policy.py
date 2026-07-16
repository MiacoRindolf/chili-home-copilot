"""Pure IQFeed subscription targeting and diagnostic lifecycle evidence.

This module deliberately has no database, socket, or provider dependencies.  The
two host bridges use it to resolve the same non-regressing target set before they
perform any I/O.  It is also the schema boundary for subscription lifecycle
evidence.  A bridge may log these records today, but logs are *not* a durable
ReplayV3 capture stream; production persistence must bind the records into the
sealed capture separately.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Mapping, Sequence


# These sets end at bailout deliberately. ``exited`` is confirmed-flat and
# ``cooldown`` is post-trade bookkeeping; a recycled session becomes protected
# again as soon as it transitions back to watching. Finished/cancelled/error
# states are terminal and never capture-active.
LIVE_ACTIVE_CAPTURE_STATES = frozenset(
    {
        "armed_pending_runner",
        "queued_live",
        "watching_live",
        "live_entry_candidate",
        "live_pending_entry",
        "live_entered",
        "live_scaling_out",
        "live_trailing",
        "live_bailout",
    }
)
PAPER_ACTIVE_CAPTURE_STATES = frozenset(
    {
        "queued",
        "watching",
        "entry_candidate",
        "pending_entry",
        "entered",
        "scaling_out",
        "trailing",
        "bailout",
    }
)


def _sql_string_list(values: Iterable[str]) -> str:
    # Values are module-owned constants, not user/provider input.
    return ",".join(f"'{value}'" for value in sorted(set(values)))


ACTIVE_EXECUTION_SESSION_SQL = (
    "SELECT DISTINCT symbol, mode, state FROM trading_automation_sessions "
    "WHERE symbol NOT LIKE '%-%' AND ("
    "(mode='live' AND state IN ("
    + _sql_string_list(LIVE_ACTIVE_CAPTURE_STATES)
    + ")) OR (mode='paper' AND state IN ("
    + _sql_string_list(PAPER_ACTIVE_CAPTURE_STATES)
    + "))) ORDER BY symbol, mode, state"
)


def is_active_capture_session(*, mode: str, state: str) -> bool:
    normalised_mode = str(mode or "").strip().lower()
    normalised_state = str(state or "").strip().lower()
    if normalised_mode == "live":
        return normalised_state in LIVE_ACTIVE_CAPTURE_STATES
    if normalised_mode == "paper":
        return normalised_state in PAPER_ACTIVE_CAPTURE_STATES
    return False


def active_capture_symbols(rows: Iterable[Sequence[object]]) -> tuple[str, ...]:
    """Fail closed over DB rows even if the SQL policy is later loosened."""
    symbols: list[str] = []
    for row in rows:
        if len(row) < 3:
            continue
        symbol, mode, state = row[0], row[1], row[2]
        if is_active_capture_session(mode=str(mode or ""), state=str(state or "")):
            symbols.append(str(symbol or ""))
    return _normalise_symbols(symbols)


class TargetCause(str, Enum):
    ACTIVE = "active"
    HINT = "hint"
    ELIGIBLE = "eligible"
    ROSS = "ross"
    FORCED = "forced"
    RETAINED = "retained"


_CAUSE_ORDER: tuple[TargetCause, ...] = (
    TargetCause.ACTIVE,
    TargetCause.HINT,
    TargetCause.ELIGIBLE,
    TargetCause.ROSS,
    TargetCause.FORCED,
    TargetCause.RETAINED,
)
REQUIRED_DYNAMIC_CAUSES = frozenset(
    {
        TargetCause.ACTIVE,
        TargetCause.HINT,
        TargetCause.ELIGIBLE,
        TargetCause.ROSS,
    }
)


def _normalise_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").strip().upper()
        if not symbol or "-" in symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return tuple(out)


@dataclass(frozen=True)
class SourceRead:
    """One source query result, preserving its deterministic ranking order."""

    cause: TargetCause
    symbols: tuple[str, ...]
    ok: bool
    error_code: str | None = None
    error_detail: str | None = None

    @classmethod
    def success(
        cls,
        cause: TargetCause,
        symbols: Iterable[str],
    ) -> "SourceRead":
        return cls(cause=cause, symbols=_normalise_symbols(symbols), ok=True)

    @classmethod
    def failure(
        cls,
        cause: TargetCause,
        *,
        error_code: str,
        error_detail: str | None = None,
    ) -> "SourceRead":
        code = str(error_code or "source_query_failed").strip()
        return cls(
            cause=cause,
            symbols=(),
            ok=False,
            error_code=code,
            error_detail=(str(error_detail)[:512] if error_detail else None),
        )


@dataclass(frozen=True)
class SymbolTarget:
    symbol: str
    causes: tuple[TargetCause, ...]


@dataclass(frozen=True)
class CoverageGap:
    code: str
    source: str
    symbol: str | None = None
    causes: tuple[TargetCause, ...] = ()
    detail: str | None = None


class SubscriptionConnectionIndeterminate(ConnectionError):
    """A watch command may have reached IQFeed only partially; reconnect required."""

    def __init__(self, gap: CoverageGap) -> None:
        super().__init__(
            f"{gap.code}: {gap.source} {gap.symbol or '-'} {gap.detail or ''}".strip()
        )
        self.gap = gap


@dataclass(frozen=True)
class TargetResolution:
    targets: tuple[SymbolTarget, ...]
    gaps: tuple[CoverageGap, ...]
    evicted_symbols: tuple[str, ...]
    retained_prior_on_failure: bool
    capacity: int

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(target.symbol for target in self.targets)

    @property
    def causes_by_symbol(self) -> dict[str, frozenset[TargetCause]]:
        return {
            target.symbol: frozenset(target.causes)
            for target in self.targets
        }

    @property
    def coverage_complete(self) -> bool:
        return not self.gaps


def require_complete_source_inventory(reads: Sequence[SourceRead]) -> None:
    """Reject a regression back to a session-only production target."""
    causes = {read.cause for read in reads}
    if causes == {TargetCause.FORCED}:
        return
    missing = REQUIRED_DYNAMIC_CAUSES - causes
    if missing:
        raise ValueError(
            "IQFeed dynamic target is missing sources: "
            + ",".join(sorted(cause.value for cause in missing))
        )


def resolve_subscription_target(
    *,
    reads: Sequence[SourceRead],
    prior_causes: Mapping[str, Iterable[TargetCause | str]] | None,
    capacity: int,
) -> TargetResolution:
    """Resolve a bounded target without silently dropping load-bearing coverage.

    Successful sources are combined as ``active | eligible | ross | hint`` while
    retaining every overlapping cause.  Active/forced symbols are non-evictable.
    If *any* source query fails, the complete prior watch set is also protected so
    an empty/error result can never be interpreted as an instruction to unwatch.

    Under capacity pressure validated fresh hints are selected before ranked
    eligible and cold Ross-fallback symbols.  Hints are still additive source
    evidence (never a replacement query): every displaced broad target is returned
    as explicit coverage-unavailable evidence, and a hint that overlaps a broad
    source retains both causes.
    """

    if int(capacity) < 0:
        raise ValueError("capacity must be non-negative")
    capacity = int(capacity)
    reads_by_cause: dict[TargetCause, SourceRead] = {}
    for read in reads:
        if read.cause in reads_by_cause:
            raise ValueError(f"duplicate source read: {read.cause.value}")
        reads_by_cause[read.cause] = read

    previous: dict[str, set[TargetCause]] = {}
    for raw_symbol, raw_causes in (prior_causes or {}).items():
        normalised = _normalise_symbols((raw_symbol,))
        if not normalised:
            continue
        parsed: set[TargetCause] = set()
        for raw_cause in raw_causes:
            try:
                parsed.add(
                    raw_cause
                    if isinstance(raw_cause, TargetCause)
                    else TargetCause(str(raw_cause))
                )
            except ValueError:
                parsed.add(TargetCause.RETAINED)
        previous[normalised[0]] = parsed or {TargetCause.RETAINED}

    desired: dict[str, set[TargetCause]] = {}
    ranked_by_cause: dict[TargetCause, tuple[str, ...]] = {}
    failures: list[SourceRead] = []
    for cause, read in reads_by_cause.items():
        if not read.ok:
            failures.append(read)
            ranked_by_cause[cause] = ()
            continue
        ranked_by_cause[cause] = read.symbols
        for symbol in read.symbols:
            desired.setdefault(symbol, set()).add(cause)

    retain_all_prior = bool(failures)
    if retain_all_prior:
        for symbol, causes in previous.items():
            desired.setdefault(symbol, set()).update(causes)

    protected: list[str] = []
    protected_seen: set[str] = set()

    def add_protected(symbols: Iterable[str]) -> None:
        for symbol in symbols:
            if symbol not in protected_seen:
                protected_seen.add(symbol)
                protected.append(symbol)

    add_protected(ranked_by_cause.get(TargetCause.ACTIVE, ()))
    add_protected(ranked_by_cause.get(TargetCause.FORCED, ()))
    if retain_all_prior:
        add_protected(sorted(previous))

    # Deterministic priority is deliberate: load-bearing active/held first,
    # newest-first fresh hints next, then viability-ranked eligible and finally
    # Ross broad fallback. Capacity losses are never silent.
    ranked: list[str] = list(protected)
    ranked_seen = set(ranked)
    for cause in _CAUSE_ORDER:
        for symbol in ranked_by_cause.get(cause, ()):
            if symbol not in ranked_seen:
                ranked_seen.add(symbol)
                ranked.append(symbol)

    selected: list[str] = list(protected)
    selected_seen = set(selected)
    for symbol in ranked:
        if symbol in selected_seen:
            continue
        if len(selected) >= capacity:
            continue
        selected_seen.add(symbol)
        selected.append(symbol)

    gaps: list[CoverageGap] = [
        CoverageGap(
            code="source_query_failed",
            source=read.cause.value,
            detail=f"{read.error_code}: {read.error_detail or ''}".rstrip(": "),
        )
        for read in failures
    ]
    if len(protected) > capacity:
        gaps.append(
            CoverageGap(
                code="protected_targets_exceed_capacity",
                source="resolver",
                detail=f"protected={len(protected)} capacity={capacity}",
            )
        )

    omitted = [symbol for symbol in ranked if symbol not in selected_seen]
    for symbol in omitted:
        causes = tuple(
            cause for cause in _CAUSE_ORDER if cause in desired.get(symbol, set())
        )
        gaps.append(
            CoverageGap(
                code="capacity_eviction",
                source="resolver",
                symbol=symbol,
                causes=causes,
                detail=f"capacity={capacity}",
            )
        )

    selected_targets = tuple(
        SymbolTarget(
            symbol=symbol,
            causes=tuple(
                cause
                for cause in _CAUSE_ORDER
                if cause in desired.get(symbol, {TargetCause.RETAINED})
            ),
        )
        for symbol in selected
    )
    evicted = tuple(sorted(set(previous) - selected_seen))
    return TargetResolution(
        targets=selected_targets,
        gaps=tuple(gaps),
        evicted_symbols=evicted,
        retained_prior_on_failure=retain_all_prior,
        capacity=capacity,
    )


class LifecycleStage(str, Enum):
    TARGET_EVALUATION = "target_evaluation"
    SEND_SUCCESS = "send_success"
    SEND_FAILURE = "send_failure"
    ACK_UNAVAILABLE = "ack_unavailable"
    FIRST_VALID_FRAME = "first_valid_frame"
    RECONNECT = "reconnect"
    LIMIT_EVICTION = "limit_eviction"
    GAP = "gap"


@dataclass(frozen=True)
class SubscriptionLifecycleRecord:
    """Typed content-addressable diagnostic record; never implies provider ACK."""

    schema_version: int
    feed: str
    run_id: str
    generation: int
    stage: LifecycleStage
    symbol: str | None
    causes: tuple[TargetCause, ...]
    parent_hashes: tuple[str, ...]
    recorded_at: datetime
    evaluated_at: datetime | None
    sent_at: datetime | None
    received_at: datetime | None
    available_at: datetime | None
    provider_event_at: datetime | None
    build_id: str
    config_hash: str
    detail_code: str | None
    detail: str | None
    provider_ack_state: str
    fidelity_claim: str
    coverage_state: str
    content_id: str

    @classmethod
    def create(
        cls,
        *,
        feed: str,
        run_id: str,
        generation: int,
        stage: LifecycleStage,
        build_id: str,
        config_hash: str,
        symbol: str | None = None,
        causes: Iterable[TargetCause | str] = (),
        parent_hashes: Iterable[str] = (),
        recorded_at: datetime | None = None,
        evaluated_at: datetime | None = None,
        sent_at: datetime | None = None,
        received_at: datetime | None = None,
        available_at: datetime | None = None,
        provider_event_at: datetime | None = None,
        detail_code: str | None = None,
        detail: str | None = None,
        coverage_state: str = "diagnostic_only",
    ) -> "SubscriptionLifecycleRecord":
        now = recorded_at or datetime.now(timezone.utc)
        if not str(feed).strip():
            raise ValueError("feed is required")
        if not str(run_id).strip():
            raise ValueError("run_id is required")
        if int(generation) < 0:
            raise ValueError("generation must be non-negative")
        if not str(build_id).strip() or not str(config_hash).strip():
            raise ValueError("build_id and config_hash are required")
        if stage is LifecycleStage.TARGET_EVALUATION and evaluated_at is None:
            raise ValueError("target_evaluation requires evaluated_at")
        if stage in {
            LifecycleStage.SEND_SUCCESS,
            LifecycleStage.SEND_FAILURE,
            LifecycleStage.ACK_UNAVAILABLE,
        } and sent_at is None:
            raise ValueError(f"{stage.value} requires sent_at")
        if stage is LifecycleStage.FIRST_VALID_FRAME and received_at is None:
            raise ValueError("first_valid_frame requires received_at")
        if stage in {
            LifecycleStage.SEND_FAILURE,
            LifecycleStage.LIMIT_EVICTION,
            LifecycleStage.GAP,
        } and coverage_state != "coverage_unavailable":
            raise ValueError(f"{stage.value} must be coverage_unavailable")
        clocks = (
            now,
            evaluated_at,
            sent_at,
            received_at,
            available_at,
            provider_event_at,
        )
        if any(clock is not None and clock.tzinfo is None for clock in clocks):
            raise ValueError("lifecycle clocks must be timezone-aware")
        parsed_causes = tuple(
            sorted(
                {
                    raw
                    if isinstance(raw, TargetCause)
                    else TargetCause(str(raw))
                    for raw in causes
                },
                key=lambda cause: _CAUSE_ORDER.index(cause),
            )
        )
        parents = tuple(sorted({str(value) for value in parent_hashes if value}))
        normalised_symbol = _normalise_symbols((symbol,)) if symbol else ()
        if symbol and not normalised_symbol:
            raise ValueError("symbol is not a supported equity symbol")
        payload = {
            "schema_version": 1,
            "feed": str(feed),
            "run_id": str(run_id),
            "generation": int(generation),
            "stage": stage.value,
            "symbol": (normalised_symbol[0] if normalised_symbol else None),
            "causes": [cause.value for cause in parsed_causes],
            "parent_hashes": list(parents),
            "recorded_at": now.isoformat(),
            "evaluated_at": evaluated_at.isoformat() if evaluated_at else None,
            "sent_at": sent_at.isoformat() if sent_at else None,
            "received_at": received_at.isoformat() if received_at else None,
            "available_at": available_at.isoformat() if available_at else None,
            "provider_event_at": (
                provider_event_at.isoformat() if provider_event_at else None
            ),
            "build_id": str(build_id),
            "config_hash": str(config_hash),
            "detail_code": str(detail_code) if detail_code else None,
            "detail": str(detail)[:1024] if detail else None,
            # IQFeed's watch commands do not provide a durable per-symbol ACK.
            "provider_ack_state": "unavailable",
            # A subscription receipt alone never certifies trade/NBBO or full L2.
            "fidelity_claim": "not_certified",
            "coverage_state": str(coverage_state),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        content_id = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        return cls(
            schema_version=1,
            feed=str(feed),
            run_id=str(run_id),
            generation=int(generation),
            stage=stage,
            symbol=payload["symbol"],
            causes=parsed_causes,
            parent_hashes=parents,
            recorded_at=now,
            evaluated_at=evaluated_at,
            sent_at=sent_at,
            received_at=received_at,
            available_at=available_at,
            provider_event_at=provider_event_at,
            build_id=str(build_id),
            config_hash=str(config_hash),
            detail_code=(str(detail_code) if detail_code else None),
            detail=(str(detail)[:1024] if detail else None),
            provider_ack_state="unavailable",
            fidelity_claim="not_certified",
            coverage_state=str(coverage_state),
            content_id=content_id,
        )

    def canonical_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "feed": self.feed,
            "run_id": self.run_id,
            "generation": self.generation,
            "stage": self.stage.value,
            "symbol": self.symbol,
            "causes": [cause.value for cause in self.causes],
            "parent_hashes": list(self.parent_hashes),
            "recorded_at": self.recorded_at.isoformat(),
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "provider_event_at": (
                self.provider_event_at.isoformat() if self.provider_event_at else None
            ),
            "build_id": self.build_id,
            "config_hash": self.config_hash,
            "detail_code": self.detail_code,
            "detail": self.detail,
            "provider_ack_state": self.provider_ack_state,
            "fidelity_claim": self.fidelity_claim,
            "coverage_state": self.coverage_state,
            "content_id": self.content_id,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
