from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models.trading import MomentumStrategyVariant, TradingAutomationEvent
from app.services.trading.execution_family_registry import (
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
)
from app.services.trading.momentum_neural.live_replay_export import (
    build_opportunity_label_rows,
    build_setup_attribution_rows,
    export_live_replay_inputs,
    infer_replay_venue_states,
)
from app.services.trading.momentum_neural.live_replay_audit import (
    _scheduler_starvation_evidence,
    run_live_replay_audit,
)
from app.services.trading.momentum_neural.live_runner_loop import LiveRunnerLoop
from app.services.trading.momentum_neural.persistence import (
    append_trading_automation_event,
    create_trading_automation_session,
)
from app.services.trading.momentum_neural.replay_v3 import (
    attribute_scheduler_timeline_pnl,
    replay_scheduler_timeline_from_live_snapshots,
)
from app.services.trading.momentum_neural.live_fsm import STATE_QUEUED_LIVE
from tests.test_momentum_live_runner import _uid, _variant_id_for_live_test


def _minimal_variant_id(db: Session, key: str = "replay_event_snapshot") -> int:
    row = MomentumStrategyVariant(
        family=key,
        variant_key=key,
        version=1,
        label=key,
        params_json={},
        is_active=True,
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        refinement_meta_json={},
    )
    db.add(row)
    db.flush()
    return int(row.id)


def test_live_replay_export_shapes_sessions_and_outcomes_for_replay(db: Session) -> None:
    uid = _uid(db, "live_replay_export")
    vid = _variant_id_for_live_test(db, symbol="JEM")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={
            "viability_score": 0.91,
            "momentum_live_execution": {
                "watch_break_level": 3.95,
                "expected_pnl_usd": 18.0,
            },
        },
        variant_id=vid,
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_exit_filled",
        {
            "realized_pnl_usd": 7.25,
            "entry_price": 3.95,
            "exit_price": 4.10,
            "qty": 48,
        },
    )
    db.commit()

    before_dirty = set(db.dirty)
    export = export_live_replay_inputs(db, session_ids=[int(sess.id)])
    after_dirty = set(db.dirty)

    assert before_dirty == after_dirty == set()
    assert len(export.session_rows) == 1
    assert export.session_rows[0]["symbol"] == "JEM"
    assert export.session_rows[0]["execution_family"] == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP
    assert len(export.outcome_rows) == 1
    assert len(export.snapshot_steps) == 1
    assert export.broker_outcomes[0].status == "filled"
    assert export.broker_outcomes[0].realized_pnl_usd == pytest.approx(7.25)

    timeline = replay_scheduler_timeline_from_live_snapshots(
        (export.snapshot_step,),
        default_capacity_limit=1,
    )
    attribution = attribute_scheduler_timeline_pnl(timeline, export.broker_outcomes)
    assert timeline.selected_session_ids == [int(sess.id)]
    assert attribution.realized_session_ids == [int(sess.id)]
    assert attribution.realized_pnl_usd == pytest.approx(7.25)


