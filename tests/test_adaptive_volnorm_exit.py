"""LEVER 2 tests — adaptive volatility-normalized trailing exit.

Covers app/services/trading/momentum_neural/volnorm_exit.py:

  * rv_live denoised vs raw: the EWMA denoise REDUCES variance/bias of the stream.
  * sqrt-of-time rv_hold scaling.
  * trail_dist within the [min_k, max_k] clamp.
  * INVARIANT-A: across a tick sequence the stop can only TIGHTEN
    (long: new_stop >= prev_stop always; short: new_stop <= prev_stop always).
  * micro-price reference (candidate stop is measured off micro_price).
  * velocity RIDE holds the band WIDE on positive flow / LOCK tightens on rollover.
  * flag-off byte-identical (the module isn't consulted — asserted by the
    caller-side contract: the helper has zero global state / side effects).
"""

from __future__ import annotations

import math
import statistics

import pytest

from app.services.trading.momentum_neural.volnorm_exit import (
    denoise_rv,
    rv_hold,
    trail_distance,
    update_trailing_stop,
    velocity_band,
)

ALPHA = 0.4
BASE_K = 2.0
MIN_K = 1.0
MAX_K = 4.0


# --------------------------------------------------------------------------
# Denoise: reduces variance / bias vs raw
# --------------------------------------------------------------------------

def test_denoise_reduces_variance_vs_raw():
    # A noisy raw rv stream around a stable mean; the EWMA-denoised series must
    # have strictly lower variance (fewer noise-driven stop moves).
    raw = [0.02, 0.10, 0.01, 0.09, 0.015, 0.085, 0.02, 0.10, 0.01, 0.09]
    denoised = []
    prev = None
    for r in raw:
        prev = denoise_rv(prev, r, ALPHA)
        denoised.append(prev)
    # Compare on the settled tail (skip the seeded first sample).
    assert statistics.pvariance(denoised[2:]) < statistics.pvariance(raw[2:])


def test_denoise_seeds_with_raw_when_no_prior():
    assert denoise_rv(None, 0.05, ALPHA) == 0.05


def test_denoise_tracks_mean_without_bias():
    # Feeding a constant converges to that constant (no systematic bias).
    prev = None
    for _ in range(50):
        prev = denoise_rv(prev, 0.03, ALPHA)
    assert prev == pytest.approx(0.03, abs=1e-6)


def test_denoise_alpha_one_is_passthrough():
    assert denoise_rv(0.01, 0.07, 1.0) == 0.07


# --------------------------------------------------------------------------
# sqrt-of-time hold scaling
# --------------------------------------------------------------------------

def test_rv_hold_sqrt_time():
    assert rv_hold(0.02, 4.0) == pytest.approx(0.02 * 2.0)
    assert rv_hold(0.02, 9.0) == pytest.approx(0.02 * 3.0)


def test_rv_hold_subbar_floored_at_one():
    assert rv_hold(0.02, 0.25) == pytest.approx(0.02)


# --------------------------------------------------------------------------
# trail_dist clamp
# --------------------------------------------------------------------------

def test_trail_distance_product():
    assert trail_distance(0.05, 2.0) == pytest.approx(0.10)


def test_velocity_band_clamped_within_bounds():
    for fv in (-5.0, -1.0, -0.2, 0.0, 0.2, 1.0, 5.0):
        vs = velocity_band(flow_velocity=fv, base_k=BASE_K, min_k=MIN_K, max_k=MAX_K)
        assert MIN_K <= vs.k <= MAX_K


# --------------------------------------------------------------------------
# velocity RIDE / LOCK
# --------------------------------------------------------------------------

def test_velocity_ride_holds_wide_on_positive_flow():
    vs = velocity_band(flow_velocity=0.8, base_k=BASE_K, min_k=MIN_K, max_k=MAX_K)
    assert vs.mode == "ride"
    assert vs.k > BASE_K  # widened to capture MFE


def test_velocity_lock_tightens_on_rollover():
    vs = velocity_band(flow_velocity=-0.8, base_k=BASE_K, min_k=MIN_K, max_k=MAX_K)
    assert vs.mode == "lock"
    assert vs.k < BASE_K  # tightened to protect gains


def test_velocity_neutral_at_base():
    vs = velocity_band(flow_velocity=0.0, base_k=BASE_K, min_k=MIN_K, max_k=MAX_K)
    assert vs.mode == "neutral"
    assert vs.k == BASE_K


def test_ride_band_strictly_wider_than_lock_band():
    ride = velocity_band(flow_velocity=0.9, base_k=BASE_K, min_k=MIN_K, max_k=MAX_K)
    lock = velocity_band(flow_velocity=-0.9, base_k=BASE_K, min_k=MIN_K, max_k=MAX_K)
    assert ride.k > lock.k


# --------------------------------------------------------------------------
# micro-price reference
# --------------------------------------------------------------------------

