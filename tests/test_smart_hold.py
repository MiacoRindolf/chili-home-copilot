"""GAP-A — smart-hold breakout-or-bailout test suite (pure unit tests, no DB).

Covers: vol-adaptive band scaling with rv, the N=expected_hold/grid_secs dimensional
check, the HOLD time-floor suppressing the first wick, CUT on volume-confirmed breach
and on decisive flow-reversal, INVARIANT-A (structural stop / max-loss still fire inside
the hold window; smart-hold only tightens), and the flag-off 0.001-buffer byte-identical
contract against the legacy ``breakout_failed_to_hold``.
"""

import math

from app.services.trading.momentum_neural.smart_hold import (
    HoldDecision,
    evaluate_smart_hold,
    hold_grid_steps,
    vol_adaptive_band,
)
from app.services.trading.momentum_neural.entry_gates import breakout_failed_to_hold


# ── Vol-adaptive band: explosive=wider, calm=tighter ──────────────────────────

def test_band_widens_with_realized_vol():
    """Higher rv => wider band; the explosive name gets more room than the calm one."""
    calm = vol_adaptive_band(rv_live=0.01, hold_bars=1.0, band_k=2.0, buffer_pct=0.001, level=100.0)
    explosive = vol_adaptive_band(rv_live=0.50, hold_bars=1.0, band_k=2.0, buffer_pct=0.001, level=100.0)
    assert explosive > calm
    # Calm with tiny rv collapses toward the fixed floor.
    assert calm >= 0.001


def test_band_collapses_to_buffer_when_rv_zero():
    """rv=0 => exactly the legacy fixed buffer (byte-identical floor)."""
    band = vol_adaptive_band(rv_live=0.0, hold_bars=4.0, band_k=3.0, buffer_pct=0.001, level=50.0)
    assert band == 0.001


def test_band_floor_is_never_below_buffer():
    """The fixed buffer is a FLOOR; the adaptive band can only ever be wider."""
    band = vol_adaptive_band(rv_live=0.0001, hold_bars=1.0, band_k=0.1, buffer_pct=0.01, level=100.0)
    assert band >= 0.01


def test_band_scales_sqrt_of_hold_horizon():
    """Band uses the sqrt-of-time diffusion rule over the hold horizon."""
    one = vol_adaptive_band(rv_live=0.1, hold_bars=1.0, band_k=1.0, buffer_pct=0.0, level=100.0)
    four = vol_adaptive_band(rv_live=0.1, hold_bars=4.0, band_k=1.0, buffer_pct=0.0, level=100.0)
    # sqrt(4) = 2x the single-bar band.
    assert math.isclose(four, one * 2.0, rel_tol=1e-9)


# ── N = expected_hold / grid_secs dimensional check ───────────────────────────

def test_grid_steps_is_ceiling_ratio():
    assert hold_grid_steps(60.0, 10.0) == 6
    assert hold_grid_steps(55.0, 10.0) == 6   # ceil(5.5)
    assert hold_grid_steps(50.0, 10.0) == 5


def test_grid_steps_dimensionless_and_floored_at_one():
    """[seconds]/[seconds] = dimensionless integer; sub-grid horizon still yields >=1."""
    n = hold_grid_steps(3.0, 10.0)
    assert isinstance(n, int)
    assert n == 1
    assert hold_grid_steps(0.0, 10.0) == 1


# ── HOLD time-floor suppresses the first wick ─────────────────────────────────

def _base_kwargs(**over):
    kw = dict(
        enabled=True,
        breakout_level=100.0,
        bid=99.0,                 # below the band => a breach by default
        held_secs=2.0,
        grid_secs=10.0,
        expected_hold_secs=60.0,
        rv_live=0.0,              # band collapses to buffer_pct=0.001 => threshold 99.9
        hold_bars=1.0,
        band_k=2.0,
        buffer_pct=0.001,
        flow_velocity=0.5,        # supportive flow (no reversal)
        flow_reversal_threshold=-0.2,
        sell_volume_ratio=2.0,    # would confirm a breach
        breach_volume_confirm=1.0,
        structural_stop_breached=False,
        is_long=True,
    )
    kw.update(over)
    return kw


def test_time_floor_suppresses_first_wick():
    """Inside the first grid-step, a band breach is the expected first wick => HOLD."""
    d = evaluate_smart_hold(**_base_kwargs(held_secs=2.0, grid_secs=10.0))
    assert d.action == "hold"
    assert d.suppressed_wick is True
    assert d.reason == "time_floor_suppress_wick"


def test_breach_cuts_after_time_floor():
    """Past the first grid-step, the same volume-confirmed breach now CUTS."""
    d = evaluate_smart_hold(**_base_kwargs(held_secs=12.0, grid_secs=10.0))
    assert d.action == "cut"
    assert d.reason == "volume_confirmed_breach"
    assert d.suppressed_wick is False


# ── CUT on volume-confirmed floor-breach ──────────────────────────────────────

def test_breach_without_volume_confirmation_holds():
    """A breach not confirmed by sell-side volume is treated as a one-print wick => HOLD."""
    d = evaluate_smart_hold(**_base_kwargs(held_secs=20.0, sell_volume_ratio=0.2, breach_volume_confirm=1.0))
    assert d.action == "hold"
    assert d.reason == "breach_no_volume_confirm"


