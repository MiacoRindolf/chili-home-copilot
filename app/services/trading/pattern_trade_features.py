"""Feature extraction for PatternTradeRow.features_json (versioned)."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

FEATURE_SCHEMA_V1 = "1"


def build_features_v1(
    *,
    trade: dict[str, Any],
    result_summary: dict[str, Any],
    indicators: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build feature dict for schema v1 from one trade + aggregate backtest result."""
    ind = indicators or {}
    feats: dict[str, Any] = {
        "schema": "1",
        "backtest_return_pct": result_summary.get("return_pct"),
        "backtest_win_rate": result_summary.get("win_rate"),
        "backtest_trade_count": result_summary.get("trade_count"),
        "trade_return_pct": trade.get("return_pct"),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
    }
    et, xt = trade.get("entry_time"), trade.get("exit_time")
    if et and xt and isinstance(et, (int, float)) and isinstance(xt, (int, float)):
        # Daily assumption: ~86400s per bar for rough bar count
        day = 86400
        feats["hold_bars_est"] = max(1, int(round((xt - et) / day)))
    # Pull common indicator scalars if present (structure varies by strategy)
    for key in ("rel_vol", "vol_ratio", "rsi_14", "atr_pct", "macd_hist"):
        if key in ind:
            try:
                v = ind[key]
                if hasattr(v, "iloc"):
                    feats[key] = float(v.iloc[-1]) if len(v) else None
                elif isinstance(v, (int, float)):
                    feats[key] = float(v)
            except Exception:
                pass
    if "rel_vol" not in feats and "vol_ratio" in feats:
        feats["rel_volume"] = feats.get("vol_ratio")
    elif "rel_vol" in feats:
        feats["rel_volume"] = feats.get("rel_vol")
    return feats


def validate_features_v1(feats: dict[str, Any]) -> bool:
    return feats.get("schema") == "1"
