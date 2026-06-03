from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import text

from app.models.trading import Trade, TradingExecutionEvent
from app.services.coinbase_service import sync_positions_to_db as sync_coinbase_positions_to_db
from app.services.broker_service import sync_positions_to_db as sync_robinhood_positions_to_db
from app.services.trading.broker_position_sync import collapse_open_broker_position_duplicates


def test_collapse_open_broker_position_duplicates_merges_into_canonical(db):
    earlier = datetime.utcnow() - timedelta(days=2)
    later = earlier + timedelta(hours=3)

    duplicate = Trade(
        user_id=None,
        ticker="ABM",
        direction="long",
        entry_price=41.25,
        quantity=8,
        entry_date=earlier,
        status="open",
        broker_source="robinhood",
        stop_loss=39.5,
        take_profit=46.0,
        indicator_snapshot={"source": "duplicate"},
        notes="duplicate row",
    )
    canonical = Trade(
        user_id=None,
        ticker="ABM",
        direction="long",
        entry_price=41.25,
        quantity=8,
        entry_date=later,
        status="open",
        broker_source="robinhood",
        broker_order_id="rh-ord-1",
        notes="canonical row",
    )
    db.add_all([duplicate, canonical])
    db.commit()

    result = collapse_open_broker_position_duplicates(
        db, broker_source="robinhood", user_id=None,
    )
    db.commit()
    db.refresh(duplicate)
    db.refresh(canonical)

    assert result == {"groups": 1, "cancelled": 1}
    assert canonical.status == "open"
    assert canonical.entry_price == 41.25
    assert canonical.stop_loss == 39.5
    assert canonical.take_profit == 46.0
    assert canonical.indicator_snapshot == {"source": "duplicate"}
    assert canonical.entry_date == earlier
    assert duplicate.status == "cancelled"
    assert duplicate.exit_reason == "sync_duplicate"
    assert duplicate.exit_date is not None


@patch("app.services.broker_service._compute_trade_snapshot", return_value=None)
@patch("app.services.broker_service.get_crypto_positions", return_value=[])
@patch(
    "app.services.broker_service.get_positions",
    return_value=[{"ticker": "ABM", "quantity": 8, "average_buy_price": 41.25}],
)
@patch("app.services.broker_service.is_connected", return_value=True)
def test_robinhood_sync_cancels_existing_duplicate_rows(
    _connected,
    _positions,
    _crypto,
    _snapshot,
    db,
):
    t1 = Trade(
        user_id=None,
        ticker="ABM",
        direction="long",
        entry_price=41.25,
        quantity=8,
        status="open",
        broker_source="robinhood",
        notes="first",
    )
    t2 = Trade(
        user_id=None,
        ticker="ABM",
        direction="long",
        entry_price=41.25,
        quantity=8,
        status="open",
        broker_source="robinhood",
        notes="second",
    )
    db.add_all([t1, t2])
    db.commit()

    result = sync_robinhood_positions_to_db(db, user_id=None)

    rows = (
        db.query(Trade)
        .filter(Trade.ticker == "ABM", Trade.broker_source == "robinhood")
        .order_by(Trade.id.asc())
        .all()
    )
    open_rows = [row for row in rows if row.status == "open"]
    cancelled_rows = [row for row in rows if row.status == "cancelled"]

    assert result["created"] == 0
    assert result["updated"] == 1
    assert result["deduped"] == 1
    assert len(open_rows) == 1
    assert len(cancelled_rows) == 1
    assert open_rows[0].last_broker_sync is not None
    assert cancelled_rows[0].exit_reason == "sync_duplicate"


@patch(
    "app.services.coinbase_service.get_positions",
    return_value=[
        {"ticker": "eth-usd", "quantity": 0.5, "average_buy_price": 2000.0},
        {"ticker": "ETH-USD", "quantity": 0.5, "average_buy_price": 2000.0},
    ],
)
@patch("app.services.coinbase_service.is_connected", return_value=True)
def test_coinbase_sync_dedupes_duplicate_incoming_positions(
    _connected,
    _positions,
    db,
):
    result = sync_coinbase_positions_to_db(db, user_id=None)

    rows = (
        db.query(Trade)
        .filter(Trade.ticker == "ETH-USD", Trade.broker_source == "coinbase")
        .all()
    )

    assert result["created"] == 1
    assert result["updated"] == 0
    assert result["deduped"] == 0
    assert len(rows) == 1
    assert rows[0].status == "open"
    assert rows[0].quantity == 0.5


