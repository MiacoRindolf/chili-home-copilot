"""Data-derived exit-target calibration (MFE/MAE, no-magic).

The first-partial target is currently a fixed reward:risk (rr_cap=6 / room_capture=0.5 magic).
The principled replacement (Sweeney's Maximum Favorable Excursion; López de Prado triple-barrier)
sets the target from the DISTRIBUTION of realized MFE across similar setups — a percentile of the
tape's OWN favorable excursion, EWMA/sample-updated per setup family, NOT an invented R multiple.

This module is PURE (no I/O). Phase 1 = ``realized_excursion_r`` (compute the per-trade MFE_R/MAE_R
the caller LOGS at exit to accumulate the distribution). Phase 2 = ``mfe_percentile_target_r``
(derive the shadow first-partial target from that accumulated sample; the caller LOGS it beside the
live magic target — SHADOW ONLY, no behavior change — until enough samples prove it out).

The ONE irreducible documented base per concept:
  * the excursion is in R-UNITS off the trade's own frozen entry-stop distance (no price magic);
  * the target percentile ``p`` (~0.5-0.66) is the single knob; everything else is the realized data;
  * a SHRINKAGE prior toward the current base R:R until ``min_samples`` (small-sample robustness).
"""
from __future__ import annotations

import math
from typing import Any


def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


# The exit reasons that mean "we left AT the first target". ``target`` is the whole-position
# flatten on a lane that cannot split; ``scale_out_target`` / ``scale_out_limit`` are the
# partial forms. All three stop the high-water mark at the target, so all three censor the MFE.
_TARGET_EXIT_REASONS = frozenset({"target", "scale_out_target", "scale_out_limit"})


def realized_excursion_r(
    *,
    entry: float,
    stop_distance: float,
    high_water_mark: float | None,
    exit_price: float,
    original_target: float | None = None,
    low_water_mark: float | None = None,
    side_long: bool = True,
) -> dict[str, Any] | None:
    """Per-trade excursion in R-units off the FROZEN entry-stop distance (the trade's own risk).

    Returns ``{mfe_r, realized_r, target_r?, mae_r?}`` (rounded), or ``None`` on bad inputs.
      * ``mfe_r``      = (high_water_mark − entry) / stop_distance  — how far the trade COULD have run
      * ``realized_r`` = (exit_price − entry) / stop_distance       — what we actually banked
      * ``target_r``   = (original_target − entry) / stop_distance  — where the (magic) first target sat
      * ``mae_r``      = (entry − low_water_mark) / stop_distance    — worst adverse excursion (heat)
    Long-only for now (the lane is long); shorts return None. Pure; no I/O."""
    if not side_long:
        return None
    e = _f(entry)
    sd = _f(stop_distance)
    if e is None or sd is None or sd <= 0:
        return None
    out: dict[str, Any] = {}
    hwm = _f(high_water_mark)
    if hwm is not None:
        out["mfe_r"] = round(max(0.0, (hwm - e) / sd), 3)
    xp = _f(exit_price)
    if xp is not None:
        out["realized_r"] = round((xp - e) / sd, 3)
    ot = _f(original_target)
    if ot is not None:
        out["target_r"] = round((ot - e) / sd, 3)
    lwm = _f(low_water_mark)
    if lwm is not None:
        out["mae_r"] = round(max(0.0, (e - lwm) / sd), 3)
    return out or None


def mfe_sample_truncated_by_target(
    exit_reason: Any,
    mfe_r: Any,
    target_r: Any,
    *,
    tolerance_frac: float = 0.02,
) -> bool:
    """Was this leg's realized MFE CENSORED by the first target itself?

    True when the leg left at the target level (``exit_reason`` in the target family) and its
    recorded high-water mark never got meaningfully past that target. Such a sample is
    RIGHT-CENSORED: the true excursion is ``>= mfe_r`` and unknowable, because closing the
    position is what stopped the high-water mark advancing. Feeding it to a percentile that is
    supposed to LIFT the target is circular — the target would be learning its own footprint,
    and the lower it went the more of the pool it would truncate.

    ``tolerance_frac`` is the slack allowed above the target before we call it a real
    excursion (the live trigger fires at ``target*(1-0.005)`` and the receipt rounds to 3
    decimals, so an exact equality test would miss most of them). Pure; no I/O.

    Back-compatible by construction: it is computed from fields ``momentum_mfe_realized``
    has always carried (``exit_reason``, ``mfe_r``, ``target_r``), so history is classified
    the same way as new rows."""
    if str(exit_reason or "").strip().lower() not in _TARGET_EXIT_REASONS:
        return False
    m = _f(mfe_r)
    t = _f(target_r)
    if m is None or t is None or t <= 0:
        return False
    try:
        tol = float(tolerance_frac)
    except (TypeError, ValueError):
        tol = 0.02
    if not math.isfinite(tol) or tol < 0:
        tol = 0.02
    return m <= t * (1.0 + tol)