def test_live_replay_export_uses_explicit_scheduler_snapshot_events_for_timeline(db: Session) -> None:
    uid = _uid(db, "live_replay_explicit_timeline")
    vid = _variant_id_for_live_test(db, symbol="FAST")
    fast = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="FAST",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={"viability_score": 0.95},
        variant_id=vid,
    )
    slow = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="SLOW",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={"viability_score": 0.70},
        variant_id=vid,
    )
    rows_t0 = [
        {
            "id": int(fast.id),
            "session_id": int(fast.id),
            "symbol": "FAST",
            "venue": "robinhood",
            "execution_family": EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
            "state": STATE_QUEUED_LIVE,
            "created_at": "2026-07-01T18:00:00Z",
            "risk_snapshot_json": {
                "viability_score": 0.95,
                "momentum_live_execution": {"watch_break_level": 4.2, "expected_pnl_usd": 30.0},
            },
        },
        {
            "id": int(slow.id),
            "session_id": int(slow.id),
            "symbol": "SLOW",
            "venue": "robinhood",
            "execution_family": EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
            "state": STATE_QUEUED_LIVE,
            "created_at": "2026-07-01T18:00:05Z",
            "risk_snapshot_json": {
                "viability_score": 0.70,
                "momentum_live_execution": {"watch_break_level": 2.5, "expected_pnl_usd": 12.0},
            },
        },
    ]
    rows_t1 = [dict(rows_t0[1], state="watch_break_level")]
    venues = [
        {
            "venue": "robinhood",
            "execution_family": EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
            "adapter_available": True,
            "order_call_budget": 1,
            "risk_budget_slots": 1,
        }
    ]
    ev0 = append_trading_automation_event(
        db,
        int(fast.id),
        "live_replay_scheduler_snapshot",
        {"ts": "2026-07-01T18:01:00Z", "rows": rows_t0, "venue_states": venues},
    )
    ev1 = append_trading_automation_event(
        db,
        int(fast.id),
        "live_replay_scheduler_snapshot",
        {"ts": "2026-07-01T18:01:10Z", "rows": rows_t1, "venue_states": venues},
    )
    ev0.ts = datetime(2026, 7, 1, 18, 1, 0)
    ev1.ts = datetime(2026, 7, 1, 18, 1, 10)
    db.commit()

    export = export_live_replay_inputs(db, session_ids=[int(fast.id), int(slow.id)])
    assert len(export.snapshot_steps) == 2

    summary = run_live_replay_audit(
        db,
        session_ids=[int(fast.id), int(slow.id)],
        capacity_limit=2,
        order_call_budget=1,
        risk_budget_slots=1,
    )

    assert summary["inputs"]["scheduler_snapshot_steps"] == 2
    assert summary["certification"]["input_shape"] == "multi_snapshot_timeline"
    assert summary["scheduler"]["selected_session_ids"] == [int(fast.id), int(slow.id)]
    assert summary["scheduler"]["selected_expected_pnl_usd"] == pytest.approx(42.0)
    assert summary["scheduler"]["missed_expected_pnl_usd"] == pytest.approx(0.0)
    assert summary["scheduler"]["priority_evidence"]["delayed_then_selected_count"] == 1
    assert summary["certification"]["scheduler_priority_claim_ready"] is True
    assert summary["certification"]["evidence_status"]["has_multi_snapshot_timeline"] is True
    assert summary["certification"]["evidence_status"]["has_scheduler_pressure_or_delay_evidence"] is True
    assert "multi_snapshot_scheduler_timeline" not in summary["certification"]["missing_evidence"]
    assert "scheduler_pressure_or_delayed_selection_evidence" not in summary["certification"]["missing_evidence"]
    assert "complete_selected_broker_outcomes" in summary["certification"]["missing_evidence"]


def test_live_replay_export_uses_event_loop_snapshot_events_for_timeline(db: Session) -> None:
    uid = _uid(db, "live_replay_event_timeline")
    vid = _minimal_variant_id(db, "event_loop_snapshot_timeline")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="EVNT",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={
            "viability_score": 0.88,
            "momentum_live_execution": {"watch_break_level": 5.25, "expected_pnl_usd": 9.0},
        },
        variant_id=vid,
    )
    rows = [
        {
            "id": int(sess.id),
            "session_id": int(sess.id),
            "symbol": "EVNT",
            "venue": "robinhood",
            "execution_family": EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
            "state": STATE_QUEUED_LIVE,
            "created_at": "2026-07-01T18:00:00Z",
            "risk_snapshot_json": {
                "viability_score": 0.88,
                "momentum_live_execution": {"watch_break_level": 5.25, "expected_pnl_usd": 9.0},
            },
        }
    ]
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_replay_event_snapshot",
        {
            "ts": "2026-07-01T18:02:00Z",
            "rows": rows,
            "venue_states": [
                {
                    "venue": "robinhood",
                    "execution_family": EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
                    "adapter_available": True,
                    "order_call_budget": 1,
                    "risk_budget_slots": 1,
                }
            ],
            "source": "live_runner_event_loop",
            "event_cause": "iqfeed_notify",
        },
    )
    db.commit()

    export = export_live_replay_inputs(db, session_ids=[int(sess.id)])

    assert len(export.snapshot_steps) == 1
    assert export.snapshot_steps[0].ts == "2026-07-01T18:02:00+00:00"
    assert export.snapshot_steps[0].rows[0]["symbol"] == "EVNT"


