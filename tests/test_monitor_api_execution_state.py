from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import models
from app.models.trading import (
    AutoTraderRun,
    BreakoutAlert,
    PaperTrade,
    PatternMonitorDecision,
    ScanPattern,
    Trade,
    TradingPosition,
)
from app.routers.trading_sub.monitor import _imminent_blocker_category, _serialize_decision


def test_serialize_decision_normalizes_legacy_llm_unavailable_reason():
    decision = PatternMonitorDecision(
        trade_id=1,
        health_score=0.61,
        action="hold",
        decision_source="llm",
        llm_confidence=0.0,
        llm_reasoning="LLM unavailable",
        created_at=datetime.utcnow(),
    )

    payload = _serialize_decision(decision)

    assert payload["decision_source"] == "llm_unavailable"
    assert payload["llm_unavailable"] is True
    assert payload["llm_reasoning"] is None


def test_imminent_blocker_category_derivation():
    promoted = SimpleNamespace(lifecycle_stage="promoted", recert_required=False)
    recert = SimpleNamespace(lifecycle_stage="promoted", recert_required=True)

    def run(reason: str, expected_net_pct: float, decision: str = "skipped"):
        return SimpleNamespace(
            decision=decision,
            reason=reason,
            rule_snapshot={"entry_edge": {"expected_net_pct": expected_net_pct}},
        )

    assert (
        _imminent_blocker_category(
            run("non_positive_expected_edge", -0.1),
            promoted,
            None,
        )
        == "negative_expected_edge"
    )
    assert (
        _imminent_blocker_category(
            run("selector:shadow_observation_signal_lane", 1.2),
            promoted,
            "shadow_near_miss",
        )
        == "positive_edge_shadow_only"
    )
    assert (
        _imminent_blocker_category(
            run("selector:shadow_observation_signal_lane", 1.2),
            recert,
            "hard_recert_shadow",
        )
        == "positive_edge_recert_debt"
    )
    assert (
        _imminent_blocker_category(run("missed_entry_slippage", 0.8), promoted, None)
        == "missed_entry_slippage"
    )
    assert (
        _imminent_blocker_category(run("broker:quantity_precision", 0.8), promoted, None)
        == "broker_execution_reject"
    )
    assert _imminent_blocker_category(None, promoted, None) == "live_eligible_candidate"


