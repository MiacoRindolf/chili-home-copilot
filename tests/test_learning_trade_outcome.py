from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.trading import learning


def _option_trade(**overrides):
    base = {
        "id": 77,
        "ticker": "SPY",
        "direction": "long",
        "entry_price": 1.25,
        "exit_price": 1.45,
        "quantity": 2.0,
        "qty": 999.0,
        "pnl": 40.0,
        "entry_date": datetime(2026, 5, 26, 14, 30),
        "exit_date": datetime(2026, 5, 26, 15, 30),
        "indicator_snapshot": {
            "asset_type": "options",
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
            "option_meta": {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
            },
        },
        "asset_kind": "option",
        "tags": None,
        "user_id": None,
        "scan_pattern_id": None,
        "exit_reason": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_closed_trade_outcome_fraction_uses_option_contract_notional() -> None:
    trade = _option_trade()

    out = learning._closed_trade_outcome_fraction(trade)

    assert out == pytest.approx(0.16)


def test_closed_trade_directional_win_uses_confirmed_return_when_pnl_missing() -> None:
    trade = _option_trade(pnl=None)

    assert learning._closed_trade_directional_win(trade) is True


def test_closed_trade_directional_win_prefers_partial_aware_return_over_pnl() -> None:
    trade = _option_trade(
        exit_price=1.15,
        quantity=1.0,
        pnl=-10.0,
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    assert learning._closed_trade_directional_win(trade) is True


def test_closed_trade_directional_win_ignores_boolean_pnl() -> None:
    trade = _option_trade(pnl=True, exit_price=1.05)

    assert learning._closed_trade_directional_win(trade) is False


def test_analyze_closed_trade_records_contract_aware_option_outcome(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _gateway_chat(**_kwargs):
        return {"reply": "", "gateway_log_id": 123}

    def _record_trade_outcome(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 456

    monkeypatch.setattr(learning, "get_insights", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.prompts.load_prompt", lambda _name: "trading analyst")
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.gateway_chat",
        _gateway_chat,
    )
    monkeypatch.setattr(
        "app.services.context_brain.outcome_tracker.record_trade_outcome",
        _record_trade_outcome,
    )

    learning.analyze_closed_trade(object(), _option_trade())

    kwargs = captured["kwargs"]
    assert kwargs["gateway_log_id"] == 123
    assert kwargs["purpose"] == "trading_pattern_mine"
    assert kwargs["pnl"] == pytest.approx(0.16)
    assert kwargs["detail"]["won"] is True
    assert kwargs["detail"]["realized_return_pct"] == pytest.approx(16.0)
