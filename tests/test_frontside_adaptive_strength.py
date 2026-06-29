"""ADAPTIVE FRONT-SIDE STRENGTH — ER-spine size-tilt (replaces the binary backside veto).

The killed binary E1 backside veto (2026-06-25, net-negative) terminally vetoed below-VWAP
RECLAIM winners. This replaces the binary cut with a CONTINUOUS strength score (Kaufman
Efficiency-Ratio spine + VWAP-dist / day-range / OFI level+slope / signed-tape) → an entry
SIZE-TILT multiplier in [size_floor, 1.0] + a soft, NON-TERMINAL defer — never a hard veto.

These pin the contract as PURE functions (no DB):
  * a clean first-push / VWAP-reclaim-turning-up scores HIGH ⇒ full size;
  * a falling-knife (CTNT) scores LOW ⇒ floor size / soft-defer (never blocked);
  * fail-OPEN on stale/missing tape (mult 1.0, no defer — stale != weak);
  * flag OFF ⇒ byte-identical (mult 1.0, no defer);
  * the multiplier is bounded [size_floor, 1.0] and never zero.
"""
from __future__ import annotations

import math

import pytest

from app.services.trading.momentum_neural.ross_momentum import (
    FRONTSIDE_SIZE_FLOOR,
    front_side_size_tilt,
    front_side_strength_score,
    kaufman_efficiency_ratio,
)


# ── Kaufman Efficiency Ratio (spine) ──────────────────────────────────────────

def test_er_clean_one_way_push_is_one():
    assert kaufman_efficiency_ratio([1, 2, 3, 4, 5, 6, 7, 8]) == pytest.approx(1.0)


def test_er_chop_round_trip_is_low():
    er = kaufman_efficiency_ratio([1, 2, 1, 2, 1, 2, 1, 2])
    assert er is not None
    assert er < 0.2


def test_er_none_on_insufficient_or_flat():
    assert kaufman_efficiency_ratio([]) is None
    assert kaufman_efficiency_ratio([5.0]) is None
    assert kaufman_efficiency_ratio([3.0, 3.0, 3.0, 3.0]) is None  # zero path


def test_er_bounded_0_1_and_directionless():
    # A down-trend is just as 'efficient' as an up-trend (magnitude, not direction).
    up = kaufman_efficiency_ratio([1, 2, 3, 4, 5])
    down = kaufman_efficiency_ratio([5, 4, 3, 2, 1])
    assert up == pytest.approx(down)
    assert 0.0 <= up <= 1.0


def test_er_ignores_nan_inf_inputs():
    er = kaufman_efficiency_ratio([1.0, float("nan"), 2.0, 3.0, 4.0])
    assert er is None or (0.0 <= er <= 1.0)


# ── front_side_strength_score (continuous blend) ──────────────────────────────

def _reclaim_kwargs():
    # below-VWAP-RECLAIM-turning-up: clean rising closes, vwap_dist crossing >0, OFI rising.
    return dict(
        closes=[1.0, 1.05, 1.1, 1.18, 1.27, 1.38, 1.5, 1.62],
        vwap_dist_sigma=0.6,
        day_range_pos=0.7,
        ofi_level=0.5,
        ofi_slope=0.2,
        signed_tape=0.4,
    )


def _falling_knife_kwargs():
    # CTNT falling-knife: choppy/down closes, below VWAP, OFI rolling over.
    return dict(
        closes=[2.0, 1.85, 1.95, 1.7, 1.82, 1.6, 1.72, 1.5],
        vwap_dist_sigma=-1.3,
        day_range_pos=0.18,
        ofi_level=-0.4,
        ofi_slope=-0.25,
        signed_tape=-0.5,
    )


def test_reclaim_up_scores_high():
    s = front_side_strength_score(**_reclaim_kwargs())
    assert s is not None
    assert s > 0.6


def test_falling_knife_scores_low():
    s = front_side_strength_score(**_falling_knife_kwargs())
    assert s is not None
    assert s < 0.45


def test_reclaim_beats_knife_by_construction():
    assert front_side_strength_score(**_reclaim_kwargs()) > front_side_strength_score(
        **_falling_knife_kwargs()
    )


def test_strength_none_when_no_informative_term():
    assert front_side_strength_score() is None
    assert front_side_strength_score(closes=[1.0]) is None  # ER None + no other terms


def test_strength_bounded_0_1():
    s = front_side_strength_score(
        closes=[1, 3, 9, 27, 81], vwap_dist_sigma=99.0, day_range_pos=5.0,
        ofi_level=99.0, ofi_slope=99.0, signed_tape=99.0,
    )
    assert s is not None
    assert 0.0 <= s <= 1.0


def test_strength_missing_term_renormalizes_not_zeros():
    # Only the spine present (clean up-push) ⇒ high, not dragged down by absent terms.
    s = front_side_strength_score(closes=[1, 2, 3, 4, 5, 6, 7, 8])
    assert s is not None
    assert s > 0.8


# ── front_side_size_tilt (size-tilt + soft defer) ─────────────────────────────

def test_tilt_strong_full_size():
    mult, defer, _ = front_side_size_tilt(0.95)
    assert mult == pytest.approx(1.0)
    assert defer is False


def test_tilt_weak_floors_and_defers():
    mult, defer, detail = front_side_size_tilt(0.05)
    assert mult == pytest.approx(FRONTSIDE_SIZE_FLOOR)
    assert defer is True  # below the default p15 defer threshold
    assert detail["reason"] == "tilt"


def test_tilt_never_zero_never_above_one():
    for s in (0.0, 0.05, 0.2, 0.5, 0.8, 1.0):
        mult, _, _ = front_side_size_tilt(s)
        assert FRONTSIDE_SIZE_FLOOR <= mult <= 1.0


def test_tilt_monotone_nondecreasing_in_strength():
    xs = [i / 20.0 for i in range(21)]
    mults = [front_side_size_tilt(x)[0] for x in xs]
    for a, b in zip(mults, mults[1:]):
        assert b >= a - 1e-9


def test_tilt_fail_open_on_stale_tape():
    mult, defer, detail = front_side_size_tilt(0.02, stale_tape=True)
    assert mult == pytest.approx(1.0)
    assert defer is False
    assert detail["reason"] == "stale_tape"


def test_tilt_fail_open_when_strength_none():
    mult, defer, detail = front_side_size_tilt(None)
    assert mult == pytest.approx(1.0)
    assert defer is False
    assert detail["reason"] == "no_strength"


def test_tilt_flag_off_byte_identical():
    mult, defer, detail = front_side_size_tilt(0.02, enabled=False)
    assert mult == pytest.approx(1.0)
    assert defer is False
    assert detail["reason"] == "disabled"


def test_tilt_defer_disabled_when_pctile_none():
    mult, defer, _ = front_side_size_tilt(0.02, defer_below=None)
    assert mult == pytest.approx(FRONTSIDE_SIZE_FLOOR)
    assert defer is False


def test_tilt_smoothstep_midband_between_floor_and_full():
    # Strength exactly at the midpoint of [s_lo, s_hi] ⇒ a partial size (smoothstep 0.5).
    mult, _, _ = front_side_size_tilt(0.5, s_lo=0.25, s_hi=0.75)
    assert FRONTSIDE_SIZE_FLOOR < mult < 1.0


def test_tilt_handles_bad_strength_input():
    mult, defer, detail = front_side_size_tilt(float("nan"))
    assert mult == pytest.approx(1.0)
    assert defer is False
    assert detail["reason"] == "bad_input"
