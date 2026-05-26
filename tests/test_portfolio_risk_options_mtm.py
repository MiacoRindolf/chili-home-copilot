from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, model):
        return _FakeQuery(self._rows)


def _option_trade_stub(**overrides):
    base = {
        "id": 5501,
        "user_id": None,
        "ticker": "SPY",
        "direction": "long",
        "entry_price": 1.25,
        "quantity": 2.0,
        "entry_date": datetime.utcnow(),
        "status": "open",
        "auto_trader_version": "v1",
        "management_scope": None,
        "indicator_snapshot": {
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
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_unrealized_pnl_for_options_uses_premium_mark_not_underlying_quote() -> None:
    from app.services.trading.portfolio_risk import _compute_unrealized_pnl

    trade = _option_trade_stub()
    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "opt-contract-1"}
    fake_options.get_quote.return_value = {"mark_price": "1.45"}

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option MTM must not fetch underlying spot"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        pnl = _compute_unrealized_pnl(_FakeDb([trade]), user_id=None)

    assert pnl == pytest.approx(40.0)
    fake_options.find_contract.assert_called_once_with("SPY", "2026-06-19", 729.0, "call")
    fake_options.get_quote.assert_called_once_with("opt-contract-1")


def test_unrealized_pnl_for_options_skips_when_no_premium_quote() -> None:
    from app.services.trading.portfolio_risk import _compute_unrealized_pnl

    trade = _option_trade_stub()
    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "opt-contract-1"}
    fake_options.get_quote.return_value = {}

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option MTM must not fall back to underlying spot"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        pnl = _compute_unrealized_pnl(_FakeDb([trade]), user_id=None)

    assert pnl == 0.0
