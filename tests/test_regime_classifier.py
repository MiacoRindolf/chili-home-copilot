"""Q1.T2 Gaussian HMM regime classifier tests."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from app.config import settings
from app.models.trading import MarketSnapshot, RegimeSnapshot
from app.services.trading.regime_classifier import (
    FEATURE_NAMES,
    FEATURE_SPEC_V1,
    compute_model_version_hash,
    fit_regime_model,
    predict_regime,
    relabel_by_mean_return,
)


def _load_backfill_module():
    root = Path(__file__).resolve().parents[1]
    p = root / "scripts" / "backfill_regime.py"
    spec = importlib.util.spec_from_file_location("backfill_regime", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _synth_feature_frame(n: int = 3200, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ix = pd.date_range("2010-01-04", periods=n, freq="B", tz=None)
    return pd.DataFrame(
        {
            FEATURE_NAMES[0]: rng.normal(0, 0.01, n),
            FEATURE_NAMES[1]: np.abs(rng.normal(0.15, 0.05, n)),
            FEATURE_NAMES[2]: rng.normal(0, 0.05, n),
            FEATURE_NAMES[3]: 15.0 + rng.normal(0, 3, n),
            FEATURE_NAMES[4]: rng.normal(0.5, 0.2, n),
        },
        index=ix,
    )


def test_fit_reproducibility():
    df = _synth_feature_frame(400, seed=7)
    m1, v1 = fit_regime_model(df, n_iter=80, random_state=42, covariance_type="diag")
    m2, v2 = fit_regime_model(df, n_iter=80, random_state=42, covariance_type="diag")
    assert v1 == v2
    assert np.allclose(m1.means_, m2.means_, rtol=1e-6, atol=1e-6)
    assert np.allclose(m1.covars_, m2.covars_, rtol=1e-5, atol=1e-5)


def test_relabel_by_mean_return_deterministic():
    from hmmlearn.hmm import GaussianHMM

    m = GaussianHMM(n_components=3, covariance_type="full", n_iter=1, random_state=0)
    m.means_ = np.array(
        [
            [0.02, 0, 0, 0, 0],
            [0.0, 0, 0, 0, 0],
            [-0.02, 0, 0, 0, 0],
        ],
        dtype=float,
    )
    m.covars_ = np.array([np.eye(5) * 0.01] * 3)
    m.transmat_ = np.full((3, 3), 1.0 / 3.0)
    m.startprob_ = np.full(3, 1.0 / 3.0)
    lm = relabel_by_mean_return(m)
    assert lm[2] == "bear" and lm[1] == "chop" and lm[0] == "bull"


def test_relabel_produces_three_distinct_labels():
    df = _synth_feature_frame(450)
    for seed in range(6):
        model, _ver = fit_regime_model(
            df, n_iter=50, random_state=seed, covariance_type="diag"
        )
        lm = relabel_by_mean_return(model)
        assert set(lm.values()) == {"bull", "chop", "bear"}
        assert len(lm) == 3


def test_posterior_sums_to_one():
    df = _synth_feature_frame(300)
    model, _ = fit_regime_model(df, n_iter=80, random_state=1, covariance_type="diag")
    lm = relabel_by_mean_return(model)
    x = df.iloc[-1].values.astype(float)
    _lab, post = predict_regime(model, x, lm)
    assert abs(sum(post.values()) - 1.0) < 1e-6


def test_viterbi_decode_matches_sampled_states():
    from hmmlearn.hmm import GaussianHMM

    rng = np.random.default_rng(42)
    g = GaussianHMM(
        n_components=3,
        covariance_type="diag",
        n_iter=100,
        random_state=99,
        min_covar=1e-3,
    )
    g.fit(rng.normal(size=(400, 5)))
    X, Z = g.sample(600)
    _, states = g.decode(X)
    assert np.mean(states == Z) > 0.75


def test_warm_start_stability():
    df1 = _synth_feature_frame(600, seed=1)
    df2 = _synth_feature_frame(600, seed=2)
    df = pd.concat([df1.iloc[:300], df2.iloc[300:]])
    m0, _ = fit_regime_model(df1, n_iter=80, random_state=0, covariance_type="diag")
    m1, _ = fit_regime_model(
        df, n_iter=80, random_state=0, warm_start_model=m0, covariance_type="diag"
    )
    lm0 = relabel_by_mean_return(m0)
    lm1 = relabel_by_mean_return(m1)
    bear0 = m0.means_[next(k for k, v in lm0.items() if v == "bear")][0]
    bear1 = m1.means_[next(k for k, v in lm1.items() if v == "bear")][0]
    assert abs(bear0 - bear1) < 0.15


def test_flag_off_is_noop(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_regime_classifier_enabled", False)
    snap = MarketSnapshot(
        ticker="SPY",
        snapshot_date=datetime.utcnow(),
        close_price=400.0,
        bar_interval="1d",
        bar_start_at=datetime(2020, 6, 1, 0, 0, 0),
        snapshot_legacy=False,
    )
    db.add(RegimeSnapshot(as_of=snap.bar_start_at, regime="bull", posterior={"bull": 1.0}, features={}, model_version="t"))
    db.commit()
    from app.services.trading.regime_classifier import attach_regime_to_market_snapshot

    attach_regime_to_market_snapshot(db, snap)
    assert snap.regime is None
    assert snap.regime_posterior is None


def test_model_version_hash_stability():
    t0 = datetime(2020, 1, 1)
    t1 = datetime(2024, 1, 1)
    a = compute_model_version_hash(
        train_start=t0, train_end=t1, feature_spec=FEATURE_SPEC_V1, random_state=42
    )
    b = compute_model_version_hash(
        train_start=t0, train_end=t1, feature_spec=FEATURE_SPEC_V1, random_state=42
    )
    assert a == b
    c = compute_model_version_hash(
        train_start=t0, train_end=t1, feature_spec=FEATURE_SPEC_V1 + "x", random_state=42
    )
    assert c != a


def test_graceful_fallback_on_missing_features_logs(monkeypatch, caplog):
    import logging
    from unittest.mock import MagicMock

    caplog.set_level(logging.WARNING)
    from app.services.trading import market_data as md
    from app.services.trading import regime_classifier as rc

    def _empty(*_a, **_k):
        return pd.DataFrame(columns=list(FEATURE_NAMES))

    monkeypatch.setattr(md, "fetch_ohlcv_df", _empty)
    m_q = MagicMock()
    m_q.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.return_value = m_q
    out = rc.build_regime_features(
        db, datetime(2022, 1, 1), datetime(2022, 6, 1), log_missing_yield=True
    )
    assert out.empty


def test_backfill_regime_dry_run_is_write_free(db, monkeypatch):
    def _fake_bf(_db, _s, _e, **kw):
        rng = np.random.default_rng(0)
        end = pd.Timestamp.now("UTC").replace(tzinfo=None).normalize()
        ix = pd.bdate_range(end=end, periods=400)
        return pd.DataFrame(
            {
                FEATURE_NAMES[0]: rng.normal(0, 0.01, len(ix)),
                FEATURE_NAMES[1]: np.abs(rng.normal(0.15, 0.05, len(ix))),
                FEATURE_NAMES[2]: rng.normal(0, 0.05, len(ix)),
                FEATURE_NAMES[3]: 15.0 + rng.normal(0, 3, len(ix)),
                FEATURE_NAMES[4]: rng.normal(0.5, 0.2, len(ix)),
            },
            index=ix,
        )

    monkeypatch.setattr(
        "app.services.trading.regime_classifier.build_regime_features",
        _fake_bf,
    )
    mod = _load_backfill_module()
    _sm, _sv = fit_regime_model(
        _synth_feature_frame(220, seed=9),
        n_iter=20,
        random_state=0,
        covariance_type="diag",
    )

    monkeypatch.setattr(mod, "fit_regime_model", lambda *a, **k: (_sm, _sv))
    monkeypatch.setattr(
        mod,
        "predict_regime",
        lambda *_a, **_k: ("chop", {"bull": 0.2, "chop": 0.6, "bear": 0.2}),
    )

    before_rs = int(db.execute(text("SELECT COUNT(*) FROM regime_snapshot")).scalar_one())
    before_ms = int(db.execute(text("SELECT COUNT(*) FROM trading_snapshots")).scalar_one())

    monkeypatch.setattr(sys, "argv", ["backfill_regime", "--dry-run"])
    assert mod.main() == 0

    db.expire_all()
    assert int(db.execute(text("SELECT COUNT(*) FROM regime_snapshot")).scalar_one()) == before_rs
    assert int(db.execute(text("SELECT COUNT(*) FROM trading_snapshots")).scalar_one()) == before_ms
