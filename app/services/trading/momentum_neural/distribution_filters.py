"""Distribution / thick-tape veto (HVM101) + non-monotonic volume preference (SCAL101).

Two SELECTION refinements for the Ross momentum lane, each a small ADDITIVE viability
delta (flag-off OR input-absent => exactly 0.0 => byte-identical score). Both are
ADAPTIVE — thresholds are batch-percentiles / ATR-relative, never absolute magic
numbers. Both fail OPEN on thin/degenerate data (a missing or unparseable field is
never a veto). The only irreducible knobs are the two documented soft-discount
magnitudes below (one per edge), mirroring ``ross_momentum.ROSS_QUALITY_VIABILITY_TILT``.

HVM101 — thick-tape / distribution veto
    A name printing HIGH cumulative (relative) volume with ~NO net price progress is
    churning into supply (distribution / rejection at a level), not breaking out. We
    measure progress-per-unit-volume = |net change %| / RVOL and DISCOUNT names that
    sit in the low tail of that ratio WHILE also being high-RVOL (the "thick tape").
    Soft discount (bounded, scaled by tail depth), never a hard cut — low over-veto.

SCAL101 — non-monotonic (inverted-U) volume preference
    Where viability rewards RVOL monotonically (more volume == strictly better), the
    "most obvious" stock has the HIGHEST volume but extreme volume == choppy / late /
    crowded. Apply a mild PEAKED roll-off: only the EXTREME upper RVOL tail loses a
    little (it never goes negative vs a mid-RVOL name's existing reward, and the body
    of the distribution is untouched), so the existing monotonic signal is softened at
    the tail, NEVER inverted.
"""

from __future__ import annotations

import bisect
from typing import Any

# ── irreducible bases (ONE documented magnitude per edge) ────────────────────────
# Max soft viability discount for a fully-distribution thick-tape name. Bounded and
# scaled DOWN by how shallow the low-progress-per-volume tail / high-RVOL condition
# is, so a borderline name barely moves. Same order of magnitude as the existing
# microstructure de-rates (-0.03..-0.05).
THICK_TAPE_MAX_DISCOUNT = 0.05

# Max soft roll-off applied ONLY to the extreme upper RVOL tail (SCAL101). Smaller
# than the thick-tape discount: this only "softens" an over-rewarded outlier, it must
# never out-weigh the legitimate explosiveness preference for a mid-high RVOL mover.
NONMONOTONIC_MAX_ROLLOFF = 0.03

# RVOL percentile at/above which a name is considered "thick tape eligible" (HVM101)
# and "extreme tail" (SCAL101). Adaptive bar = within-batch percentile, not absolute.
_HIGH_RVOL_PCTL = 0.70
# Progress-per-volume percentile at/below which a high-RVOL name is "distribution".
_LOW_PROGRESS_PCTL = 0.30
# SCAL101 roll-off engages only ABOVE this RVOL percentile (the extreme tail only).
_EXTREME_RVOL_PCTL = 0.85
# SCAL101 IGNITER EXEMPTION: a name that is BOTH in the extreme RVOL tail AND in the
# upper tail of day-change is a genuine Ross igniter (the explosive name the lane
# WANTS) — the inverted-U must never soften it. Above this day-change percentile the
# exemption ramps in and fully cancels the roll-off at the very top. Adaptive bar =
# within-batch percentile, not absolute. Fail-open: no change data ⇒ no exemption,
# byte-identical to the prior roll-off.
_IGNITER_CHANGE_PCTL = 0.70

# Schema-tolerant field aliases (mirror ross_momentum._extract_pillars).
_RVOL_KEYS = ("vol_ratio", "rvol", "volume_ratio")
_CHANGE_KEYS = ("daily_change_pct", "change_24h", "change_pct", "todays_change_perc", "gap_pct")


