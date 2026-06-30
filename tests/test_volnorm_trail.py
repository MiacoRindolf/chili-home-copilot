"""LEVER 2A — MATH-VERIFIED adaptive vol-normalized runner trail (CORE).

Pure-function tests for the math core + the load-bearing INVARIANT-A proof
(ratchet-only: the trail NEVER loosens / nulls the structural or breakeven stop).
"""

from __future__ import annotations

import math

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    denoised_rv_ewma,
    micro_price,
    roll_effective_spread_pct,
    volnorm_runner_trail_stop,
    volnorm_trail_dist_pct,
)
from app.services.trading.momentum_neural.pipeline import _event_grid_log_returns


# --------------------------------------------------------------------------- #
# denoised_rv_ewma
# --------------------------------------------------------------------------- #
def test_rv_ewma_none_when_too_few() -> None:
    assert denoised_rv_ewma([], half_life=5.0) is None
    assert denoised_rv_ewma([0.001], half_life=5.0) is None


def test_rv_ewma_constant_returns_recovers_magnitude() -> None:
    # constant |r| = 0.002 -> EWMA variance == 0.002^2 -> stdev == 0.002
    rv = denoised_rv_ewma([0.002] * 20, half_life=5.0)
    assert rv is not None
    assert abs(rv - 0.002) < 1e-9


def test_rv_ewma_weights_recent_more() -> None:
    # a recent burst of vol after a calm stretch lifts the estimate above the
    # calm level (recency weighting), but stays below the burst level.
    calm = [0.0005] * 30
    burst = [0.01] * 5
    rv = denoised_rv_ewma(calm + burst, half_life=4.0)
    assert rv is not None
    assert 0.0005 < rv < 0.01


def test_rv_ewma_rejects_bad_half_life() -> None:
    assert denoised_rv_ewma([0.001, 0.002, 0.003], half_life=0.0) is None
    assert denoised_rv_ewma([0.001, 0.002, 0.003], half_life=-1.0) is None


# --------------------------------------------------------------------------- #
# roll_effective_spread_pct
# --------------------------------------------------------------------------- #
def test_roll_none_without_bounce_signature() -> None:
    # trending (positively autocorrelated) returns -> non-negative cov -> None
    assert roll_effective_spread_pct([0.001, 0.001, 0.001, 0.001]) is None


def test_roll_detects_bid_ask_bounce() -> None:
    # alternating returns = classic bounce: lag-1 cov is negative -> positive spread
    bounce = [0.002, -0.002, 0.002, -0.002, 0.002, -0.002]
    hs = roll_effective_spread_pct(bounce)
    assert hs is not None
    assert hs > 0.0


# --------------------------------------------------------------------------- #
# volnorm_trail_dist_pct
# --------------------------------------------------------------------------- #
def test_trail_dist_sqrt_of_time_scaling() -> None:
    # rv_step 0.001, N = hold/grid_secs = 50/2 = 25 -> rv_hold = 0.001*5 = 0.005
    # candidate = k*rv_hold = 1.3*0.005 = 0.0065 (above floor, below cap)
    d = volnorm_trail_dist_pct(
        rv_live=0.001, expected_hold_s=50.0, grid_secs=2.0,
        k=1.3, vol_floor_pct=0.005,
    )
    assert abs(d - 0.0065) < 1e-9


def test_trail_dist_clamped_to_floor() -> None:
    # tiny vol -> candidate below the floor -> floor wins
    d = volnorm_trail_dist_pct(
        rv_live=1e-6, expected_hold_s=10.0, grid_secs=2.0,
        k=1.3, vol_floor_pct=0.02,
    )
    assert abs(d - 0.02) < 1e-9


def test_trail_dist_clamped_to_ceiling() -> None:
    # huge vol -> candidate above 0.15 -> capped at 0.15
    d = volnorm_trail_dist_pct(
        rv_live=0.05, expected_hold_s=100.0, grid_secs=2.0,
        k=1.7, vol_floor_pct=0.01,
    )
    assert abs(d - 0.15) < 1e-12


def test_trail_dist_spread_floor_pushes_outside_bounce() -> None:
    # eff half-spread 0.02 * spread_floor_mult 1.5 = 0.03 floor; vol_floor lower ->
    # the spread floor governs so the stop sits OUTSIDE the bounce.
    d = volnorm_trail_dist_pct(
        rv_live=1e-6, expected_hold_s=10.0, grid_secs=2.0,
        k=1.3, vol_floor_pct=0.005, effective_spread_pct=0.02, spread_floor_mult=1.5,
    )
    assert abs(d - 0.03) < 1e-9


