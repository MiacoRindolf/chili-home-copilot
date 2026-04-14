"""A/B shadow testing framework for strategy variants.

Runs new strategy variants in paper mode alongside current live strategies,
then uses statistical tests to determine if the new variant is significantly better.
"""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime
from typing import Any

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

    control_returns, control_hold_days = _extract_trade_returns(control_trades)
    variant_returns, variant_hold_days = _extract_trade_returns(variant_trades)
    control_daily = _dailyize_returns(control_returns, control_hold_days)
    variant_daily = _dailyize_returns(variant_returns, variant_hold_days)

    result: dict[str, Any] = {"ok": True}

    result["control_stats"] = _compute_strategy_stats(control_returns, control_daily)
    result["variant_stats"] = _compute_strategy_stats(variant_returns, variant_daily)

    result["paired_ttest"] = _welch_return_ttest(control_returns, variant_returns)

    result["bootstrap_sharpe"] = _bootstrap_sharpe_difference(control_daily, variant_daily)

    result["sharpe_ztest"] = _sharpe_ratio_ztest(control_daily, variant_daily)

    pvals = {
        "paired_ttest": float(result["paired_ttest"].get("p_value", 1.0)),
        "bootstrap_sharpe": float(result["bootstrap_sharpe"].get("p_value", 1.0)),
        "sharpe_ztest": float(result["sharpe_ztest"].get("p_value", 1.0)),
    }
    adjusted = _holm_bonferroni(pvals, alpha=SIGNIFICANCE_LEVEL)
    for k, v in adjusted.items():
        result[k]["significant"] = bool(v["significant"])
        result[k]["p_value_adjusted"] = round(v["p_value_adjusted"], 6)
    result["multiple_testing"] = {"method": "holm_bonferroni", "alpha": SIGNIFICANCE_LEVEL}

    tests_passed = sum(
        1 for t in ["paired_ttest", "bootstrap_sharpe", "sharpe_ztest"]
        if result[t].get("significant", False)
    )
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


def _extract_trade_returns(trades: list[PaperTrade]) -> tuple[list[float], list[float]]:
    returns: list[float] = []
    hold_days: list[float] = []
    for t in trades:
        returns.append(float(t.pnl_pct or 0.0))
        entry = getattr(t, "entry_date", None)
        exit_ = getattr(t, "exit_date", None)
        if entry and exit_:
            days = max((exit_ - entry).total_seconds() / 86400.0, 1.0)
        else:
            days = 1.0
        hold_days.append(days)
    return returns, hold_days


def _dailyize_returns(returns: list[float], hold_days: list[float]) -> list[float]:
    out: list[float] = []
    for idx, value in enumerate(returns):
        d = hold_days[idx] if idx < len(hold_days) else 1.0
        out.append(float(value) / max(float(d), 1.0))
    return out


def _compute_strategy_stats(returns: list[float], daily_returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {}
    n = len(returns)
    mean_ret = sum(returns) / n
    var_ret = sum((r - mean_ret) ** 2 for r in returns) / max(n - 1, 1)
    std_ret = math.sqrt(var_ret)
    mean_daily = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
    var_daily = (
        sum((r - mean_daily) ** 2 for r in daily_returns) / max(len(daily_returns) - 1, 1)
        if daily_returns
        else 0.0
    )
    std_daily = math.sqrt(var_daily) if var_daily > 0 else 0.0
    wins = sum(1 for r in returns if r > 0)
    sharpe = (mean_daily / std_daily * math.sqrt(252)) if std_daily > 0 else 0
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


def _welch_return_ttest(
    control: list[float],
    variant: list[float],
) -> dict[str, Any]:
    """Welch t-test for independent samples (paper trades are not naturally paired)."""
    n_c = len(control)
    n_v = len(variant)
    if n_c < 2 or n_v < 2:
        return {
            "t_statistic": 0.0,
            "p_value": 1.0,
            "mean_diff": 0.0,
            "n_pairs": min(n_c, n_v),
            "significant": False,
            "variant_better": False,
            "method": "welch_ttest_independent",
        }

    m_c = sum(control) / n_c
    m_v = sum(variant) / n_v
    var_c = sum((x - m_c) ** 2 for x in control) / max(n_c - 1, 1)
    var_v = sum((x - m_v) ** 2 for x in variant) / max(n_v - 1, 1)
    se = math.sqrt((var_c / n_c) + (var_v / n_v))
    if se <= 1e-12:
        t_stat = 0.0
    else:
        t_stat = (m_v - m_c) / se

    df_num = (var_c / n_c + var_v / n_v) ** 2
    df_den = ((var_c / n_c) ** 2 / max(n_c - 1, 1)) + ((var_v / n_v) ** 2 / max(n_v - 1, 1))
    df = int(round(df_num / df_den)) if df_den > 0 else max(min(n_c, n_v) - 1, 1)
    p_value = _t_to_p(abs(t_stat), df) * 2

    return {
        "t_statistic": round(t_stat, 4),
        "p_value": round(p_value, 6),
        "mean_diff": round(m_v - m_c, 4),
        "n_pairs": min(n_c, n_v),
        "significant": p_value < SIGNIFICANCE_LEVEL,
        "variant_better": (m_v - m_c) > 0,
        "method": "welch_ttest_independent",
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

    p_nonpos = sum(1 for d in diffs if d <= 0) / n_resamples
    p_nonneg = sum(1 for d in diffs if d >= 0) / n_resamples
    p_value = min(1.0, 2 * min(p_nonpos, p_nonneg))
    significant = p_value < SIGNIFICANCE_LEVEL

    return {
        "mean_sharpe_diff": round(mean_diff, 4),
        "ci_lower": round(lo, 4),
        "ci_upper": round(hi, 4),
        "p_value": round(p_value, 6),
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


def _holm_bonferroni(pvals: dict[str, float], alpha: float = 0.05) -> dict[str, dict[str, float | bool]]:
    """Holm-Bonferroni correction for a small family of tests."""
    m = len(pvals)
    ordered = sorted(pvals.items(), key=lambda kv: kv[1])
    out: dict[str, dict[str, float | bool]] = {}
    stop = False
    for i, (name, p) in enumerate(ordered):
        threshold = alpha / max(m - i, 1)
        significant = (not stop) and (p <= threshold)
        if not significant:
            stop = True
        adjusted = min(1.0, p * max(m - i, 1))
        out[name] = {"significant": significant, "p_value_adjusted": adjusted}
    return out


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
