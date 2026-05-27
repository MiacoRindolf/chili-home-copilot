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
from types import SimpleNamespace
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


class TestRobinhoodOptionOrderRouting:
    @staticmethod
    def _fake_robinhood_options_module(monkeypatch, *, state: str = "unconfirmed"):
        import sys
        import types

        root = types.ModuleType("robin_stocks")
        root.__path__ = []
        rh = types.ModuleType("robin_stocks.robinhood")
        rh.orders = SimpleNamespace(
            order_option_debit_spread=lambda **_kwargs: {"id": "opt-spread-1", "state": state},
            order_option_credit_spread=lambda **_kwargs: {"id": "opt-spread-1", "state": state},
        )
        root.robinhood = rh
        monkeypatch.setitem(sys.modules, "robin_stocks", root)
        monkeypatch.setitem(sys.modules, "robin_stocks.robinhood", rh)

    def test_option_order_envelope_is_self_describing(self):
        from app.services.broker_service import _normalize_option_order_envelope

        out = _normalize_option_order_envelope(
            {"id": "opt-buy-1", "state": "filled", "average_price": "1.25"},
            action="buy",
            underlying="SPY",
            expiration="2026-06-19",
            strike=729.0,
            option_type="call",
            quantity=2,
            limit_price=1.25,
            position_effect="open",
        )

        assert out["ok"] is True
        assert out["order_id"] == "opt-buy-1"
        assert out["state"] == "filled"
        assert out["status"] == "filled"
        assert out["side"] == "buy"
        assert out["position_effect"] == "open"
        assert out["underlying"] == "SPY"
        assert out["quantity"] == 2
        assert out["base_size"] == 2
        assert out["limit_price"] == 1.25
        assert out["average_price"] == "1.25"

    def test_verify_option_order_landed_uses_option_endpoint(self, monkeypatch):
        from app.services import broker_service

        calls: list[str] = []

        def _option_order(order_id):
            calls.append(order_id)
            return {"state": "cancelled"}

        monkeypatch.setattr(broker_service, "get_option_order_by_id", _option_order)

        verdict, observed = broker_service.verify_option_order_landed(
            "opt-post-submit",
            max_wait_s=1.0,
            poll_interval_s=0.0,
        )

        assert (verdict, observed) == ("rejected", "cancelled")
        assert calls == ["opt-post-submit"]

    def test_verify_option_order_landed_preserves_terminal_fill(self, monkeypatch):
        from app.services import broker_service

        monkeypatch.setattr(
            broker_service,
            "get_option_order_by_id",
            lambda _order_id: {"state": "cancelled", "processed_quantity": "1"},
        )

        verdict, observed = broker_service.verify_option_order_landed(
            "opt-post-submit",
            max_wait_s=1.0,
            poll_interval_s=0.0,
        )

        assert (verdict, observed) == ("executed", "cancelled")

    def test_option_submit_verifier_rejects_post_accept_cancel(self, monkeypatch):
        from app.services import broker_service

        monkeypatch.setattr(
            broker_service,
            "verify_option_order_landed",
            lambda _order_id: ("rejected", "cancelled"),
        )

        updated, rejected = broker_service._verify_submitted_option_order(
            {"id": "opt-buy-1", "state": "unconfirmed"},
            order_id="opt-buy-1",
            label="BUY-OPT SPY",
        )

        assert updated["state"] == "unconfirmed"
        assert rejected is not None
        assert rejected["ok"] is False
        assert rejected["error"] == "option_order_cancelled"
        assert rejected["order_id"] == "opt-buy-1"

    def test_option_submit_verifier_preserves_terminal_fill(self, monkeypatch):
        from app.services import broker_service

        updated, rejected = broker_service._verify_submitted_option_order(
            {
                "id": "opt-buy-1",
                "state": "cancelled",
                "processed_quantity": "1",
            },
            order_id="opt-buy-1",
            label="BUY-OPT SPY",
        )

        assert rejected is None
        assert updated["state"] == "cancelled"
        assert updated["processed_quantity"] == "1"

    def test_option_submit_verifier_promotes_resting_state(self, monkeypatch):
        from app.services import broker_service

        monkeypatch.setattr(
            broker_service,
            "verify_option_order_landed",
            lambda _order_id: ("resting", "queued"),
        )

        updated, rejected = broker_service._verify_submitted_option_order(
            {"id": "opt-buy-1", "state": "unconfirmed"},
            order_id="opt-buy-1",
            label="BUY-OPT SPY",
        )

        assert rejected is None
        assert updated["state"] == "queued"

    def test_option_submit_verifier_promotes_verified_terminal_fill(self, monkeypatch):
        from app.services import broker_service

        monkeypatch.setattr(
            broker_service,
            "verify_option_order_landed",
            lambda _order_id: ("executed", "cancelled"),
        )

        updated, rejected = broker_service._verify_submitted_option_order(
            {"id": "opt-buy-1", "state": "unconfirmed"},
            order_id="opt-buy-1",
            label="BUY-OPT SPY",
        )

        assert rejected is None
        assert updated["state"] == "cancelled"

    def test_option_spread_rejects_post_accept_cancel(self, monkeypatch):
        from app.services import broker_service

        self._fake_robinhood_options_module(monkeypatch)
        monkeypatch.setattr(broker_service, "_rh_available", True)
        monkeypatch.setattr(broker_service, "is_connected", lambda: True)
        monkeypatch.setattr(
            broker_service,
            "_retry_api_call",
            lambda fn, *, label: fn(),
        )
        monkeypatch.setattr(
            broker_service,
            "_verify_submitted_option_order",
            lambda result, *, order_id, label: (
                result,
                {
                    "ok": False,
                    "error": "option_order_cancelled",
                    "order_id": order_id,
                    "state": "cancelled",
                    "raw": result,
                },
            ),
        )

        out = broker_service.place_option_spread(
            legs=[
                {
                    "expiration": "2026-06-19",
                    "strike": 729.0,
                    "option_type": "call",
                    "action": "buy",
                },
                {
                    "expiration": "2026-06-19",
                    "strike": 735.0,
                    "option_type": "call",
                    "action": "sell",
                },
            ],
            underlying="SPY",
            quantity=1,
            limit_price=1.25,
            direction="debit",
        )

        assert out["ok"] is False
        assert out["error"] == "option_order_cancelled"
        assert out["order_id"] == "opt-spread-1"

    def test_option_spread_promotes_verified_resting_state(self, monkeypatch):
        from app.services import broker_service

        self._fake_robinhood_options_module(monkeypatch)
        monkeypatch.setattr(broker_service, "_rh_available", True)
        monkeypatch.setattr(broker_service, "is_connected", lambda: True)
        monkeypatch.setattr(
            broker_service,
            "_retry_api_call",
            lambda fn, *, label: fn(),
        )

        def _verify(result, *, order_id, label):
            updated = dict(result)
            updated["state"] = "queued"
            return updated, None

        monkeypatch.setattr(broker_service, "_verify_submitted_option_order", _verify)

        out = broker_service.place_option_spread(
            legs=[
                {
                    "expiration": "2026-06-19",
                    "strike": 729.0,
                    "option_type": "call",
                    "action": "buy",
                },
                {
                    "expiration": "2026-06-19",
                    "strike": 735.0,
                    "option_type": "call",
                    "action": "sell",
                },
            ],
            underlying="SPY",
            quantity=1,
            limit_price=1.25,
            direction="debit",
        )

        assert out["ok"] is True
        assert out["order_id"] == "opt-spread-1"
        assert out["state"] == "queued"
        assert out["raw"]["state"] == "queued"

    @patch("app.services.broker_service.get_order_by_id")
    @patch("app.services.broker_service.get_option_order_by_id")
    def test_option_trade_uses_option_order_lookup(self, mock_option_order, mock_stock_order):
        from app.services.broker_service import _robinhood_order_lookup_for_trade

        trade = SimpleNamespace(
            indicator_snapshot={"breakout_alert": {"asset_type": "options"}},
        )
        mock_option_order.return_value = {"id": "opt-1", "state": "filled"}

        out = _robinhood_order_lookup_for_trade(trade, "opt-1")

        assert out == {"id": "opt-1", "state": "filled"}
        mock_option_order.assert_called_once_with("opt-1")
        mock_stock_order.assert_not_called()

    @patch("app.services.broker_service.get_order_by_id")
    @patch("app.services.broker_service.get_option_order_by_id")
    def test_stock_trade_uses_stock_order_lookup(self, mock_option_order, mock_stock_order):
        from app.services.broker_service import _robinhood_order_lookup_for_trade

        trade = SimpleNamespace(indicator_snapshot={})
        mock_stock_order.return_value = {"id": "stock-1", "state": "filled"}

        out = _robinhood_order_lookup_for_trade(trade, "stock-1")

        assert out == {"id": "stock-1", "state": "filled"}
        mock_stock_order.assert_called_once_with("stock-1")
        mock_option_order.assert_not_called()


