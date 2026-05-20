from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text


def test_finalize_filled_exit_closes_bracket_intent(db, monkeypatch):
    from app.models.trading import Trade
    from app.services.trading import robinhood_exit_execution as rh_exit

    monkeypatch.setattr(
        "app.services.trading.brain_work.execution_hooks.on_live_trade_closed",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.trading.auto_trader_position_overrides.clear_position_overrides",
        lambda *args, **kwargs: None,
    )

    trade = Trade(
        user_id=None,
        ticker="BRKTEST",
        direction="long",
        entry_price=10.0,
        quantity=2.0,
        entry_date=datetime.utcnow(),
        status="open",
        broker_source="robinhood",
    )
    db.add(trade)
    db.flush()
    db.execute(text("""
        INSERT INTO trading_bracket_intents (
            trade_id, ticker, direction, quantity, entry_price,
            stop_price, target_price, intent_state, shadow_mode,
            broker_source, payload_json, created_at, updated_at
        ) VALUES (
            :tid, 'BRKTEST', 'long', 2.0, 10.0,
            9.0, 12.0, 'reconciled', false,
            'robinhood', '{}'::jsonb, NOW(), NOW()
        )
    """), {"tid": trade.id})
    db.commit()
    db.refresh(trade)

    pnl = rh_exit._finalize_filled_exit(
        db,
        trade,
        raw_order={"state": "filled", "average_price": "11.25"},
        exit_reason="pattern_exit_now",
        fallback_price=None,
        filled_at=datetime.now(timezone.utc),
    )

    db.refresh(trade)
    assert pnl == 2.5
    assert trade.status == "closed"

    row = db.execute(text(
        "SELECT intent_state, last_diff_reason "
        "FROM trading_bracket_intents WHERE trade_id = :tid"
    ), {"tid": trade.id}).first()
    assert row[0] == "closed"
    assert row[1] == "exit_fill:pattern_exit_now"
