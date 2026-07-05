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
    Returns ``{target_r, n, pctl_r, shrink_w, source}``."""
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
