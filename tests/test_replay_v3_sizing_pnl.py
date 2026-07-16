"""Replay v3 sizing/add policy coverage for the CHILI momentum lane."""

from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.replay_v3 import (
    MicroBar,
    ReplayBrokerOutcome,
    ReplayPolicy,
    ReplaySchedulerCandidate,
    ReplaySchedulerLiveSnapshotStep,
    ReplaySchedulerTimelineStep,
    ReplayVenueState,
    ReplaySetup,
    SizingPlan,
    attribute_scheduler_timeline_pnl,
    evidence_rows,
    missed_winner_impact,
    replay_broker_outcomes_from_rows,
    replay_scheduler_batch,
    replay_scheduler_candidates_from_live_rows,
    replay_scheduler_timeline,
    replay_scheduler_timeline_from_live_snapshots,
    replay_policies,
)
from app.services.trading.momentum_neural.live_replay_audit import _scheduler_priority_evidence
from app.services.trading.momentum_neural.live_replay_audit import _certification_boundary


def _lgps_style_setup() -> ReplaySetup:
    return ReplaySetup(
        symbol="LGPS",
        entry_price=10.00,
        stop_price=9.70,
        sizing=SizingPlan(full_qty=178.0, probe_qty=44.0, remainder_qty=134.0),
    )


def test_confirmed_winner_compares_probe_adaptive_and_full_size_pnl() -> None:
    setup = _lgps_style_setup()
    tape = [
        MicroBar(
            ts="2026-07-01T13:30:00Z",
            open=9.95,
            high=10.08,
            low=9.90,
            close=10.00,
            entry_signal=True,
            state="entry_signal_ross_pullback_break",
        ),
        MicroBar(
            ts="2026-07-01T13:31:00Z",
            open=10.00,
            high=10.26,
            low=10.00,
            close=10.20,
            confirm_remainder=True,
            confirmation_price=10.20,
            state="explicit_confirmation_strength_holds",
        ),
        MicroBar(
            ts="2026-07-01T13:32:00Z",
            open=10.20,
            high=10.68,
            low=10.15,
            close=10.60,
            state="winner_extends",
        ),
        MicroBar(
            ts="2026-07-01T13:33:00Z",
            open=10.60,
            high=10.86,
            low=10.50,
            close=10.80,
            exit_signal=True,
            exit_price=10.80,
            state="explicit_exit_into_strength",
        ),
    ]

    results = replay_policies(setup, tape)
    probe = results[ReplayPolicy.PROBE_ONLY]
    adaptive = results[ReplayPolicy.ADAPTIVE_STARTER_REMAINDER]
    full = results[ReplayPolicy.FULL_SIZE_SINGLE_ENTRY]

    assert probe.realized_pnl_usd == pytest.approx(35.20)
    assert adaptive.realized_pnl_usd == pytest.approx(115.60)
    assert full.realized_pnl_usd == pytest.approx(142.40)
    assert adaptive.realized_pnl_usd > probe.realized_pnl_usd
    assert full.realized_pnl_usd > adaptive.realized_pnl_usd

    missed = missed_winner_impact(results)
    assert missed["baseline_missed_vs_reference_usd"] == pytest.approx(107.20)
    assert missed["adaptive_reclaimed_vs_baseline_usd"] == pytest.approx(80.40)
    assert missed["adaptive_remaining_gap_vs_reference_usd"] == pytest.approx(26.80)
    assert missed["adaptive_reclaim_rate"] == pytest.approx(0.75)

    assert "confirmation_add" not in probe.event_names
    assert "confirmation_add" in adaptive.event_names
    assert "confirmation_add" not in full.event_names
    assert [point.qty for point in adaptive.quantity_path] == [44.0, 178.0, 178.0, 0]
    assert [point.qty for point in probe.quantity_path] == [44.0, 44.0, 44.0, 0]
    assert [point.qty for point in full.quantity_path] == [178.0, 178.0, 178.0, 0]

    for result in results.values():
        assert result.unrealized_pnl_usd == pytest.approx(0.0)
        assert result.final_qty == pytest.approx(0.0)
        assert result.exit_reason == "exit_signal"
        assert result.stop_exit_bounded is True
        assert result.mae_usd <= 0

    rows = evidence_rows(results)
    assert rows[0]["policy"] == "probe_only"
    assert rows[1]["policy"] == "adaptive_starter_remainder"
    assert rows[2]["policy"] == "full_size_single_entry"
    assert rows[1]["expectancy_usd_per_trade"] == pytest.approx(115.60)
    assert rows[1]["events"] == ["probe_entry", "confirmation_add", "signal_exit"]


