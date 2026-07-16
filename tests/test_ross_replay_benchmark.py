from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.services.trading.momentum_neural.ross_replay_benchmark import (
    EventTimeVetoEvidence,
    ReplayTradeObservation,
    TradablePathPoint,
    ValidatedPhaseWindow,
    ValidatedReplayCoverage,
    evaluate_long_trade_path,
    grade_manifest_phase_labels,
    grade_recap_decision,
    grade_recap_phase_window,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


def _point(seconds: int, bid: float, ask: float) -> TradablePathPoint:
    return TradablePathPoint(BASE + timedelta(seconds=seconds), bid, ask)


def test_long_path_metrics_use_ask_entry_bid_exit_and_stop_at_exit():
    points = [
        _point(0, 9.98, 10.0),
        _point(1, 10.48, 10.5),
        _point(2, 11.98, 12.0),
        _point(3, 10.98, 11.0),
        # This later high must not leak into the exited trade's MFE.
        _point(4, 19.98, 20.0),
    ]

    out = evaluate_long_trade_path(
        points,
        entry_ts=BASE,
        exit_ts=BASE + timedelta(seconds=3),
        qty=100,
        planned_stop_price=9.0,
    )

    assert out.entry_fill_price == pytest.approx(10.0)
    assert out.exit_fill_price == pytest.approx(10.98)
    assert out.peak_executable_bid == pytest.approx(11.98)
    assert out.gross_pnl_usd == pytest.approx(98.0)
    assert out.peak_open_profit_usd == pytest.approx(198.0)
    assert out.open_profit_giveback_usd == pytest.approx(100.0)
    assert out.open_profit_giveback_fraction == pytest.approx(100 / 198)
    assert out.realized_mfe_capture_ratio == pytest.approx(0.98 / 1.98)
    assert out.mfe_r == pytest.approx(1.98)
    assert out.mae_r == pytest.approx(0.02)
    assert out.path_points_used == 4


def test_long_path_metrics_preserve_more_than_one_hundred_percent_giveback():
    points = [
        _point(0, 9.98, 10.0),
        _point(1, 10.98, 11.0),
        _point(2, 9.48, 9.5),
    ]

    out = evaluate_long_trade_path(
        points,
        entry_ts=BASE,
        exit_ts=BASE + timedelta(seconds=2),
        qty=100,
        planned_stop_price=9.0,
    )

    assert out.gross_pnl_usd == pytest.approx(-52.0)
    assert out.peak_open_profit_usd == pytest.approx(98.0)
    assert out.open_profit_giveback_usd == pytest.approx(150.0)
    assert out.open_profit_giveback_fraction > 1.0
    assert out.realized_mfe_capture_ratio < 0.0


def test_invalid_stop_or_missing_exit_quote_fails_closed():
    points = [_point(0, 9.98, 10.0)]

    with pytest.raises(ValueError, match="no quote at or after exit_ts"):
        evaluate_long_trade_path(
            points,
            entry_ts=BASE,
            exit_ts=BASE + timedelta(seconds=1),
            qty=100,
        )
    with pytest.raises(ValueError, match="planned_stop_price must be below"):
        evaluate_long_trade_path(
            points,
            entry_ts=BASE,
            exit_ts=BASE,
            qty=100,
            planned_stop_price=10.0,
        )


def test_never_profitable_trade_has_zero_open_profit_giveback():
    points = [
        _point(0, 9.98, 10.0),
        _point(1, 9.48, 9.5),
    ]

    out = evaluate_long_trade_path(
        points,
        entry_ts=BASE,
        exit_ts=BASE + timedelta(seconds=1),
        qty=100,
        planned_stop_price=9.0,
    )

    assert out.peak_open_profit_usd == 0.0
    assert out.open_profit_giveback_usd == 0.0
    assert out.open_profit_giveback_fraction is None
    assert out.mae_r == pytest.approx(0.52)


def test_explicit_optimistic_fill_outside_quote_fails_closed():
    points = [_point(0, 9.98, 10.0)]

    with pytest.raises(ValueError, match="below the executable ask"):
        evaluate_long_trade_path(
            points,
            entry_ts=BASE,
            exit_ts=BASE,
            qty=100,
            entry_fill_price=9.99,
        )
    with pytest.raises(ValueError, match="above the executable bid"):
        evaluate_long_trade_path(
            points,
            entry_ts=BASE,
            exit_ts=BASE,
            qty=100,
            exit_fill_price=9.99,
        )


def test_recap_grade_scores_correct_no_trade_and_valid_independent_veto():
    no_trade = grade_recap_decision(
        expected_action="reject",
        actual_action="reject",
        veto_reason="spread too wide",
    )
    valid_veto = grade_recap_decision(
        expected_action="trade",
        actual_action="reject",
        decision_ts=BASE,
        veto_evidence=EventTimeVetoEvidence(
            reason="feed stale at decision time",
            source="causal_feed_health",
            observed_at=BASE - timedelta(seconds=1),
            provenance_certified=True,
        ),
    )

    assert no_trade.status == "matched_reject"
    assert no_trade.credit == 1.0
    assert valid_veto.status == "valid_veto"
    assert valid_veto.credit == 1.0


def test_recap_grade_does_not_accept_unproven_excuse_for_missed_winner():
    out = grade_recap_decision(
        expected_action="trade",
        actual_action="reject",
        veto_reason="recap-only hindsight",
    )

    assert out.status == "missed_profitable_setup"
    assert out.credit == 0.0


def test_recap_grade_requires_right_phase_and_executable_outcome():
    wrong_phase = grade_recap_decision(
        expected_action="trade",
        actual_action="trade",
        phase_window_matched=False,
        trade_outcome_acceptable=True,
    )
    losing_match = grade_recap_decision(
        expected_action="trade",
        actual_action="trade",
        phase_window_matched=True,
        trade_outcome_acceptable=False,
    )
    matched = grade_recap_decision(
        expected_action="trade",
        actual_action="trade",
        phase_window_matched=True,
        trade_outcome_acceptable=True,
    )

    assert wrong_phase.status == "wrong_phase_trade"
    assert wrong_phase.credit == 0.0
    assert losing_match.status == "unmatched_trade_outcome"
    assert losing_match.credit == 0.0
    assert matched.status == "matched_trade"
    assert matched.credit == 1.0


def test_reject_label_counts_miss_as_correct_no_trade():
    out = grade_recap_decision(
        expected_action="reject",
        actual_action="miss",
    )

    assert out.status == "matched_reject"
    assert out.credit == 1.0


def test_future_or_uncertified_veto_evidence_gets_no_credit():
    future = grade_recap_decision(
        expected_action="trade",
        actual_action="reject",
        decision_ts=BASE,
        veto_evidence=EventTimeVetoEvidence(
            reason="later recap excuse",
            source="recap",
            observed_at=BASE + timedelta(seconds=1),
            provenance_certified=True,
        ),
    )
    uncertified = grade_recap_decision(
        expected_action="trade",
        actual_action="reject",
        decision_ts=BASE,
        veto_evidence=EventTimeVetoEvidence(
            reason="uncertified feed flag",
            source="feed",
            observed_at=BASE - timedelta(seconds=1),
            provenance_certified=False,
        ),
    )

    assert future.status == "missed_profitable_setup"
    assert uncertified.status == "missed_profitable_setup"


def _phase_window(
    *,
    label_id: str = "2026-07-13_VEEE_fresh_front_side_sequence",
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


def _replay_coverage(
    window: ValidatedPhaseWindow | None = None,
    **overrides,
) -> ValidatedReplayCoverage:
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


def test_phase_benchmark_requires_independently_validated_exact_window():
    trade = ReplayTradeObservation(
        symbol="VEEE",
        entry_ts=BASE + timedelta(minutes=1),
        exit_ts=BASE + timedelta(minutes=2),
        pnl_usd=100.0,
    )

    missing = grade_recap_phase_window(
        label_id="2026-07-13_VEEE_fresh_front_side_sequence",
        symbol="VEEE",
        expected_action="trade",
        trades=[trade],
        phase_window=None,
    )
    unverified_window = _phase_window()
    unverified_window = ValidatedPhaseWindow(
        **{
            **unverified_window.__dict__,
            "independently_verified": False,
            "evidence_source": "youtube_recap_approximate_time",
        }
    )
    unverified = grade_recap_phase_window(
        label_id="2026-07-13_VEEE_fresh_front_side_sequence",
        symbol="VEEE",
        expected_action="trade",
        trades=[trade],
        phase_window=unverified_window,
    )

    assert missing.grade.status == "unscorable"
    assert unverified.grade.status == "unscorable"


def test_phase_benchmark_requires_separate_causal_coverage_proof():
    window = _phase_window()
    trade = ReplayTradeObservation(
        symbol="VEEE",
        entry_ts=BASE + timedelta(minutes=1),
        exit_ts=BASE + timedelta(minutes=2),
        pnl_usd=100.0,
    )

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol=window.symbol,
        expected_action="trade",
        trades=[trade],
        phase_window=window,
    )

    assert out.grade.status == "unscorable"
    assert out.grade.credit is None
    assert out.coverage_reasons == ("replay_coverage_missing",)


def test_phase_benchmark_rejects_sampled_or_proxy_clock_coverage():
    window = _phase_window()
    coverage = _replay_coverage(
        window,
        uncapped=False,
        exact_quote_event_clock=False,
        provider_watermark_proven=False,
    )

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol=window.symbol,
        expected_action="trade",
        trades=[],
        phase_window=window,
        replay_coverage=coverage,
    )

    assert out.grade.status == "unscorable"
    assert out.grade.credit is None
    assert out.coverage_reasons == (
        "sealed_decision_coverage_not_bound",
        "sampled_or_capped_tape",
        "provider_watermark_unproven",
        "exact_quote_event_clock_unavailable",
    )


