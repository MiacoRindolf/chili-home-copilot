from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from app import models
from app.models.trading import PatternMonitorDecision, Trade, TradingPosition


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
