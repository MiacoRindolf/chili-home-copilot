"""Execution quality tracking for the trading brain.

Measures the gap between signal price (when the brain fires) and actual
fill price (when the trade is opened).  Used to:
1. Track slippage per ticker/asset class
2. Adaptively adjust backtest_spread for more realistic backtests
3. Identify tickers with consistently poor execution

Decision-stack realism rollups for momentum viability JSON live in
``execution_realism_service.apply_realism_rollup_to_viability_json`` (merges into
``execution_readiness_json["chili_realism_rollup"]``) — not a second slippage table.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import Trade

logger = logging.getLogger(__name__)


def compute_execution_stats(
    db: Session,
    user_id: int | None = None,
    lookback_days: int = 90,
) -> dict[str, Any]:
    """Compute execution quality metrics from closed trades."""
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)

    trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.entry_date >= cutoff,
    ).all()

    if not trades:
        return {"ok": True, "trades_analyzed": 0}

    slippages: list[float] = []
    by_ticker: dict[str, list[float]] = defaultdict(list)
    by_class: dict[str, list[float]] = defaultdict(list)

    for t in trades:
        signal_price = _extract_signal_price(t)
        if signal_price is None or signal_price <= 0:
            continue

        slip_pct = abs(t.entry_price - signal_price) / signal_price * 100
        slippages.append(slip_pct)
        by_ticker[t.ticker].append(slip_pct)

        asset_class = "crypto" if t.ticker.endswith("-USD") else "stock"
        by_class[asset_class].append(slip_pct)

    if not slippages:
        return {"ok": True, "trades_analyzed": len(trades), "measurable": 0}

    avg_slip = sum(slippages) / len(slippages)
    p90_idx = int(len(slippages) * 0.9)
    sorted_slips = sorted(slippages)
    p90_slip = sorted_slips[min(p90_idx, len(sorted_slips) - 1)]

    ticker_stats = {
        ticker: {
            "avg_slippage_pct": round(sum(slips) / len(slips), 4),
            "max_slippage_pct": round(max(slips), 4),
            "trades": len(slips),
        }
        for ticker, slips in by_ticker.items()
        if len(slips) >= 2
    }

    class_stats = {
        cls: {
            "avg_slippage_pct": round(sum(slips) / len(slips), 4),
            "trades": len(slips),
        }
        for cls, slips in by_class.items()
    }

    return {
        "ok": True,
        "trades_analyzed": len(trades),
        "measurable": len(slippages),
        "avg_slippage_pct": round(avg_slip, 4),
        "p90_slippage_pct": round(p90_slip, 4),
        "by_ticker": dict(sorted(ticker_stats.items(), key=lambda x: x[1]["avg_slippage_pct"], reverse=True)[:20]),
        "by_class": class_stats,
    }


def suggest_adaptive_spread(
    db: Session,
    user_id: int | None = None,
    lookback_days: int = 60,
) -> dict[str, Any]:
    """Suggest backtest spread based on actual execution slippage.

    Returns a recommended spread value that covers 90th percentile of
    observed slippage, ensuring backtests are realistic.
    """
    from ...config import settings

    stats = compute_execution_stats(db, user_id, lookback_days)
    current_spread = float(settings.backtest_spread)

    if stats.get("measurable", 0) < 10:
        return {
            "current_spread": current_spread,
            "suggested_spread": current_spread,
            "reason": "insufficient_data",
            "trades_measured": stats.get("measurable", 0),
        }

    p90 = stats.get("p90_slippage_pct", 0)
    suggested = max(0.001, round(p90 / 100, 4))

    # Don't suggest anything too far from current (avoid div-by-zero if spread misconfigured)
    if current_spread and current_spread > 0:
        suggested = min(suggested, current_spread * 3)
        suggested = max(suggested, current_spread * 0.5)

    denom = current_spread if current_spread and current_spread > 0 else None
    if denom:
        should_update = abs(suggested - current_spread) / denom > 0.2
    else:
        should_update = abs(suggested - current_spread) > 1e-12

    return {
        "current_spread": current_spread,
        "suggested_spread": round(suggested, 4),
        "p90_slippage_pct": round(p90, 4),
        "avg_slippage_pct": stats.get("avg_slippage_pct", 0),
        "trades_measured": stats.get("measurable", 0),
        "should_update": should_update,
    }


def flag_poor_execution_tickers(
    db: Session,
    user_id: int | None = None,
    threshold_pct: float = 0.5,
) -> list[dict[str, Any]]:
    """Identify tickers with consistently poor execution (high slippage)."""
    stats = compute_execution_stats(db, user_id)
    flagged = []

    for ticker, data in stats.get("by_ticker", {}).items():
        if data["avg_slippage_pct"] >= threshold_pct and data["trades"] >= 3:
            flagged.append({
                "ticker": ticker,
                "avg_slippage_pct": data["avg_slippage_pct"],
                "max_slippage_pct": data["max_slippage_pct"],
                "trades": data["trades"],
                "recommendation": "widen_stops" if data["avg_slippage_pct"] > 1.0 else "monitor",
            })

    return sorted(flagged, key=lambda x: x["avg_slippage_pct"], reverse=True)


def _extract_signal_price(trade: Trade) -> float | None:
    """Extract the signal/brain-recommended price from trade metadata."""
    import json

    if trade.indicator_snapshot:
        try:
            snap = json.loads(trade.indicator_snapshot) if isinstance(trade.indicator_snapshot, str) else trade.indicator_snapshot
            sp = snap.get("signal_price") or snap.get("brain_price") or snap.get("price", {}).get("value")
            if sp:
                return float(sp)
        except Exception:
            pass

    if trade.tags:
        try:
            tags = json.loads(trade.tags) if isinstance(trade.tags, str) and trade.tags.startswith("{") else {}
            sp = tags.get("signal_price") or tags.get("brain_entry")
            if sp:
                return float(sp)
        except Exception:
            pass

    return None


# ── Implementation Shortfall ──────────────────────────────────────────


def compute_implementation_shortfall(
    db: Session,
    user_id: int | None = None,
    lookback_days: int = 90,
) -> dict[str, Any]:
    """Compute implementation shortfall: the gap between paper portfolio
    performance and actual realized performance.

    IS = (paper return - realized return) decomposed into:
    - delay_cost: price moved between signal and execution
    - spread_cost: bid-ask spread paid
    - impact_cost: market impact of order
    - opportunity_cost: unfilled orders / missed entries
    """
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)

    trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.entry_date >= cutoff,
    ).all()

    if not trades:
        return {"ok": True, "trades_analyzed": 0}

    components: dict[str, list[float]] = {
        "delay_bps": [],
        "spread_bps": [],
        "total_is_bps": [],
    }

    for t in trades:
        signal_price = _extract_signal_price(t)
        if signal_price is None or signal_price <= 0 or not t.entry_price:
            continue

        # Delay cost: signal_price -> arrival_price (use entry_price as proxy)
        delay_bps = abs(t.entry_price - signal_price) / signal_price * 10000
        components["delay_bps"].append(delay_bps)

        # Spread cost from TCA
        entry_slip = float(t.tca_entry_slippage_bps or 0)
        exit_slip = float(t.tca_exit_slippage_bps or 0)
        spread_bps = entry_slip + exit_slip
        components["spread_bps"].append(spread_bps)

        # Total IS
        total_is = delay_bps + spread_bps
        components["total_is_bps"].append(total_is)

    if not components["total_is_bps"]:
        return {"ok": True, "trades_analyzed": len(trades), "measurable": 0}

    result: dict[str, Any] = {
        "ok": True,
        "trades_analyzed": len(trades),
        "measurable": len(components["total_is_bps"]),
    }

    for key, vals in components.items():
        if vals:
            sorted_vals = sorted(vals)
            result[f"mean_{key}"] = round(sum(vals) / len(vals), 2)
            result[f"median_{key}"] = round(
                sorted_vals[len(sorted_vals) // 2], 2,
            )
            p90_idx = min(int(len(sorted_vals) * 0.9), len(sorted_vals) - 1)
            result[f"p90_{key}"] = round(sorted_vals[p90_idx], 2)

    return result


def calibrate_backtest_costs(
    db: Session,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Use realized execution data to calibrate backtest spread/commission.

    Computes asset-class specific costs from actual fills and returns
    recommended values.  Intended to be called periodically and fed
    into the backtest engine's defaults.
    """
    stats = compute_execution_stats(db, user_id, lookback_days=90)

    recommendations: dict[str, dict[str, float]] = {}

    for asset_class, class_data in stats.get("by_class", {}).items():
        if class_data.get("trades", 0) < 5:
            continue
        avg_slip = class_data.get("avg_slippage_pct", 0)
        # Spread should cover at least the average round-trip slippage
        recommended_spread = max(0.0001, round(avg_slip / 100 * 1.2, 6))
        recommendations[asset_class] = {
            "recommended_spread": recommended_spread,
            "avg_slippage_pct": avg_slip,
            "trades": class_data["trades"],
        }

    # IS metrics
    is_data = compute_implementation_shortfall(db, user_id)

    return {
        "ok": True,
        "asset_class_recommendations": recommendations,
        "implementation_shortfall": is_data,
        "suggestion": suggest_adaptive_spread(db, user_id),
    }
