from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *args, **kwargs):
        return self

    def one_or_none(self):
        return self._row


class _FakeDb:
    def __init__(self, trade):
        self.trade = trade
        self.add = MagicMock()
        self.commit = MagicMock()
        self.refresh = MagicMock()

    def query(self, model):
        return _FakeQuery(self.trade)


class _FakeAllQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeAllDb:
    def __init__(self, rows):
        self._rows = rows
        self.commit = MagicMock()
        self.rollback = MagicMock()

    def query(self, _model):
        return _FakeAllQuery(self._rows)


def _option_trade_stub():
    return SimpleNamespace(
        id=7701,
        user_id=None,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=1.0,
        entry_date=datetime.utcnow(),
        status="open",
        broker_source="robinhood",
        auto_trader_version="v1",
        tags="options",
        pending_exit_order_id=None,
        pending_exit_status=None,
        pending_exit_requested_at=None,
        pending_exit_reason=None,
        pending_exit_limit_price=None,
        tca_reference_exit_price=None,
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


def test_evaluate_all_delegates_option_trades_without_stock_stop_evaluation() -> None:
    from app.services.trading.stop_engine import evaluate_all

    trade = _option_trade_stub()

    with patch(
        "app.services.trading.stop_engine._load_recent_decisions",
        return_value={},
    ), patch(
        "app.services.trading.stop_engine.get_adaptive_cooldowns",
        return_value={},
    ), patch(
        "app.services.trading.stop_engine._fetch_market_context",
        side_effect=AssertionError("option stop engine must not fetch stock market context"),
    ), patch(
        "app.services.trading.stop_engine.evaluate_trade",
        side_effect=AssertionError("option stop engine must not run stock stop evaluation"),
    ):
        out = evaluate_all(_FakeAllDb([trade]), user_id=None)

    assert out["total_checked"] == 0
    assert out["alerts"] == []
    assert out["skipped_options"] == 1
    assert out["delegated_to_options_exit_monitor"] == [trade.id]


def test_stop_engine_auto_execute_option_uses_sell_to_close(monkeypatch) -> None:
    from app.config import settings
    from app.services.trading.stop_engine import _try_auto_execute_stop

    trade = _option_trade_stub()
    fake_db = _FakeDb(trade)
    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "opt-contract-1"}
    fake_options.get_quote.return_value = {"bid_price": "1.40", "mark_price": "1.45"}
    fake_options.place_option_sell.return_value = {
        "ok": True,
        "order_id": "opt-stop-close",
        "state": "queued",
        "raw": {"state": "queued"},
    }

    monkeypatch.setattr(settings, "chili_auto_execute_stops", True, raising=False)
    with patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        _try_auto_execute_stop(
            fake_db,
            user_id=None,
            alert={
                "trade_id": trade.id,
                "ticker": "SPY",
                "event": "STOP_HIT",
                "price": 715.0,
            },
        )

    assert trade.status == "open"
    assert trade.pending_exit_order_id == "opt-stop-close"
    assert trade.pending_exit_status == "queued"
    assert trade.pending_exit_reason == "STOP_HIT"
    assert trade.pending_exit_limit_price == pytest.approx(1.40)
    assert trade.tca_reference_exit_price == pytest.approx(1.45)
    fake_options.place_option_sell.assert_called_once_with(
        underlying="SPY",
        expiration="2026-06-19",
        strike=729.0,
        option_type="call",
        quantity=1,
        limit_price=1.40,
        position_effect="close",
    )


def test_stop_positions_option_uses_premium_quote_not_underlying(paired_client, db) -> None:
    from app.models.trading import Trade

    client, user = paired_client
    trade = Trade(
        user_id=user.id,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=0.80,
        take_profit=2.50,
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
    db.add(trade)
    db.commit()

    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "spy-729c"}
    fake_options.get_quote.return_value = {"mark_price": "1.45"}

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("stop positions must not fetch underlying spot for options"),
    ), patch(
        "app.services.trading.stop_engine._build_brain_context",
        return_value=SimpleNamespace(summary_dict=lambda: {}),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        resp = client.get("/api/trading/stops/positions")

    assert resp.status_code == 200
    row = next(x for x in resp.json()["positions"] if x["id"] == trade.id)
    assert row["asset_type"] == "options"
    assert row["current_price"] == pytest.approx(1.45)
    assert row["pnl_pct"] == pytest.approx(16.0)
