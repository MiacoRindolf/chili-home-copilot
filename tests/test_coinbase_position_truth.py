from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.trading import ScanPattern, Trade, TradingExecutionEvent


def test_coinbase_positions_and_portfolio_use_live_usd_marks(monkeypatch):
    from app.services import coinbase_service

    coinbase_service.clear_cache()

    class Client:
        def get_accounts(self, limit: int = 250):
            assert limit == 250
            return {
                "accounts": [
                    {
                        "currency": "USD",
                        "available_balance": {"value": "100", "currency": "USD"},
                        "hold": {"value": "0", "currency": "USD"},
                    },
                    {
                        "currency": "ACX",
                        "name": "Across Protocol",
                        "available_balance": {"value": "0", "currency": "ACX"},
                        "hold": {"value": "3822", "currency": "ACX"},
                    },
                    {
                        "currency": "USDC",
                        "name": "USDC",
                        "available_balance": {"value": "50", "currency": "USDC"},
                        "hold": {"value": "0", "currency": "USDC"},
                    },
                ]
            }

        def get_market_trades(self, product_id: str, limit: int = 1):
            assert product_id == "ACX-USD"
            assert limit == 1
            return {"trades": [{"price": "0.0426"}]}

        def get_best_bid_ask(self, product_ids):
            return {
                "pricebooks": [
                    {
                        "product_id": "ACX-USD",
                        "bids": [{"price": "0.0425"}],
                        "asks": [{"price": "0.0427"}],
                    }
                ]
            }

        def get_fills(self, product_id: str, limit: int = 250):
            if product_id == "USDC-USD":
                return {"fills": []}
            assert product_id == "ACX-USD"
            return {
                "fills": [
                    {
                        "order_id": "acx-buy",
                        "trade_time": "2026-05-30T03:36:55Z",
                        "side": "BUY",
                        "price": "0.0427",
                        "size": "3822",
                        "commission": "0.6527976",
                    }
                ]
            }

    monkeypatch.setattr(coinbase_service, "is_connected", lambda: True)
    monkeypatch.setattr(coinbase_service, "_get_client", lambda: Client())

    positions = coinbase_service.get_positions(use_cache=False)

    acx = next(p for p in positions if p["ticker"] == "ACX-USD")
    assert acx["quantity"] == pytest.approx(3822.0)
    assert acx["available_quantity"] == pytest.approx(0.0)
    assert acx["held_quantity"] == pytest.approx(3822.0)
    assert acx["current_price"] == pytest.approx(0.0426)
    assert acx["equity"] == pytest.approx(3822.0 * 0.0426)
    assert acx["average_buy_price"] == pytest.approx(
        ((3822.0 * 0.0427) + 0.6527976) / 3822.0,
        rel=1e-7,
    )
    assert acx["equity_change"] < 0
    assert acx["percent_change"] < 0
    usdc = next(p for p in positions if p["ticker"] == "USDC-USD")
    assert usdc["current_price"] == pytest.approx(1.0)
    assert usdc["equity"] == pytest.approx(50.0)

    coinbase_service.clear_cache()
    portfolio = coinbase_service.get_portfolio()
    assert portfolio["cash"] == pytest.approx(100.0)
    assert portfolio["buying_power"] == pytest.approx(100.0)
    assert portfolio["equity"] == pytest.approx(round(150.0 + 3822.0 * 0.0426, 2))


def test_coinbase_unavailable_product_suppresses_repeated_position_mark_queries(monkeypatch):
    from app.services import coinbase_service

    coinbase_service.clear_cache()

    class Client:
        fills_calls = 0
        market_trade_calls = 0
        bbo_calls = 0

        def get_fills(self, product_id: str, limit: int = 250):
            self.fills_calls += 1
            assert product_id == "GAL-USD"
            raise RuntimeError('ProductID "GAL-USD" could not be found.')

        def get_market_trades(self, product_id: str, limit: int = 1):
            self.market_trade_calls += 1
            raise AssertionError("unavailable products should not be re-priced")

        def get_best_bid_ask(self, product_ids):
            self.bbo_calls += 1
            raise AssertionError("unavailable products should not fetch BBO")

    client = Client()
    monkeypatch.setattr(coinbase_service, "_get_client", lambda: client)

    assert coinbase_service._get_cost_basis_from_fills("GAL-USD", current_qty=1.0) == 0.0
    assert client.fills_calls == 1

    price_cache: dict[str, float] = {}
    assert coinbase_service._coinbase_current_price("GAL-USD", price_cache) == 0.0
    assert price_cache["GAL-USD"] == 0.0
    assert client.market_trade_calls == 0
    assert client.bbo_calls == 0

    assert coinbase_service._get_cost_basis_from_fills("GAL-USD", current_qty=1.0) == 0.0
    assert client.fills_calls == 1


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