def test_trail_dist_bad_inputs_fall_back_to_floor() -> None:
    d = volnorm_trail_dist_pct(
        rv_live=float("nan"), expected_hold_s=10.0, grid_secs=2.0,
        k=1.3, vol_floor_pct=0.012,
    )
    assert abs(d - 0.012) < 1e-12


# --------------------------------------------------------------------------- #
# micro_price (Stoikov)
# --------------------------------------------------------------------------- #
def test_micro_price_leans_to_deeper_side() -> None:
    # bid 10.00 (size 100), ask 10.02 (size 900): more size on the ask -> mp leans
    # toward the bid (the side less likely to move). mp = (10*900 + 10.02*100)/1000.
    mp = micro_price(10.00, 100, 10.02, 900)
    assert mp is not None
    assert 10.00 < mp < 10.01  # closer to bid


def test_micro_price_degenerate_none() -> None:
    assert micro_price(10.0, 0, 10.02, 0) is None


# --------------------------------------------------------------------------- #
# INVARIANT-A — ratchet-only. THE load-bearing safety property.
# new_stop = max(current_stop, breakeven, candidate); never below either.
# --------------------------------------------------------------------------- #
def test_invariant_a_never_loosens_existing_stop() -> None:
    # candidate trail (collapsed vol -> tight dist) lands BELOW the live stop:
    # the existing higher stop MUST be preserved (ratchet-only).
    out = volnorm_runner_trail_stop(
        high_water_mark=10.0, trail_dist_pct=0.30,  # candidate = 7.0
        breakeven_floor=0.0, current_stop=9.5, side_long=True,
    )
    assert out == 9.5  # NOT loosened down to 7.0


def test_invariant_a_never_below_breakeven() -> None:
    out = volnorm_runner_trail_stop(
        high_water_mark=10.0, trail_dist_pct=0.20,  # candidate = 8.0
        breakeven_floor=9.0, current_stop=8.5, side_long=True,
    )
    assert out == 9.0  # breakeven floor governs


def test_invariant_a_tightens_when_candidate_higher() -> None:
    # vol-norm candidate ABOVE the live stop -> it tightens (this is the win).
    out = volnorm_runner_trail_stop(
        high_water_mark=10.0, trail_dist_pct=0.02,  # candidate = 9.8
        breakeven_floor=9.0, current_stop=9.5, side_long=True,
    )
    assert abs(out - 9.8) < 1e-9


def test_invariant_a_property_over_random_inputs() -> None:
    # PROOF (exhaustive over a grid): the returned stop is ALWAYS >= max(current,
    # breakeven) for a long. It can only ever ratchet UP.
    for hwm in (5.0, 10.0, 50.0):
        for dist in (0.0, 0.01, 0.05, 0.2, 0.5):
            for cs in (0.0, hwm * 0.5, hwm * 0.99):
                for be in (0.0, hwm * 0.4, hwm * 0.95):
                    out = volnorm_runner_trail_stop(
                        high_water_mark=hwm, trail_dist_pct=dist,
                        breakeven_floor=be, current_stop=cs, side_long=True,
                    )
                    assert out >= cs - 1e-12, (hwm, dist, cs, be, out)
                    assert out >= be - 1e-12, (hwm, dist, cs, be, out)


def test_invariant_a_bad_inputs_return_current_stop() -> None:
    out = volnorm_runner_trail_stop(
        high_water_mark=float("nan"), trail_dist_pct=0.05,
        breakeven_floor=9.0, current_stop=9.5, side_long=True,
    )
    assert out == 9.5  # never nulls the live stop


# --------------------------------------------------------------------------- #
# _event_grid_log_returns — sub-sample denoising + tick-rate
# --------------------------------------------------------------------------- #
def _ticks(prices_at_secs):
    """[(epoch_secs, price)] -> rows shaped (price, observed_at) with .timestamp()."""
    class _TS:
        def __init__(self, s):
            self._s = s

        def timestamp(self):
            return self._s

    return [(p, _TS(s)) for s, p in prices_at_secs]


def test_event_grid_subsamples_and_tick_rate() -> None:
    # 11 ticks over 10s; grid 2s -> ~one sample per 2s bucket (denoising).
    rows = _ticks([(float(i), 10.0 + 0.01 * i) for i in range(11)])
    returns, tick_rate, dbg = _event_grid_log_returns(rows, grid_secs=2.0)
    assert dbg["n_ticks"] == 11
    assert dbg["n_grid"] < 11  # sub-sampled below the raw tick count
    assert tick_rate == pytest.approx(11 / 10.0, rel=1e-6)
    assert all(math.isfinite(r) for r in returns)


def test_event_grid_thin_tape_empty() -> None:
    returns, tick_rate, dbg = _event_grid_log_returns(_ticks([(0.0, 10.0)]), grid_secs=2.0)
    assert returns == []
    assert dbg["n_grid"] == 0
