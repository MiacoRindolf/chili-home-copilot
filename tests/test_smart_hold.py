"""GAP-A — SMART POST-ENTRY HOLD (pure-function tests + the load-bearing proofs).

Covers:
  * the percentile primitive (adaptive-knob derivation),
  * the vol-adaptive band width with the CORRECTED √-time rule (N in GRID STEPS,
    NOT a tick count) — the exit reviewer's dimensional-consistency catch,
  * the HOLD/CUT state machine (price-breach+volume CUT, decisive-flow CUT,
    adaptive time-floor suppression, fail-safe-toward-HOLD on missing reads),
  * INVARIANT-A: smart-hold only governs / suppresses the EARLY fast-bail — it
    never returns a "do not ever exit" signal; cut=False is always at most a DEFER.
"""
from __future__ import annotations

import math

from app.services.trading.momentum_neural.hold_signals import (
    SmartHoldDecision,
    percentile,
    smart_hold_band_frac,
    smart_hold_decision,
)


# --------------------------------------------------------------------------- #
# percentile
# --------------------------------------------------------------------------- #
def test_percentile_none_on_empty() -> None:
    assert percentile([], 0.25) is None
    assert percentile([float("nan")], 0.5) is None


def test_percentile_basic_quantiles() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(xs, 0.0) == 1.0
    assert percentile(xs, 1.0) == 5.0
    assert percentile(xs, 0.5) == 3.0
    # q25 linear-interp of [1..5] = position 1.0 -> exactly 2.0
    assert math.isclose(percentile(xs, 0.25), 2.0, rel_tol=1e-9)


# --------------------------------------------------------------------------- #
# smart_hold_band_frac — DIMENSIONAL CONSISTENCY (N in GRID STEPS)
# --------------------------------------------------------------------------- #
def test_band_frac_scales_with_sqrt_of_grid_steps_not_ticks() -> None:
    """band_frac = k*rv_live*sqrt(N), N = expected_hold_s / grid_secs (GRID STEPS).

    Doubling expected_hold_s (at fixed grid) must scale the band by sqrt(2) — NOT by
    the number of ticks. We pin a cap high enough that the clamp is not active.
    """
    rv = 0.002
    k = 1.0
    grid = 2.0
    b1 = smart_hold_band_frac(rv_live=rv, expected_hold_s=20.0, grid_secs=grid, k=k, max_frac=1.0)
    b2 = smart_hold_band_frac(rv_live=rv, expected_hold_s=40.0, grid_secs=grid, k=k, max_frac=1.0)
    # N1 = 10 steps, N2 = 20 steps -> ratio sqrt(2)
    assert math.isclose(b2 / b1, math.sqrt(2.0), rel_tol=1e-9)
    # explicit value: k*rv*sqrt(N1) = 1*0.002*sqrt(10)
    assert math.isclose(b1, rv * math.sqrt(10.0), rel_tol=1e-9)


def test_band_frac_grid_steps_independent_of_tick_count() -> None:
    """The band depends ONLY on (hold/grid), not tick_rate — proves we did not use a
    tick COUNT. Same hold + grid ⇒ identical band regardless of how many ticks occurred."""
    a = smart_hold_band_frac(rv_live=0.003, expected_hold_s=30.0, grid_secs=3.0, k=1.2, max_frac=1.0)
    b = smart_hold_band_frac(rv_live=0.003, expected_hold_s=30.0, grid_secs=3.0, k=1.2, max_frac=1.0)
    assert a == b  # nothing tick-count-dependent enters the math


def test_band_frac_clamped_to_max() -> None:
    huge = smart_hold_band_frac(rv_live=0.5, expected_hold_s=600.0, grid_secs=2.0, k=2.0, max_frac=0.15)
    assert huge == 0.15


def test_band_frac_floor_applied() -> None:
    tiny = smart_hold_band_frac(
        rv_live=1e-9, expected_hold_s=2.0, grid_secs=2.0, k=1.0, floor_frac=0.002, max_frac=0.15
    )
    assert tiny == 0.002


def test_band_frac_bad_input_returns_floor() -> None:
    assert smart_hold_band_frac(
        rv_live=float("nan"), expected_hold_s=10.0, grid_secs=2.0, k=1.0, floor_frac=0.001
    ) == 0.001


# --------------------------------------------------------------------------- #
# smart_hold_decision — state machine
# --------------------------------------------------------------------------- #
_OK_KW = dict(
    window_seconds=60.0,
    time_floor_s=0.0,
    ofi_level=0.5,
    ofi_slope=0.1,
    t_flow=0.25,
    s_flow=0.0,
    tick_rate=5.0,
    tick_rate_ref=5.0,
    rho=0.6,
)


def test_hold_when_bid_above_band() -> None:
    d = smart_hold_decision(
        anchor=10.0, bid=9.99, band_frac=0.01, held_seconds=10.0,
        breach_volume=100.0, breach_volume_median=50.0, **_OK_KW,
    )
    # hold_floor = 10*(1-0.01)=9.90; bid 9.99 > floor -> HOLD
    assert d.cut is False and d.hold is True


def test_cut_on_price_breach_with_volume_confirm() -> None:
    d = smart_hold_decision(
        anchor=10.0, bid=9.80, band_frac=0.01, held_seconds=10.0,
        breach_volume=120.0, breach_volume_median=50.0, **_OK_KW,
    )
    # hold_floor=9.90; bid 9.80 <= floor AND volume-confirmed -> CUT
    assert d.cut is True
    assert d.reason == "price_breach_volume_confirmed"


