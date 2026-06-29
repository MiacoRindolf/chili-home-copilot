"""GAP-A — the SMART POST-ENTRY HOLD (pure, unit-testable helpers).

The deployed early breakout-or-bailout (``entry_gates.breakout_failed_to_hold``)
cuts a fresh entry the instant the bid dips a FIXED 0.001 (10 bps) below the broken
level. On the lane's explosive, wide-spread, low-float names that fixed wick buffer
is far inside the name's own microstructure noise: a violent squeeze steps the bid
DOWN and dip-tests the broken level within seconds while the breakout is WORKING, and
the fast-bail reads that backwards and bails winners at break-even (FCUV +21% after a
4.5s bail).

GAP-A replaces the fixed 0.001 buffer with a VOL-ADAPTIVE band on the name's own live
realized vol scaled to the expected holding horizon, and gates the actual CUT on
order-flow + a volume-confirmed breach + an adaptive time-floor, so the position is
HELD through healthy noise and only cut on a genuinely failing break.

Dimensional consistency (the exit reviewer's correction, applied identically here):
``rv_live`` is the PER-GRID-STEP stdev (``paper_execution.denoised_rv_ewma``), so the
sqrt-of-time scaling to the holding horizon must use N = expected_hold_s / grid_secs
(the number of GRID STEPS over the hold), NOT tick_rate*hold (a tick COUNT). Using a
tick count would over-scale the band by ~sqrt(tick_rate*grid_secs).

INVARIANT-A / backstop contract: GAP-A governs ONLY the first post-entry window and can
only TIGHTEN, never loosen — it decides whether the EARLY fast-bail (the level-retest
cut) fires SOONER or is SUPPRESSED. The structural stop and the #769 max-loss circuit
are evaluated AHEAD of and independently from this gate (they run every tick, BEFORE
this block at the call site), so a genuinely collapsing position still exits regardless
of what GAP-A returns. ``smart_hold_decision`` therefore never returns a "don't ever
exit" — at most it DEFERS the fast-bail; the always-live backstops are untouched.

Every knob is ADAPTIVE (a percentile / z-score of the name's OWN recent distribution)
with ONE documented base each; no magic numbers/strings live here. Pure & deterministic.
"""
from __future__ import annotations

