"""Phase M.2.c — pure kill-switch / auto-quarantine model.

Evaluates whether a pattern should be quarantined (``transition_on_decay``)
based on *consecutive-day* negative expectancy across the M.1 ledger.
Single-day flakes never trigger: the consecutive-day threshold is a
first-class input.

Contract:

* Quarantine fires only when *every* day in the last
  ``consecutive_days_negative`` evaluations (inclusive of today) has
  at least one confident dimension with expectancy below the threshold
  AND at least one confident dimension overall. Gaps in evaluation
  history (no row for a day) break the streak.
* Below coverage floor → no-op, ``reason=insufficient_coverage``.
* Circuit breaker: consumers should check ``recent_quarantine_count``
  externally (DB lookup) and pass ``at_circuit_breaker=True`` when a
  pattern has already been quarantined >= N times in the rolling
  window. This model respects the flag and returns a
  ``circuit_breaker`` decision.

The model is otherwise pure: no DB, no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from .pattern_regime_ledger_lookup import ResolvedContext

__all__ = [
    "KillSwitchConfig",
    "KillSwitchDecision",
    "DailyExpectancyPoint",
    "evaluate_killswitch",
    "compute_consecutive_streak",
]


@dataclass(frozen=True)
class KillSwitchConfig:
    consecutive_days_negative: int = 3
    neg_expectancy_threshold: float = -0.005
    min_confident_dimensions: int = 3
    max_per_pattern_30d: int = 1

    def __post_init__(self) -> None:
        if self.consecutive_days_negative < 1:
            raise ValueError("consecutive_days_negative must be >= 1")
        if self.min_confident_dimensions < 0:
            raise ValueError("min_confident_dimensions must be >= 0")
        if self.max_per_pattern_30d < 1:
            raise ValueError("max_per_pattern_30d must be >= 1")


@dataclass(frozen=True)
class DailyExpectancyPoint:
    """One day of prior ledger aggregate used by the streak calculator."""

    as_of_date: date
    n_confident_dimensions: int
    mean_expectancy: Optional[float]
    worst_dimension: Optional[str] = None
    worst_expectancy: Optional[float] = None


@dataclass(frozen=True)
class KillSwitchDecision:
    consumer_quarantine: bool
    reason_code: str
    # Accepted reason codes:
    #   "quarantine"
    #   "negative_but_streak_too_short"
    #   "healthy"
    #   "insufficient_coverage"
    #   "circuit_breaker"
    #   "disabled"
    consecutive_days_negative: int
    worst_dimension: Optional[str] = None
    worst_expectancy: Optional[float] = None
    n_confident_dimensions: int = 0
    fallback_used: bool = False
    streak_points: Tuple[DailyExpectancyPoint, ...] = field(default_factory=tuple)


def compute_consecutive_streak(
    points: List[DailyExpectancyPoint],
    *,
    threshold: float,
) -> int:
    """Count the trailing streak of days where ``mean_expectancy < threshold``.

    ``points`` MUST be ordered by ``as_of_date`` ASCENDING (oldest
    first). The streak counts back from the latest point. A day whose
    ``mean_expectancy is None`` breaks the streak (treat missing as
    not-confirmed-negative). A day without confident coverage also
    breaks the streak.
    """
    if not points:
        return 0
    streak = 0
    for point in reversed(points):
        if point.n_confident_dimensions <= 0:
            break
        if point.mean_expectancy is None:
            break
        try:
            v = float(point.mean_expectancy)
        except (TypeError, ValueError):
            break
        if not math.isfinite(v):
            break
        if v < float(threshold):
            streak += 1
        else:
            break
    return streak


def evaluate_killswitch(
    ctx: ResolvedContext,
    *,
    history: List[DailyExpectancyPoint],
    config: KillSwitchConfig,
    at_circuit_breaker: bool = False,
    baseline_status: Optional[str] = None,
) -> KillSwitchDecision:
    """Return the quarantine decision.

    ``history`` is the full window (oldest first) of prior daily
    aggregates for this pattern, INCLUDING today's row if already
    computed. If today's aggregate is not yet in history, the caller
    should synthesize it from ``ctx`` and append.

    ``at_circuit_breaker`` indicates that the pattern has already
    been quarantined at least ``max_per_pattern_30d`` times in the
    rolling 30-day window and should not be quarantined again.
    """
    n_confident = ctx.n_confident_dimensions

    if at_circuit_breaker:
        return KillSwitchDecision(
            consumer_quarantine=False,
            reason_code="circuit_breaker",
            consecutive_days_negative=0,
            n_confident_dimensions=n_confident,
            fallback_used=True,
        )

    if n_confident < int(config.min_confident_dimensions):
        return KillSwitchDecision(
            consumer_quarantine=False,
            reason_code="insufficient_coverage",
            consecutive_days_negative=0,
            n_confident_dimensions=n_confident,
            fallback_used=True,
        )

    streak = compute_consecutive_streak(
        history, threshold=float(config.neg_expectancy_threshold)
    )

    # Find the worst dimension in ctx (most-negative confident cell).
    worst_dim: Optional[str] = None
    worst_exp: Optional[float] = None
    for dim, cell in ctx.cells_by_dimension.items():
        exp = cell.expectancy
        if exp is None:
            continue
        try:
            f = float(exp)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(f):
            continue
        if worst_exp is None or f < worst_exp:
            worst_exp = f
            worst_dim = dim

    today_points = list(history[-int(config.consecutive_days_negative):])

    if streak >= int(config.consecutive_days_negative):
        return KillSwitchDecision(
            consumer_quarantine=True,
            reason_code="quarantine",
            consecutive_days_negative=streak,
            worst_dimension=worst_dim,
            worst_expectancy=worst_exp,
            n_confident_dimensions=n_confident,
            fallback_used=False,
            streak_points=tuple(today_points),
        )

    mean_exp_today = ctx.mean_expectancy()
    if (
        mean_exp_today is not None
        and mean_exp_today < float(config.neg_expectancy_threshold)
    ):
        reason = "negative_but_streak_too_short"
    else:
        reason = "healthy"

    return KillSwitchDecision(
        consumer_quarantine=False,
        reason_code=reason,
        consecutive_days_negative=streak,
        worst_dimension=worst_dim,
        worst_expectancy=worst_exp,
        n_confident_dimensions=n_confident,
        fallback_used=False,
        streak_points=tuple(today_points),
    )
