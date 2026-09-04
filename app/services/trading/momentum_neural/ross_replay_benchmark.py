"""Phase-aware Ross recap labels and causal CHILI replay metrics.

Recaps are after-the-fact labels only.  They may grade a replay decision, but
must never be injected as an event-time scanner/news/admission signal.  Price
path metrics use executable sides of the recorded quote (ask entry, bid exit)
and stop at the simulated exit instant so later highs cannot improve a score.

Two evidence tiers exist, and they are never averaged together:

* ``certified`` — a :class:`ValidatedReplayCoverage` bound to an exact sealed
  decision-checkpoint identity.  This is the only tier whose credit may be
  quoted as a CHILI-vs-Ross benchmark result.
* ``diagnostic_only`` — a :class:`DiagnosticReplayCoverage` describing a
  hydrated (non-sealed) replay.  Hydrated tape can never carry a sealed
  capture, so before this tier existed every hydrated label graded
  ``unscorable`` and the bench produced no signal at all.  Diagnostic credit is
  reported *alongside* the reason it is not certified, never instead of it, and
  it accumulates into a separate ``credit_diagnostic`` figure.

The sealed tier is unchanged by the diagnostic tier: when a sealed coverage
record is supplied it decides the outcome outright, so a failing seal can never
be bypassed by also attaching a diagnostic record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
import math
from typing import Any, Literal, Mapping, Sequence


ExpectedAction = Literal["trade", "reject"]
ActualAction = Literal["trade", "reject", "miss", "unavailable"]
# Which evidence tier produced a graded row.  ``unscorable`` means no tier
# graded it, so neither credit accumulator may count it.
EvidenceGrade = Literal["certified", "diagnostic_only", "unscorable"]

# Every reason emitted alongside (rather than instead of) diagnostic credit
# carries this prefix, so a downstream reader can never mistake a diagnostic
# caveat for a certified-tier coverage failure by string match alone.
DIAGNOSTIC_REASON_PREFIX = "diagnostic_only:"


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


def _nonzero_source_count(sources: Any) -> int:
    """Count the distinct tape providers named for one table.

    ``tape_sources`` reaches us in the shape the replay driver writes it:
    ``{table_name: {source_name: row_count}}`` (built by
    ``assert_single_hydrated_source`` at scripts/replay_v3_fsm_window.py:346-379
    and emitted under the ``tape_sources`` key at
    scripts/replay_v3_fsm_window.py:1148).  A plain sequence of source names is
    also accepted so a caller that already collapsed the counts is not forced to
    re-inflate them.  Zero-row entries do not count: the driver's own
    single-source assertion filters them the same way at
    scripts/replay_v3_fsm_window.py:1585.
    """

    if isinstance(sources, Mapping):
        return sum(1 for value in sources.values() if value)
    if isinstance(sources, (str, bytes)):
        return 1 if str(sources).strip() else 0
    try:
        return sum(1 for value in sources if str(value).strip())
    except TypeError:
        return 0


@dataclass(frozen=True, kw_only=True)
class DiagnosticReplayCoverage:
    """Non-sealed description of what a hydrated replay actually observed.

    Why this exists: :meth:`ValidatedReplayCoverage.failure_reasons` always
    emits ``sealed_decision_coverage_not_bound`` first, because that record has
    no binding to an exact sealed capture identity.  Hydrated tape *cannot*
    acquire such a binding after the fact, so on hydrated data every label
    graded ``unscorable`` and the bench measured nothing.  This record makes a
    hydrated run gradeable at a strictly lower, explicitly labelled tier.

    What it does NOT do: it does not relax identity or temporal rigour.  Label,
    symbol, timezone-awareness, clock ordering and full phase/hold coverage are
    checked exactly as the sealed record checks them.  The single requirement
    that is dropped is the sealed-capture binding, and that drop is reported on
    every graded row via :meth:`advisory_reasons`.

    ``kw_only=True`` keeps the declared field order (``evidence_role`` carries a
    default while ``tree_sha`` does not) legal, and matches how every coverage
    record in this module is already constructed at its call sites.

    Fields map onto the replay receipt (schema
    ``chili.replay_v3_fsm_window_result.v1``, written at
    scripts/replay_v3_fsm_window.py:1140-1183):

    * ``coverage_start`` / ``coverage_end`` — the outer bound of every recorded
      input, warmup included.  In receipt terms ``env.FRAME_START`` ..
      ``env.WIN_END`` (scripts/replay_v3_fsm_window.py:777-778).
    * ``tape_sources`` — the receipt's ``tape_sources`` mapping, verbatim.
    * ``tick_stride`` — ``env.TICK_STRIDE``.  ``1`` means no print was skipped;
      anything higher means the replay saw a subsample of the tape and is
      surfaced as an advisory rather than silently absorbed into credit.
    * ``grid_step_s`` — ``env.GRID_STEP_S``, the FSM evaluation cadence.
    * ``nbbo_vendor`` — the provider behind the NBBO mirror.  The receipt has no
      scalar field for this; it is the single key of
      ``tape_sources["momentum_nbbo_spread_tape"]``.
    * ``depth_rows`` — ``mirrored.depth_rows``.  Recorded, but never turned into
      a failure or an advisory: a run with no L2 mirrored is a legitimate
      configuration, not a defect, and claiming otherwise would be an invented
      requirement.
    * ``tree_sha`` — the git tree object that ran (``tree.tree``), not a branch
      name, so a stale build tree cannot masquerade as the code under test.
    """

    label_id: str
    symbol: str
    coverage_start: datetime
    coverage_end: datetime
    tape_sources: Mapping[str, Any]
    tick_stride: int
    grid_step_s: float
    nbbo_vendor: str
    depth_rows: int
    evidence_role: Literal["diagnostic_only"] = "diagnostic_only"
    tree_sha: str

    def failure_reasons(
        self,
        *,
        label_id: str,
        symbol: str,
        phase_start_ts: datetime,
        phase_end_ts: datetime,
        required_coverage_end_ts: datetime,
    ) -> tuple[str, ...]:
        """Reasons this record is unusable even as a diagnostic.

        These are DISQUALIFYING, not advisory.  A non-empty result keeps the
        label ``unscorable`` exactly as a failing sealed record would.  Reason
        strings deliberately reuse the sealed vocabulary wherever the check is
        the same check, so a downstream reader parses one vocabulary, not two.
        Notably absent: ``sealed_decision_coverage_not_bound``.  That is the one
        condition this tier exists to report alongside credit rather than to
        fail on, and it is emitted by :meth:`advisory_reasons` instead.
        """

        reasons: list[str] = []
        sym = str(symbol or "").strip().upper()
        if self.label_id != label_id:
            reasons.append("coverage_label_mismatch")
        if str(self.symbol or "").strip().upper() != sym:
            reasons.append("coverage_symbol_mismatch")
        if self.evidence_role != "diagnostic_only":
            reasons.append("coverage_evidence_role_invalid")

        clocks = (
            self.coverage_start,
            self.coverage_end,
            phase_start_ts,
            phase_end_ts,
            required_coverage_end_ts,
        )
        if any(not isinstance(value, datetime) or value.tzinfo is None for value in clocks):
            reasons.append("coverage_clock_missing_or_naive")
        else:
            if self.coverage_start > self.coverage_end:
                reasons.append("coverage_clock_order_invalid")
            # A diagnostic record has no separately proven decision interval, so
            # the coverage bounds themselves must contain the whole phase.  This
            # is stricter than the sealed record's decision_start/decision_end
            # test, never looser.
            if self.coverage_start > phase_start_ts:
                reasons.append("phase_start_not_covered")
            if self.coverage_end < phase_end_ts:
                reasons.append("phase_end_not_covered")
            if self.coverage_end < required_coverage_end_ts:
                reasons.append("hold_exit_not_covered")

        if not self.tape_sources:
            reasons.append("diagnostic_tape_sources_missing")
        else:
            # Mirrors the driver's own abort at
            # scripts/replay_v3_fsm_window.py:1584-1590: two providers for one
            # table means the replay read both tapes concatenated, so every
            # print is doubled and no density figure means anything.
            for table_sources in self.tape_sources.values():
                if _nonzero_source_count(table_sources) > 1:
                    reasons.append("diagnostic_tape_source_ambiguous")
                    break

        # Lower bound only.  A stride of 1 skips nothing; any integer above it is
        # a legal subsample that advisory_reasons surfaces.  There is no tuned
        # maximum here on purpose — this module does not own that threshold.
        if not isinstance(self.tick_stride, int) or isinstance(self.tick_stride, bool):
            reasons.append("diagnostic_tick_stride_invalid")
        elif self.tick_stride < 1:
            reasons.append("diagnostic_tick_stride_invalid")

        try:
            step = float(self.grid_step_s)
        except (TypeError, ValueError):
            step = float("nan")
        if not math.isfinite(step) or step <= 0:
            reasons.append("diagnostic_grid_step_invalid")

        if not str(self.nbbo_vendor or "").strip():
            reasons.append("diagnostic_nbbo_vendor_missing")

        try:
            depth = int(self.depth_rows)
        except (TypeError, ValueError):
            depth = -1
        if depth < 0:
            reasons.append("diagnostic_depth_rows_invalid")

        if not str(self.tree_sha or "").strip():
            reasons.append("diagnostic_tree_sha_missing")

        return tuple(dict.fromkeys(reasons))

    def advisory_reasons(self) -> tuple[str, ...]:
        """Caveats reported ALONGSIDE diagnostic credit, never instead of it.

        The first entry is always present: this tier is, by construction, not
        bound to a sealed decision checkpoint, and a graded diagnostic row must
        carry that fact with it wherever it travels.
        """

        reasons = [f"{DIAGNOSTIC_REASON_PREFIX}sealed_decision_coverage_not_bound"]
        try:
            stride = int(self.tick_stride)
        except (TypeError, ValueError):
            stride = 1
        if stride > 1:
            # Definitional, not a tuned threshold: stride N feeds one print in
            # every N to the FSM, so the run did not see the tape it is being
            # credited against.
            reasons.append(f"{DIAGNOSTIC_REASON_PREFIX}tick_stride_subsampled")
        return tuple(reasons)

    def provenance(self) -> dict[str, Any]:
        """Flat, JSON-safe descriptors for a bench report row.

        Carrying these next to the credit is what makes a diagnostic number
        auditable later: which tape, at what stride and cadence, from which
        tree.
        """

        return {
            "evidence_role": self.evidence_role,
            "coverage_start": self.coverage_start.isoformat()
            if isinstance(self.coverage_start, datetime)
            else None,
            "coverage_end": self.coverage_end.isoformat()
            if isinstance(self.coverage_end, datetime)
            else None,
            "tape_sources": {
                str(table): sources for table, sources in dict(self.tape_sources or {}).items()
            },
            "tick_stride": self.tick_stride,
            "grid_step_s": self.grid_step_s,
            "nbbo_vendor": self.nbbo_vendor,
            "depth_rows": self.depth_rows,
            "tree_sha": self.tree_sha,
        }


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
    # Which evidence tier produced this row.  Defaulted so existing positional
    # construction keeps working; every return path in this module sets it
    # explicitly.  Callers that aggregate credit MUST branch on this — a
    # ``diagnostic_only`` row's credit is not a benchmark result.
    evidence_grade: EvidenceGrade = "unscorable"
    # Provenance of the diagnostic tape, when this row was graded diagnostically.
    # Empty on the certified path, which carries its proof in the sealed record.
    diagnostic_provenance: Mapping[str, Any] | None = None


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


def _grade_covered_phase(
    *,
    expected_action: ExpectedAction,
    phase_window: ValidatedPhaseWindow,
    matching: Sequence[ReplayTradeObservation],
    symbol_trades: Sequence[ReplayTradeObservation],
    aggregate_pnl: float | None,
    veto_evidence: EventTimeVetoEvidence | None,
    minimum_aggregate_pnl_usd: float,
) -> RecapDecisionGrade:
    """The decision logic that runs once coverage — of either tier — is accepted.

    Extracted verbatim from ``grade_recap_phase_window`` so the certified and
    diagnostic tiers cannot drift apart.  The tiers differ ONLY in what proof
    they demand before reaching this point; they must never differ in how a
    decision is scored, or a diagnostic number would stop being comparable to a
    certified one.  In particular the valid-veto rule is unchanged: a Ross
    winner CHILI rejected earns credit only through
    ``EventTimeVetoEvidence.valid_at``.
    """

    if expected_action == "reject":
        actual_action: ActualAction = "trade" if matching else "reject"
        return grade_recap_decision(
            expected_action=expected_action,
            actual_action=actual_action,
            decision_ts=phase_window.decision_ts,
        )
    if matching:
        return grade_recap_decision(
            expected_action=expected_action,
            actual_action="trade",
            decision_ts=phase_window.decision_ts,
            phase_window_matched=True,
            trade_outcome_acceptable=(
                aggregate_pnl is not None
                and aggregate_pnl > float(minimum_aggregate_pnl_usd)
            ) if aggregate_pnl is not None else None,
        )
    if symbol_trades:
        return grade_recap_decision(
            expected_action=expected_action,
            actual_action="trade",
            decision_ts=phase_window.decision_ts,
            phase_window_matched=False,
            trade_outcome_acceptable=None,
        )
    return grade_recap_decision(
        expected_action=expected_action,
        actual_action="reject" if veto_evidence is not None else "miss",
        decision_ts=phase_window.decision_ts,
        veto_evidence=veto_evidence,
    )


def grade_recap_phase_window(
    *,
    label_id: str,
    symbol: str,
    expected_action: ExpectedAction,
    trades: Sequence[ReplayTradeObservation],
    phase_window: ValidatedPhaseWindow | None,
    replay_coverage: ValidatedReplayCoverage | None = None,
    diagnostic_coverage: DiagnosticReplayCoverage | None = None,
    veto_evidence: EventTimeVetoEvidence | None = None,
    minimum_aggregate_pnl_usd: float = 0.0,
) -> PhaseBenchmarkGrade:
    """Grade one recap phase against executable replay trades.

    The phase window is mandatory and must be independently validated.  Coverage
    proof is mandatory too, at one of two tiers.  For a profitable phase, all
    replay subtrades whose *entries* fall inside the exact window are
    aggregated; this accommodates Ross-style sequences such as VEEE where some
    attempts lose but the phase is net profitable.  A symbol trade only outside
    the labeled winner is surfaced as a wrong-phase trade instead of receiving
    credit.

    Coverage precedence, in order:

    1. ``replay_coverage`` supplied — the SEALED tier decides outright, pass or
       fail, and ``diagnostic_coverage`` is ignored entirely.  This ordering is
       load-bearing: if a failing seal could fall through to the diagnostic
       tier, attaching a diagnostic record would become a way to launder a
       rejected sealed proof into credit.
    2. Neither supplied — ``replay_coverage_missing``, unchanged.
    3. Only ``diagnostic_coverage`` supplied — the DIAGNOSTIC tier.  Identity,
       clock and phase/hold-coverage checks still apply and still fail closed.
       When they pass, the phase is graded and the row is stamped
       ``evidence_grade="diagnostic_only"`` with the un-bound-seal caveat
       reported next to the credit.
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
            evidence_grade="unscorable",
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

    # Tier selection.  ``replay_coverage is not None`` is deliberately the whole
    # test, not "the sealed record passed" — see the precedence note above.
    if replay_coverage is not None:
        tier: EvidenceGrade = "certified"
        coverage_reasons = replay_coverage.failure_reasons(
            label_id=label_id,
            symbol=sym,
            phase_start_ts=phase_window.start_ts,
            phase_end_ts=phase_window.end_ts,
            required_coverage_end_ts=required_coverage_end_ts,
        )
        advisory_reasons: tuple[str, ...] = ()
        provenance: Mapping[str, Any] | None = None
    elif diagnostic_coverage is not None:
        tier = "diagnostic_only"
        coverage_reasons = diagnostic_coverage.failure_reasons(
            label_id=label_id,
            symbol=sym,
            phase_start_ts=phase_window.start_ts,
            phase_end_ts=phase_window.end_ts,
            required_coverage_end_ts=required_coverage_end_ts,
        )
        advisory_reasons = diagnostic_coverage.advisory_reasons()
        provenance = diagnostic_coverage.provenance()
    else:
        tier = "unscorable"
        coverage_reasons = ("replay_coverage_missing",)
        advisory_reasons = ()
        provenance = None

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
            # A tier that failed its own checks graded nothing, so it is not
            # allowed to label the row with its tier name.
            evidence_grade="unscorable",
        )

    pnl_values = [float(trade.pnl_usd) for trade in matching if trade.pnl_usd is not None]
    aggregate_pnl = sum(pnl_values) if len(pnl_values) == len(matching) and matching else None

    grade = _grade_covered_phase(
        expected_action=expected_action,
        phase_window=phase_window,
        matching=matching,
        symbol_trades=symbol_trades,
        aggregate_pnl=aggregate_pnl,
        veto_evidence=veto_evidence,
        minimum_aggregate_pnl_usd=minimum_aggregate_pnl_usd,
    )

    return PhaseBenchmarkGrade(
        label_id=label_id,
        symbol=sym,
        matching_trade_count=len(matching),
        aggregate_pnl_usd=aggregate_pnl,
        grade=grade,
        # Certified rows keep the historical empty tuple.  Diagnostic rows carry
        # their caveat ALONGSIDE the credit rather than in place of it.
        coverage_reasons=advisory_reasons,
        evidence_grade=tier,
        diagnostic_provenance=provenance,
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
    diagnostic_coverages: Sequence[DiagnosticReplayCoverage] = (),
    veto_evidence_by_label: Mapping[str, EventTimeVetoEvidence] | None = None,
) -> dict[str, Any]:
    """Grade every supported phase label in a Ross playlist manifest.

    This is deliberately an after-fact join.  The manifest and windows are not
    accepted by any replay/strategy entry function.  Missing exact windows stay
    visible as unscorable rows instead of silently turning sequence descriptions
    or approximate YouTube timestamps into market-time inputs.

    Two credit figures come back and they are never mixed.  ``credit`` is the
    mean over CERTIFIED rows only and keeps its historical meaning exactly.
    ``credit_diagnostic`` is the mean over ``diagnostic_only`` rows.  A summary
    that averaged the two would let hydrated tape inflate a sealed benchmark
    number, which is the failure this split exists to make impossible.
    """

    windows = {window.label_id: window for window in phase_windows}
    coverages = {coverage.label_id: coverage for coverage in replay_coverages}
    diagnostics = {
        coverage.label_id: coverage for coverage in diagnostic_coverages
    }
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
                        "evidence_grade": "unscorable",
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
                diagnostic_coverage=diagnostics.get(label_id),
                veto_evidence=vetoes.get(label_id),
            )
            row: dict[str, Any] = {
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
                "evidence_grade": phase_grade.evidence_grade,
            }
            if phase_grade.diagnostic_provenance is not None:
                row["diagnostic_provenance"] = dict(phase_grade.diagnostic_provenance)
            rows.append(row)

    # Two disjoint accumulators.  ``credit is not None`` alone is no longer a
    # sufficient test for the certified figure: a diagnostic row also carries a
    # credit, and merging it here is exactly the silent inflation this split
    # prevents.
    certified = [
        row
        for row in rows
        if row["credit"] is not None and row["evidence_grade"] == "certified"
    ]
    diagnostic = [
        row
        for row in rows
        if row["credit"] is not None and row["evidence_grade"] == "diagnostic_only"
    ]
    return {
        "manifest_id": manifest.get("manifest_id"),
        "evidence_role": manifest.get("evidence_role"),
        "label_count": len(rows),
        # Unchanged meaning: certified-tier rows only.
        "scorable_label_count": len(certified),
        "unscorable_label_count": len(rows) - len(certified) - len(diagnostic),
        "credit": (
            sum(float(row["credit"]) for row in certified) / len(certified)
            if certified
            else None
        ),
        "diagnostic_label_count": len(diagnostic),
        "credit_diagnostic": (
            sum(float(row["credit"]) for row in diagnostic) / len(diagnostic)
            if diagnostic
            else None
        ),
        "rows": rows,
    }


