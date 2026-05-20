"""Posterior-smoothed payoff-ratio sizing for autotrader entries.

The raw ``scan_patterns.payoff_ratio`` signal is useful, but treating exact
thresholds as cliffs creates brittle behavior: a pattern at 4.96:1 should not
size materially differently from one at 5.01:1. This module keeps the familiar
tier labels for audit logs while making the multiplier continuous and
confidence-weighted.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PayoffSizingDecision:
    tier: str
    multiplier: float
    observed_ratio: float | None
    observed_n: int
    adjusted_ratio: float | None
    confidence_weight: float
    method: str = "posterior_smoothed_piecewise_v1"

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "payoff_sizing_tier": self.tier,
            "payoff_sizing_multiplier": round(float(self.multiplier), 6),
            "payoff_ratio_observed": self.observed_ratio,
            "payoff_ratio_n_observed": self.observed_n,
            "payoff_ratio_adjusted": (
                round(float(self.adjusted_ratio), 6)
                if self.adjusted_ratio is not None
                else None
            ),
            "payoff_sizing_confidence_weight": round(
                float(self.confidence_weight), 6
            ),
            "payoff_sizing_method": self.method,
        }


def _finite_or_none(value: Any) -> float | None:
    try:
        out = float(value)
        if math.isfinite(out):
            return out
    except (TypeError, ValueError):
        pass
    return None


def _raw_piecewise_multiplier(
    adjusted_ratio: float,
    *,
    min_multiplier: float,
    max_multiplier: float,
) -> float:
    """Continuous map from payoff ratio to desired size before confidence.

    Anchors:
    - ratio <= 0: min multiplier
    - ratio == 1: neutral 1.0x
    - ratio == 2: halfway-to-high 1.25x when max is 1.5x
    - ratio >= 5: max multiplier
    """
    min_m = max(0.0, float(min_multiplier))
    max_m = max(1.0, float(max_multiplier))
    ratio = max(0.0, float(adjusted_ratio))

    if ratio < 1.0:
        return min(max_m, max(min_m, min_m + (1.0 - min_m) * ratio))

    high_mid = 1.0 + (max_m - 1.0) * 0.5
    if ratio < 2.0:
        return 1.0 + (high_mid - 1.0) * (ratio - 1.0)

    if ratio < 5.0:
        return high_mid + (max_m - high_mid) * ((ratio - 2.0) / 3.0)

    return max_m


def compute_payoff_sizing(
    *,
    payoff_ratio: Any,
    payoff_ratio_n: Any,
    min_n: int = 5,
    prior_ratio: float = 1.0,
    prior_n: int = 20,
    min_multiplier: float = 0.5,
    max_multiplier: float = 1.5,
) -> PayoffSizingDecision:
    """Return a confidence-weighted payoff sizing decision.

    ``prior_ratio`` and ``prior_n`` shrink thin samples toward neutral 1.0x.
    The final multiplier also blends the raw multiplier toward 1.0 by the
    confidence weight ``n / (n + prior_n)``. That keeps spectacular but
    seven-trade patterns from immediately receiving full size while still
    allowing mature edges to earn more capital.
    """
    ratio = _finite_or_none(payoff_ratio)
    try:
        n = max(0, int(payoff_ratio_n or 0))
    except (TypeError, ValueError):
        n = 0

    min_n_i = max(1, int(min_n or 1))
    prior_n_i = max(0, int(prior_n or 0))
    prior_ratio_f = _finite_or_none(prior_ratio)
    if prior_ratio_f is None:
        prior_ratio_f = 1.0

    if ratio is None or n < min_n_i:
        return PayoffSizingDecision(
            tier="insufficient_n",
            multiplier=1.0,
            observed_ratio=ratio,
            observed_n=n,
            adjusted_ratio=None,
            confidence_weight=0.0,
        )

    adjusted = (
        (max(0.0, ratio) * n + max(0.0, prior_ratio_f) * prior_n_i)
        / max(1, n + prior_n_i)
    )
    confidence = n / max(1, n + prior_n_i)

    if adjusted >= 5.0:
        tier = "very_high"
    elif adjusted >= 2.0:
        tier = "high"
    elif adjusted >= 1.0:
        tier = "moderate"
    else:
        tier = "low"

    raw_multiplier = _raw_piecewise_multiplier(
        adjusted,
        min_multiplier=min_multiplier,
        max_multiplier=max_multiplier,
    )
    multiplier = 1.0 + (raw_multiplier - 1.0) * confidence
    multiplier = min(float(max_multiplier), max(float(min_multiplier), multiplier))

    return PayoffSizingDecision(
        tier=tier,
        multiplier=multiplier,
        observed_ratio=ratio,
        observed_n=n,
        adjusted_ratio=adjusted,
        confidence_weight=confidence,
    )


__all__ = ["PayoffSizingDecision", "compute_payoff_sizing"]
