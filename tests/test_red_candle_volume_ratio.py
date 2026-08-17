"""Red-candle volume ratio (Ross 2026-08-17 Aral #18: "Higher volume on the red
candle" = distribution tell). Telemetry-first pure helper."""
from __future__ import annotations

import pandas as pd

from app.services.trading.momentum_neural.candles import (
    red_candle_volume_ratio_from_df,
)


def _df(rows):
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"])


def test_distribution_pattern_ratio_above_one():
    """Malakas ang volume sa pulang bars → ratio > 1 (ang babala ni Ross)."""
    df = _df([
        [1.00, 1.06, 0.99, 1.05, 1000],   # berde
        [1.05, 1.07, 1.01, 1.02, 5000],   # pula, malakas
        [1.02, 1.05, 1.01, 1.04, 800],    # berde
        [1.04, 1.05, 0.98, 0.99, 7000],   # pula, malakas
    ])
    r = red_candle_volume_ratio_from_df(df, lookback_bars=10)
    assert r is not None and r > 1.0
    assert abs(r - (12000.0 / 1800.0)) < 1e-9


def test_accumulation_pattern_ratio_below_one():
    df = _df([
        [1.00, 1.06, 0.99, 1.05, 9000],   # berde, malakas
        [1.05, 1.07, 1.01, 1.02, 500],    # pula, mahina
        [1.02, 1.08, 1.01, 1.07, 8000],   # berde, malakas
    ])
    r = red_candle_volume_ratio_from_df(df)
    assert r is not None and r < 1.0


def test_lookback_limits_window():
    rows = [[1.00, 1.10, 0.99, 0.90, 100000]] + [
        [1.00, 1.06, 0.99, 1.05, 100] for _ in range(10)
    ]
    r = red_candle_volume_ratio_from_df(_df(rows), lookback_bars=10)
    # ang malaking pulang bar ay LABAS sa 10-bar lookback → walang red volume
    assert r == 0.0


def test_fail_open_on_missing_or_thin():
    assert red_candle_volume_ratio_from_df(None) is None
    assert red_candle_volume_ratio_from_df(_df([[1, 1, 1, 1, 100]])) is None
    df_no_vol = pd.DataFrame(
        [[1.0, 1.1, 0.9, 1.05], [1.05, 1.1, 1.0, 1.02]],
        columns=["Open", "High", "Low", "Close"],
    )
    assert red_candle_volume_ratio_from_df(df_no_vol) is None


def test_all_doji_is_none():
    df = _df([[1.0, 1.1, 0.9, 1.0, 500], [1.0, 1.05, 0.95, 1.0, 600]])
    assert red_candle_volume_ratio_from_df(df) is None
