"""Backtest / live parity tests for indicator computation and condition evaluation.

These tests ensure that:
1. The shared indicator_core produces the same results used by both paths
2. The backtest condition evaluator delegates to the canonical pattern_engine
3. Signal replay: a known set of conditions evaluated bar-by-bar in backtest
   matches the live evaluation on the same data
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _make_ohlcv_df(n: int = 100, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame for testing."""
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0.5, 2.0, n)
    low = close - rng.uniform(0.5, 2.0, n)
    open_ = close + rng.normal(0, 0.5, n)
    volume = rng.integers(100_000, 1_000_000, n).astype(float)

    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume,
    }, index=idx)


class TestConditionEvaluatorParity:
    """The backtest _eval_condition_bt must delegate to pattern_engine._eval_condition."""

    def test_simple_gt(self):
        from app.services.backtest_service import _eval_condition_bt
        from app.services.trading.pattern_engine import _eval_condition

        cond = {"indicator": "rsi_14", "op": ">", "value": 50}
        snap = {"rsi_14": 65.0}
        assert _eval_condition_bt(cond, snap) == _eval_condition(cond, snap) == True

        snap2 = {"rsi_14": 35.0}
        assert _eval_condition_bt(cond, snap2) == _eval_condition(cond, snap2) == False

    def test_ref_comparison(self):
        from app.services.backtest_service import _eval_condition_bt
        from app.services.trading.pattern_engine import _eval_condition

        cond = {"indicator": "macd", "op": ">", "ref": "macd_signal"}
        snap = {"macd": 0.5, "macd_signal": 0.3}
        assert _eval_condition_bt(cond, snap) == _eval_condition(cond, snap) == True

    def test_between(self):
        from app.services.backtest_service import _eval_condition_bt
        from app.services.trading.pattern_engine import _eval_condition

        cond = {"indicator": "rsi_14", "op": "between", "value": [30, 70]}
        snap = {"rsi_14": 50.0}
        assert _eval_condition_bt(cond, snap) == _eval_condition(cond, snap) == True

        snap2 = {"rsi_14": 80.0}
        assert _eval_condition_bt(cond, snap2) == _eval_condition(cond, snap2) == False

    def test_missing_indicator(self):
        from app.services.backtest_service import _eval_condition_bt
        from app.services.trading.pattern_engine import _eval_condition

        cond = {"indicator": "missing_ind", "op": ">", "value": 0}
        snap = {"rsi_14": 50}
        assert _eval_condition_bt(cond, snap) == _eval_condition(cond, snap) == False


class TestIndicatorCoreParity:
    """indicator_core.compute_all_from_df should produce correct arrays."""

    def test_rsi_values_in_range(self):
        from app.services.trading.indicator_core import compute_all_from_df

        df = _make_ohlcv_df(100)
        result = compute_all_from_df(df, needed={"rsi_14"})
        rsi_vals = [v for v in result["rsi_14"] if v is not None]
        assert len(rsi_vals) > 50
        assert all(0 <= v <= 100 for v in rsi_vals)

    def test_ema_length_matches(self):
        from app.services.trading.indicator_core import compute_all_from_df

        df = _make_ohlcv_df(100)
        result = compute_all_from_df(df, needed={"ema_20", "sma_20"})
        assert len(result["ema_20"]) == 100
        assert len(result["sma_20"]) == 100

    def test_macd_components(self):
        from app.services.trading.indicator_core import compute_all_from_df

        df = _make_ohlcv_df(100)
        result = compute_all_from_df(df, needed={"macd", "macd_signal", "macd_hist"})
        assert "macd" in result
        assert "macd_signal" in result
        assert "macd_hist" in result
        assert len(result["macd"]) == 100

    def test_relative_volume(self):
        from app.services.trading.indicator_core import compute_all_from_df

        df = _make_ohlcv_df(100)
        result = compute_all_from_df(df, needed={"rel_vol"})
        rvs = [v for v in result["rel_vol"] if v is not None]
        assert len(rvs) > 50
        assert all(v > 0 for v in rvs)

    def test_gap_pct(self):
        from app.services.trading.indicator_core import compute_all_from_df

        df = _make_ohlcv_df(100)
        result = compute_all_from_df(df, needed={"gap_pct"})
        gaps = [v for v in result["gap_pct"] if v is not None]
        assert len(gaps) >= 99


class TestSignalReplay:
    """Verify that evaluating conditions bar-by-bar in backtest style
    matches live-style evaluation on the same data point."""

    def test_replay_rsi_oversold(self):
        from app.services.trading.indicator_core import compute_all_from_df
        from app.services.trading.pattern_engine import _eval_condition
        from app.services.backtest_service import _eval_condition_bt

        df = _make_ohlcv_df(100)
        arrays = compute_all_from_df(df, needed={"rsi_14", "price"})
        cond = {"indicator": "rsi_14", "op": "<", "value": 40}

        for i in range(len(df)):
            snap = {k: arr[i] for k, arr in arrays.items() if arr[i] is not None}
            live_result = _eval_condition(cond, snap)
            bt_result = _eval_condition_bt(cond, snap)
            assert live_result == bt_result, f"Mismatch at bar {i}: live={live_result}, bt={bt_result}"

    def test_replay_multi_condition(self):
        from app.services.trading.indicator_core import compute_all_from_df
        from app.services.trading.pattern_engine import _eval_condition
        from app.services.backtest_service import _eval_condition_bt

        df = _make_ohlcv_df(200)
        needed = {"rsi_14", "adx", "macd_hist", "price"}
        arrays = compute_all_from_df(df, needed=needed)

        conditions = [
            {"indicator": "rsi_14", "op": "<", "value": 45},
            {"indicator": "adx", "op": ">", "value": 20},
            {"indicator": "macd_hist", "op": ">", "value": 0},
        ]

        for i in range(len(df)):
            snap = {k: arr[i] for k, arr in arrays.items() if arr[i] is not None}
            for cond in conditions:
                live_result = _eval_condition(cond, snap)
                bt_result = _eval_condition_bt(cond, snap)
                assert live_result == bt_result, (
                    f"Mismatch at bar {i} for {cond}: live={live_result}, bt={bt_result}"
                )