class TestRobinhoodExitFinalizer:
    def test_option_exit_pnl_uses_contract_multiplier(self):
        from app.services.trading.robinhood_exit_execution import _finalize_filled_exit

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return []

        class _Db:
            def add(self, *_args, **_kwargs):
                return None

            def commit(self):
                return None

            def execute(self, *_args, **_kwargs):
                return _Result()

        trade = SimpleNamespace(
            id=4321,
            user_id=None,
            ticker="SPY",
            direction="long",
            entry_price=1.25,
            quantity=1,
            status="open",
            broker_source="robinhood",
            broker_status="submitted",
            pending_exit_order_id="opt-exit-1",
            pending_exit_status="submitted",
            pending_exit_requested_at=datetime.utcnow(),
            pending_exit_reason="options_premium_take_profit",
            pending_exit_limit_price=1.45,
            scan_pattern_id=None,
            indicator_snapshot={"breakout_alert": {"asset_type": "options"}},
        )

        pnl = _finalize_filled_exit(
            _Db(),
            trade,
            raw_order={"id": "opt-exit-1", "state": "filled", "average_price": "1.45"},
            exit_reason="options_premium_take_profit",
            fallback_price=None,
            filled_at=datetime.utcnow(),
        )

        assert pnl == 20.0
        assert trade.status == "closed"
        assert trade.exit_price == 1.45
        assert trade.pnl == 20.0
        assert trade.pending_exit_order_id is None


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
    @patch("app.services.broker_service.get_option_order_by_id")
    def test_option_working_entry_syncs_from_option_order(
        self,
        mock_option_order,
        mock_stock_order,
        mock_connected,
        db,
    ):
        from app.services.broker_service import sync_orders_to_db

        trade = self._seed_working_trade(
            db,
            ticker="SPY",
            entry_price=1.25,
            quantity=2,
            broker_order_id="opt-entry-1",
            asset_kind="option",
            indicator_snapshot={
                "breakout_alert": {
                    "asset_type": "options",
                    "option_meta": {
                        "underlying": "SPY",
                        "expiration": "2026-06-19",
                        "strike": 729.0,
                        "option_type": "call",
                    },
                }
            },
        )
        mock_option_order.return_value = {
            "id": "opt-entry-1",
            "state": "filled",
            "quantity": "2",
            "processed_quantity": "2",
            "average_price": "1.30",
        }

        result = sync_orders_to_db(db, user_id=None)

        db.refresh(trade)
        assert result["filled"] == 1
        assert trade.status == "open"
        assert trade.broker_status == "filled"
        assert trade.entry_price == 1.30
        assert trade.avg_fill_price == 1.30
        assert trade.filled_quantity == 2
        assert trade.remaining_quantity == 0
        mock_option_order.assert_called_once_with("opt-entry-1")
        mock_stock_order.assert_not_called()

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id")
    @patch("app.services.broker_service.get_option_order_by_id")
    def test_option_cancelled_entry_with_fill_keeps_only_filled_contracts(
        self,
        mock_option_order,
        mock_stock_order,
        mock_connected,
        db,
    ):
        from app.services.broker_service import sync_orders_to_db

        trade = self._seed_working_trade(
            db,
            ticker="SPY",
            entry_price=1.25,
            quantity=2,
            broker_order_id="opt-entry-partial",
            asset_kind="option",
            indicator_snapshot={
                "breakout_alert": {
                    "asset_type": "options",
                    "option_meta": {
                        "underlying": "SPY",
                        "expiration": "2026-06-19",
                        "strike": 729.0,
                        "option_type": "call",
                    },
                },
                "entry_execution": {"active_order_type": "option_limit"},
            },
        )
        mock_option_order.return_value = {
            "id": "opt-entry-partial",
            "state": "cancelled",
            "quantity": "2",
            "processed_quantity": "1",
            "average_price": "1.30",
        }

        result = sync_orders_to_db(db, user_id=None)

        db.refresh(trade)
        assert result["filled"] == 1
        assert trade.status == "open"
        assert trade.broker_status == "partially_filled_cancelled"
        assert trade.quantity == 1
        assert trade.filled_quantity == 1
        assert trade.remaining_quantity == 0
        assert trade.entry_price == 1.30
        assert trade.avg_fill_price == 1.30
        entry = trade.indicator_snapshot["entry_execution"]
        assert entry["option_position_partial"] is True
        assert entry["option_position_requested_quantity"] == 2.0
        assert entry["option_position_quantity"] == 1.0
        assert entry["option_entry_cancel_reason"] == "partial_entry_cancelled_by_broker"
        mock_option_order.assert_called_once_with("opt-entry-partial")
        mock_stock_order.assert_not_called()

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


    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id")
    @patch("app.services.broker_service.get_option_order_by_id")
    def test_option_pending_exit_cancelled_with_full_fill_closes_trade(
        self,
        mock_option_order,
        mock_stock_order,
        mock_connected,
        db,
    ):
        from app.services.broker_service import sync_orders_to_db

        now = datetime.utcnow()
        trade = self._seed_working_trade(
            db,
            ticker="SPY",
            entry_price=1.25,
            quantity=1,
            status="open",
            broker_order_id=None,
            pending_exit_order_id="opt-exit-full-fill",
            pending_exit_status="working",
            pending_exit_requested_at=now,
            pending_exit_reason="options_premium_take_profit",
            pending_exit_limit_price=1.40,
            asset_kind="option",
            indicator_snapshot={"breakout_alert": {"asset_type": "options"}},
        )
        mock_option_order.return_value = {
            "id": "opt-exit-full-fill",
            "state": "cancelled",
            "quantity": "1",
            "processed_quantity": "1",
            "average_price": "1.45",
            "last_transaction_at": now.isoformat() + "Z",
        }

        result = sync_orders_to_db(db, user_id=None)

        db.refresh(trade)
        assert result["filled"] == 1
        assert trade.status == "closed"
        assert trade.exit_reason == "options_premium_take_profit"
        assert trade.exit_price == 1.45
        assert trade.pnl == 20.0
        assert trade.pending_exit_order_id is None
        assert trade.pending_exit_status is None
        mock_option_order.assert_called_once_with("opt-exit-full-fill")
        mock_stock_order.assert_not_called()

    @patch("app.services.broker_service.is_connected", return_value=True)
    @patch("app.services.broker_service.get_order_by_id")
    @patch("app.services.broker_service.get_option_order_by_id")
    def test_option_pending_exit_cancelled_with_partial_fill_reduces_open_contracts(
        self,
        mock_option_order,
        mock_stock_order,
        mock_connected,
        db,
    ):
        from app.models.trading import TradingExecutionEvent
        from app.services.broker_service import sync_orders_to_db

        now = datetime.utcnow()
        trade = self._seed_working_trade(
            db,
            ticker="SPY",
            entry_price=1.25,
            avg_fill_price=1.25,
            quantity=2,
            filled_quantity=2,
            remaining_quantity=0,
            status="open",
            broker_order_id=None,
            pending_exit_order_id="opt-exit-partial-fill",
            pending_exit_status="working",
            pending_exit_requested_at=now,
            pending_exit_reason="options_premium_take_profit",
            pending_exit_limit_price=1.40,
            asset_kind="option",
            indicator_snapshot={"breakout_alert": {"asset_type": "options"}},
        )
        mock_option_order.return_value = {
            "id": "opt-exit-partial-fill",
            "state": "cancelled",
            "quantity": "2",
            "processed_quantity": "1",
            "average_price": "1.45",
            "last_transaction_at": now.isoformat() + "Z",
        }

        result = sync_orders_to_db(db, user_id=None)

        db.refresh(trade)
        assert result["synced"] == 1
        assert result["filled"] == 0
        assert result["cancelled"] == 0
        assert trade.status == "open"
        assert trade.broker_status == "partially_filled_cancelled"
        assert trade.quantity == 1
        assert trade.filled_quantity == 1
        assert trade.remaining_quantity == 0
        assert trade.entry_price == 1.25
        assert trade.avg_fill_price == 1.25
        assert trade.pending_exit_order_id is None
        assert trade.pending_exit_status is None
        exit_execution = trade.indicator_snapshot["exit_execution"]
        assert exit_execution["option_exit_partial"] is True
        assert exit_execution["partial_exit_requested_quantity"] == 2.0
        assert exit_execution["partial_exit_filled_quantity"] == 1.0
        assert exit_execution["partial_exit_remaining_quantity"] == 1.0
        assert exit_execution["partial_exit_price"] == 1.45
        assert exit_execution["partial_exit_pnl"] == 20.0
        event = (
            db.query(TradingExecutionEvent)
            .filter(TradingExecutionEvent.trade_id == trade.id)
            .filter(TradingExecutionEvent.order_id == "opt-exit-partial-fill")
            .one()
        )
        assert event.event_type == "partial_fill"
        assert event.status == "partially_filled"
        assert event.average_fill_price == 1.45
        mock_option_order.assert_called_once_with("opt-exit-partial-fill")
        mock_stock_order.assert_not_called()


