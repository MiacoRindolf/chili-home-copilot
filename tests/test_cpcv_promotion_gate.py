"""CPCV / DSR / PBO promotion gate (Q1.T1)."""
from __future__ import annotations

import numpy as np
import pytest

from app.config import settings
from app.services.trading.mining_validation import compute_deflated_sharpe_ratio, compute_pbo
from app.services.trading.promotion_gate import (
    CPCV_FEATURE_NAMES,
    LGBM_CPCV_PARAMS,
    bars_per_year,
    cpcv_vertical_max_bars,
    finalize_promotion_with_cpcv,
    normalize_mining_row_features,
    promotion_gate_passes,
)


def test_cpcv_feature_vector_order_and_lgbm_params_locked():
    assert len(CPCV_FEATURE_NAMES) == 13
    assert LGBM_CPCV_PARAMS["n_estimators"] == 200
    assert LGBM_CPCV_PARAMS["min_data_in_leaf"] == 100
    row = {k: 1.0 for k in CPCV_FEATURE_NAMES}
    row["bb_squeeze"] = True
    row["stoch_bull_div"] = False
    v = normalize_mining_row_features(row)
    assert v is not None and v.shape[0] == len(CPCV_FEATURE_NAMES)


def test_dsr_closed_form_matches_mining_helper():
    rng = np.random.default_rng(7)
    rets = (rng.normal(0.0008, 0.012, 120)).tolist()
    a = compute_deflated_sharpe_ratio(rets, n_trials=5, annualization=252.0)
    b = compute_deflated_sharpe_ratio(rets, n_trials=5, annualization=252.0)
    assert a["dsr"] == b["dsr"]
    assert a["sharpe_observed"] == b["sharpe_observed"]
    assert 0.0 <= float(a["dsr"] or 0) <= 1.0


def test_pbo_reproducible():
    rng = np.random.default_rng(0)
    mat = rng.normal(0, 0.01, (400, 2))
    a = compute_pbo(mat, rng_seed=42)
    b = compute_pbo(mat, rng_seed=42)
    assert a["pbo"] == b["pbo"]


def test_promotion_gate_passes_thresholds():
    ok, reasons = promotion_gate_passes(
        {
            "skipped": False,
            "cpcv_n_paths": 50,
            "cpcv_median_sharpe": 0.6,
            "deflated_sharpe": 0.96,
            "pbo": 0.15,
            "n_trades": 40,
        }
    )
    assert ok is True
    assert reasons == []

    ok2, reasons2 = promotion_gate_passes(
        {
            "skipped": False,
            "cpcv_n_paths": 49,
            "cpcv_median_sharpe": 0.6,
            "deflated_sharpe": 0.96,
            "pbo": 0.15,
            "n_trades": 40,
        }
    )
    assert ok2 is False
    assert "cpcv_n_paths_lt_50" in reasons2


def test_bars_per_year_and_vertical_cap():
    assert bars_per_year("1d") == 252.0
    assert cpcv_vertical_max_bars("1d") == 60
    assert cpcv_vertical_max_bars("1h") >= 60


def test_finalize_shadow_does_not_block_on_failed_metrics(monkeypatch):
    def _fake_eval(*_a, **_kw):
        return {
            "skipped": False,
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": 0.0,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": 0.1,
            "pbo": 0.9,
            "n_effective_trials": 1,
            "n_trades": 40,
            "n_labeled_samples": 40,
        }

    monkeypatch.setattr(
        "app.services.trading.promotion_gate.evaluate_pattern_cpcv",
        _fake_eval,
    )
    monkeypatch.setattr(settings, "chili_cpcv_promotion_gate_enabled", False)
    detail: dict = {"ensemble": {}}
    out = finalize_promotion_with_cpcv(detail, [{"ret_5d": 1.0}], n_hypotheses_tested=1)
    assert out.get("blocked") != "cpcv_promotion_gate_failed"
    assert "cpcv_promotion_gate" in out


def test_finalize_enforced_blocks(monkeypatch):
    def _fake_eval(*_a, **_kw):
        return {
            "skipped": False,
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": 0.0,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": 0.1,
            "pbo": 0.9,
            "n_effective_trials": 1,
            "n_trades": 40,
            "n_labeled_samples": 40,
        }

    monkeypatch.setattr(
        "app.services.trading.promotion_gate.evaluate_pattern_cpcv",
        _fake_eval,
    )
    monkeypatch.setattr(settings, "chili_cpcv_promotion_gate_enabled", True)
    detail = {}
    out = finalize_promotion_with_cpcv(detail, [{"ret_5d": 1.0}], n_hypotheses_tested=1)
    assert out.get("blocked") == "cpcv_promotion_gate_failed"


def test_purged_cv_splits_respect_sample_count():
    from skfolio.model_selection import CombinatorialPurgedCV

    n = 200
    X = np.random.default_rng(1).normal(size=(n, 4))
    cv = CombinatorialPurgedCV(n_folds=8, n_test_folds=3, purged_size=5, embargo_size=2)
    splits = list(cv.split(X))
    assert len(splits) >= 1
    for tr, te in splits[:3]:
        te_idx = np.concatenate(te) if isinstance(te, (list, tuple)) else np.asarray(te)
        assert len(np.intersect1d(np.asarray(tr, dtype=int), te_idx)) == 0
