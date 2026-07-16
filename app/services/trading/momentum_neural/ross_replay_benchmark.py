"""Phase-aware Ross recap labels and causal CHILI replay metrics.

Recaps are after-the-fact labels only.  They may grade a replay decision, but
must never be injected as an event-time scanner/news/admission signal.  Price
path metrics use executable sides of the recorded quote (ask entry, bid exit)
and stop at the simulated exit instant so later highs cannot improve a score.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Literal, Mapping, Sequence


ExpectedAction = Literal["trade", "reject"]
ActualAction = Literal["trade", "reject", "miss", "unavailable"]


@dataclass(frozen=True)
class TradablePathPoint:
    ts: datetime
    bid: float
    ask: float

    def validate(self) -> None:
        if not isinstance(self.ts, datetime):
            raise ValueError("path timestamp is required")
        if not all(math.isfinite(float(value)) for value in (self.bid, self.ask)):
            raise ValueError("path prices must be finite")
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("path quote must be positive and uncrossed")


@dataclass(frozen=True)
class LongTradePathMetrics:
    entry_ts: datetime
    exit_ts: datetime
    entry_fill_price: float
    exit_fill_price: float
    peak_executable_bid: float
    peak_ts: datetime
    trough_executable_bid: float
    gross_pnl_usd: float
    peak_open_profit_usd: float
    open_profit_giveback_usd: float
    open_profit_giveback_fraction: float | None
    realized_mfe_capture_ratio: float | None
    mfe_r: float | None
    mae_r: float | None
    seconds_to_peak: float
    peak_to_exit_seconds: float
    path_points_used: int


@dataclass(frozen=True)
class EventTimeVetoEvidence:
    """Independent, provenance-certified evidence available by the decision."""

    reason: str
    source: str
    observed_at: datetime
    provenance_certified: bool

    def valid_at(self, decision_ts: datetime | None) -> bool:
        if decision_ts is None:
            return False
        if self.observed_at.tzinfo is None or decision_ts.tzinfo is None:
            return False
        return bool(
            self.provenance_certified
            and str(self.reason or "").strip()
            and str(self.source or "").strip()
            and self.observed_at <= decision_ts
        )


@dataclass(frozen=True)
class RecapDecisionGrade:
    expected_action: ExpectedAction
    actual_action: ActualAction
    status: Literal[
        "matched_trade",
        "matched_reject",
        "valid_veto",
        "missed_profitable_setup",
        "false_positive_trade",
        "wrong_phase_trade",
        "unmatched_trade_outcome",
        "unscorable",
    ]
    credit: float | None
    reason: str


@dataclass(frozen=True)
class ValidatedPhaseWindow:
    """Exact after-the-fact grading window, never a strategy input.

    Approximate times copied from a recap are intentionally insufficient.  A
    window becomes usable only after its boundaries are independently checked
    against recorded market/broker evidence and explicitly marked as grading-
    only.  This prevents a hindsight label from entering CHILI's event-time
    scanner or admission path.
    """

    label_id: str
    symbol: str
    start_ts: datetime
    end_ts: datetime
    decision_ts: datetime
    evidence_source: str
    evidence_role: Literal["after_fact_grading_only"]
    independently_verified: bool

    def valid_for(self, *, label_id: str, symbol: str) -> bool:
        values = (self.start_ts, self.end_ts, self.decision_ts)
        if any(value.tzinfo is None for value in values):
            return False
        return bool(
            self.independently_verified
            and self.evidence_role == "after_fact_grading_only"
            and str(self.evidence_source or "").strip()
            and self.label_id == label_id
            and self.symbol.strip().upper() == symbol.strip().upper()
            and self.start_ts <= self.decision_ts <= self.end_ts
        )


@dataclass(frozen=True)
class ValidatedReplayCoverage:
    """Legacy diagnostic description of a recap phase's replay coverage.

    A phase label and a replay result answer different questions.  The label
    identifies the after-the-fact market phase to grade; this record proves
    that the replay actually observed the inputs needed to make and manage a
    decision throughout that phase.  Neither record is accepted by CHILI's
    scanner or entry path.

    These booleans are not, by themselves, certification evidence.  Until each
    replay trade/decision is bound to an exact sealed capture identity, final
    seal SHA, checkpoint and coverage grade, this record must remain
    unscorable.  ``coverage_start_ts``/``coverage_end_ts`` bound all recorded inputs,
    including warmup and post-entry management.  ``decision_start_ts`` and
    ``decision_end_ts`` bound the interval in which candidate evaluation is
    proven continuous.  Boolean assertions are intentionally explicit so a
    sampled legacy tape, a receipt-only clock, or an unbounded late stream
    cannot silently earn benchmark credit.
    """

    label_id: str
    symbol: str
    coverage_start_ts: datetime
    decision_start_ts: datetime
    decision_end_ts: datetime
    coverage_end_ts: datetime
    evidence_source: str
    evidence_role: Literal["after_fact_replay_grading_only"]
    independently_verified: bool
    uncapped: bool
    warmup_complete: bool
    continuous_quote_coverage: bool
    continuous_trade_coverage: bool
    causal_provenance_enforced: bool
    provider_watermark_proven: bool
    bounded_lateness_proven: bool
    exact_quote_event_clock: bool
    exact_trade_event_clock: bool
    required_event_time_inputs_complete: bool

    def failure_reasons(
        self,
        *,
        label_id: str,
        symbol: str,
        phase_start_ts: datetime,
        phase_end_ts: datetime,
        required_coverage_end_ts: datetime,
    ) -> tuple[str, ...]:
        # This legacy record has no exact decision-checkpoint/final-seal binding.
        # Keep it useful for diagnostics while preventing asserted booleans from
        # manufacturing Ross benchmark credit.
        reasons: list[str] = ["sealed_decision_coverage_not_bound"]
        sym = str(symbol or "").strip().upper()
        if self.label_id != label_id:
            reasons.append("coverage_label_mismatch")
        if self.symbol.strip().upper() != sym:
            reasons.append("coverage_symbol_mismatch")
        if not self.independently_verified:
            reasons.append("coverage_not_independently_verified")
        if self.evidence_role != "after_fact_replay_grading_only":
            reasons.append("coverage_evidence_role_invalid")
        if not str(self.evidence_source or "").strip():
            reasons.append("coverage_evidence_source_missing")

        clocks = (
            self.coverage_start_ts,
            self.decision_start_ts,
            self.decision_end_ts,
            self.coverage_end_ts,
            phase_start_ts,
            phase_end_ts,
            required_coverage_end_ts,
        )
        if any(not isinstance(value, datetime) or value.tzinfo is None for value in clocks):
            reasons.append("coverage_clock_missing_or_naive")
        else:
            if not (
                self.coverage_start_ts
                <= self.decision_start_ts
                <= self.decision_end_ts
                <= self.coverage_end_ts
            ):
                reasons.append("coverage_clock_order_invalid")
            if self.decision_start_ts > phase_start_ts:
                reasons.append("phase_start_not_covered")
            if self.decision_end_ts < phase_end_ts:
                reasons.append("phase_end_not_covered")
            if self.coverage_end_ts < required_coverage_end_ts:
                reasons.append("hold_exit_not_covered")

        assertions = (
            (self.uncapped, "sampled_or_capped_tape"),
            (self.warmup_complete, "warmup_coverage_incomplete"),
            (
                self.continuous_quote_coverage,
                "continuous_quote_coverage_unproven",
            ),
            (
                self.continuous_trade_coverage,
                "continuous_trade_coverage_unproven",
            ),
            (
                self.causal_provenance_enforced,
                "causal_provenance_not_enforced",
            ),
            (
                self.provider_watermark_proven,
                "provider_watermark_unproven",
            ),
            (self.bounded_lateness_proven, "bounded_lateness_unproven"),
            (self.exact_quote_event_clock, "exact_quote_event_clock_unavailable"),
            (self.exact_trade_event_clock, "exact_trade_event_clock_unavailable"),
            (
                self.required_event_time_inputs_complete,
                "required_event_time_inputs_incomplete",
            ),
        )
        reasons.extend(reason for ready, reason in assertions if not ready)
        return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class ReplayTradeObservation:
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    pnl_usd: float | None
    pnl_r: float | None = None

    def valid(self) -> bool:
        return bool(
            self.entry_ts.tzinfo is not None
            and self.exit_ts.tzinfo is not None
            and self.exit_ts >= self.entry_ts
            and str(self.symbol or "").strip()
        )


@dataclass(frozen=True)
class PhaseBenchmarkGrade:
    label_id: str
    symbol: str
    matching_trade_count: int
    aggregate_pnl_usd: float | None
    grade: RecapDecisionGrade
    coverage_reasons: tuple[str, ...] = ()


def _finite_positive(value: float, *, name: str) -> float:
    out = float(value)
    if not math.isfinite(out) or out <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return out


def evaluate_long_trade_path(
    points: Sequence[TradablePathPoint],
    *,
    entry_ts: datetime,
    exit_ts: datetime,
    qty: float,
    planned_stop_price: float | None = None,
    entry_fill_price: float | None = None,
    exit_fill_price: float | None = None,
) -> LongTradePathMetrics:
    """Measure executable MFE capture/giveback without looking beyond exit.

    If explicit fills are absent, the first quote at/after entry supplies the
    ask fill and the first quote at/after exit supplies the bid fill.  MFE/MAE
    use bids because a long position can only realize against the bid.
    """

    if exit_ts < entry_ts:
        raise ValueError("exit_ts must be at or after entry_ts")
    quantity = _finite_positive(qty, name="qty")
    ordered = sorted(points, key=lambda point: point.ts)
    for point in ordered:
        point.validate()
    entry_quote = next((point for point in ordered if point.ts >= entry_ts), None)
    exit_quote = next((point for point in ordered if point.ts >= exit_ts), None)
    if entry_quote is None:
        raise ValueError("no quote at or after entry_ts")
    if exit_quote is None:
        raise ValueError("no quote at or after exit_ts")

    entry_fill = _finite_positive(
        entry_fill_price if entry_fill_price is not None else entry_quote.ask,
        name="entry_fill_price",
    )
    exit_fill = _finite_positive(
        exit_fill_price if exit_fill_price is not None else exit_quote.bid,
        name="exit_fill_price",
    )
    # A scalar override is not broker lifecycle evidence.  It therefore cannot
    # claim price improvement inside the spread: replay benchmarks remain
    # conservative at ask-entry/bid-exit, while worse slippage remains valid.
    # A genuinely improved live fill must enter through a separately verified,
    # content-addressed broker-fill path rather than this quote-only evaluator.
    if entry_fill < entry_quote.ask:
        raise ValueError("entry_fill_price is below the executable ask")
    if exit_fill > exit_quote.bid:
        raise ValueError("exit_fill_price is above the executable bid")
    path = [
        point
        for point in ordered
        if entry_quote.ts <= point.ts <= exit_quote.ts
    ]
    if not path:
        raise ValueError("no causal quote path between entry and exit")
    peak = max(path, key=lambda point: (point.bid, -point.ts.timestamp()))
    trough = min(path, key=lambda point: (point.bid, point.ts.timestamp()))

    realized_per_share = exit_fill - entry_fill
    peak_per_share = max(0.0, peak.bid - entry_fill)
    gross_pnl = realized_per_share * quantity
    peak_open_profit = peak_per_share * quantity
    giveback = (
        max(0.0, peak_open_profit - gross_pnl)
        if peak_open_profit > 0
        else 0.0
    )
    giveback_fraction = (
        giveback / peak_open_profit if peak_open_profit > 0 else None
    )
    capture_ratio = (
        realized_per_share / peak_per_share if peak_per_share > 0 else None
    )

    mfe_r: float | None = None
    mae_r: float | None = None
    if planned_stop_price is not None:
        stop = _finite_positive(planned_stop_price, name="planned_stop_price")
        risk_per_share = entry_fill - stop
        if risk_per_share <= 0:
            raise ValueError("planned_stop_price must be below a long entry")
        mfe_r = max(0.0, (peak.bid - entry_fill) / risk_per_share)
        # Project convention: adverse excursion is a positive magnitude.
        mae_r = max(0.0, (entry_fill - trough.bid) / risk_per_share)

    return LongTradePathMetrics(
        entry_ts=entry_quote.ts,
        exit_ts=exit_quote.ts,
        entry_fill_price=entry_fill,
        exit_fill_price=exit_fill,
        peak_executable_bid=float(peak.bid),
        peak_ts=peak.ts,
        trough_executable_bid=float(trough.bid),
        gross_pnl_usd=gross_pnl,
        peak_open_profit_usd=peak_open_profit,
        open_profit_giveback_usd=giveback,
        open_profit_giveback_fraction=giveback_fraction,
        realized_mfe_capture_ratio=capture_ratio,
        mfe_r=mfe_r,
        mae_r=mae_r,
        seconds_to_peak=max(0.0, (peak.ts - entry_quote.ts).total_seconds()),
        peak_to_exit_seconds=max(0.0, (exit_quote.ts - peak.ts).total_seconds()),
        path_points_used=len(path),
    )


def grade_recap_decision(
    *,
    expected_action: ExpectedAction,
    actual_action: ActualAction,
    decision_ts: datetime | None = None,
    phase_window_matched: bool | None = None,
    trade_outcome_acceptable: bool | None = None,
    veto_evidence: EventTimeVetoEvidence | None = None,
    veto_reason: str | None = None,
) -> RecapDecisionGrade:
    """Grade trades and correct no-trades without forcing blind imitation.

    A Ross winner that CHILI rejects can receive valid-veto credit only when the
    veto is independently observable and fresh at event time.  A recap-derived
    or hindsight-only excuse is not a valid veto.
    """

    if expected_action not in {"trade", "reject"}:
        raise ValueError("unsupported expected_action")
    if actual_action not in {"trade", "reject", "miss", "unavailable"}:
        raise ValueError("unsupported actual_action")
    if actual_action == "unavailable":
        return RecapDecisionGrade(
            expected_action,
            actual_action,
            "unscorable",
            None,
            "required event-time evidence unavailable",
        )
    if expected_action == "reject":
        if actual_action in {"reject", "miss"}:
            return RecapDecisionGrade(
                expected_action,
                actual_action,
                "matched_reject",
                1.0,
                veto_reason or "correct no-trade",
            )
        return RecapDecisionGrade(
            expected_action,
            actual_action,
            "false_positive_trade",
            0.0,
            "replay acted on a labeled no-trade/negative phase",
        )
    if actual_action == "trade":
        if phase_window_matched is not True:
            return RecapDecisionGrade(
                expected_action,
                actual_action,
                "wrong_phase_trade",
                0.0,
                "replay trade did not overlap the labeled profitable phase",
            )
        if trade_outcome_acceptable is None:
            return RecapDecisionGrade(
                expected_action,
                actual_action,
                "unscorable",
                None,
                "phase matched but executable trade outcome was not supplied",
            )
        if trade_outcome_acceptable is not True:
            return RecapDecisionGrade(
                expected_action,
                actual_action,
                "unmatched_trade_outcome",
                0.0,
                "phase matched but the executable replay outcome failed the benchmark",
            )
        return RecapDecisionGrade(
            expected_action,
            actual_action,
            "matched_trade",
            1.0,
            "replay admitted the labeled profitable setup",
        )
    if (
        actual_action == "reject"
        and veto_evidence is not None
        and veto_evidence.valid_at(decision_ts)
    ):
        return RecapDecisionGrade(
            expected_action,
            actual_action,
            "valid_veto",
            1.0,
            veto_evidence.reason,
        )
    return RecapDecisionGrade(
        expected_action,
        actual_action,
        "missed_profitable_setup",
        0.0,
        veto_reason or "no valid event-time veto",
    )


def grade_recap_phase_window(
    *,
    label_id: str,
    symbol: str,
    expected_action: ExpectedAction,
    trades: Sequence[ReplayTradeObservation],
    phase_window: ValidatedPhaseWindow | None,
    replay_coverage: ValidatedReplayCoverage | None = None,
    veto_evidence: EventTimeVetoEvidence | None = None,
    minimum_aggregate_pnl_usd: float = 0.0,
) -> PhaseBenchmarkGrade:
    """Grade one recap phase against executable replay trades.

    The phase window and replay-coverage proof are both mandatory and must be
    independently validated.  When either is absent, mismatched, sampled, or
    temporally incomplete, the label remains unscorable.  For a profitable
    phase, all replay subtrades whose *entries* fall inside the exact window
    are aggregated; this accommodates Ross-style sequences such as VEEE where
    some attempts lose but the phase is net profitable.  A symbol trade only
    outside the labeled winner is surfaced as a wrong-phase trade instead of
    receiving credit.
    """

    sym = str(symbol or "").strip().upper()
    if phase_window is None or not phase_window.valid_for(
        label_id=label_id,
        symbol=sym,
    ):
        return PhaseBenchmarkGrade(
            label_id=label_id,
            symbol=sym,
            matching_trade_count=0,
            aggregate_pnl_usd=None,
            grade=grade_recap_decision(
                expected_action=expected_action,
                actual_action="unavailable",
            ),
            coverage_reasons=("phase_window_missing_or_unverified",),
        )

    symbol_trades = [
        trade
        for trade in trades
        if trade.valid() and trade.symbol.strip().upper() == sym
    ]
    matching = [
        trade
        for trade in symbol_trades
        if phase_window.start_ts <= trade.entry_ts <= phase_window.end_ts
    ]
    required_coverage_end_ts = max(
        (trade.exit_ts for trade in matching),
        default=phase_window.end_ts,
    )
    if replay_coverage is None:
        coverage_reasons = ("replay_coverage_missing",)
    else:
        coverage_reasons = replay_coverage.failure_reasons(
            label_id=label_id,
            symbol=sym,
            phase_start_ts=phase_window.start_ts,
            phase_end_ts=phase_window.end_ts,
            required_coverage_end_ts=required_coverage_end_ts,
        )
    if coverage_reasons:
        return PhaseBenchmarkGrade(
            label_id=label_id,
            symbol=sym,
            matching_trade_count=0,
            aggregate_pnl_usd=None,
            grade=RecapDecisionGrade(
                expected_action=expected_action,
                actual_action="unavailable",
                status="unscorable",
                credit=None,
                reason=(
                    "required causal replay coverage unavailable: "
                    + ", ".join(coverage_reasons)
                ),
            ),
            coverage_reasons=coverage_reasons,
        )

    pnl_values = [float(trade.pnl_usd) for trade in matching if trade.pnl_usd is not None]
    aggregate_pnl = sum(pnl_values) if len(pnl_values) == len(matching) and matching else None

    if expected_action == "reject":
        actual_action: ActualAction = "trade" if matching else "reject"
        grade = grade_recap_decision(
            expected_action=expected_action,
            actual_action=actual_action,
            decision_ts=phase_window.decision_ts,
        )
    elif matching:
        grade = grade_recap_decision(
            expected_action=expected_action,
            actual_action="trade",
            decision_ts=phase_window.decision_ts,
            phase_window_matched=True,
            trade_outcome_acceptable=(
                aggregate_pnl is not None
                and aggregate_pnl > float(minimum_aggregate_pnl_usd)
            ) if aggregate_pnl is not None else None,
        )
    elif symbol_trades:
        grade = grade_recap_decision(
            expected_action=expected_action,
            actual_action="trade",
            decision_ts=phase_window.decision_ts,
            phase_window_matched=False,
            trade_outcome_acceptable=None,
        )
    else:
        grade = grade_recap_decision(
            expected_action=expected_action,
            actual_action="reject" if veto_evidence is not None else "miss",
            decision_ts=phase_window.decision_ts,
            veto_evidence=veto_evidence,
        )

    return PhaseBenchmarkGrade(
        label_id=label_id,
        symbol=sym,
        matching_trade_count=len(matching),
        aggregate_pnl_usd=aggregate_pnl,
        grade=grade,
        coverage_reasons=(),
    )


def replay_trade_observations(results: Sequence[Any]) -> list[ReplayTradeObservation]:
    """Adapt counterfactual result rows without importing the replay module.

    Duck typing avoids a circular import and keeps the benchmark an after-fact
    consumer.  Malformed rows are skipped; they can never become benchmark
    credit.
    """

    observations: list[ReplayTradeObservation] = []
    for row in results or ():
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        for trade in getattr(row, "trades", ()) or ():
            entry_ts = getattr(trade, "entry_ts", None)
            exit_ts = getattr(trade, "exit_ts", None)
            if not isinstance(entry_ts, datetime) or not isinstance(exit_ts, datetime):
                continue
            observation = ReplayTradeObservation(
                symbol=symbol or str(getattr(trade, "symbol", "") or "").strip().upper(),
                entry_ts=entry_ts,
                exit_ts=exit_ts,
                pnl_usd=getattr(trade, "pnl_usd", None),
                pnl_r=getattr(trade, "pnl_r", None),
            )
            if observation.valid():
                observations.append(observation)
    return observations


def grade_manifest_phase_labels(
    manifest: Mapping[str, Any],
    *,
    trades: Sequence[ReplayTradeObservation],
    phase_windows: Sequence[ValidatedPhaseWindow] = (),
    replay_coverages: Sequence[ValidatedReplayCoverage] = (),
    veto_evidence_by_label: Mapping[str, EventTimeVetoEvidence] | None = None,
) -> dict[str, Any]:
    """Grade every supported phase label in a Ross playlist manifest.

    This is deliberately an after-fact join.  The manifest and windows are not
    accepted by any replay/strategy entry function.  Missing exact windows stay
    visible as unscorable rows instead of silently turning sequence descriptions
    or approximate YouTube timestamps into market-time inputs.
    """

    windows = {window.label_id: window for window in phase_windows}
    coverages = {coverage.label_id: coverage for coverage in replay_coverages}
    vetoes = dict(veto_evidence_by_label or {})
    rows: list[dict[str, Any]] = []
    for entry in manifest.get("entries", ()) or ():
        if not isinstance(entry, Mapping):
            continue
        trade_date = str(entry.get("date") or "")
        for label in entry.get("phase_labels", ()) or ():
            if not isinstance(label, Mapping):
                continue
            label_id = str(label.get("label_id") or "")
            symbol = str(label.get("symbol") or "").strip().upper()
            target = str(label.get("benchmark_target") or "")
            if target not in {"trade", "reject"}:
                rows.append(
                    {
                        "label_id": label_id,
                        "trade_date": trade_date,
                        "symbol": symbol,
                        "benchmark_target": target,
                        "status": "unscorable",
                        "credit": None,
                        "matching_trade_count": 0,
                        "aggregate_pnl_usd": None,
                        "reason": "loss-containment policy/window threshold not yet defined",
                    }
                )
                continue
            phase_grade = grade_recap_phase_window(
                label_id=label_id,
                symbol=symbol,
                expected_action=target,
                trades=trades,
                phase_window=windows.get(label_id),
                replay_coverage=coverages.get(label_id),
                veto_evidence=vetoes.get(label_id),
            )
            rows.append(
                {
                    "label_id": label_id,
                    "trade_date": trade_date,
                    "symbol": symbol,
                    "benchmark_target": target,
                    "status": phase_grade.grade.status,
                    "credit": phase_grade.grade.credit,
                    "matching_trade_count": phase_grade.matching_trade_count,
                    "aggregate_pnl_usd": phase_grade.aggregate_pnl_usd,
                    "reason": phase_grade.grade.reason,
                    "coverage_reasons": list(phase_grade.coverage_reasons),
                }
            )
    scorable = [row for row in rows if row["credit"] is not None]
    return {
        "manifest_id": manifest.get("manifest_id"),
        "evidence_role": manifest.get("evidence_role"),
        "label_count": len(rows),
        "scorable_label_count": len(scorable),
        "unscorable_label_count": len(rows) - len(scorable),
        "credit": (
            sum(float(row["credit"]) for row in scorable) / len(scorable)
            if scorable
            else None
        ),
        "rows": rows,
    }
