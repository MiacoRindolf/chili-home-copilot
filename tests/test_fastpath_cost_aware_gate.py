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
    gate_calibrated_tradeability,
    gate_live_alpha_evidence,
    gate_pullback_ticker_allowed,
)


def _alert(*, ticker="BTC-USD", alert_type="volume_breakout_long",
           signal_score=0.85):
    return {
        "ticker": ticker,
        "alert_type": alert_type,
        "signal_score": signal_score,
        "fired_at": datetime.utcnow(),
    }


def _ctx(*, spread_bps=5.0, engine="fake_engine_object", mode="paper"):
    return ExecContext(
        now_wall=datetime.utcnow(),
        best_bid=100.0,
        best_ask=100.05,
        spread_bps=spread_bps,
        engine=engine,
        mode=mode,
    )


def _stub_fp_settings(
    *, enabled: bool = True, taker_fee_bps: float = 5.0,
    live_alpha_gate: bool = True, live_min_samples: int = 50,
    live_min_net_bps: float = 0.0,
):
    """Patch fast_path.settings.load to return a stub with the given knobs."""
    from app.services.trading.fast_path.settings import FastPathSettings

    return FastPathSettings(
        cost_aware_admission_enabled=enabled,
        cost_aware_taker_fee_bps=taker_fee_bps,
        live_alpha_evidence_gate_enabled=live_alpha_gate,
        live_alpha_min_samples=live_min_samples,
        live_alpha_min_net_bps=live_min_net_bps,
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


def test_calibrated_tradeability_defers_when_cost_aware_enabled():
    """Avoid the stale static cost bar shadowing the dynamic cost gate."""
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True),
    ), patch(
        "app.services.trading.fast_path.calibration.is_score_tradeable",
        side_effect=AssertionError("should not consult static tradeability"),
    ):
        result = gate_calibrated_tradeability(_alert(), _ctx())
    assert result.allow is True
    assert result.detail.get("verdict") == "deferred_to_cost_aware_admission"


def test_pullback_allowlist_blocks_btc_after_realized_drift():
    result = gate_pullback_ticker_allowed(
        _alert(ticker="BTC-USD", alert_type="volume_breakout_pullback_long"),
        _ctx(),
    )
    assert result.allow is False
    assert result.reason == "pullback_ticker_not_allowed:BTC-USD"


def test_pullback_allowlist_keeps_sol():
    result = gate_pullback_ticker_allowed(
        _alert(ticker="SOL-USD", alert_type="volume_breakout_pullback_long"),
        _ctx(),
    )
    assert result.allow is True


def test_live_alpha_evidence_allows_paper_exploration_without_engine():
    result = gate_live_alpha_evidence(_alert(), _ctx(engine=None, mode="paper"))
    assert result.allow is True
    assert result.detail["verdict"] == "paper_mode"


def test_live_alpha_evidence_blocks_live_without_engine():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(live_alpha_gate=True),
    ):
        result = gate_live_alpha_evidence(_alert(), _ctx(engine=None, mode="live"))
    assert result.allow is False
    assert result.reason == "no_engine"


def test_live_alpha_evidence_blocks_insufficient_decay_samples():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(
            enabled=True, taker_fee_bps=5.0, live_min_samples=50,
        ),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=None,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_live_alpha_evidence(_alert(), _ctx(spread_bps=2.0, mode="live"))
    assert result.allow is False
    assert result.reason == "insufficient_decay_evidence"


def test_live_alpha_evidence_allows_live_when_bucket_clears_cost():
    best = {"horizon_s": 60, "sample_count": 75, "mean_return": 0.003}
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(
            enabled=True, taker_fee_bps=5.0, live_min_samples=50,
            live_min_net_bps=0.0,
        ),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[best],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=best,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_live_alpha_evidence(_alert(), _ctx(spread_bps=2.0, mode="live"))
    assert result.allow is True
    assert result.detail["net_bps"] == pytest.approx(16.0)


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
