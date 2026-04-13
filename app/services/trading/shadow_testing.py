"""A/B shadow testing framework for strategy variants.

Runs new strategy variants in paper mode alongside current live strategies,
then uses statistical tests to determine if the new variant is significantly better.
"""
from __future__ import annotations

import json
import logging
import math
import random
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from ...models.trading import PaperTrade, ScanPattern

logger = logging.getLogger(__name__)

MIN_TRADES_FOR_COMPARISON = 30
MIN_DAYS_FOR_COMPARISON = 30
SIGNIFICANCE_LEVEL = 0.05


def create_shadow_test(
    db: Session,
    control_pattern_id: int,
    variant_pattern_id: int,
    *,
    min_trades: int = MIN_TRADES_FOR_COMPARISON,
    min_days: int = MIN_DAYS_FOR_COMPARISON,
) -> dict[str, Any]:
    """Register a shadow test between a control (live) pattern and a variant."""
    control = db.query(ScanPattern).filter(ScanPattern.id == control_pattern_id).first()
    variant = db.query(ScanPattern).filter(ScanPattern.id == variant_pattern_id).first()

    if not control or not variant:
        return {"ok": False, "error": "Pattern not found"}

    test_meta = {
        "shadow_test": {
            "control_id": control_pattern_id,
            "variant_id": variant_pattern_id,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "min_trades": min_trades,
            "min_days": min_days,
            "status": "running",
        }
    }

    existing_meta = variant.paper_book_json or {}
    existing_meta.update(test_meta)
    variant.paper_book_json = existing_meta
    db.commit()

    logger.info(
        "[shadow_test] Started: control=%d (%s) vs variant=%d (%s)",
        control.id, control.name, variant.id, variant.name,
    )

    return {
        "ok": True,
        "control": {"id": control.id, "name": control.name},
        "variant": {"id": variant.id, "name": variant.name},
        "min_trades": min_trades,
        "min_days": min_days,
    }


def evaluate_shadow_test(
    db: Session,
    control_pattern_id: int,
    variant_pattern_id: int,
) -> dict[str, Any]:
    """Compare control vs variant using statistical tests.

    Tests:
    1. Paired t-test on daily returns
    2. Bootstrap on Sharpe ratio difference
    3. Z-test on Sharpe ratios
    """
    control_trades = _get_closed_trades(db, control_pattern_id)
    variant_trades = _get_closed_trades(db, variant_pattern_id)

    if len(control_trades) < MIN_TRADES_FOR_COMPARISON:
        return {"ok": False, "reason": "insufficient_control_trades", "n": len(control_trades)}
    if len(variant_trades) < MIN_TRADES_FOR_COMPARISON:
        return {"ok": False, "reason": "insufficient_variant_trades", "n": len(variant_trades)}

    control_returns = [t.pnl_pct or 0 for t in control_trades]
    variant_returns = [t.pnl_pct or 0 for t in variant_trades]

    result: dict[str, Any] = {"ok": True}

    result["control_stats"] = _compute_strategy_stats(control_returns)
    result["variant_stats"] = _compute_strategy_stats(variant_returns)

    result["paired_ttest"] = _paired_return_ttest(control_returns, variant_returns)

    result["bootstrap_sharpe"] = _bootstrap_sharpe_difference(control_returns, variant_returns)

    result["sharpe_ztest"] = _sharpe_ratio_ztest(control_returns, variant_returns)

    tests_passed = sum(1 for t in ["paired_ttest", "bootstrap_sharpe", "sharpe_ztest"]
                       if result[t].get("significant", False))
    result["tests_passed"] = tests_passed
    result["promote_variant"] = tests_passed >= 2
    result["recommendation"] = (
        "PROMOTE variant" if result["promote_variant"]
        else "KEEP control (insufficient evidence)"
    )

    logger.info(
        "[shadow_test] Control=%d vs Variant=%d: %d/3 tests passed -> %s",
        control_pattern_id, variant_pattern_id, tests_passed, result["recommendation"],
    )

    return result


def _get_closed_trades(db: Session, pattern_id: int) -> list[PaperTrade]:
    return (
        db.query(PaperTrade)
        .filter(
            PaperTrade.scan_pattern_id == pattern_id,
            PaperTrade.status == "closed",
        )
        .order_by(PaperTrade.exit_date.asc())
        .all()
    )


