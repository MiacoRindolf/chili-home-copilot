"""Tests for Robinhood → Chili order sync and status mapping.

## DB-backed vs pure tests

The mapping / session-flag / terminal / guardrail tests are pure logic —
they touch no ORM. The ``TestSyncOrdersToDb`` and ``TestExecuteProposalStatus``
suites exercise real behavior against the ``_test`` database (seeded
``Trade`` / ``StrategyProposal`` rows) per CLAUDE.md Hard Rule 4. Broker-side
HTTP is still patched; the database is real.

Previously these suites used ``db = MagicMock()`` with
``filter.return_value.all.side_effect = [...]`` to script query responses.
That approach hid broker-sync invariants because the real SQLAlchemy
session would surface FK / constraint / flush errors the mock silently
ignored. The conversion in Phase A of the tech-debt remediation restored
integration-level coverage.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from app.services.broker_service import (
    map_rh_status,
    is_rh_terminal,
    _rh_order_session_kwargs,
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


class TestRobinhoodSessionFlags:
    def test_regular_hours_when_extended_disabled(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "chili_autotrader_allow_extended_hours", False, raising=False)
        assert _rh_order_session_kwargs() == {
            "extendedHours": False,
            "market_hours": "regular_hours",
        }

    def test_all_day_hours_when_extended_session_open(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "chili_autotrader_allow_extended_hours", True, raising=False)
        with patch(
            "app.services.trading.pattern_imminent_alerts.us_stock_session_open",
            return_value=False,
        ), patch(
            "app.services.trading.pattern_imminent_alerts.us_stock_extended_session_open",
            return_value=True,
        ):
            assert _rh_order_session_kwargs() == {
                "extendedHours": True,
                "market_hours": "all_day_hours",
            }


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
    """Test the sync_orders_to_db function against the real _test database.

    Each test seeds the specific ``Trade`` row the code path should observe.
    Only RH HTTP calls (is_connected, get_order_by_id) are patched —
    everything else runs on live ORM, so FK / constraint / flush errors
    surface rather than being swallowed by a MagicMock.
    """

    @staticmethod
    def _seed_working_trade(db, **overrides):
        """Seed a Trade in 'working' state with a broker_order_id by default."""
        from app.models.trading import Trade

        now = datetime.utcnow()
        fields: dict = dict(
            user_id=None,
            ticker="AAPL",
            direction="long",
            entry_price=150.0,
            quantity=10,
            status="working",
            broker_source="robinhood",
            broker_order_id="order-123",
            broker_status="queued",
            submitted_at=now,
            acknowledged_at=now,
            notes="Order placed from proposal #1: test thesis",
        )
        fields.update(overrides)
        trade = Trade(**fields)
        db.add(trade)
        db.commit()
        db.refresh(trade)
        return trade

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id")
    def test_filled_order_updates_trade(self, mock_get_order, mock_connected, db):
        from app.services.broker_service import sync_orders_to_db
        from app.models.trading import Trade

        trade = self._seed_working_trade(db)
        mock_get_order.return_value = {
            "state": "filled",
            "average_price": "151.50",
            "cumulative_quantity": "10.00000000",
        }

        result = sync_orders_to_db(db, user_id=None)

        db.refresh(trade)
        assert result["filled"] == 1
        # A filled Robinhood order sets broker_status = "filled". Local status
        # transitions to "open" (position now live) via normalize_robinhood_order_event.
        assert trade.broker_status == "filled"
        assert trade.filled_at is not None
        assert trade.last_broker_sync is not None
        # avg_fill_price and entry_price reflect the average fill.
        assert trade.avg_fill_price == 151.5
        assert trade.entry_price == 151.5
        # Verify persistence survived the explicit commit inside sync_orders_to_db.
        persisted = db.query(Trade).filter(Trade.id == trade.id).one()
        assert persisted.broker_status == "filled"

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id")
    def test_cancelled_order_updates_trade(self, mock_get_order, mock_connected, db):
        from app.services.broker_service import sync_orders_to_db

        trade = self._seed_working_trade(db)
        mock_get_order.return_value = {"state": "cancelled"}

        result = sync_orders_to_db(db, user_id=None)

        db.refresh(trade)
        assert result["cancelled"] == 1
        assert trade.status == "cancelled"
        assert trade.broker_status == "cancelled"

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id")
    def test_still_queued_stays_working(self, mock_get_order, mock_connected, db):
        from app.services.broker_service import sync_orders_to_db

        trade = self._seed_working_trade(db)
        mock_get_order.return_value = {"state": "queued"}

        result = sync_orders_to_db(db, user_id=None)

        db.refresh(trade)
        assert result["synced"] == 1
        assert result["filled"] == 0
        assert trade.status == "working"
        assert trade.broker_status == "queued"

    @patch("app.services.broker_service.is_connected", return_value=False)
    def test_not_connected_returns_zeros(self, mock_connected, db):
        """No broker connection: returns counters without touching the DB."""
        from app.services.broker_service import sync_orders_to_db

        # No seeded rows — verifies the early-return path.
        result = sync_orders_to_db(db, user_id=None)
        assert result == {"synced": 0, "filled": 0, "cancelled": 0, "errors": 0}

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id", return_value=None)
    def test_missing_order_counts_as_error(self, mock_get_order, mock_connected, db):
        from app.services.broker_service import sync_orders_to_db

        self._seed_working_trade(db)

        result = sync_orders_to_db(db, user_id=None)
        assert result["errors"] == 1

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id")
    def test_pending_exit_fill_closes_trade(self, mock_get_order, mock_connected, db):
        from app.services.broker_service import sync_orders_to_db

        now = datetime.utcnow()
        # Seed an open position with a pending exit order. No broker_order_id so
        # this row is picked up by the open_with_pending_exit query only.
        trade = self._seed_working_trade(
            db,
            status="open",
            broker_order_id=None,
            pending_exit_order_id="exit-123",
            pending_exit_status="working",
            pending_exit_requested_at=now,
            pending_exit_reason="pattern_exit_now",
            pending_exit_limit_price=150.5,
        )
        mock_get_order.return_value = {
            "id": "exit-123",
            "state": "filled",
            "average_price": "151.50",
            "last_transaction_at": now.isoformat() + "Z",
        }

        result = sync_orders_to_db(db, user_id=None)

        db.refresh(trade)
        assert result["filled"] == 1
        assert trade.status == "closed"
        assert trade.exit_reason == "pattern_exit_now"
        assert trade.exit_price == 151.5
        assert trade.pending_exit_order_id is None
        assert trade.pending_exit_status is None


# ── Execute proposal status ──────────────────────────────────────────


class TestExecuteProposalStatus:
    """Verify _execute_proposal sets correct statuses against the real DB.

    Risk gate is still patched (it belongs to a different test suite) but
    everything else — proposal persistence, Trade creation, status flips —
    runs on the real ORM.
    """

    @staticmethod
    def _seed_proposal(db, **overrides):
        from app.models.trading import StrategyProposal

        fields: dict = dict(
            user_id=None,
            ticker="AAPL",
            direction="long",
            entry_price=150.0,
            stop_loss=145.0,
            take_profit=160.0,
            quantity=10,
            thesis="Test thesis for the trade",
            status="approved",
            projected_profit_pct=6.0,
            projected_loss_pct=3.0,
            risk_reward_ratio=2.0,
            confidence=0.6,
            timeframe="swing",
        )
        fields.update(overrides)
        proposal = StrategyProposal(**fields)
        db.add(proposal)
        db.commit()
        db.refresh(proposal)
        return proposal

    @patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, None))
    @patch("app.services.trading.alerts._get_buying_power", return_value=10000)
    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.place_buy_order")
    @patch("app.services.trading.alerts.dispatch_alert")
    def test_limit_order_placed_sets_working(
        self, mock_alert, mock_buy, mock_conn, mock_bp, mock_risk, db,
    ):
        from app.services.trading.alerts import _execute_proposal
        from app.models.trading import StrategyProposal

        mock_buy.return_value = {
            "ok": True,
            "order_id": "order-abc",
            "state": "queued",
            "raw": {"state": "queued", "average_price": None},
        }

        proposal = self._seed_proposal(db)

        result = _execute_proposal(db, proposal, user_id=None)

        db.refresh(proposal)
        assert result["status"] == "working"
        assert proposal.status == "working"
        assert proposal.broker_order_id == "order-abc"
        assert proposal.executed_at is None
        # Trade row was created and linked.
        assert proposal.trade_id is not None
        persisted = db.query(StrategyProposal).filter(StrategyProposal.id == proposal.id).one()
        assert persisted.status == "working"

    @patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, None))
    @patch("app.services.trading.alerts._get_buying_power", return_value=10000)
    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.place_buy_order")
    @patch("app.services.trading.alerts.dispatch_alert")
    def test_market_order_instant_fill_sets_executed(
        self, mock_alert, mock_buy, mock_conn, mock_bp, mock_risk, db,
    ):
        from app.services.trading.alerts import _execute_proposal

        mock_buy.return_value = {
            "ok": True,
            "order_id": "order-xyz",
            "state": "filled",
            "raw": {"state": "filled", "average_price": "150.25"},
        }

        proposal = self._seed_proposal(db)

        result = _execute_proposal(db, proposal, user_id=None)

        db.refresh(proposal)
        assert result["status"] == "executed"
        assert proposal.status == "executed"
        assert proposal.executed_at is not None

    @patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, None))
    @patch("app.services.broker_service.is_connected", return_value=False)
    def test_no_broker_records_locally(self, mock_conn, mock_risk, db):
        from app.services.trading.alerts import _execute_proposal

        proposal = self._seed_proposal(db)

        result = _execute_proposal(db, proposal, user_id=None)

        db.refresh(proposal)
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
