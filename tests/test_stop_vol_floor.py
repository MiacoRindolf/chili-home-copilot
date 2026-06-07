"""Volatility-floored stop ATR-pct (the KAIO shake-out fix)."""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    effective_stop_atr_pct,
    stop_target_prices,
)


def test_floors_stop_to_outside_live_noise():
    # KAIO: regime ATR 1.2%, live expected_move 400bps, stop_atr_mult 0.60.
    # regime-only stop = 0.012*0.60 = 72bps (inside the 400bps noise -> shaken out).
    eff = effective_stop_atr_pct(0.012, 400.0, stop_atr_mult=0.60, vol_floor_mult=0.5)
    # floor_atr_pct = 0.5*0.04/0.60 = 0.0333 -> stop_dist = 0.0333*0.60 = 200bps
    assert eff == pytest.approx(0.03333, rel=1e-3)
    stop_px, _ = stop_target_prices(1.0, atr_pct=eff, side_long=True, stop_atr_mult=0.60, target_atr_mult=1.2)
    assert (1.0 - stop_px) == pytest.approx(0.02, rel=1e-3)  # 200bps, outside noise


def test_calm_instrument_keeps_regime_stop():
    # Low expected_move -> the regime ATR already clears the floor; no widening.
    eff = effective_stop_atr_pct(0.012, 50.0, stop_atr_mult=0.60, vol_floor_mult=0.5)
    # floor = 0.5*0.005/0.60 = 0.00417 < regime 0.012 -> regime wins
    assert eff == pytest.approx(0.012, rel=1e-6)


def test_missing_expected_move_falls_back_to_regime():
    assert effective_stop_atr_pct(0.012, None, stop_atr_mult=0.60) == pytest.approx(0.012)
    assert effective_stop_atr_pct(0.012, 0.0, stop_atr_mult=0.60) == pytest.approx(0.012)


def test_clamped_to_sane_max():
    # Pathologically high expected move -> clamp the stop ATR-pct, do not explode.
    eff = effective_stop_atr_pct(0.012, 50_000.0, stop_atr_mult=0.60, vol_floor_mult=0.5)
    assert eff == pytest.approx(0.15)


def test_wider_stop_means_constant_risk_smaller_size():
    # Risk-first sizing: with a wider (vol-floored) stop, qty must shrink so $risk
    # stays the same. qty = risk / (entry * atr_pct * stop_atr_mult).
    from app.services.trading.momentum_neural.risk_policy import compute_risk_first_quantity
    eff_narrow = 0.012
    eff_wide = effective_stop_atr_pct(0.012, 400.0, stop_atr_mult=0.60, vol_floor_mult=0.5)
    q_narrow, _ = compute_risk_first_quantity(entry_price=1.0, atr_pct=eff_narrow, max_loss_usd=20.0, max_notional_ceiling_usd=1e12, stop_atr_mult=0.60)
    q_wide, _ = compute_risk_first_quantity(entry_price=1.0, atr_pct=eff_wide, max_loss_usd=20.0, max_notional_ceiling_usd=1e12, stop_atr_mult=0.60)
    assert q_wide < q_narrow  # wider stop -> smaller size at constant risk
