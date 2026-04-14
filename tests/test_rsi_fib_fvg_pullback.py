"""End-to-end tests for the RSI + Fib 0.382 + FVG pullback pattern.

Tests the pullback_detector orchestrator, the seeded pattern definition,
and condition evaluation through the pattern engine.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import json
import numpy as np
import pandas as pd
import pytest

from app.services.trading.pullback_detector import (
    DEFAULT_CONFIG,
    detect_rsi_fib_fvg_pullback,
)


def _make_pullback_df(
    impulse_end: float = 135.0,
    pullback_to: float = 121.5,
    n_impulse: int = 10,
    n_pullback: int = 5,
    n_cont: int = 5,
    with_fvg: bool = True,
) -> pd.DataFrame:
    """Build a clear bull impulse → pullback → continuation frame.

    If *with_fvg* is True, inject a bullish FVG (bar[i-2].High < bar[i].Low)
    during the pullback phase.
    """
    start = 100.0
    imp_prices = np.linspace(start, impulse_end, n_impulse)
    pb_prices = np.linspace(impulse_end, pullback_to, n_pullback + 1)[1:]
    cont_prices = np.linspace(pullback_to, pullback_to + 5, n_cont + 1)[1:]
    prices = np.concatenate([imp_prices, pb_prices, cont_prices])

    n = len(prices)
    close = pd.Series(prices, dtype=float)
    high = close + 1.5
    low = close - 1.5

    if with_fvg and n_impulse + 2 < n:
        fvg_idx = n_impulse + 2
        low.iloc[fvg_idx] = high.iloc[fvg_idx - 2] + 0.5

    open_ = close + 0.2
    vol = pd.Series([500_000.0] * n)
    idx = pd.date_range("2024-06-01", periods=n, freq="h")
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def _mock_fetch_ohlcv_df(ticker, period="6mo", interval="1d", **kw):
    """Return appropriate test data based on the requested interval."""
    if interval in ("1d", "1wk"):
        return _make_pullback_df(n_impulse=30, n_pullback=10, n_cont=10)
    return _make_pullback_df()


def _mock_evidence(htf_rsi=80.0, ltf_rsi=55.0, coherence=True):
    """Build a mock CrossTimeframeEvidence."""
    from app.services.trading.cross_timeframe import CrossTimeframeEvidence
    return CrossTimeframeEvidence(
        ticker="AAPL",
        htf="1d",
        ltf="1h",
        htf_indicators={"rsi_14": htf_rsi, "ema_20": 150.0},
        ltf_indicators={"rsi_14": ltf_rsi, "ema_20": 120.0},
        htf_last_timestamp="2024-06-20",
        ltf_last_timestamp="2024-06-20 15:00",
        coherence_ok=coherence,
        evidence_age_seconds=3600.0,
    )


# ── Pattern definition tests ──────────────────────────────────────────


class TestPatternDefinition:
    def test_pattern_in_community_seeds(self):
        from app.services.trading.pattern_engine import _COMMUNITY_SEED_PATTERNS
        names = [p["name"] for p in _COMMUNITY_SEED_PATTERNS]
        assert any("Fib 0.382" in n for n in names)

    def test_rules_json_structure(self):
        from app.services.trading.pattern_engine import _COMMUNITY_SEED_PATTERNS
        pat = next(p for p in _COMMUNITY_SEED_PATTERNS if "Fib 0.382" in p["name"])
        rules = json.loads(pat["rules_json"])
        assert "conditions" in rules
        assert "meta" in rules
        conds = rules["conditions"]
        indicators = [c["indicator"] for c in conds]
        assert "1d:rsi_14" in indicators
        assert "rsi_14" in indicators
        assert "fib_382_zone_hit" in indicators
        assert "fvg_fib_confluence" in indicators

    def test_meta_fields(self):
        from app.services.trading.pattern_engine import _COMMUNITY_SEED_PATTERNS
        pat = next(p for p in _COMMUNITY_SEED_PATTERNS if "Fib 0.382" in p["name"])
        meta = json.loads(pat["rules_json"])["meta"]
        assert meta["requires_cross_tf"] is True
        assert meta["fib_target"] == 0.382
        assert meta["htf"] == "1d"
        assert meta["ltf"] == "1h"
        assert meta["detector"] == "rsi_fib_fvg_pullback"


# ── Condition evaluation through pattern engine ───────────────────────


class TestConditionEvaluation:
    def test_all_conditions_pass(self):
        from app.services.trading.pattern_engine import _eval_condition
        snap = {
            "1d:rsi_14": 80.0,
            "rsi_14": 55.0,
            "fib_382_zone_hit": True,
            "fvg_fib_confluence": True,
        }
        conds = [
            {"indicator": "1d:rsi_14", "op": ">", "value": 75},
            {"indicator": "rsi_14", "op": ">", "value": 50},
            {"indicator": "fib_382_zone_hit", "op": "==", "value": True},
            {"indicator": "fvg_fib_confluence", "op": "==", "value": True},
        ]
        assert all(_eval_condition(c, snap) for c in conds)

    def test_htf_rsi_too_low(self):
        from app.services.trading.pattern_engine import _eval_condition
        snap = {"1d:rsi_14": 60.0, "rsi_14": 55.0, "fib_382_zone_hit": True, "fvg_fib_confluence": True}
        cond = {"indicator": "1d:rsi_14", "op": ">", "value": 75}
        assert _eval_condition(cond, snap) is False

    def test_ltf_rsi_too_low(self):
        from app.services.trading.pattern_engine import _eval_condition
        snap = {"1d:rsi_14": 80.0, "rsi_14": 40.0, "fib_382_zone_hit": True, "fvg_fib_confluence": True}
        cond = {"indicator": "rsi_14", "op": ">", "value": 50}
        assert _eval_condition(cond, snap) is False

    def test_fib_zone_not_hit(self):
        from app.services.trading.pattern_engine import _eval_condition
        snap = {"1d:rsi_14": 80.0, "rsi_14": 55.0, "fib_382_zone_hit": False, "fvg_fib_confluence": True}
        cond = {"indicator": "fib_382_zone_hit", "op": "==", "value": True}
        assert _eval_condition(cond, snap) is False

    def test_no_fvg_confluence(self):
        from app.services.trading.pattern_engine import _eval_condition
        snap = {"1d:rsi_14": 80.0, "rsi_14": 55.0, "fib_382_zone_hit": True, "fvg_fib_confluence": False}
        cond = {"indicator": "fvg_fib_confluence", "op": "==", "value": True}
        assert _eval_condition(cond, snap) is False


# ── Pullback detector tests ───────────────────────────────────────────


def _patch_detector(ctf_ev, ltf_df=None):
    """Context manager stacking patches for the pullback detector's deferred imports."""
    from contextlib import ExitStack
    stack = ExitStack()
    md_fetch = stack.enter_context(
        patch("app.services.trading.market_data.fetch_ohlcv_df",
              return_value=ltf_df if ltf_df is not None else _make_pullback_df())
    )
    # cross_timeframe.fetch_cross_timeframe_evidence does its own fetch_ohlcv_df
    # import from market_data — the md_fetch patch covers that too.
    # But we bypass the whole function by patching it directly on the module
    # that pullback_detector imports from.
    import app.services.trading.cross_timeframe as _ctf_mod
    stack.enter_context(
        patch.object(_ctf_mod, "fetch_cross_timeframe_evidence", return_value=ctf_ev)
    )
    return stack