def _to_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_float(signal: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        fv = _to_float(signal.get(k))
        if fv is not None:
            return fv
    return None


def _percentile_rank(value: float, sorted_vals: list[float]) -> float | None:
    """Fraction of the batch at-or-below ``value`` in [0,1]. None when the batch is
    too small to define a percentile (fail-open: caller treats None as no-signal)."""
    n = len(sorted_vals)
    if n < 4:  # degenerate batch -> no adaptive bar -> no-op
        return None
    return bisect.bisect_right(sorted_vals, value) / n


def _rvol_of(sig: Any) -> float | None:
    if not isinstance(sig, dict):
        return None
    rv = _first_float(sig, _RVOL_KEYS)
    if rv is None or rv <= 0:
        return None
    return rv


def _abs_change_of(sig: Any) -> float | None:
    if not isinstance(sig, dict):
        return None
    ch = _first_float(sig, _CHANGE_KEYS)
    if ch is None:
        return None
    return abs(ch)


def _progress_per_volume(sig: Any) -> float | None:
    """|net change %| per unit RVOL. Low == high volume, ~no net progress (churn)."""
    rv = _rvol_of(sig)
    ach = _abs_change_of(sig)
    if rv is None or ach is None:
        return None
    return ach / rv


def thick_tape_discount(
    symbol: str,
    ross_signals: Any,
    *,
    atr_pct: float | None = None,
) -> float:
    """HVM101 soft distribution discount in [-THICK_TAPE_MAX_DISCOUNT, 0.0].

    Returns 0.0 (no-op) when: signals absent / not a dict, this symbol has no parseable
    RVOL+change, the batch is too small for a percentile, the name is NOT high-RVOL, or
    its progress-per-volume is NOT in the low tail. Equity-only callers gate the
    ``-USD`` exemption upstream; this fn is symbol-agnostic and additive.

    Adaptive: both the "high volume" and "no progress" bars are within-batch
    percentiles; the discount magnitude is scaled by how deep into the low-progress
    tail the name sits AND (optionally) damped UP when ATR is rich (a wide-range
    regime makes flat progress more clearly distribution). Never a hard cut.
    """
    if not isinstance(ross_signals, dict) or not ross_signals:
        return 0.0
    try:
        sig = ross_signals.get(symbol)
        rv = _rvol_of(sig)
        ppv = _progress_per_volume(sig)
        if rv is None or ppv is None:
            return 0.0  # fail-open on thin data for this symbol

        rvols = sorted(
            r for r in (_rvol_of(s) for s in ross_signals.values()) if r is not None
        )
        ppvs = sorted(
            p for p in (_progress_per_volume(s) for s in ross_signals.values()) if p is not None
        )
        rv_pct = _percentile_rank(rv, rvols)
        ppv_pct = _percentile_rank(ppv, ppvs)
        if rv_pct is None or ppv_pct is None:
            return 0.0  # degenerate batch -> no-op

        # Only fire when BOTH: high relative volume AND low net progress for that volume.
        if rv_pct < _HIGH_RVOL_PCTL or ppv_pct > _LOW_PROGRESS_PCTL:
            return 0.0

        # Tail depth in [0,1]: 0 at the threshold edge, 1 at the very bottom of progress.
        depth = (_LOW_PROGRESS_PCTL - ppv_pct) / _LOW_PROGRESS_PCTL
        depth = max(0.0, min(1.0, depth))
        # Volume conviction in [0,1]: stronger when RVOL is deep in the high tail.
        vol_conv = (rv_pct - _HIGH_RVOL_PCTL) / max(1e-9, (1.0 - _HIGH_RVOL_PCTL))
        vol_conv = max(0.0, min(1.0, vol_conv))

        # ATR-relative damp: a richer-range regime makes flat progress more clearly
        # distribution (vs a quiet tape where small progress is normal). Bounded [0.5,1.0]
        # multiplier; absent ATR -> neutral 1.0 (fail-open / byte-identical to no damp).
        atr_mult = 1.0
        a = _to_float(atr_pct)
        if a is not None and a > 0:
            # map atr_pct ~[0,5%] -> [0.5,1.0]; clamp. No hard threshold, smooth.
            atr_mult = 0.5 + 0.5 * max(0.0, min(1.0, a / 5.0))

        mag = THICK_TAPE_MAX_DISCOUNT * (0.5 * depth + 0.5 * vol_conv) * atr_mult
        return -max(0.0, min(THICK_TAPE_MAX_DISCOUNT, mag))
    except (TypeError, ValueError, AttributeError, ZeroDivisionError):
        return 0.0  # fail-open: a bug never blocks a real mover


def nonmonotonic_volume_rolloff(symbol: str, ross_signals: Any) -> float:
    """SCAL101 inverted-U roll-off in [-NONMONOTONIC_MAX_ROLLOFF, 0.0].

    Returns 0.0 (no-op) unless this symbol's RVOL is in the EXTREME upper tail of the
    batch (>= _EXTREME_RVOL_PCTL). For tail names, applies a mild peaked roll-off that
    GROWS toward the very top of the distribution — gently penalizing the "too obvious /
    too crowded" extreme without ever inverting the existing monotonic RVOL reward (the
    roll-off is capped well below the per-percentile slope of that reward, and the body
    of the distribution is untouched -> 0.0). Adaptive (batch percentile). Fail-open.

    IGNITER EXEMPTION: an extreme-RVOL name that is ALSO in the upper tail of day-change
    is a genuine Ross igniter (very high volume AND a large real move) — exactly the
    explosive name the lane is built to catch. The inverted-U exists to soften the
    "most obvious, mid-high-volume, ordinary" name; it must NEVER bite the explosive
    tail. So the roll-off is faded out smoothly by how deep the name sits in the
    day-change tail: at/above the top of the change distribution the roll-off is fully
    cancelled (returns 0.0). Adaptive (within-batch change percentile). Fail-open: no
    parseable change for this symbol or the batch ⇒ no exemption (prior roll-off
    unchanged, byte-identical).
    """
    if not isinstance(ross_signals, dict) or not ross_signals:
        return 0.0
    try:
        rv = _rvol_of(ross_signals.get(symbol))
        if rv is None:
            return 0.0
        rvols = sorted(
            r for r in (_rvol_of(s) for s in ross_signals.values()) if r is not None
        )
        rv_pct = _percentile_rank(rv, rvols)
        if rv_pct is None or rv_pct < _EXTREME_RVOL_PCTL:
            return 0.0  # body of the distribution -> untouched

        # Tail position in [0,1]: 0 at the extreme-tail edge, 1 at the top.
        t = (rv_pct - _EXTREME_RVOL_PCTL) / max(1e-9, (1.0 - _EXTREME_RVOL_PCTL))
        t = max(0.0, min(1.0, t))
        # Smooth (quadratic) ramp so the roll-off is gentle near the edge and only
        # bites at the very crowded top — a mild inverted-U right-side, not a cliff.
        rolloff = -NONMONOTONIC_MAX_ROLLOFF * (t * t)

        # IGNITER EXEMPTION (adaptive): fade the roll-off out by how far this name sits
        # in the upper tail of day-change. keep_frac in [0,1] = 1.0 below the change
        # tail (full prior roll-off), ramping to 0.0 at the very top of the change
        # distribution (fully exempt — a real explosive igniter is never softened).
        ach = _abs_change_of(ross_signals.get(symbol))
        if ach is not None:
            achs = sorted(
                a for a in (_abs_change_of(s) for s in ross_signals.values()) if a is not None
            )
            ch_pct = _percentile_rank(ach, achs)
            if ch_pct is not None and ch_pct > _IGNITER_CHANGE_PCTL:
                exempt = (ch_pct - _IGNITER_CHANGE_PCTL) / max(1e-9, (1.0 - _IGNITER_CHANGE_PCTL))
                keep_frac = 1.0 - max(0.0, min(1.0, exempt))
                rolloff *= keep_frac
        return rolloff
    except (TypeError, ValueError, AttributeError, ZeroDivisionError):
        return 0.0