def test_live_runner_loop_emits_event_replay_snapshot_best_effort(db: Session, monkeypatch) -> None:
    uid = _uid(db, "live_loop_event_snapshot")
    vid = _minimal_variant_id(db, "live_loop_event_snapshot")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="LOOP",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={
            "viability_score": 0.92,
            "momentum_live_execution": {"watch_break_level": 4.4, "expected_pnl_usd": 13.0},
        },
        variant_id=vid,
    )
    db.commit()
    loop = LiveRunnerLoop()
    monkeypatch.setattr(loop._tracker, "get_all_session_ids", lambda: [int(sess.id)])

    try:
        loop._emit_event_replay_snapshot(
            db,
            int(sess.id),
            cause="iqfeed_notify",
            result={"state": STATE_QUEUED_LIVE},
        )
    finally:
        loop._executor.shutdown(wait=False, cancel_futures=True)

    events = db.query(TradingAutomationEvent).filter_by(
        session_id=int(sess.id),
        event_type="live_replay_event_snapshot",
    ).all()
    assert len(events) == 1
    payload = events[0].payload_json
    assert payload["source"] == "live_runner_event_loop"
    assert payload["event_cause"] == "iqfeed_notify"
    assert payload["rows"][0]["session_id"] == int(sess.id)
    assert payload["selected_session_ids"] == [int(sess.id)]


def test_live_replay_export_preserves_ask_eaten_print_attribution(db: Session) -> None:
    uid = _uid(db, "live_replay_ask_eaten")
    vid = _variant_id_for_live_test(db, symbol="JEM")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={
            "viability_score": 0.94,
            "momentum_live_execution": {
                "expected_pnl_usd": 24.0,
                "entry_trigger_reason": "absorption_snap_tick",
                "entry_trigger_debug": {
                    "ask_eaten_confirmed": True,
                    "ask_eaten_frac": 0.80,
                    "ask_eaten_pctile": 0.3333,
                    "ask_lift_print_confirmed": True,
                    "ask_lift_volume": 10_000,
                    "target_print_volume": 10_000,
                    "ask_lift_ratio": 0.9091,
                    "target_print_ratio": 1.0,
                    "n_target_prints": 3,
                },
            },
        },
        variant_id=vid,
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_entry_candidate",
        {
            "trigger_reason": "absorption_snap_tick",
            "entry_trigger_debug": {
                "ask_eaten_confirmed": True,
                "ask_lift_print_confirmed": True,
                "ask_lift_volume": 10_000,
                "target_print_volume": 10_000,
                "n_target_prints": 3,
            },
        },
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_exit_filled",
        {"realized_pnl_usd": 6.5, "entry_price": 10.70, "exit_price": 10.90, "qty": 33},
    )
    db.commit()

    export = export_live_replay_inputs(db, session_ids=[int(sess.id)])

    assert len(export.setup_attribution_rows) == 1
    row = export.setup_attribution_rows[0]
    assert row["session_id"] == int(sess.id)
    assert row["trigger_reason"] == "absorption_snap_tick"
    assert row["bucket"] == "ask_eaten_with_lifted_prints"
    assert row["ask_eaten_frac"] == pytest.approx(0.80)
    assert row["ask_lift_volume"] == pytest.approx(10_000)
    assert row["target_print_volume"] == pytest.approx(10_000)

    summary = run_live_replay_audit(db, session_ids=[int(sess.id)], capacity_limit=1)
    assert summary["inputs"]["setup_attribution_rows"] == 1
    assert summary["setup_attribution"]["bucket_counts"] == {"ask_eaten_with_lifted_prints": 1}
    assert summary["setup_attribution"]["ask_lift_volume"] == pytest.approx(10_000)
    assert summary["setup_attribution"]["target_print_volume"] == pytest.approx(10_000)
    assert summary["pnl_attribution"]["realized_pnl_usd"] == pytest.approx(6.5)