# ── Execute proposal status ──────────────────────────────────────────

class TestCoinbaseSyncOrdersToDb:
    """Coinbase order sync should also finalize monitor-submitted exits."""

    @staticmethod
    def _seed_open_coinbase_trade(db, **overrides):
        from app.models.trading import Trade

        now = datetime.utcnow()
        fields: dict = dict(
            user_id=None,
            ticker="ADA-USD",
            direction="long",
            entry_price=10.0,
            quantity=3.0,
            status="open",
            broker_source="coinbase",
            pending_exit_order_id="cb-exit-1",
            pending_exit_status="submitted",
            pending_exit_requested_at=now,
            pending_exit_reason="stop_loss_hit",
            pending_exit_limit_price=8.5,
        )
        fields.update(overrides)
        trade = Trade(**fields)
        db.add(trade)
        db.commit()
        db.refresh(trade)
        return trade

    @patch("app.services.trading.auto_trader_position_overrides.clear_position_overrides")
    @patch("app.services.trading.brain_work.execution_hooks.on_live_trade_closed")
    @patch("app.services.coinbase_service.is_connected", return_value=True)
    @patch("app.services.coinbase_service.get_order_by_id")
    def test_pending_exit_fill_closes_coinbase_trade(
        self, mock_get_order, mock_connected, mock_closed_hook, mock_clear_overrides, db,
    ):
        from app.services.coinbase_service import sync_orders_to_db

        now = datetime.utcnow()
        trade = self._seed_open_coinbase_trade(db)
        mock_get_order.return_value = {
            "order_id": "cb-exit-1",
            "status": "FILLED",
            "product_id": "ADA-USD",
            "base_size": "3.0",
            "filled_size": "3.0",
            "average_filled_price": "8.50",
            "completion_time": now.isoformat() + "Z",
        }

        result = sync_orders_to_db(db, user_id=None)

        db.refresh(trade)
        assert result["filled"] == 1
        assert trade.status == "closed"
        assert trade.exit_reason == "stop_loss_hit"
        assert trade.exit_price == 8.5
        assert trade.quantity == 3.0
        assert trade.pnl == -4.5
        assert trade.pending_exit_order_id is None
        assert trade.pending_exit_status is None
        mock_closed_hook.assert_called_once()
        mock_clear_overrides.assert_called_once()


