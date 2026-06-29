"""DESIGN#2 — adaptive trail-width maturity widen (pure, NO DB).

Mirrors tests/test_volnorm_trail.py import style; runnable standalone (no fixtures,
no tables). Verifies the pure widen factor, its fail-neutral / bounded behaviour, and
the load-bearing INVARIANT-A proof that a widened band can only ratchet TIGHTER.
"""

import math

from app.services.trading.momentum_neural.paper_execution import (
    trail_width_maturity_factor,
    volnorm_trail_dist_pct,
    volnorm_runner_trail_stop,
)


def test_factor_neutral_on_missing_inputs():
    # missing rv / vol_floor / max_widen<=1 => byte-identical 1.0
    assert trail_width_maturity_factor(
        rv_live=None, vol_floor_pct=0.03, ofi_level=0.4, ofi_slope=0.05
    ) == 1.0
    assert trail_width_maturity_factor(
        rv_live=0.06, vol_floor_pct=None, ofi_level=0.4, ofi_slope=0.05
    ) == 1.0
    assert trail_width_maturity_factor(
        rv_live=0.06, vol_floor_pct=0.03, ofi_level=0.4, ofi_slope=0.05, max_widen=1.0
    ) == 1.0


def test_factor_neutral_when_calm():
    # rv_live == vol_floor => vol_regime == pivot => vol_gate 0 => factor 1.0
    # regardless of OFI.
    f = trail_width_maturity_factor(
        rv_live=0.03, vol_floor_pct=0.03, ofi_level=0.9, ofi_slope=0.5, max_widen=2.0
    )
    assert f == 1.0


def test_factor_full_widen_fresh_trend():
    # rv_live = 2*vol_floor (vol_gate=1) AND fresh, fed trend => factor == max_widen.
    f = trail_width_maturity_factor(
        rv_live=0.06, vol_floor_pct=0.03, ofi_level=0.4, ofi_slope=0.05, max_widen=2.0
    )
    assert f == 2.0


def test_factor_decays_on_rollover():
    # same vol but slope rolling over => maturity_gate 0 => factor 1.0 (LOCK takes over).
    f = trail_width_maturity_factor(
        rv_live=0.06, vol_floor_pct=0.03, ofi_level=0.4, ofi_slope=-0.05, max_widen=2.0
    )
    assert f == 1.0


def test_factor_half_widen_on_missing_flow():
    # vol_gate=1, OFI read missing => maturity_gate 0.5 => factor 1.0 + (mw-1)*0.5.
    f = trail_width_maturity_factor(
        rv_live=0.06, vol_floor_pct=0.03, ofi_level=None, ofi_slope=None, max_widen=2.0
    )
    assert abs(f - 1.5) < 1e-9


def test_factor_monotone_in_vol_regime():
    # factor non-decreasing as rv_live rises 1x->3x floor (fresh-trend gate held).
    floor = 0.03
    prev = 0.0
    for mult in [1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]:
        f = trail_width_maturity_factor(
            rv_live=floor * mult,
            vol_floor_pct=floor,
            ofi_level=0.4,
            ofi_slope=0.05,
            max_widen=2.0,
        )
        assert f >= prev - 1e-12
        assert f <= 2.0 + 1e-12
        prev = f


def test_factor_bounded():
    # never <1.0, never >max_widen across a grid incl NaN/inf.
    grid = [None, 0.0, 0.03, 0.06, 0.5, float("nan"), float("inf"), -1.0]
    for rv in grid:
        for vf in grid:
            for lvl in [None, 0.0, 0.4, float("nan")]:
                for slp in [None, -0.1, 0.0, 0.1, float("inf")]:
                    f = trail_width_maturity_factor(
                        rv_live=rv,
                        vol_floor_pct=vf,
                        ofi_level=lvl,
                        ofi_slope=slp,
                        max_widen=2.0,
                    )
                    assert math.isfinite(f)
                    assert 1.0 <= f <= 2.0


def test_invariant_a_widen_only_tightens():
    # INTEGRATION: a widened band yields a LOWER candidate; the trail stop is still
    # >= current_stop and >= breakeven for BOTH base-k and widened-k dist.
    rv = 0.02
    hold = 120.0
    grid = 2.0
    base_k = 1.3
    widened_k = base_k * 2.0  # full maturity widen
    floor = 0.01
    base_dist = volnorm_trail_dist_pct(
        rv_live=rv, expected_hold_s=hold, grid_secs=grid, k=base_k,
        vol_floor_pct=floor, max_dist_pct=0.20,
    )
    wide_dist = volnorm_trail_dist_pct(
        rv_live=rv, expected_hold_s=hold, grid_secs=grid, k=widened_k,
        vol_floor_pct=floor, max_dist_pct=0.20,
    )
    # widened band must be >= base band (wider trail => lower candidate).
    assert wide_dist >= base_dist
    hwm = 12.50
    cs = 11.00
    be = 11.50
    for dist in (base_dist, wide_dist):
        new_stop = volnorm_runner_trail_stop(
            high_water_mark=hwm,
            trail_dist_pct=dist,
            breakeven_floor=be,
            current_stop=cs,
            side_long=True,
        )
        # INVARIANT-A: never below current_stop, never below breakeven.
        assert new_stop >= cs - 1e-12
        assert new_stop >= be - 1e-12
    # The wider band's candidate is <= the base band's candidate (decline-to-ratchet).
    base_cand = hwm * (1.0 - base_dist)
    wide_cand = hwm * (1.0 - wide_dist)
    assert wide_cand <= base_cand + 1e-12


def test_max_dist_ceiling_reachable():
    # high rv_hold + widened k clips at the passed max_dist_pct (0.20), not the old 0.15.
    rv = 0.08
    hold = 300.0
    grid = 2.0
    k = 1.3 * 2.0  # 2.6
    floor = 0.01
    d20 = volnorm_trail_dist_pct(
        rv_live=rv, expected_hold_s=hold, grid_secs=grid, k=k,
        vol_floor_pct=floor, max_dist_pct=0.20,
    )
    assert abs(d20 - 0.20) < 1e-9
    # back-compat default path: clips at 0.15.
    d15 = volnorm_trail_dist_pct(
        rv_live=rv, expected_hold_s=hold, grid_secs=grid, k=k,
        vol_floor_pct=floor, max_dist_pct=0.15,
    )
    assert abs(d15 - 0.15) < 1e-9
