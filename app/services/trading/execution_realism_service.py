"""Practical execution realism estimates for decision packets (not exchange-perfect).

Uses existing viability execution_readiness_json, regime ATR, spread, and sizing context.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from ...config import settings


def _sf(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def estimate_execution_realism(
    *,
    symbol: str,
    execution_readiness: dict[str, Any] | None,
    regime_snapshot: dict[str, Any] | None,
    quote_mid: float | None,
    spread_bps: float,
    intended_notional_usd: float,
    execution_mode: str,
) -> dict[str, Any]:
    """Return fill/slippage proxies and execution_penalty in [0,1]."""
    ex = execution_readiness if isinstance(execution_readiness, dict) else {}
    reg = regime_snapshot if isinstance(regime_snapshot, dict) else {}

    base_slip = _sf(ex.get("slippage_estimate_bps"), 6.0)
    vol_mult = 1.0
    atr_pct = _sf(reg.get("atr_pct") or reg.get("atr_percent"), 0.0)
    if atr_pct > 0:
        vol_mult = min(2.2, max(0.85, 1.0 + atr_pct / 200.0))

    spread_mult = min(1.8, max(0.9, 1.0 + max(0.0, spread_bps - 8.0) / 80.0))
    slip_bps = base_slip * vol_mult * spread_mult

    hour_bucket = datetime.utcnow().hour
    tod_mult = 1.0
    if execution_mode == "live" and not (str(symbol).upper().endswith("-USD")):
        if hour_bucket < 14 or hour_bucket > 20:
            tod_mult = 1.12
    slip_bps *= tod_mult

    size_penalty = 0.0
    if intended_notional_usd > 0 and quote_mid and quote_mid > 0:
        notion_k = intended_notional_usd / 1000.0
        size_penalty = min(0.35, math.log1p(notion_k) * 0.04)

    p_fill = max(0.35, min(0.995, 0.92 - size_penalty * 0.4 - min(0.25, spread_bps / 120.0)))
    p_partial = max(0.02, min(0.45, 0.08 + size_penalty * 0.5 + spread_bps / 200.0))
    p_missed = max(0.005, min(0.5, 1.0 - p_fill + p_partial * 0.35))

    exec_penalty = min(
        0.95,
        (slip_bps / 100.0) * 0.35 + size_penalty * 0.45 + max(0.0, (spread_bps - 12.0) / 100.0) * 0.2,
    )

    return {
        "expected_slippage_bps": round(slip_bps, 4),
        "expected_fill_probability": round(p_fill, 4),
        "expected_partial_fill_probability": round(p_partial, 4),
        "expected_missed_fill_probability": round(p_missed, 4),
        "execution_penalty": round(exec_penalty, 4),
        "size_penalty": round(size_penalty, 4),
        "inputs": {
            "spread_bps": spread_bps,
            "atr_pct": atr_pct,
            "hour_utc": hour_bucket,
            "tod_mult": tod_mult,
            "vol_mult": vol_mult,
        },
    }


def gap_through_stop_penalty(
    *,
    stop_price: float,
    entry_price: float,
    regime_high: float | None,
    regime_low: float | None,
    side_long: bool = True,
) -> dict[str, Any]:
    """Conservative gap-through-stop flag when regime range implies gap past stop."""
    if stop_price <= 0 or entry_price <= 0:
        return {"gap_risk": False, "penalty_add": 0.0}
    penalty = 0.0
    gap_risk = False
    if side_long and regime_low is not None and regime_low < stop_price:
        gap_risk = True
        penalty = min(0.25, (stop_price - regime_low) / entry_price * 2.0)
    if not side_long and regime_high is not None and regime_high > stop_price:
        gap_risk = True
        penalty = min(0.25, (regime_high - stop_price) / entry_price * 2.0)
    return {"gap_risk": gap_risk, "penalty_add": round(penalty, 4)}


def apply_realism_rollup_to_viability_json(current: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Merge a single observation into execution_readiness-style rollup (for learning loops)."""
    if not settings.brain_enable_execution_realism:
        return current
    out = dict(current) if isinstance(current, dict) else {}
    roll = dict(out.get("chili_realism_rollup") or {})
    n = int(roll.get("n") or 0) + 1
    prev_slip = _sf(roll.get("avg_slippage_bps"))
    obs_slip = _sf(observation.get("expected_slippage_bps"))
    roll["avg_slippage_bps"] = round((prev_slip * (n - 1) + obs_slip) / max(1, n), 4)
    roll["n"] = n
    roll["last_expected_fill_p"] = observation.get("expected_fill_probability")
    out["chili_realism_rollup"] = roll
    return out