def test_volume_confirmed_breach_cuts():
    d = evaluate_smart_hold(**_base_kwargs(held_secs=20.0, sell_volume_ratio=1.5, breach_volume_confirm=1.0))
    assert d.action == "cut"
    assert d.reason == "volume_confirmed_breach"


def test_holds_above_band():
    """Bid above the band threshold => HOLD regardless of timing."""
    d = evaluate_smart_hold(**_base_kwargs(held_secs=20.0, bid=100.5))
    assert d.action == "hold"
    assert d.reason == "holding_above_band"


# ── CUT on decisive flow-reversal (even before band breach, even inside floor) ─

def test_flow_reversal_cuts_even_without_breach():
    d = evaluate_smart_hold(**_base_kwargs(held_secs=20.0, bid=101.0, flow_velocity=-0.9, flow_reversal_threshold=-0.2))
    assert d.action == "cut"
    assert d.reason == "flow_reversal"


def test_flow_reversal_overrides_time_floor():
    """A decisive reversal cuts even inside the time-floor — the buyers are gone."""
    d = evaluate_smart_hold(**_base_kwargs(held_secs=1.0, grid_secs=10.0, flow_velocity=-0.9, flow_reversal_threshold=-0.2))
    assert d.action == "cut"
    assert d.reason == "flow_reversal"


# ── INVARIANT-A: structural stop / max-loss STILL fire inside the hold window ──

def test_structural_stop_fires_inside_time_floor():
    """The hard stop overrides the time-floor suppression — smart-hold never loosens it."""
    d = evaluate_smart_hold(**_base_kwargs(held_secs=1.0, grid_secs=10.0, structural_stop_breached=True))
    assert d.action == "cut"
    assert d.reason == "structural_stop"
    assert d.suppressed_wick is False


def test_structural_stop_takes_precedence_over_flow():
    d = evaluate_smart_hold(
        **_base_kwargs(held_secs=1.0, structural_stop_breached=True, flow_velocity=0.9)
    )
    assert d.action == "cut"
    assert d.reason == "structural_stop"


def test_smart_hold_only_ever_tightens_never_loosens():
    """Across a tick sequence smart-hold can move HOLD->CUT but never CUT->HOLD on the
    same hard-stop condition: once the structural stop is breached it always cuts."""
    # First tick: holding fine.
    d1 = evaluate_smart_hold(**_base_kwargs(held_secs=20.0, bid=101.0, flow_velocity=0.5))
    assert d1.action == "hold"
    # Later tick: stop breached -> cut. There is no input that turns a breached stop
    # back into a hold.
    d2 = evaluate_smart_hold(**_base_kwargs(held_secs=21.0, bid=101.0, flow_velocity=0.9, structural_stop_breached=True))
    assert d2.action == "cut"


# ── Flag-off => 0.001-buffer byte-identical to the legacy breakout_failed_to_hold ─

def test_flag_off_matches_legacy_breakout_failed_to_hold():
    """With rv=0 and buffer_pct=0.001, the smart-hold band threshold equals the legacy
    fixed-buffer level, so the CUT decision matches breakout_failed_to_hold exactly
    (the byte-identical flag-off contract)."""
    level, buffer_pct = 100.0, 0.001
    # A grid of post-entry ticks, all past the time-floor so the time-floor doesn't
    # diverge from the legacy (which has no time-floor concept).
    for bid in (99.5, 99.89, 99.90, 99.91, 100.2):
        legacy_cut = breakout_failed_to_hold(
            breakout_level=level, bid=bid, held_seconds=20.0, window_seconds=120.0, buffer_pct=buffer_pct
        )
        d = evaluate_smart_hold(
            enabled=True, breakout_level=level, bid=bid, held_secs=20.0, grid_secs=10.0,
            expected_hold_secs=10.0, rv_live=0.0, hold_bars=1.0, band_k=2.0, buffer_pct=buffer_pct,
            flow_velocity=0.5, flow_reversal_threshold=-0.2, sell_volume_ratio=2.0,
            breach_volume_confirm=1.0, structural_stop_breached=False, is_long=True,
        )
        smart_cut = d.action == "cut"
        assert smart_cut == legacy_cut, f"divergence at bid={bid}: legacy={legacy_cut} smart={smart_cut}"


def test_flag_off_threshold_equals_legacy_buffer_level():
    """The vol-adaptive band with rv=0 reproduces the legacy threshold = level*(1-0.001)."""
    d = evaluate_smart_hold(
        enabled=True, breakout_level=100.0, bid=100.5, held_secs=20.0, grid_secs=10.0,
        expected_hold_secs=10.0, rv_live=0.0, hold_bars=1.0, band_k=5.0, buffer_pct=0.001,
        flow_velocity=0.5, flow_reversal_threshold=-0.2, sell_volume_ratio=2.0,
        breach_volume_confirm=1.0, structural_stop_breached=False, is_long=True,
    )
    assert math.isclose(d.threshold, 100.0 * (1.0 - 0.001), rel_tol=1e-12)
    assert math.isclose(d.band_frac, 0.001, rel_tol=1e-12)


def test_decision_dataclass_shape():
    d = evaluate_smart_hold(**_base_kwargs())
    assert isinstance(d, HoldDecision)
    assert d.grid_steps == hold_grid_steps(60.0, 10.0)