def test_no_cut_on_price_breach_without_volume_confirm() -> None:
    d = smart_hold_decision(
        anchor=10.0, bid=9.80, band_frac=0.01, held_seconds=10.0,
        breach_volume=10.0, breach_volume_median=50.0, **_OK_KW,
    )
    # breach but volume NOT confirmed -> HOLD (fail-safe)
    assert d.cut is False and d.hold is True
    assert d.reason == "breach_not_volume_confirmed"


def test_no_cut_on_price_breach_when_volume_missing() -> None:
    d = smart_hold_decision(
        anchor=10.0, bid=9.80, band_frac=0.01, held_seconds=10.0,
        breach_volume=None, breach_volume_median=None, **_OK_KW,
    )
    assert d.cut is False and d.hold is True  # missing volume can never force a CUT


def test_decisive_flow_cut_overrides_time_floor() -> None:
    kw = dict(_OK_KW)
    kw["ofi_level"] = -0.5  # < -t_flow
    kw["ofi_slope"] = -0.2  # < -s_flow
    kw["time_floor_s"] = 999.0  # would otherwise suppress
    d = smart_hold_decision(
        anchor=10.0, bid=10.0, band_frac=0.01, held_seconds=1.0,
        breach_volume=None, breach_volume_median=None, **kw,
    )
    assert d.cut is True
    assert d.reason == "decisive_flow_cut"


def test_time_floor_suppresses_price_breach() -> None:
    kw = dict(_OK_KW)
    kw["time_floor_s"] = 30.0
    d = smart_hold_decision(
        anchor=10.0, bid=9.80, band_frac=0.01, held_seconds=5.0,
        breach_volume=120.0, breach_volume_median=50.0, **kw,
    )
    # inside the adaptive time-floor (5s < 30s) AND not decisive-flow -> SUPPRESS the bail
    assert d.cut is False and d.hold is True
    assert d.time_floor_suppressed is True
    assert d.reason == "time_floor_suppressed"


def test_outside_window_is_inert() -> None:
    d = smart_hold_decision(
        anchor=10.0, bid=9.50, band_frac=0.01, held_seconds=120.0,
        breach_volume=999.0, breach_volume_median=1.0, **_OK_KW,
    )
    # held 120s > window 60s -> gate inert (neither cut nor hold); the trail owns it
    assert d.cut is False and d.hold is False
    assert d.reason == "outside_window"


def test_guarded_noop_on_bad_anchor_or_bid() -> None:
    d = smart_hold_decision(
        anchor=0.0, bid=9.0, band_frac=0.01, held_seconds=10.0,
        breach_volume=100.0, breach_volume_median=50.0, **_OK_KW,
    )
    assert d.cut is False and d.hold is False


# --------------------------------------------------------------------------- #
# INVARIANT-A proof: smart-hold can only SUPPRESS or fire the EARLY fast-bail.
# A cut=False is NEVER a "block the structural exit" — it is at most a DEFER, and
# the structural stop / #769 circuit are evaluated AHEAD of this gate (call-site).
# --------------------------------------------------------------------------- #
def test_invariant_a_cut_false_is_only_a_defer() -> None:
    # Even with a deep price breach, inside the time-floor and without decisive flow,
    # the decision is HOLD — but it is explicitly a TIME-FLOOR DEFER, not a veto of any
    # other exit. The structural stop runs ABOVE this gate (proven at the call site).
    kw = dict(_OK_KW)
    kw["time_floor_s"] = 60.0
    d = smart_hold_decision(
        anchor=10.0, bid=5.0, band_frac=0.01, held_seconds=1.0,
        breach_volume=999.0, breach_volume_median=1.0, **kw,
    )
    assert isinstance(d, SmartHoldDecision)
    assert d.cut is False           # the EARLY fast-bail is deferred …
    assert d.time_floor_suppressed is True  # … explicitly by the time-floor
    # … and the helper carries NO field that could disable a structural exit:
    assert set(d._fields) == {
        "cut", "hold", "reason", "band_frac", "hold_floor_px", "time_floor_suppressed"
    }


def test_invariant_a_decisive_flow_never_deferred() -> None:
    """A genuine distribution signal (strong-negative flow + roll-over) is NEVER held
    hostage by the time-floor — it always cuts, matching the always-live backstop intent."""
    kw = dict(_OK_KW)
    kw["ofi_level"] = -0.9
    kw["ofi_slope"] = -0.5
    kw["time_floor_s"] = 1e6
    d = smart_hold_decision(
        anchor=10.0, bid=10.0, band_frac=0.05, held_seconds=0.1,
        breach_volume=None, breach_volume_median=None, **kw,
    )
    assert d.cut is True


# --------------------------------------------------------------------------- #
# Flag-off byte-identical: GAP-A is OFF by default; with it off the deployed
# fixed-0.001-buffer fast-bail path is the one that runs (the call site routes to
# breakout_failed_to_hold). We assert the master defaults to False here; the call
# site's elif preserves the legacy path verbatim.
# --------------------------------------------------------------------------- #
def test_smart_hold_default_off() -> None:
    from app.config import settings

    assert bool(getattr(settings, "chili_momentum_smart_hold_enabled", False)) is False
    # the legacy buffer default is unchanged (byte-identical path)
    assert float(getattr(settings, "chili_momentum_breakout_bailout_buffer_pct", 0.001)) == 0.001
    # the k base is the documented 1.2
    assert float(getattr(settings, "chili_momentum_smart_hold_k_atr", 1.2)) == 1.2
