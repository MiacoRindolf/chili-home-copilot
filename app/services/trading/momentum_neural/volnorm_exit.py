"""LEVER 2 — adaptive volatility-normalized trailing exit (ratchet-only).

A fixed ATR-multiple trail over-tightens in quiet tape (noise stop-outs) and
under-tightens on a rollover (gives back MFE). This lever sizes the trail off the
instrument's own *denoised* realized volatility, scaled to the hold horizon by the
square-root-of-time rule, referenced to the micro-price (true fair value, not the
noisy last print), and modulated by order-flow VELOCITY:

  * RIDE  (positive, sustained flow)  -> hold the band WIDE  (capture MFE)
  * LOCK  (rollover / flow flips)     -> tighten the band    (protect gains)

INVARIANT-A (the load-bearing safety property): a *sequence* of ticks can only
ever TIGHTEN the stop, never loosen it. The proposed stop is a candidate; the
returned stop is ``max(prev_stop, candidate)`` for a long (``min`` for a short).
So ``new_stop >= prev_stop`` always holds for a long across any tick sequence,
regardless of how the band widens — RIDE widening the band can only fail to raise
the stop, it can never pull it back down.

Flag-OFF => the caller keeps its existing fixed-ATR trail (this module isn't even
consulted), so the path is byte-identical.

All functions are pure and side-effect-free for replay + unit testing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def denoise_rv(prev_rv: float | None, raw_rv: float, alpha: float) -> float:
    """EWMA-denoise the live realized-vol estimate.

    ``alpha`` weights the new raw sample; ``1 - alpha`` keeps the smoothed prior.
    Lower alpha = smoother (fewer noise-driven stop-outs). With no prior the raw
    sample seeds the series. The denoised value's variance is strictly lower than
    the raw stream's for alpha < 1, which is what reduces noise stop-outs.
    """
    a = max(0.0, min(1.0, float(alpha)))
    rr = max(0.0, float(raw_rv))
    if prev_rv is None:
        return rr
    return a * rr + (1.0 - a) * max(0.0, float(prev_rv))


def rv_hold(rv_live: float, hold_bars: float) -> float:
    """Scale a per-bar realized-vol to the hold horizon via sqrt-of-time.

    rv over ``n`` independent bars scales as ``rv * sqrt(n)`` — the diffusion
    rule. ``hold_bars`` is clamped to >= 1 so a sub-bar hold never shrinks the
    band below the single-bar vol.
    """
    n = max(1.0, float(hold_bars))
    return max(0.0, float(rv_live)) * math.sqrt(n)


@dataclass(frozen=True)
class VelocityState:
    """Order-flow velocity verdict driving the band width."""

    mode: str        # "ride" | "lock" | "neutral"
    k: float         # trail width in rv_hold units, within [min_k, max_k]


def velocity_band(
    *,
    flow_velocity: float,
    base_k: float,
    min_k: float,
    max_k: float,
    ride_threshold: float = 0.0,
    lock_threshold: float = 0.0,
) -> VelocityState:
    """Pick the trail width ``k`` from order-flow velocity.

    Positive sustained flow (``flow_velocity > ride_threshold``) => RIDE: widen
    toward ``max_k`` to let the runner run. A rollover (``flow_velocity <
    lock_threshold``, default 0 => any negative flow) => LOCK: tighten toward
    ``min_k``. In between => neutral at ``base_k``. ``k`` is always clamped to
    ``[min_k, max_k]`` so the band can never exceed its documented bounds.
    """
    lo = min(float(min_k), float(max_k))
    hi = max(float(min_k), float(max_k))
    bk = max(lo, min(hi, float(base_k)))
    fv = float(flow_velocity)
    if fv > float(ride_threshold):
        # Scale toward max_k with flow strength (bounded). Saturates at fv>=1.
        frac = max(0.0, min(1.0, fv))
        k = bk + (hi - bk) * frac
        return VelocityState(mode="ride", k=max(lo, min(hi, k)))
    if fv < float(lock_threshold):
        frac = max(0.0, min(1.0, -fv))
        k = bk - (bk - lo) * frac
        return VelocityState(mode="lock", k=max(lo, min(hi, k)))
    return VelocityState(mode="neutral", k=bk)


def trail_distance(rv_hold_val: float, k: float) -> float:
    """Trail distance in price units = volatility-over-hold x band width."""
    return max(0.0, float(rv_hold_val)) * max(0.0, float(k))


@dataclass(frozen=True)
class TrailUpdate:
    new_stop: float
    candidate_stop: float
    trail_dist: float
    k: float
    mode: str
    ratcheted: bool       # True if the stop moved this tick


def update_trailing_stop(
    *,
    prev_stop: float | None,
    micro_price: float,
    rv_live: float,
    hold_bars: float,
    flow_velocity: float,
    base_k: float,
    min_k: float,
    max_k: float,
    is_long: bool = True,
) -> TrailUpdate:
    """Compute the next trailing stop. ENFORCES INVARIANT-A (ratchet-only).

    The candidate stop is ``micro_price - trail_dist`` (long) referenced to the
    MICRO-PRICE (fair value), not the last print. The returned ``new_stop`` is
    ``max(prev_stop, candidate)`` for a long / ``min`` for a short, so the stop
    can only ever tighten across a tick sequence — RIDE widening the band lowers
    the *candidate* but can never lower the *returned* stop below ``prev_stop``.
    """
    vs = velocity_band(
        flow_velocity=flow_velocity,
        base_k=base_k,
        min_k=min_k,
        max_k=max_k,
    )
    rvh = rv_hold(rv_live, hold_bars)
    dist = trail_distance(rvh, vs.k)

    if is_long:
        candidate = float(micro_price) - dist
        if prev_stop is None:
            new_stop = candidate
        else:
            new_stop = max(float(prev_stop), candidate)  # INVARIANT-A: tighten only
    else:
        candidate = float(micro_price) + dist
        if prev_stop is None:
            new_stop = candidate
        else:
            new_stop = min(float(prev_stop), candidate)  # INVARIANT-A: tighten only

    ratcheted = prev_stop is None or new_stop != float(prev_stop)
    return TrailUpdate(
        new_stop=new_stop,
        candidate_stop=candidate,
        trail_dist=dist,
        k=vs.k,
        mode=vs.mode,
        ratcheted=ratcheted,
    )
