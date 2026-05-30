from __future__ import annotations

import json
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


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _MaterialCacheDb(_FakeDb):
    def __init__(self, row):
        self.row = row

    def execute(self, *_args, **_kwargs):
        return _FakeResult(self.row)


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


@pytest.mark.parametrize("bad_price", [True, float("nan"), float("inf"), 0, -1, "bad"])
def test_position_plan_option_context_rejects_bad_premium_quote(bad_price) -> None:
    from app.services.trading.position_plan_generator import _build_position_context

    trade = _option_trade_stub()
    positions = _build_position_context(
        _FakeDb(),
        [trade],
        quotes={"SPY": {"price": 729.0}},
        trade_quotes={trade.id: {"price": bad_price, "source": "robinhood_options"}},
    )

    pos = positions[0]
    assert pos["asset_type"] == "options"
    assert pos["current_price"] is None
    assert pos["pnl_pct"] is None
    assert pos["current_value_usd"] is None
    assert pos["unrealized_pnl_usd"] is None
    assert pos["quote_source"] == "robinhood_options"


@pytest.mark.parametrize("bad_entry", [True, float("nan"), float("inf"), 0, -1, "bad"])
def test_position_plan_option_context_rejects_bad_entry_price(bad_entry) -> None:
    from app.services.trading.position_plan_generator import _build_position_context

    trade = _option_trade_stub(entry_price=bad_entry)
    positions = _build_position_context(
        _FakeDb(),
        [trade],
        quotes={},
        trade_quotes={trade.id: {"price": 1.45, "source": "robinhood_options"}},
    )

    pos = positions[0]
    assert pos["asset_type"] == "options"
    assert pos["entry_price"] is None
    assert pos["pnl_pct"] is None
    assert pos["entry_value_usd"] is None
    assert pos["unrealized_pnl_usd"] is None
    assert pos["max_premium_at_risk_usd"] is None


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


def test_position_plan_material_signature_ignores_quote_noise_and_tiny_moves() -> None:
    from app.services.trading.position_plan_generator import _position_plan_material_signature

    portfolio = {
        "total_positions": 1,
        "regime": "risk_on",
        "spy_direction": "up",
        "vix": 15.1,
        "vix_regime": "normal",
        "avg_pnl_pct": 2.1,
        "winning_count": 1,
        "losing_count": 0,
        "sector_breakdown": {"Tech": 1},
    }
    base = [{
        "trade_id": 1,
        "ticker": "AAPL",
        "asset_type": "stock",
        "direction": "long",
        "entry_price": 100.0,
        "current_price": 102.0,
        "pnl_pct": 2.0,
        "quantity": 5,
        "stop_loss": 96.0,
        "take_profit": 110.0,
        "bars_held": 7,
        "quote_ts": "2026-05-28T10:00:00Z",
    }]
    tiny_move = [dict(base[0], current_price=102.01, quote_ts="2026-05-28T10:01:00Z")]
    large_move = [dict(base[0], current_price=104.0, pnl_pct=4.0)]

    assert _position_plan_material_signature(portfolio, base) == (
        _position_plan_material_signature(portfolio, tiny_move)
    )
    assert _position_plan_material_signature(portfolio, base) != (
        _position_plan_material_signature(portfolio, large_move)
    )


def test_position_plan_material_cache_reuses_matching_signature() -> None:
    from app.services.trading.position_plan_generator import (
        MATERIAL_SIGNATURE_VERSION,
        _get_cached_plans_by_material_signature,
    )

    sig = "same-material-state"
    row = (
        json.dumps({
            "portfolio_summary": {"total_positions": 1},
            "position_plans": [{"ticker": "AAPL", "action": "hold"}],
            "_chili_material_state": {
                "signature": sig,
                "version": MATERIAL_SIGNATURE_VERSION,
            },
        }),
        datetime.utcnow(),
        json.dumps([1]),
    )

    cached = _get_cached_plans_by_material_signature(
        _MaterialCacheDb(row),
        user_id=7,
        trade_ids=[1],
        material_signature=sig,
    )

    assert cached is not None
    assert cached["cached"] is True
    assert cached["cache_reason"] == "material_state_unchanged"
    assert cached["position_plans"][0]["action"] == "hold"
    assert _get_cached_plans_by_material_signature(
        _MaterialCacheDb(row),
        user_id=7,
        trade_ids=[1],
        material_signature="changed",
    ) is None