def test_phase_benchmark_requires_hold_exit_coverage_for_matching_trade():
    window = _phase_window()
    trade = ReplayTradeObservation(
        symbol="VEEE",
        entry_ts=BASE + timedelta(minutes=1),
        exit_ts=BASE + timedelta(minutes=20),
        pnl_usd=100.0,
    )
    coverage = _replay_coverage(
        window,
        coverage_end_ts=BASE + timedelta(minutes=10),
    )

    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol=window.symbol,
        expected_action="trade",
        trades=[trade],
        phase_window=window,
        replay_coverage=coverage,
    )

    assert out.grade.status == "unscorable"
    assert out.coverage_reasons == (
        "sealed_decision_coverage_not_bound",
        "hold_exit_not_covered",
    )


def test_phase_benchmark_aggregates_mixed_subtrades_inside_profitable_sequence():
    trades = [
        ReplayTradeObservation(
            symbol="VEEE",
            entry_ts=BASE + timedelta(minutes=1),
            exit_ts=BASE + timedelta(minutes=2),
            pnl_usd=-40.0,
        ),
        ReplayTradeObservation(
            symbol="VEEE",
            entry_ts=BASE + timedelta(minutes=3),
            exit_ts=BASE + timedelta(minutes=4),
            pnl_usd=140.0,
        ),
    ]

    window = _phase_window()
    out = grade_recap_phase_window(
        label_id="2026-07-13_VEEE_fresh_front_side_sequence",
        symbol="VEEE",
        expected_action="trade",
        trades=trades,
        phase_window=window,
        replay_coverage=_replay_coverage(window),
    )

    assert out.matching_trade_count == 0
    assert out.aggregate_pnl_usd is None
    assert out.grade.status == "unscorable"
    assert out.coverage_reasons == ("sealed_decision_coverage_not_bound",)