def _compute_strategy_stats(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {}
    n = len(returns)
    mean_ret = sum(returns) / n
    var_ret = sum((r - mean_ret) ** 2 for r in returns) / max(n - 1, 1)
    std_ret = math.sqrt(var_ret)
    wins = sum(1 for r in returns if r > 0)
    sharpe = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0
    max_dd = _max_drawdown(returns)

    return {
        "n": n,
        "mean_return": round(mean_ret, 4),
        "std_return": round(std_ret, 4),
        "win_rate": round(wins / n * 100, 1),
        "sharpe_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd, 2),
    }


def _max_drawdown(returns: list[float]) -> float:
    cumulative = 0
    peak = 0
    max_dd = 0
    for r in returns:
        cumulative += r
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _paired_return_ttest(
    control: list[float],
    variant: list[float],
) -> dict[str, Any]:
    """Paired t-test on matched daily returns (or trade returns if not daily-aligned)."""
    min_n = min(len(control), len(variant))
    c = control[:min_n]
    v = variant[:min_n]

    diffs = [v[i] - c[i] for i in range(min_n)]
    mean_diff = sum(diffs) / min_n
    var_diff = sum((d - mean_diff) ** 2 for d in diffs) / max(min_n - 1, 1)
    se = math.sqrt(var_diff / min_n) if var_diff > 0 else 1e-10
    t_stat = mean_diff / se

    df = min_n - 1
    p_value = _t_to_p(abs(t_stat), df) * 2

    return {
        "t_statistic": round(t_stat, 4),
        "p_value": round(p_value, 6),
        "mean_diff": round(mean_diff, 4),
        "n_pairs": min_n,
        "significant": p_value < SIGNIFICANCE_LEVEL,
        "variant_better": mean_diff > 0,
    }


def _bootstrap_sharpe_difference(
    control: list[float],
    variant: list[float],
    n_resamples: int = 1000,
) -> dict[str, Any]:
    """Bootstrap CI on Sharpe ratio difference (variant - control)."""
    rng = random.Random(42)
    diffs = []

    for _ in range(n_resamples):
        c_sample = rng.choices(control, k=len(control))
        v_sample = rng.choices(variant, k=len(variant))
        c_sharpe = _sharpe(c_sample)
        v_sharpe = _sharpe(v_sample)
        diffs.append(v_sharpe - c_sharpe)

    diffs.sort()
    lo = diffs[int(0.025 * n_resamples)]
    hi = diffs[int(0.975 * n_resamples)]
    mean_diff = sum(diffs) / n_resamples

    significant = lo > 0

    return {
        "mean_sharpe_diff": round(mean_diff, 4),
        "ci_lower": round(lo, 4),
        "ci_upper": round(hi, 4),
        "significant": significant,
        "variant_better": mean_diff > 0,
    }


def _sharpe_ratio_ztest(
    control: list[float],
    variant: list[float],
) -> dict[str, Any]:
    """Z-test comparing two Sharpe ratios (Jobson-Korkie test)."""
    c_sharpe = _sharpe(control)
    v_sharpe = _sharpe(variant)
    n_c = len(control)
    n_v = len(variant)

    se_c = math.sqrt((1 + c_sharpe ** 2 / 2) / max(n_c - 1, 1))
    se_v = math.sqrt((1 + v_sharpe ** 2 / 2) / max(n_v - 1, 1))
    se_diff = math.sqrt(se_c ** 2 + se_v ** 2) if (se_c ** 2 + se_v ** 2) > 0 else 1e-10

    z = (v_sharpe - c_sharpe) / se_diff
    p_value = 2 * (1 - _norm_cdf(abs(z)))

    return {
        "control_sharpe": round(c_sharpe, 4),
        "variant_sharpe": round(v_sharpe, 4),
        "z_statistic": round(z, 4),
        "p_value": round(p_value, 6),
        "significant": p_value < SIGNIFICANCE_LEVEL,
        "variant_better": v_sharpe > c_sharpe,
    }


def _sharpe(returns: list[float]) -> float:
    if not returns:
        return 0.0
    mean_r = sum(returns) / len(returns)
    var_r = sum((r - mean_r) ** 2 for r in returns) / max(len(returns) - 1, 1)
    std_r = math.sqrt(var_r)
    return (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0.0


def _norm_cdf(x: float) -> float:
    """Approximation of the normal CDF."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _t_to_p(t: float, df: int) -> float:
    """Approximate one-sided p-value from t-statistic using normal approximation for large df."""
    if df <= 0:
        return 1.0
    if df > 30:
        return 1 - _norm_cdf(t)
    z = t * (1 - 1 / (4 * df)) / math.sqrt(1 + t ** 2 / (2 * df))
    return 1 - _norm_cdf(z)
