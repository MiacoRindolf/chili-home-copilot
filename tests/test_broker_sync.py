"""Tests for Robinhood → Chili order sync and status mapping."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.services.broker_service import (
    map_rh_status,
    is_rh_terminal,
    _RH_TO_CHILI_STATUS,
)


# ── Status mapping ────────────────────────────────────────────────────


class TestStatusMapping:
    """Verify that Robinhood states map to the correct Chili statuses."""

    def test_queued_maps_to_working(self):
        assert map_rh_status("queued") == "working"

    def test_confirmed_maps_to_working(self):
        assert map_rh_status("confirmed") == "working"

    def test_unconfirmed_maps_to_working(self):
        assert map_rh_status("unconfirmed") == "working"

    def test_partially_filled_maps_to_working(self):
        assert map_rh_status("partially_filled") == "working"

    def test_filled_maps_to_open(self):
        assert map_rh_status("filled") == "open"

    def test_cancelled_maps_to_cancelled(self):
        assert map_rh_status("cancelled") == "cancelled"

    def test_canceled_alternate_spelling(self):
        assert map_rh_status("canceled") == "cancelled"

    def test_rejected_maps_to_rejected(self):
        assert map_rh_status("rejected") == "rejected"

    def test_failed_maps_to_rejected(self):
        assert map_rh_status("failed") == "rejected"

    def test_none_defaults_to_working(self):
        assert map_rh_status(None) == "working"

    def test_unknown_state_defaults_to_working(self):
        assert map_rh_status("some_new_state") == "working"

    def test_case_insensitive(self):
        assert map_rh_status("FILLED") == "open"
        assert map_rh_status("Queued") == "working"


class TestIsRhTerminal:
    def test_filled_is_terminal(self):
        assert is_rh_terminal("filled") is True

    def test_cancelled_is_terminal(self):
        assert is_rh_terminal("cancelled") is True

    def test_rejected_is_terminal(self):
        assert is_rh_terminal("rejected") is True

    def test_queued_is_not_terminal(self):
        assert is_rh_terminal("queued") is False

    def test_confirmed_is_not_terminal(self):
        assert is_rh_terminal("confirmed") is False

    def test_none_is_not_terminal(self):
        assert is_rh_terminal(None) is False


# ── Order sync ────────────────────────────────────────────────────────


class TestSyncOrdersToDb:
    """Test the sync_orders_to_db function with mocked DB and RH API."""

    def _make_trade(self, **overrides):
        trade = MagicMock()
        trade.user_id = None
        trade.ticker = "AAPL"
        trade.broker_source = "robinhood"
        trade.broker_order_id = "order-123"
        trade.status = "working"
        trade.broker_status = "queued"
        trade.last_broker_sync = None
        trade.filled_at = None
        trade.avg_fill_price = None
        trade.entry_price = 150.0
        trade.quantity = 10
        trade.notes = "Order placed from proposal #1: test thesis"
        for k, v in overrides.items():
            setattr(trade, k, v)
        return trade

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id")
    def test_filled_order_updates_trade(self, mock_get_order, mock_connected):
        from app.services.broker_service import sync_orders_to_db

        trade = self._make_trade()
        mock_get_order.return_value = {
            "state": "filled",
            "average_price": "151.50",
            "cumulative_quantity": "10.00000000",
        }

        db = MagicMock()
        filter_mock = db.query.return_value.filter.return_value
        filter_mock.all.side_effect = [[trade], []]

        result = sync_orders_to_db(db, user_id=None)

        assert result["filled"] == 1
        assert trade.status == "open"
        assert trade.broker_status == "filled"
        assert trade.avg_fill_price == 151.5
        assert trade.entry_price == 151.5
        assert trade.filled_at is not None
        db.commit.assert_called()

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id")
    def test_cancelled_order_updates_trade(self, mock_get_order, mock_connected):
        from app.services.broker_service import sync_orders_to_db

        trade = self._make_trade()
        mock_get_order.return_value = {"state": "cancelled"}

        db = MagicMock()
        # First .all() call = working trades, second = open-with-order-id reconciliation
        filter_mock = db.query.return_value.filter.return_value
        filter_mock.all.side_effect = [[trade], []]

        result = sync_orders_to_db(db, user_id=None)

        assert result["cancelled"] == 1
        assert trade.status == "cancelled"
        assert trade.broker_status == "cancelled"

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id")
    def test_still_queued_stays_working(self, mock_get_order, mock_connected):
        from app.services.broker_service import sync_orders_to_db

        trade = self._make_trade()
        mock_get_order.return_value = {"state": "queued"}

        db = MagicMock()
        filter_mock = db.query.return_value.filter.return_value
        filter_mock.all.side_effect = [[trade], []]

        result = sync_orders_to_db(db, user_id=None)

        assert result["synced"] == 1
        assert result["filled"] == 0
        assert trade.status == "working"
        assert trade.broker_status == "queued"

    @patch("app.services.broker_service.is_connected", return_value=False)
    def test_not_connected_returns_zeros(self, mock_connected):
        from app.services.broker_service import sync_orders_to_db

        db = MagicMock()
        result = sync_orders_to_db(db, user_id=None)
        assert result == {"synced": 0, "filled": 0, "cancelled": 0, "errors": 0}

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id", return_value=None)
    def test_missing_order_counts_as_error(self, mock_get_order, mock_connected):
        from app.services.broker_service import sync_orders_to_db

        trade = self._make_trade()
        db = MagicMock()
        filter_mock = db.query.return_value.filter.return_value
        filter_mock.all.side_effect = [[trade], []]

        result = sync_orders_to_db(db, user_id=None)
        assert result["errors"] == 1


# ── Execute proposal status ──────────────────────────────────────────


class TestExecuteProposalStatus:
    """Verify _execute_proposal sets correct statuses."""

    def _make_proposal(self):
        p = MagicMock()
        p.id = 1
        p.ticker = "AAPL"
        p.direction = "long"
        p.entry_price = 150.0
        p.stop_loss = 145.0
        p.take_profit = 160.0
        p.quantity = 10
        p.thesis = "Test thesis for the trade"
        p.signals_json = None
        p.status = "approved"
        p.broker_order_id = None
        p.trade_id = None
        p.executed_at = None
        return p

    @patch("app.services.trading.alerts._get_buying_power", return_value=10000)
    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.place_buy_order")
    @patch("app.services.trading.alerts.dispatch_alert")
    def test_limit_order_placed_sets_working(self, mock_alert, mock_buy, mock_conn, mock_bp):
        from app.services.trading.alerts import _execute_proposal

        mock_buy.return_value = {
            "ok": True,
            "order_id": "order-abc",
            "state": "queued",
            "raw": {"state": "queued", "average_price": None},
        }

        proposal = self._make_proposal()
        db = MagicMock()
        db.flush = MagicMock()
        db.add = MagicMock()

        result = _execute_proposal(db, proposal, user_id=None)

        assert result["status"] == "working"
        assert proposal.status == "working"
        assert proposal.broker_order_id == "order-abc"
        assert proposal.executed_at is None

    @patch("app.services.trading.alerts._get_buying_power", return_value=10000)
    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.place_buy_order")
    @patch("app.services.trading.alerts.dispatch_alert")
    def test_market_order_instant_fill_sets_executed(self, mock_alert, mock_buy, mock_conn, mock_bp):
        from app.services.trading.alerts import _execute_proposal

        mock_buy.return_value = {
            "ok": True,
            "order_id": "order-xyz",
            "state": "filled",
            "raw": {"state": "filled", "average_price": "150.25"},
        }

        proposal = self._make_proposal()
        db = MagicMock()
        db.flush = MagicMock()
        db.add = MagicMock()

        result = _execute_proposal(db, proposal, user_id=None)

        assert result["status"] == "executed"
        assert proposal.status == "executed"
        assert proposal.executed_at is not None

    @patch("app.services.broker_service.is_connected", return_value=False)
    def test_no_broker_records_locally(self, mock_conn):
        from app.services.trading.alerts import _execute_proposal

        proposal = self._make_proposal()
        db = MagicMock()
        db.flush = MagicMock()
        db.add = MagicMock()

        result = _execute_proposal(db, proposal, user_id=None)

        assert result["status"] == "recorded"
        assert proposal.status == "executed"


# ── Guardrails ────────────────────────────────────────────────────────


class TestBrokerGuardrails:
    """Verify graceful handling when robin_stocks isn't available."""

    @patch("app.services.broker_service._rh_available", False)
    def test_is_connected_returns_false(self):
        from app.services.broker_service import is_connected
        assert is_connected() is False

    @patch("app.services.broker_service._rh_available", False)
    def test_place_buy_returns_error(self):
        from app.services.broker_service import place_buy_order
        result = place_buy_order("AAPL", 1)
        assert result["ok"] is False
        assert "not installed" in result["error"]

    @patch("app.services.broker_service._rh_available", False)
    def test_place_sell_returns_error(self):
        from app.services.broker_service import place_sell_order
        result = place_sell_order("AAPL", 1)
        assert result["ok"] is False
        assert "not installed" in result["error"]

    @patch("app.services.broker_service._rh_available", False)
    def test_login_step1_returns_error(self):
        from app.services.broker_service import login_step1_sms
        result = login_step1_sms()
        assert result["status"] == "error"
        assert "not installed" in result["message"]

    @patch("app.services.broker_service._rh_available", False)
    def test_connection_status_shows_unavailable(self):
        from app.services.broker_service import get_connection_status
        status = get_connection_status()
        assert status["rh_available"] is False
        assert status["connected"] is False