def test_toxic_non_confirmed_case_does_not_add_and_stop_loss_is_bounded() -> None:
    setup = _lgps_style_setup()
    tape = [
        MicroBar(
            ts="2026-07-01T13:30:00Z",
            open=9.95,
            high=10.05,
            low=9.90,
            close=10.00,
            entry_signal=True,
            state="entry_signal_ross_pullback_break",
        ),
        MicroBar(
            ts="2026-07-01T13:31:00Z",
            open=10.00,
            high=10.04,
            low=9.82,
            close=9.86,
            confirm_remainder=False,
            state="toxic_no_confirmation_fades",
        ),
        MicroBar(
            ts="2026-07-01T13:32:00Z",
            open=9.86,
            high=9.90,
            low=9.69,
            close=9.72,
            confirm_remainder=False,
            state="stop_touched_before_any_valid_add",
        ),
    ]

    results = replay_policies(setup, tape)
    probe = results[ReplayPolicy.PROBE_ONLY]
    adaptive = results[ReplayPolicy.ADAPTIVE_STARTER_REMAINDER]
    full = results[ReplayPolicy.FULL_SIZE_SINGLE_ENTRY]

    assert adaptive.event_names == ["probe_entry", "stop_exit"]
    assert "confirmation_add" not in adaptive.event_names
    assert adaptive.realized_pnl_usd == pytest.approx(probe.realized_pnl_usd)
    assert adaptive.realized_pnl_usd == pytest.approx(-13.20)
    assert full.realized_pnl_usd == pytest.approx(-53.40)
    assert full.realized_pnl_usd < adaptive.realized_pnl_usd

    assert adaptive.stop_exit_bounded is True
    assert adaptive.stop_loss_bound_usd == pytest.approx(13.20)
    assert adaptive.mae_usd == pytest.approx(-13.64)
    assert adaptive.max_drawdown_usd == pytest.approx(13.20)
    assert [point.qty for point in adaptive.quantity_path] == [44.0, 44.0, 0]

    for result in results.values():
        assert result.unrealized_pnl_usd == pytest.approx(0.0)
        assert result.final_qty == pytest.approx(0.0)
        assert result.exit_reason == "stop"
        assert result.stop_exit_bounded is True


def test_scheduler_replay_prefilters_unavailable_venue_without_starving_equity() -> None:
    candidates = [
        ReplaySchedulerCandidate(
            session_id=1,
            symbol="BTC-USD",
            venue="coinbase",
            execution_family="coinbase_spot",
            state="queued_live",
            quality_score=0.99,
            queued_age_seconds=120,
            expires_in_seconds=10,
            tick_armed=True,
            expected_pnl_usd=25,
        ),
        ReplaySchedulerCandidate(
            session_id=2,
            symbol="JEM",
            venue="robinhood",
            execution_family="robinhood_agentic_mcp",
            state="queued_live",
            quality_score=0.80,
            queued_age_seconds=30,
            expires_in_seconds=40,
            tick_armed=True,
            expected_pnl_usd=15,
        ),
    ]
    venues = [
        ReplayVenueState("coinbase", "coinbase_spot", adapter_available=False),
        ReplayVenueState("robinhood", "robinhood_agentic_mcp", adapter_available=True),
    ]

    out = replay_scheduler_batch(candidates, venues, capacity_limit=1, order_call_budget=1, risk_budget_slots=1)

    assert out.selected_session_ids == [2]
    assert out.free_skip_count == 1
    assert out.useful_capacity_used == 1
    by_id = {d.session_id: d for d in out.decisions}
    assert by_id[1].reason == "venue_adapter_unavailable"
    assert by_id[1].consumes_capacity is False
    assert by_id[2].reason == "selected"
    assert by_id[2].consumes_capacity is True


