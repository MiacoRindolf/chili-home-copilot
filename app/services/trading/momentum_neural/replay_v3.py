"""Deterministic Replay v3 harness for momentum sizing and scheduler policy tests.

Replay v3 has two deliberately separate layers:

* Explicit microbar tapes compare sizing/add/exit policies without hidden
  thresholds; the fixture supplies entry, confirmation, stop, and exit facts.
* Scheduler replay compares candidate priority, per-venue adapter availability,
  order/risk budgets, delayed candidates, and broker-outcome attribution from
  fixture or live-export snapshots.

The scheduler layer can certify starvation/priority mechanics when the replay
contains the matching evidence. It still does not claim scheduler-slot PnL
min/max unless the input includes multi-snapshot market-path opportunity labels
and complete missed-vs-taken broker outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping


class ReplayPolicy(str, Enum):
    """Sizing/add policies compared by Replay v3."""

    PROBE_ONLY = "probe_only"
    ADAPTIVE_STARTER_REMAINDER = "adaptive_starter_remainder"
    FULL_SIZE_SINGLE_ENTRY = "full_size_single_entry"


DEFAULT_POLICIES: tuple[ReplayPolicy, ...] = (
    ReplayPolicy.PROBE_ONLY,
    ReplayPolicy.ADAPTIVE_STARTER_REMAINDER,
    ReplayPolicy.FULL_SIZE_SINGLE_ENTRY,
)


@dataclass(frozen=True)
class SizingPlan:
    """Explicit sizing plan from the scheduler/risk layer."""

    full_qty: float
    probe_qty: float
    remainder_qty: float

    def validate(self) -> None:
        if self.full_qty <= 0:
            raise ValueError("full_qty must be positive")
        if self.probe_qty <= 0:
            raise ValueError("probe_qty must be positive")
        if self.remainder_qty < 0:
            raise ValueError("remainder_qty cannot be negative")
        if abs((self.probe_qty + self.remainder_qty) - self.full_qty) > 1e-9:
            raise ValueError("probe_qty + remainder_qty must equal full_qty")


@dataclass(frozen=True)
class ReplaySetup:
    """Trade setup values shared by all policies for a deterministic replay."""

    symbol: str
    entry_price: float
    stop_price: float
    sizing: SizingPlan

    def validate(self) -> None:
        self.sizing.validate()
        if self.entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if self.stop_price <= 0 or self.stop_price >= self.entry_price:
            raise ValueError("stop_price must be positive and below entry_price")


@dataclass(frozen=True)
class MicroBar:
    """One deterministic tape step.

    State flags are fixture facts, not thresholds:
    - entry_signal opens the trade at ReplaySetup.entry_price.
    - confirm_remainder allows the adaptive policy to add its remainder.
    - exit_signal closes the open quantity at exit_price or close.
    """

    ts: str
    open: float
    high: float
    low: float
    close: float
    entry_signal: bool = False
    confirm_remainder: bool = False
    exit_signal: bool = False
    confirmation_price: float | None = None
    exit_price: float | None = None
    state: str = ""

    def validate(self) -> None:
        values = (self.open, self.high, self.low, self.close)
        if any(v <= 0 for v in values):
            raise ValueError(f"{self.ts}: OHLC values must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(f"{self.ts}: high/low do not contain open/close")
        if self.confirmation_price is not None and self.confirmation_price <= 0:
            raise ValueError(f"{self.ts}: confirmation_price must be positive")
        if self.exit_price is not None and self.exit_price <= 0:
            raise ValueError(f"{self.ts}: exit_price must be positive")


@dataclass(frozen=True)
class ReplayEvent:
    ts: str
    event: str
    qty: float
    price: float
    realized_pnl_usd: float = 0.0
    state: str = ""


@dataclass(frozen=True)
class QuantityPoint:
    ts: str
    qty: float
    avg_entry_price: float | None
    realized_pnl_usd: float
    mark_pnl_usd: float
    state: str = ""


@dataclass(frozen=True)
class ReplayResult:
    symbol: str
    policy: ReplayPolicy
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    total_pnl_usd: float
    max_drawdown_usd: float
    mae_usd: float
    final_qty: float
    avg_entry_price: float | None
    quantity_path: list[QuantityPoint]
    events: list[ReplayEvent]
    exit_reason: str | None
    stop_exit_bounded: bool
    stop_loss_bound_usd: float

    @property
    def event_names(self) -> list[str]:
        return [event.event for event in self.events]

    @property
    def expectancy_usd_per_trade(self) -> float:
        return self.total_pnl_usd


@dataclass
class _Lot:
    qty: float
    price: float


@dataclass
class _ReplayBook:
    lots: list[_Lot] = field(default_factory=list)
    realized_pnl_usd: float = 0.0

    @property
    def qty(self) -> float:
        return sum(lot.qty for lot in self.lots)

    @property
    def avg_entry_price(self) -> float | None:
        qty = self.qty
        if qty <= 0:
            return None
        return sum(lot.qty * lot.price for lot in self.lots) / qty

    def open(self, *, qty: float, price: float) -> None:
        if qty <= 0:
            return
        self.lots.append(_Lot(qty=float(qty), price=float(price)))

    def unrealized(self, mark: float) -> float:
        return sum(lot.qty * (float(mark) - lot.price) for lot in self.lots)

    def close_all(self, *, price: float) -> float:
        pnl = self.unrealized(float(price))
        self.realized_pnl_usd += pnl
        self.lots.clear()
        return pnl

    def stop_loss_bound(self, stop_price: float) -> float:
        return sum(lot.qty * max(lot.price - float(stop_price), 0.0) for lot in self.lots)


def replay_policy(
    setup: ReplaySetup,
    tape: Iterable[MicroBar],
    policy: ReplayPolicy | str,
    *,
    close_open_at_end: bool = False,
) -> ReplayResult:
    """Replay one sizing/add policy over an explicit microbar tape."""

    setup.validate()
    bars = list(tape)
    if not bars:
        raise ValueError("tape must contain at least one MicroBar")
    for bar in bars:
        bar.validate()
    resolved_policy = ReplayPolicy(policy)

    book = _ReplayBook()
    events: list[ReplayEvent] = []
    quantity_path: list[QuantityPoint] = []
    equity_points: list[float] = [0.0]
    mae_usd = 0.0
    added_remainder = False
    exit_reason: str | None = None
    stop_exit_bounded = True
    stop_loss_bound_usd = 0.0

    def emit(ts: str, event: str, qty: float, price: float, realized: float = 0.0, state: str = "") -> None:
        events.append(
            ReplayEvent(
                ts=ts,
                event=event,
                qty=float(qty),
                price=float(price),
                realized_pnl_usd=float(realized),
                state=state,
            )
        )

    def record_path(bar: MicroBar) -> None:
        mark_pnl = book.realized_pnl_usd + book.unrealized(bar.close)
        equity_points.append(mark_pnl)
        quantity_path.append(
            QuantityPoint(
                ts=bar.ts,
                qty=book.qty,
                avg_entry_price=book.avg_entry_price,
                realized_pnl_usd=book.realized_pnl_usd,
                mark_pnl_usd=mark_pnl,
                state=bar.state,
            )
        )

    for bar in bars:
        if book.qty <= 0 and exit_reason is not None:
            record_path(bar)
            continue

        if book.qty <= 0 and bar.entry_signal:
            if resolved_policy == ReplayPolicy.FULL_SIZE_SINGLE_ENTRY:
                qty = setup.sizing.full_qty
                event = "full_entry"
            else:
                qty = setup.sizing.probe_qty
                event = "probe_entry"
            book.open(qty=qty, price=setup.entry_price)
            emit(bar.ts, event, qty, setup.entry_price, state=bar.state)

        if book.qty > 0:
            low_pnl = book.realized_pnl_usd + book.unrealized(bar.low)
            mae_usd = min(mae_usd, low_pnl)

            if bar.low <= setup.stop_price:
                bound = book.stop_loss_bound(setup.stop_price)
                pnl = book.close_all(price=setup.stop_price)
                exit_reason = "stop"
                stop_loss_bound_usd = max(stop_loss_bound_usd, bound)
                stop_exit_bounded = pnl >= -bound - 1e-9
                emit(bar.ts, "stop_exit", 0.0, setup.stop_price, pnl, state=bar.state)
                record_path(bar)
                continue

            if (
                resolved_policy == ReplayPolicy.ADAPTIVE_STARTER_REMAINDER
                and not added_remainder
                and bar.confirm_remainder
                and setup.sizing.remainder_qty > 0
            ):
                add_price = bar.confirmation_price if bar.confirmation_price is not None else bar.close
                book.open(qty=setup.sizing.remainder_qty, price=add_price)
                added_remainder = True
                emit(
                    bar.ts,
                    "confirmation_add",
                    setup.sizing.remainder_qty,
                    add_price,
                    state=bar.state,
                )

            if bar.exit_signal:
                exit_price = bar.exit_price if bar.exit_price is not None else bar.close
                qty_before = book.qty
                pnl = book.close_all(price=exit_price)
                exit_reason = "exit_signal"
                emit(bar.ts, "signal_exit", qty_before, exit_price, pnl, state=bar.state)

        record_path(bar)

    if book.qty > 0 and close_open_at_end:
        last = bars[-1]
        qty_before = book.qty
        pnl = book.close_all(price=last.close)
        exit_reason = "end_of_tape"
        emit(last.ts, "end_of_tape_exit", qty_before, last.close, pnl, state=last.state)
        record_path(last)

    unrealized_pnl = book.unrealized(bars[-1].close)
    total_pnl = book.realized_pnl_usd + unrealized_pnl
    peak = equity_points[0]
    max_drawdown = 0.0
    for point in equity_points:
        peak = max(peak, point)
        max_drawdown = max(max_drawdown, peak - point)

    return ReplayResult(
        symbol=setup.symbol,
        policy=resolved_policy,
        realized_pnl_usd=book.realized_pnl_usd,
        unrealized_pnl_usd=unrealized_pnl,
        total_pnl_usd=total_pnl,
        max_drawdown_usd=max_drawdown,
        mae_usd=mae_usd,
        final_qty=book.qty,
        avg_entry_price=book.avg_entry_price,
        quantity_path=quantity_path,
        events=events,
        exit_reason=exit_reason,
        stop_exit_bounded=stop_exit_bounded and (book.qty <= 1e-12 or setup.stop_price > 0),
        stop_loss_bound_usd=stop_loss_bound_usd,
    )


def replay_policies(
    setup: ReplaySetup,
    tape: Iterable[MicroBar],
    policies: Iterable[ReplayPolicy | str] = DEFAULT_POLICIES,
    *,
    close_open_at_end: bool = False,
) -> dict[ReplayPolicy, ReplayResult]:
    """Replay several sizing/add policies against the same setup and tape."""

    bars = tuple(tape)
    return {
        ReplayPolicy(policy): replay_policy(
            setup,
            bars,
            ReplayPolicy(policy),
            close_open_at_end=close_open_at_end,
        )
        for policy in policies
    }


def evidence_rows(results: dict[ReplayPolicy, ReplayResult]) -> list[dict[str, object]]:
    """Compact evidence rows for reports/tests."""

    rows: list[dict[str, object]] = []
    for policy in DEFAULT_POLICIES:
        if policy not in results:
            continue
        result = results[policy]
        rows.append(
            {
                "policy": policy.value,
                "realized_pnl_usd": round(result.realized_pnl_usd, 4),
                "unrealized_pnl_usd": round(result.unrealized_pnl_usd, 4),
                "total_pnl_usd": round(result.total_pnl_usd, 4),
                "expectancy_usd_per_trade": round(result.expectancy_usd_per_trade, 4),
                "max_drawdown_usd": round(result.max_drawdown_usd, 4),
                "mae_usd": round(result.mae_usd, 4),
                "qty_path": [round(point.qty, 8) for point in result.quantity_path],
                "events": result.event_names,
                "exit_reason": result.exit_reason,
                "stop_exit_bounded": result.stop_exit_bounded,
                "stop_loss_bound_usd": round(result.stop_loss_bound_usd, 4),
            }
        )
    return rows


def missed_winner_impact(
    results: dict[ReplayPolicy, ReplayResult],
    *,
    baseline: ReplayPolicy = ReplayPolicy.PROBE_ONLY,
    adaptive: ReplayPolicy = ReplayPolicy.ADAPTIVE_STARTER_REMAINDER,
    reference: ReplayPolicy = ReplayPolicy.FULL_SIZE_SINGLE_ENTRY,
) -> dict[str, float]:
    """Quantify how much upside the probe-only policy left uncaptured."""

    base_pnl = results[baseline].total_pnl_usd
    adaptive_pnl = results[adaptive].total_pnl_usd
    reference_pnl = results[reference].total_pnl_usd
    missed_vs_reference = reference_pnl - base_pnl
    reclaimed_by_adaptive = adaptive_pnl - base_pnl
    if missed_vs_reference > 0:
        reclaim_rate = reclaimed_by_adaptive / missed_vs_reference
    else:
        reclaim_rate = 0.0
    return {
        "baseline_missed_vs_reference_usd": round(missed_vs_reference, 4),
        "adaptive_reclaimed_vs_baseline_usd": round(reclaimed_by_adaptive, 4),
        "adaptive_remaining_gap_vs_reference_usd": round(reference_pnl - adaptive_pnl, 4),
        "adaptive_reclaim_rate": round(reclaim_rate, 6),
    }


@dataclass(frozen=True)
class ReplayVenueState:
    """Venue/execution-family availability for scheduler replay."""

    venue: str
    execution_family: str
    adapter_available: bool = True
    venue_enabled: bool = True
    order_call_budget: int = 1
    risk_budget_slots: int = 1


@dataclass(frozen=True)
class ReplaySchedulerCandidate:
    """One live-session candidate presented to the scheduler replay."""

    session_id: int
    symbol: str
    venue: str
    execution_family: str
    state: str
    quality_score: float | None = None
    queued_age_seconds: float | None = None
    expires_in_seconds: float | None = None
    tick_armed: bool = False
    terminalizable: bool = False
    expired: bool = False
    wrong_venue: bool = False
    expected_pnl_usd: float | None = None


@dataclass(frozen=True)
class ReplaySchedulerDecision:
    session_id: int
    symbol: str
    action: str
    reason: str
    consumes_capacity: bool
    priority_score: float


@dataclass(frozen=True)
class ReplaySchedulerBatchResult:
    selected_session_ids: list[int]
    decisions: list[ReplaySchedulerDecision]
    capacity_limit: int
    useful_capacity_used: int
    order_call_budget_used: int
    risk_budget_used: int
    free_skip_count: int
    missed_expected_pnl_usd: float


@dataclass(frozen=True)
class ReplaySchedulerTimelineStep:
    """One simulated scheduler tick with its own venue health and budgets."""

    ts: str
    candidates: tuple[ReplaySchedulerCandidate, ...]
    venue_states: tuple[ReplayVenueState, ...]
    capacity_limit: int | None = None
    order_call_budget: int | None = None
    risk_budget_slots: int | None = None


@dataclass(frozen=True)
class ReplaySchedulerLiveSnapshotStep:
    """DB-export-shaped scheduler tick for Replay v3.

    `rows` may be dicts or ORM-like `TradingAutomationSession` objects. The
    bridge extracts the same candidate fields as `replay_scheduler_candidates_from_live_rows`
    and then feeds the deterministic scheduler timeline.
    """

    ts: str
    rows: tuple[Any, ...]
    venue_states: tuple[ReplayVenueState, ...]
    capacity_limit: int | None = None
    order_call_budget: int | None = None
    risk_budget_slots: int | None = None


@dataclass(frozen=True)
class ReplaySchedulerTimelineStepResult:
    ts: str
    batch: ReplaySchedulerBatchResult


@dataclass(frozen=True)
class ReplaySchedulerTimelineResult:
    """Multi-tick scheduler replay evidence.

    `missed_expected_pnl_usd` is counted once at terminal/expired end state, not
    every time a candidate is delayed by a per-tick budget. This is the key
    distinction from a single batch: a delayed candidate is not a missed trade
    if a later scheduler tick can still evaluate it.
    """

    steps: list[ReplaySchedulerTimelineStepResult]
    selected_session_ids: list[int]
    terminalized_session_ids: list[int]
    pending_session_ids: list[int]
    selected_expected_pnl_usd: float
    selected_expected_pnl_by_session: dict[int, float]
    missed_expected_pnl_usd: float
    open_expected_pnl_usd: float
    skipped_expected_pnl_by_reason: dict[str, float]
    decision_trace: dict[int, list[dict[str, Any]]]


@dataclass(frozen=True)
class ReplayBrokerOutcome:
    """Broker/fill outcome for one replayed scheduler session."""

    session_id: int
    status: str
    realized_pnl_usd: float | None = None
    reject_reason: str | None = None
    entry_fill_price: float | None = None
    exit_fill_price: float | None = None
    filled_qty: float | None = None


@dataclass(frozen=True)
class ReplaySchedulerPnLAttribution:
    selected_session_ids: list[int]
    realized_session_ids: list[int]
    rejected_session_ids: list[int]
    no_fill_session_ids: list[int]
    selected_without_outcome_ids: list[int]
    realized_pnl_usd: float
    selected_expected_pnl_usd: float
    missed_expected_pnl_usd: float
    rejected_expected_pnl_usd: float
    no_fill_expected_pnl_usd: float
    open_expected_pnl_usd: float
    realized_vs_selected_expected_usd: float
    outcome_trace: dict[int, dict[str, Any]]


def _mapping_or_attr(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _replay_live_exec_from_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        return {}
    live_exec = snapshot.get("momentum_live_execution")
    return dict(live_exec) if isinstance(live_exec, Mapping) else {}


def _replay_float(raw: Any) -> float | None:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val or val in (float("inf"), float("-inf")):
        return None
    return val


def _replay_first_float(*values: Any) -> float | None:
    for raw in values:
        if raw is None:
            continue
        val = _replay_float(raw)
        if val is not None:
            return val
    return None


def _replay_payload(row: Any) -> dict[str, Any]:
    raw = _mapping_or_attr(row, "payload_json", None)
    if raw is None and isinstance(row, Mapping):
        raw = row.get("payload") or row.get("payload_json")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _replay_snapshot(row: Any) -> dict[str, Any]:
    raw = _mapping_or_attr(row, "risk_snapshot_json", None)
    if raw is None and isinstance(row, Mapping):
        raw = row.get("snapshot") or row.get("risk_snapshot_json")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _replay_parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _replay_quality_from_snapshot(snapshot: Any) -> float | None:
    if not isinstance(snapshot, Mapping):
        return None
    live_exec = _replay_live_exec_from_snapshot(snapshot)
    candidates = (
        live_exec.get("admission_viability_score"),
        snapshot.get("viability_score"),
        (snapshot.get("momentum_risk") or {}).get("viability_score")
        if isinstance(snapshot.get("momentum_risk"), Mapping)
        else None,
        (snapshot.get("momentum_risk_snapshot") or {}).get("viability_score")
        if isinstance(snapshot.get("momentum_risk_snapshot"), Mapping)
        else None,
    )
    for raw in candidates:
        val = _replay_float(raw)
        if val is not None:
            return val
    return None


def replay_scheduler_candidates_from_live_rows(
    rows: Iterable[Any],
    *,
    now: datetime | str | None = None,
) -> list[ReplaySchedulerCandidate]:
    """Convert DB/session-shaped rows into scheduler replay candidates.

    This is the DB-backed bridge Replay v3 lacked: tests can feed real-ish
    `TradingAutomationSession` rows or dict snapshots, and the replay extracts
    the same evidence family the live scheduler uses: quality, queue age, expiry
    urgency, and tick-arm state. It still performs no broker calls and does not
    mutate rows.
    """

    now_dt = _replay_parse_dt(now) or datetime.now(timezone.utc)
    out: list[ReplaySchedulerCandidate] = []
    for row in rows:
        snapshot = _mapping_or_attr(row, "risk_snapshot_json", {}) or {}
        if not isinstance(snapshot, Mapping):
            snapshot = {}
        live_exec = _replay_live_exec_from_snapshot(snapshot)
        sid = int(_mapping_or_attr(row, "id", _mapping_or_attr(row, "session_id", 0)) or 0)
        symbol = str(_mapping_or_attr(row, "symbol", "") or "")
        venue = str(_mapping_or_attr(row, "venue", "") or "")
        execution_family = str(_mapping_or_attr(row, "execution_family", "") or "")
        state = str(_mapping_or_attr(row, "state", "") or "")

        created = _replay_parse_dt(
            _mapping_or_attr(row, "started_at", None)
            or _mapping_or_attr(row, "created_at", None)
            or _mapping_or_attr(row, "updated_at", None)
        )
        queued_age = None
        if created is not None:
            queued_age = max(0.0, (now_dt - created).total_seconds())

        expires = _replay_parse_dt(snapshot.get("expires_at_utc") or live_exec.get("expires_at_utc"))
        expires_in = None
        if expires is not None:
            expires_in = (expires - now_dt).total_seconds()

        expected_pnl = _replay_first_float(
            live_exec.get("expected_pnl_usd"),
            snapshot.get("expected_pnl_usd"),
            snapshot.get("missed_expected_pnl_usd"),
        )

        out.append(
            ReplaySchedulerCandidate(
                session_id=sid,
                symbol=symbol,
                venue=venue,
                execution_family=execution_family,
                state=state,
                quality_score=_replay_quality_from_snapshot(snapshot),
                queued_age_seconds=queued_age,
                expires_in_seconds=expires_in,
                tick_armed=bool(live_exec.get("watch_break_level") is not None or state == "watch_break_level"),
                terminalizable=bool(live_exec.get("terminalizable") or snapshot.get("terminalizable")),
                expired=bool(expires_in is not None and expires_in <= 0),
                wrong_venue=bool(live_exec.get("wrong_venue") or snapshot.get("wrong_venue")),
                expected_pnl_usd=expected_pnl,
            )
        )
    return out


def _rank_percentiles(values: dict[int, float], *, reverse: bool = False) -> dict[int, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=reverse)
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    denom = float(len(ordered) - 1)
    return {sid: 1.0 - (idx / denom) for idx, (sid, _value) in enumerate(ordered)}


def _scheduler_priority_scores(candidates: list[ReplaySchedulerCandidate]) -> dict[int, float]:
    quality = {
        c.session_id: float(c.quality_score)
        for c in candidates
        if c.quality_score is not None
    }
    age = {
        c.session_id: float(c.queued_age_seconds)
        for c in candidates
        if c.queued_age_seconds is not None
    }
    urgency = {
        c.session_id: float(c.expires_in_seconds)
        for c in candidates
        if c.expires_in_seconds is not None
    }
    q_rank = _rank_percentiles(quality, reverse=True)
    age_rank = _rank_percentiles(age, reverse=True)
    urgency_rank = _rank_percentiles(urgency, reverse=False)
    out: dict[int, float] = {}
    for c in candidates:
        pieces: list[float] = []
        if c.session_id in q_rank:
            pieces.append(q_rank[c.session_id])
        if c.session_id in age_rank:
            pieces.append(age_rank[c.session_id])
        if c.session_id in urgency_rank:
            pieces.append(urgency_rank[c.session_id])
        pieces.append(1.0 if c.tick_armed else 0.0)
        out[c.session_id] = sum(pieces) / float(len(pieces)) if pieces else 0.0
    return out


def _scheduler_phase_order(candidate: ReplaySchedulerCandidate) -> int:
    """Mirror live batch FSM priority before adaptive score tie-breaks.

    The live runner protects already-managed risk before new-entry work. Replay
    must model that phase order, otherwise a high-quality queued setup can look
    like it should outrank a lower-scored held position that needs exit/add/stop
    management. Within the same phase, the adaptive percentile score still owns
    quality/age/urgency/tick-arm ordering.
    """
    state = str(candidate.state or "").strip().lower()
    if state in {"live_entered", "live_scaling_out", "live_trailing", "live_bailout"}:
        return 0
    if state == "live_pending_entry":
        return 1
    if bool(candidate.tick_armed) or state == "watch_break_level":
        return 2
    if state == "queued_live":
        return 3
    if state == "live_entry_candidate":
        return 4
    if state == "watching_live":
        return 5
    return 6


def replay_scheduler_batch(
    candidates: Iterable[ReplaySchedulerCandidate],
    venue_states: Iterable[ReplayVenueState],
    *,
    capacity_limit: int,
    order_call_budget: int | None = None,
    risk_budget_slots: int | None = None,
) -> ReplaySchedulerBatchResult:
    """Replay useful scheduler capacity without broker calls.

    The model is self-normalizing: priority is the mean of available percentile
    ranks for setup quality, queue age, expiry urgency, and tick-arm state.
    Adapter-disabled, wrong-venue, expired, and pre-entry terminal rows are free
    skips before useful capacity is charged.
    """

    limit = max(0, int(capacity_limit))
    global_order_budget = limit if order_call_budget is None else max(0, int(order_call_budget))
    global_risk_budget = limit if risk_budget_slots is None else max(0, int(risk_budget_slots))
    rows = list(candidates)
    venues = {
        (str(v.venue).lower(), str(v.execution_family).lower()): v
        for v in venue_states
    }
    scores = _scheduler_priority_scores(rows)
    decisions: list[ReplaySchedulerDecision] = []
    selected: list[int] = []
    venue_order_used: dict[tuple[str, str], int] = {}
    venue_risk_used: dict[tuple[str, str], int] = {}
    order_used = 0
    risk_used = 0
    free_skips = 0
    missed_pnl = 0.0

    def decide(c: ReplaySchedulerCandidate, action: str, reason: str, consumes: bool) -> None:
        decisions.append(
            ReplaySchedulerDecision(
                session_id=int(c.session_id),
                symbol=str(c.symbol),
                action=action,
                reason=reason,
                consumes_capacity=bool(consumes),
                priority_score=round(scores.get(c.session_id, 0.0), 6),
            )
        )

    ordered = sorted(
        rows,
        key=lambda c: (
            _scheduler_phase_order(c),
            -scores.get(c.session_id, 0.0),
            c.session_id,
        ),
    )
    for c in ordered:
        key = (str(c.venue).lower(), str(c.execution_family).lower())
        venue_state = venues.get(key)
        if c.expired or c.terminalizable:
            free_skips += 1
            decide(c, "skipped", "pre_entry_terminal", False)
            continue
        if c.wrong_venue:
            free_skips += 1
            decide(c, "skipped", "wrong_venue", False)
            continue
        if venue_state is not None and not venue_state.venue_enabled:
            free_skips += 1
            decide(c, "skipped", "venue_disabled", False)
            continue
        if venue_state is not None and not venue_state.adapter_available:
            free_skips += 1
            decide(c, "skipped", "venue_adapter_unavailable", False)
            continue
        if len(selected) >= limit:
            missed_pnl += max(0.0, float(c.expected_pnl_usd or 0.0))
            decide(c, "skipped", "capacity_exhausted", False)
            continue
        venue_order_limit = (
            max(0, int(venue_state.order_call_budget))
            if venue_state is not None
            else global_order_budget
        )
        venue_risk_limit = (
            max(0, int(venue_state.risk_budget_slots))
            if venue_state is not None
            else global_risk_budget
        )
        if order_used >= global_order_budget or venue_order_used.get(key, 0) >= venue_order_limit:
            missed_pnl += max(0.0, float(c.expected_pnl_usd or 0.0))
            decide(c, "skipped", "order_call_budget_exhausted", False)
            continue
        if risk_used >= global_risk_budget or venue_risk_used.get(key, 0) >= venue_risk_limit:
            missed_pnl += max(0.0, float(c.expected_pnl_usd or 0.0))
            decide(c, "skipped", "risk_budget_exhausted", False)
            continue
        selected.append(int(c.session_id))
        order_used += 1
        risk_used += 1
        venue_order_used[key] = venue_order_used.get(key, 0) + 1
        venue_risk_used[key] = venue_risk_used.get(key, 0) + 1
        decide(c, "selected", "selected", True)

    return ReplaySchedulerBatchResult(
        selected_session_ids=selected,
        decisions=decisions,
        capacity_limit=limit,
        useful_capacity_used=len(selected),
        order_call_budget_used=order_used,
        risk_budget_used=risk_used,
        free_skip_count=free_skips,
        missed_expected_pnl_usd=round(missed_pnl, 4),
    )


def replay_scheduler_timeline(
    steps: Iterable[ReplaySchedulerTimelineStep],
    *,
    default_capacity_limit: int,
    default_order_call_budget: int | None = None,
    default_risk_budget_slots: int | None = None,
) -> ReplaySchedulerTimelineResult:
    """Replay scheduler priority over simulated time.

    The one-batch replay answers "who should this tick evaluate?" This timeline
    wrapper answers the operational question CHILI needs for continuous
    development: whether delayed candidates remain eligible, whether unavailable
    venues starve later rows, and whether expected PnL was actually missed or
    merely deferred until a later tick.
    """

    selected: set[int] = set()
    terminalized: set[int] = set()
    selected_order: list[int] = []
    terminal_order: list[int] = []
    last_seen: dict[int, ReplaySchedulerCandidate] = {}
    trace: dict[int, list[dict[str, Any]]] = {}
    selected_pnl = 0.0
    selected_pnl_by_session: dict[int, float] = {}
    missed_pnl = 0.0
    skipped_pnl_by_reason: dict[str, float] = {}
    step_results: list[ReplaySchedulerTimelineStepResult] = []

    def expected(c: ReplaySchedulerCandidate | None) -> float:
        if c is None:
            return 0.0
        try:
            val = float(c.expected_pnl_usd or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, val) if val == val and val != float("inf") else 0.0

    def mark_terminal(c: ReplaySchedulerCandidate, reason: str) -> None:
        nonlocal missed_pnl
        sid = int(c.session_id)
        if sid in selected or sid in terminalized:
            return
        terminalized.add(sid)
        terminal_order.append(sid)
        pnl = expected(c)
        missed_pnl += pnl
        skipped_pnl_by_reason[reason] = skipped_pnl_by_reason.get(reason, 0.0) + pnl

    for step in steps:
        active: list[ReplaySchedulerCandidate] = []
        for c in step.candidates:
            sid = int(c.session_id)
            last_seen[sid] = c
            if sid in selected or sid in terminalized:
                continue
            active.append(c)

        batch = replay_scheduler_batch(
            active,
            step.venue_states,
            capacity_limit=(
                default_capacity_limit
                if step.capacity_limit is None
                else int(step.capacity_limit)
            ),
            order_call_budget=(
                default_order_call_budget
                if step.order_call_budget is None
                else int(step.order_call_budget)
            ),
            risk_budget_slots=(
                default_risk_budget_slots
                if step.risk_budget_slots is None
                else int(step.risk_budget_slots)
            ),
        )
        step_results.append(ReplaySchedulerTimelineStepResult(ts=step.ts, batch=batch))
        by_id = {int(c.session_id): c for c in active}
        for decision in batch.decisions:
            sid = int(decision.session_id)
            c = by_id.get(sid)
            trace.setdefault(sid, []).append(
                {
                    "ts": step.ts,
                    "action": decision.action,
                    "reason": decision.reason,
                    "consumes_capacity": decision.consumes_capacity,
                    "priority_score": decision.priority_score,
                }
            )
            if c is None:
                continue
            if decision.reason == "selected":
                selected.add(sid)
                selected_order.append(sid)
                exp = expected(c)
                selected_pnl += exp
                selected_pnl_by_session[sid] = exp
                continue
            if c.expired or c.terminalizable:
                mark_terminal(c, decision.reason)

    pending: list[int] = []
    open_pnl = 0.0
    for sid, c in sorted(last_seen.items()):
        if sid in selected or sid in terminalized:
            continue
        if c.expired or c.terminalizable:
            mark_terminal(c, "terminal_at_end")
        else:
            pending.append(sid)
            open_pnl += expected(c)

    return ReplaySchedulerTimelineResult(
        steps=step_results,
        selected_session_ids=selected_order,
        terminalized_session_ids=terminal_order,
        pending_session_ids=pending,
        selected_expected_pnl_usd=round(selected_pnl, 4),
        selected_expected_pnl_by_session={
            sid: round(pnl, 4)
            for sid, pnl in sorted(selected_pnl_by_session.items())
        },
        missed_expected_pnl_usd=round(missed_pnl, 4),
        open_expected_pnl_usd=round(open_pnl, 4),
        skipped_expected_pnl_by_reason={
            reason: round(pnl, 4)
            for reason, pnl in sorted(skipped_pnl_by_reason.items())
        },
        decision_trace=trace,
    )


def replay_scheduler_timeline_from_live_snapshots(
    steps: Iterable[ReplaySchedulerLiveSnapshotStep],
    *,
    default_capacity_limit: int,
    default_order_call_budget: int | None = None,
    default_risk_budget_slots: int | None = None,
) -> ReplaySchedulerTimelineResult:
    """Replay multi-tick scheduler behavior from DB-shaped live snapshots."""

    timeline_steps: list[ReplaySchedulerTimelineStep] = []
    for step in steps:
        candidates = tuple(
            replay_scheduler_candidates_from_live_rows(step.rows, now=step.ts)
        )
        timeline_steps.append(
            ReplaySchedulerTimelineStep(
                ts=step.ts,
                candidates=candidates,
                venue_states=step.venue_states,
                capacity_limit=step.capacity_limit,
                order_call_budget=step.order_call_budget,
                risk_budget_slots=step.risk_budget_slots,
            )
        )
    return replay_scheduler_timeline(
        timeline_steps,
        default_capacity_limit=default_capacity_limit,
        default_order_call_budget=default_order_call_budget,
        default_risk_budget_slots=default_risk_budget_slots,
    )


def replay_broker_outcomes_from_rows(rows: Iterable[Any]) -> list[ReplayBrokerOutcome]:
    """Normalize exported event/session/fill rows into broker outcome fixtures."""

    out: list[ReplayBrokerOutcome] = []
    for row in rows:
        payload = _replay_payload(row)
        snapshot = _replay_snapshot(row)
        live_exec = _replay_live_exec_from_snapshot(snapshot)
        sid_raw = (
            _mapping_or_attr(row, "session_id", None)
            or _mapping_or_attr(row, "id", None)
            or payload.get("session_id")
        )
        try:
            sid = int(sid_raw)
        except (TypeError, ValueError):
            continue

        status = str(
            _mapping_or_attr(row, "status", None)
            or _mapping_or_attr(row, "outcome_status", None)
            or _mapping_or_attr(row, "broker_status", None)
            or payload.get("status")
            or payload.get("outcome_status")
            or payload.get("broker_status")
            or _mapping_or_attr(row, "event_type", "")
            or ""
        ).lower()
        if status in {"live_exit_filled", "paper_exit_filled", "exit_fill", "filled", "success"}:
            status = "filled"
        elif status in {"live_entry_cancelled", "cancelled", "canceled"}:
            status = "cancelled"
        elif status in {"reject", "rejected", "failed", "broker_rejected"}:
            status = "rejected"
        elif status in {"no_fill", "zero_fill"}:
            status = "no_fill"
        elif status in {"open", "working", "queued", "submitted"}:
            status = "open"
        if not status:
            status = "unknown"

        realized = _replay_first_float(
            _mapping_or_attr(row, "realized_pnl_usd", None),
            payload.get("realized_pnl_usd"),
            live_exec.get("realized_pnl_usd"),
        )
        entry = _replay_first_float(
            _mapping_or_attr(row, "entry_fill_price", None),
            payload.get("entry_fill_price"),
            payload.get("entry_price"),
            live_exec.get("avg_entry_price"),
        )
        exit_px = _replay_first_float(
            _mapping_or_attr(row, "exit_fill_price", None),
            payload.get("exit_fill_price"),
            payload.get("exit_price"),
            live_exec.get("last_exit_price"),
        )
        qty = _replay_first_float(
            _mapping_or_attr(row, "filled_qty", None),
            _mapping_or_attr(row, "filled_quantity", None),
            payload.get("filled_qty"),
            payload.get("filled_quantity"),
            payload.get("qty"),
        )
        out.append(
            ReplayBrokerOutcome(
                session_id=sid,
                status=status,
                realized_pnl_usd=realized,
                reject_reason=(
                    str(payload.get("reason") or payload.get("error") or "")
                    or None
                ),
                entry_fill_price=entry,
                exit_fill_price=exit_px,
                filled_qty=qty,
            )
        )
    return out


def attribute_scheduler_timeline_pnl(
    timeline: ReplaySchedulerTimelineResult,
    outcomes: Iterable[ReplayBrokerOutcome],
) -> ReplaySchedulerPnLAttribution:
    """Attribute replay scheduler selections to broker/fill outcomes."""

    selected = [int(sid) for sid in timeline.selected_session_ids]
    outcome_by_id = {int(o.session_id): o for o in outcomes}
    expected_by_id = {
        int(sid): float(pnl)
        for sid, pnl in getattr(timeline, "selected_expected_pnl_by_session", {}).items()
    }

    realized_ids: list[int] = []
    rejected_ids: list[int] = []
    no_fill_ids: list[int] = []
    missing_ids: list[int] = []
    realized_pnl = 0.0
    rejected_expected = 0.0
    no_fill_expected = 0.0
    open_expected = 0.0
    trace_out: dict[int, dict[str, Any]] = {}

    for sid in selected:
        outcome = outcome_by_id.get(sid)
        if outcome is None:
            missing_ids.append(sid)
            trace_out[sid] = {"status": "missing_outcome"}
            continue
        expected = expected_by_id.get(sid, 0.0)
        if outcome.status == "filled":
            realized_ids.append(sid)
            realized = float(outcome.realized_pnl_usd or 0.0)
            realized_pnl += realized
            trace_out[sid] = {
                "status": "filled",
                "realized_pnl_usd": round(realized, 4),
                "entry_fill_price": outcome.entry_fill_price,
                "exit_fill_price": outcome.exit_fill_price,
                "filled_qty": outcome.filled_qty,
            }
        elif outcome.status in {"rejected", "cancelled"}:
            rejected_ids.append(sid)
            rejected_expected += expected
            trace_out[sid] = {
                "status": outcome.status,
                "reject_reason": outcome.reject_reason,
                "expected_pnl_usd": round(expected, 4),
            }
        elif outcome.status == "no_fill":
            no_fill_ids.append(sid)
            no_fill_expected += expected
            trace_out[sid] = {"status": "no_fill", "expected_pnl_usd": round(expected, 4)}
        else:
            missing_ids.append(sid)
            open_expected += expected
            trace_out[sid] = {"status": outcome.status or "unknown", "expected_pnl_usd": round(expected, 4)}

    return ReplaySchedulerPnLAttribution(
        selected_session_ids=selected,
        realized_session_ids=realized_ids,
        rejected_session_ids=rejected_ids,
        no_fill_session_ids=no_fill_ids,
        selected_without_outcome_ids=missing_ids,
        realized_pnl_usd=round(realized_pnl, 4),
        selected_expected_pnl_usd=round(float(timeline.selected_expected_pnl_usd), 4),
        missed_expected_pnl_usd=round(float(timeline.missed_expected_pnl_usd), 4),
        rejected_expected_pnl_usd=round(rejected_expected, 4),
        no_fill_expected_pnl_usd=round(no_fill_expected, 4),
        open_expected_pnl_usd=round(float(timeline.open_expected_pnl_usd) + open_expected, 4),
        realized_vs_selected_expected_usd=round(realized_pnl - float(timeline.selected_expected_pnl_usd), 4),
        outcome_trace=trace_out,
    )
