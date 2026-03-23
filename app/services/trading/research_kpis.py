"""Institutional-style research KPIs for pattern / strategy backtests.

Values align with ``backtesting.py`` ``compute_stats`` where possible (Sharpe, Sortino,
Calmar, CAPM-style Alpha/Beta vs buy-and-hold). Information ratio and win/loss payoff
are computed from equity and trade series.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any

import pandas as pd


def annualization_factor_from_interval(interval: str) -> float:
    """Sqrt of periods per year for scaling per-bar excess return IR (rough heuristic)."""
    iv = (interval or "1d").strip().lower()
    if iv in ("1d", "d", "day", "1day"):
        return 252.0**0.5
    if iv in ("1wk", "1w", "week", "weekly"):
        return 52.0**0.5
    if iv in ("1h", "60m", "60min", "h"):
        return (252.0 * 6.5) ** 0.5
    if iv in ("4h", "240m"):
        return (252.0 * 2.0) ** 0.5
    if iv in ("15m", "15min"):
        return (252.0 * 26.0) ** 0.5
    return 252.0**0.5


def _finite_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def information_ratio_vs_buyhold(
    equity: pd.Series,
    benchmark_close: pd.Series,
    sqrt_periods_per_year: float,
) -> float | None:
    """IR vs buy-and-hold on same bars: mean(excess) / TE, annualized (sqrt scaling)."""
    if equity is None or benchmark_close is None:
        return None
    eq = pd.Series(equity, dtype=float).reindex(benchmark_close.index).ffill().bfill()
    bc = pd.Series(benchmark_close, dtype=float)
    common = eq.index.intersection(bc.index)
    if len(common) < 12:
        return None
    eq = eq.loc[common]
    bc = bc.loc[common]
    r_p = eq.pct_change()
    r_b = bc.pct_change()
    excess = (r_p - r_b).dropna()
    if len(excess) < 10:
        return None
    mu = float(excess.mean())
    sd = float(excess.std(ddof=1))
    if sd < 1e-12:
        return None
    return float((mu / sd) * sqrt_periods_per_year)


def win_loss_payoff_ratio_from_trades(trades_df: pd.DataFrame) -> float | None:
    """Average winning trade return / |average losing trade return| (ReturnPct as fraction)."""
    if trades_df is None or trades_df.empty:
        return None
    if "ReturnPct" not in trades_df.columns:
        return None
    r = trades_df["ReturnPct"].astype(float)
    wins = r[r > 0]
    losses = r[r < 0]
    if wins.empty or losses.empty:
        return None
    aw = float(wins.mean())
    al = float(losses.mean())
    if al == 0:
        return None
    return float(aw / abs(al))


def build_research_kpis(
    stats: Any,
    *,
    equity_df: pd.DataFrame | None,
    close_series: pd.Series,
    interval: str,
    raw_trades: pd.DataFrame | None,
) -> dict[str, Any]:
    """Assemble KPI dict for API / JSON persistence (round for stability)."""

    def stat(key: str) -> float | None:
        try:
            if hasattr(stats, "get"):
                v = stats.get(key)
            else:
                v = stats[key]  # type: ignore[index]
        except Exception:
            return None
        return _finite_float(v)

    sqrt_ppy = annualization_factor_from_interval(interval)
    ir = None
    if equity_df is not None and not equity_df.empty and "Equity" in equity_df.columns:
        ir = information_ratio_vs_buyhold(
            equity_df["Equity"],
            close_series,
            sqrt_ppy,
        )

    wl = win_loss_payoff_ratio_from_trades(raw_trades) if raw_trades is not None else None

    out: dict[str, Any] = {
        "sharpe_ratio": _round_opt(stat("Sharpe Ratio"), 3),
        "sortino_ratio": _round_opt(stat("Sortino Ratio"), 3),
        "calmar_ratio": _round_opt(stat("Calmar Ratio"), 3),
        "max_drawdown_pct": _round_opt(stat("Max. Drawdown [%]"), 2),
        "volatility_ann_pct": _round_opt(stat("Volatility (Ann.) [%]"), 2),
        "return_ann_pct": _round_opt(stat("Return (Ann.) [%]"), 2),
        "cagr_pct": _round_opt(stat("CAGR [%]"), 2),
        "jensen_alpha_pct": _round_opt(stat("Alpha [%]"), 2),
        "beta": _round_opt(stat("Beta"), 3),
        "information_ratio": _round_opt(ir, 3),
        "expectancy_pct": _round_opt(stat("Expectancy [%]"), 3),
        "profit_factor": _round_opt(stat("Profit Factor"), 3),
        "win_rate_pct": _round_opt(stat("Win Rate [%]"), 2),
        "win_loss_payoff_ratio": _round_opt(wl, 3),
    }
    return {k: v for k, v in out.items() if v is not None}


def _round_opt(v: float | None, nd: int) -> float | None:
    if v is None:
        return None
    return round(float(v), nd)


def parse_kpis_from_backtest_params(params: str | None) -> dict[str, Any] | None:
    if not params:
        return None
    try:
        blob = json.loads(params) if isinstance(params, str) else params
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(blob, dict):
        return None
    k = blob.get("kpis")
    return k if isinstance(k, dict) else None


def aggregate_kpis_from_params_rows(
    params_strings: list[str | None],
    *,
    max_samples: int = 200,
) -> dict[str, Any]:
    """Mean of each numeric KPI across stored backtest params (recent-first list)."""
    buckets: dict[str, list[float]] = defaultdict(list)
    n = 0
    for raw in params_strings[:max_samples]:
        k = parse_kpis_from_backtest_params(raw)
        if not k:
            continue
        n += 1
        for key, val in k.items():
            if isinstance(val, (int, float)) and not (isinstance(val, float) and math.isnan(val)):
                buckets[key].append(float(val))
    if not buckets:
        return {"sample_count": 0}
    means: dict[str, float] = {}
    for key, vals in buckets.items():
        if vals:
            means[key] = round(sum(vals) / len(vals), 4)
    return {"sample_count": n, "means": means}
