"""Reusable Fibonacci retracement and impulse-leg detection.

Provides swing-pivot detection, impulse-leg identification, Fibonacci level
computation, zone-hit checks, and bar-by-bar retracement series suitable for
use by ``indicator_core.compute_all_from_df`` and ``backtest_service``.

All functions are pure (no DB, no network) and operate on pandas Series /
numpy arrays.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_FIB_LEVELS: tuple[float, ...] = (0.236, 0.382, 0.5, 0.618, 0.786)


# ── Swing pivot detection ──────────────────────────────────────────────

def find_swing_highs(high: pd.Series, lookback: int = 5) -> pd.Series:
    """Causal fractal-style pivot-high detection.

    A pivot at bar ``i`` is only CONFIRMED ``lookback`` bars later — when we
    can verify nothing in ``[i+1, i+lookback]`` exceeded ``high.iloc[i]``.
    Therefore the returned boolean Series is True at bar ``i + lookback``
    (the confirmation bar), NOT at bar ``i`` (the pivot bar).

    FIX (deep audit 2026-04-28, research_integrity strict mode): the prior
    implementation used ``rolling(window, center=True).max()`` which marks
    the pivot AT the pivot bar but requires ``lookback`` future bars to do
    so. That produced different results on truncated vs full dataframes
    (the same bar ``i`` could be flagged a pivot when computed on the full
    df but not when computed on ``df.iloc[:i+1]``), causing 5,230 lookahead
    failures in the research_integrity check across the
    "RSI + Fib 0.382 + FVG Pullback" pattern family (fingerprint
    1d81b0d2605e1417).

    The new implementation:
        rolling_max_trailing = high.rolling(window).max()
        was_pivot_lookback_bars_ago = (high.shift(lookback) == rolling_max_trailing)

    Reads as: "lookback bars ago, the high was the max of the trailing
    ``window``-bar window ending now". Equivalent to the old centred check
    but with the result lagged so it's only emitted once causally observable.

    Note: downstream callers (find_impulse_leg, compute_fib_retracement_series)
    already slice ``df.iloc[:i+1]`` per bar, so the lag doesn't change leg
    endpoints — it just makes the pivot-detection step itself causal.
    """
    window = 2 * lookback + 1
    rolling_max_trailing = high.rolling(window).max()
    high_lag = high.shift(lookback)
    return (
        (high_lag == rolling_max_trailing)
        & high_lag.notna()
        & rolling_max_trailing.notna()
    )


def find_swing_lows(low: pd.Series, lookback: int = 5) -> pd.Series:
    """Causal fractal-style pivot-low detection (mirror of
    :func:`find_swing_highs`). Pivot at bar ``i`` is emitted at bar
    ``i + lookback``. See :func:`find_swing_highs` for rationale."""
    window = 2 * lookback + 1
    rolling_min_trailing = low.rolling(window).min()
    low_lag = low.shift(lookback)
    return (
        (low_lag == rolling_min_trailing)
        & low_lag.notna()
        & rolling_min_trailing.notna()
    )


# ── Impulse leg identification ─────────────────────────────────────────

def find_impulse_leg(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    direction: str = "bull",
    lookback: int = 50,
    pivot_lookback: int = 5,
    min_bars: int = 3,
) -> dict[str, Any] | None:
    """Find the most recent impulse leg within *lookback* bars.

    For a **bullish** impulse the leg runs from the most recent confirmed
    swing-low to the subsequent swing-high that is higher.  The search
    proceeds backwards from the end of the series.

    Returns ``None`` when no qualifying leg is found.

    Return dict keys:
        start_idx, end_idx, start_price, end_price, bars, direction
    """
    n = len(high)
    if n < min_bars + 2 * pivot_lookback:
        return None

    start = max(0, n - lookback)
    seg_high = high.iloc[start:]
    seg_low = low.iloc[start:]

    if direction == "bull":
        swing_lows = find_swing_lows(seg_low, pivot_lookback)
        swing_highs = find_swing_highs(seg_high, pivot_lookback)

        low_idxs = [i for i, v in enumerate(swing_lows) if v]
        high_idxs = [i for i, v in enumerate(swing_highs) if v]

        if not low_idxs or not high_idxs:
            return None

        for hi_pos in reversed(high_idxs):
            for lo_pos in reversed(low_idxs):
                if lo_pos >= hi_pos:
                    continue
                if hi_pos - lo_pos < min_bars:
                    continue
                sp = float(seg_low.iloc[lo_pos])
                ep = float(seg_high.iloc[hi_pos])
                if ep <= sp:
                    continue
                return {
                    "start_idx": start + lo_pos,
                    "end_idx": start + hi_pos,
                    "start_price": sp,
                    "end_price": ep,
                    "bars": hi_pos - lo_pos,
                    "direction": "bull",
                }
    else:
        swing_highs = find_swing_highs(seg_high, pivot_lookback)
        swing_lows = find_swing_lows(seg_low, pivot_lookback)

        high_idxs = [i for i, v in enumerate(swing_highs) if v]
        low_idxs = [i for i, v in enumerate(swing_lows) if v]

        if not high_idxs or not low_idxs:
            return None

        for lo_pos in reversed(low_idxs):
            for hi_pos in reversed(high_idxs):
                if hi_pos >= lo_pos:
                    continue
                if lo_pos - hi_pos < min_bars:
                    continue
                sp = float(seg_high.iloc[hi_pos])
                ep = float(seg_low.iloc[lo_pos])
                if ep >= sp:
                    continue
                return {
                    "start_idx": start + hi_pos,
                    "end_idx": start + lo_pos,
                    "start_price": sp,
                    "end_price": ep,
                    "bars": lo_pos - hi_pos,
                    "direction": "bear",
                }

    return None


# ── Fibonacci level computation ────────────────────────────────────────

def compute_fib_levels(
    impulse_high: float,
    impulse_low: float,
    levels: tuple[float, ...] = DEFAULT_FIB_LEVELS,
) -> dict[float, float]:
    """Compute retracement price levels for a bullish impulse leg.

    For a bull leg (low→high), retracement levels measure how far price
    has pulled back from the high toward the low::

        level_price = high - level_ratio * (high - low)

    Returns ``{ratio: price}`` for each requested level.
    """
    rng = impulse_high - impulse_low
    if rng == 0:
        return {lvl: impulse_high for lvl in levels}
    return {lvl: round(impulse_high - lvl * rng, 6) for lvl in levels}


def check_fib_level_hit(
    price: float,
    fib_level_price: float,
    tolerance_pct: float = 0.5,
) -> bool:
    """Return True when *price* is within *tolerance_pct* % of *fib_level_price*."""
    if fib_level_price == 0:
        return False
    distance_pct = abs(price - fib_level_price) / abs(fib_level_price) * 100
    return distance_pct <= tolerance_pct


# ── Bar-by-bar retracement series ──────────────────────────────────────

def compute_fib_retracement_series(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    target_level: float = 0.382,
    tolerance_pct: float = 0.5,
    lookback: int = 50,
    pivot_lookback: int = 5,
    direction: str = "bull",
) -> dict[str, list]:
    """Compute bar-by-bar Fibonacci retracement indicator arrays.

    For each bar, searches backwards for an impulse leg and checks whether
    close is within *tolerance_pct* of the *target_level* retracement.

    Returned keys (all lists of length ``len(close)``):
        fib_{lvl}_zone_hit   – bool (None when no impulse found)
        fib_{lvl}_level      – price of the retracement level (None …)
        impulse_high         – anchor high of the impulse (None …)
        impulse_low          – anchor low of the impulse (None …)

    where ``{lvl}`` is the target level with the decimal point removed,
    e.g. ``fib_382_zone_hit`` for 0.382.
    """
    n = len(close)
    lvl_tag = str(target_level).replace("0.", "").replace(".", "").rstrip("0") or "0"
    key_hit = f"fib_{lvl_tag}_zone_hit"
    key_level = f"fib_{lvl_tag}_level"
    key_imp_hi = "impulse_high"
    key_imp_lo = "impulse_low"

    hit: list[bool | None] = [None] * n
    level: list[float | None] = [None] * n
    imp_hi: list[float | None] = [None] * n
    imp_lo: list[float | None] = [None] * n

    min_start = max(2 * pivot_lookback + 3, 20)

    for i in range(min_start, n):
        seg_start = max(0, i - lookback)
        seg_h = high.iloc[seg_start:i + 1]
        seg_l = low.iloc[seg_start:i + 1]
        seg_c = close.iloc[seg_start:i + 1]

        leg = find_impulse_leg(
            seg_h, seg_l, seg_c,
            direction=direction,
            lookback=len(seg_h),
            pivot_lookback=pivot_lookback,
            min_bars=3,
        )
        if leg is None:
            hit[i] = False
            continue

        fib_prices = compute_fib_levels(leg["end_price"], leg["start_price"], levels=(target_level,))
        fib_price = fib_prices[target_level]

        c = float(close.iloc[i])
        imp_hi[i] = leg["end_price"]
        imp_lo[i] = leg["start_price"]
        level[i] = fib_price
        hit[i] = check_fib_level_hit(c, fib_price, tolerance_pct)

    return {key_hit: hit, key_level: level, key_imp_hi: imp_hi, key_imp_lo: imp_lo}
