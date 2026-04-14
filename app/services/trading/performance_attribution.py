"""Performance attribution: decompose trade P&L into alpha, beta, and costs.

Separates pattern edge from market exposure so the brain can learn *why*
strategies succeed or fail, not just *that* they did.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from ...models.trading import Trade

logger = logging.getLogger(__name__)

_BENCHMARK_TICKER = "SPY"


def _fetch_benchmark_return(
    entry_date: datetime | None,
    exit_date: datetime | None,
) -> float | None:
    """Fetch SPY return over the trade's holding period."""
    if not entry_date or not exit_date:
        return None
    try:
        from .market_data import fetch_ohlcv_df

        days = max(1, (exit_date - entry_date).days + 5)
        df = fetch_ohlcv_df(_BENCHMARK_TICKER, period=f"{days + 30}d", interval="1d")
        if df is None or len(df) < 2:
            return None

        df.index = df.index.tz_localize(None) if df.index.tz else df.index

        entry_dt = entry_date.replace(tzinfo=None) if entry_date.tzinfo else entry_date
        exit_dt = exit_date.replace(tzinfo=None) if exit_date.tzinfo else exit_date

        entry_idx = df.index.searchsorted(entry_dt)
        exit_idx = df.index.searchsorted(exit_dt)
        entry_idx = min(entry_idx, len(df) - 1)
        exit_idx = min(exit_idx, len(df) - 1)

        if entry_idx >= exit_idx:
            return 0.0

        entry_price = float(df["Close"].iloc[entry_idx])
        exit_price = float(df["Close"].iloc[exit_idx])
        if entry_price <= 0:
            return None
        return (exit_price - entry_price) / entry_price * 100
    except Exception:
        return None


def attribute_trade(trade: Trade) -> dict[str, Any]:
    """Decompose a single closed trade's P&L into components.

    Returns:
        gross_return_pct: total return before costs
        benchmark_return_pct: SPY return over same period
        alpha_pct: gross_return - benchmark_return (excess return)
        estimated_cost_pct: spread + commission estimate
        net_alpha_pct: alpha_pct - estimated_cost_pct
    """
    result: dict[str, Any] = {"trade_id": trade.id, "ticker": trade.ticker}

    if not trade.entry_price or trade.entry_price <= 0:
        result["error"] = "missing_entry_price"
        return result

    gross_return_pct = 0.0
    if trade.exit_price:
        gross_return_pct = (
            (trade.exit_price - trade.entry_price) / trade.entry_price * 100
        )
    result["gross_return_pct"] = round(gross_return_pct, 4)

    benchmark_ret = _fetch_benchmark_return(trade.entry_date, trade.exit_date)
    result["benchmark_return_pct"] = round(benchmark_ret, 4) if benchmark_ret is not None else None

    if benchmark_ret is not None:
        alpha = gross_return_pct - benchmark_ret
        result["alpha_pct"] = round(alpha, 4)
    else:
        result["alpha_pct"] = None

    # Estimate transaction costs from TCA fields or defaults
    entry_slip = float(trade.tca_entry_slippage_bps or 0) / 100
    exit_slip = float(trade.tca_exit_slippage_bps or 0) / 100
    estimated_cost = entry_slip + exit_slip
    if estimated_cost == 0:
        is_crypto = (trade.ticker or "").upper().endswith("-USD")
        estimated_cost = 0.04 if is_crypto else 0.02  # default cost estimate in %
    result["estimated_cost_pct"] = round(estimated_cost, 4)

    if result["alpha_pct"] is not None:
        result["net_alpha_pct"] = round(result["alpha_pct"] - estimated_cost, 4)
    else:
        result["net_alpha_pct"] = None

    # Holding period
    if trade.entry_date and trade.exit_date:
        result["holding_days"] = (trade.exit_date - trade.entry_date).days

    return result


def attribute_pattern_trades(
    db: Session,
    pattern_id: int,
    *,
    user_id: int | None = None,
    lookback_days: int = 90,
) -> dict[str, Any]:
    """Attribute all closed trades for a pattern, aggregating alpha vs beta."""
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)

    q = db.query(Trade).filter(
        Trade.scan_pattern_id == pattern_id,
        Trade.status == "closed",
        Trade.exit_date >= cutoff,
    )
    if user_id:
        q = q.filter(Trade.user_id == user_id)
    trades = q.order_by(Trade.exit_date.asc()).all()

    if not trades:
        return {"pattern_id": pattern_id, "trade_count": 0, "attributions": []}

    attributions = []
    alphas = []
    betas = []
    costs = []

    for t in trades:
        attr = attribute_trade(t)
        attributions.append(attr)
        if attr.get("alpha_pct") is not None:
            alphas.append(attr["alpha_pct"])
        if attr.get("benchmark_return_pct") is not None:
            betas.append(attr["benchmark_return_pct"])
        if attr.get("estimated_cost_pct") is not None:
            costs.append(attr["estimated_cost_pct"])

    summary: dict[str, Any] = {
        "pattern_id": pattern_id,
        "trade_count": len(trades),
        "trades_with_attribution": len(alphas),
    }

    if alphas:
        arr = np.array(alphas)
        summary["mean_alpha_pct"] = round(float(np.mean(arr)), 4)
        summary["median_alpha_pct"] = round(float(np.median(arr)), 4)
        summary["alpha_positive_rate"] = round(
            float(np.sum(arr > 0)) / len(arr), 4,
        )
        summary["alpha_sharpe"] = (
            round(float(np.mean(arr) / np.std(arr)), 4) if np.std(arr) > 0 else None
        )

    if betas:
        summary["mean_beta_pct"] = round(float(np.mean(betas)), 4)

    if costs:
        summary["mean_cost_pct"] = round(float(np.mean(costs)), 4)
        summary["total_cost_pct"] = round(float(np.sum(costs)), 4)

    # Flag patterns that only profit from beta
    if summary.get("mean_alpha_pct") is not None and summary.get("mean_beta_pct") is not None:
        summary["beta_dependent"] = (
            summary["mean_alpha_pct"] < 0 and summary["mean_beta_pct"] > 0
        )

    summary["attributions"] = attributions
    return summary
