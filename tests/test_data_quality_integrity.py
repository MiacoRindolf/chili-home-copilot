"""OHLCV integrity checks for large gaps and split-like moves."""
from __future__ import annotations

import pandas as pd

from app.services.trading.data_quality import validate_ohlcv_integrity


def _ohlcv(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [100_000 for _ in closes],
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


def test_daily_large_gap_not_near_split_ratio_stays_clean() -> None:
    report = validate_ohlcv_integrity(
        _ohlcv([214.34, 318.10]),
        symbol="MDB",
        interval="1d",
    )

    assert report["clean"] is True
    assert report["issues"] == []


def test_daily_common_split_ratio_is_still_flagged() -> None:
    report = validate_ohlcv_integrity(
        _ohlcv([100.0, 50.0]),
        symbol="AAPL",
        interval="1d",
    )

    assert report["clean"] is False
    assert report["issues"] == ["probable_splits_1"]


def test_crypto_large_gap_is_not_treated_as_stock_split() -> None:
    report = validate_ohlcv_integrity(
        _ohlcv([1.0, 2.6]),
        symbol="FET-USD",
        interval="1d",
    )

    assert report["clean"] is True
    assert report["issues"] == []


def test_intraday_large_stock_gap_is_not_treated_as_split() -> None:
    report = validate_ohlcv_integrity(
        _ohlcv([7.38, 13.81]),
        symbol="ERNA",
        interval="1h",
    )

    assert report["clean"] is True
    assert report["issues"] == []
