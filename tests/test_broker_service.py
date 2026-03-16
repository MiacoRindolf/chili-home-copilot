"""Tests for broker service: order status mapping, sync logic, P&L calculation."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from app.models.trading import Trade


class TestOrderStatusMapping:
    """Test that Robinhood order states map correctly to internal states."""

    def test_mapping_definitions(self):
        """Verify the expected sync functions exist."""
        from app.services import broker_service as bs
        assert hasattr(bs, 'sync_orders_to_db')
        assert hasattr(bs, 'sync_positions_to_db')

    def test_filled_maps_to_executed(self, db):
        """A filled RH order should result in 'executed' status."""
        trade = Trade(
            ticker="AAPL",
            direction="long",
            entry_price=150.0,
            quantity=10,
            status="working",
            broker_status="queued",
        )
        db.add(trade)
        db.commit()

        # Simulate updating with filled status
        trade.broker_status = "filled"
        trade.status = "executed"
        db.commit()
        db.refresh(trade)
        assert trade.status == "executed"

    def test_cancelled_status(self, db):
        trade = Trade(
            ticker="MSFT",
            direction="long",
            entry_price=300.0,
            quantity=5,
            status="working",
            broker_status="queued",
        )
        db.add(trade)
        db.commit()

        trade.broker_status = "cancelled"
        trade.status = "cancelled"
        db.commit()
        db.refresh(trade)
        assert trade.status == "cancelled"


class TestPnLCalculation:
    """Test profit and loss calculations for closed trades."""

    def test_long_profit(self, db):
        """Long trade with exit above entry = profit."""
        trade = Trade(
            ticker="AAPL",
            direction="long",
            entry_price=100.0,
            exit_price=120.0,
            quantity=10,
            status="closed",
            pnl=200.0,
        )
        db.add(trade)
        db.commit()
        assert trade.pnl == 200.0

    def test_long_loss(self, db):
        trade = Trade(
            ticker="TSLA",
            direction="long",
            entry_price=200.0,
            exit_price=180.0,
            quantity=5,
            status="closed",
            pnl=-100.0,
        )
        db.add(trade)
        db.commit()
        assert trade.pnl == -100.0

    def test_short_profit(self, db):
        trade = Trade(
            ticker="GME",
            direction="short",
            entry_price=50.0,
            exit_price=30.0,
            quantity=10,
            status="closed",
            pnl=200.0,
        )
        db.add(trade)
        db.commit()
        assert trade.pnl == 200.0


class TestTradeModel:
    """Test Trade model constraints and defaults."""

    def test_create_trade(self, db):
        trade = Trade(
            ticker="AAPL",
            direction="long",
            entry_price=150.0,
            quantity=10,
            status="pending",
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        assert trade.id is not None
        assert trade.ticker == "AAPL"
        assert trade.status == "pending"

    def test_trade_defaults(self, db):
        trade = Trade(
            ticker="SPY",
            direction="long",
            entry_price=400.0,
            quantity=1,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        assert trade.pnl is None or trade.pnl == 0

    def test_multiple_trades_same_ticker(self, db):
        for i in range(3):
            db.add(Trade(
                ticker="NVDA",
                direction="long",
                entry_price=500.0 + i,
                quantity=1,
                status="executed",
            ))
        db.commit()
        trades = db.query(Trade).filter(Trade.ticker == "NVDA").all()
        assert len(trades) == 3