def test_imminent_alerts_exposes_edge_supply_diagnostics(db, paired_client):
    client, user = paired_client

    recert_pat = ScanPattern(
        name="diag_recert",
        rules_json={},
        origin="brain",
        asset_class="stock",
        timeframe="1d",
        active=True,
        lifecycle_stage="promoted",
        recert_required=True,
        recert_reason="negative_oos_recert",
    )
    negative_pat = ScanPattern(
        name="diag_negative",
        rules_json={},
        origin="brain",
        asset_class="stock",
        timeframe="1d",
        active=True,
        lifecycle_stage="promoted",
    )
    slippage_pat = ScanPattern(
        name="diag_slippage",
        rules_json={},
        origin="brain",
        asset_class="stock",
        timeframe="1d",
        active=True,
        lifecycle_stage="promoted",
    )
    error_pat = ScanPattern(
        name="diag_error",
        rules_json={},
        origin="brain",
        asset_class="crypto",
        timeframe="1d",
        active=True,
        lifecycle_stage="promoted",
    )
    db.add_all([recert_pat, negative_pat, slippage_pat, error_pat])
    db.flush()

    recert_alert = BreakoutAlert(
        user_id=user.id,
        scan_pattern_id=recert_pat.id,
        ticker="RECRT",
        asset_type="stock",
        alert_tier="pattern_imminent",
        outcome="pending",
        score_at_alert=81.0,
        price_at_alert=20.0,
        entry_price=20.0,
        stop_loss=18.0,
        target_price=24.0,
        alerted_at=datetime.utcnow(),
        indicator_snapshot={
            "imminent_scorecard": {"signal_lane": "hard_recert_shadow"}
        },
    )
    negative_alert = BreakoutAlert(
        user_id=user.id,
        scan_pattern_id=negative_pat.id,
        ticker="NEGEV",
        asset_type="stock",
        alert_tier="pattern_imminent",
        outcome="pending",
        score_at_alert=72.0,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        alerted_at=datetime.utcnow(),
        indicator_snapshot={},
    )
    slippage_alert = BreakoutAlert(
        user_id=user.id,
        scan_pattern_id=slippage_pat.id,
        ticker="SLIP",
        asset_type="stock",
        alert_tier="pattern_imminent",
        outcome="pending",
        score_at_alert=77.0,
        price_at_alert=30.0,
        entry_price=30.0,
        stop_loss=27.0,
        target_price=36.0,
        alerted_at=datetime.utcnow(),
        indicator_snapshot={},
    )
    error_alert = BreakoutAlert(
        user_id=user.id,
        scan_pattern_id=error_pat.id,
        ticker="ERRQ",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        outcome="pending",
        score_at_alert=79.0,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        alerted_at=datetime.utcnow(),
        indicator_snapshot={},
    )
    db.add_all([recert_alert, negative_alert, slippage_alert, error_alert])
    db.flush()
    db.add_all([
        AutoTraderRun(
            user_id=user.id,
            breakout_alert_id=recert_alert.id,
            scan_pattern_id=recert_pat.id,
            ticker="RECRT",
            decision="blocked",
            reason="selector:shadow_observation_signal_lane",
            rule_snapshot={
                "paper_observation_signal_lane": "hard_recert_shadow",
                "entry_edge": {
                    "expected_net_pct": 2.5,
                    "probability": 0.56,
                    "breakeven_probability": 0.45,
                    "probability_source": "pattern_regime_hit_rate",
                },
            },
        ),
        AutoTraderRun(
            user_id=user.id,
            breakout_alert_id=negative_alert.id,
            scan_pattern_id=negative_pat.id,
            ticker="NEGEV",
            decision="skipped",
            reason="non_positive_expected_edge",
            rule_snapshot={
                "entry_edge": {
                    "expected_net_pct": -0.42,
                    "probability": 0.21,
                    "breakeven_probability": 0.32,
                    "probability_source": "directional_mfe_mae_pattern",
                },
            },
        ),
        AutoTraderRun(
            user_id=user.id,
            breakout_alert_id=slippage_alert.id,
            scan_pattern_id=slippage_pat.id,
            ticker="SLIP",
            decision="skipped",
            reason="missed_entry_slippage",
            rule_snapshot={
                "entry_edge": {
                    "expected_net_pct": 1.1,
                    "probability": 0.58,
                    "breakeven_probability": 0.47,
                    "probability_source": "directional_mfe_mae_pattern",
                },
                "entry_slippage_pct": 3.75,
                "entry_slippage_signed_pct": 3.75,
                "entry_slippage_direction": "adverse",
                "slippage_tolerance_pct": 1.0,
                "slippage_source": "configured",
                "slippage_reprice_original_entry_price": 30.0,
                "slippage_reprice_current_price": 31.125,
                "slippage_reprice_expected_net_pct": -0.35,
                "slippage_reprice_positive_edge": False,
                "slippage_reprice_positive_edge_enabled": True,
                "slippage_reprice_max_pct": 2.5,
                "slippage_reprice_edge_reason": "non_positive_expected_edge",
            },
        ),
        AutoTraderRun(
            user_id=user.id,
            breakout_alert_id=error_alert.id,
            scan_pattern_id=error_pat.id,
            ticker="ERRQ",
            decision="error",
            reason="Query.filter() being called on a Query which already has LIMIT",
            rule_snapshot={
                "autotrader_exception": True,
                "error_phase": "process_alert",
                "error_type": "InvalidRequestError",
                "error_classification": "query_filter_after_limit",
            },
        ),
    ])
    db.commit()

    resp = client.get("/api/trading/monitor/imminent-alerts?hours=72")

    assert resp.status_code == 200
    body = resp.json()
    by_ticker = {row["ticker"]: row for row in body["alerts"]}
    assert by_ticker["RECRT"]["entry_edge_expected_net_pct"] == pytest.approx(2.5)
    assert by_ticker["RECRT"]["paper_observation_signal_lane"] == "hard_recert_shadow"
    assert by_ticker["RECRT"]["recert_required"] is True
    assert by_ticker["RECRT"]["autotrader_blocker_category"] == "positive_edge_recert_debt"
    assert by_ticker["NEGEV"]["autotrader_blocker_category"] == "negative_expected_edge"
    assert by_ticker["SLIP"]["autotrader_blocker_category"] == "missed_entry_slippage"
    assert by_ticker["SLIP"]["entry_slippage_pct"] == pytest.approx(3.75)
    assert by_ticker["SLIP"]["slippage_reprice_current_price"] == pytest.approx(31.125)
    assert by_ticker["SLIP"]["slippage_reprice_expected_net_pct"] == pytest.approx(-0.35)
    assert by_ticker["SLIP"]["slippage_reprice_positive_edge"] is False
    assert by_ticker["SLIP"]["slippage_reprice_positive_edge_enabled"] is True
    assert by_ticker["SLIP"]["slippage_reprice_max_pct"] == pytest.approx(2.5)
    assert by_ticker["SLIP"]["slippage_reprice_accepted"] is None
    assert (
        by_ticker["SLIP"]["slippage_reprice_next_action"]
        == "wait_for_fresh_entry_or_tighter_price"
    )
    assert (
        by_ticker["ERRQ"]["autotrader_blocker_category"]
        == "autotrader_execution_error"
    )
    assert by_ticker["ERRQ"]["autotrader_next_action"] == "inspect_autotrader_exception"
    assert by_ticker["ERRQ"]["autotrader_error_type"] == "InvalidRequestError"
    assert (
        by_ticker["ERRQ"]["autotrader_error_classification"]
        == "query_filter_after_limit"
    )
    assert by_ticker["ERRQ"]["autotrader_error_phase"] == "process_alert"
    assert body["summary"]["positive_edge_recert_debt"] >= 1
    assert body["summary"]["negative_expected_edge"] >= 1
    assert body["summary"]["missed_entry_slippage"] >= 1
    assert body["summary"]["autotrader_execution_errors"] >= 1