def test_phase_benchmark_rejects_late_backside_trade_and_flags_wrong_phase():
    reject_window = _phase_window(
        label_id="2026-07-13_PLSM_backside_fomo_curl",
        symbol="PLSM",
    )
    late_trade = ReplayTradeObservation(
        symbol="PLSM",
        entry_ts=BASE + timedelta(minutes=1),
        exit_ts=BASE + timedelta(minutes=2),
        pnl_usd=-50.0,
    )
    false_positive = grade_recap_phase_window(
        label_id=reject_window.label_id,
        symbol="PLSM",
        expected_action="reject",
        trades=[late_trade],
        phase_window=reject_window,
        replay_coverage=_replay_coverage(reject_window),
    )

    winner_window = _phase_window(
        label_id="2026-07-13_PLSM_front_side_first_dip",
        symbol="PLSM",
    )
    outside_trade = ReplayTradeObservation(
        symbol="PLSM",
        entry_ts=BASE + timedelta(minutes=8),
        exit_ts=BASE + timedelta(minutes=9),
        pnl_usd=50.0,
    )
    wrong_phase = grade_recap_phase_window(
        label_id=winner_window.label_id,
        symbol="PLSM",
        expected_action="trade",
        trades=[outside_trade],
        phase_window=winner_window,
        replay_coverage=_replay_coverage(winner_window),
    )

    assert false_positive.grade.status == "unscorable"
    assert wrong_phase.grade.status == "unscorable"
    assert false_positive.coverage_reasons == (
        "sealed_decision_coverage_not_bound",
    )
    assert wrong_phase.coverage_reasons == (
        "sealed_decision_coverage_not_bound",
    )


def test_phase_benchmark_valid_veto_must_exist_by_exact_decision_time():
    window = _phase_window()
    out = grade_recap_phase_window(
        label_id=window.label_id,
        symbol="VEEE",
        expected_action="trade",
        trades=[],
        phase_window=window,
        replay_coverage=_replay_coverage(window),
        veto_evidence=EventTimeVetoEvidence(
            reason="certified spread exceeded executable limit",
            source="recorded_nbbo_gate",
            observed_at=window.decision_ts - timedelta(seconds=1),
            provenance_certified=True,
        ),
    )

    assert out.grade.status == "unscorable"
    assert out.grade.credit is None
    assert out.coverage_reasons == ("sealed_decision_coverage_not_bound",)


