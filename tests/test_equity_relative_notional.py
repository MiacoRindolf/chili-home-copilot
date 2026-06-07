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
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: 2000.0)
    assert rp.equity_relative_notional_cap(500.0) == pytest.approx(300.0)  # 2000 * 0.15


def test_equity_relative_scales_down_in_drawdown(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    # Equity halved (drawdown) -> notional cap halves automatically.
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: 1000.0)
    assert rp.equity_relative_notional_cap(500.0) == pytest.approx(150.0)


def test_equity_relative_falls_back_when_no_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: None)
    assert rp.equity_relative_notional_cap(500.0) == 500.0  # documented fixed fallback


def test_equity_relative_falls_back_on_zero_fraction(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: 2000.0)
    assert rp.equity_relative_notional_cap(500.0) == 500.0  # fraction disabled -> fixed


def test_equity_relative_falls_back_on_nonpositive_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: 0.0)
    assert rp.equity_relative_notional_cap(500.0) == 500.0


def test_equity_relative_preserves_zero_disable_cap(monkeypatch) -> None:
    # A deliberate 0 cap (operator disable/block) must be preserved, not
    # resurrected to equity x fraction.
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: 2000.0)
    assert rp.equity_relative_notional_cap(0.0) == 0.0


# ── per-trade MAX-LOSS cap (sibling of the notional cap) ──────────────────────


def test_equity_relative_loss_cap_uses_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: 2000.0)
    assert rp.equity_relative_loss_cap(50.0) == pytest.approx(20.0)  # 2000 * 0.01


def test_equity_relative_loss_cap_preserves_zero(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: 2000.0)
    assert rp.equity_relative_loss_cap(0.0) == 0.0


def test_equity_relative_loss_cap_falls_back_no_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: None)
    assert rp.equity_relative_loss_cap(50.0) == 50.0


# ── DAILY-LOSS circuit-breaker cap (global, evaluated live) ───────────────────


def test_equity_relative_daily_loss_uses_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_daily_loss_fraction_of_equity", 0.05)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: 2000.0)
    assert rp.equity_relative_daily_loss_cap(250.0) == pytest.approx(100.0)  # 2000 * 0.05


def test_equity_relative_daily_loss_falls_back_no_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_daily_loss_fraction_of_equity", 0.05)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a: None)
    assert rp.equity_relative_daily_loss_cap(250.0) == 250.0
