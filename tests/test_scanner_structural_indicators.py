from __future__ import annotations

import pandas as pd

from app.services.trading.scanner import (
    PATTERN_STRUCTURAL_BB_WIDTH_LOOKBACK_BARS,
    PATTERN_STRUCTURAL_VCP_LOOKBACK_BARS,
    _pattern_structural_indicators_from_df,
)


TEST_EXTRA_ROWS = 5
TEST_PRICE = 100.25
TEST_RESISTANCE = 102.0
TEST_BAR_RANGE = 2.0


def test_pattern_structural_indicators_expose_backtest_parity_fields() -> None:
    rows = max(
        PATTERN_STRUCTURAL_VCP_LOOKBACK_BARS,
        PATTERN_STRUCTURAL_BB_WIDTH_LOOKBACK_BARS,
    ) + TEST_EXTRA_ROWS
    index = pd.date_range("2026-01-01", periods=rows, freq="D")
    high_values = [TEST_RESISTANCE - 1.0 + idx * 0.01 for idx in range(rows)]
    low_values = [high - TEST_BAR_RANGE for high in high_values]
    close_values = [low + (high - low) * 0.25 for high, low in zip(high_values, low_values)]
    volume_values = [float(rows - idx) for idx in range(rows)]
    df = pd.DataFrame(
        {
            "Open": close_values,
            "High": high_values,
            "Low": low_values,
            "Close": close_values,
            "Volume": volume_values,
        },
        index=index,
    )

    out = _pattern_structural_indicators_from_df(
        df,
        price=TEST_PRICE,
        resistance=TEST_RESISTANCE,
    )

    assert "ibs" in out
    assert "pullback_stretch_entry" in out
    assert "daily_change_pct" in out
    assert "bb_width_percentile" in out
    assert "bb_squeeze" in out
    assert "bb_squeeze_firing" in out
    assert "vwap_reclaim" in out
    assert "support" in out
    assert "price_dist_to_support_pct" in out
    assert "low_pierce_support_pct" in out
    assert "close_above_support" in out
    assert "close_higher_after_test" in out
    assert "narrow_range" in out
    assert "vcp_count" in out
    assert "resistance_retests" in out
    assert "retest_range_tightening" in out
