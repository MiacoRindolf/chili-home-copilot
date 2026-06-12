"""OHLCV integrity checks for large gaps and split-like moves."""
from __future__ import annotations

import pandas as pd

from app.services.trading.data_quality import (
    clean_ohlcv,
    filter_bad_prints,
    validate_ohlcv_integrity,
)


def _ohlcv(closes: list[float], volumes: list[int] | None = None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": volumes if volumes is not None else [100_000 for _ in closes],
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


def _hypermover_ohlcv() -> pd.DataFrame:
    """DSY/NPT-shaped frame: 61 quiet low-float days, then a REAL +99%
    day (1.29 -> 2.57, the NPT 2026-06-09 move) on explosive volume.
    The close ratio lands on the 2.0 "split ratio"; the volume says
    momentum, not corporate action.
    """
    closes = [1.25, 1.26] * 30 + [1.29, 2.57]
    volumes = [300_000] * 61 + [80_000_000]
    return _ohlcv(closes, volumes)


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


def test_real_hypermover_double_day_is_not_flagged_as_split() -> None:
    """Regression (2026-06-12): DSY/NPT/MTEN-style +100% days on explosive
    volume were rejected as probable_splits, silently nulling every 1d
    feature for exactly the Ross-lane names the momentum system targets.
    """
    report = validate_ohlcv_integrity(
        _hypermover_ohlcv(),
        symbol="NPT",
        interval="1d",
    )

    assert report["clean"] is True
    assert report["issues"] == []


def test_clean_ohlcv_keeps_volume_confirmed_hypermover_bar() -> None:
    """The +99% bar is a huge z-outlier but must survive filter_bad_prints:
    dropping it silently corrupts gap / RVOL / continuation features.
    """
    df = _hypermover_ohlcv()

    cleaned = clean_ohlcv(df, symbol="NPT")

    assert len(cleaned) == len(df)
    assert float(cleaned["Close"].iloc[-1]) == 2.57
    report = validate_ohlcv_integrity(cleaned, symbol="NPT", interval="1d")
    assert report["clean"] is True


def test_real_crash_day_after_pump_is_not_flagged_as_split() -> None:
    """Regression (2026-06-12, live NPT 03-18): a -49% collapse the bar
    after a vertical pump lands on the 0.5 "split ratio". The trailing
    tape is the pump itself, so the crash volume only matches it -- the
    frame-wide median baseline must confirm the crash as a real move.
    """
    closes = [15.0, 15.75, 8.05] + [8.0, 7.9] * 20
    volumes = [60_000_000, 90_000_000, 70_000_000] + [400_000] * 40

    report = validate_ohlcv_integrity(
        _ohlcv(closes, volumes),
        symbol="NPT",
        interval="1d",
    )

    assert report["clean"] is True
    assert report["issues"] == []


def test_unadjusted_forward_split_with_flat_dollar_volume_still_flagged() -> None:
    """2:1 forward split: price halves, share volume doubles mechanically,
    dollar turnover stays ~1x the trailing tape -> still a probable split.
    """
    closes = [100.0] * 30 + [50.0]
    volumes = [1_000_000] * 30 + [2_000_000]

    report = validate_ohlcv_integrity(
        _ohlcv(closes, volumes),
        symbol="AAPL",
        interval="1d",
    )

    assert report["clean"] is False
    assert report["issues"] == ["probable_splits_1"]


def test_unadjusted_reverse_split_with_flat_dollar_volume_still_flagged() -> None:
    """1:10 reverse split: price 10x on ~1x dollar turnover -> probable split."""
    closes = [0.50] * 30 + [5.0]
    volumes = [10_000_000] * 30 + [1_000_000]

    report = validate_ohlcv_integrity(
        _ohlcv(closes, volumes),
        symbol="SNDL",
        interval="1d",
    )

    assert report["clean"] is False
    assert report["issues"] == ["probable_splits_1"]


def test_bad_print_without_volume_expansion_is_still_dropped() -> None:
    """A z-outlier spike on ordinary volume is a bad print and must still
    be filtered -- the volume exemption only protects volume-confirmed moves.
    """
    closes = [9.95, 10.05] * 30
    closes[15] = 16.0
    df = _ohlcv(closes, [500_000] * 60)

    filtered = filter_bad_prints(df)

    assert 16.0 not in filtered["Close"].astype(float).tolist()
    assert len(filtered) < len(df)