def test_scheduler_replay_priority_and_budget_missed_pnl_are_explicit() -> None:
    candidates = [
        ReplaySchedulerCandidate(
            session_id=10,
            symbol="A",
            venue="robinhood",
            execution_family="robinhood_agentic_mcp",
            state="watch_break_level",
            quality_score=0.70,
            queued_age_seconds=5,
            expires_in_seconds=60,
            tick_armed=True,
            expected_pnl_usd=10,
        ),
        ReplaySchedulerCandidate(
            session_id=11,
            symbol="B",
            venue="robinhood",
            execution_family="robinhood_agentic_mcp",
            state="queued_live",
            quality_score=0.95,
            queued_age_seconds=50,
            expires_in_seconds=15,
            tick_armed=True,
            expected_pnl_usd=30,
        ),
        ReplaySchedulerCandidate(
            session_id=12,
            symbol="C",
            venue="robinhood",
            execution_family="robinhood_agentic_mcp",
            state="queued_live",
            quality_score=0.20,
            queued_age_seconds=10,
            expires_in_seconds=120,
            tick_armed=False,
            expected_pnl_usd=5,
        ),
    ]
    venues = [
        ReplayVenueState(
            "robinhood",
            "robinhood_agentic_mcp",
            adapter_available=True,
            order_call_budget=1,
            risk_budget_slots=2,
        )
    ]

    out = replay_scheduler_batch(candidates, venues, capacity_limit=3, order_call_budget=3, risk_budget_slots=3)

    assert out.selected_session_ids == [11]
    assert out.order_call_budget_used == 1
    assert out.risk_budget_used == 1
    assert out.missed_expected_pnl_usd == pytest.approx(15.0)
    by_id = {d.session_id: d for d in out.decisions}
    assert by_id[11].priority_score > by_id[10].priority_score > by_id[12].priority_score
    assert by_id[10].reason == "order_call_budget_exhausted"
    assert by_id[12].reason == "order_call_budget_exhausted"


def test_scheduler_replay_protects_managed_position_before_new_entry_score() -> None:
    candidates = [
        ReplaySchedulerCandidate(
            session_id=20,
            symbol="HELD",
            venue="robinhood",
            execution_family="robinhood_agentic_mcp",
            state="live_entered",
            quality_score=0.20,
            queued_age_seconds=5,
            expires_in_seconds=120,
            tick_armed=False,
            expected_pnl_usd=4,
        ),
        ReplaySchedulerCandidate(
            session_id=21,
            symbol="SHINY",
            venue="robinhood",
            execution_family="robinhood_agentic_mcp",
            state="queued_live",
            quality_score=0.99,
            queued_age_seconds=80,
            expires_in_seconds=10,
            tick_armed=True,
            expected_pnl_usd=30,
        ),
    ]

    out = replay_scheduler_batch(
        candidates,
        [ReplayVenueState("robinhood", "robinhood_agentic_mcp", adapter_available=True)],
        capacity_limit=1,
        order_call_budget=1,
        risk_budget_slots=1,
    )

    assert out.selected_session_ids == [20]
    by_id = {d.session_id: d for d in out.decisions}
    assert by_id[20].reason == "selected"
    assert by_id[21].reason == "capacity_exhausted"


def test_scheduler_replay_builds_candidates_from_live_session_rows() -> None:
    rows = [
        {
            "id": 201,
            "symbol": "BTC-USD",
            "venue": "coinbase",
            "execution_family": "coinbase_spot",
            "state": "queued_live",
            "created_at": "2026-07-01T17:59:00Z",
            "risk_snapshot_json": {
                "viability_score": 0.99,
                "expires_at_utc": "2026-07-01T18:02:00Z",
                "momentum_live_execution": {"watch_break_level": 100.0, "expected_pnl_usd": 24.0},
            },
        },
        {
            "id": 202,
            "symbol": "JEM",
            "venue": "robinhood",
            "execution_family": "robinhood_agentic_mcp",
            "state": "queued_live",
            "created_at": "2026-07-01T17:59:30Z",
            "risk_snapshot_json": {
                "momentum_risk": {"viability_score": 0.72},
                "expires_at_utc": "2026-07-01T18:03:00Z",
                "momentum_live_execution": {"watch_break_level": 3.95, "expected_pnl_usd": 18.0},
            },
        },
    ]
    candidates = replay_scheduler_candidates_from_live_rows(rows, now="2026-07-01T18:00:00Z")

    assert [c.session_id for c in candidates] == [201, 202]
    assert candidates[0].quality_score == pytest.approx(0.99)
    assert candidates[0].queued_age_seconds == pytest.approx(60.0)
    assert candidates[0].expires_in_seconds == pytest.approx(120.0)
    assert candidates[0].tick_armed is True
    assert candidates[1].quality_score == pytest.approx(0.72)
    assert candidates[1].expected_pnl_usd == pytest.approx(18.0)


