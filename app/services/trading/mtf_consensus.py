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
    """Compute a minimal set of indicators from an OHLCV DataFrame."""
    import pandas as pd

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)

    result: dict[str, Any] = {
        "price": float(close.iloc[-1]),
    }

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    if not rsi.empty and not pd.isna(rsi.iloc[-1]):
        result["rsi_14"] = round(float(rsi.iloc[-1]), 2)

    # EMAs
    for span in (9, 20, 50, 100):
        ema = close.ewm(span=span, adjust=False).mean()
        if not ema.empty:
            result[f"ema_{span}"] = round(float(ema.iloc[-1]), 4)

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    if not macd.empty:
        result["macd"] = round(float(macd.iloc[-1]), 4)
        result["macd_signal"] = round(float(macd_signal.iloc[-1]), 4)
        result["macd_histogram"] = round(float((macd - macd_signal).iloc[-1]), 4)

    # ADX
    if len(df) >= 14:
        try:
            tr = pd.concat([
                high - low,
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ], axis=1).max(axis=1)
            atr = tr.rolling(14).mean()
            plus_dm = (high.diff()).clip(lower=0)
            minus_dm = (-low.diff()).clip(lower=0)
            plus_di = 100 * (plus_dm.rolling(14).mean() / atr)
            minus_di = 100 * (minus_dm.rolling(14).mean() / atr)
            dx = (plus_di - minus_di).abs() / (plus_di + minus_di) * 100
            adx = dx.rolling(14).mean()
            if not adx.empty and not pd.isna(adx.iloc[-1]):
                result["adx"] = round(float(adx.iloc[-1]), 2)
        except Exception:
            pass

    # Volume ratio
    avg_vol = volume.rolling(20).mean()
    if not avg_vol.empty and avg_vol.iloc[-1] > 0:
        result["volume_ratio"] = round(float(volume.iloc[-1] / avg_vol.iloc[-1]), 2)

    return result


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
