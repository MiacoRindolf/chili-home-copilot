"""Reusable Fair Value Gap (FVG) detection and confluence logic.

A **Fair Value Gap** is a three-candle price imbalance:

* **Bullish FVG**: ``bar[i-2].High < bar[i].Low``  (gap up; unfilled zone
  between ``bar[i-2].High`` and ``bar[i].Low``).
* **Bearish FVG**: ``bar[i-2].Low > bar[i].High`` (gap down; zone between
  ``bar[i].High`` and ``bar[i-2].Low``).

Functions in this module are pure (no DB, no network).
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Core FVG detection ─────────────────────────────────────────────────

def detect_fvg_records(
    high: pd.Series,
    low: pd.Series,
) -> list[dict[str, Any]]:
    """Return a list of FVG records found in the OHLCV data.

    Each record: ``{bar_idx, fvg_high, fvg_low, direction}``.
    ``bar_idx`` is the index of the *middle* candle of the three-bar pattern.
    """
    n = len(high)
    records: list[dict[str, Any]] = []
    h_arr = high.values
    l_arr = low.values

    for i in range(2, n):
        prev2_h = h_arr[i - 2]
        curr_l = l_arr[i]
        if prev2_h < curr_l:
            records.append({
                "bar_idx": i - 1,
                "fvg_high": float(curr_l),
                "fvg_low": float(prev2_h),
                "direction": "bull",
            })

        prev2_l = l_arr[i - 2]
        curr_h = h_arr[i]
        if prev2_l > curr_h:
            records.append({
                "bar_idx": i - 1,
                "fvg_high": float(prev2_l),
                "fvg_low": float(curr_h),
                "direction": "bear",
            })

    return records


# ── Active-FVG bar-by-bar series ───────────────────────────────────────

def compute_fvg_series(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    lookback: int = 20,
    direction_filter: str | None = "bull",
) -> dict[str, list]:
    """Compute bar-by-bar FVG indicator arrays.

    For each bar, searches the preceding *lookback* bars for **un-mitigated**
    FVGs (price has not yet traded through the gap zone).

    Returned keys (lists of length ``len(close)``):
        fvg_present – bool (True when an active FVG exists)
        fvg_high    – upper bound of nearest active FVG (float | None)
        fvg_low     – lower bound (float | None)
    """
    n = len(close)
    present: list[bool | None] = [None] * n
    fvg_hi: list[float | None] = [None] * n
    fvg_lo: list[float | None] = [None] * n

    records = detect_fvg_records(high, low)
    if direction_filter:
        records = [r for r in records if r["direction"] == direction_filter]

    for i in range(2, n):
        active_fvgs: list[dict] = []
        for rec in records:
            idx = rec["bar_idx"]
            if idx > i or idx < i - lookback:
                continue
            zone_hi = rec["fvg_high"]
            zone_lo = rec["fvg_low"]
            mitigated = False
            for j in range(idx + 1, i + 1):
                bar_lo = float(low.iloc[j])
                bar_hi = float(high.iloc[j])
                if bar_lo <= zone_lo and bar_hi >= zone_hi:
                    mitigated = True
                    break
                if rec["direction"] == "bull" and bar_lo <= zone_lo:
                    mitigated = True
                    break
                if rec["direction"] == "bear" and bar_hi >= zone_hi:
                    mitigated = True
                    break
            if not mitigated:
                active_fvgs.append(rec)

        if active_fvgs:
            nearest = max(active_fvgs, key=lambda r: r["bar_idx"])
            present[i] = True
            fvg_hi[i] = nearest["fvg_high"]
            fvg_lo[i] = nearest["fvg_low"]
        else:
            present[i] = False

    return {"fvg_present": present, "fvg_high": fvg_hi, "fvg_low": fvg_lo}


# ── FVG + Fibonacci confluence ─────────────────────────────────────────

def check_fvg_fib_confluence(
    fvg_high: float,
    fvg_low: float,
    fib_level_price: float,
    tolerance_pct: float = 0.5,
) -> bool:
    """Return True if the FVG zone overlaps or is close to *fib_level_price*.

    Overlap is detected when the fib level (expanded by *tolerance_pct* on
    each side) intersects the FVG zone ``[fvg_low, fvg_high]``.
    """
    if fib_level_price <= 0:
        return False
    tol = abs(fib_level_price) * tolerance_pct / 100
    fib_lo = fib_level_price - tol
    fib_hi = fib_level_price + tol
    return fvg_low <= fib_hi and fvg_high >= fib_lo


def compute_fvg_fib_confluence_series(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    fib_level_series: list[float | None],
    *,
    fib_tolerance_pct: float = 0.5,
    fvg_lookback: int = 20,
    direction_filter: str | None = "bull",
) -> dict[str, list]:
    """Bar-by-bar FVG–Fibonacci confluence.

    Combines :func:`compute_fvg_series` and per-bar *fib_level_series* to
    produce:
        fvg_fib_confluence    – bool
        fvg_fib_distance_pct  – float (% distance between FVG midpoint and
                                 fib level; None when no data)
    """
    fvg = compute_fvg_series(high, low, close, lookback=fvg_lookback, direction_filter=direction_filter)

    n = len(close)
    confluence: list[bool | None] = [None] * n
    distance: list[float | None] = [None] * n

    for i in range(n):
        fvg_ok = fvg["fvg_present"][i]
        fib_price = fib_level_series[i] if i < len(fib_level_series) else None

        if not fvg_ok or fib_price is None:
            confluence[i] = False if fvg_ok is not None else None
            continue

        fh = fvg["fvg_high"][i]
        fl = fvg["fvg_low"][i]
        if fh is None or fl is None:
            confluence[i] = False
            continue

        confluence[i] = check_fvg_fib_confluence(fh, fl, fib_price, fib_tolerance_pct)
        mid = (fh + fl) / 2
        if fib_price != 0:
            distance[i] = round(abs(mid - fib_price) / abs(fib_price) * 100, 4)

    return {"fvg_fib_confluence": confluence, "fvg_fib_distance_pct": distance}