@patch(
    "app.services.coinbase_service.get_positions",
    return_value=[
        {"ticker": "XYZ-USD", "quantity": 2.0, "average_buy_price": 1.0},
    ],
)
@patch("app.services.coinbase_service.is_connected", return_value=True)
def test_coinbase_sync_scores_stale_close_from_sell_fills(
    _connected,
    _positions,
    db,
    monkeypatch,
):
    from app.services import coinbase_service
    from app.services.trading.brain_work import execution_hooks

    entry_at = datetime.utcnow() - timedelta(minutes=15)
    fill_at = datetime.utcnow() - timedelta(minutes=1)
    trade = Trade(
        user_id=None,
        ticker="ABC-USD",
        direction="long",
        entry_price=1.0,
        quantity=10.0,
        entry_date=entry_at,
        status="open",
        broker_source="coinbase",
        broker_order_id="buy-1",
        broker_sync_missing_streak=1,
        pending_exit_status="submitted",
        pending_exit_requested_at=entry_at,
        pending_exit_reason="pattern_exit_now",
        pending_exit_limit_price=1.25,
    )
    db.add(trade)
    db.commit()

    class _FakeClient:
        def get_fills(self, product_id=None, limit=100):
            return {
                "fills": [
                    {
                        "product_id": "ABC-USD",
                        "side": "SELL",
                        "price": "1.25",
                        "size": "10",
                        "trade_time": fill_at.isoformat() + "Z",
                    }
                ]
            }

    monkeypatch.setattr(coinbase_service, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr(coinbase_service, "_COINBASE_RECONCILE_MISSING_STREAK_MIN", 2)
    monkeypatch.setattr(
        execution_hooks,
        "on_broker_reconciled_close",
        lambda *args, **kwargs: None,
    )

    result = sync_coinbase_positions_to_db(db, user_id=None)
    db.refresh(trade)

    assert result["closed"] == 1
    assert trade.status == "closed"
    assert trade.exit_reason == "pattern_exit_now"
    assert trade.exit_price == 1.25
    assert trade.pnl == 2.5
    assert trade.broker_status == "filled"
    assert trade.last_broker_sync is not None
    assert trade.pending_exit_status is None
    assert trade.pending_exit_reason is None
    assert "exit priced from recent_sell_fills" in (trade.notes or "")
    event = (
        db.query(TradingExecutionEvent)
        .filter(TradingExecutionEvent.trade_id == trade.id)
        .filter(TradingExecutionEvent.event_type == "coinbase_sync_gone_close")
        .one()
    )
    assert event.payload_json["exit_reason"] == "pattern_exit_now"
    assert event.payload_json["pending_exit_reason"] == "pattern_exit_now"
    assert (
        event.payload_json["broker_reconcile_exit_reason"]
        == "coinbase_position_sync_gone"
    )


@patch(
    "app.services.coinbase_service.get_positions",
    return_value=[
        {"ticker": "XYZ-USD", "quantity": 1.0, "average_buy_price": 2.0},
    ],
)
@patch("app.services.coinbase_service.is_connected", return_value=True)
def test_coinbase_sync_partial_list_first_miss_defers_close(
    _connected,
    _positions,
    db,
    monkeypatch,
):
    from app.services import coinbase_service

    old = datetime.utcnow() - timedelta(minutes=30)
    trade = Trade(
        user_id=None,
        ticker="ABC-USD",
        direction="long",
        entry_price=1.0,
        quantity=10.0,
        entry_date=old,
        last_broker_sync=old,
        status="open",
        broker_source="coinbase",
        broker_sync_missing_streak=0,
    )
    db.add(trade)
    db.commit()

    monkeypatch.setattr(coinbase_service, "_COINBASE_RECONCILE_MISSING_STREAK_MIN", 2)
    monkeypatch.setattr(coinbase_service, "_coinbase_has_working_sell_orders", lambda _ticker: False)

    result = sync_coinbase_positions_to_db(db, user_id=None)
    db.refresh(trade)

    assert result["closed"] == 0
    assert trade.status == "open"
    assert trade.broker_sync_missing_streak == 1


@patch(
    "app.services.coinbase_service.get_positions",
    return_value=[
        {"ticker": "XYZ-USD", "quantity": 1.0, "average_buy_price": 2.0},
    ],
)
@patch("app.services.coinbase_service.is_connected", return_value=True)
def test_coinbase_sync_fresh_observation_window_defers_close(
    _connected,
    _positions,
    db,
    monkeypatch,
):
    from app.services import coinbase_service

    trade = Trade(
        user_id=None,
        ticker="ABC-USD",
        direction="long",
        entry_price=1.0,
        quantity=10.0,
        entry_date=datetime.utcnow() - timedelta(hours=2),
        last_broker_sync=datetime.utcnow(),
        status="open",
        broker_source="coinbase",
        broker_sync_missing_streak=5,
    )
    db.add(trade)
    db.commit()

    monkeypatch.setattr(coinbase_service, "_COINBASE_RECONCILE_MISSING_STREAK_MIN", 2)
    monkeypatch.setattr(coinbase_service, "_COINBASE_RECONCILE_CONFIRM_WINDOW", 300)
    monkeypatch.setattr(coinbase_service, "_coinbase_has_working_sell_orders", lambda _ticker: False)

    result = sync_coinbase_positions_to_db(db, user_id=None)
    db.refresh(trade)

    assert result["closed"] == 0
    assert trade.status == "open"
    assert trade.exit_reason is None
    assert trade.broker_sync_missing_streak == 6


@patch(
    "app.services.coinbase_service.get_positions",
    return_value=[
        {"ticker": "XYZ-USD", "quantity": 1.0, "average_buy_price": 2.0},
    ],
)
@patch("app.services.coinbase_service.is_connected", return_value=True)
def test_coinbase_sync_missing_without_sell_fill_keeps_monitoring(
    _connected,
    _positions,
    db,
    monkeypatch,
):
    from app.services import coinbase_service

    trade = Trade(
        user_id=None,
        ticker="ABC-USD",
        direction="long",
        entry_price=1.0,
        quantity=10.0,
        entry_date=datetime.utcnow() - timedelta(hours=2),
        last_broker_sync=datetime.utcnow() - timedelta(hours=2),
        status="open",
        broker_source="coinbase",
        broker_sync_missing_streak=5,
    )
    db.add(trade)
    db.commit()

    class _FakeClient:
        def get_fills(self, product_id=None, limit=100):
            return {"fills": []}

    monkeypatch.setattr(coinbase_service, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr(coinbase_service, "_COINBASE_RECONCILE_MISSING_STREAK_MIN", 2)
    monkeypatch.setattr(coinbase_service, "_COINBASE_RECONCILE_CONFIRM_WINDOW", 300)
    monkeypatch.setattr(coinbase_service, "_coinbase_has_working_sell_orders", lambda _ticker: False)

    result = sync_coinbase_positions_to_db(db, user_id=None)
    db.refresh(trade)

    assert result["closed"] == 0
    assert trade.status == "open"
    assert trade.exit_reason is None
    assert trade.exit_price is None
    assert trade.pnl is None


@patch(
    "app.services.coinbase_service.get_positions",
    return_value=[
        {"ticker": "XYZ-USD", "quantity": 1.0, "average_buy_price": 2.0},
    ],
)
@patch("app.services.coinbase_service.is_connected", return_value=True)
def test_coinbase_sync_long_absent_no_fill_closes_without_pnl(
    _connected,
    _positions,
    db,
    monkeypatch,
):
    from app.services import coinbase_service
    from app.services.trading.brain_work import execution_hooks

    old = datetime.utcnow() - timedelta(hours=2)
    trade = Trade(
        user_id=None,
        ticker="ABC-USD",
        direction="long",
        entry_price=1.0,
        quantity=10.0,
        entry_date=old,
        last_broker_sync=old,
        status="open",
        broker_source="coinbase",
        broker_sync_missing_streak=20,
    )
    db.add(trade)
    db.commit()

    class _FakeClient:
        def get_fills(self, product_id=None, limit=100):
            return {"fills": []}

    monkeypatch.setattr(coinbase_service, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr(coinbase_service, "_COINBASE_RECONCILE_MISSING_STREAK_MIN", 2)
    monkeypatch.setattr(coinbase_service, "_COINBASE_RECONCILE_CONFIRM_WINDOW", 300)
    monkeypatch.setattr(coinbase_service, "_COINBASE_ABSENT_NO_FILL_STREAK_MIN", 12)
    monkeypatch.setattr(coinbase_service, "_COINBASE_ABSENT_NO_FILL_MIN_AGE_SECONDS", 300)
    monkeypatch.setattr(coinbase_service, "_coinbase_has_working_sell_orders", lambda _ticker: False)
    monkeypatch.setattr(
        execution_hooks,
        "on_broker_reconciled_close",
        lambda *args, **kwargs: None,
    )

    result = sync_coinbase_positions_to_db(db, user_id=None)
    db.refresh(trade)

    assert result["closed"] == 1
    assert trade.status == "closed"
    assert trade.exit_reason == "broker_reconcile_no_exit_price"
    assert trade.exit_price is None
    assert trade.pnl is None
    assert trade.broker_status == "no_position"
    row = db.execute(
        text(
            "SELECT status, average_fill_price, cumulative_filled_quantity "
            "FROM trading_execution_events "
            "WHERE trade_id = :trade_id "
            "AND event_type = 'coinbase_position_absent_no_fill_close'"
        ),
        {"trade_id": trade.id},
    ).first()
    assert row is not None
    assert tuple(row) == ("closed", None, 0.0)


@patch(
    "app.services.coinbase_service.get_positions",
    return_value=[
        {"ticker": "ABC-USD", "quantity": 10.0, "average_buy_price": 1.05},
    ],
)
@patch("app.services.coinbase_service.is_connected", return_value=True)
def test_coinbase_sync_reopens_recent_bookkeeping_close(
    _connected,
    _positions,
    db,
):
    closed_at = datetime.utcnow() - timedelta(minutes=20)
    trade = Trade(
        user_id=None,
        ticker="ABC-USD",
        direction="long",
        entry_price=1.0,
        quantity=10.0,
        entry_date=datetime.utcnow() - timedelta(hours=1),
        exit_date=closed_at,
        status="closed",
        broker_source="coinbase",
        exit_reason="coinbase_position_sync_gone",
    )
    db.add(trade)
    db.commit()

    db.execute(text("""
        INSERT INTO trading_bracket_intents (
            trade_id, ticker, direction, quantity, entry_price,
            stop_price, target_price, intent_state, shadow_mode,
            broker_source, payload_json, created_at, updated_at
        ) VALUES (
            :tid, 'ABC-USD', 'long', 10.0, 1.0,
            0.9, 1.2, 'reconciled', false,
            'coinbase', '{}'::jsonb, NOW(), NOW()
        )
    """), {"tid": trade.id})
    db.commit()

    result = sync_coinbase_positions_to_db(db, user_id=None)
    db.refresh(trade)

    assert result["reopened"] == 1
    assert result["created"] == 0
    assert trade.status == "open"
    assert trade.exit_reason is None
    assert trade.exit_date is None
    assert trade.exit_price is None
    assert trade.pnl is None
    assert trade.quantity == 10.0
    assert trade.entry_price == 1.05
    assert trade.broker_sync_missing_streak == 0

    intent_state = db.execute(text(
        "SELECT intent_state FROM trading_bracket_intents WHERE trade_id = :tid"
    ), {"tid": trade.id}).scalar()
    assert intent_state == "intent"
