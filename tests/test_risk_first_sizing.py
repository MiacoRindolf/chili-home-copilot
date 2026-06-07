"""Risk-first (Ross-style) position sizing.

qty = per-trade max-loss / stop-distance, capped at the equity-relative notional
ceiling. A TIGHTER stop buys MORE size at constant risk — vs notional-first where
stop distance doesn't drive size.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.risk_policy import compute_risk_first_quantity


def test_tighter_stop_buys_more_size_at_same_risk() -> None:
    q_tight, _ = compute_risk_first_quantity(
        entry_price=100.0, atr_pct=0.005, max_loss_usd=20.0, max_notional_ceiling_usd=1e12
    )
    q_wide, _ = compute_risk_first_quantity(
        entry_price=100.0, atr_pct=0.02, max_loss_usd=20.0, max_notional_ceiling_usd=1e12
    )
    assert q_tight > q_wide  # tighter stop -> more shares, same $ risk


def test_qty_equals_max_loss_over_stop_distance() -> None:
    q, meta = compute_risk_first_quantity(
        entry_price=100.0, atr_pct=0.02, max_loss_usd=20.0, max_notional_ceiling_usd=1e12
    )
    # stop_dist = 100 * (0.02 * 0.60) = 1.2 ; qty = 20 / 1.2
    assert meta["stop_distance"] == pytest.approx(1.2)
    assert q == pytest.approx(20.0 / 1.2, rel=1e-3)
    assert meta["model"] == "risk_first"


def test_notional_ceiling_caps_the_qty() -> None:
    # tiny ATR -> 0.3% stop floor -> huge risk-first qty -> notional ceiling binds
    q, meta = compute_risk_first_quantity(
        entry_price=100.0, atr_pct=0.002, max_loss_usd=20.0, max_notional_ceiling_usd=300.0
    )
    assert meta["capped_by"] == "notional_ceiling"
    assert q * 100.0 == pytest.approx(300.0, rel=1e-3)


def test_guards_return_zero() -> None:
    assert compute_risk_first_quantity(
        entry_price=0.0, atr_pct=0.02, max_loss_usd=20.0, max_notional_ceiling_usd=1e12
    )[0] == 0.0
    assert compute_risk_first_quantity(
        entry_price=100.0, atr_pct=0.02, max_loss_usd=0.0, max_notional_ceiling_usd=1e12
    )[0] == 0.0


def test_below_min_size_returns_zero() -> None:
    q, meta = compute_risk_first_quantity(
        entry_price=100.0, atr_pct=0.02, max_loss_usd=0.01, max_notional_ceiling_usd=1e12, base_min_size=1.0
    )
    assert q == 0.0
    assert meta["reason"] == "below_min_size"


def test_increment_rounds_down() -> None:
    q, _ = compute_risk_first_quantity(
        entry_price=100.0, atr_pct=0.02, max_loss_usd=20.0, max_notional_ceiling_usd=1e12, base_increment=1.0
    )
    # 20/1.2 = 16.67 -> floor to 16
    assert q == pytest.approx(16.0)
