"""GAP-A — Smart-hold breakout-or-bailout (vol-adaptive, time-floored, flow-confirmed).

The original breakout-or-bailout (``entry_gates.breakout_failed_to_hold``) cuts the
moment the bid dips below the broken level minus a fixed ``0.001`` buffer, inside an
early window. In explosive low-float tape that fixed band over-fires on the FIRST
post-entry wick — the normal breakout shakeout Ross *expects* — booking a tiny loss
on names that then run. Conversely in calm tape the same band is too wide and gives
back MFE before cutting.

Smart-hold replaces the single fixed buffer with three coupled ideas, each pure and
adaptive (no scattered magic numbers — the band derives from the instrument's own
realized vol, the time-floor from the entry-interval grid):

  1. **Vol-adaptive band.** The bail threshold sits ``band_k`` realized-vol-units below
     the broken level, sqrt-of-time-scaled to the hold horizon (reusing the diffusion
     rule from ``volnorm_exit.rv_hold``). Explosive name (high rv) => WIDER band (don't
     cut on its normal wick); calm name (low rv) => TIGHTER band. ``rv=0`` collapses to
     the original fixed ``buffer_pct`` so a no-vol input is byte-identical to the legacy
     path.

  2. **HOLD time-floor.** ``N = ceil(expected_hold_secs / grid_secs)`` grid-steps. Inside
     the first grid-step (``held_secs < grid_secs``) the FIRST wick below the band is
     SUPPRESSED — the breakout is given its expected shakeout window before the bail is
     allowed to fire. The floor is dimensional: a horizon in seconds over a grid in
     seconds yields a dimensionless step count (asserted in tests).

  3. **CUT confirmation.** Past the time-floor the bail fires on EITHER a volume-confirmed
     floor breach (price below the band AND sell-side volume confirms the breach is real,
     not a one-print wick) OR a decisive flow-reversal (order-flow velocity flips hard
     negative — the buyers are gone) even before the band is breached.

INVARIANT-A (load-bearing): smart-hold is an EARLY discretionary cut layered ON TOP of
the structural stop / max-loss circuit; it can only ever tighten, never loosen, the
effective exit. The structural stop and max-loss circuit STILL fire inside the hold
window regardless of the time-floor — ``structural_stop_breached`` short-circuits the
floor. Smart-hold never returns "hold" when the hard stop is hit.

Flag-OFF (``enabled=False``) => the caller keeps the legacy ``breakout_failed_to_hold``
fixed-0.001-buffer path; this module isn't consulted, so the decision is byte-identical.

All functions are pure and side-effect-free for replay + unit testing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def hold_grid_steps(expected_hold_secs: float, grid_secs: float) -> int:
    """``N = ceil(expected_hold / grid_secs)`` — the time-floor in grid-STEPS.

    Dimensional check: ``[seconds] / [seconds] = [dimensionless]`` step count. The
    horizon and the grid are both in seconds, so ``N`` is a pure integer count of
    evaluation ticks, never a duration. Clamped to >= 1 so a sub-grid horizon still
    yields one protective step.
    """
    g = max(1e-9, float(grid_secs))
    n = math.ceil(max(0.0, float(expected_hold_secs)) / g)
    return max(1, int(n))


def vol_adaptive_band(
    *,
    rv_live: float,
    hold_bars: float,
    band_k: float,
    buffer_pct: float,
    level: float,
) -> float:
    """The bail threshold *distance below the broken level*, as a fraction of level.

    The realized vol is scaled to the hold horizon (sqrt-of-time, the diffusion rule),
    multiplied by the band width ``band_k``, and expressed as a fraction of ``level``.
    The legacy fixed ``buffer_pct`` is a FLOOR, so the adaptive band can only ever be
    WIDER than the original, never tighter — an explosive name gets more room, a calm
    name collapses back to the fixed buffer. ``rv_live=0`` => exactly ``buffer_pct``
    (byte-identical to the legacy path).
    """
    bp = max(0.0, float(buffer_pct))
    lvl = float(level)
    if lvl <= 0.0:
        return bp
    n = max(1.0, float(hold_bars))
    rvh = max(0.0, float(rv_live)) * math.sqrt(n)          # vol over the hold horizon
    adaptive_frac = (rvh * max(0.0, float(band_k))) / lvl  # as a fraction of level
    return max(bp, adaptive_frac)


@dataclass(frozen=True)
class HoldDecision:
    """Smart-hold verdict for one post-entry tick."""

    action: str          # "hold" | "cut"
    reason: str
    band_frac: float     # the vol-adaptive band as a fraction of level
    threshold: float     # absolute price level the bid must hold
    grid_steps: int      # N = ceil(expected_hold / grid_secs)
    suppressed_wick: bool  # True if a band breach was suppressed by the time-floor


def evaluate_smart_hold(
    *,
    enabled: bool,
    breakout_level: float | None,
    bid: float | None,
    held_secs: float,
    grid_secs: float,
    expected_hold_secs: float,
    rv_live: float,
    hold_bars: float,
    band_k: float,
    buffer_pct: float = 0.001,
    flow_velocity: float = 0.0,
    flow_reversal_threshold: float = 0.0,
    sell_volume_ratio: float = 0.0,
    breach_volume_confirm: float = 1.0,
    structural_stop_breached: bool = False,
    is_long: bool = True,
) -> HoldDecision:
    """Decide HOLD vs CUT for a post-entry breakout tick.

    Order of precedence (INVARIANT-A first):
      0. ``structural_stop_breached`` / max-loss => always CUT, even inside the floor.
      1. Inside the first grid-step (``held_secs < grid_secs``): a band breach is the
         expected first-wick shakeout — SUPPRESS it (return HOLD, ``suppressed_wick``).
         The hard stop above still overrides.
      2. Decisive flow-reversal (``flow_velocity < flow_reversal_threshold`` hard) =>
         CUT even if the band hasn't been breached: the buyers are gone.
      3. Volume-confirmed floor breach (bid below the vol-adaptive band AND
         ``sell_volume_ratio >= breach_volume_confirm``) => CUT.
      4. Otherwise HOLD.

    ``enabled=False`` is NOT handled here (the caller skips this module entirely and
    uses the legacy fixed-buffer ``breakout_failed_to_hold``); the parameter is kept
    so a caller can pass it through and assert the byte-identical contract in tests.
    """
    n_steps = hold_grid_steps(expected_hold_secs, grid_secs)

    try:
        lvl = float(breakout_level) if breakout_level is not None else 0.0
        b = float(bid) if bid is not None else 0.0
    except (TypeError, ValueError):
        lvl = b = 0.0

    band_frac = vol_adaptive_band(
        rv_live=rv_live, hold_bars=hold_bars, band_k=band_k, buffer_pct=buffer_pct, level=lvl
    )
    if is_long:
        threshold = lvl * (1.0 - band_frac)
        breached = lvl > 0.0 and b > 0.0 and b < threshold
    else:
        threshold = lvl * (1.0 + band_frac)
        breached = lvl > 0.0 and b > 0.0 and b > threshold

    # 0. INVARIANT-A: the structural stop / max-loss circuit ALWAYS fires, even inside
    #    the time-floor. Smart-hold can only tighten the exit, never loosen the hard stop.
    if structural_stop_breached:
        return HoldDecision(
            action="cut", reason="structural_stop", band_frac=band_frac,
            threshold=threshold, grid_steps=n_steps, suppressed_wick=False,
        )

    # 2. Decisive flow-reversal: buyers gone — cut before the band is even breached.
    reversed_flow = float(flow_velocity) < float(flow_reversal_threshold)
    if reversed_flow:
        return HoldDecision(
            action="cut", reason="flow_reversal", band_frac=band_frac,
            threshold=threshold, grid_steps=n_steps, suppressed_wick=False,
        )

    # 1. Time-floor: inside the first grid-step a band breach is the expected first
    #    wick — suppress it (the hard stop above already handled the real-stop case).
    in_time_floor = float(held_secs) < float(grid_secs)
    if breached and in_time_floor:
        return HoldDecision(
            action="hold", reason="time_floor_suppress_wick", band_frac=band_frac,
            threshold=threshold, grid_steps=n_steps, suppressed_wick=True,
        )

    # 3. Volume-confirmed floor breach.
    if breached:
        vol_confirms = float(sell_volume_ratio) >= float(breach_volume_confirm)
        if vol_confirms:
            return HoldDecision(
                action="cut", reason="volume_confirmed_breach", band_frac=band_frac,
                threshold=threshold, grid_steps=n_steps, suppressed_wick=False,
            )
        return HoldDecision(
            action="hold", reason="breach_no_volume_confirm", band_frac=band_frac,
            threshold=threshold, grid_steps=n_steps, suppressed_wick=False,
        )

    return HoldDecision(
        action="hold", reason="holding_above_band", band_frac=band_frac,
        threshold=threshold, grid_steps=n_steps, suppressed_wick=False,
    )
