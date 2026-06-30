"""ADAPTIVE STRUCTURAL-STOP VOL-FLOOR (DESIGN#2 — widen the 2A trail vol-band for choppy
low-floats, dollar-risk pinned by risk-first sizing, bounded + kill-switched + INVARIANT-A).

The 2A base k (~1.3-sigma) trails too TIGHT vs the chandelier-ATR literature (PF peaks
~3x ATR; 2x over-tightens — the ~40%-of-MFE shakeout). The adaptive maturity factor widens
the EFFECTIVE k toward the literature band ONLY for a fresh, vol-rich runner, decaying to
1.0 at exhaustion. This complements the existing tests/test_trail_width_maturity.py with the
STOP-FLOOR discipline: the widen only changes the vol-band WIDTH (the trail distance), never
the structural low, is bounded by the documented ceiling, and is INVARIANT-A safe (a wider
band only LOWERS the trail candidate — it can decline to ratchet, never loosen a placed stop).
Pure functions; no DB.
"""
from __future__ import annotations

import math

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    trail_width_maturity_factor,
    volnorm_runner_trail_stop,
    volnorm_trail_dist_pct,
)


# ── adaptive widen: a vol-rich FRESH runner widens; calm / exhausting stays 1.0 ──

def test_widen_full_on_fresh_vol_rich_runner():
    # rv_live = 2x the entry vol-floor (vol_gate=1) + fresh fed trend ⇒ full widen.
    f = trail_width_maturity_factor(
        rv_live=0.04, vol_floor_pct=0.02, ofi_level=0.4, ofi_slope=0.05, max_widen=2.0
    )
    assert f == pytest.approx(2.0)


def test_no_widen_when_calm():
    # rv_live == vol_floor (vol_regime == pivot) ⇒ vol_gate 0 ⇒ factor 1.0 (byte-identical).
    f = trail_width_maturity_factor(
        rv_live=0.02, vol_floor_pct=0.02, ofi_level=0.4, ofi_slope=0.05, max_widen=2.0
    )
    assert f == pytest.approx(1.0)


def test_decays_to_one_on_rollover():
    # vol-rich but OFI slope rolling over ⇒ maturity_gate 0 ⇒ factor 1.0 (defer to LOCK/HARD).
    f = trail_width_maturity_factor(
        rv_live=0.06, vol_floor_pct=0.02, ofi_level=0.4, ofi_slope=-0.05, max_widen=2.0
    )
    assert f == pytest.approx(1.0)


def test_half_widen_on_missing_flow():
    # vol-rich, flow read missing ⇒ maturity_gate 0.5 ⇒ factor 1 + (mw-1)*1*0.5 = 1.5.
    f = trail_width_maturity_factor(
        rv_live=0.04, vol_floor_pct=0.02, ofi_level=None, ofi_slope=None, max_widen=2.0
    )
    assert f == pytest.approx(1.5)


def test_factor_neutral_on_bad_inputs():
    assert trail_width_maturity_factor(
        rv_live=None, vol_floor_pct=0.02, ofi_level=0.4, ofi_slope=0.05
    ) == pytest.approx(1.0)
    assert trail_width_maturity_factor(
        rv_live=0.04, vol_floor_pct=None, ofi_level=0.4, ofi_slope=0.05
    ) == pytest.approx(1.0)
    assert trail_width_maturity_factor(
        rv_live=float("nan"), vol_floor_pct=0.02, ofi_level=0.4, ofi_slope=0.05
    ) == pytest.approx(1.0)


def test_factor_bounded_across_grid():
    for rv in (0.0, 0.01, 0.02, 0.05, 0.1, 1.0):
        for slope in (-1.0, -0.01, 0.0, 0.01, 1.0):
            f = trail_width_maturity_factor(
                rv_live=rv, vol_floor_pct=0.02, ofi_level=0.3, ofi_slope=slope, max_widen=2.0
            )
            assert 1.0 <= f <= 2.0
            assert math.isfinite(f)


# ── the widen changes only the BAND WIDTH (vol-floor), bounded by the ceiling ──

def test_widened_k_raises_trail_distance_but_clamped_by_ceiling():
    # A high rv_hold + widened k pushes the candidate above the OLD hard 0.15; the new knob
    # (0.20) lets it materialize, but it is still clamped (bounded — never unbounded).
    base = volnorm_trail_dist_pct(
        rv_live=0.05, expected_hold_s=60.0, grid_secs=2.0, k=1.3, vol_floor_pct=0.02,
        max_dist_pct=0.20,
    )
    widened = volnorm_trail_dist_pct(
        rv_live=0.05, expected_hold_s=60.0, grid_secs=2.0, k=1.3 * 2.0, vol_floor_pct=0.02,
        max_dist_pct=0.20,
    )
    assert widened >= base
    assert widened <= 0.20  # bounded by the documented ceiling


def test_back_compat_default_ceiling_is_015():
    # Default max_dist_pct (no knob passed) clamps at 0.15 — out-of-tree callers byte-identical.
    d = volnorm_trail_dist_pct(
        rv_live=0.2, expected_hold_s=120.0, grid_secs=2.0, k=2.6, vol_floor_pct=0.02
    )
    assert d == pytest.approx(0.15)


def test_floor_keeps_stop_outside_the_spread_bounce():
    # The vol-floor / spread-floor is the LOWER bound — the widen never tightens BELOW it.
    d = volnorm_trail_dist_pct(
        rv_live=0.0, expected_hold_s=60.0, grid_secs=2.0, k=1.3, vol_floor_pct=0.01,
        effective_spread_pct=0.04, spread_floor_mult=1.5, max_dist_pct=0.20,
    )
    assert d == pytest.approx(0.06)  # max(vol_floor 0.01, 1.5*0.04=0.06)


# ── INVARIANT-A: a WIDER band only LOWERS the candidate, never loosens a stop ──

def test_invariant_a_widen_only_tightens_or_holds():
    hwm, be, cs = 100.0, 95.0, 96.0
    base_dist = volnorm_trail_dist_pct(
        rv_live=0.03, expected_hold_s=60.0, grid_secs=2.0, k=1.3, vol_floor_pct=0.02,
        max_dist_pct=0.20,
    )
    widened_dist = volnorm_trail_dist_pct(
        rv_live=0.03, expected_hold_s=60.0, grid_secs=2.0, k=1.3 * 2.0, vol_floor_pct=0.02,
        max_dist_pct=0.20,
    )
    base_stop = volnorm_runner_trail_stop(
        high_water_mark=hwm, trail_dist_pct=base_dist, breakeven_floor=be, current_stop=cs
    )
    widened_stop = volnorm_runner_trail_stop(
        high_water_mark=hwm, trail_dist_pct=widened_dist, breakeven_floor=be, current_stop=cs
    )
    # The wider band's candidate is LOWER, but INVARIANT-A floors at max(cs, be, candidate):
    # both stops are >= current_stop and >= breakeven — never loosened.
    assert base_stop >= cs and base_stop >= be
    assert widened_stop >= cs and widened_stop >= be
    # A wider band can only ratchet LESS aggressively (lower-or-equal), never below the floor.
    assert widened_stop <= base_stop
    assert widened_stop >= max(cs, be)
