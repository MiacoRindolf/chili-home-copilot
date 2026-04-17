"""Phase M.2.a — pure tilt model.

Produces a *bounded* sizing multiplier from a
:class:`~app.services.trading.pattern_regime_ledger_lookup.ResolvedContext`
(M.1 ledger cells) so the position sizer can tilt toward historically-
favourable regimes and away from historically-punitive ones for a
given pattern.

This module is **pure**: no DB, no I/O, no settings access. All inputs
are passed in explicitly. Services are the ones that read
``brain_pattern_regime_tilt_*`` settings and call into here.

Contract (stable — tests and consumers may rely on all of these):

* Multiplier is clamped to ``[min_multiplier, max_multiplier]``.
* Insufficient confident coverage → multiplier ``1.0`` with reason
  ``insufficient_coverage`` (never tilts).
* All-zero / all-NaN expectancies → ``1.0`` with reason ``no_signal``.
* Deterministic: same ``ResolvedContext`` + same config → same output.
* Returns a :class:`TiltDecision` with full audit trail for the
  append-only log writer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

from .pattern_regime_ledger_lookup import ResolvedContext

__all__ = [
    "TiltConfig",
    "TiltDecision",
    "compute_tilt_multiplier",
    "classify_diff",
]


# ---------------------------------------------------------------------------
# Config + decision dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TiltConfig:
    """Bounds + coverage requirements."""

    min_multiplier: float = 0.25
    max_multiplier: float = 2.00
    min_confident_dimensions: int = 3
    # Cells whose expectancy magnitude is below this are treated as zero
    # (noise floor). Prevents 1e-6-expectancy cells from swinging tilts.
    noise_floor: float = 1e-5

    def __post_init__(self) -> None:
        if not math.isfinite(self.min_multiplier) or self.min_multiplier <= 0:
            raise ValueError("min_multiplier must be positive finite")
        if not math.isfinite(self.max_multiplier) or self.max_multiplier <= 0:
            raise ValueError("max_multiplier must be positive finite")
        if self.min_multiplier > self.max_multiplier:
            raise ValueError("min_multiplier must be <= max_multiplier")
        if self.min_confident_dimensions < 0:
            raise ValueError("min_confident_dimensions must be >= 0")
        if self.noise_floor < 0 or not math.isfinite(self.noise_floor):
            raise ValueError("noise_floor must be non-negative finite")


@dataclass(frozen=True)
class TiltDecision:
    """Full audit trail of the tilt decision."""

    multiplier: float
    reason_code: str
    # Accepted reason codes:
    #   "applied"
    #   "insufficient_coverage"
    #   "no_signal"
    #   "clamped_high"
    #   "clamped_low"
    #   "disabled"
    n_confident_dimensions: int
    contributing_dimensions: Dict[str, float] = field(default_factory=dict)
    mean_expectancy: Optional[float] = None
    clamped: bool = False
    fallback_used: bool = False


# ---------------------------------------------------------------------------
# Pure API
# ---------------------------------------------------------------------------


def compute_tilt_multiplier(
    ctx: ResolvedContext,
    *,
    config: TiltConfig,
) -> TiltDecision:
    """Main entry point. See module docstring for contract."""
    cells = ctx.cells_by_dimension or {}
    n_confident = len(cells)

    # Gate 1 — coverage. Not enough confident dimensions → no tilt.
    if n_confident < int(config.min_confident_dimensions):
        return TiltDecision(
            multiplier=1.0,
            reason_code="insufficient_coverage",
            n_confident_dimensions=n_confident,
            contributing_dimensions={},
            mean_expectancy=ctx.mean_expectancy(),
            clamped=False,
            fallback_used=True,
        )

    # Gate 2 — signal. Collect valid finite expectancies.
    contrib: Dict[str, float] = {}
    for dim, cell in cells.items():
        exp = cell.expectancy
        if exp is None:
            continue
        try:
            f = float(exp)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(f):
            continue
        if abs(f) < config.noise_floor:
            continue
        contrib[dim] = f

    if not contrib:
        return TiltDecision(
            multiplier=1.0,
            reason_code="no_signal",
            n_confident_dimensions=n_confident,
            contributing_dimensions={},
            mean_expectancy=ctx.mean_expectancy(),
            clamped=False,
            fallback_used=True,
        )

    # Raw multiplier: sigmoid-like mapping from mean expectancy to
    # [min, max]. A mean of 0 maps to 1.0; positive expectancies push
    # toward max, negative toward min. Scaling constant 40 was chosen
    # so a per-trade expectancy of +2.5 % maps to ~1.5x (near-saturated
    # for small samples).
    mean_exp = sum(contrib.values()) / len(contrib)
    raw = 1.0 + 2.0 * _sigmoid_centered(mean_exp * 40.0)
    # At mean_exp=0, raw=2.0 (bad!). Rescale using sigmoid_centered which
    # is 0 at 0, so raw = 1.0. See inner helper.
    raw = _expectancy_to_multiplier(mean_exp, config=config)

    clamped = False
    out = raw
    if out > config.max_multiplier:
        out = float(config.max_multiplier)
        clamped = True
        reason = "clamped_high"
    elif out < config.min_multiplier:
        out = float(config.min_multiplier)
        clamped = True
        reason = "clamped_low"
    else:
        reason = "applied"

    return TiltDecision(
        multiplier=float(out),
        reason_code=reason,
        n_confident_dimensions=n_confident,
        contributing_dimensions=contrib,
        mean_expectancy=mean_exp,
        clamped=clamped,
        fallback_used=False,
    )


def classify_diff(
    baseline_dollars: Optional[float],
    consumer_dollars: Optional[float],
    *,
    tolerance_bps: float = 25.0,
) -> str:
    """Classify the baseline vs consumer-sized notional difference.

    Returns one of: ``upsize``, ``downsize``, ``none``, ``unknown``.
    """
    if (
        baseline_dollars is None
        or consumer_dollars is None
        or baseline_dollars <= 0
    ):
        return "unknown"
    try:
        b = float(baseline_dollars)
        c = float(consumer_dollars)
    except (TypeError, ValueError):
        return "unknown"
    if not (math.isfinite(b) and math.isfinite(c)) or b <= 0:
        return "unknown"
    diff_bps = (c - b) / b * 10_000.0
    if diff_bps > tolerance_bps:
        return "upsize"
    if diff_bps < -tolerance_bps:
        return "downsize"
    return "none"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sigmoid_centered(x: float) -> float:
    """Sigmoid shifted so sigmoid_centered(0) == 0."""
    try:
        s = 1.0 / (1.0 + math.exp(-float(x)))
    except OverflowError:
        s = 1.0 if x > 0 else 0.0
    return s - 0.5


def _expectancy_to_multiplier(mean_exp: float, *, config: TiltConfig) -> float:
    """Smooth, monotone mapping of mean expectancy to a multiplier.

    Shape:
      * mean_exp <= 0    → multiplier in [min_multiplier, 1.0]
      * mean_exp == 0    → 1.0 exactly
      * mean_exp >= 0    → multiplier in [1.0, max_multiplier]
    Uses a tanh saturation with scaling constant k=40 so a 2.5 %
    expectancy reaches ~0.76 saturation and a 5 % expectancy ~0.96.
    """
    k = 40.0
    try:
        sat = math.tanh(k * float(mean_exp))
    except (ValueError, OverflowError):
        sat = 1.0 if mean_exp > 0 else -1.0
    if sat >= 0:
        span = float(config.max_multiplier) - 1.0
        return 1.0 + sat * span
    span = 1.0 - float(config.min_multiplier)
    return 1.0 + sat * span
