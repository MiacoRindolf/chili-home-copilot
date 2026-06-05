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


def compute_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume — cumulative volume signed by price direction."""
    try:
        from ta.volume import OnBalanceVolumeIndicator
        return OnBalanceVolumeIndicator(close, volume).on_balance_volume()
    except Exception:
        direction = np.sign(close.diff().fillna(0.0))
        return (direction * volume).fillna(0.0).cumsum()


def compute_mfi(high: pd.Series, low: pd.Series, close: pd.Series,
                volume: pd.Series, window: int = 14) -> pd.Series:
    """Money Flow Index — volume-weighted RSI in [0, 100]."""
    try:
        from ta.volume import MFIIndicator
        return MFIIndicator(high, low, close, volume, window=window).money_flow_index()
    except Exception:
        tp = (high + low + close) / 3.0
        mf = tp * volume
        pos = mf.where(tp > tp.shift(1), 0.0).rolling(window).sum()
        neg = mf.where(tp < tp.shift(1), 0.0).rolling(window).sum()
        mfr = pos / neg.replace(0, np.nan)
        return 100 - (100 / (1 + mfr))


def compute_vwap(high: pd.Series, low: pd.Series, close: pd.Series,
                 volume: pd.Series, window: int = 20) -> pd.Series:
    """Rolling volume-weighted average price (session-agnostic proxy).

    A true anchored VWAP needs a session boundary; for continuous (esp. 24/7
    crypto) bars a rolling window is the cross-asset-consistent proxy.
    """
    tp = (high + low + close) / 3.0
    pv = (tp * volume).rolling(window).sum()
    vol = volume.rolling(window).sum().replace(0, np.nan)
    return pv / vol


def compute_cci(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20) -> pd.Series:
    """Commodity Channel Index — typical-price deviation from its mean."""
    try:
        from ta.trend import CCIIndicator
        return CCIIndicator(high, low, close, window=window).cci()
    except Exception:
        tp = (high + low + close) / 3.0
        sma = tp.rolling(window).mean()
        mad = (tp - sma).abs().rolling(window).mean()
        return (tp - sma) / (0.015 * mad.replace(0, np.nan))


def compute_roc(close: pd.Series, window: int = 10) -> pd.Series:
    """Rate of change — percent momentum over *window* bars."""
    try:
        from ta.momentum import ROCIndicator
        return ROCIndicator(close, window=window).roc()
    except Exception:
        return (close / close.shift(window).replace(0, np.nan) - 1.0) * 100.0


def compute_keltner(high: pd.Series, low: pd.Series, close: pd.Series,
                    ema_window: int = 20, atr_window: int = 10,
                    mult: float = 1.5) -> dict[str, pd.Series]:
    """Keltner channels (EMA mid +/- mult*ATR). Returns upper/lower/mid."""
    mid = close.ewm(span=ema_window, adjust=False).mean()
    atr = compute_atr(high, low, close, window=atr_window)
    return {"upper": mid + mult * atr, "lower": mid - mult * atr, "mid": mid}


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

    # ── Volume-flow indicators ────────────────────────────────────────
    if compute_all or "obv" in needed:
        result["obv"] = _safe(compute_obv(close, volume))

    if compute_all or "mfi" in needed:
        result["mfi"] = _safe(compute_mfi(high, low, close, volume))

    if compute_all or "vwap" in needed or "vwap_dist_pct" in needed:
        _vwap = compute_vwap(high, low, close, volume)
        result["vwap"] = _safe(_vwap)
        result["vwap_dist_pct"] = _safe((close - _vwap) / _vwap.replace(0, np.nan) * 100.0)

    # ── Alt momentum / mean-reversion ─────────────────────────────────
    if compute_all or "cci" in needed:
        result["cci"] = _safe(compute_cci(high, low, close))

    if compute_all or "roc" in needed:
        result["roc"] = _safe(compute_roc(close))

    # ── Volatility squeeze (breakout precursor) ───────────────────────
    if compute_all or any(
        k in needed for k in ("keltner_upper", "keltner_lower", "keltner_mid", "ttm_squeeze")
    ):
        _kc = compute_keltner(high, low, close)
        result["keltner_upper"] = _safe(_kc["upper"])
        result["keltner_lower"] = _safe(_kc["lower"])
        result["keltner_mid"] = _safe(_kc["mid"])
        # TTM squeeze: Bollinger inside Keltner = volatility compression coiling
        # for a breakout. True iff both BB bands sit inside the Keltner channel.
        _bb_u = result.get("bb_upper")
        _bb_l = result.get("bb_lower")
        if _bb_u is None or _bb_l is None:
            _bb = compute_bollinger(close)
            _bb_u = _safe(_bb["upper"])
            _bb_l = _safe(_bb["lower"])
        result["ttm_squeeze"] = [
            (bu is not None and bl is not None and ku is not None and kl is not None
             and bu < ku and bl > kl)
            for bu, bl, ku, kl in zip(
                _bb_u, _bb_l, result["keltner_upper"], result["keltner_lower"]
            )
        ]

    # ── Derived boolean indicators ────────────────────────────────────
    _EMA_STACK_KEYS = {"ema_stack"}
    if compute_all or _EMA_STACK_KEYS & needed:
        _p = result.get("price") or _safe(close)
        _e20 = result.get("ema_20") or _safe(compute_ema(close, 20))
        _e50 = result.get("ema_50") or _safe(compute_ema(close, 50))
        _e100 = result.get("ema_100") or _safe(compute_ema(close, 100))
        result["ema_stack"] = [
            (p is not None and e20 is not None and e50 is not None
             and e100 is not None and p > e20 > e50 > e100)
            for p, e20, e50, e100 in zip(_p, _e20, _e50, _e100)
        ]

    _STOCH_DIV_KEYS = {"stoch_bull_div", "stoch_bear_div"}
    if compute_all or _STOCH_DIV_KEYS & needed:
        _sk = result.get("stoch_k") or _safe(compute_stochastic(high, low, close)[0])
        _pr = result.get("price") or _safe(close)
        bull_div: list[bool] = [False] * n
        bear_div: list[bool] = [False] * n
        for _i in range(5, n):
            prices_5 = [_pr[_j] for _j in range(_i - 4, _i + 1)]
            stochs_5 = [_sk[_j] if _sk[_j] is not None else 50.0 for _j in range(_i - 4, _i + 1)]
            if any(v is None for v in prices_5):
                continue
            if prices_5[-1] < min(prices_5[:-1]) and stochs_5[-1] > min(stochs_5[:-1]):
                bull_div[_i] = True
            if prices_5[-1] > max(prices_5[:-1]) and stochs_5[-1] < max(stochs_5[:-1]):
                bear_div[_i] = True
        result["stoch_bull_div"] = bull_div
        result["stoch_bear_div"] = bear_div

    _BB_SQUEEZE_KEYS = {"bb_squeeze"}
    if compute_all or _BB_SQUEEZE_KEYS & needed:
        _bw = result.get("bb_width")
        if _bw is None:
            bb_data = compute_bollinger(close)
            _bw = _safe(bb_data["width"])
        result["bb_squeeze"] = [(w is not None and w < 0.04) for w in _bw]

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

    # ── Fibonacci retracement series (lazy) ────────────────────────────
    _FIB_KEYS = {"fib_382_zone_hit", "fib_382_level", "impulse_high", "impulse_low"}
    if not compute_all and _FIB_KEYS & needed:
        try:
            from .fibonacci import compute_fib_retracement_series
            fib = compute_fib_retracement_series(high, low, close, target_level=0.382)
            result.update(fib)
        except Exception:
            logger.debug("[indicator_core] Fibonacci series computation failed", exc_info=True)

    # ── FVG series (lazy) ──────────────────────────────────────────────
    _FVG_KEYS = {"fvg_present", "fvg_high", "fvg_low"}
    if not compute_all and _FVG_KEYS & needed:
        try:
            from .fvg import compute_fvg_series
            fvg = compute_fvg_series(high, low, close)
            result.update(fvg)
        except Exception:
            logger.debug("[indicator_core] FVG series computation failed", exc_info=True)

    # ── FVG + Fibonacci confluence (lazy, requires fib series) ─────────
    if not compute_all and "fvg_fib_confluence" in needed:
        try:
            fib_level_list = result.get("fib_382_level")
            if fib_level_list is None:
                from .fibonacci import compute_fib_retracement_series
                fib = compute_fib_retracement_series(high, low, close, target_level=0.382)
                result.update(fib)
                fib_level_list = fib.get("fib_382_level", [None] * n)

            from .fvg import compute_fvg_fib_confluence_series
            conf = compute_fvg_fib_confluence_series(high, low, close, fib_level_list)
            result.update(conf)
        except Exception:
            logger.debug("[indicator_core] FVG-Fib confluence computation failed", exc_info=True)

    return result
