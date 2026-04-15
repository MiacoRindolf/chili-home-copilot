"""Unit tests for setup_vitals trajectory scoring."""
from __future__ import annotations

from app.services.trading.setup_vitals import _compute_vitals_from_flats, _normalized_slope


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
