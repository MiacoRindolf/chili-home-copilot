"""Equity-relative per-trade notional cap (adaptive, no fixed-$ magic).

Per-trade notional = account_equity x ONE documented fraction; falls back to the
fixed cap when equity/fraction is unavailable. Scales DOWN in drawdown.
[[feedback_adaptive_no_magic]]
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural import risk_policy as rp


def test_equity_relative_uses_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda: 2000.0)
    assert rp.equity_relative_notional_cap(500.0) == pytest.approx(300.0)  # 2000 * 0.15


def test_equity_relative_scales_down_in_drawdown(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    # Equity halved (drawdown) -> notional cap halves automatically.
    monkeypatch.setattr(rp, "_account_equity_usd", lambda: 1000.0)
    assert rp.equity_relative_notional_cap(500.0) == pytest.approx(150.0)


def test_equity_relative_falls_back_when_no_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda: None)
    assert rp.equity_relative_notional_cap(500.0) == 500.0  # documented fixed fallback


def test_equity_relative_falls_back_on_zero_fraction(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda: 2000.0)
    assert rp.equity_relative_notional_cap(500.0) == 500.0  # fraction disabled -> fixed


def test_equity_relative_falls_back_on_nonpositive_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda: 0.0)
    assert rp.equity_relative_notional_cap(500.0) == 500.0


def test_equity_relative_preserves_zero_disable_cap(monkeypatch) -> None:
    # A deliberate 0 cap (operator disable/block) must be preserved, not
    # resurrected to equity x fraction.
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda: 2000.0)
    assert rp.equity_relative_notional_cap(0.0) == 0.0