def test_setup_attribution_buckets_quote_only_absorption() -> None:
    rows = build_setup_attribution_rows(
        (
            {
                "session_id": 42,
                "symbol": "JEM",
                "execution_family": EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
                "risk_snapshot_json": {
                    "momentum_live_execution": {
                        "entry_trigger_reason": "absorption_snap_tick",
                        "entry_trigger_debug": {
                            "ask_eaten_confirmed": True,
                            "ask_eaten_frac": 0.50,
                            "ask_lift_print_confirmed": False,
                        },
                    }
                },
            },
        ),
        (),
    )

    assert len(rows) == 1
    assert rows[0]["bucket"] == "ask_eaten_quote_only"
    assert rows[0]["ask_eaten_frac"] == pytest.approx(0.50)


def test_live_replay_export_infers_unavailable_venue_from_window_events(db: Session) -> None:
    uid = _uid(db, "live_replay_export_unavailable")
    vid = _variant_id_for_live_test(db, symbol="FIDA-USD")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="FIDA-USD",
        venue="coinbase",
        execution_family=EXECUTION_FAMILY_COINBASE_SPOT,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={
            "viability_score": 0.88,
            "momentum_live_execution": {"expected_pnl_usd": 11.0},
        },
        variant_id=vid,
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_entry_rejected",
        {"reason": "venue_adapter_unavailable"},
    )
    db.commit()

    export = export_live_replay_inputs(db, session_ids=[int(sess.id)])

    assert len(export.venue_states) == 1
    assert export.venue_states[0].execution_family == EXECUTION_FAMILY_COINBASE_SPOT
    assert export.venue_states[0].adapter_available is False


def test_live_replay_export_limit_prefers_recent_sessions(db: Session) -> None:
    uid = _uid(db, "live_replay_export_recent")
    vid = _variant_id_for_live_test(db, symbol="NEW")
    old = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="OLD",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={"viability_score": 0.20},
        variant_id=vid,
    )
    new = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="NEW",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={"viability_score": 0.90},
        variant_id=vid,
    )
    base = datetime(2026, 7, 1, 12, 0, 0)
    old.updated_at = base
    new.updated_at = base + timedelta(seconds=5)
    db.commit()

    export = export_live_replay_inputs(db, session_ids=[int(old.id), int(new.id)], limit=1)

    assert len(export.session_rows) == 1
    assert export.session_rows[0]["session_id"] == int(new.id)
    assert export.session_rows[0]["symbol"] == "NEW"


def test_infer_replay_venue_states_keeps_available_without_explicit_unavailable_reason() -> None:
    rows = (
        {"session_id": 1, "execution_family": EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP},
    )
    outcomes = (
        {
            "session_id": 1,
            "execution_family": EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
            "status": "filled",
            "payload_json": {"reason": "target"},
        },
    )

    states = infer_replay_venue_states(rows, outcomes)

    assert len(states) == 1
    assert states[0].adapter_available is True