import math
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Small pure statistics helpers (adaptive knobs are percentiles of the name's
# own recent distribution — these are the primitives the calibration recipe uses).
# ---------------------------------------------------------------------------
def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile of ``values`` at quantile ``q`` in [0, 1].

    Returns None on empty / all-non-finite input. Pure — used to derive every
    adaptive knob (T_flow / S_flow / rho / the time-floor) from the name's OWN
    recent distribution rather than a hardcoded threshold."""
    xs = sorted(float(v) for v in (values or []) if isinstance(v, (int, float)) and math.isfinite(v))
    if not xs:
        return None
    try:
        qq = float(q)
    except (TypeError, ValueError):
        return None
    qq = max(0.0, min(1.0, qq))
    if len(xs) == 1:
        return xs[0]
    pos = qq * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def median(values: list[float]) -> float | None:
    """Median of ``values`` (linear-interp p50). None on empty / all-non-finite. Pure.

    Used by GAP-B to derive the name's OWN compression / volume baselines (a ratio vs
    its recent median) rather than any hardcoded level."""
    return percentile(values, 0.5)


def adaptive_quantile_clamp(
    values: list[float], q: float, *, floor: float, ceil: float, fallback: float
) -> float:
    """ADAPTIVE knob primitive shared by GAP-B: the ``q``-quantile of the name's OWN recent
    distribution ``values``, clamped to ``[floor, ceil]``. When the sample is empty / too
    thin to rank (None percentile), return ``fallback`` (the ONE documented base for that
    knob). Pure & deterministic — every GAP-B threshold is one of these (compression theta,
    volume multiple, flow tail), so NO magic number lives at the call site, only a
    documented floor/ceil/base."""
    p = percentile(values, q)
    if p is None:
        try:
            base = float(fallback)
        except (TypeError, ValueError):
            base = 0.0
    else:
        base = float(p)
    try:
        lo = float(floor)
        hi = float(ceil)
    except (TypeError, ValueError):
        return base
    if hi < lo:
        lo, hi = hi, lo
    return max(lo, min(base, hi))


def smart_hold_band_frac(
    *,
    rv_live: float,
    expected_hold_s: float,
    grid_secs: float,
    k: float,
    floor_frac: float = 0.0,
    max_frac: float = 0.15,
) -> float:
    """The VOL-ADAPTIVE hold band as a FRACTION of price (replaces the fixed 0.001).

      rv_hold   = rv_live * sqrt(N),   N = expected_hold_s / grid_secs   (GRID STEPS)
      band_frac = clamp(k * rv_hold, floor_frac, max_frac)

    ``k`` is the caller's ``chili_momentum_smart_hold_k_atr * 1.2533`` (the 1.2533 =
    sqrt(pi/2) converts an EWMA mean-abs-style scale to a stdev-equivalent half-width,
    matching the band geometry of the exit's ``volnorm_trail_dist_pct``). ``rv_live`` is
    the per-grid-step stdev (``denoised_rv_ewma``); N is in GRID STEPS (expected_hold_s /
    grid_secs), NOT a tick count — the SAME dimensional rule the vol-norm exit uses.
    ``max_frac`` mirrors the existing 0.15 ATR-pct clamp. Pure; deterministic.

    NOTE: this is the BAND width only. The HOLD/CUT decision (which composes this band
    with order-flow + a volume-confirmed breach + the adaptive time-floor) lives in
    ``smart_hold_decision``; the band alone never widens or removes any stop."""
    try:
        rv = float(rv_live)
        hold = float(expected_hold_s)
        gs = float(grid_secs)
        kk = float(k)
        floor = max(0.0, float(floor_frac or 0.0))
        cap = float(max_frac)
    except (TypeError, ValueError):
        return max(0.0, float(floor_frac or 0.0))
    if not (math.isfinite(rv) and rv >= 0.0):
        return floor
    n = (
        max(1.0, hold / max(gs, 1e-9))
        if (math.isfinite(hold) and math.isfinite(gs))
        else 1.0
    )
    rv_hold = rv * math.sqrt(n)
    candidate = max(0.0, kk) * rv_hold
    if not (math.isfinite(cap) and cap > 0.0):
        cap = 0.15
    lo = min(floor, cap)
    return max(lo, min(candidate, cap))


class SmartHoldDecision(NamedTuple):
    """Result of the GAP-A post-entry hold state machine.

    ``cut`` — fire the EARLY fast-bail now (route through the existing BAILOUT machinery).
    ``hold`` — explicitly hold through the early window (suppress the fast-bail).
    ``reason`` — audit tag. ``band_frac`` / ``hold_floor_px`` — the derived band for logs.
    ``time_floor_suppressed`` — the bail was deferred by the adaptive time-floor.

    INVARIANT-A: a ``cut=False`` NEVER means "do not exit" — the structural stop and the
    #769 max-loss circuit run AHEAD of this gate every tick and are unaffected. GAP-A only
    governs whether the EARLY level-retest fast-bail fires sooner or is suppressed."""

    cut: bool
    hold: bool
    reason: str
    band_frac: float
    hold_floor_px: float
    time_floor_suppressed: bool


def smart_hold_decision(
    *,
    anchor: float,
    bid: float,
    band_frac: float,
    held_seconds: float,
    window_seconds: float,
    time_floor_s: float,
    ofi_level: float | None,
    ofi_slope: float | None,
    t_flow: float,
    s_flow: float,
    tick_rate: float | None,
    tick_rate_ref: float | None,
    rho: float,
    breach_volume: float | None,
    breach_volume_median: float | None,
) -> SmartHoldDecision:
    """GAP-A post-entry HOLD/CUT state machine for the first post-entry window.

    Anchor = max(breakout_level, entry_avg); hold_floor_px = anchor*(1 - band_frac).

    HOLD while ALL of:
        bid > hold_floor_px
        ofi_level  > -t_flow        (flow not strongly negative)
        ofi_slope  > -s_flow        (flow not rolling over hard)
        tick_rate  > rho * tick_rate_ref   (the thrust is still being fed)

    CUT on EITHER:
        (bid <= hold_floor_px) AND volume-confirmed   (breach_volume >= its recent median)
        (ofi_level < -t_flow) AND (ofi_slope < -s_flow)   (decisive distribution)

    Adaptive TIME-FLOOR: while held_seconds < time_floor_s the fast-bail is SUPPRESSED
    (a momentary sub-floor dip inside the name's own typical break-resolution time is not
    a failed breakout) — UNLESS the decisive double-negative-flow CUT fires, which is a
    genuine distribution signal that should not be deferred. Outside ``window_seconds``
    the gate is inert (cut=False, hold=False) — the normal trail/stop owns the position.

    Order-flow / volume reads are OPTIONAL and fail-SAFE toward HOLD: a missing ofi/volume
    read can never by itself force a CUT (the price-retest CUT additionally requires the
    volume confirm, so absent volume ⇒ no price-CUT). Pure; deterministic.

    INVARIANT-A: ``cut=False`` only DEFERS the early fast-bail; the structural stop and the
    #769 max-loss circuit are evaluated ahead of this gate and are NOT gated by it."""
    try:
        a = float(anchor)
        b = float(bid)
        bf = max(0.0, float(band_frac))
        held = float(held_seconds)
        window = float(window_seconds)
        tfloor = max(0.0, float(time_floor_s))
        tflow = abs(float(t_flow))
        sflow = abs(float(s_flow))
        rr = max(0.0, float(rho))
    except (TypeError, ValueError):
        return SmartHoldDecision(False, False, "bad_input", 0.0, 0.0, False)
    if not (math.isfinite(a) and a > 0.0 and math.isfinite(b) and b > 0.0 and window > 0.0):
        return SmartHoldDecision(False, False, "guarded_noop", bf, 0.0, False)
    # Outside the early window the gate is inert — the trail/structural stop owns it.
    if held > window:
        return SmartHoldDecision(False, False, "outside_window", bf, a * (1.0 - bf), False)

    hold_floor_px = a * (1.0 - bf)

    ofi_l = float(ofi_level) if (ofi_level is not None and math.isfinite(float(ofi_level))) else None
    ofi_s = float(ofi_slope) if (ofi_slope is not None and math.isfinite(float(ofi_slope))) else None

    # DECISIVE distribution CUT: strong-negative flow AND rolling over hard. This is a
    # genuine failure signal and is NOT deferred by the time-floor.
    decisive_flow_cut = (
        ofi_l is not None and ofi_s is not None and ofi_l < -tflow and ofi_s < -sflow
    )
    if decisive_flow_cut:
        return SmartHoldDecision(True, False, "decisive_flow_cut", bf, hold_floor_px, False)

    # PRICE-retest CUT: bid breaches the vol-adaptive floor AND the breach is
    # volume-confirmed (a real distribution breach, not a thin-print wick). Absent a
    # volume read, the breach is NOT confirmed ⇒ HOLD (fail-safe).
    breached = b <= hold_floor_px
    vol_confirmed = (
        breach_volume is not None
        and breach_volume_median is not None
        and math.isfinite(float(breach_volume))
        and math.isfinite(float(breach_volume_median))
        and float(breach_volume) >= float(breach_volume_median)
    )

    # Adaptive TIME-FLOOR: inside the name's own typical break-resolution time, a
    # momentary sub-floor dip is suppressed (the decisive-flow CUT above already escaped).
    if held < tfloor:
        return SmartHoldDecision(False, True, "time_floor_suppressed", bf, hold_floor_px, True)

    if breached and vol_confirmed:
        return SmartHoldDecision(True, False, "price_breach_volume_confirmed", bf, hold_floor_px, False)

    # Otherwise HOLD: either the bid still sits above the band, or the breach is not
    # volume-confirmed. Compute the persistence read for the audit tag (does not change
    # the HOLD outcome — the price/flow CUTs above are the only ways to cut here).
    tr = float(tick_rate) if (tick_rate is not None and math.isfinite(float(tick_rate))) else None
    trr = float(tick_rate_ref) if (tick_rate_ref is not None and math.isfinite(float(tick_rate_ref))) else None
    pace_ok = (tr is None or trr is None or trr <= 0.0) or (tr > rr * trr)
    flow_ok = (ofi_l is None or ofi_l > -tflow) and (ofi_s is None or ofi_s > -sflow)
    if breached and not vol_confirmed:
        reason = "breach_not_volume_confirmed"
    elif pace_ok and flow_ok:
        reason = "hold_persistent"
    else:
        reason = "hold_above_band"
    return SmartHoldDecision(False, True, reason, bf, hold_floor_px, False)