def test_scheduler_replay_live_rows_do_not_starve_equity_behind_unavailable_adapter() -> None:
    rows = [
        {
            "id": 301,
            "symbol": "BTC-USD",
            "venue": "coinbase",
            "execution_family": "coinbase_spot",
            "state": "queued_live",
            "created_at": "2026-07-01T17:58:00Z",
            "risk_snapshot_json": {
                "viability_score": 0.99,
                "expires_at_utc": "2026-07-01T18:01:00Z",
                "momentum_live_execution": {"watch_break_level": 100.0, "expected_pnl_usd": 25.0},
            },
        },
        {
            "id": 302,
            "symbol": "JEM",
            "venue": "robinhood",
            "execution_family": "robinhood_agentic_mcp",
            "state": "queued_live",
            "created_at": "2026-07-01T17:59:30Z",
            "risk_snapshot_json": {
                "viability_score": 0.65,
                "expires_at_utc": "2026-07-01T18:04:00Z",
                "momentum_live_execution": {"watch_break_level": 3.95, "expected_pnl_usd": 12.0},
            },
        },
    ]
    candidates = replay_scheduler_candidates_from_live_rows(rows, now="2026-07-01T18:00:00Z")
    out = replay_scheduler_batch(
        candidates,
        [
            ReplayVenueState("coinbase", "coinbase_spot", adapter_available=False),
            ReplayVenueState("robinhood", "robinhood_agentic_mcp", adapter_available=True),
        ],
        capacity_limit=1,
        order_call_budget=1,
        risk_budget_slots=1,
    )

    assert out.selected_session_ids == [302]
    by_id = {d.session_id: d for d in out.decisions}
    assert by_id[301].reason == "venue_adapter_unavailable"
    assert by_id[301].consumes_capacity is False
    assert by_id[302].reason == "selected"


def test_scheduler_timeline_delayed_candidate_is_not_counted_as_missed_if_selected_later() -> None:
    venues = (
        ReplayVenueState(
            "robinhood",
            "robinhood_agentic_mcp",
            adapter_available=True,
            order_call_budget=1,
            risk_budget_slots=1,
        ),
    )
    first = ReplaySchedulerTimelineStep(
        ts="2026-07-01T18:00:00Z",
        venue_states=venues,
        candidates=(
            ReplaySchedulerCandidate(
                session_id=401,
                symbol="FAST",
                venue="robinhood",
                execution_family="robinhood_agentic_mcp",
                state="watch_break_level",
                quality_score=0.95,
                queued_age_seconds=40,
                expires_in_seconds=20,
                tick_armed=True,
                expected_pnl_usd=30.0,
            ),
            ReplaySchedulerCandidate(
                session_id=402,
                symbol="SLOW",
                venue="robinhood",
                execution_family="robinhood_agentic_mcp",
                state="queued_live",
                quality_score=0.70,
                queued_age_seconds=10,
                expires_in_seconds=90,
                tick_armed=True,
                expected_pnl_usd=12.0,
            ),
        ),
    )
    second = ReplaySchedulerTimelineStep(
        ts="2026-07-01T18:00:10Z",
        venue_states=venues,
        candidates=(
            ReplaySchedulerCandidate(
                session_id=402,
                symbol="SLOW",
                venue="robinhood",
                execution_family="robinhood_agentic_mcp",
                state="watch_break_level",
                quality_score=0.70,
                queued_age_seconds=20,
                expires_in_seconds=80,
                tick_armed=True,
                expected_pnl_usd=12.0,
            ),
        ),
    )

    out = replay_scheduler_timeline(
        [first, second],
        default_capacity_limit=2,
        default_order_call_budget=1,
        default_risk_budget_slots=1,
    )

    assert out.selected_session_ids == [401, 402]
    assert out.selected_expected_pnl_usd == pytest.approx(42.0)
    assert out.missed_expected_pnl_usd == pytest.approx(0.0)
    assert out.pending_session_ids == []
    assert out.decision_trace[402][0]["reason"] == "order_call_budget_exhausted"
    assert out.decision_trace[402][1]["reason"] == "selected"

    evidence = _scheduler_priority_evidence(out)
    assert evidence["budget_skip_count"] == 1
    assert evidence["delayed_then_selected_count"] == 1
    assert evidence["scheduler_priority_claim_ready"] is True