def test_edge_supply_endpoint_exposes_profitability_diagnostics(db, paired_client):
    client, user = paired_client

    pat = ScanPattern(
        name="edge_supply_ready",
        rules_json={},
        origin="brain",
        asset_class="stock",
        timeframe="1d",
        active=True,
        lifecycle_stage="promoted",
    )
    db.add(pat)
    db.flush()

    alert = BreakoutAlert(
        user_id=user.id,
        scan_pattern_id=pat.id,
        ticker="READY",
        asset_type="stock",
        alert_tier="pattern_imminent",
        outcome="pending",
        score_at_alert=86.0,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=47.0,
        target_price=58.0,
        alerted_at=datetime.utcnow(),
        indicator_snapshot={"imminent_scorecard": {"signal_lane": "standard"}},
    )
    db.add(alert)
    db.flush()

    db.add(
        AutoTraderRun(
            user_id=user.id,
            breakout_alert_id=alert.id,
            scan_pattern_id=pat.id,
            ticker="READY",
            decision="skipped",
            reason="selector:secondary_gate_observation",
            rule_snapshot={
                "entry_edge": {
                    "expected_net_pct": 2.0,
                    "probability": 0.62,
                    "breakeven_probability": 0.46,
                    "probability_source": "pattern_regime_hit_rate",
                },
            },
        )
    )
    for idx in range(5):
        db.add(
            PaperTrade(
                scan_pattern_id=pat.id,
                paper_shadow_of_alert_id=alert.id,
                ticker="READY",
                direction="long",
                entry_price=50.0,
                stop_price=47.0,
                target_price=58.0,
                quantity=1.0,
                status="closed",
                entry_date=datetime.utcnow() - timedelta(hours=idx + 1),
                exit_date=datetime.utcnow() - timedelta(minutes=idx + 1),
                exit_price=52.0,
                pnl=2.0,
                pnl_pct=4.0,
                signal_json={"paper_shadow": True},
            )
        )
    db.commit()

    resp = client.get("/api/trading/monitor/edge-supply?window_days=7&limit=10")

    assert resp.status_code == 200
    body = resp.json()
    row = next(x for x in body["rows"] if x["scan_pattern_id"] == pat.id)
    assert row["calibrated_ev_pct"] == pytest.approx(2.5)
    assert row["realized_ev_pct"] == pytest.approx(4.0)
    assert row["ev_calibration_error"] == pytest.approx(2.0)
    assert row["brier_score"] == pytest.approx(0.1444)
    assert row["closed_evidence_count"] == 5
    assert row["graduation_blocker"] == "graduation_ready"
    assert row["recommended_work_event"] == "edge_reliability_refresh"
    assert row["cash_deployment_rank"] == 1
    assert row["calibrated_ev_after_cost_pct"] == pytest.approx(2.45)
    assert row["cash_deployment_category"] == "live_deployable"
    assert row["allocation_score"] > 0
    assert body["summary"]["graduation_ready"] >= 1
    assert body["cash_deployment_summary"]["live_deployable"] >= 1