def test_playlist_manifest_join_stays_unscorable_until_exact_windows_exist():
    path = (
        Path(__file__).parent
        / "fixtures"
        / "ross_replay"
        / "small_account_challenge_manifest.json"
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    no_windows = grade_manifest_phase_labels(
        manifest,
        trades=[],
    )

    assert no_windows["label_count"] == 12
    assert no_windows["scorable_label_count"] == 0
    assert no_windows["credit"] is None

    trade = ReplayTradeObservation(
        symbol="VEEE",
        entry_ts=BASE + timedelta(minutes=1),
        exit_ts=BASE + timedelta(minutes=2),
        pnl_usd=100.0,
    )
    one_window = grade_manifest_phase_labels(
        manifest,
        trades=[trade],
        phase_windows=[
            _phase_window(
                label_id="2026-07-13_VEEE_fresh_front_side_pullback"
            )
        ],
        replay_coverages=[
            _replay_coverage(
                _phase_window(
                    label_id="2026-07-13_VEEE_fresh_front_side_pullback"
                )
            )
        ],
    )
    vee = next(
        row
        for row in one_window["rows"]
        if row["label_id"] == "2026-07-13_VEEE_fresh_front_side_pullback"
    )

    assert one_window["scorable_label_count"] == 0
    assert vee["status"] == "unscorable"
    assert vee["aggregate_pnl_usd"] is None
    assert vee["coverage_reasons"] == ["sealed_decision_coverage_not_bound"]


def test_july_13_enrichment_fixture_creates_no_canonical_labels():
    path = Path(__file__).parent / "fixtures" / "ross_replay" / "2026-07-13_RZbM0qXOFbc.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["evidence_role"] == (
        "after_fact_enrichment_only_never_canonical_label_or_event_time_input"
    )
    assert payload["canonical_source_video_id"] == "S2sOq-stPgA"
    assert payload["creates_canonical_labels"] is False
    assert payload["phase_labels"] == []
    assert payload["verified_aggregate_day_pnl_usd"] == {
        "main_account": 8430.49,
        "small_account": 205.18,
    }
    assert "-2924" not in json.dumps(payload, sort_keys=True)
    assert "34678.35" not in json.dumps(payload, sort_keys=True)

    contexts = payload["enrichment_context"]
    canonical_ids = [
        row["canonical_label_id"]
        for row in contexts
        if row["canonical_label_id"] is not None
    ]
    assert canonical_ids == [
        "S2sOq-stPgA::QTTB::veto",
        "S2sOq-stPgA::PLSM::first_dip",
        "S2sOq-stPgA::PLSM::backside",
        "S2sOq-stPgA::VEEE::pullback",
    ]
    assert not any(value.startswith("RZbM0qXOFbc::") for value in canonical_ids)
    qttb = contexts[0]
    veee = contexts[3]
    ve = contexts[4]
    assert qttb["headline_context_time_et"] == "06:59:00"
    assert qttb["context_role"] == "headline_context_never_phase_boundary"
    assert veee["scanner_observations_et"] == [
        {"time": "08:58:59", "price": 8.89, "volume": 2750000},
        {"time": "08:59:08", "price": 9.72, "volume": 3010000},
    ]
    assert veee["approx_execution_window_et"] == {
        "start": "09:17:30",
        "end": "09:21:10",
        "certainty": "approximate",
    }
    assert veee["exact_order_time"] is None
    assert ve["canonical_label_id"] is None
    assert ve["symbol"] == "VE"
    assert ve["account"] == "main"
    assert ve["context_role"] == (
        "different_symbol_cross_account_campaign_never_canonical_label"
    )
    assert ve["campaign_start_approx_et"] == "08:50:00"
    assert ve["campaign_pnl_usd_approx"] == 34000


def test_july_13_plsm_winner_cannot_receive_replay_credit_from_late_capture():
    path = Path(__file__).parent / "fixtures" / "ross_replay" / "2026-07-13_RZbM0qXOFbc.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    audit = payload["local_capture_audit"]["PLSM_front_side_first_dip"]

    assert audit["coverage_status"] == "coverage_unavailable"
    assert audit["first_massive_quote"]["bid"] > audit["benchmark_entry_price"]
    assert audit["first_iqfeed_trade"]["price"] > audit["benchmark_entry_price"]
    assert audit["live_runner_started"]["mid"] > audit["benchmark_entry_price"]
    assert {
        "pretrigger_quote_trade_l2_prefix",
        "exact_quote_provider_event_clock",
        "provider_watermark_and_bounded_lateness",
    }.issubset(set(audit["missing_proofs"]))