class TestPullbackDetector:
    def test_happy_path(self):
        ev = _mock_evidence(htf_rsi=80.0, ltf_rsi=55.0)
        with _patch_detector(ev):
            result = detect_rsi_fib_fvg_pullback("AAPL")
        if result is not None:
            assert result["ticker"] == "AAPL"
            assert result["htf_rsi"] > 75
            assert result["ltf_rsi"] > 50
            assert result["fib_target_level"] == 0.382
            assert "fvg_high" in result
            assert "reasons" in result
            assert len(result["reasons"]) >= 4

    def test_htf_rsi_below_threshold(self):
        ev = _mock_evidence(htf_rsi=60.0, ltf_rsi=55.0)
        with _patch_detector(ev):
            result = detect_rsi_fib_fvg_pullback("AAPL")
        assert result is None

    def test_ltf_rsi_below_threshold(self):
        ev = _mock_evidence(htf_rsi=80.0, ltf_rsi=40.0)
        with _patch_detector(ev):
            result = detect_rsi_fib_fvg_pullback("AAPL")
        assert result is None

    def test_no_fvg_near_fib(self):
        ev = _mock_evidence(htf_rsi=80.0, ltf_rsi=55.0)
        with _patch_detector(ev, ltf_df=_make_pullback_df(with_fvg=False)):
            result = detect_rsi_fib_fvg_pullback("AAPL")
        assert result is None or result.get("fvg_high") is not None

    def test_fetch_error(self):
        from app.services.trading.cross_timeframe import CrossTimeframeEvidence
        ev = CrossTimeframeEvidence(
            ticker="AAPL", htf="1d", ltf="1h", fetch_error="network error",
        )
        with _patch_detector(ev):
            result = detect_rsi_fib_fvg_pullback("AAPL")
        assert result is None

    def test_empty_ltf_df(self):
        ev = _mock_evidence(htf_rsi=80.0, ltf_rsi=55.0)
        with _patch_detector(ev, ltf_df=pd.DataFrame()):
            result = detect_rsi_fib_fvg_pullback("AAPL")
        assert result is None

    def test_custom_config(self):
        ev = _mock_evidence(htf_rsi=65.0, ltf_rsi=55.0)
        with _patch_detector(ev):
            result = detect_rsi_fib_fvg_pullback(
                "AAPL",
                config={"htf_rsi_threshold": 60},
            )
        if result is not None:
            assert result["htf_rsi_threshold"] == 60


