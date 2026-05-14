from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from app.models.trading import Trade
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
        entry_price=0.0,
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


@patch("app.services.coinbase_service.get_positions", return_value=[])
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
    monkeypatch.setattr(
        execution_hooks,
        "on_broker_reconciled_close",
        lambda *args, **kwargs: None,
    )

    result = sync_coinbase_positions_to_db(db, user_id=None)
    db.refresh(trade)

    assert result["closed"] == 1
    assert trade.status == "closed"
    assert trade.exit_reason == "coinbase_position_sync_gone"
    assert trade.exit_price == 1.25
    assert trade.pnl == 2.5
    assert trade.broker_status == "filled"
    assert trade.last_broker_sync is not None
    assert "exit priced from recent_sell_fills" in (trade.notes or "")
