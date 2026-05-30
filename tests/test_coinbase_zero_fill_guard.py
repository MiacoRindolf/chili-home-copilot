from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest


def test_current_lot_cost_basis_uses_sells_and_fees(monkeypatch) -> None:
    from app.services import coinbase_service

    coinbase_service._cache.clear()

    class Client:
        def get_fills(self, product_id: str, limit: int = 250):
            assert product_id == "ACX-USD"
            assert limit == 250
            return {
                "fills": [
                    {
                        "order_id": "old-buy",
                        "trade_time": "2026-05-10T08:11:13.250Z",
                        "side": "BUY",
                        "price": "0.0457966458",
                        "size": "6615",
                        "commission": "3.635337743604",
                    },
                    {
                        "order_id": "old-sell",
                        "trade_time": "2026-05-13T13:29:29.289Z",
                        "side": "SELL",
                        "price": "0.0422945312597",
                        "size": "6615",
                        "commission": "3.357339891394986",
                    },
                    {
                        "order_id": "new-buy",
                        "trade_time": "2026-05-30T03:36:55.415678Z",
                        "side": "BUY",
                        "price": "0.0427",
                        "size": "2241.1",
                        "commission": "0.38277988",
                    },
                    {
                        "order_id": "new-buy",
                        "trade_time": "2026-05-30T03:36:55.418582Z",
                        "side": "BUY",
                        "price": "0.0427",
                        "size": "1580.9",
                        "commission": "0.27001772",
                    },
                ]
            }

    monkeypatch.setattr(coinbase_service, "_get_client", lambda: Client())

    avg = coinbase_service._get_cost_basis_from_fills("ACX-USD", current_qty=3822.0)

    expected = ((3822.0 * 0.0427) + 0.38277988 + 0.27001772) / 3822.0
    assert avg == pytest.approx(expected, rel=1e-7)


