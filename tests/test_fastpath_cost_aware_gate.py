"""Tests for f-fastpath-universe-rotation gate_cost_aware_admission.

Helper-level tests with mocked settings + mocked calibration helpers.
No DB / no broker.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from app.services.trading.fast_path.gates import (
    ExecContext,
    gate_cost_aware_admission,
)


def _alert(*, ticker="BTC-USD", alert_type="volume_breakout_long",
           signal_score=0.85):
    return {
        "ticker": ticker,
        "alert_type": alert_type,
        "signal_score": signal_score,
        "fired_at": datetime.utcnow(),
    }


def _ctx(*, spread_bps=5.0, engine="fake_engine_object"):
    return ExecContext(
        now_wall=datetime.utcnow(),
        best_bid=100.0,
        best_ask=100.05,
        spread_bps=spread_bps,
        engine=engine,
    )


def _stub_fp_settings(
    *, enabled: bool = True, taker_fee_bps: float = 5.0,
):
    """Patch fast_path.settings.load to return a stub with the given knobs."""
    from app.services.trading.fast_path.settings import FastPathSettings

    return FastPathSettings(
        cost_aware_admission_enabled=enabled,
        cost_aware_taker_fee_bps=taker_fee_bps,
    )


# ---------------------------------------------------------------------------
# Disabled flag short-circuit
# ---------------------------------------------------------------------------

def test_gate_disabled_returns_allow_with_disabled_verdict():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=False),
    ):
        result = gate_cost_aware_admission(_alert(), _ctx())
    assert result.allow is True
    assert result.detail.get("verdict") == "disabled"


# ---------------------------------------------------------------------------
# No engine -> allow with verdict='no_engine'
# ---------------------------------------------------------------------------

def test_gate_no_engine_allows_through():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True),
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(engine=None))
    assert result.allow is True
    assert result.detail.get("verdict") == "no_engine"


# ---------------------------------------------------------------------------
# Best-row mean clears 2x cost -> allow
# ---------------------------------------------------------------------------

def test_gate_clears_when_mean_above_round_trip_cost():
    """Cost = 2 * (5 bps fee + 5 bps spread) = 20 bps = 0.002.
    Mean = 30 bps = 0.003 -> clears."""
    fake_row = {
        "horizon_s": 60,
        "sample_count": 100,
        "mean_return": 0.003,
        "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True, taker_fee_bps=5.0),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(spread_bps=5.0))
    assert result.allow is True
    assert result.detail["verdict"] == "cleared"
    assert result.detail["mean_return_bps"] == pytest.approx(30.0, abs=0.01)
    assert result.detail["cost_bps"] == pytest.approx(20.0, abs=0.01)


# ---------------------------------------------------------------------------
# Best-row mean below 2x cost -> reject
# ---------------------------------------------------------------------------

def test_gate_rejects_when_mean_below_round_trip_cost():
    """Cost = 2 * (5 + 5) = 20 bps. Mean = 10 bps = 0.001 -> reject."""
    fake_row = {
        "horizon_s": 60,
        "sample_count": 100,
        "mean_return": 0.001,
        "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True, taker_fee_bps=5.0),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(spread_bps=5.0))
    assert result.allow is False
    assert result.reason == "below_round_trip_cost"
    assert result.detail["verdict"] == "below_cost"
    assert result.detail["mean_return_bps"] == pytest.approx(10.0, abs=0.01)
    assert result.detail["cost_bps"] == pytest.approx(20.0, abs=0.01)


# ---------------------------------------------------------------------------
# No data -> allow with no_data verdict (cold-start safety)
# ---------------------------------------------------------------------------

def test_gate_no_data_allows_through_with_no_data_verdict():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[],
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx())
    assert result.allow is True
    assert result.detail["verdict"] == "no_data"


# ---------------------------------------------------------------------------
# Lookup failure -> allow (mirrors gate_calibrated_tradeability behaviour)
# ---------------------------------------------------------------------------

def test_gate_lookup_failure_allows_through():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        side_effect=RuntimeError("simulated DB failure"),
    ):
        result = gate_cost_aware_admission(_alert(), _ctx())
    assert result.allow is True
    assert result.detail["verdict"] == "lookup_failed"


# ---------------------------------------------------------------------------
# Live spread surfaces in detail (non-zero spread reflected)
# ---------------------------------------------------------------------------

def test_gate_uses_live_spread_from_ctx():
    fake_row = {
        "horizon_s": 60, "sample_count": 100,
        "mean_return": 0.002, "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True, taker_fee_bps=5.0),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        # Spread = 15 bps -> cost = 2 * (5 + 15) = 40 bps
        result = gate_cost_aware_admission(
            _alert(), _ctx(spread_bps=15.0),
        )
    assert result.detail["spread_bps"] == pytest.approx(15.0, abs=0.01)
    assert result.detail["cost_bps"] == pytest.approx(40.0, abs=0.01)
    # Mean = 20 bps, cost = 40 bps -> below
    assert result.allow is False