def test_cash_deployment_endpoint_separates_deployable_and_provenance(db, paired_client):
    client, user = paired_client

    pat = ScanPattern(
        name="cash_endpoint_ready",
        rules_json={},
        origin="brain",
        asset_class="stock",
        timeframe="1d",
        active=True,
        lifecycle_stage="promoted",
    )
    db.add(pat)
    db.flush()

    alert = BreakoutAlert(
        user_id=user.id,
        scan_pattern_id=pat.id,
        ticker="CASHR",
        asset_type="stock",
        alert_tier="pattern_imminent",
        outcome="pending",
        score_at_alert=86.0,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=47.0,
        target_price=58.0,
        alerted_at=datetime.utcnow(),
        indicator_snapshot={"imminent_scorecard": {"signal_lane": "standard"}},
    )
    db.add(alert)
    db.flush()
    db.add(
        AutoTraderRun(
            user_id=user.id,
            breakout_alert_id=alert.id,
            scan_pattern_id=pat.id,
            ticker="CASHR",
            decision="skipped",
            reason="selector:secondary_gate_observation",
            rule_snapshot={
                "entry_edge": {
                    "expected_net_pct": 2.0,
                    "probability": 0.62,
                    "breakeven_probability": 0.46,
                    "probability_source": "pattern_regime_hit_rate",
                },
            },
        )
    )
    for idx in range(5):
        db.add(
            PaperTrade(
                scan_pattern_id=pat.id,
                paper_shadow_of_alert_id=alert.id,
                ticker="CASHR",
                direction="long",
                entry_price=50.0,
                stop_price=47.0,
                target_price=58.0,
                quantity=1.0,
                status="closed",
                entry_date=datetime.utcnow() - timedelta(hours=idx + 2),
                exit_date=datetime.utcnow() - timedelta(hours=idx + 1),
                exit_price=52.0,
                pnl=2.0,
                pnl_pct=4.0,
                signal_json={"paper_shadow": True},
            )
        )
    db.add(
        PaperTrade(
            scan_pattern_id=None,
            ticker="NULLW",
            direction="short",
            entry_price=100.0,
            stop_price=110.0,
            target_price=80.0,
            quantity=10.0,
            status="closed",
            entry_date=datetime.utcnow() - timedelta(hours=3),
            exit_date=datetime.utcnow() - timedelta(hours=2),
            exit_price=85.0,
            pnl=150.0,
            pnl_pct=15.0,
            signal_json={"source": "null_lineage_short", "direction": "short"},
        )
    )
    db.commit()

    resp = client.get("/api/trading/monitor/cash-deployment?window_days=7&limit=10")

    assert resp.status_code == 200
    body = resp.json()
    row = next(x for x in body["rows"] if x["scan_pattern_id"] == pat.id)
    assert row["live_deployable"] is True
    assert row["cash_deployment_rank"] == 1
    assert row["calibrated_ev_after_cost_pct"] > 0
    assert row["max_safe_notional"] > 0
    assert body["summary"]["live_deployable"] >= 1
    assert body["summary"]["needs_provenance"] >= 1
    assert body["null_lineage_research_candidates"][0]["cash_deployment_category"] == "needs_provenance"


