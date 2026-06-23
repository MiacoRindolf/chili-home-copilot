"""Meta-label de-rate (meta_label.py): the gate refuses thin data, and the size multiplier
de-rates ONLY the below-base-rate (loser) profile, bounded [floor,1.0], never zero (no veto).
"""
from __future__ import annotations

from app.services.trading.momentum_neural.meta_label import (
    DEFAULT_FEATURES,
    DERATE_FLOOR,
    size_multiplier,
    train_meta_label,
)


def _rows(n: int):
    rows = []
    for i in range(n):
        win = 1 if (i % 10) < 3 else 0  # ~30% positive
        ofi = (0.6 if win else -0.6) + 0.01 * (i % 5)
        rows.append({
            "run_r": 1.5 if win else 0.0,
            "day": f"d{i % 6}",
            "features": {
                "ofi": ofi, "micro_edge_bps": (1.0 if win else -1.0),
                "vol_ratio": 3.0, "sustained_rvol": 2.0, "front_side_score": 0.6,
                "vwap_dist_sigma": 1.0, "day_range_pos": 0.8, "retrace_from_hod": 0.1,
                "spread_bps": 20.0, "atr_pct": 0.02, "stop_pct_eff": 0.03,
                "dollar_vol": 1e6, "liq_mult": 1.0, "above_vwap": 1.0,
                "is_backside": 0.0, "premarket": 0.0,
            },
        })
    return rows


def test_insufficient_data_no_go():
    m = train_meta_label(_rows(40))
    assert m["status"] == "insufficient_data" and m["go"] is False


def test_trains_on_enough_data():
    m = train_meta_label(_rows(220))
    assert m["status"] == "trained"
    assert m["n"] >= 120 and m["positives"] >= 20
    assert "coef" in m and len(m["coef"]) == len(DEFAULT_FEATURES)


def test_multiplier_neutral_without_gated_model():
    assert size_multiplier({"ofi": 0.5}, None) == 1.0                      # no model
    assert size_multiplier({"ofi": 0.5}, {"status": "trained", "go": False}) == 1.0  # not gated


def test_multiplier_derates_only_loser_profile():
    model = {
        "status": "trained", "go": True, "base_rate": 0.3,
        "features": ["ofi"], "coef": [2.0], "intercept": 0.0,
        "mean": [0.0], "std": [1.0], "median": [0.0],
    }
    hi = size_multiplier({"ofi": 5.0}, model)    # P high (>base) -> full size
    lo = size_multiplier({"ofi": -5.0}, model)   # P low (<base) -> de-rated
    assert hi == 1.0
    assert DERATE_FLOOR <= lo < 1.0
    assert lo > 0.0                               # NEVER zero -> never a veto


def test_multiplier_missing_feature_uses_median():
    model = {
        "status": "trained", "go": True, "base_rate": 0.3,
        "features": ["ofi"], "coef": [2.0], "intercept": 0.0,
        "mean": [0.0], "std": [1.0], "median": [0.0],
    }
    # absent 'ofi' -> median (0.0) -> P==0.5 >= base -> neutral 1.0, no crash
    assert size_multiplier({}, model) == 1.0