def test_live_replay_audit_returns_read_only_certification_summary(db: Session) -> None:
    uid = _uid(db, "live_replay_audit")
    vid = _variant_id_for_live_test(db, symbol="AUDT")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="AUDT",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={
            "viability_score": 0.93,
            "momentum_live_execution": {
                "watch_break_level": 4.20,
                "expected_pnl_usd": 21.0,
            },
        },
        variant_id=vid,
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_entry_candidate",
        {
            "setup_trace": {
                "setup_alias": "abcd_break_tick_ok",
                "source_wait_reason": "",
                "structural_stop_covered": True,
                "a_setup_floor_covered": True,
                "structural_stop_price": 4.01,
            }
        },
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_entry_filled",
        {"price": 4.20, "qty": 33},
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_trailing_armed",
        {"bid": 4.31},
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_pullback_add_fired",
        {"add_qty": 8, "limit_price": 4.36},
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_pullback_add_fill",
        {"add_qty": 8, "add_price": 4.36},
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_exit_filled",
        {
            "realized_pnl_usd": 5.0,
            "entry_price": 4.20,
            "exit_price": 4.35,
            "qty": 33,
        },
    )
    other = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="NOISE",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={"momentum_live_execution": {"expected_pnl_usd": 7.0}},
        variant_id=vid,
    )
    append_trading_automation_event(
        db,
        int(other.id),
        "live_entry_filled",
        {"price": 2.10, "qty": 50},
    )
    append_trading_automation_event(
        db,
        int(other.id),
        "live_pullback_add_fill",
        {"add_qty": 10, "add_price": 2.25},
    )
    db.commit()

    before_dirty = set(db.dirty)
    summary = run_live_replay_audit(
        db,
        session_ids=[int(sess.id)],
        capacity_limit=1,
        setup_trace_limit=10,
    )
    after_dirty = set(db.dirty)

    assert before_dirty == after_dirty == set()
    assert summary["read_only"] is True
    assert summary["inputs"]["session_rows"] == 1
    assert summary["inputs"]["session_state_counts"] == {STATE_QUEUED_LIVE: 1}
    assert summary["inputs"]["execution_family_counts"] == {EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP: 1}
    assert summary["scheduler"]["selected_count"] == 1
    assert summary["scheduler"]["no_selected_reason"] is None
    assert summary["scheduler"]["selected_session_ids"] == [int(sess.id)]
    assert summary["certification"]["input_shape"] == "single_snapshot_batch"
    assert summary["certification"]["scheduler_priority_claim_ready"] is False
    assert summary["certification"]["broker_outcome_attribution_ready"] is True
    assert summary["certification"]["realized_vs_selected_expected_claim_ready"] is True
    assert summary["certification"]["pnl_minmax_claim_ready"] is False
    assert (
        summary["certification"]["pnl_minmax_blocker"]
        == "live_export_has_no_historical_intra_session_snapshot_timeline"
    )
    assert summary["certification"]["evidence_status"] == {
        "has_live_session_rows": True,
        "has_multi_snapshot_timeline": False,
        "has_scheduler_pressure_or_delay_evidence": False,
        "has_adapter_unavailable_same_step_selection_evidence": False,
        "has_selected_sessions": True,
        "has_broker_outcomes": True,
        "has_complete_selected_outcomes": True,
        "has_market_path_counterfactual_opportunity_labels": False,
        "has_complete_missed_vs_taken_outcome_labels": False,
    }
    assert "multi_snapshot_scheduler_timeline" in summary["certification"]["missing_evidence"]
    assert "scheduler_pressure_or_delayed_selection_evidence" in summary["certification"]["missing_evidence"]
    assert "adapter_unavailable_same_step_selection_evidence" in summary["certification"]["missing_evidence"]
    assert "market_path_counterfactual_opportunity_labels" in summary["certification"]["missing_evidence"]
    assert "complete_missed_vs_taken_outcome_labels" in summary["certification"]["missing_evidence"]
    assert "complete_selected_broker_outcomes" not in summary["certification"]["missing_evidence"]
    debt_by_key = {row["missing_evidence"]: row for row in summary["replay_evidence_debt"]}
    assert debt_by_key["multi_snapshot_scheduler_timeline"]["claim_gate"] is True
    assert debt_by_key["multi_snapshot_scheduler_timeline"]["enablement_gate"] is False
    assert "event-loop ticks" in debt_by_key["multi_snapshot_scheduler_timeline"]["how_to_collect"]
    assert debt_by_key["market_path_counterfactual_opportunity_labels"]["blocks"] == ["pnl_minmax"]
    assert "will not infer opportunity labels from fills alone" in debt_by_key[
        "market_path_counterfactual_opportunity_labels"
    ]["how_to_collect"]
    assert summary["pnl_attribution"]["realized_pnl_usd"] == pytest.approx(5.0)
    assert summary["pnl_evidence"]["complete_selected_outcomes"] is True
    assert summary["pnl_evidence"]["realized_vs_selected_expected_claim_ready"] is True
    assert summary["pnl_evidence"]["pnl_minmax_claim_ready"] is False
    assert "market_path_counterfactual_opportunity_labels" in summary["pnl_evidence"]["pnl_minmax_missing_evidence"]
    assert summary["inputs"]["opportunity_label_rows"] == 0
    assert summary["opportunity_label_evidence"]["row_count"] == 0
    assert summary["opportunity_label_evidence"]["complete_missed_vs_taken_outcome_labels"] is False
    assert summary["setup_trace"]["ok"] is True
    assert summary["setup_trace"]["finding_count"] == 0
    assert summary["setup_trace"]["certification"]["trace_coverage_ok"] is True
    assert summary["setup_trace"]["certification"]["lifecycle_order_ok"] is True
    assert summary["setup_trace"]["certification"]["lifecycle_claim_ready"] is True
    assert summary["setup_trace"]["lifecycle_summary"]["sessions_with_entry_and_add"] == 1
    assert summary["setup_trace"]["lifecycle_summary"]["sessions_with_entry_and_exit"] == 1
    assert summary["setup_trace"]["lifecycle_summary"]["stage_counts"]["trailing_armed"] == 1
    assert summary["setup_trace"]["lifecycle_summary"]["stage_counts"]["entry_fill"] == 1
    assert summary["setup_trace"]["lifecycle_summary"]["stage_counts"]["add_fill"] == 1
    assert "does not execute broker calls" in summary["boundary"]
    assert "certification.pnl_minmax_claim_ready" in summary["boundary"]


