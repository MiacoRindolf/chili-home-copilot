"""Economic promotion metric for the ML model registry (Phase D, pure).

The legacy `ModelRegistry.check_shadow_vs_active` compares candidate vs
active using a **single** metric key (default ``oos_accuracy``). That is
dangerous: a model that is slightly more accurate but worse-calibrated
(higher Brier) or worse at generating expected PnL would still get
promoted.

This module defines a composite **economic score**

    economic_score = expected_pnl_per_trade - brier_penalty * oos_brier

and a comparator that refuses promotion when Brier regresses beyond
``max_brier_regression`` (calibration guardrail) even if expected PnL
looks fine. Both helpers are **pure** — same inputs, same outputs, no
I/O — and are exhaustively unit-tested.

The comparator **does not** auto-promote. It returns a decision dict so
the caller (Phase D shadow hook or a future cutover) can log and decide.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

_EXPECTED_PNL_KEYS = ("expected_pnl_oos_pct", "expected_pnl_per_trade")
_BRIER_KEYS = ("oos_brier_score", "brier_score", "oos_brier")


def _first_numeric(
    metrics: Mapping[str, Any] | None, keys: tuple[str, ...]
) -> float | None:
    if not metrics:
        return None
    for k in keys:
        v = metrics.get(k) if isinstance(metrics, Mapping) else None
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True)
class EconomicComparison:
    """Result of comparing shadow vs active under the economic metric."""

    better: bool
    reason: str
    active_expected_pnl: float | None
    shadow_expected_pnl: float | None
    active_brier: float | None
    shadow_brier: float | None
    active_economic_score: float | None
    shadow_economic_score: float | None
    economic_delta: float | None
    expected_pnl_delta: float | None
    brier_delta: float | None
    brier_regression_ok: bool
    min_improvement: float
    max_brier_regression: float


def compute_economic_score(
    expected_pnl_per_trade: float | None,
    oos_brier: float | None,
    *,
    brier_penalty: float = 1.0,
) -> float | None:
    """Scalar score for ranking candidates.

    Returns ``None`` if either ingredient is missing — callers must treat
    missing scores as "not comparable" and avoid promoting blindly.
    """
    if expected_pnl_per_trade is None or oos_brier is None:
        return None
    return float(expected_pnl_per_trade) - float(brier_penalty) * float(oos_brier)


def compare_economic(
    active_metrics: Mapping[str, Any] | None,
    shadow_metrics: Mapping[str, Any] | None,
    *,
    min_improvement: float = 0.0,
    max_brier_regression: float = 0.01,
    brier_penalty: float = 1.0,
) -> EconomicComparison:
    """Decide whether ``shadow`` is *economically* better than ``active``.

    Rules:
      1. Both sides must expose ``expected_pnl_*`` and ``*brier*`` metrics.
         Otherwise ``better=False, reason='missing_metric'``.
      2. The Brier guardrail comes first: if
         ``shadow_brier - active_brier > max_brier_regression`` (worse
         calibration), we reject regardless of expected PnL.
      3. Otherwise we compute the composite score and require
         ``shadow - active >= min_improvement``.
    """
    a_pnl = _first_numeric(active_metrics, _EXPECTED_PNL_KEYS)
    s_pnl = _first_numeric(shadow_metrics, _EXPECTED_PNL_KEYS)
    a_brier = _first_numeric(active_metrics, _BRIER_KEYS)
    s_brier = _first_numeric(shadow_metrics, _BRIER_KEYS)

    a_score = compute_economic_score(a_pnl, a_brier, brier_penalty=brier_penalty)
    s_score = compute_economic_score(s_pnl, s_brier, brier_penalty=brier_penalty)

    if s_score is None or a_score is None:
        return EconomicComparison(
            better=False,
            reason="missing_metric",
            active_expected_pnl=a_pnl,
            shadow_expected_pnl=s_pnl,
            active_brier=a_brier,
            shadow_brier=s_brier,
            active_economic_score=a_score,
            shadow_economic_score=s_score,
            economic_delta=None,
            expected_pnl_delta=(
                (s_pnl - a_pnl) if (s_pnl is not None and a_pnl is not None) else None
            ),
            brier_delta=(
                (s_brier - a_brier)
                if (s_brier is not None and a_brier is not None)
                else None
            ),
            brier_regression_ok=True,
            min_improvement=min_improvement,
            max_brier_regression=max_brier_regression,
        )

    brier_delta = s_brier - a_brier  # positive = worse
    brier_regression_ok = brier_delta <= max_brier_regression

    if not brier_regression_ok:
        return EconomicComparison(
            better=False,
            reason="brier_regression",
            active_expected_pnl=a_pnl,
            shadow_expected_pnl=s_pnl,
            active_brier=a_brier,
            shadow_brier=s_brier,
            active_economic_score=a_score,
            shadow_economic_score=s_score,
            economic_delta=s_score - a_score,
            expected_pnl_delta=s_pnl - a_pnl,
            brier_delta=brier_delta,
            brier_regression_ok=False,
            min_improvement=min_improvement,
            max_brier_regression=max_brier_regression,
        )

    economic_delta = s_score - a_score
    better = economic_delta >= min_improvement
    return EconomicComparison(
        better=better,
        reason="economic_improvement" if better else "insufficient_improvement",
        active_expected_pnl=a_pnl,
        shadow_expected_pnl=s_pnl,
        active_brier=a_brier,
        shadow_brier=s_brier,
        active_economic_score=a_score,
        shadow_economic_score=s_score,
        economic_delta=economic_delta,
        expected_pnl_delta=s_pnl - a_pnl,
        brier_delta=brier_delta,
        brier_regression_ok=True,
        min_improvement=min_improvement,
        max_brier_regression=max_brier_regression,
    )


__all__ = [
    "EconomicComparison",
    "compute_economic_score",
    "compare_economic",
]