# ─── replay receipt adapter ──────────────────────────────────────────────────
# The replay driver writes one JSON receipt per arm.  These constants name the
# exact contract this adapter reads; a version bump upstream must fail loudly
# here rather than silently mapping the wrong keys.

# scripts/replay_v3_fsm_window.py:199 — REPLAY_RESULT_SCHEMA.
REPLAY_RECEIPT_SCHEMA = "chili.replay_v3_fsm_window_result.v1"
# scripts/replay_v3_fsm_window.py:353 — the NBBO tape table the driver surveys
# for hydration sources.  Its single source name IS the NBBO vendor; the receipt
# carries no scalar field for it.
NBBO_TAPE_TABLE = "momentum_nbbo_spread_tape"


def _receipt_clock(
    raw: Any,
    *,
    name: str,
    naive_clock_tz: tzinfo | None,
) -> datetime:
    """Parse one receipt clock, refusing to guess a timezone.

    MEASURED, by reading the driver: ``WIN_START``/``WIN_END``/``OHLCV_START``
    are built with a bare ``datetime.fromisoformat`` over an env string
    (scripts/replay_v3_fsm_window.py:154-156) and ``FRAME_START`` is derived
    from them (:168), so all four are NAIVE and the receipt's ``env`` echo of
    them (:777-778) is a naive ISO string.  This module requires tz-aware
    clocks.  The driver's own usage example (:17-18) pairs a 12:35 window start
    with a US premarket session, which is consistent with UTC — but "consistent
    with" is not "verified", so this function will NOT assume it.  The caller
    must state the timezone those naive strings are in.
    """

    if isinstance(raw, datetime):
        parsed = raw
    else:
        text = str(raw or "").strip()
        if not text:
            raise ValueError(f"replay receipt is missing {name}")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"replay receipt {name} is not an ISO timestamp: {text!r}") from exc
    if parsed.tzinfo is not None:
        return parsed
    if naive_clock_tz is None:
        raise ValueError(
            f"replay receipt {name} is naive; pass naive_clock_tz to state which "
            "timezone the driver's env clocks were expressed in"
        )
    return parsed.replace(tzinfo=naive_clock_tz)