def test_live_replay_audit_empty_session_scope_does_not_audit_global_recent_events(db: Session) -> None:
    uid = _uid(db, "live_replay_audit_empty_scope")
    vid = _variant_id_for_live_test(db, symbol="GLBL")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="GLBL",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={},
        variant_id=vid,
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_entry_filled",
        {"price": 3.0, "qty": 10},
    )
    db.commit()

    summary = run_live_replay_audit(db, session_ids=[], setup_trace_limit=10)

    assert summary["inputs"]["session_rows"] == 0
    assert summary["setup_trace"]["events_seen"] == 0
    assert summary["setup_trace"]["lifecycle_summary"]["stage_counts"] == {}
    assert summary["arm_lifecycle"]["session_count"] == 0


def test_live_replay_audit_surfaces_arm_requested_without_confirm_as_lifecycle_gap(
    db: Session,
) -> None:
    uid = _uid(db, "live_replay_arm_lifecycle_gap")
    vid = _variant_id_for_live_test(db, symbol="JEM")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state="expired",
        risk_snapshot_json={"viability_score": 0.80},
        variant_id=vid,
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_arm_requested",
        {"symbol": "JEM", "variant_id": vid},
    )
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_arm_expired",
        {"reason": "expires_at_utc_passed"},
    )
    db.commit()

    summary = run_live_replay_audit(db, session_ids=[int(sess.id)], setup_trace_limit=10)

    arm = summary["arm_lifecycle"]
    assert arm["arm_requested_count"] == 1
    assert arm["arm_confirmed_count"] == 0
    assert arm["runner_started_count"] == 0
    assert arm["arm_expired_count"] == 1
    assert arm["requested_without_confirm_count"] == 1
    assert arm["expired_without_runner_count"] == 1
    assert arm["requested_without_confirm_session_ids"] == [int(sess.id)]
    assert arm["samples"][0]["reason"] == "arm_requested_without_confirm"
    assert "not entry setup refusals" in arm["claim_model"]


