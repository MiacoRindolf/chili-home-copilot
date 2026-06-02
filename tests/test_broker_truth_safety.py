from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.models.trading import AlertHistory, ScanPattern, Trade, TradingPosition


def _stale_rh_trade(db, *, ticker: str = "ACMR") -> Trade:
    pat = ScanPattern(
        name=f"stale {ticker}",
        rules_json={},
        origin="test",
        asset_class="stock",
        timeframe="1d",
        active=True,
        lifecycle_stage="promoted",
    )
    db.add(pat)
    db.flush()
    pos = TradingPosition(
        user_id=None,
        broker_source="robinhood",
        account_type="cash",
        ticker=ticker,
        direction="long",
        asset_kind="equity",
        current_quantity=0.0,
        current_avg_price=67.4,
        state="closed",
        last_observed_at=datetime.utcnow() - timedelta(days=1),
        last_state_transition_at=datetime.utcnow() - timedelta(days=1),
    )
    db.add(pos)
    db.flush()
    trade = Trade(
        user_id=None,
        ticker=ticker,
        direction="long",
        entry_price=67.4,
        quantity=6.0,
        status="open",
        broker_source="robinhood",
        broker_order_id="filled-entry-order",
        broker_status="filled",
        position_id=pos.id,
        scan_pattern_id=pat.id,
        stop_loss=89.25,
        take_profit=90.78,
        entry_date=datetime.utcnow() - timedelta(days=3),
        filled_at=datetime.utcnow() - timedelta(days=3),
        submitted_at=datetime.utcnow() - timedelta(days=3),
        last_broker_sync=datetime.utcnow() - timedelta(minutes=1),
        broker_sync_missing_streak=12,
    )
    db.add(trade)
    db.flush()
    pos.current_envelope_id = trade.id
    db.flush()
    return trade


def test_stop_engine_reconciles_stale_robinhood_trade_before_alerting(db):
    from app.services.trading.stop_engine import evaluate_all

    trade = _stale_rh_trade(db)
    db.commit()

    with patch(
        "app.services.trading.stop_engine._fetch_market_context",
        side_effect=AssertionError("stale RH trade must not fetch market data"),
    ), patch(
        "app.services.trading.stop_engine.evaluate_trade",
        side_effect=AssertionError("stale RH trade must not be stop-evaluated"),
    ), patch(
        "app.services.trading.stop_engine.get_adaptive_cooldowns",
        return_value={},
    ):
        out = evaluate_all(db, user_id=None)

    db.refresh(trade)
    assert out["total_checked"] == 0
    assert out["alerts"] == []
    assert out["skipped_stale_broker_positions"][0]["id"] == trade.id
    assert out["reconciled_stale_broker_positions"][0]["id"] == trade.id
    assert trade.status == "closed"
    assert trade.exit_reason == "broker_reconcile_no_exit_price"
    assert trade.exit_price is None
    assert trade.pnl is None
    assert trade.pending_exit_status is None


def test_mesh_critical_dispatch_suppresses_stale_robinhood_trade(db):
    from app.services.trading.brain_neural_mesh.action_handlers import (
        handle_action_signals,
    )

    trade = _stale_rh_trade(db, ticker="MESH")
    db.commit()
    state = SimpleNamespace(local_state={}, updated_at=None)

    with patch(
        "app.services.trading.alerts.dispatch_alert",
        side_effect=AssertionError("stale critical mesh signal must not dispatch"),
    ):
        decision = handle_action_signals(
            db,
            "nm_action_signals",
            state,
            {
                "children_state": {
                    "nm_stop_eval": {
                        "trade_id": trade.id,
                        "ticker": trade.ticker,
                        "action": "STOP_HIT",
                        "urgency": "critical",
                        "price": 88.0,
                        "stop_level": 89.25,
                    }
                }
            },
        )

    assert decision["action"] == "suppressed_stale_broker_position"
    assert decision["dispatched"] is False
    assert decision["broker_truth"]["reason"] == "position_identity_closed"


def test_mesh_action_signals_ignore_stale_child_local_state(db):
    from app.services.trading.brain_neural_mesh.action_handlers import (
        handle_action_signals,
    )

    state = SimpleNamespace(local_state={}, updated_at=None)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()

    with patch(
        "app.services.trading.alerts.dispatch_alert",
        side_effect=AssertionError("stale critical mesh state must not dispatch"),
    ):
        decision = handle_action_signals(
            db,
            "nm_action_signals",
            state,
            {
                "children_state": {
                    "nm_stop_eval": {
                        "trade_id": 2128,
                        "ticker": "RLS-USD",
                        "action": "STOP_HIT",
                        "urgency": "critical",
                        "price": 0.0036,
                        "stop_level": 0.00365,
                        "updated_at": old_ts,
                    },
                    "nm_imminent_eval": {
                        "ticker": "DEXT-USD",
                        "action": "imminent_breakout",
                        "urgency": "info",
                        "price": 0.12,
                        "updated_at": fresh_ts,
                    },
                }
            },
        )

    assert decision["urgency"] == "info"
    assert decision["action"] == "imminent_breakout"
    assert decision["ticker"] == "DEXT-USD"
    assert "broker_truth" not in decision


