"""Multi-timeframe consensus scoring.

Evaluates a pattern's conditions across multiple timeframes and requires
agreement before considering a signal valid.  Timeframe-prefixed indicators
(e.g. "1h:rsi_14", "1d:ema_20") are resolved from OHLCV fetched at each
interval.

Usage in pattern DSL conditions:
    {"indicator": "1h:rsi_14", "op": ">", "value": 50}
    {"indicator": "1d:ema_20", "op": "<", "ref": "1d:price"}
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SUPPORTED_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d", "1wk")


def compute_mtf_indicators(
    ticker: str,
    timeframes: tuple[str, ...] = ("15m", "1h", "1d"),
) -> dict[str, Any]:
    """Fetch OHLCV for each timeframe and compute base indicators, prefixed by TF.

    Returns a flat dict like:
        {"15m:rsi_14": 62.3, "1h:rsi_14": 55.1, "1d:rsi_14": 48.0, ...}
    """
    from .market_data import fetch_ohlcv_df

    result: dict[str, Any] = {}

    _period_map = {
        "5m": "5d", "15m": "14d", "1h": "30d", "4h": "60d", "1d": "6mo", "1wk": "2y",
    }

    for tf in timeframes:
        period = _period_map.get(tf, "60d")
        try:
            df = fetch_ohlcv_df(ticker, period=period, interval=tf)
            if df.empty or len(df) < 20:
                continue
            indicators = _compute_basic_indicators(df)
            for key, val in indicators.items():
                result[f"{tf}:{key}"] = val
        except Exception as e:
            logger.debug("[mtf] Failed to get %s data for %s: %s", tf, ticker, e)

    return result


def _compute_basic_indicators(df) -> dict[str, Any]:
    """Compute indicators via indicator_core (single source of truth)."""
    from .indicator_core import compute_all_from_df

    if df is None or len(df) < 2:
        return {"price": 0.0}

    try:
        arrays = compute_all_from_df(
            df,
            needed=["price", "rsi_14", "ema_9", "ema_20", "ema_50", "ema_100",
                     "macd", "macd_signal", "macd_histogram", "adx",
                     "volume_ratio", "atr"],
        )
        result: dict[str, Any] = {}
        for key, arr in arrays.items():
            if arr is not None and len(arr) > 0:
                import math
                val = float(arr[-1])
                if not math.isnan(val):
                    result[key] = round(val, 4)
        if "price" not in result and len(df) > 0:
            result["price"] = float(df["Close"].iloc[-1])
        return result
    except Exception:
        return {"price": float(df["Close"].iloc[-1]) if len(df) > 0 else 0.0}


def score_mtf_consensus(
    ticker: str,
    pattern_conditions: list[dict[str, Any]],
    timeframes: tuple[str, ...] = ("15m", "1h", "1d"),
) -> dict[str, Any]:
    """Score how many timeframes agree on the pattern conditions.

    Returns:
        {
            "consensus_score": 0.0 - 1.0,
            "agreeing_timeframes": ["1h", "1d"],
            "total_timeframes": 3,
            "per_timeframe": {"15m": False, "1h": True, "1d": True},
        }
    """
    from .pattern_engine import _eval_condition

    mtf_indicators = compute_mtf_indicators(ticker, timeframes)

    per_tf: dict[str, bool] = {}
    for tf in timeframes:
        tf_prefix = f"{tf}:"
        tf_indicators = {}
        for key, val in mtf_indicators.items():
            if key.startswith(tf_prefix):
                tf_indicators[key[len(tf_prefix):]] = val

        if not tf_indicators:
            per_tf[tf] = False
            continue

        non_regime_conds = [c for c in pattern_conditions if not (c.get("indicator", "").startswith("regime_"))]
        base_conds = [c for c in non_regime_conds if ":" not in c.get("indicator", "")]

        if not base_conds:
            per_tf[tf] = True
            continue

        all_pass = True
        for cond in base_conds:
            if not _eval_condition(cond, tf_indicators):
                all_pass = False
                break
        per_tf[tf] = all_pass

    agreeing = [tf for tf, passed in per_tf.items() if passed]
    total = len(timeframes)
    score = len(agreeing) / total if total > 0 else 0

    return {
        "consensus_score": round(score, 3),
        "agreeing_timeframes": agreeing,
        "total_timeframes": total,
        "per_timeframe": per_tf,
    }