def test_live_replay_audit_certifies_adapter_unavailable_starvation_evidence(db: Session) -> None:
    uid = _uid(db, "live_replay_starvation_evidence")
    cb_vid = _variant_id_for_live_test(db, symbol="FIDA-USD")
    rh_vid = _variant_id_for_live_test(db, symbol="JEM")
    crypto = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="FIDA-USD",
        venue="coinbase",
        execution_family=EXECUTION_FAMILY_COINBASE_SPOT,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={
            "viability_score": 0.99,
            "momentum_live_execution": {"expected_pnl_usd": 50.0},
        },
        variant_id=cb_vid,
    )
    equity = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={
            "viability_score": 0.70,
            "momentum_live_execution": {"expected_pnl_usd": 18.0},
        },
        variant_id=rh_vid,
    )
    append_trading_automation_event(
        db,
        int(crypto.id),
        "live_entry_rejected",
        {"reason": "venue_adapter_unavailable"},
    )
    db.commit()

    summary = run_live_replay_audit(
        db,
        session_ids=[int(crypto.id), int(equity.id)],
        capacity_limit=1,
        order_call_budget=1,
        risk_budget_slots=1,
    )

    evidence = summary["scheduler"]["starvation_evidence"]
    assert summary["scheduler"]["selected_session_ids"] == [int(equity.id)]
    assert evidence["unavailable_free_skip_count"] == 1
    assert evidence["decision_reason_counts"]["venue_adapter_unavailable"] == 1
    assert evidence["free_skip_reason_counts"]["venue_adapter_unavailable"] == 1
    assert "venue_adapter_unavailable" not in evidence["capacity_consuming_reason_counts"]
    assert evidence["steps_with_unavailable_free_skip_and_selection"] == 1
    assert evidence["adapter_unavailable_starvation_claim_ready"] is True
    assert summary["certification"]["adapter_unavailable_starvation_claim_ready"] is True


def test_live_replay_audit_certifies_pnl_minmax_only_with_complete_opportunity_labels(db: Session) -> None:
    uid = _uid(db, "live_replay_pnl_minmax_labels")
    vid = _minimal_variant_id(db, "live_replay_pnl_minmax_labels")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={
            "viability_score": 0.95,
            "momentum_live_execution": {"expected_pnl_usd": 12.0},
        },
        variant_id=vid,
    )
    rows = [
        {
            "id": int(sess.id),
            "session_id": int(sess.id),
            "symbol": "JEM",
            "venue": "robinhood",
            "execution_family": EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
            "state": STATE_QUEUED_LIVE,
            "created_at": "2026-07-01T18:00:00Z",
            "risk_snapshot_json": {
                "viability_score": 0.95,
                "momentum_live_execution": {"expected_pnl_usd": 12.0},
            },
        }
    ]
    venues = [
        {
            "venue": "robinhood",
            "execution_family": EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
            "adapter_available": True,
            "order_call_budget": 1,
            "risk_budget_slots": 1,
        }
    ]
    ev0 = append_trading_automation_event(
        db,
        int(sess.id),
        "live_replay_event_snapshot",
        {
            "ts": "2026-07-01T18:01:00Z",
            "rows": rows,
            "venue_states": venues,
            "opportunity_label_summary": {
                "pnl_minmax_label_ready": True,
                "rows": [
                    {
                        "symbol": "JEM",
                        "session_id": int(sess.id),
                        "status": "labeled_taken",
                        "label_ready": True,
                        "opportunity_ts": "2026-07-01T18:00:05Z",
                        "pnl_usd": 5.5,
                    }
                ],
            },
        },
    )
    ev1 = append_trading_automation_event(
        db,
        int(sess.id),
        "live_replay_event_snapshot",
        {"ts": "2026-07-01T18:01:05Z", "rows": rows, "venue_states": venues},
    )
    ev0.ts = datetime(2026, 7, 1, 18, 1, 0)
    ev1.ts = datetime(2026, 7, 1, 18, 1, 5)
    append_trading_automation_event(
        db,
        int(sess.id),
        "live_exit_filled",
        {"realized_pnl_usd": 5.5, "entry_price": 4.0, "exit_price": 4.2, "qty": 27.5},
    )
    db.commit()

    summary = run_live_replay_audit(db, session_ids=[int(sess.id)], capacity_limit=1)

    assert summary["inputs"]["scheduler_snapshot_steps"] == 2
    assert summary["inputs"]["opportunity_label_rows"] == 1
    assert summary["opportunity_label_evidence"]["complete_missed_vs_taken_outcome_labels"] is True
    assert summary["opportunity_label_evidence"]["pnl_usd_labeled"] == pytest.approx(5.5)
    assert summary["certification"]["evidence_status"]["has_market_path_counterfactual_opportunity_labels"] is True
    assert summary["certification"]["evidence_status"]["has_complete_missed_vs_taken_outcome_labels"] is True
    assert "market_path_counterfactual_opportunity_labels" not in summary["certification"]["missing_evidence"]
    assert "complete_missed_vs_taken_outcome_labels" not in summary["certification"]["missing_evidence"]
    assert summary["certification"]["pnl_minmax_claim_ready"] is True
    assert summary["pnl_evidence"]["pnl_minmax_claim_ready"] is True