def test_scheduler_timeline_records_expired_budget_miss_once_with_reason() -> None:
    venues = (
        ReplayVenueState(
            "robinhood",
            "robinhood_agentic_mcp",
            adapter_available=True,
            order_call_budget=1,
            risk_budget_slots=1,
        ),
    )
    first = ReplaySchedulerTimelineStep(
        ts="2026-07-01T18:00:00Z",
        venue_states=venues,
        candidates=(
            ReplaySchedulerCandidate(
                session_id=501,
                symbol="AONE",
                venue="robinhood",
                execution_family="robinhood_agentic_mcp",
                state="watch_break_level",
                quality_score=0.99,
                queued_age_seconds=60,
                expires_in_seconds=20,
                tick_armed=True,
                expected_pnl_usd=40.0,
            ),
            ReplaySchedulerCandidate(
                session_id=502,
                symbol="BTWO",
                venue="robinhood",
                execution_family="robinhood_agentic_mcp",
                state="queued_live",
                quality_score=0.90,
                queued_age_seconds=50,
                expires_in_seconds=5,
                tick_armed=True,
                expected_pnl_usd=25.0,
            ),
        ),
    )
    expired_second = ReplaySchedulerTimelineStep(
        ts="2026-07-01T18:00:10Z",
        venue_states=venues,
        candidates=(
            ReplaySchedulerCandidate(
                session_id=502,
                symbol="BTWO",
                venue="robinhood",
                execution_family="robinhood_agentic_mcp",
                state="queued_live",
                quality_score=0.90,
                queued_age_seconds=60,
                expires_in_seconds=-5,
                tick_armed=True,
                expired=True,
                expected_pnl_usd=25.0,
            ),
        ),
    )

    out = replay_scheduler_timeline(
        [first, expired_second],
        default_capacity_limit=2,
        default_order_call_budget=1,
        default_risk_budget_slots=1,
    )

    assert out.selected_session_ids == [501]
    assert out.terminalized_session_ids == [502]
    assert out.selected_expected_pnl_usd == pytest.approx(40.0)
    assert out.missed_expected_pnl_usd == pytest.approx(25.0)
    assert out.skipped_expected_pnl_by_reason == {"pre_entry_terminal": 25.0}
    assert out.decision_trace[502][0]["reason"] == "order_call_budget_exhausted"
    assert out.decision_trace[502][1]["reason"] == "pre_entry_terminal"


