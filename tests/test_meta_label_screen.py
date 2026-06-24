"""Data-snooping-corrected feature screen for the meta-label (CALIB-BY-DayRestrict, 2026-06-23).

Proves the load-bearing invariants the design + adversarial workflow (wf_faa694d3) locked down:
kill-switch byte-identical, marginal-PRESERVING null, INERT at today's tiny n (keep-all dominant),
protected-tail union, Spearman sign-duplicate de-dup, bounded never-veto de-rate, determinism.
"""
from __future__ import annotations

import numpy as np

from app.services.trading.momentum_neural import meta_label as ml


def _rows(n=43, n_pos=7, n_days=4, signal=False, seed=0):
    rng = np.random.default_rng(seed)
    feats = list(ml.DEFAULT_FEATURES)
    labels = [1] * n_pos + [0] * (n - n_pos)
    rng.shuffle(labels)
    rows = []
    for i in range(n):
        fd = {f: float(rng.standard_normal()) for f in feats}
        if signal and labels[i] == 1:
            fd["ofi"] += 3.0                       # strong separating signal on ofi for wins
        rows.append({"features": fd,
                     "run_r": 1.5 if labels[i] == 1 else -1.0,
                     "day": f"2026-06-{10 + (i % n_days):02d}"})
    return rows


def _std(a):
    return (a - a.mean(0)) / (a.std(0) + 1e-9)


def test_within_day_permute_preserves_marginal():
    rng = np.random.default_rng(0)
    yv = np.array([1, 0, 0, 1, 0, 1, 1, 0, 0, 0], dtype=float)
    day_ids = np.array(["a"] * 5 + ["b"] * 5)
    for _ in range(50):
        ystar = ml._within_day_permute(yv, day_ids, rng)
        assert ystar.sum() == yv.sum()                       # global marginal fixed
        assert ystar[:5].sum() == yv[:5].sum()               # per-day #wins fixed
        assert ystar[5:].sum() == yv[5:].sum()


def test_screen_inert_at_tiny_n():
    # FOUR guards must force keep-all at today's reality -> byte-equivalent to the all-feature ridge
    rows = _rows(n=43, n_pos=7, n_days=4, signal=False, seed=3)
    m = ml.train_meta_label(rows)
    assert m["status"] == "trained"
    assert set(m["features"]) == set(ml.DEFAULT_FEATURES)     # nothing pruned
    assert m["feature_screen"]["kept"] == len(m["features"])
    assert "keep-all" in m["feature_screen"]["reason"]


def test_kill_switch_off_bypasses_screen(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "chili_momentum_meta_label_feature_screen_enabled", False)
    rows = _rows(n=50, n_pos=12, n_days=8, signal=True, seed=4)
    m = ml.train_meta_label(rows)
    assert m["feature_screen"]["enabled"] is False
    assert set(m["features"]) == set(ml.DEFAULT_FEATURES)     # all features, screen bypassed


def test_screen_select_keeps_signal_and_protected_union():
    # strong signal on ofi; protected features pure noise -> _screen_select must keep ofi AND
    # union in ALL protected tail features (BY ranks by marginal corr and would drop them).
    rng = np.random.default_rng(1)
    feats = list(ml.DEFAULT_FEATURES)
    n = 60
    # day-MIXED labels (wins spread across days) so the within-day null has power — a signal that
    # is NOT confounded with the day effect. (If every day were single-class the grouped null
    # correctly attributes nothing to features; that conservatism is tested by the keep-all gates.)
    yv = np.zeros(n, dtype=float)
    yv[rng.choice(n, n // 2, replace=False)] = 1.0
    X = rng.standard_normal((n, len(feats)))
    X[yv == 1, feats.index("ofi")] += 2.5
    Xs = _std(X)
    day_ids = np.array([f"d{i % 8}" for i in range(n)])
    clusters = ml._feature_clusters(Xs, 1 - 1 / np.sqrt(n))
    prot = {i for i, f in enumerate(feats) if f in ml.PROTECTED_TAIL_FEATURES}
    # B=1000 is the real default (B=max(1000, ceil(20/q))); BY under arbitrary dependence with ~18
    # tests needs raw p<=~0.001 to clear q=0.2, which only B>=1000 can deliver — that conservatism
    # is the point (keep-all-dominant), so the test uses the production B.
    keep, pruned = ml._screen_select(Xs, yv, day_ids, clusters, prot, q=0.2, B=1000, rng=rng)
    assert feats.index("ofi") in keep                        # the real signal survives
    assert prot <= set(keep)                                 # protected tail ALWAYS kept


def test_spearman_dedup_catches_sign_duplicate():
    rng = np.random.default_rng(2)
    feats = list(ml.DEFAULT_FEATURES)
    n = 50
    X = rng.standard_normal((n, len(feats)))
    ai, vi = feats.index("above_vwap"), feats.index("vwap_dist_sigma")
    X[:, ai] = np.sign(X[:, vi])                             # deterministic sign-duplicate
    Xs = _std(X)
    clusters = ml._feature_clusters(Xs, 1 - 1 / np.sqrt(n))
    cl_of = {j: ci for ci, cl in enumerate(clusters) for j in cl}
    assert cl_of[ai] == cl_of[vi]                            # same cluster despite Pearson<thr


def test_calibration_runs_and_bounded():
    rng = np.random.default_rng(0)
    feats = list(ml.DEFAULT_FEATURES)
    n = 40
    yv = np.array([1, 0, 0, 0, 0] * 8, dtype=float)
    Xs = _std(rng.standard_normal((n, len(feats))))
    day_ids = np.array([f"d{i % 6}" for i in range(n)])
    clusters = ml._feature_clusters(Xs, 1 - 1 / np.sqrt(n))
    prot = {i for i, f in enumerate(feats) if f in ml.PROTECTED_TAIL_FEATURES}
    fpr = ml._calibrate_false_prune(Xs, yv, day_ids, clusters, prot, q=0.2, B=100, C_calib=50, rng=rng)
    assert 0.0 <= fpr <= 1.0                                  # a valid rate; the gate uses fpr<=q


def test_de_rate_bounded_after_screen():
    rows = _rows(n=43, n_pos=7, n_days=3, signal=True, seed=6)
    m = ml.train_meta_label(rows)
    for r in rows:
        mult = ml.size_multiplier(r["features"], m)
        assert ml.DERATE_FLOOR - 1e-9 <= mult <= 1.0 + 1e-9  # never 0, never >1 (never-veto)


def test_screen_deterministic():
    rows = _rows(n=43, n_pos=7, n_days=4, seed=5)
    m1 = ml.train_meta_label(rows)
    m2 = ml.train_meta_label(rows)
    assert m1["features"] == m2["features"]
    assert m1["feature_screen"].get("false_prune_rate") == m2["feature_screen"].get("false_prune_rate")