def mfe_percentile_target_r(
    mfe_samples: list[float],
    *,
    percentile: float,
    base_rr: float,
    min_samples: int,
) -> dict[str, Any]:
    """SHADOW first-partial target in R = a percentile of the realized-MFE sample, SHRUNK toward
    ``base_rr`` until ``min_samples`` accumulate (small-sample robustness — the current magic R:R
    is a robust PRIOR we lean on until the tape's own distribution is trustworthy).

    target_r = w · percentile_p(mfe_samples) + (1 − w) · base_rr,  w = min(1, n / min_samples)

    ``percentile`` (0..1) is the ONE documented base (the fraction of the proven favorable run we
    aim the first partial at — sell earlier at a lower p, ride later at a higher p). Never below
    ``base_rr`` (Ross's floor: don't first-scale below the plan's minimum R:R). Pure; no I/O.
    Returns ``{target_r, n, pctl_r, shrink_w, source}``.

    [27b] 2026-09-10 — ``base_rr`` IS NOW THE FIRST-PARTIAL LEVEL (``first_partial_target_r``,
    0.7R), not the plan R:R (2.5). Nothing here changes; what changes is the FLOOR's height, and
    it is worth being explicit about what that does to the live per-family numbers. Measured on
    ``momentum_mfe_target_applied`` (live, 2026-09-10): every deep family's 60th-percentile MFE
    sits at pctl_r 0.00–0.23 on n = 14–16 (momentum_ok_rel_vol 0.154, abcd_break_tick_ok 0.000,
    momentum_continuation 0.134, momentum_ok_rel_vol_rate 0.229) — so with w = n/30 ≈ 0.5 the
    blend lands near 0.42 and ``max(base, blended)`` returns the base 0.7 unchanged. The one
    family that lifts is momentum_ok_tick_surge (n = 2, pctl_r 7.040 → 1.122).

    ⚠️ THE CENSORING THAT WOULD HAVE MADE THIS A ONE-WAY RATCHET (review 2026-09-10). The
    samples come from ``momentum_mfe_realized``, whose ``mfe_r`` is the position's high-water
    mark AT EXIT — and the HWM stops advancing the moment the position closes. On the only live
    execution family the first target closes the WHOLE position, so every ``target`` exit would
    record ``mfe_r ≈ the applied target``, and a low target is reached far more often than a
    high one. The pool would then be unable to produce a percentile above the level that
    truncated it: ``max(base, blended)`` pinned at the base forever, a lift that can only
    ratchet DOWN. The fix is upstream of this function and is a SAMPLE-SELECTION fix:
    ``mfe_sample_truncated_by_target`` marks those legs and ``_recent_mfe_samples`` drops them,
    because a right-censored observation is not evidence about where the MFE would have gone.
    Those samples are ALSO known to under-read the tape (AUUD 09-01 recorded peak 0.00 while
    the prints crossed 0.3R) — a separate defect in ``momentum_mfe_realized``."""
    try:
        p = max(0.0, min(1.0, float(percentile)))
    except (TypeError, ValueError):
        p = 0.6
    try:
        base = float(base_rr)
        if not math.isfinite(base) or base <= 0:
            base = 2.0
    except (TypeError, ValueError):
        base = 2.0
    try:
        need = max(1, int(min_samples))
    except (TypeError, ValueError):
        need = 30
    xs = sorted(x for x in (_f(v) for v in (mfe_samples or [])) if x is not None and x >= 0.0)
    n = len(xs)
    if n == 0:
        return {"target_r": round(base, 3), "n": 0, "pctl_r": None, "shrink_w": 0.0, "source": "prior_only"}
    # nearest-rank percentile (robust, no interpolation blow-up on tiny samples)
    idx = min(n - 1, max(0, int(math.ceil(p * n)) - 1))
    pctl_r = xs[idx]
    w = min(1.0, n / float(need))
    target_r = w * pctl_r + (1.0 - w) * base
    target_r = max(base, target_r)  # never first-scale below the plan floor
    return {
        "target_r": round(target_r, 3),
        "n": n,
        "pctl_r": round(pctl_r, 3),
        "shrink_w": round(w, 3),
        "source": "blended" if w < 1.0 else "data",
    }
