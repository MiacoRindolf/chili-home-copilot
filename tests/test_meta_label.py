"""Meta-label de-rate (adaptive rev): NO hard gate — the de-rate is always-on but EVIDENCE-
SCALED by a continuous confidence; it de-rates ONLY the below-base-rate (loser) profile,
shrunk toward neutral by confidence, bounded [floor,1.0], never zero (no veto)."""
from __future__ import annotations

from app.services.trading.momentum_neural.meta_label import (
    DERATE_FLOOR,
    size_multiplier,
    train_meta_label,
)


def _rows(n: int):
    rows = []
    for i in range(n):
        win = 1 if (i % 10) < 3 else 0
        ofi = (0.6 if win else -0.6) + 0.01 * (i % 5)
        rows.append({
            "run_r": 1.5 if win else 0.0, "day": f"d{i % 6}",
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


def test_unfittable_below_min_per_class():
    m = train_meta_label(_rows(20)[:4])            # 3 wins / 1 loss -> a class < min to fit
    assert m["status"] == "unfittable" and m["confidence"] == 0.0


def test_trains_with_continuous_confidence_no_gate():
    m = train_meta_label(_rows(220))
    assert m["status"] == "trained"
    assert 0.0 <= m["confidence"] <= 1.0            # a continuous weight, NOT a go/no-go flag
    assert "heldout_auc" in m and "coef" in m


def test_thin_data_trains_not_gated_off():
    # the operator's point: don't gate OFF on thin data — train + use it (gently). No hard gate.
    m = train_meta_label(_rows(40))
    assert m["status"] == "trained"                 # NOT gated off (verified live: real 43-row
    assert 0.0 <= m["confidence"] <= 1.0            # data -> confidence 0.054, a gentle de-rate)


def test_multiplier_neutral_without_model_or_confidence():
    assert size_multiplier({"ofi": 0.5}, None) == 1.0
    assert size_multiplier({"ofi": -5.0}, {"status": "trained", "confidence": 0.0,
                                           "base_rate": 0.3, "features": ["ofi"], "coef": [2.0],
                                           "intercept": 0.0, "mean": [0.0], "std": [1.0],
                                           "median": [0.0]}) == 1.0   # conf 0 -> neutral


def test_multiplier_scales_with_confidence_and_never_zero():
    base = {"status": "trained", "base_rate": 0.3, "features": ["ofi"], "coef": [2.0],
            "intercept": 0.0, "mean": [0.0], "std": [1.0], "median": [0.0]}
    full = {**base, "confidence": 1.0}
    half = {**base, "confidence": 0.5}
    assert size_multiplier({"ofi": 5.0}, full) == 1.0                 # P>base -> full size
    lo_full = size_multiplier({"ofi": -5.0}, full)                    # clear loser, full conf
    lo_half = size_multiplier({"ofi": -5.0}, half)                    # same, half conf
    assert DERATE_FLOOR <= lo_full < 1.0
    assert lo_full < lo_half < 1.0                                    # more confidence -> stronger de-rate
    assert lo_full > 0.0                                             # NEVER zero -> never a veto


def test_multiplier_missing_feature_uses_median():
    model = {"status": "trained", "confidence": 1.0, "base_rate": 0.3, "features": ["ofi"],
             "coef": [2.0], "intercept": 0.0, "mean": [0.0], "std": [1.0], "median": [0.0]}
    assert size_multiplier({}, model) == 1.0                          # median(0)->P=0.5>=base->1.0


def test_self_critic_report_structure(monkeypatch):
    # the self-critic: data-driven gap-analysis + proposals, structured report
    import app.services.trading.momentum_neural.meta_label as ml
    monkeypatch.setattr(ml, "load_training_rows", lambda db, **k: _rows(4))   # 3 pos / 1 neg -> thin
    monkeypatch.setattr(ml, "load_model", lambda p: None)
    rep = ml.analyze_learning_gaps(db=None, report_path="_nonexistent_dir_/x.json")
    assert rep["n_samples"] == 4 and isinstance(rep["feature_coverage"], dict)
    assert isinstance(rep["gaps"], list) and isinstance(rep["proposals"], list)
    assert len(rep["proposals"]) >= 1                          # always proposes something
    assert any("thin dataset" in g for g in rep["gaps"])       # 1 neg < min-per-class
