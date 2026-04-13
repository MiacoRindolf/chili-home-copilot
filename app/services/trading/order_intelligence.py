"""Smart order routing and liquidity intelligence.

Provides:
- ADV fraction checks (block/warn if position > N% of average daily volume)
- Smart order type selection (limit vs market)
- Post-trade reconciliation feedback into spread assumptions
- Fill rate tracking by ticker, time-of-day, and order type
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MAX_ADV_FRACTION_PCT = 5.0
LARGE_SPREAD_THRESHOLD_BPS = 50

_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "execution_quality"
_FILL_LOG = _DATA_DIR / "fill_log.jsonl"


def check_liquidity(
    ticker: str,
    position_size_shares: int,
    *,
    max_adv_pct: float = MAX_ADV_FRACTION_PCT,
) -> dict[str, Any]:
    """Check if position size is appropriate for the ticker's liquidity.

    Args:
        ticker: The ticker symbol.
        position_size_shares: Planned number of shares.
        max_adv_pct: Maximum allowed percentage of average daily volume.

    Returns:
        Dict with allowed flag, ADV data, and warnings.
    """
    from .market_data import fetch_ohlcv_df

    try:
        df = fetch_ohlcv_df(ticker, period="1mo", interval="1d")
        if df is None or len(df) < 5:
            return {"ok": True, "allowed": True, "warning": "insufficient_volume_data"}

        avg_volume = float(df["Volume"].mean())
        if avg_volume <= 0:
            return {"ok": True, "allowed": True, "warning": "zero_avg_volume"}

        adv_fraction = (position_size_shares / avg_volume) * 100
        allowed = adv_fraction <= max_adv_pct

        result: dict[str, Any] = {
            "ok": True,
            "allowed": allowed,
            "ticker": ticker,
            "position_shares": position_size_shares,
            "avg_daily_volume": int(avg_volume),
            "adv_fraction_pct": round(adv_fraction, 2),
            "max_adv_pct": max_adv_pct,
        }

        if not allowed:
            result["warning"] = (
                f"Position ({position_size_shares} shares) is {adv_fraction:.1f}% "
                f"of ADV ({int(avg_volume)}), exceeds {max_adv_pct}% limit"
            )
            result["suggested_max_shares"] = int(avg_volume * max_adv_pct / 100)

        return result

    except Exception as e:
        return {"ok": True, "allowed": True, "warning": f"liquidity_check_failed: {e}"}


def suggest_order_type(
    ticker: str,
    signal_urgency: str = "normal",
    current_spread_bps: float | None = None,
) -> dict[str, Any]:
    """Recommend limit vs market order based on conditions.

    Args:
        ticker: The ticker symbol.
        signal_urgency: "high" for time-sensitive, "normal" for standard, "low" for patient.
        current_spread_bps: Current bid-ask spread in basis points (if known).
    """
    if signal_urgency == "high":
        order_type = "market"
        reason = "Time-sensitive signal — market order to ensure fill"
    elif current_spread_bps is not None and current_spread_bps > LARGE_SPREAD_THRESHOLD_BPS:
        order_type = "limit"
        reason = f"Wide spread ({current_spread_bps:.0f} bps) — limit order to control slippage"
    elif signal_urgency == "low":
        order_type = "limit"
        reason = "Patient entry — limit order for better fill"
    else:
        order_type = "limit"
        reason = "Standard entry — limit order preferred"

    return {
        "order_type": order_type,
        "reason": reason,
        "signal_urgency": signal_urgency,
        "spread_bps": current_spread_bps,
    }


def log_fill(
    ticker: str,
    signal_price: float,
    fill_price: float,
    order_type: str,
    fill_time_seconds: float | None = None,
) -> dict[str, Any]:
    """Log an execution fill for reconciliation analysis."""
    slippage_bps = abs(fill_price - signal_price) / signal_price * 10_000

    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "ticker": ticker,
        "signal_price": signal_price,
        "fill_price": fill_price,
        "slippage_bps": round(slippage_bps, 2),
        "order_type": order_type,
        "fill_time_seconds": fill_time_seconds,
        "hour_of_day": datetime.utcnow().hour,
    }

    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_FILL_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug("[order_intel] Failed to log fill: %s", e)

    return entry


def get_execution_quality_report(
    ticker: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Analyze fill quality from logged executions."""
    if not _FILL_LOG.exists():
        return {"ok": True, "fills": 0, "report": {}}

    fills = []
    try:
        for line in _FILL_LOG.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if ticker and entry.get("ticker") != ticker:
                    continue
                fills.append(entry)
            except json.JSONDecodeError:
                continue
    except Exception:
        return {"ok": True, "fills": 0, "report": {}}

    fills = fills[-limit:]
    if not fills:
        return {"ok": True, "fills": 0, "report": {}}

    slippages = [f["slippage_bps"] for f in fills]
    avg_slip = sum(slippages) / len(slippages)
    slippages_sorted = sorted(slippages)
    p90_slip = slippages_sorted[int(len(slippages_sorted) * 0.9)]

    by_type: dict[str, list[float]] = {}
    by_hour: dict[int, list[float]] = {}
    for f in fills:
        by_type.setdefault(f.get("order_type", "unknown"), []).append(f["slippage_bps"])
        by_hour.setdefault(f.get("hour_of_day", 0), []).append(f["slippage_bps"])

    type_stats = {
        t: {"avg_bps": round(sum(s) / len(s), 2), "n": len(s)}
        for t, s in by_type.items()
    }
    hour_stats = {
        h: {"avg_bps": round(sum(s) / len(s), 2), "n": len(s)}
        for h, s in sorted(by_hour.items())
    }

    return {
        "ok": True,
        "fills": len(fills),
        "avg_slippage_bps": round(avg_slip, 2),
        "p90_slippage_bps": round(p90_slip, 2),
        "by_order_type": type_stats,
        "by_hour": hour_stats,
        "suggested_spread_bps": round(p90_slip * 1.1, 2),
    }


def reconcile_spread_assumptions(db: Session) -> dict[str, Any]:
    """Feed realized slippage back into backtest spread assumptions.

    Compares actual fill slippage with the current backtest spread setting
    and recommends adjustments.
    """
    from ...config import settings

    report = get_execution_quality_report()
    if report.get("fills", 0) < 10:
        return {"ok": True, "action": "none", "reason": "insufficient_fills"}

    current_spread = float(settings.backtest_spread) * 10_000
    suggested = report.get("suggested_spread_bps", current_spread)

    gap = abs(suggested - current_spread)
    if gap > 10:
        action = "increase" if suggested > current_spread else "decrease"
    else:
        action = "none"

    return {
        "ok": True,
        "current_spread_bps": round(current_spread, 2),
        "realized_p90_bps": report.get("p90_slippage_bps"),
        "suggested_spread_bps": round(suggested, 2),
        "action": action,
        "fills_analyzed": report.get("fills", 0),
    }