@patch("app.services.coinbase_service.is_connected", return_value=True)
@patch("app.services.coinbase_service.get_positions")
@patch("app.services.coinbase_service.get_order_by_id")
def test_working_entry_zero_fill_order_adopts_position_truth(
    mock_get_order,
    mock_positions,
    mock_connected,
    db,
) -> None:
    from app.models.trading import Trade
    from app.services.coinbase_service import sync_orders_to_db

    trade = Trade(
        user_id=None,
        ticker="ACX-USD",
        direction="long",
        entry_price=0.0427,
        quantity=3822.0,
        filled_quantity=0.0,
        remaining_quantity=3822.0,
        status="working",
        broker_source="coinbase",
        broker_order_id="cb-entry-zero",
        broker_status="open",
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    mock_get_order.return_value = {
        "order_id": "cb-entry-zero",
        "status": "FILLED",
        "product_id": "ACX-USD",
        "base_size": "3822",
        "filled_size": "0",
        "average_filled_price": None,
    }
    mock_positions.return_value = [
        {
            "ticker": "ACX-USD",
            "quantity": 3822.0,
            "average_buy_price": 0.0428708,
            "broker_source": "coinbase",
        }
    ]

    result = sync_orders_to_db(db, user_id=None)

    db.refresh(trade)
    assert result["filled"] == 1
    assert result["cancelled"] == 0
    assert trade.status == "open"
    assert trade.broker_status == "filled"
    assert trade.quantity == 3822.0
    assert trade.filled_quantity == 3822.0
    assert trade.remaining_quantity == 0.0
    assert trade.entry_price == 0.0428708
    assert trade.avg_fill_price == 0.0428708
    assert trade.position_id is not None


@patch("app.services.coinbase_service.is_connected", return_value=True)
@patch("app.services.coinbase_service._coinbase_order_fill_truth", return_value=None)
@patch("app.services.coinbase_service.get_positions", return_value=[])
@patch("app.services.coinbase_service.get_order_by_id")
def test_working_entry_zero_fill_without_broker_truth_is_not_opened(
    mock_get_order,
    mock_positions,
    mock_fill_truth,
    mock_connected,
    db,
) -> None:
    from app.models.trading import Trade
    from app.services.coinbase_service import sync_orders_to_db

    trade = Trade(
        user_id=None,
        ticker="ACX-USD",
        direction="long",
        entry_price=0.0427,
        quantity=3822.0,
        filled_quantity=0.0,
        remaining_quantity=3822.0,
        status="working",
        broker_source="coinbase",
        broker_order_id="cb-entry-zero",
        broker_status="open",
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    mock_get_order.return_value = {
        "order_id": "cb-entry-zero",
        "status": "FILLED",
        "product_id": "ACX-USD",
        "base_size": "3822",
        "filled_size": "0",
        "average_filled_price": None,
    }

    result = sync_orders_to_db(db, user_id=None)

    db.refresh(trade)
    assert result["filled"] == 0
    assert result["cancelled"] == 1
    assert trade.status == "cancelled"
    assert trade.broker_status == "filled_zero_quantity"
    assert trade.filled_quantity == 0.0
    assert trade.remaining_quantity == 0.0


@patch("app.services.coinbase_service.cancel_order_by_id")
@patch("app.services.coinbase_service.get_order_by_id")
@patch("app.services.coinbase_service.collapse_open_broker_position_duplicates")
@patch("app.services.coinbase_service.acquire_broker_position_sync_lock")
@patch("app.services.coinbase_service.is_connected", return_value=True)
@patch("app.services.coinbase_service.get_positions")
def test_position_sync_cancels_duplicate_zero_fill_buy_order(
    mock_positions,
    mock_connected,
    mock_lock,
    mock_collapse,
    mock_get_order,
    mock_cancel,
    db,
) -> None:
    from app.models.trading import BracketIntent, Trade
    from app.services.coinbase_service import sync_positions_to_db

    open_trade = Trade(
        user_id=None,
        ticker="ACX-USD",
        direction="long",
        entry_price=0.0428708,
        quantity=100.0,
        filled_quantity=100.0,
        remaining_quantity=0.0,
        status="open",
        broker_source="coinbase",
        broker_status="filled",
        entry_date=datetime.utcnow(),
    )
    intent = BracketIntent(
        trade_id=0,
        user_id=None,
        ticker="ACX-USD",
        direction="long",
        quantity=100.0,
        entry_price=0.0426,
        stop_price=0.0402,
        target_price=0.0465,
        intent_state="intent",
        shadow_mode=False,
        broker_source="coinbase",
    )
    duplicate = Trade(
        user_id=None,
        ticker="ACX-USD",
        direction="long",
        entry_price=0.0426,
        quantity=3819.8,
        filled_quantity=0.0,
        remaining_quantity=3819.8,
        status="working",
        broker_source="coinbase",
        broker_order_id="duplicate-buy",
        broker_status="open",
        entry_date=datetime.utcnow(),
    )
    db.add_all([open_trade, duplicate])
    db.flush()
    intent.trade_id = open_trade.id
    db.add(intent)
    db.commit()
    db.refresh(open_trade)
    db.refresh(duplicate)

    mock_collapse.return_value = {"cancelled": 0}
    mock_positions.return_value = [
        {
            "ticker": "ACX-USD",
            "quantity": 3822.0,
            "average_buy_price": 0.0428708,
            "broker_source": "coinbase",
        }
    ]
    mock_get_order.return_value = {
        "order_id": "duplicate-buy",
        "status": "OPEN",
        "product_id": "ACX-USD",
        "side": "BUY",
        "filled_size": "0",
    }
    mock_cancel.return_value = {"ok": True, "order_id": "duplicate-buy"}

    result = sync_positions_to_db(db, user_id=None)

    db.refresh(duplicate)
    db.refresh(intent)
    assert result["updated"] == 1
    assert result["duplicate_entry_orders_cancelled"] == 1
    mock_cancel.assert_called_once_with("duplicate-buy")
    assert duplicate.status == "cancelled"
    assert duplicate.broker_status == "cancelled"
    assert duplicate.exit_reason == "coinbase_duplicate_entry_live_position"
    assert open_trade.quantity == 3822.0
    assert intent.quantity == 3822.0
    assert intent.entry_price == 0.0428708
    assert intent.last_diff_reason == "coinbase_position_truth_quantity"