def test_mesh_critical_dispatch_suppresses_coinbase_missing_qty_backoff(db):
    from app.services.trading.brain_neural_mesh.action_handlers import (
        handle_action_signals,
    )

    trade = Trade(
        user_id=None,
        ticker="DIEM-USD",
        direction="long",
        entry_price=1570.60,
        quantity=0.02996,
        status="open",
        broker_source="coinbase",
        broker_status="filled",
        asset_kind="crypto",
        stop_loss=1551.51,
        take_profit=2080.73,
        entry_date=datetime.utcnow() - timedelta(days=1),
        pending_exit_status="deferred",
        pending_exit_reason="missing_broker_qty",
        crypto_broker_zero_qty_streak=19,
        indicator_snapshot={
            "crypto_exit_missing_qty_backoff": {
                "reason": "missing_broker_qty",
                "streak": 19,
                "backoff_until": (datetime.utcnow() + timedelta(minutes=5)).isoformat(),
            }
        },
    )
    db.add(trade)
    db.commit()
    state = SimpleNamespace(local_state={}, updated_at=None)

    with patch(
        "app.services.trading.alerts.dispatch_alert",
        side_effect=AssertionError("missing-qty backoff must not dispatch mesh alert"),
    ):
        decision = handle_action_signals(
            db,
            "nm_action_signals",
            state,
            {
                "children_state": {
                    "nm_stop_eval": {
                        "trade_id": trade.id,
                        "ticker": trade.ticker,
                        "action": "STOP_HIT",
                        "urgency": "critical",
                        "price": 1328.86,
                        "stop_level": 1551.51,
                    }
                }
            },
        )

    assert decision["action"] == "suppressed_non_actionable_trade_state"
    assert decision["dispatched"] is False
    assert decision["suppressed_reason"] == "crypto_missing_broker_qty_backoff"
    assert decision["broker_truth"]["pending_exit_reason"] == "missing_broker_qty"


def test_mesh_critical_dispatch_uses_durable_content_signature_cooldown(db):
    from app.services.trading.brain_neural_mesh.action_handlers import (
        _critical_dispatch_signature,
        handle_action_signals,
    )

    trade = Trade(
        user_id=None,
        ticker="COOL",
        direction="long",
        entry_price=10.25,
        quantity=1.0,
        status="open",
        stop_loss=10.0,
        take_profit=12.0,
        entry_date=datetime.utcnow() - timedelta(hours=2),
    )
    db.add(trade)
    db.flush()
    child = {
        "trade_id": trade.id,
        "ticker": "COOL",
        "action": "STOP_HIT",
        "urgency": "critical",
        "price": 9.91,
        "stop_level": 10.0,
    }
    db.add(
        AlertHistory(
            alert_type="stop_hit",
            ticker="COOL",
            message="previous critical",
            content_signature=_critical_dispatch_signature("STOP_HIT", child),
            sent_via="twilio",
            success=True,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()
    state = SimpleNamespace(local_state={}, updated_at=None)

    with patch(
        "app.services.trading.alerts.dispatch_alert",
        side_effect=AssertionError("durable cooldown must suppress dispatch"),
    ):
        decision = handle_action_signals(
            db,
            "nm_action_signals",
            state,
            {"children_state": {"nm_stop_eval": child}},
        )

    assert decision["dispatched"] is False
    assert decision["suppressed_reason"] == "critical_dispatch_cooldown"
    assert "9.91" not in decision["dispatch_signature"]


def test_sync_orders_does_not_refresh_already_filled_entry_clock(db, monkeypatch):
    from app.services import broker_service

    old_sync = datetime.utcnow() - timedelta(hours=4)
    trade = Trade(
        user_id=None,
        ticker="CLOCK",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        status="open",
        broker_source="robinhood",
        broker_order_id="entry-filled",
        broker_status="filled",
        entry_date=datetime.utcnow() - timedelta(days=1),
        last_broker_sync=old_sync,
    )
    db.add(trade)
    db.commit()

    monkeypatch.setattr(broker_service, "is_connected", lambda: True)
    monkeypatch.setattr(
        broker_service,
        "get_order_by_id",
        lambda _order_id: {
            "id": "entry-filled",
            "state": "filled",
            "side": "buy",
            "cumulative_quantity": "1",
            "average_price": "10.00",
        },
    )

    out = broker_service.sync_orders_to_db(db, user_id=None)

    db.refresh(trade)
    assert out["synced"] >= 1
    assert trade.last_broker_sync == old_sync


def test_fresh_robinhood_fill_inside_grace_is_not_marked_stale(db):
    from app.services.trading.broker_position_truth import broker_stale_open_trade_snapshot

    pos = TradingPosition(
        user_id=None,
        broker_source="robinhood",
        account_type="cash",
        ticker="FRESH",
        direction="long",
        asset_kind="equity",
        current_quantity=0.0,
        current_avg_price=10.0,
        state="closed",
        last_observed_at=datetime.utcnow(),
        last_state_transition_at=datetime.utcnow(),
    )
    db.add(pos)
    db.flush()
    trade = Trade(
        user_id=None,
        ticker="FRESH",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        status="open",
        broker_source="robinhood",
        broker_order_id="fresh-filled-entry",
        broker_status="filled",
        position_id=pos.id,
        entry_date=datetime.utcnow(),
        filled_at=datetime.utcnow(),
        submitted_at=datetime.utcnow(),
    )
    db.add(trade)
    db.commit()

    assert broker_stale_open_trade_snapshot(db, trade) is None


def test_portfolio_risk_excludes_stale_robinhood_open_trade(db):
    from app.services.trading.portfolio_risk import get_portfolio_risk_snapshot

    _stale_rh_trade(db, ticker="RISK")
    db.commit()

    budget = get_portfolio_risk_snapshot(db, user_id=None, capital=25_000.0)

    assert budget.open_positions == 0
    assert budget.stock_positions == 0
    assert budget.total_heat_pct == 0.0
    assert budget.can_open_new is True
