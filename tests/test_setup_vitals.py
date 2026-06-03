"""Unit tests for setup_vitals trajectory scoring."""
from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.setup_vitals import (
    _compute_vitals_from_flats,
    _normalized_slope,
    _ticker_vitals_row_to_setup,
)


def test_normalized_slope_trend_up():
    vals = [10.0, 11.0, 12.0, 13.0]
    s = _normalized_slope(vals)
    assert s > 0


def test_vitals_from_synthetic_flats():
    flats = []
    for i in range(8):
        rsi = 40.0 + i * 2
        flats.append({
            "price": 100.0 + i,
            "rsi_14": rsi,
            "macd_hist": 0.01 * i,
            "ema_20": 99.0 + i * 0.5,
            "ema_50": 95.0 + i * 0.3,
            "stoch_k": 30 + i,
            "obv": 1e6 + i * 100,
            "bb_pct_b": 0.4 + i * 0.02,
        })
    v = _compute_vitals_from_flats(flats, source="test")
    assert -1.0 <= v.momentum_score <= 1.0
    assert 0.0 <= v.composite_health <= 1.0
    assert "rsi_14" in v.trajectory_details


def test_ticker_vitals_row_to_setup_preserves_zero_composite_health() -> None:
    row = SimpleNamespace(
        momentum_score=0.0,
        volume_score=0.0,
        trend_score=0.0,
        overextension_risk=0.0,
        composite_health=0.0,
        divergences_json=[],
        trajectory_json={},
    )

    vitals = _ticker_vitals_row_to_setup(row)

    assert vitals.composite_health == 0.0
    assert vitals.source == "cache"


def test_ticker_vitals_row_to_setup_defaults_missing_composite_health() -> None:
    row = SimpleNamespace(
        momentum_score=None,
        volume_score=None,
        trend_score=None,
        overextension_risk=None,
        composite_health=None,
        divergences_json=[],
        trajectory_json={},
    )

    vitals = _ticker_vitals_row_to_setup(row)

    assert vitals.composite_health == 0.5