class TestCoinbaseOrderLookup:
    def test_get_order_by_id_unwraps_sdk_order_object(self):
        from app.services.coinbase_service import get_order_by_id

        class OrderObj:
            def __init__(self):
                self.order_id = "cb-filled-1"
                self.status = "FILLED"
                self.filled_size = "3"
                self.average_filled_price = "8.50"

        class ResponseObj:
            def __init__(self):
                self.order = OrderObj()

        class Client:
            def get_order(self, order_id):
                assert order_id == "cb-filled-1"
                return ResponseObj()

        with patch("app.services.coinbase_service._get_client", return_value=Client()):
            order = get_order_by_id("cb-filled-1")

        assert order == {
            "order_id": "cb-filled-1",
            "status": "FILLED",
            "filled_size": "3",
            "average_filled_price": "8.50",
        }


class TestCoinbasePositionSync:
    @patch("app.services.coinbase_service.collapse_open_broker_position_duplicates")
    @patch("app.services.coinbase_service.acquire_broker_position_sync_lock")
    @patch("app.services.coinbase_service.is_connected", return_value=True)
    @patch("app.services.coinbase_service.get_positions")
    def test_zero_cost_basis_position_is_not_auto_created(
        self, mock_positions, mock_connected, mock_lock, mock_collapse, db,
    ):
        from app.models.trading import Trade
        from app.services.coinbase_service import sync_positions_to_db

        mock_positions.return_value = [
            {
                "ticker": "AMP-USD",
                "quantity": 10.0,
                "average_buy_price": 0.0,
            }
        ]
        mock_collapse.return_value = {"cancelled": 0}

        result = sync_positions_to_db(db, user_id=None)

        assert result["created"] == 0
        assert db.query(Trade).filter(Trade.ticker == "AMP-USD").count() == 0


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
