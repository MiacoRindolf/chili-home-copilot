"""Shared indicator computation used by both live scanning and backtesting.

This module is the single source of truth for how indicators are computed
from OHLCV DataFrames, ensuring backtest/live parity.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _safe(series: pd.Series) -> list:
    """Convert a pandas Series to a list, replacing NaN with None."""
    return [None if pd.isna(v) else float(v) for v in series]


def compute_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Compute RSI using ta library, with manual fallback."""
    try:
        from ta.momentum import RSIIndicator
        return RSIIndicator(close, window=window).rsi()
    except Exception:
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).rolling(window).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))


def compute_ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def compute_sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    try:
        from ta.trend import ADXIndicator
        return ADXIndicator(high, low, close, window=window).adx()
    except Exception:
        return pd.Series([None] * len(close), index=close.index)


def compute_macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    try:
        from ta.trend import MACD
        m = MACD(close)
        return m.macd(), m.macd_signal(), m.macd_diff()
    except Exception:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal
        return macd_line, signal, hist


def compute_bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0) -> dict[str, pd.Series]:
    """Returns dict with keys: upper, lower, mid, pct, width."""
    try:
        from ta.volatility import BollingerBands
        bb = BollingerBands(close, window=window, window_dev=num_std)
        return {
            "upper": bb.bollinger_hband(),
            "lower": bb.bollinger_lband(),
            "mid": bb.bollinger_mavg(),
            "pct": bb.bollinger_pband(),
            "width": bb.bollinger_wband(),
        }
    except Exception:
        mid = close.rolling(window).mean()
        std = close.rolling(window).std()
        upper = mid + num_std * std
        lower = mid - num_std * std
        pct = (close - lower) / (upper - lower).replace(0, np.nan)
        width = 2 * std / mid.replace(0, np.nan)
        return {"upper": upper, "lower": lower, "mid": mid, "pct": pct, "width": width}


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    try:
        from ta.volatility import AverageTrueRange
        return AverageTrueRange(high, low, close, window=window).average_true_range()
    except Exception:
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window).mean()


def compute_stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
                       window: int = 14, smooth_window: int = 3) -> tuple[pd.Series, pd.Series]:
    """Returns (k, d)."""
    try:
        from ta.momentum import StochasticOscillator
        so = StochasticOscillator(high, low, close, window=window, smooth_window=smooth_window)
        return so.stoch(), so.stoch_signal()
    except Exception:
        lowest_low = low.rolling(window).min()
        highest_high = high.rolling(window).max()
        k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
        d = k.rolling(smooth_window).mean()
        return k, d


def compute_relative_volume(volume: pd.Series, window: int = 20) -> pd.Series:
    vol_avg = volume.rolling(window).mean()
    return volume / vol_avg.replace(0, np.nan)


def compute_gap_pct(open_: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return (open_ - prev_close) / prev_close.replace(0, np.nan) * 100


def compute_swing_low(low: pd.Series, lookback: int = 5) -> pd.Series:
    """Rolling swing low for BOS detection."""
    return low.rolling(lookback * 2 + 1, center=True).min()


def compute_all_from_df(
    df: pd.DataFrame,
    needed: set[str] | None = None,
) -> dict[str, list]:
    """Compute a full set of indicator arrays from an OHLCV DataFrame.

    If *needed* is provided, only those indicators are computed.
    Returns a dict of {indicator_name: list_of_values}.
    """
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)
    n = len(df)

    compute_all = needed is None
    if needed is None:
        needed = set()

    result: dict[str, list] = {}

    if compute_all or "price" in needed:
        result["price"] = _safe(close)

    if compute_all or "rsi_14" in needed:
        result["rsi_14"] = _safe(compute_rsi(close, 14))

    for span in (9, 12, 20, 21, 26, 50, 100, 200):
        key = f"ema_{span}"
        if compute_all or key in needed:
            result[key] = _safe(compute_ema(close, span))

    for span in (10, 20, 50, 100, 200):
        key = f"sma_{span}"
        if compute_all or key in needed:
            result[key] = _safe(compute_sma(close, span))

    if compute_all or "adx" in needed:
        result["adx"] = _safe(compute_adx(high, low, close))

    if compute_all or "macd" in needed or "macd_signal" in needed or "macd_hist" in needed:
        macd_l, sig, hist = compute_macd(close)
        if compute_all or "macd" in needed:
            result["macd"] = _safe(macd_l)
        if compute_all or "macd_signal" in needed:
            result["macd_signal"] = _safe(sig)
        if compute_all or "macd_hist" in needed or "macd_histogram" in needed:
            result["macd_hist"] = _safe(hist)
            result["macd_histogram"] = result["macd_hist"]

    if compute_all or any(k in needed for k in ("bb_upper", "bb_lower", "bb_mid", "bb_pct", "bb_width")):
        bb = compute_bollinger(close)
        result["bb_upper"] = _safe(bb["upper"])
        result["bb_lower"] = _safe(bb["lower"])
        result["bb_mid"] = _safe(bb["mid"])
        result["bb_pct"] = _safe(bb["pct"])
        result["bb_width"] = _safe(bb["width"])

    if compute_all or "atr" in needed:
        result["atr"] = _safe(compute_atr(high, low, close))

    if compute_all or "stoch_k" in needed or "stoch_d" in needed or "stochastic_k" in needed:
        k, d = compute_stochastic(high, low, close)
        result["stoch_k"] = _safe(k)
        result["stochastic_k"] = result["stoch_k"]
        result["stoch_d"] = _safe(d)

    if compute_all or "rel_vol" in needed or "volume_ratio" in needed:
        rv = compute_relative_volume(volume)
        result["rel_vol"] = _safe(rv)
        result["volume_ratio"] = result["rel_vol"]

    if compute_all or "gap_pct" in needed:
        result["gap_pct"] = _safe(compute_gap_pct(df["Open"].astype(float), close))

    if compute_all or "daily_change_pct" in needed:
        prev = close.shift(1)
        pct = (close - prev) / prev.replace(0, np.nan) * 100
        result["daily_change_pct"] = _safe(pct)

    if compute_all or "resistance" in needed:
        result["resistance"] = _safe(high.rolling(20).max())

    if compute_all or "dist_to_resistance_pct" in needed:
        res = high.rolling(20).max()
        dist = (res - close) / close.replace(0, np.nan) * 100
        result["dist_to_resistance_pct"] = _safe(dist)

    return result
