"""Tests for the reusable FVG module (app.services.trading.fvg)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.trading.fvg import (
    check_fvg_fib_confluence,
    compute_fvg_fib_confluence_series,
    compute_fvg_series,
    detect_fvg_records,
)


def _make_ohlcv(n: int = 60, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0.5, 2.0, n)
    low = close - rng.uniform(0.5, 2.0, n)
    open_ = close + rng.normal(0, 0.5, n)
    vol = rng.integers(100_000, 1_000_000, n).astype(float)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def _make_fvg_df() -> pd.DataFrame:
    """Build a DataFrame with a guaranteed bullish FVG at index 2 (middle candle 1).

    Bullish FVG: bar[0].High < bar[2].Low
    """
    data = {
        "Open":  [100.0, 103.0, 108.0, 110.0, 109.0],
        "High":  [102.0, 106.0, 112.0, 111.0, 110.0],
        "Low":   [99.0,  102.5, 107.0, 108.0, 108.0],
        "Close": [101.0, 105.0, 111.0, 109.5, 109.0],
        "Volume": [500_000.0] * 5,
    }
    idx = pd.date_range("2024-06-01", periods=5, freq="D")
    return pd.DataFrame(data, index=idx)


# ── FVG record detection ──────────────────────────────────────────────


class TestDetectFvgRecords:
    def test_bullish_fvg_detected(self):
        df = _make_fvg_df()
        records = detect_fvg_records(df["High"], df["Low"])
        bull = [r for r in records if r["direction"] == "bull"]
        assert len(bull) >= 1
        rec = bull[0]
        assert rec["fvg_low"] < rec["fvg_high"]

    def test_bearish_fvg_with_gap_down(self):
        data = {
            "Open":  [110.0, 107.0, 100.0, 99.0, 98.0],
            "High":  [112.0, 108.0, 101.0, 100.0, 99.0],
            "Low":   [109.0, 106.0, 99.0,  98.0,  97.0],
            "Close": [111.0, 106.5, 99.5,  98.5,  97.5],
        }
        df = pd.DataFrame(data)
        records = detect_fvg_records(df["High"], df["Low"])
        bear = [r for r in records if r["direction"] == "bear"]
        assert len(bear) >= 1

    def test_no_fvg_in_tight_range(self):
        close = pd.Series([100.0] * 10)
        high = close + 0.1
        low = close - 0.1
        records = detect_fvg_records(high, low)
        assert len(records) == 0

    def test_returns_correct_bar_idx(self):
        df = _make_fvg_df()
        records = detect_fvg_records(df["High"], df["Low"])
        for rec in records:
            assert 0 < rec["bar_idx"] < len(df) - 1


# ── FVG series ────────────────────────────────────────────────────────


class TestComputeFvgSeries:
    def test_series_length(self):
        df = _make_ohlcv(60)
        result = compute_fvg_series(df["High"], df["Low"], df["Close"])
        assert len(result["fvg_present"]) == 60

    def test_fvg_present_has_bools(self):
        df = _make_fvg_df()
        result = compute_fvg_series(df["High"], df["Low"], df["Close"], lookback=5)
        for val in result["fvg_present"]:
            assert val is None or isinstance(val, bool)

    def test_known_fvg_detected(self):
        df = _make_fvg_df()
        result = compute_fvg_series(df["High"], df["Low"], df["Close"], lookback=5)
        has_fvg = any(v is True for v in result["fvg_present"])
        assert has_fvg, "Known FVG should be detected"

    def test_direction_filter(self):
        df = _make_fvg_df()
        bull = compute_fvg_series(df["High"], df["Low"], df["Close"], direction_filter="bull")
        bear = compute_fvg_series(df["High"], df["Low"], df["Close"], direction_filter="bear")
        both = compute_fvg_series(df["High"], df["Low"], df["Close"], direction_filter=None)
        bull_count = sum(1 for v in bull["fvg_present"] if v is True)
        bear_count = sum(1 for v in bear["fvg_present"] if v is True)
        both_count = sum(1 for v in both["fvg_present"] if v is True)
        assert both_count >= bull_count
        assert both_count >= bear_count


# ── FVG-Fib confluence ────────────────────────────────────────────────


class TestFvgFibConfluence:
    def test_overlap(self):
        assert check_fvg_fib_confluence(105.0, 103.0, 104.0, tolerance_pct=0.5) is True

    def test_no_overlap(self):
        assert check_fvg_fib_confluence(105.0, 103.0, 120.0, tolerance_pct=0.5) is False

    def test_edge_overlap_with_tolerance(self):
        assert check_fvg_fib_confluence(103.0, 101.0, 103.5, tolerance_pct=1.0) is True

    def test_zero_fib_level(self):
        assert check_fvg_fib_confluence(105.0, 103.0, 0.0, tolerance_pct=1.0) is False


class TestFvgFibConfluenceSeries:
    def test_series_length(self):
        df = _make_ohlcv(50)
        fib_levels = [None] * 20 + [105.0] * 30
        result = compute_fvg_fib_confluence_series(
            df["High"], df["Low"], df["Close"], fib_levels,
        )
        assert len(result["fvg_fib_confluence"]) == 50
        assert len(result["fvg_fib_distance_pct"]) == 50

    def test_none_when_no_fib(self):
        df = _make_ohlcv(30)
        fib_levels = [None] * 30
        result = compute_fvg_fib_confluence_series(
            df["High"], df["Low"], df["Close"], fib_levels,
        )
        for v in result["fvg_fib_confluence"]:
            assert v is not True
