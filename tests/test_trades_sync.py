"""Tests for trades sync (manual cleanup, partial sell, sync pipeline)."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.services.broker_service import cleanup_manual_trades


# ── Manual cleanup ─────────────────────────────────────────────────────

def _make_trade(**overrides):
    """Build a mock Trade object with sensible defaults."""
    t = MagicMock()
    t.id = overrides.get("id", 1)
    t.ticker = overrides.get("ticker", "ALM")
    t.status = overrides.get("status", "open")
    t.broker_source = overrides.get("broker_source", None)
    t.broker_order_id = overrides.get("broker_order_id", None)
    t.user_id = overrides.get("user_id", None)
    t.notes = overrides.get("notes", "")
    t.exit_reason = overrides.get("exit_reason", None)
    t.entry_price = overrides.get("entry_price", 10.0)
    t.exit_price = overrides.get("exit_price", None)
    t.quantity = overrides.get("quantity", 1.0)
    t.direction = overrides.get("direction", "long")
    t.pnl = overrides.get("pnl", None)
    t.indicator_snapshot = overrides.get("indicator_snapshot", {})
    t.exit_date = None
    return t


class TestCleanupManualTrades:
    """Verify cleanup_manual_trades auto-closes manual trades not on RH."""

    def test_manual_trade_not_in_rh_gets_closed(self):
        alm = _make_trade(ticker="ALM", broker_source=None, broker_order_id=None)
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [alm]

        result = cleanup_manual_trades(db, user_id=None, live_tickers={"BNAI", "STLA"})

        assert result["closed_manual"] == 1
        assert alm.status == "closed"
        assert alm.exit_date is not None
        assert "Auto-closed during RH sync" in alm.notes
        db.commit.assert_called()

    def test_manual_trade_with_matching_rh_position_untouched(self):
        alm = _make_trade(ticker="ALM", broker_source=None, broker_order_id=None)
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [alm]

        result = cleanup_manual_trades(db, user_id=None, live_tickers={"ALM", "BNAI"})

        assert result["closed_manual"] == 0
        assert alm.status == "open"
        db.commit.assert_not_called()

    def test_rh_linked_trade_not_touched(self):
        """Trades with broker_order_id should not appear in the manual query."""
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []

        result = cleanup_manual_trades(db, user_id=None, live_tickers=set())
        assert result["closed_manual"] == 0

    def test_no_open_manual_trades(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []

        result = cleanup_manual_trades(db, user_id=None, live_tickers={"BNAI"})
        assert result["closed_manual"] == 0

    def test_multiple_manual_trades_cleaned(self):
        t1 = _make_trade(ticker="ALM", broker_source="manual", broker_order_id=None)
        t2 = _make_trade(ticker="FOO", broker_source=None, broker_order_id=None)
        t3 = _make_trade(ticker="BNAI", broker_source=None, broker_order_id=None)
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [t1, t2, t3]

        result = cleanup_manual_trades(db, user_id=None, live_tickers={"BNAI"})

        assert result["closed_manual"] == 2
        assert t1.status == "closed"
        assert t2.status == "closed"
        assert t3.status == "open"


# ── sync_positions_to_db returns live_tickers ──────────────────────────

    @patch(
        "app.services.broker_service._get_exit_price",
        side_effect=AssertionError("option manual cleanup must not use underlying quote"),
    )
    def test_option_manual_trade_not_auto_closed_from_stock_position_list(self, _px):
        opt = _make_trade(
            ticker="SPY",
            broker_source="manual",
            broker_order_id=None,
            indicator_snapshot={"breakout_alert": {"asset_type": "options"}},
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [opt]

        result = cleanup_manual_trades(db, user_id=None, live_tickers=set())

        assert result["closed_manual"] == 0
        assert result["skipped_options"] == 1
        assert opt.status == "open"
        db.commit.assert_not_called()


class TestBackfillClosedTradePnl:
    @patch(
        "app.services.broker_service._get_exit_price",
        side_effect=AssertionError("option backfill must not use underlying quote"),
    )
    def test_closed_option_without_exit_price_is_skipped(self, _px):
        from app.services.broker_service import backfill_closed_trade_pnl

        opt = _make_trade(
            ticker="SPY",
            status="closed",
            exit_price=None,
            pnl=None,
            indicator_snapshot={"breakout_alert": {"asset_type": "options"}},
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [opt]

        patched = backfill_closed_trade_pnl(db, user_id=None)

        assert patched == 0
        assert opt.exit_price is None
        assert opt.pnl is None
        db.commit.assert_not_called()

    @patch(
        "app.services.broker_service._get_exit_price",
        side_effect=AssertionError("premium exit price is already present"),
    )
    def test_closed_option_with_exit_price_uses_contract_multiplier(self, _px):
        from app.services.broker_service import backfill_closed_trade_pnl

        opt = _make_trade(
            ticker="SPY",
            status="closed",
            entry_price=1.25,
            exit_price=1.45,
            quantity=2,
            pnl=None,
            indicator_snapshot={"breakout_alert": {"asset_type": "options"}},
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [opt]

        patched = backfill_closed_trade_pnl(db, user_id=None)

        assert patched == 1
        assert opt.pnl == 40.0
        db.commit.assert_called_once()


class TestSyncPositionsReturnsLiveTickers:
    """Verify that sync_positions_to_db returns _live_tickers key."""

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_positions", return_value=[
        {"ticker": "BNAI", "quantity": 14, "average_buy_price": 37.94},
    ])
    @patch("app.services.broker_service.get_crypto_positions", return_value=[])
    @patch("app.services.broker_service._compute_trade_snapshot", return_value=None)
    def test_returns_live_tickers_set(self, _snap, _crypto, _pos, _conn):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter.return_value.all.return_value = []

        from app.services.broker_service import sync_positions_to_db
        result = sync_positions_to_db(db, user_id=None)

        assert "_live_tickers" in result
        assert "BNAI" in result["_live_tickers"]


# ── Partial sell endpoint ──────────────────────────────────────────────

class TestPartialSellEndpoint:
    """Test the sell logic at the service layer by calling the route handler."""

    def _mock_trade(self, **kw):
        t = MagicMock()
        t.id = kw.get("id", 1)
        t.ticker = kw.get("ticker", "BNAI")
        t.quantity = kw.get("quantity", 14.0)
        t.entry_price = kw.get("entry_price", 37.94)
        t.status = kw.get("status", "open")
        t.broker_source = kw.get("broker_source", "robinhood")
        t.direction = "long"
        t.exit_price = None
        t.exit_date = None
        t.pnl = None
        t.exit_reason = kw.get("exit_reason", None)
        t.notes = ""
        t.indicator_snapshot = kw.get("indicator_snapshot", {})
        return t

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.place_sell_order")
    def test_partial_sell_reduces_quantity(self, mock_sell, _conn):
        mock_sell.return_value = {"ok": True, "order_id": "abc123", "state": "queued"}
        trade = self._mock_trade(quantity=14.0)

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = trade

        from app.routers.trading import api_sell_trade
        from app.schemas.trading import TradeSell

        body = TradeSell(quantity=5.0, limit_price=40.0)
        request = MagicMock()

        with patch("app.routers.trading.get_identity_ctx", return_value={"user_id": None}):
            resp = api_sell_trade(trade_id=1, body=body, request=request, db=db)

        data = resp.body
        import json
        data = json.loads(data)
        assert data["ok"] is True
        assert data["sold_qty"] == 5.0
        assert data["remaining_qty"] == 9.0
        mock_sell.assert_called_once_with(
            ticker="BNAI", quantity=5.0, order_type="limit", limit_price=40.0,
        )

    def test_sell_more_than_held_returns_error(self):
        trade = self._mock_trade(quantity=14.0)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = trade

        from app.routers.trading import api_sell_trade
        from app.schemas.trading import TradeSell

        body = TradeSell(quantity=20.0)
        request = MagicMock()

        with patch("app.routers.trading.get_identity_ctx", return_value={"user_id": None}):
            resp = api_sell_trade(trade_id=1, body=body, request=request, db=db)

        import json
        data = json.loads(resp.body)
        assert data["ok"] is False
        assert "Cannot sell" in data["error"]

    def test_sell_closed_trade_returns_error(self):
        trade = self._mock_trade(status="closed")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = trade

        from app.routers.trading import api_sell_trade
        from app.schemas.trading import TradeSell

        body = TradeSell(quantity=1.0)
        request = MagicMock()

        with patch("app.routers.trading.get_identity_ctx", return_value={"user_id": None}):
            resp = api_sell_trade(trade_id=1, body=body, request=request, db=db)

        import json
        data = json.loads(resp.body)
        assert data["ok"] is False
        assert "closed" in data["error"]

    def test_manual_full_sell_closes_trade(self):
        trade = self._mock_trade(broker_source=None, quantity=10.0, entry_price=5.0)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = trade

        from app.routers.trading import api_sell_trade
        from app.schemas.trading import TradeSell

        body = TradeSell(quantity=10.0, limit_price=6.0)
        request = MagicMock()

        with patch("app.routers.trading.get_identity_ctx", return_value={"user_id": None}):
            resp = api_sell_trade(trade_id=1, body=body, request=request, db=db)

        import json
        data = json.loads(resp.body)
        assert data["ok"] is True
        assert trade.status == "closed"
        assert trade.exit_price == 6.0
        assert trade.pnl == 10.0

    def test_option_partial_sell_rejected_before_stock_sell(self):
        trade = self._mock_trade(
            ticker="SPY",
            broker_source="robinhood",
            quantity=2.0,
            entry_price=1.25,
            indicator_snapshot={"breakout_alert": {"asset_type": "options"}},
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = trade

        from app.routers.trading_sub.trades import api_sell_trade
        from app.schemas.trading import TradeSell

        body = TradeSell(quantity=1.0, limit_price=1.40)
        request = MagicMock()

        with patch(
            "app.routers.trading_sub.trades.get_identity_ctx",
            return_value={"user_id": None},
        ), patch(
            "app.routers.trading_sub.trades.broker_manager.place_sell_order",
            side_effect=AssertionError("option partial sell must not use stock sell"),
        ):
            resp = api_sell_trade(trade_id=1, body=body, request=request, db=db)

        import json
        data = json.loads(resp.body)
        assert resp.status_code == 400
        assert data["error"] == "option_partial_sell_not_supported"
        assert trade.status == "open"

    def test_manual_option_full_sell_uses_contract_multiplier(self):
        trade = self._mock_trade(
            ticker="SPY",
            broker_source=None,
            quantity=2.0,
            entry_price=1.25,
            indicator_snapshot={"breakout_alert": {"asset_type": "options"}},
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = trade

        from app.routers.trading_sub.trades import api_sell_trade
        from app.schemas.trading import TradeSell

        body = TradeSell(quantity=2.0, limit_price=1.45)
        request = MagicMock()

        with patch(
            "app.routers.trading_sub.trades.get_identity_ctx",
            return_value={"user_id": None},
        ), patch(
            "app.routers.trading_sub.trades.broker_manager.place_sell_order",
            side_effect=AssertionError("manual option sell must not use stock sell"),
        ):
            resp = api_sell_trade(trade_id=1, body=body, request=request, db=db)

        import json
        data = json.loads(resp.body)
        assert data["ok"] is True
        assert trade.status == "closed"
        assert trade.exit_price == 1.45
        assert trade.pnl == 40.0
        assert trade.exit_reason == "api_sell_manual_option"
