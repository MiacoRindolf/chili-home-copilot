from __future__ import annotations

from sqlalchemy import text

from app.models.trading import ScanPattern, Trade, TradingExecutionEvent


def test_coinbase_position_sync_aligns_existing_trade_to_broker_truth(
    db,
    monkeypatch,
):
    from app.services import coinbase_service

    pattern = ScanPattern(
        name="coinbase position truth test",
        rules_json={},
        active=True,
        lifecycle_stage="promoted",
        promotion_status="promoted",
    )
    db.add(pattern)
    db.flush()
    trade = Trade(
        user_id=None,
        ticker="QNT-USD",
        direction="long",
        entry_price=70.0,
        quantity=1.0,
        filled_quantity=0.0,
        remaining_quantity=1.0,
        status="open",
        broker_source="coinbase",
        broker_status="open",
        scan_pattern_id=pattern.id,
        management_scope="auto_trader_v1",
    )
    db.add(trade)
    db.commit()

    broker_position = {
        "ticker": "QNT-USD",
        "quantity": 0.56601291,
        "average_buy_price": 71.52994973471097,
    }
    monkeypatch.setattr(coinbase_service, "is_connected", lambda: True)
    monkeypatch.setattr(coinbase_service, "get_positions", lambda: [broker_position])
    monkeypatch.setattr(
        coinbase_service,
        "acquire_broker_position_sync_lock",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        coinbase_service,
        "collapse_open_broker_position_duplicates",
        lambda *args, **kwargs: {"cancelled": 0},
    )

    result = coinbase_service.sync_positions_to_db(db, user_id=None)

    db.refresh(trade)
    assert result["updated"] == 1
    assert trade.status == "open"
    assert trade.broker_status == "filled"
    assert trade.quantity == broker_position["quantity"]
    assert trade.filled_quantity == broker_position["quantity"]
    assert trade.remaining_quantity == 0.0
    assert trade.entry_price == broker_position["average_buy_price"]
    assert trade.avg_fill_price == broker_position["average_buy_price"]
    assert trade.position_id is not None
    position_row = db.execute(
        text(
            "SELECT current_quantity, current_avg_price "
            "FROM trading_positions WHERE id = :position_id"
        ),
        {"position_id": int(trade.position_id)},
    ).one()
    assert position_row[0] == broker_position["quantity"]
    assert position_row[1] == broker_position["average_buy_price"]

    event = (
        db.query(TradingExecutionEvent)
        .filter(TradingExecutionEvent.trade_id == trade.id)
        .filter(
            TradingExecutionEvent.event_type
            == "coinbase_position_sync_inventory"
        )
        .one()
    )
    assert event.status == "filled"
    assert event.cumulative_filled_quantity == broker_position["quantity"]
    assert event.average_fill_price == broker_position["average_buy_price"]
    assert event.payload_json["synthetic"] is True

    coinbase_service.sync_positions_to_db(db, user_id=None)
    event_count = (
        db.query(TradingExecutionEvent)
        .filter(TradingExecutionEvent.trade_id == trade.id)
        .filter(
            TradingExecutionEvent.event_type
            == "coinbase_position_sync_inventory"
        )
        .count()
    )
    assert event_count == 1