def test_scheduler_timeline_keeps_unavailable_venue_free_and_selects_equity_same_tick() -> None:
    step = ReplaySchedulerTimelineStep(
        ts="2026-07-01T18:00:00Z",
        venue_states=(
            ReplayVenueState("coinbase", "coinbase_spot", adapter_available=False),
            ReplayVenueState("robinhood", "robinhood_agentic_mcp", adapter_available=True),
        ),
        candidates=(
            ReplaySchedulerCandidate(
                session_id=601,
                symbol="BTC-USD",
                venue="coinbase",
                execution_family="coinbase_spot",
                state="queued_live",
                quality_score=0.99,
                queued_age_seconds=120,
                expires_in_seconds=30,
                tick_armed=True,
                expected_pnl_usd=50.0,
            ),
            ReplaySchedulerCandidate(
                session_id=602,
                symbol="JEM",
                venue="robinhood",
                execution_family="robinhood_agentic_mcp",
                state="watch_break_level",
                quality_score=0.72,
                queued_age_seconds=20,
                expires_in_seconds=30,
                tick_armed=True,
                expected_pnl_usd=18.0,
            ),
        ),
    )

    out = replay_scheduler_timeline(
        [step],
        default_capacity_limit=1,
        default_order_call_budget=1,
        default_risk_budget_slots=1,
    )

    assert out.selected_session_ids == [602]
    assert out.pending_session_ids == [601]
    assert out.selected_expected_pnl_usd == pytest.approx(18.0)
    assert out.open_expected_pnl_usd == pytest.approx(50.0)
    assert out.missed_expected_pnl_usd == pytest.approx(0.0)
    assert out.decision_trace[601][0]["reason"] == "venue_adapter_unavailable"
    assert out.decision_trace[601][0]["consumes_capacity"] is False


def test_scheduler_timeline_from_live_snapshots_tracks_delayed_rows_over_time() -> None:
    venues = (
        ReplayVenueState(
            "robinhood",
            "robinhood_agentic_mcp",
            adapter_available=True,
            order_call_budget=1,
            risk_budget_slots=1,
        ),
    )
    first_rows = (
        {
            "id": 701,
            "symbol": "AONE",
            "venue": "robinhood",
            "execution_family": "robinhood_agentic_mcp",
            "state": "queued_live",
            "created_at": "2026-07-01T17:59:00Z",
            "risk_snapshot_json": {
                "viability_score": 0.98,
                "expires_at_utc": "2026-07-01T18:05:00Z",
                "momentum_live_execution": {
                    "watch_break_level": 4.2,
                    "expected_pnl_usd": 32.0,
                },
            },
        },
        {
            "id": 702,
            "symbol": "BTWO",
            "venue": "robinhood",
            "execution_family": "robinhood_agentic_mcp",
            "state": "queued_live",
            "created_at": "2026-07-01T17:59:30Z",
            "risk_snapshot_json": {
                "viability_score": 0.75,
                "expires_at_utc": "2026-07-01T18:05:00Z",
                "momentum_live_execution": {
                    "watch_break_level": 2.5,
                    "expected_pnl_usd": 14.0,
                },
            },
        },
    )
    second_rows = (
        {
            "id": 702,
            "symbol": "BTWO",
            "venue": "robinhood",
            "execution_family": "robinhood_agentic_mcp",
            "state": "watch_break_level",
            "created_at": "2026-07-01T17:59:30Z",
            "risk_snapshot_json": {
                "viability_score": 0.75,
                "expires_at_utc": "2026-07-01T18:05:00Z",
                "momentum_live_execution": {
                    "watch_break_level": 2.5,
                    "expected_pnl_usd": 14.0,
                },
            },
        },
    )

    out = replay_scheduler_timeline_from_live_snapshots(
        [
            ReplaySchedulerLiveSnapshotStep(
                ts="2026-07-01T18:00:00Z",
                rows=first_rows,
                venue_states=venues,
            ),
            ReplaySchedulerLiveSnapshotStep(
                ts="2026-07-01T18:00:10Z",
                rows=second_rows,
                venue_states=venues,
            ),
        ],
        default_capacity_limit=2,
        default_order_call_budget=1,
        default_risk_budget_slots=1,
    )

    assert out.selected_session_ids == [701, 702]
    assert out.selected_expected_pnl_usd == pytest.approx(46.0)
    assert out.missed_expected_pnl_usd == pytest.approx(0.0)
    assert out.decision_trace[702][0]["reason"] == "order_call_budget_exhausted"
    assert out.decision_trace[702][1]["reason"] == "selected"


