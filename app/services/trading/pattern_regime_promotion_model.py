"""Phase M.2.b — pure promotion-gate model.

Given a ``ResolvedContext`` (M.1 ledger cells) and a baseline
pass/fail decision from the existing promotion path, decide whether
the pattern should be promoted to live. The consumer only ever
*blocks* a baseline-allow; it never up-grades a baseline-block to an
allow (that would add risk under the authoritative contract).

Contract:
* Insufficient coverage → defers to baseline, ``reason=insufficient_coverage``.
* ``n_blocking_dimensions`` confident dims with expectancy below
  ``block_on_negative_expectancy_threshold`` → block.
* Overall ``mean_expectancy`` < ``min_mean_expectancy`` → block.
* Otherwise → allow (matches baseline).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .pattern_regime_ledger_lookup import ResolvedContext

__all__ = [
    "PromotionConfig",
    "PromotionDecision",
    "evaluate_promotion",
]


@dataclass(frozen=True)
class PromotionConfig:
    min_confident_dimensions: int = 3
    # A dimension counts as "blocking" when its expectancy is strictly
    # less than this threshold.
    block_on_negative_expectancy_threshold: float = 0.0
    # Minimum n_blocking_dimensions to block the promotion.
    min_blocking_dimensions: int = 2
    # Minimum mean expectancy across confident dimensions required
    # to allow promotion.
    min_mean_expectancy: float = 0.0

    def __post_init__(self) -> None:
        if self.min_confident_dimensions < 0:
            raise ValueError("min_confident_dimensions must be >= 0")
        if self.min_blocking_dimensions < 1:
            raise ValueError("min_blocking_dimensions must be >= 1")


@dataclass(frozen=True)
class PromotionDecision:
    consumer_allow: bool
    reason_code: str
    # Accepted reason codes:
    #   "baseline_matched"
    #   "baseline_deferred"
    #   "blocked_negative_dimensions"
    #   "blocked_low_mean_expectancy"
    #   "insufficient_coverage"
    #   "disabled"
    n_confident_dimensions: int
    blocking_dimensions: Dict[str, float] = field(default_factory=dict)
    mean_expectancy: Optional[float] = None
    fallback_used: bool = False


def evaluate_promotion(
    ctx: ResolvedContext,
    *,
    baseline_allow: Optional[bool],
    config: PromotionConfig,
) -> PromotionDecision:
    """Return the consumer's pass/fail decision.

    ``baseline_allow`` is the decision the existing promotion pipeline
    would have made. If ``None`` (unknown), the model falls through
    to its own evidence rather than propagating ``None``.
    """
    cells = ctx.cells_by_dimension or {}
    n_confident = len(cells)

    # Below coverage floor → defer entirely to baseline. Never block.
    if n_confident < int(config.min_confident_dimensions):
        allow = bool(baseline_allow) if baseline_allow is not None else True
        return PromotionDecision(
            consumer_allow=allow,
            reason_code="insufficient_coverage"
            if baseline_allow is None
            else "baseline_deferred",
            n_confident_dimensions=n_confident,
            blocking_dimensions={},
            mean_expectancy=ctx.mean_expectancy(),
            fallback_used=True,
        )

    # Never upgrade a baseline block.
    if baseline_allow is False:
        return PromotionDecision(
            consumer_allow=False,
            reason_code="baseline_matched",
            n_confident_dimensions=n_confident,
            blocking_dimensions={},
            mean_expectancy=ctx.mean_expectancy(),
            fallback_used=False,
        )

    # Compute blocking dimensions and overall mean expectancy.
    blocking: Dict[str, float] = {}
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
        if f < float(config.block_on_negative_expectancy_threshold):
            blocking[dim] = f

    mean_exp = ctx.mean_expectancy()

    if len(blocking) >= int(config.min_blocking_dimensions):
        return PromotionDecision(
            consumer_allow=False,
            reason_code="blocked_negative_dimensions",
            n_confident_dimensions=n_confident,
            blocking_dimensions=blocking,
            mean_expectancy=mean_exp,
            fallback_used=False,
        )

    if mean_exp is not None and mean_exp < float(config.min_mean_expectancy):
        return PromotionDecision(
            consumer_allow=False,
            reason_code="blocked_low_mean_expectancy",
            n_confident_dimensions=n_confident,
            blocking_dimensions=blocking,
            mean_expectancy=mean_exp,
            fallback_used=False,
        )

    return PromotionDecision(
        consumer_allow=True,
        reason_code="baseline_matched",
        n_confident_dimensions=n_confident,
        blocking_dimensions=blocking,
        mean_expectancy=mean_exp,
        fallback_used=False,
    )
