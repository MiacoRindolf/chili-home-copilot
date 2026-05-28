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
    assert pos["quantity"] == pytest.approx(2.0)
    assert pos["entry_value_usd"] == pytest.approx(250.0)
    assert pos["current_value_usd"] == pytest.approx(290.0)
    assert pos["unrealized_pnl_usd"] == pytest.approx(40.0)
    assert pos["max_premium_at_risk_usd"] == pytest.approx(250.0)
    assert pos["quote_source"] == "robinhood_options"
    assert pos["option_meta"]["strike"] == 729.0


def test_position_plan_option_context_separates_underlying_levels_from_premium_quote() -> None:
    from app.services.trading.position_plan_generator import _build_position_context

    trade = _option_trade_stub(
        stop_loss=700.0,
        take_profit=750.0,
        indicator_snapshot={
            "asset_type": "options",
            "option_meta": {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
            },
            "price_domains": {
                "entry_price": "option_premium",
                "current_price": "option_premium",
                "stop_loss": "underlying_spot",
                "take_profit": "underlying_spot",
            },
        },
    )
    positions = _build_position_context(
        _FakeDb(),
        [trade],
        quotes={"SPY": {"price": 729.0}},
        trade_quotes={trade.id: {"price": 1.45, "source": "robinhood_options"}},
    )

    pos = positions[0]
    assert pos["current_price"] == pytest.approx(1.45)
    assert pos["stop_loss"] is None
    assert pos["take_profit"] is None
    assert pos["underlying_stop_loss"] == pytest.approx(700.0)
    assert pos["underlying_take_profit"] == pytest.approx(750.0)
    assert pos["price_domains"] == {
        "entry_price": "option_premium",
        "current_price": "option_premium",
        "stop_loss": "underlying_spot",
        "take_profit": "underlying_spot",
    }


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
    assert pos["untrusted_stop_loss"] == pytest.approx(0.80)
    assert pos["stop_loss"] is None


def test_position_plan_option_context_does_not_default_bad_quantity_to_one() -> None:
    from app.services.trading.position_plan_generator import _build_position_context

    trade = _option_trade_stub(quantity=1.5)
    positions = _build_position_context(
        _FakeDb(),
        [trade],
        quotes={},
        trade_quotes={trade.id: {"price": 1.45, "source": "robinhood_options"}},
    )

    pos = positions[0]
    assert pos["asset_type"] == "options"
    assert pos["quantity"] is None
    assert pos["quantity_error"] == "invalid_option_contract_quantity"


def test_position_plan_llm_call_opts_into_cache_and_singleflight(monkeypatch) -> None:
    from app.services.trading import position_plan_generator as ppg

    captured = {}

    def fake_call_llm(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        return '{"portfolio_summary":{},"position_plans":[]}'

    monkeypatch.setattr(ppg, "call_llm", fake_call_llm)

    raw = ppg._call_position_plan_llm(
        [{"role": "user", "content": '{"portfolio":{},"positions":[]}'}],
        max_tokens=900,
        system_prompt="system",
    )

    assert raw.startswith("{")
    assert captured["cacheable"] is True
    assert captured["purpose"] == "position_plan_generator"
    assert captured["trace_id"] == "position-plan-generator"
    assert captured["max_tokens"] == 900
    assert captured["system_prompt"] == "system"