def test_scheduler_timeline_pnl_attribution_splits_fills_rejects_and_no_fills() -> None:
    timeline = replay_scheduler_timeline(
        [
            ReplaySchedulerTimelineStep(
                ts="2026-07-01T18:00:00Z",
                venue_states=(
                    ReplayVenueState(
                        "robinhood",
                        "robinhood_agentic_mcp",
                        adapter_available=True,
                        order_call_budget=3,
                        risk_budget_slots=3,
                    ),
                ),
                candidates=(
                    ReplaySchedulerCandidate(
                        session_id=801,
                        symbol="WIN",
                        venue="robinhood",
                        execution_family="robinhood_agentic_mcp",
                        state="watch_break_level",
                        quality_score=0.95,
                        queued_age_seconds=30,
                        expires_in_seconds=60,
                        tick_armed=True,
                        expected_pnl_usd=20.0,
                    ),
                    ReplaySchedulerCandidate(
                        session_id=802,
                        symbol="REJ",
                        venue="robinhood",
                        execution_family="robinhood_agentic_mcp",
                        state="watch_break_level",
                        quality_score=0.90,
                        queued_age_seconds=25,
                        expires_in_seconds=60,
                        tick_armed=True,
                        expected_pnl_usd=15.0,
                    ),
                    ReplaySchedulerCandidate(
                        session_id=803,
                        symbol="NOFILL",
                        venue="robinhood",
                        execution_family="robinhood_agentic_mcp",
                        state="watch_break_level",
                        quality_score=0.85,
                        queued_age_seconds=20,
                        expires_in_seconds=60,
                        tick_armed=True,
                        expected_pnl_usd=8.0,
                    ),
                ),
            )
        ],
        default_capacity_limit=3,
        default_order_call_budget=3,
        default_risk_budget_slots=3,
    )

    out = attribute_scheduler_timeline_pnl(
        timeline,
        [
            ReplayBrokerOutcome(
                session_id=801,
                status="filled",
                realized_pnl_usd=18.5,
                entry_fill_price=4.10,
                exit_fill_price=4.35,
                filled_qty=74,
            ),
            ReplayBrokerOutcome(session_id=802, status="rejected", reject_reason="broker_reject"),
            ReplayBrokerOutcome(session_id=803, status="no_fill"),
        ],
    )

    assert timeline.selected_expected_pnl_by_session == {801: 20.0, 802: 15.0, 803: 8.0}
    assert out.realized_session_ids == [801]
    assert out.rejected_session_ids == [802]
    assert out.no_fill_session_ids == [803]
    assert out.realized_pnl_usd == pytest.approx(18.5)
    assert out.selected_expected_pnl_usd == pytest.approx(43.0)
    assert out.rejected_expected_pnl_usd == pytest.approx(15.0)
    assert out.no_fill_expected_pnl_usd == pytest.approx(8.0)
    assert out.realized_vs_selected_expected_usd == pytest.approx(-24.5)
    assert out.outcome_trace[801]["entry_fill_price"] == pytest.approx(4.10)