def test_candidate_referenced_to_micro_price():
    upd = update_trailing_stop(
        prev_stop=None,
        micro_price=100.0,
        rv_live=0.05,
        hold_bars=4.0,
        flow_velocity=0.0,  # neutral => k = base_k
        base_k=BASE_K,
        min_k=MIN_K,
        max_k=MAX_K,
        is_long=True,
    )
    # rv_hold = 0.05 * sqrt(4) = 0.10; dist = 0.10 * 2.0 = 0.20; cand = 100 - 0.20
    assert upd.trail_dist == pytest.approx(0.20)
    assert upd.candidate_stop == pytest.approx(99.80)
    assert upd.new_stop == pytest.approx(99.80)


# --------------------------------------------------------------------------
# INVARIANT-A: ratchet-only across a tick sequence
# --------------------------------------------------------------------------

def test_invariant_a_long_only_tightens():
    # A realistic noisy tick sequence (price wiggles, flow flips ride<->lock).
    prices = [100.0, 100.5, 100.2, 101.0, 100.8, 101.5, 101.1, 102.0, 101.6, 102.4]
    flows = [0.9, -0.9, 0.5, 0.9, -0.5, 0.9, -0.9, 0.9, -0.9, 0.9]
    rvs = [0.04, 0.09, 0.05, 0.08, 0.06, 0.10, 0.04, 0.09, 0.05, 0.08]
    prev = None
    for p, fv, rv in zip(prices, flows, rvs):
        upd = update_trailing_stop(
            prev_stop=prev,
            micro_price=p,
            rv_live=rv,
            hold_bars=4.0,
            flow_velocity=fv,
            base_k=BASE_K,
            min_k=MIN_K,
            max_k=MAX_K,
            is_long=True,
        )
        if prev is not None:
            assert upd.new_stop >= prev - 1e-12  # INVARIANT-A: never loosens
        prev = upd.new_stop


def test_invariant_a_short_only_tightens():
    prices = [100.0, 99.5, 99.8, 99.0, 99.2, 98.5, 98.9, 98.0, 98.4, 97.6]
    flows = [0.9, -0.9, 0.5, 0.9, -0.5, 0.9, -0.9, 0.9, -0.9, 0.9]
    rvs = [0.04, 0.09, 0.05, 0.08, 0.06, 0.10, 0.04, 0.09, 0.05, 0.08]
    prev = None
    for p, fv, rv in zip(prices, flows, rvs):
        upd = update_trailing_stop(
            prev_stop=prev,
            micro_price=p,
            rv_live=rv,
            hold_bars=4.0,
            flow_velocity=fv,
            base_k=BASE_K,
            min_k=MIN_K,
            max_k=MAX_K,
            is_long=False,
        )
        if prev is not None:
            assert upd.new_stop <= prev + 1e-12  # INVARIANT-A: never loosens (short)
        prev = upd.new_stop


def test_invariant_a_ride_widening_cannot_loosen_stop():
    # Set a tight stop on a LOCK tick, then a RIDE tick widens the band; the
    # candidate drops but the returned stop must hold (ratchet-only).
    lock = update_trailing_stop(
        prev_stop=None, micro_price=100.0, rv_live=0.05, hold_bars=4.0,
        flow_velocity=-0.9, base_k=BASE_K, min_k=MIN_K, max_k=MAX_K, is_long=True,
    )
    ride = update_trailing_stop(
        prev_stop=lock.new_stop, micro_price=100.05, rv_live=0.05, hold_bars=4.0,
        flow_velocity=0.9, base_k=BASE_K, min_k=MIN_K, max_k=MAX_K, is_long=True,
    )
    assert ride.candidate_stop < lock.new_stop   # band widened => lower candidate
    assert ride.new_stop == lock.new_stop        # but the stop did NOT loosen


# --------------------------------------------------------------------------
# Flag-off byte-identical: the helpers are pure (no global state / side effects),
# so when the caller's flag is off and it never invokes them, behavior is
# byte-identical. We assert purity: identical inputs => identical outputs, and
# no shared mutable state leaks between calls.
# --------------------------------------------------------------------------

def test_pure_functions_deterministic_no_side_effects():
    args = dict(
        prev_stop=99.0, micro_price=100.0, rv_live=0.05, hold_bars=4.0,
        flow_velocity=0.3, base_k=BASE_K, min_k=MIN_K, max_k=MAX_K, is_long=True,
    )
    a = update_trailing_stop(**args)
    b = update_trailing_stop(**args)
    assert a == b
    # An interleaved unrelated call doesn't perturb a repeat of the original.
    update_trailing_stop(
        prev_stop=None, micro_price=50.0, rv_live=0.9, hold_bars=16.0,
        flow_velocity=-1.0, base_k=BASE_K, min_k=MIN_K, max_k=MAX_K, is_long=False,
    )
    c = update_trailing_stop(**args)
    assert c == a
