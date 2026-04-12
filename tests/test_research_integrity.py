"""Research integrity: causality checks, provenance on persisted backtests."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from app.services.backtest_service import _compute_series_for_conditions, save_backtest
from app.services.trading.research_integrity import (
    aggregate_promotion_integrity,
    build_data_provenance,
    check_signal_bar_alignment,
    promotion_blocked_by_integrity,
    rules_json_fingerprint,
)


def _sample_ohlcv(n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    close = 100 + rng.standard_normal(n).cumsum()
    df = pd.DataFrame(
        {
            "Open": close + rng.standard_normal(n) * 0.1,
            "High": close + rng.random(n) * 2,
            "Low": close - rng.random(n) * 2,
            "Close": close,
            "Volume": rng.integers(1_000_000, 5_000_000, n),
        },
        index=pd.date_range("2022-01-01", periods=n, freq="D"),
    )
    return df


def test_rules_json_fingerprint_stable():
    c = [{"indicator": "rsi_14", "op": "<", "value": 30}]
    assert rules_json_fingerprint(c) == rules_json_fingerprint(list(c))


def test_build_data_provenance_shape():
    df = _sample_ohlcv(50)
    p = build_data_provenance(
        ticker="BTC-USD",
        period="1y",
        interval="1d",
        df=df,
        scan_pattern_id=7,
        rules_fingerprint="abc",
    )
    assert p["ticker"] == "BTC-USD"
    assert p["ohlc_bars"] == 50
    assert p["scan_pattern_id"] == 7
    assert p["rules_fingerprint"] == "abc"
    assert "chart_time_from" in p


def test_check_signal_bar_alignment_detects_corruption():
    df = _sample_ohlcv(140)
    conditions = [{"indicator": "rsi_14", "op": "<", "value": 99}]
    arrays = _compute_series_for_conditions(df, conditions)
    corrupt = {k: list(v) for k, v in arrays.items()}
    if "rsi_14" in corrupt and corrupt["rsi_14"]:
        corrupt["rsi_14"][-1] = 999.0
    out = check_signal_bar_alignment(df, conditions, corrupt, max_check_bars=30)
    assert out["lookahead_ok"] is False
    assert out["mismatches"]


def test_check_signal_bar_alignment_passes_clean():
    df = _sample_ohlcv(140)
    conditions = [{"indicator": "rsi_14", "op": "<", "value": 99}]
    arrays = _compute_series_for_conditions(df, conditions)
    out = check_signal_bar_alignment(df, conditions, arrays, max_check_bars=24)
    assert out["lookahead_ok"] is True
    assert out["causality_checked_bars"] >= 1


def test_aggregate_promotion_integrity():
    agg = aggregate_promotion_integrity(
        [
            {"ticker": "A", "lookahead_ok": True, "recursive_ok": True},
            {"ticker": "B", "lookahead_ok": False, "mismatches": [{"bar": 1}]},
        ]
    )
    assert agg["lookahead_ok_all"] is False
    assert agg["any_warnings"] is True
    assert len(agg["per_ticker"]) == 2


def test_research_integrity_strict_default_is_true():
    """Strict mode must default to True to prevent lookahead-biased patterns reaching live."""
    from app.config import Settings

    fresh = Settings()
    assert fresh.brain_research_integrity_strict is True


def test_promotion_blocked_by_integrity_strict_only(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "brain_research_integrity_strict", False, raising=False)
    assert promotion_blocked_by_integrity(
        {"lookahead_ok_all": False}, target_status="promoted"
    ) is False

    monkeypatch.setattr(settings, "brain_research_integrity_strict", True, raising=False)
    assert promotion_blocked_by_integrity(
        {"lookahead_ok_all": False}, target_status="promoted"
    ) is True
    assert promotion_blocked_by_integrity(
        {"lookahead_ok_all": True}, target_status="promoted"
    ) is False
    assert promotion_blocked_by_integrity(
        {"lookahead_ok_all": False}, target_status="candidate",
    ) is False


def test_save_backtest_merges_provenance_json(db):
    result = {
        "ticker": "ZZZ",
        "strategy": "Unit Test Pattern",
        "return_pct": 1.5,
        "win_rate": 55.0,
        "sharpe": 0.5,
        "max_drawdown": 10.0,
        "trade_count": 3,
        "equity_curve": [],
        "data_provenance": {
            "ticker": "ZZZ",
            "interval": "1d",
            "period": "1y",
            "ohlc_bars": 99,
            "status": "test",
        },
        "research_integrity": {
            "lookahead_ok": True,
            "causality_checked_bars": 10,
            "mismatches": [],
            "recursive_ok": True,
            "recursive_warnings": [],
        },
    }
    rec = save_backtest(db, None, result)
    blob = json.loads(rec.params or "{}")
    assert blob["data_provenance"]["ticker"] == "ZZZ"
    assert blob["research_integrity"]["lookahead_ok"] is True