def test_live_replay_certification_pnl_minmax_requires_all_proof_legs() -> None:
    timeline = replay_scheduler_timeline(
        [
            ReplaySchedulerTimelineStep(
                ts="2026-07-01T18:00:00Z",
                venue_states=(
                    ReplayVenueState(
                        "robinhood",
                        "robinhood_agentic_mcp",
                        adapter_available=True,
                        order_call_budget=1,
                        risk_budget_slots=1,
                    ),
                ),
                candidates=(
                    ReplaySchedulerCandidate(
                        session_id=851,
                        symbol="WIN",
                        venue="robinhood",
                        execution_family="robinhood_agentic_mcp",
                        state="watch_break_level",
                        quality_score=0.95,
                        queued_age_seconds=30,
                        expires_in_seconds=60,
                        tick_armed=True,
                        expected_pnl_usd=20.0,
                    ),
                    ReplaySchedulerCandidate(
                        session_id=852,
                        symbol="DELAY",
                        venue="robinhood",
                        execution_family="robinhood_agentic_mcp",
                        state="queued_live",
                        quality_score=0.80,
                        queued_age_seconds=25,
                        expires_in_seconds=60,
                        tick_armed=True,
                        expected_pnl_usd=8.0,
                    ),
                ),
            ),
            ReplaySchedulerTimelineStep(
                ts="2026-07-01T18:00:10Z",
                venue_states=(
                    ReplayVenueState(
                        "robinhood",
                        "robinhood_agentic_mcp",
                        adapter_available=True,
                        order_call_budget=1,
                        risk_budget_slots=1,
                    ),
                ),
                candidates=(
                    ReplaySchedulerCandidate(
                        session_id=852,
                        symbol="DELAY",
                        venue="robinhood",
                        execution_family="robinhood_agentic_mcp",
                        state="watch_break_level",
                        quality_score=0.80,
                        queued_age_seconds=35,
                        expires_in_seconds=50,
                        tick_armed=True,
                        expected_pnl_usd=8.0,
                    ),
                ),
            ),
        ],
        default_capacity_limit=2,
        default_order_call_budget=1,
        default_risk_budget_slots=1,
    )
    attribution = attribute_scheduler_timeline_pnl(
        timeline,
        [
            ReplayBrokerOutcome(session_id=851, status="filled", realized_pnl_usd=18.0),
            ReplayBrokerOutcome(session_id=852, status="filled", realized_pnl_usd=7.0),
        ],
    )

    missing_labels = _certification_boundary(
        timeline=timeline,
        attribution=attribution,
        broker_outcome_count=2,
        session_row_count=2,
        opportunity_labels={
            "has_market_path_counterfactual_opportunity_labels": False,
            "complete_missed_vs_taken_outcome_labels": False,
        },
    )
    complete_labels = _certification_boundary(
        timeline=timeline,
        attribution=attribution,
        broker_outcome_count=2,
        session_row_count=2,
        opportunity_labels={
            "has_market_path_counterfactual_opportunity_labels": True,
            "complete_missed_vs_taken_outcome_labels": True,
        },
    )

    assert missing_labels["pnl_minmax_claim_ready"] is False
    assert "market_path_counterfactual_opportunity_labels" in missing_labels["missing_evidence"]
    assert "complete_missed_vs_taken_outcome_labels" in missing_labels["missing_evidence"]
    assert complete_labels["pnl_minmax_claim_ready"] is True
    assert complete_labels["pnl_minmax_blocker"] == ""
    assert "complete_selected_broker_outcomes" not in complete_labels["missing_evidence"]


def test_replay_broker_outcomes_from_rows_normalizes_event_and_snapshot_shapes() -> None:
    rows = [
        {
            "session_id": 901,
            "event_type": "live_exit_filled",
            "payload_json": {
                "realized_pnl_usd": 12.25,
                "entry_price": 2.0,
                "exit_price": 2.25,
                "qty": 49,
            },
        },
        {
            "session_id": 902,
            "status": "rejected",
            "payload_json": {"reason": "venue_adapter_unavailable"},
        },
        {
            "id": 903,
            "risk_snapshot_json": {
                "momentum_live_execution": {
                    "realized_pnl_usd": -3.5,
                    "avg_entry_price": 5.0,
                    "last_exit_price": 4.95,
                }
            },
            "status": "filled",
        },
    ]

    outcomes = replay_broker_outcomes_from_rows(rows)

    assert [o.session_id for o in outcomes] == [901, 902, 903]
    assert outcomes[0].status == "filled"
    assert outcomes[0].realized_pnl_usd == pytest.approx(12.25)
    assert outcomes[1].status == "rejected"
    assert outcomes[1].reject_reason == "venue_adapter_unavailable"
    assert outcomes[2].realized_pnl_usd == pytest.approx(-3.5)


def test_replay_broker_outcomes_from_rows_preserves_zero_values() -> None:
    rows = [
        {
            "session_id": 904,
            "status": "filled",
            "realized_pnl_usd": 0.0,
            "entry_fill_price": 0.0,
            "exit_fill_price": 0.0,
            "filled_qty": 0.0,
            "payload_json": {
                "realized_pnl_usd": 9.99,
                "entry_price": 1.25,
                "exit_price": 1.30,
                "qty": 10,
            },
        }
    ]

    outcomes = replay_broker_outcomes_from_rows(rows)

    assert outcomes[0].realized_pnl_usd == pytest.approx(0.0)
    assert outcomes[0].entry_fill_price == pytest.approx(0.0)
    assert outcomes[0].exit_fill_price == pytest.approx(0.0)
    assert outcomes[0].filled_qty == pytest.approx(0.0)