def test_opportunity_labels_require_explicit_ready_rows() -> None:
    rows = build_opportunity_label_rows(
        (
            {
                "session_id": 42,
                "symbol": "JEM",
                "risk_snapshot_json": {
                    "momentum_live_execution": {
                        "counterfactual_opportunity_label": {
                            "status": "source_not_certified",
                            "label_ready": False,
                        }
                    }
                },
            },
        ),
        (),
    )

    assert len(rows) == 1
    assert rows[0]["status"] == "source_not_certified"
    assert rows[0]["label_ready"] is False


def test_live_replay_audit_reports_pre_entry_terminal_free_skip_reason(db: Session) -> None:
    uid = _uid(db, "live_replay_terminal_reason_counts")
    vid = _variant_id_for_live_test(db, symbol="JEM")
    terminal = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="OLD",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state="live_cancelled",
        risk_snapshot_json={"viability_score": 0.99, "terminalizable": True},
        variant_id=vid,
    )
    active = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        venue="robinhood",
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={"viability_score": 0.70},
        variant_id=vid,
    )
    db.commit()

    summary = run_live_replay_audit(
        db,
        session_ids=[int(terminal.id), int(active.id)],
        capacity_limit=1,
        order_call_budget=1,
        risk_budget_slots=1,
    )

    evidence = summary["scheduler"]["starvation_evidence"]
    assert summary["scheduler"]["selected_session_ids"] == [int(active.id)]
    assert evidence["free_skip_reason_counts"]["pre_entry_terminal"] == 1
    assert evidence["decision_reason_counts"]["pre_entry_terminal"] == 1
    assert "pre_entry_terminal" not in evidence["capacity_consuming_reason_counts"]


def test_live_replay_audit_counts_venue_asset_class_mismatch_as_wrong_venue_free_skip() -> None:
    timeline = SimpleNamespace(
        steps=[
            SimpleNamespace(
                batch=SimpleNamespace(
                    decisions=[
                        SimpleNamespace(reason="venue_asset_class_mismatch", consumes_capacity=False),
                        SimpleNamespace(reason="selected", consumes_capacity=True),
                    ]
                )
            )
        ]
    )

    evidence = _scheduler_starvation_evidence(timeline)

    assert evidence["free_skip_reason_counts"]["venue_asset_class_mismatch"] == 1
    assert evidence["decision_reason_counts"]["venue_asset_class_mismatch"] == 1
    assert "venue_asset_class_mismatch" not in evidence["capacity_consuming_reason_counts"]
    assert evidence["unavailable_free_skip_count"] == 1
    assert evidence["steps_with_unavailable_free_skip_and_selection"] == 1
    assert evidence["adapter_unavailable_starvation_claim_ready"] is True
