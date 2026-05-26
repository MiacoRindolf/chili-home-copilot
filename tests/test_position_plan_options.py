from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return []


class _FakeDb:
    def query(self, _model):
        return _FakeQuery()


def _option_trade_stub(**overrides):
    base = {
        "id": 8801,
        "ticker": "SPY",
        "direction": "long",
        "entry_price": 1.25,
        "quantity": 2.0,
        "stop_loss": 0.80,
        "take_profit": 2.50,
        "entry_date": datetime.utcnow(),
        "scan_pattern_id": None,
        "related_alert_id": None,
        "sector": None,
        "trade_type": None,
        "notes": "",
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


def test_position_plan_option_context_uses_trade_premium_quote() -> None:
    from app.services.trading.position_plan_generator import _build_position_context

    trade = _option_trade_stub()
    positions = _build_position_context(
        _FakeDb(),
        [trade],
        quotes={"SPY": {"price": 729.0}},
        trade_quotes={trade.id: {"price": 1.45, "source": "robinhood_options"}},
    )

    assert len(positions) == 1
    pos = positions[0]
    assert pos["asset_type"] == "options"
    assert pos["price_domain"] == "option_premium"
    assert pos["contract_multiplier"] == 100
    assert pos["current_price"] == pytest.approx(1.45)
    assert pos["pnl_pct"] == pytest.approx(16.0)
    assert pos["option_meta"]["strike"] == 729.0


def test_position_plan_option_context_never_falls_back_to_underlying_quote() -> None:
    from app.services.trading.position_plan_generator import _build_position_context

    trade = _option_trade_stub()
    positions = _build_position_context(
        _FakeDb(),
        [trade],
        quotes={"SPY": {"price": 729.0}},
        trade_quotes={},
    )

    pos = positions[0]
    assert pos["asset_type"] == "options"
    assert pos["current_price"] is None
    assert pos["pnl_pct"] is None