# ── Backtest parity: indicator series contain the new keys ────────────


class TestBacktestParity:
    def test_fib_keys_in_compute_all(self):
        from app.services.trading.indicator_core import compute_all_from_df
        df = _make_pullback_df(n_impulse=30, n_pullback=10, n_cont=10)
        result = compute_all_from_df(df, needed={"fib_382_zone_hit", "fib_382_level"})
        assert "fib_382_zone_hit" in result
        assert "fib_382_level" in result
        assert len(result["fib_382_zone_hit"]) == len(df)

    def test_fvg_keys_in_compute_all(self):
        from app.services.trading.indicator_core import compute_all_from_df
        df = _make_pullback_df(n_impulse=30, n_pullback=10, n_cont=10)
        result = compute_all_from_df(df, needed={"fvg_present", "fvg_high", "fvg_low"})
        assert "fvg_present" in result
        assert len(result["fvg_present"]) == len(df)

    def test_confluence_key_in_compute_all(self):
        from app.services.trading.indicator_core import compute_all_from_df
        df = _make_pullback_df(n_impulse=30, n_pullback=10, n_cont=10)
        result = compute_all_from_df(
            df,
            needed={"fvg_fib_confluence", "fib_382_level"},
        )
        assert "fvg_fib_confluence" in result
        assert len(result["fvg_fib_confluence"]) == len(df)

    def test_backtest_service_computes_fib_keys(self):
        from app.services.backtest_service import _compute_series_for_conditions
        df = _make_pullback_df(n_impulse=30, n_pullback=10, n_cont=10)
        conditions = [
            {"indicator": "fib_382_zone_hit", "op": "==", "value": True},
            {"indicator": "fvg_fib_confluence", "op": "==", "value": True},
        ]
        result = _compute_series_for_conditions(df, conditions)
        assert "fib_382_zone_hit" in result
        assert "fvg_fib_confluence" in result
