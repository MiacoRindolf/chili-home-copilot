"""Data quality filters for OHLCV data.

Applied before mining, backtesting, or snapshot creation to reject
bad bars that would pollute indicators and pattern signals.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def detect_stock_split(df: pd.DataFrame, threshold: float = 0.45) -> list[dict[str, Any]]:
    """Detect probable stock splits where close changes by >threshold in one bar.

    Returns list of {index, ratio, close_before, close_after} for flagged bars.
    """
    if df.empty or len(df) < 2:
        return []
    close = df["Close"].astype(float)
    pct = close.pct_change().abs()
    splits = []
    for i in range(1, len(pct)):
        if pct.iloc[i] >= threshold:
            ratio = round(close.iloc[i] / close.iloc[i - 1], 3)
            splits.append({
                "index": i,
                "date": str(df.index[i])[:10] if hasattr(df.index[i], "strftime") else str(i),
                "ratio": ratio,
                "close_before": float(close.iloc[i - 1]),
                "close_after": float(close.iloc[i]),
            })
    return splits


def filter_bad_prints(df: pd.DataFrame, z_threshold: float = 5.0) -> pd.DataFrame:
    """Remove bars where OHLCV values are statistical outliers (bad prints).

    Uses z-score on close-to-close returns. Bars with |z| > z_threshold are dropped.
    """
    if df.empty or len(df) < 10:
        return df
    close = df["Close"].astype(float)
    rets = close.pct_change()
    mu = rets.mean()
    sigma = rets.std()
    if sigma == 0 or pd.isna(sigma):
        return df
    z = ((rets - mu) / sigma).abs()
    mask = (z <= z_threshold) | z.isna()
    dropped = (~mask).sum()
    if dropped > 0:
        logger.debug("[data_quality] Dropped %d bad-print bars (z>%.1f)", dropped, z_threshold)
    return df[mask].copy()


def filter_zero_volume(df: pd.DataFrame) -> pd.DataFrame:
    """Drop bars with zero volume (often after-hours placeholder bars)."""
    if df.empty or "Volume" not in df.columns:
        return df
    before = len(df)
    df = df[df["Volume"] > 0].copy()
    dropped = before - len(df)
    if dropped > 0:
        logger.debug("[data_quality] Dropped %d zero-volume bars", dropped)
    return df


def validate_ohlcv_integrity(df: pd.DataFrame) -> dict[str, Any]:
    """Check OHLCV data for common issues. Returns a report dict."""
    report: dict[str, Any] = {
        "bars": len(df),
        "issues": [],
    }
    if df.empty:
        report["issues"].append("empty_dataframe")
        return report

    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in df.columns:
            report["issues"].append(f"missing_column_{col}")
            return report

    null_counts = df[["Open", "High", "Low", "Close"]].isnull().sum()
    for col, cnt in null_counts.items():
        if cnt > 0:
            report["issues"].append(f"null_{col.lower()}_{cnt}")

    bad_hl = (df["High"] < df["Low"]).sum()
    if bad_hl > 0:
        report["issues"].append(f"high_below_low_{bad_hl}")

    bad_range = ((df["Close"] > df["High"]) | (df["Close"] < df["Low"])).sum()
    if bad_range > 0:
        report["issues"].append(f"close_outside_range_{bad_range}")

    neg_vol = (df["Volume"] < 0).sum()
    if neg_vol > 0:
        report["issues"].append(f"negative_volume_{neg_vol}")

    splits = detect_stock_split(df)
    if splits:
        report["issues"].append(f"probable_splits_{len(splits)}")
        report["splits"] = splits

    report["clean"] = len(report["issues"]) == 0
    return report


def clean_ohlcv(df: pd.DataFrame, *, z_threshold: float = 5.0) -> pd.DataFrame:
    """Apply all quality filters in sequence. Safe for indicator computation."""
    if df.empty:
        return df
    df = filter_zero_volume(df)
    df = filter_bad_prints(df, z_threshold=z_threshold)
    return df
