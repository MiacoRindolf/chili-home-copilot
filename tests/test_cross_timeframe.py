"""Tests for the cross-timeframe evidence assembler (app.services.trading.cross_timeframe)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd
import pytest

from app.services.trading.cross_timeframe import (
    CrossTimeframeEvidence,
    build_cross_tf_snapshot_keys,
    eval_cross_timeframe_conditions,
    fetch_cross_timeframe_evidence,
)


def _make_ohlcv(n: int = 60, seed: int = 42, base: float = 100) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = base + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0.5, 2.0, n)
    low = close - rng.uniform(0.5, 2.0, n)
    open_ = close + rng.normal(0, 0.5, n)
    vol = rng.integers(100_000, 1_000_000, n).astype(float)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


# ── Evidence dataclass ────────────────────────────────────────────────


class TestCrossTimeframeEvidence:
    def test_default_fields(self):
        ev = CrossTimeframeEvidence(ticker="AAPL", htf="1d", ltf="1h")
        assert ev.ticker == "AAPL"
        assert ev.coherence_ok is False
        assert ev.fetch_error is None
        d = ev.to_dict()
        assert d["ticker"] == "AAPL"

    def test_to_dict_roundtrip(self):
        ev = CrossTimeframeEvidence(
            ticker="BTC-USD", htf="1d", ltf="1h",
            htf_indicators={"rsi_14": 78.5},
            ltf_indicators={"rsi_14": 55.2},
            coherence_ok=True,
        )
        d = ev.to_dict()
        assert d["htf_indicators"]["rsi_14"] == 78.5
        assert d["coherence_ok"] is True


# ── eval_cross_timeframe_conditions ───────────────────────────────────


class TestEvalCrossTimeframeConditions:
    def test_htf_and_ltf_pass(self):
        ev = CrossTimeframeEvidence(
            ticker="AAPL", htf="1d", ltf="1h",
            htf_indicators={"rsi_14": 80.0, "ema_20": 150.0},
            ltf_indicators={"rsi_14": 55.0},
            coherence_ok=True,
        )
        result = eval_cross_timeframe_conditions(
            ev,
            htf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 75}],
            ltf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 50}],
        )
        assert result["htf_pass"] is True
        assert result["ltf_pass"] is True
        assert result["all_pass"] is True

    def test_htf_fails(self):
        ev = CrossTimeframeEvidence(
            ticker="AAPL", htf="1d", ltf="1h",
            htf_indicators={"rsi_14": 60.0},
            ltf_indicators={"rsi_14": 55.0},
            coherence_ok=True,
        )
        result = eval_cross_timeframe_conditions(
            ev,
            htf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 75}],
            ltf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 50}],
        )
        assert result["htf_pass"] is False
        assert result["all_pass"] is False

    def test_ltf_fails(self):
        ev = CrossTimeframeEvidence(
            ticker="AAPL", htf="1d", ltf="1h",
            htf_indicators={"rsi_14": 80.0},
            ltf_indicators={"rsi_14": 40.0},
            coherence_ok=True,
        )
        result = eval_cross_timeframe_conditions(
            ev,
            htf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 75}],
            ltf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 50}],
        )
        assert result["ltf_pass"] is False
        assert result["all_pass"] is False

    def test_coherence_failure_blocks_all_pass(self):
        ev = CrossTimeframeEvidence(
            ticker="AAPL", htf="1d", ltf="1h",
            htf_indicators={"rsi_14": 80.0},
            ltf_indicators={"rsi_14": 55.0},
            coherence_ok=False,
        )
        result = eval_cross_timeframe_conditions(
            ev,
            htf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 75}],
            ltf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 50}],
        )
        assert result["htf_pass"] is True
        assert result["ltf_pass"] is True
        assert result["all_pass"] is False

    def test_empty_conditions(self):
        ev = CrossTimeframeEvidence(
            ticker="AAPL", htf="1d", ltf="1h",
            htf_indicators={"rsi_14": 80.0},
            ltf_indicators={"rsi_14": 55.0},
            coherence_ok=True,
        )
        result = eval_cross_timeframe_conditions(ev, htf_conditions=[], ltf_conditions=[])
        assert result["all_pass"] is True

    def test_missing_indicator_fails(self):
        ev = CrossTimeframeEvidence(
            ticker="AAPL", htf="1d", ltf="1h",
            htf_indicators={},
            ltf_indicators={"rsi_14": 55.0},
            coherence_ok=True,
        )
        result = eval_cross_timeframe_conditions(
            ev,
            htf_conditions=[{"indicator": "rsi_14", "op": ">", "value": 75}],
            ltf_conditions=[],
        )
        assert result["htf_pass"] is False


# ── build_cross_tf_snapshot_keys ──────────────────────────────────────


class TestBuildCrossTfSnapshotKeys:
    def test_htf_prefixed_ltf_unprefixed(self):
        ev = CrossTimeframeEvidence(
            ticker="AAPL", htf="1d", ltf="1h",
            htf_indicators={"rsi_14": 80.0, "ema_20": 150.0},
            ltf_indicators={"rsi_14": 55.0},
        )
        keys = build_cross_tf_snapshot_keys(ev)
        assert keys["1d:rsi_14"] == 80.0
        assert keys["1d:ema_20"] == 150.0
        assert keys["rsi_14"] == 55.0

    def test_same_ticker_guaranteed(self):
        ev = CrossTimeframeEvidence(
            ticker="BTC-USD", htf="1d", ltf="1h",
            htf_indicators={"rsi_14": 30.0},
            ltf_indicators={"rsi_14": 45.0},
        )
        keys = build_cross_tf_snapshot_keys(ev)
        assert "1d:rsi_14" in keys


# ── fetch_cross_timeframe_evidence (mocked) ───────────────────────────


class TestFetchCrossTimeframeEvidence:
    @patch("app.services.trading.market_data.fetch_ohlcv_df")
    def test_returns_evidence_on_success(self, mock_fetch):
        df = _make_ohlcv(60)
        mock_fetch.return_value = df
        ev = fetch_cross_timeframe_evidence("AAPL", htf="1d", ltf="1h")
        assert ev.ticker == "AAPL"
        assert "rsi_14" in ev.htf_indicators
        assert "rsi_14" in ev.ltf_indicators
        assert ev.fetch_error is None

    @patch("app.services.trading.market_data.fetch_ohlcv_df")
    def test_handles_fetch_error(self, mock_fetch):
        mock_fetch.side_effect = RuntimeError("network down")
        ev = fetch_cross_timeframe_evidence("AAPL")
        assert ev.fetch_error is not None
        assert ev.coherence_ok is False

    @patch("app.services.trading.market_data.fetch_ohlcv_df")
    def test_handles_empty_df(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame()
        ev = fetch_cross_timeframe_evidence("AAPL")
        assert ev.fetch_error is not None