def test_active_setups_exposes_execution_state(db, paired_client):
    client, _user = paired_client
    user = db.query(models.User).order_by(models.User.id.desc()).first()
    assert user is not None

    trade = Trade(
        user_id=user.id,
        ticker="EXEC",
        direction="long",
        entry_price=10.0,
        quantity=5.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=12.0,
        broker_source="robinhood",
        pending_exit_status="deferred",
        pending_exit_reason="pattern_exit_now",
        pending_exit_requested_at=datetime.utcnow(),
    )
    db.add(trade)
    db.flush()
    db.add(
        PatternMonitorDecision(
            trade_id=trade.id,
            health_score=0.15,
            action="exit_now",
            decision_source="plan_levels",
            price_at_decision=10.25,
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
    )
    db.commit()

    with patch(
        "app.routers.trading_sub.monitor.ts.fetch_quotes_batch",
        return_value={"EXEC": {"price": 10.2}},
    ), patch(
        "app.routers.trading_sub.monitor.describe_trade_execution_state",
        return_value={
            "execution_state": "deferred",
            "execution_label": "WEEKEND CLOSED",
            "execution_reason": "Weekend closed",
            "pending_exit_status": "deferred",
            "pending_exit_order_id": None,
            "pending_exit_limit_price": None,
            "next_eligible_session_at": datetime.utcnow().isoformat(),
        },
    ):
        resp = client.get("/api/trading/active-setups")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    setup = next(row for row in body["setups"] if row["trade_id"] == trade.id)
    assert setup["execution_state"] == "deferred"
    assert setup["execution_label"] == "WEEKEND CLOSED"
    assert setup["execution_reason"] == "Weekend closed"
    assert setup["pending_exit_status"] == "deferred"
    assert setup["next_eligible_session_at"] is not None


def test_active_setups_suppresses_closed_broker_position(db, paired_client):
    client, user = paired_client

    pos = TradingPosition(
        user_id=user.id,
        broker_source="robinhood",
        account_type="cash",
        ticker="MONCLOSED",
        direction="long",
        current_quantity=0,
        current_avg_price=10.0,
        state="closed",
    )
    db.add(pos)
    db.flush()

    trade = Trade(
        user_id=user.id,
        ticker="MONCLOSED",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        entry_date=datetime.utcnow() - timedelta(hours=2),
        status="open",
        stop_loss=9.0,
        take_profit=12.0,
        broker_source="robinhood",
        position_id=pos.id,
    )
    db.add(trade)
    db.commit()

    resp = client.get("/api/trading/active-setups")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert all(row["ticker"] != "MONCLOSED" for row in body["setups"])
    assert body["summary"]["suppressed_stale_count"] >= 1
    assert any(
        row["ticker"] == "MONCLOSED" and row["reason"] == "position_identity_closed"
        for row in body["suppressed_stale_trades"]
    )


def test_active_setups_option_uses_premium_quote_not_underlying(db, paired_client):
    client, user = paired_client
    trade = Trade(
        user_id=user.id,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=0.80,
        take_profit=2.50,
        indicator_snapshot={
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "SPY",
                    "expiration": "2026-06-19",
                    "strike": 729.0,
                    "option_type": "call",
                },
            }
        },
    )
    db.add(trade)
    db.commit()

    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "spy-729c"}
    fake_options.get_quote.return_value = {"mark_price": "1.45"}

    with patch(
        "app.routers.trading_sub.monitor.ts.fetch_quotes_batch",
        side_effect=AssertionError("option active setups must not fetch underlying spot"),
    ), patch(
        "app.routers.trading_sub.monitor.describe_trade_execution_state",
        return_value={},
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        resp = client.get("/api/trading/active-setups")

    assert resp.status_code == 200
    body = resp.json()
    setup = next(row for row in body["setups"] if row["trade_id"] == trade.id)
    assert setup["current_price"] == pytest.approx(1.45)
    assert setup["quote_source"] == "robinhood_options"
    assert setup["pnl_pct"] == pytest.approx(16.0)
