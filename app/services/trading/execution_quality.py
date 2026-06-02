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

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
import math
from typing import Any, Mapping

from sqlalchemy.orm import Session

from ...models.trading import Trade

logger = logging.getLogger(__name__)


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _positive_float(value: Any) -> float | None:
    out = _finite_float(value)
    if out is None or out <= 0.0:
        return None
    return out


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        if isinstance(parsed, Mapping):
            return parsed
    return None


def _mapping_get(mapping: Mapping[str, Any] | None, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, Mapping) else None


def _domain_is_option_premium(value: Any) -> bool:
    return str(value or "").strip().lower() == "option_premium"


def _is_option_trade(trade: Any, snap: Mapping[str, Any] | None = None) -> bool:
    try:
        from .autopilot_scope import is_option_trade

        return bool(is_option_trade(trade))
    except Exception:
        pass

    markers = [
        getattr(trade, "asset_kind", None),
        getattr(trade, "asset_type", None),
        _mapping_get(snap, "asset_kind"),
        _mapping_get(snap, "asset_type"),
        _mapping_get(snap, "asset_class"),
    ]
    breakout = _as_mapping(_mapping_get(snap, "breakout_alert"))
    if breakout:
        markers.extend([
            breakout.get("asset_kind"),
            breakout.get("asset_type"),
            breakout.get("asset_class"),
        ])
    return any("option" in str(marker or "").strip().lower() for marker in markers)


def _option_reference_is_premium(
    trade: Any,
    snap: Mapping[str, Any] | None = None,
) -> bool:
    if _domain_is_option_premium(getattr(trade, "tca_reference_domain", None)):
        return True
    entry_execution = _as_mapping(_mapping_get(snap, "entry_execution"))
    if entry_execution:
        if _domain_is_option_premium(entry_execution.get("tca_reference_domain")):
            return True
        if _domain_is_option_premium(entry_execution.get("option_price_domain")):
            return True
    domains = _as_mapping(_mapping_get(snap, "price_domains"))
    if domains:
        for key in ("entry_price", "limit_price", "current_price", "signal_price"):
            if _domain_is_option_premium(domains.get(key)):
                return True
    option_meta = _as_mapping(_mapping_get(snap, "option_meta"))
    if option_meta and _domain_is_option_premium(option_meta.get("price_domain")):
        return True
    breakout = _as_mapping(_mapping_get(snap, "breakout_alert"))
    if breakout:
        if _option_reference_is_premium(trade, breakout):
            return True
    return False


def _tca_bps_or_zero(trade: Any, attr: str) -> float | None:
    value = getattr(trade, attr, None)
    if value is None:
        return 0.0
    try:
        from .execution_cost_builder import _usable_tca_bps

        return _usable_tca_bps(trade, attr)
    except Exception:
        return _finite_float(value)


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
        entry_price = _positive_float(getattr(t, "entry_price", None))
        if signal_price is None or entry_price is None:
            continue

        slip_pct = abs(entry_price - signal_price) / signal_price * 100
        slippages.append(slip_pct)
        ticker = str(getattr(t, "ticker", "") or "")
        by_ticker[ticker].append(slip_pct)

        asset_class = "option" if _is_option_trade(t) else ("crypto" if ticker.endswith("-USD") else "stock")
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
    snap = _as_mapping(getattr(trade, "indicator_snapshot", None))
    is_option = _is_option_trade(trade, snap)

    tca_ref = _positive_float(getattr(trade, "tca_reference_entry_price", None))
    if tca_ref is not None:
        if not is_option or _option_reference_is_premium(trade, snap):
            return tca_ref
        return None

    if snap:
        price_obj = _as_mapping(snap.get("price"))
        candidates = [
            snap.get("signal_price"),
            snap.get("brain_price"),
            price_obj.get("value") if price_obj else None,
        ]
        if not is_option or _option_reference_is_premium(trade, snap):
            for candidate in candidates:
                price = _positive_float(candidate)
                if price is not None:
                    return price

    tags = _as_mapping(getattr(trade, "tags", None))
    if tags and not is_option:
        for candidate in (tags.get("signal_price"), tags.get("brain_entry")):
            price = _positive_float(candidate)
            if price is not None:
                return price

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
        entry_price = _positive_float(getattr(t, "entry_price", None))
        if signal_price is None or entry_price is None:
            continue

        # Delay cost: signal_price -> arrival_price (use entry_price as proxy)
        delay_bps = abs(entry_price - signal_price) / signal_price * 10000
        components["delay_bps"].append(delay_bps)

        # Spread cost from TCA
        entry_slip = _tca_bps_or_zero(t, "tca_entry_slippage_bps")
        exit_slip = _tca_bps_or_zero(t, "tca_exit_slippage_bps")
        if entry_slip is None or exit_slip is None:
            components["delay_bps"].pop()
            continue
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
