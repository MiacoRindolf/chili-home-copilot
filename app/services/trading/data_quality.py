"""Data quality filters for OHLCV data.

Applied before mining, backtesting, or snapshot creation to reject
bad bars that would pollute indicators and pattern signals.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_SPLIT_RATIO_TOLERANCE = 0.06
_COMMON_SPLIT_RATIOS = (
    0.10,
    0.125,
    0.20,
    0.25,
    0.50,
    2.0,
    3.0,
    4.0,
    5.0,
    8.0,
    10.0,
)


def _is_crypto_symbol(symbol: str | None) -> bool:
    return str(symbol or "").strip().upper().endswith("-USD")


def _split_detection_interval_allowed(interval: str | None) -> bool:
    if not interval:
        return True
    s = str(interval).strip().lower()
    return s in {"1d", "d", "day", "daily", "1wk", "1w", "wk", "week", "weekly"}


def _looks_like_common_split_ratio(ratio: float) -> bool:
    if ratio <= 0:
        return False
    for target in _COMMON_SPLIT_RATIOS:
        if abs(ratio - target) / target <= _SPLIT_RATIO_TOLERANCE:
            return True
    return False


def detect_stock_split(df: pd.DataFrame, threshold: float = 0.45) -> list[dict[str, Any]]:
    """Detect unadjusted stock splits without blocking ordinary large gaps.

    Returns list of {index, ratio, close_before, close_after} for flagged bars.
    """
    if df.empty or len(df) < 2:
        return []
    close = df["Close"].astype(float)
    pct = close.pct_change().abs()
    splits = []
    for i in range(1, len(pct)):
        if pct.iloc[i] >= threshold:
            raw_ratio = float(close.iloc[i] / close.iloc[i - 1])
            ratio = round(raw_ratio, 3)
            if not _looks_like_common_split_ratio(raw_ratio):
                continue
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


def _is_index_symbol(symbol: str | None) -> bool:
    """Index tickers (^VIX, ^GSPC, ^DJI, ^IXIC, ^RUT, etc.) legitimately
    report zero volume on yfinance/Polygon. They are price-only series.

    Round-21 FIX (2026-04-30, third-party audit HIGH): the prior
    ``filter_zero_volume`` blanket-dropped all zero-volume bars, so
    ^VIX and friends came out of ``clean_ohlcv`` empty. Regime gates
    that depend on VIX then saw "no data" and silently skipped or
    used stale fallbacks.
    """
    if not symbol:
        return False
    s = symbol.strip()
    if not s:
        return False
    # yfinance index convention is the leading caret.
    if s.startswith("^"):
        return True
    # Polygon convention is the I: prefix.
    if s.upper().startswith("I:"):
        return True
    return False


def filter_zero_volume(df: pd.DataFrame, *, symbol: str | None = None) -> pd.DataFrame:
    """Drop bars with zero volume (often after-hours placeholder bars).

    Skipped entirely for index tickers (``^VIX``, ``^GSPC``, etc.) where
    zero volume is normal and dropping bars would empty the series.
    Callers that know the symbol should pass it; ``clean_ohlcv`` does so.
    """
    if df.empty or "Volume" not in df.columns:
        return df
    if _is_index_symbol(symbol):
        # Don't filter -- zero volume is the norm for index price series.
        return df
    before = len(df)
    df = df[df["Volume"] > 0].copy()
    dropped = before - len(df)
    if dropped > 0:
        logger.debug("[data_quality] Dropped %d zero-volume bars", dropped)
    return df


def validate_ohlcv_integrity(
    df: pd.DataFrame,
    *,
    symbol: str | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
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

    splits = []
    if (
        not _is_crypto_symbol(symbol)
        and _split_detection_interval_allowed(interval)
    ):
        splits = detect_stock_split(df)
    if splits:
        report["issues"].append(f"probable_splits_{len(splits)}")
        report["splits"] = splits

    report["clean"] = len(report["issues"]) == 0
    return report


def clean_ohlcv(
    df: pd.DataFrame,
    *,
    z_threshold: float = 5.0,
    symbol: str | None = None,
) -> pd.DataFrame:
    """Apply all quality filters in sequence. Safe for indicator computation.

    Round-21 FIX (2026-04-30): pass ``symbol`` to ``filter_zero_volume`` so
    index tickers (^VIX, ^GSPC, ...) keep their bars. Without ``symbol``
    the filter falls back to old blanket-drop behavior (safe for stocks).
    """
    if df.empty:
        return df
    df = filter_zero_volume(df, symbol=symbol)
    df = filter_bad_prints(df, z_threshold=z_threshold)
    return df
