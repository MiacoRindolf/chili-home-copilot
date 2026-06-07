"""Ross structural pullback-low stop selection (paper_execution helper).

The momentum lane was 0/8 all-time — every exit flagged ``stop_too_tight`` while
price ran 3-13% after the stop. The fix: when the pullback-break entry captures a
structural pullback low, stop just UNDER it (Ross) instead of at a noise-tight ATR
— but never tighter than the vol floor (shake-out guard).
"""
from __future__ import annotations

from app.services.trading.momentum_neural.paper_execution import (
    structural_or_vol_floored_atr_pct,
)


def _atr_pct_to_stop_price(entry, atr_pct, stop_atr_mult):
    return entry * (1.0 - atr_pct * stop_atr_mult)


def test_structural_used_when_wider_than_vol_floor():
    # entry 100, pullback low 96 -> 4% structural stop distance. Vol floor at ~1%.
    eff, model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.0167,  # 0.0167 * 0.60 = 1.0% stop distance
        structural_stop_price=96.0,
        entry_price=100.0,
        stop_atr_mult=0.60,
    )
    assert model == "structural_pullback"
    # the effective atr_pct must reconstruct the structural stop at ~96.
    assert round(_atr_pct_to_stop_price(100.0, eff, 0.60), 2) == 96.0


def test_vol_floor_kept_when_structure_is_tighter():
    # A very shallow pullback (low 99.5 -> 0.5% structural) is INSIDE the noise; the
    # vol floor (3% here) must win so the trade is not shaken out again.
    eff, model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.05,  # 0.05 * 0.60 = 3.0% stop distance
        structural_stop_price=99.5,
        entry_price=100.0,
        stop_atr_mult=0.60,
    )
    assert model == "vol_floored_atr"
    assert eff == 0.05


def test_no_structural_stop_falls_back_to_vol_floor():
    eff, model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.02,
        structural_stop_price=None,
        entry_price=100.0,
        stop_atr_mult=0.60,
    )
    assert model == "vol_floored_atr"
    assert eff == 0.02


def test_structural_above_entry_is_ignored():
    # A stop at/above entry is invalid for a long; keep the vol floor.
    eff, model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.02,
        structural_stop_price=101.0,
        entry_price=100.0,
        stop_atr_mult=0.60,
    )
    assert model == "vol_floored_atr"
    assert eff == 0.02


def test_structural_distance_capped_at_15pct():
    # An absurdly far pullback low must not blow the stop past the 0.15 sanity cap.
    eff, model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.02,
        structural_stop_price=10.0,  # 90% below entry
        entry_price=100.0,
        stop_atr_mult=0.60,
    )
    assert eff <= 0.15
    assert model == "structural_pullback"


def test_garbage_inputs_fall_back():
    eff, model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.02,
        structural_stop_price="not-a-number",
        entry_price=100.0,
        stop_atr_mult=0.60,
    )
    assert model == "vol_floored_atr"
    assert eff == 0.02
