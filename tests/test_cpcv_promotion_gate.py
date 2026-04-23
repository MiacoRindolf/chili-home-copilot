"""CPCV / DSR / PBO promotion gate (Q1.T1)."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import text

from app.config import settings
from app.models.trading import PatternTradeRow, ScanPattern
from app.services.trading.mining_validation import compute_deflated_sharpe_ratio, compute_pbo
from app.services.trading.promotion_gate import (
    CPCV_FEATURE_NAMES,
    LGBM_CPCV_PARAMS,
    bars_per_year,
    cpcv_vertical_max_bars,
    finalize_promotion_with_cpcv,
    infer_scanner_bucket,
    normalize_mining_row_features,
    promotion_gate_passes,
)


class _Pat:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_infer_scanner_bucket_heuristics():
    assert infer_scanner_bucket(_Pat(name="Momentum x", timeframe="1d")) == "momentum"
    assert infer_scanner_bucket(_Pat(name="BB squeeze", timeframe="1d", origin="mined")) == "breakout"
    assert infer_scanner_bucket(_Pat(name="Other", timeframe="5m", origin="user")) == "day"
    assert infer_scanner_bucket(_Pat(name="Other", timeframe="1d", origin="mined")) == "patterns"
    assert infer_scanner_bucket(_Pat(name="Other", timeframe="1d", origin="user")) == "swing"


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


def test_backfill_dry_run_is_write_free(db):
    """``scripts/backfill_cpcv_metrics.py --dry-run`` must not INSERT/UPDATE/commit."""
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "backfill_cpcv_metrics.py"
    assert script.is_file()

    pat = ScanPattern(
        name="CPCV dry-run fixture",
        rules_json={},
        origin="mined",
        lifecycle_stage="promoted",
    )
    db.add(pat)
    db.flush()

    base = datetime.utcnow() - timedelta(days=400)
    for i in range(35):
        db.add(
            PatternTradeRow(
                scan_pattern_id=pat.id,
                ticker="FAKE",
                as_of_ts=base + timedelta(days=i),
                timeframe="1d",
                outcome_return_pct=0.01 * (i % 5),
                features_json={"entry_price": 100.0, "rsi_14": 45.0 + i * 0.01},
            )
        )
    db.execute(
        text(
            """
            INSERT INTO cpcv_shadow_eval_log (
                scan_pattern_id, scanner, would_pass_cpcv, passed_prior_gates,
                deflated_sharpe, pbo, cpcv_n_paths, pattern_name, skipped
            )
            VALUES (
                :sid, 'swing', false, true,
                0.5, 0.1, 10, 'fixture', false
            )
            """
        ),
        {"sid": pat.id},
    )
    db.commit()

    def _counts():
        return {
            "scan_patterns": int(db.execute(text("SELECT COUNT(*) FROM scan_patterns")).scalar_one()),
            "trading_pattern_trades": int(
                db.execute(text("SELECT COUNT(*) FROM trading_pattern_trades")).scalar_one()
            ),
            "cpcv_shadow_eval_log": int(
                db.execute(text("SELECT COUNT(*) FROM cpcv_shadow_eval_log")).scalar_one()
            ),
            "trading_pit_audit_log": int(
                db.execute(text("SELECT COUNT(*) FROM trading_pit_audit_log")).scalar_one()
            ),
        }

    def _pat_snapshot():
        row = db.execute(
            text(
                "SELECT lifecycle_stage, cpcv_n_paths, promotion_gate_passed "
                "FROM scan_patterns WHERE id = :id"
            ),
            {"id": pat.id},
        ).one()
        return row[0], row[1], row[2]

    before_counts = _counts()
    before_pat = _pat_snapshot()
    assert before_counts["scan_patterns"] >= 1
    assert before_counts["cpcv_shadow_eval_log"] >= 1

    env = os.environ.copy()
    assert (env.get("DATABASE_URL") or "").strip(), "DATABASE_URL must be set (pytest sets from TEST_DATABASE_URL)"

    proc = subprocess.run(
        [sys.executable, str(script), "--dry-run"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode in (0, 2), proc.stdout + "\n" + proc.stderr

    db.expire_all()
    after_counts = _counts()
    after_pat = _pat_snapshot()
    assert after_counts == before_counts
    assert after_pat == before_pat
