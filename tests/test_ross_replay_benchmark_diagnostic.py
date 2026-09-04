"""Diagnostic-tier grading for the Ross replay benchmark.

Why this tier exists: ``ValidatedReplayCoverage.failure_reasons`` always emits
``sealed_decision_coverage_not_bound`` first, because that legacy record carries
no binding to an exact sealed capture identity.  Hydrated tape cannot acquire
such a binding after the fact, so on hydrated data every label graded
``unscorable`` and the bench measured nothing at all.

These tests pin the two properties that make the relaxation safe:

1. The SEALED path is untouched.  When a sealed coverage record is supplied it
   decides the outcome outright — a failing seal can never be laundered into
   credit by also attaching a diagnostic record.
2. Diagnostic credit is reported ALONGSIDE the reason it is not certified, and
   accumulates into a separate ``credit_diagnostic`` figure that never merges
   into the certified ``credit``.

Everything else the module already guaranteed — exact phase windows, tz-aware
clocks, full phase/hold coverage, and event-time-only veto credit — must hold
identically on the diagnostic tier.  Regression coverage for the sealed tier
itself lives in tests/test_ross_replay_benchmark.py and is not duplicated here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.services.trading.momentum_neural.ross_replay_benchmark import (
    DIAGNOSTIC_REASON_PREFIX,
    DiagnosticReplayCoverage,
    EventTimeVetoEvidence,
    NBBO_TAPE_TABLE,
    REPLAY_RECEIPT_SCHEMA,
    ReplayTradeObservation,
    ValidatedPhaseWindow,
    ValidatedReplayCoverage,
    diagnostic_coverage_from_replay_receipt,
    grade_manifest_phase_labels,
    grade_recap_phase_window,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
VEEE_LABEL = "2026-07-13_VEEE_fresh_front_side_pullback"
PLSM_REJECT_LABEL = "2026-07-13_PLSM_backside_fomo_curl"
NOT_BOUND = f"{DIAGNOSTIC_REASON_PREFIX}sealed_decision_coverage_not_bound"
SUBSAMPLED = f"{DIAGNOSTIC_REASON_PREFIX}tick_stride_subsampled"


def _phase_window(
    *,
    label_id: str = VEEE_LABEL,
    symbol: str = "VEEE",
) -> ValidatedPhaseWindow:
    return ValidatedPhaseWindow(
        label_id=label_id,
        symbol=symbol,
        start_ts=BASE,
        end_ts=BASE + timedelta(minutes=5),
        decision_ts=BASE + timedelta(seconds=30),
        evidence_source="recorded_iqfeed_and_broker_phase_review",
        evidence_role="after_fact_grading_only",
        independently_verified=True,
    )


def _sealed_coverage(
    window: ValidatedPhaseWindow | None = None,
    **overrides,
) -> ValidatedReplayCoverage:
    """A sealed record whose booleans are all asserted true.

    It still fails, on ``sealed_decision_coverage_not_bound`` alone — that is the
    module's existing behaviour and these tests must not change it.
    """

    phase = window or _phase_window()
    values = {
        "label_id": phase.label_id,
        "symbol": phase.symbol,
        "coverage_start_ts": phase.start_ts - timedelta(minutes=30),
        "decision_start_ts": phase.start_ts,
        "decision_end_ts": phase.end_ts,
        "coverage_end_ts": phase.end_ts + timedelta(minutes=30),
        "evidence_source": "independent_uncapped_causal_tape_audit",
        "evidence_role": "after_fact_replay_grading_only",
        "independently_verified": True,
        "uncapped": True,
        "warmup_complete": True,
        "continuous_quote_coverage": True,
        "continuous_trade_coverage": True,
        "causal_provenance_enforced": True,
        "provider_watermark_proven": True,
        "bounded_lateness_proven": True,
        "exact_quote_event_clock": True,
        "exact_trade_event_clock": True,
        "required_event_time_inputs_complete": True,
    }
    values.update(overrides)
    return ValidatedReplayCoverage(**values)


class _StubSealedCoverage:
    """Stands in for a future sealed record that actually binds a checkpoint.

    No real ``ValidatedReplayCoverage`` can pass today, so without a stand-in the
    certified branch of ``grade_recap_phase_window`` and the certified
    accumulator in ``grade_manifest_phase_labels`` would be untested dead code.
    ``grade_recap_phase_window`` consumes coverage by duck typing (it calls
    ``failure_reasons`` and nothing else), so this is sufficient — and it does
    NOT weaken the real record, which is exercised unchanged above and in
    tests/test_ross_replay_benchmark.py.
    """

    def __init__(self, label_id: str, reasons: tuple[str, ...] = ()) -> None:
        self.label_id = label_id
        self._reasons = reasons

    def failure_reasons(self, **_kwargs) -> tuple[str, ...]:
        return self._reasons


def _diagnostic_coverage(
    window: ValidatedPhaseWindow | None = None,
    **overrides,
) -> DiagnosticReplayCoverage:
    """A clean hydrated-replay coverage record.

    Values are shaped like a real receipt rather than invented: ``tape_sources``
    uses the ``{table: {source: rows}}`` mapping the replay driver emits, and
    ``depth_rows=0`` is the ordinary case for an equity replay run without an L2
    mirror.
    """

    phase = window or _phase_window()
    values = {
        "label_id": phase.label_id,
        "symbol": phase.symbol,
        "coverage_start": phase.start_ts - timedelta(minutes=30),
        "coverage_end": phase.end_ts + timedelta(minutes=30),
        "tape_sources": {
            "iqfeed_trade_ticks": {"iqfeed_l1": 31286},
            NBBO_TAPE_TABLE: {"iqfeed_l1": 12040},
        },
        "tick_stride": 1,
        "grid_step_s": 1.0,
        "nbbo_vendor": "iqfeed_l1",
        "depth_rows": 0,
        "tree_sha": "0" * 40,
    }
    values.update(overrides)
    return DiagnosticReplayCoverage(**values)


def _winner(symbol: str = "VEEE", pnl_usd: float = 100.0) -> ReplayTradeObservation:
    return ReplayTradeObservation(
        symbol=symbol,
        entry_ts=BASE + timedelta(minutes=1),
        exit_ts=BASE + timedelta(minutes=2),
        pnl_usd=pnl_usd,
    )


# ─── the tier exists at all ──────────────────────────────────────────────────


def test_diagnostic_coverage_grades_a_hydrated_winner_the_sealed_tier_cannot():
    window = _phase_window()

    sealed_only = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        replay_coverage=_sealed_coverage(window),
    )
    diagnostic = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        diagnostic_coverage=_diagnostic_coverage(window),
    )

    # Unchanged: the sealed tier is still inert on data with no sealed binding.
    assert sealed_only.grade.status == "unscorable"
    assert sealed_only.grade.credit is None
    assert sealed_only.evidence_grade == "unscorable"
    # New: the same run is now measurable, at an explicitly lower tier.
    assert diagnostic.grade.status == "matched_trade"
    assert diagnostic.grade.credit == 1.0
    assert diagnostic.evidence_grade == "diagnostic_only"
    assert diagnostic.matching_trade_count == 1
    assert diagnostic.aggregate_pnl_usd == pytest.approx(100.0)


def test_diagnostic_credit_reports_the_unbound_seal_alongside_not_instead():
    window = _phase_window()

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        diagnostic_coverage=_diagnostic_coverage(window),
    )

    assert out.grade.credit == 1.0
    assert out.coverage_reasons == (NOT_BOUND,)
    # The caveat must be machine-separable from a certified-tier failure.
    assert all(reason.startswith(DIAGNOSTIC_REASON_PREFIX) for reason in out.coverage_reasons)


def test_diagnostic_row_carries_its_tape_provenance():
    window = _phase_window()

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        diagnostic_coverage=_diagnostic_coverage(window, tick_stride=1, grid_step_s=1.5),
    )

    provenance = out.diagnostic_provenance
    assert provenance is not None
    assert provenance["evidence_role"] == "diagnostic_only"
    assert provenance["nbbo_vendor"] == "iqfeed_l1"
    assert provenance["tick_stride"] == 1
    assert provenance["grid_step_s"] == 1.5
    assert provenance["tree_sha"] == "0" * 40
    assert provenance["tape_sources"][NBBO_TAPE_TABLE] == {"iqfeed_l1": 12040}


# ─── the sealed path is not weakened ─────────────────────────────────────────


def test_a_failing_sealed_record_cannot_be_laundered_by_a_diagnostic_one():
    """Precedence, not fallback.

    If a rejected seal fell through to the diagnostic tier, attaching a
    diagnostic record would become a bypass for every sealed-coverage check.
    """

    window = _phase_window()

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        replay_coverage=_sealed_coverage(window, uncapped=False),
        diagnostic_coverage=_diagnostic_coverage(window),
    )

    assert out.grade.status == "unscorable"
    assert out.grade.credit is None
    assert out.evidence_grade == "unscorable"
    assert out.coverage_reasons == (
        "sealed_decision_coverage_not_bound",
        "sampled_or_capped_tape",
    )
    assert not any(r.startswith(DIAGNOSTIC_REASON_PREFIX) for r in out.coverage_reasons)
    assert out.diagnostic_provenance is None


def test_a_passing_sealed_record_grades_certified_and_ignores_the_diagnostic():
    window = _phase_window()

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        replay_coverage=_StubSealedCoverage(window.label_id),
        diagnostic_coverage=_diagnostic_coverage(window),
    )

    assert out.grade.status == "matched_trade"
    assert out.grade.credit == 1.0
    assert out.evidence_grade == "certified"
    # A certified row carries no caveat and no diagnostic provenance.
    assert out.coverage_reasons == ()
    assert out.diagnostic_provenance is None


def test_supplying_neither_coverage_is_unchanged():
    window = _phase_window()

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
    )

    assert out.coverage_reasons == ("replay_coverage_missing",)
    assert out.evidence_grade == "unscorable"


def test_exact_phase_window_is_still_mandatory_under_diagnostic_mode():
    """Diagnostic mode relaxes the SEAL, never the after-the-fact window rule.

    An approximate recap timestamp must not become gradeable just because a
    hydrated tape is available for the day.
    """

    unverified = ValidatedPhaseWindow(
        **{
            **_phase_window().__dict__,
            "independently_verified": False,
            "evidence_source": "youtube_recap_approximate_time",
        }
    )

    missing = grade_recap_phase_window(
        label_id=VEEE_LABEL,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=None,
        diagnostic_coverage=_diagnostic_coverage(),
    )
    approximate = grade_recap_phase_window(
        label_id=VEEE_LABEL,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=unverified,
        diagnostic_coverage=_diagnostic_coverage(),
    )

    for out in (missing, approximate):
        assert out.grade.status == "unscorable"
        assert out.evidence_grade == "unscorable"
        assert out.coverage_reasons == ("phase_window_missing_or_unverified",)


# ─── the diagnostic record still fails closed ────────────────────────────────


@pytest.mark.parametrize(
    "overrides,expected_reason",
    [
        ({"label_id": "some_other_label"}, "coverage_label_mismatch"),
        ({"symbol": "PLSM"}, "coverage_symbol_mismatch"),
        (
            {"evidence_role": "after_fact_replay_grading_only"},
            "coverage_evidence_role_invalid",
        ),
        # A naive clock cannot be compared to a tz-aware phase boundary at all.
        ({"coverage_start": datetime(2026, 7, 13, 11, 30)}, "coverage_clock_missing_or_naive"),
        ({"coverage_start": BASE + timedelta(minutes=2)}, "phase_start_not_covered"),
        ({"coverage_end": BASE + timedelta(minutes=3)}, "phase_end_not_covered"),
        ({"tape_sources": {}}, "diagnostic_tape_sources_missing"),
        ({"tick_stride": 0}, "diagnostic_tick_stride_invalid"),
        ({"tick_stride": 1.5}, "diagnostic_tick_stride_invalid"),
        ({"grid_step_s": 0.0}, "diagnostic_grid_step_invalid"),
        ({"grid_step_s": float("nan")}, "diagnostic_grid_step_invalid"),
        ({"nbbo_vendor": "   "}, "diagnostic_nbbo_vendor_missing"),
        ({"depth_rows": -1}, "diagnostic_depth_rows_invalid"),
        ({"tree_sha": ""}, "diagnostic_tree_sha_missing"),
    ],
)
def test_disqualifying_diagnostic_defects_stay_unscorable(overrides, expected_reason):
    window = _phase_window()

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        diagnostic_coverage=_diagnostic_coverage(window, **overrides),
    )

    assert out.grade.status == "unscorable"
    assert out.grade.credit is None
    # A tier that failed its own checks may not stamp the row with its name.
    assert out.evidence_grade == "unscorable"
    assert expected_reason in out.coverage_reasons
    # Disqualifying reasons are bare, so they cannot be mistaken for the
    # alongside-credit caveat vocabulary.
    assert NOT_BOUND not in out.coverage_reasons


def test_two_hydration_sources_in_one_table_is_disqualifying():
    """Mirrors the replay driver's own abort.

    Two providers for one symbol-day means the replay read both tapes
    concatenated, so every print is doubled and no density figure is meaningful.
    """

    window = _phase_window()

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        diagnostic_coverage=_diagnostic_coverage(
            window,
            tape_sources={
                "iqfeed_trade_ticks": {"iqfeed_l1": 16933, "massive_ws_universe": 16933},
                NBBO_TAPE_TABLE: {"iqfeed_l1": 12040},
            },
        ),
    )

    assert out.grade.status == "unscorable"
    assert "diagnostic_tape_source_ambiguous" in out.coverage_reasons


def test_a_zero_row_source_does_not_make_a_table_ambiguous():
    """A provider present with zero rows contributed nothing to the tape."""

    window = _phase_window()

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        diagnostic_coverage=_diagnostic_coverage(
            window,
            tape_sources={
                "iqfeed_trade_ticks": {"iqfeed_l1": 31286, "massive_snapshot": 0},
                NBBO_TAPE_TABLE: {"iqfeed_l1": 12040},
            },
        ),
    )

    assert out.grade.status == "matched_trade"
    assert out.coverage_reasons == (NOT_BOUND,)


def test_hold_beyond_diagnostic_coverage_is_disqualifying():
    """The exit must be inside the tape, exactly as the sealed tier requires."""

    window = _phase_window()
    long_hold = ReplayTradeObservation(
        symbol="VEEE",
        entry_ts=BASE + timedelta(minutes=1),
        exit_ts=BASE + timedelta(minutes=90),
        pnl_usd=100.0,
    )

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[long_hold],
        phase_window=window,
        diagnostic_coverage=_diagnostic_coverage(window),
    )

    assert out.grade.status == "unscorable"
    assert "hold_exit_not_covered" in out.coverage_reasons


def test_a_subsampled_tape_still_grades_but_says_so():
    """Definitional, not a tuned threshold.

    Stride N feeds one print in every N to the FSM, so the run did not see the
    tape it is being credited against.  Crediting that silently would be the
    overclaim; refusing it outright would discard most existing bench runs.
    """

    window = _phase_window()

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        diagnostic_coverage=_diagnostic_coverage(window, tick_stride=8),
    )

    assert out.grade.credit == 1.0
    assert out.evidence_grade == "diagnostic_only"
    assert out.coverage_reasons == (NOT_BOUND, SUBSAMPLED)


def test_absent_depth_rows_is_a_configuration_not_a_defect():
    """No L2 mirrored is a legal run.  Failing it would be an invented rule."""

    window = _phase_window()

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        diagnostic_coverage=_diagnostic_coverage(window, depth_rows=0),
    )

    assert out.grade.status == "matched_trade"
    assert out.coverage_reasons == (NOT_BOUND,)


# ─── the epistemic rules survive the relaxation ──────────────────────────────


def test_diagnostic_tier_keeps_the_event_time_veto_rule():
    """A Ross winner CHILI rejected earns credit ONLY on fresh, certified,
    independently observable evidence that existed by the decision instant."""

    window = _phase_window()
    common = {
        "label_id": window.label_id,
        "symbol": "VEEE",
        "expected_action": "trade",
        "trades": [],
        "phase_window": window,
        "diagnostic_coverage": _diagnostic_coverage(window),
    }

    no_evidence = grade_recap_phase_window(**common)
    fresh = grade_recap_phase_window(
        **common,
        veto_evidence=EventTimeVetoEvidence(
            reason="certified spread exceeded executable limit",
            source="recorded_nbbo_gate",
            observed_at=window.decision_ts - timedelta(seconds=1),
            provenance_certified=True,
        ),
    )
    hindsight = grade_recap_phase_window(
        **common,
        veto_evidence=EventTimeVetoEvidence(
            reason="recap said it faded later",
            source="recap",
            observed_at=window.decision_ts + timedelta(seconds=1),
            provenance_certified=True,
        ),
    )
    uncertified = grade_recap_phase_window(
        **common,
        veto_evidence=EventTimeVetoEvidence(
            reason="uncertified feed flag",
            source="feed",
            observed_at=window.decision_ts - timedelta(seconds=1),
            provenance_certified=False,
        ),
    )

    assert no_evidence.grade.status == "missed_profitable_setup"
    assert no_evidence.grade.credit == 0.0
    assert fresh.grade.status == "valid_veto"
    assert fresh.grade.credit == 1.0
    assert hindsight.grade.status == "missed_profitable_setup"
    assert uncertified.grade.status == "missed_profitable_setup"
    # Even a fully-credited valid veto still declares its tier.
    assert fresh.evidence_grade == "diagnostic_only"
    assert fresh.coverage_reasons == (NOT_BOUND,)


def test_diagnostic_tier_keeps_wrong_phase_and_false_positive_verdicts():
    winner_window = _phase_window(
        label_id="2026-07-13_PLSM_front_side_first_dip",
        symbol="PLSM",
    )
    outside = ReplayTradeObservation(
        symbol="PLSM",
        entry_ts=BASE + timedelta(minutes=8),
        exit_ts=BASE + timedelta(minutes=9),
        pnl_usd=50.0,
    )
    wrong_phase = grade_recap_phase_window(
        label_id=winner_window.label_id,
        symbol="PLSM",
        expected_action="trade",
        trades=[outside],
        phase_window=winner_window,
        diagnostic_coverage=_diagnostic_coverage(winner_window),
    )

    reject_window = _phase_window(label_id=PLSM_REJECT_LABEL, symbol="PLSM")
    inside = ReplayTradeObservation(
        symbol="PLSM",
        entry_ts=BASE + timedelta(minutes=1),
        exit_ts=BASE + timedelta(minutes=2),
        pnl_usd=-50.0,
    )
    false_positive = grade_recap_phase_window(
        label_id=reject_window.label_id,
        symbol="PLSM",
        expected_action="reject",
        trades=[inside],
        phase_window=reject_window,
        diagnostic_coverage=_diagnostic_coverage(reject_window),
    )
    correct_reject = grade_recap_phase_window(
        label_id=reject_window.label_id,
        symbol="PLSM",
        expected_action="reject",
        trades=[],
        phase_window=reject_window,
        diagnostic_coverage=_diagnostic_coverage(reject_window),
    )

    assert wrong_phase.grade.status == "wrong_phase_trade"
    assert wrong_phase.grade.credit == 0.0
    assert false_positive.grade.status == "false_positive_trade"
    assert false_positive.grade.credit == 0.0
    assert correct_reject.grade.status == "matched_reject"
    assert correct_reject.grade.credit == 1.0


def test_a_losing_aggregate_inside_the_phase_earns_no_diagnostic_credit():
    """The diagnostic tier changes what proof is required, never how a decision
    is scored.  A net-losing sequence inside a labeled winner still fails."""

    window = _phase_window()
    trades = [
        ReplayTradeObservation(
            symbol="VEEE",
            entry_ts=BASE + timedelta(minutes=1),
            exit_ts=BASE + timedelta(minutes=2),
            pnl_usd=-140.0,
        ),
        ReplayTradeObservation(
            symbol="VEEE",
            entry_ts=BASE + timedelta(minutes=3),
            exit_ts=BASE + timedelta(minutes=4),
            pnl_usd=40.0,
        ),
    ]

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=trades,
        phase_window=window,
        diagnostic_coverage=_diagnostic_coverage(window),
    )

    assert out.matching_trade_count == 2
    assert out.aggregate_pnl_usd == pytest.approx(-100.0)
    assert out.grade.status == "unmatched_trade_outcome"
    assert out.grade.credit == 0.0
    assert out.evidence_grade == "diagnostic_only"


# ─── manifest-level accumulator separation ───────────────────────────────────


def _manifest() -> dict:
    path = (
        Path(__file__).parent
        / "fixtures"
        / "ross_replay"
        / "small_account_challenge_manifest.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_diagnostic_credit_never_merges_into_certified_credit():
    manifest = _manifest()
    veee_window = _phase_window(label_id=VEEE_LABEL, symbol="VEEE")
    plsm_window = _phase_window(label_id=PLSM_REJECT_LABEL, symbol="PLSM")

    report = grade_manifest_phase_labels(
        manifest,
        trades=[_winner()],
        phase_windows=[veee_window, plsm_window],
        # PLSM grades on a (stubbed) sealed binding; VEEE only on hydrated tape.
        replay_coverages=[_StubSealedCoverage(PLSM_REJECT_LABEL)],
        diagnostic_coverages=[_diagnostic_coverage(veee_window)],
    )

    assert report["label_count"] == 12
    # Certified figures see the PLSM correct-reject only.
    assert report["scorable_label_count"] == 1
    assert report["credit"] == pytest.approx(1.0)
    # Diagnostic figures see the VEEE winner only.
    assert report["diagnostic_label_count"] == 1
    assert report["credit_diagnostic"] == pytest.approx(1.0)
    # Every remaining label is still unscorable and is counted exactly once.
    assert report["unscorable_label_count"] == 10
    assert (
        report["scorable_label_count"]
        + report["diagnostic_label_count"]
        + report["unscorable_label_count"]
        == report["label_count"]
    )

    rows = {row["label_id"]: row for row in report["rows"]}
    assert rows[PLSM_REJECT_LABEL]["evidence_grade"] == "certified"
    assert rows[PLSM_REJECT_LABEL]["coverage_reasons"] == []
    assert "diagnostic_provenance" not in rows[PLSM_REJECT_LABEL]
    assert rows[VEEE_LABEL]["evidence_grade"] == "diagnostic_only"
    assert rows[VEEE_LABEL]["coverage_reasons"] == [NOT_BOUND]
    assert rows[VEEE_LABEL]["diagnostic_provenance"]["nbbo_vendor"] == "iqfeed_l1"


def test_manifest_without_diagnostic_coverages_is_byte_for_byte_the_old_verdict():
    """The added keys must not change any pre-existing figure."""

    manifest = _manifest()

    report = grade_manifest_phase_labels(manifest, trades=[])

    assert report["manifest_id"] == "ross_small_account_challenge_2026"
    assert report["label_count"] == 12
    assert report["scorable_label_count"] == 0
    assert report["unscorable_label_count"] == 12
    assert report["credit"] is None
    assert report["diagnostic_label_count"] == 0
    assert report["credit_diagnostic"] is None
    assert all(row["evidence_grade"] == "unscorable" for row in report["rows"])


def test_manifest_diagnostic_zero_credit_is_still_a_diagnostic_row():
    """A graded miss is a measurement.  Counting it as ``unscorable`` would hide
    exactly the failures the bench exists to surface."""

    manifest = _manifest()
    veee_window = _phase_window(label_id=VEEE_LABEL, symbol="VEEE")

    report = grade_manifest_phase_labels(
        manifest,
        trades=[],  # CHILI took nothing; the labeled winner is a miss.
        phase_windows=[veee_window],
        diagnostic_coverages=[_diagnostic_coverage(veee_window)],
    )

    row = next(r for r in report["rows"] if r["label_id"] == VEEE_LABEL)
    assert row["status"] == "missed_profitable_setup"
    assert row["credit"] == 0.0
    assert row["evidence_grade"] == "diagnostic_only"
    assert report["diagnostic_label_count"] == 1
    assert report["credit_diagnostic"] == pytest.approx(0.0)
    assert report["credit"] is None


# ─── replay receipt adapter ──────────────────────────────────────────────────


def _receipt(**overrides) -> dict:
    """A receipt shaped like the replay driver's own emission.

    Key set and nesting mirror the ``chili.replay_v3_fsm_window_result.v1``
    document; note the naive ``env`` clocks, which is what the driver actually
    writes because it builds those datetimes from bare env strings.
    """

    doc = {
        "schema": REPLAY_RECEIPT_SCHEMA,
        "label": "VEEE_2026-07-13",
        "arm": "g4_on",
        "tree": {"head": "h" * 40, "tree": "t" * 40, "branch": "seam/x", "dirty": False},
        "env": {
            "SYMBOL": "veee",
            "FRAME_START": "2026-07-13T11:30:00",
            "WIN_START": "2026-07-13T12:00:00",
            "WIN_END": "2026-07-13T12:35:00",
            "TICK_STRIDE": 1,
            "GRID_STEP_S": 1.5,
        },
        "tape_sources": {
            "iqfeed_trade_ticks": {"iqfeed_l1": 31286},
            NBBO_TAPE_TABLE: {"iqfeed_l1": 12040, "massive_ws_universe": 0},
        },
        "mirrored": {"tick_rows": 31286, "nbbo_rows": 12040, "depth_rows": 0},
    }
    doc.update(overrides)
    return doc


def test_receipt_adapter_maps_the_documented_keys():
    coverage = diagnostic_coverage_from_replay_receipt(
        _receipt(),
        label_id=VEEE_LABEL,
        naive_clock_tz=UTC,
    )

    assert coverage.label_id == VEEE_LABEL
    assert coverage.symbol == "VEEE"
    assert coverage.coverage_start == datetime(2026, 7, 13, 11, 30, tzinfo=UTC)
    assert coverage.coverage_end == datetime(2026, 7, 13, 12, 35, tzinfo=UTC)
    assert coverage.tick_stride == 1
    assert coverage.grid_step_s == pytest.approx(1.5)
    # A zero-row provider is present in the receipt but is not the vendor.
    assert coverage.nbbo_vendor == "iqfeed_l1"
    assert coverage.depth_rows == 0
    assert coverage.tree_sha == "t" * 40
    assert coverage.evidence_role == "diagnostic_only"


def test_receipt_adapter_refuses_to_guess_a_timezone():
    """The driver's env clocks are naive.  Assuming UTC here would be exactly
    the kind of silent, unverified constant this benchmark rejects."""

    with pytest.raises(ValueError, match="naive_clock_tz"):
        diagnostic_coverage_from_replay_receipt(_receipt(), label_id=VEEE_LABEL)


def test_receipt_adapter_requires_an_opt_in_for_a_dirty_build_tree():
    dirty = _receipt(tree={"head": "h" * 40, "tree": "t" * 40, "branch": "b", "dirty": True})

    with pytest.raises(ValueError, match="dirty working tree"):
        diagnostic_coverage_from_replay_receipt(dirty, label_id=VEEE_LABEL, naive_clock_tz=UTC)

    accepted = diagnostic_coverage_from_replay_receipt(
        dirty,
        label_id=VEEE_LABEL,
        naive_clock_tz=UTC,
        allow_dirty_tree=True,
    )
    assert accepted.tree_sha == "t" * 40


def test_receipt_adapter_fails_closed_on_a_schema_bump():
    with pytest.raises(ValueError, match="unsupported replay receipt schema"):
        diagnostic_coverage_from_replay_receipt(
            _receipt(schema="chili.replay_v3_fsm_window_result.v2"),
            label_id=VEEE_LABEL,
            naive_clock_tz=UTC,
        )


def test_receipt_adapter_refuses_an_ambiguous_or_absent_nbbo_vendor():
    two_vendors = _receipt(
        tape_sources={
            "iqfeed_trade_ticks": {"iqfeed_l1": 31286},
            NBBO_TAPE_TABLE: {"iqfeed_l1": 12040, "massive_ws_universe": 900},
        }
    )
    no_nbbo = _receipt(tape_sources={"iqfeed_trade_ticks": {"iqfeed_l1": 31286}})

    for doc in (two_vendors, no_nbbo):
        with pytest.raises(ValueError, match="exactly one"):
            diagnostic_coverage_from_replay_receipt(
                doc,
                label_id=VEEE_LABEL,
                naive_clock_tz=UTC,
            )


@pytest.mark.parametrize(
    "mutation,message",
    [
        ({"env": {}}, "SYMBOL"),
        ({"tape_sources": {}}, "tape_sources"),
        ({"mirrored": {"tick_rows": 1}}, "mirrored.depth_rows"),
        ({"tree": {"head": "h" * 40, "tree": "", "dirty": False}}, "tree.tree"),
    ],
)
def test_receipt_adapter_never_substitutes_a_default(mutation, message):
    with pytest.raises(ValueError, match=message):
        diagnostic_coverage_from_replay_receipt(
            _receipt(**mutation),
            label_id=VEEE_LABEL,
            naive_clock_tz=UTC,
        )


def test_receipt_adapter_output_grades_end_to_end():
    """The adapter's product must satisfy the grader it was built for."""

    window = ValidatedPhaseWindow(
        label_id=VEEE_LABEL,
        symbol="VEEE",
        start_ts=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        end_ts=datetime(2026, 7, 13, 12, 5, tzinfo=UTC),
        decision_ts=datetime(2026, 7, 13, 12, 0, 30, tzinfo=UTC),
        evidence_source="recorded_iqfeed_and_broker_phase_review",
        evidence_role="after_fact_grading_only",
        independently_verified=True,
    )
    coverage = diagnostic_coverage_from_replay_receipt(
        _receipt(),
        label_id=VEEE_LABEL,
        naive_clock_tz=UTC,
    )

    out = grade_recap_phase_window(
        label_id=VEEE_LABEL,
        symbol="VEEE",
        expected_action="trade",
        trades=[_winner()],
        phase_window=window,
        diagnostic_coverage=coverage,
    )

    assert out.grade.status == "matched_trade"
    assert out.evidence_grade == "diagnostic_only"
    assert out.coverage_reasons == (NOT_BOUND,)