def diagnostic_coverage_from_replay_receipt(
    receipt: Mapping[str, Any],
    *,
    label_id: str,
    naive_clock_tz: tzinfo | None = None,
    allow_dirty_tree: bool = False,
) -> DiagnosticReplayCoverage:
    """Build a diagnostic coverage record from one replay-driver receipt.

    Convenience only — nothing in this module requires it, and a caller that
    assembles the record itself is equally valid.  It exists so the mapping from
    receipt keys to coverage fields is written down once, next to the grader
    that consumes it, instead of being re-derived per bench script.

    Key mapping, every one of them read out of the emission block at
    scripts/replay_v3_fsm_window.py:1140-1183:

    ==========================  ==================================
    coverage field              receipt key
    ==========================  ==================================
    ``symbol``                  ``env.SYMBOL``
    ``coverage_start``          ``env.FRAME_START`` (warmup start)
    ``coverage_end``            ``env.WIN_END``
    ``tape_sources``            ``tape_sources``
    ``tick_stride``             ``env.TICK_STRIDE``
    ``grid_step_s``             ``env.GRID_STEP_S``
    ``nbbo_vendor``             sole key of ``tape_sources[NBBO_TAPE_TABLE]``
    ``depth_rows``              ``mirrored.depth_rows``
    ``tree_sha``                ``tree.tree``
    ==========================  ==================================

    ``allow_dirty_tree`` defaults to False so an uncommitted working tree — where
    the recorded tree sha does NOT describe the code that ran — fails closed.
    Note for operators: this repository routinely carries untracked working
    files, so ``tree.dirty`` will often be true and bench runs will have to opt
    in explicitly.  That is the intended friction; it keeps the provenance claim
    honest rather than automatic.

    Raises ``ValueError`` on any missing/ambiguous field.  It never substitutes a
    default, because a coverage record that quietly filled in its own provenance
    would be worse than no record.
    """

    if not isinstance(receipt, Mapping):
        raise ValueError("replay receipt must be a mapping")
    schema = str(receipt.get("schema") or "")
    if schema != REPLAY_RECEIPT_SCHEMA:
        raise ValueError(
            f"unsupported replay receipt schema {schema!r}; "
            f"this adapter reads {REPLAY_RECEIPT_SCHEMA!r}"
        )

    env = receipt.get("env")
    if not isinstance(env, Mapping):
        raise ValueError("replay receipt is missing its env contract block")

    symbol = str(env.get("SYMBOL") or "").strip().upper()
    if not symbol:
        raise ValueError("replay receipt env is missing SYMBOL")

    tape_sources = receipt.get("tape_sources")
    if not isinstance(tape_sources, Mapping) or not tape_sources:
        raise ValueError("replay receipt is missing a non-empty tape_sources block")

    nbbo_sources = tape_sources.get(NBBO_TAPE_TABLE)
    if isinstance(nbbo_sources, Mapping):
        vendors = [str(name) for name, rows in nbbo_sources.items() if rows]
    elif isinstance(nbbo_sources, (list, tuple)):
        vendors = [str(name) for name in nbbo_sources if str(name).strip()]
    else:
        vendors = []
    if len(vendors) != 1:
        raise ValueError(
            f"replay receipt must name exactly one {NBBO_TAPE_TABLE} source to "
            f"identify the NBBO vendor; found {vendors!r}"
        )

    for required in ("TICK_STRIDE", "GRID_STEP_S"):
        if env.get(required) is None:
            raise ValueError(f"replay receipt env is missing {required}")

    mirrored = receipt.get("mirrored")
    if not isinstance(mirrored, Mapping) or "depth_rows" not in mirrored:
        raise ValueError("replay receipt is missing mirrored.depth_rows")

    tree = receipt.get("tree")
    if not isinstance(tree, Mapping):
        raise ValueError("replay receipt is missing its tree block")
    tree_sha = str(tree.get("tree") or "").strip()
    if not tree_sha:
        raise ValueError("replay receipt tree.tree is empty; the build tree is unidentified")
    if tree.get("dirty") and not allow_dirty_tree:
        raise ValueError(
            "replay receipt reports a dirty working tree, so tree.tree does not "
            "describe the code that ran; pass allow_dirty_tree=True to accept it"
        )

    return DiagnosticReplayCoverage(
        label_id=label_id,
        symbol=symbol,
        coverage_start=_receipt_clock(
            env.get("FRAME_START"),
            name="env.FRAME_START",
            naive_clock_tz=naive_clock_tz,
        ),
        coverage_end=_receipt_clock(
            env.get("WIN_END"),
            name="env.WIN_END",
            naive_clock_tz=naive_clock_tz,
        ),
        tape_sources=dict(tape_sources),
        tick_stride=int(env["TICK_STRIDE"]),
        grid_step_s=float(env["GRID_STEP_S"]),
        nbbo_vendor=vendors[0],
        depth_rows=int(mirrored["depth_rows"]),
        tree_sha=tree_sha,
    )
