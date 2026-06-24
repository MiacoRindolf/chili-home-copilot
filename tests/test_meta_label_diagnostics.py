"""Self-monitoring DIAGNOSTICS + research-agenda PROPOSER for the self-critic (2026-06-23, wf_a7af66e3).

Proves: Kish n_eff deflation, leakage univariate-AUC soft-flag, coef sign-stability tiny-n gating,
and the researcher phase is PROPOSE-only (never auto-launches deep-research — operator-gated spend).
"""
from __future__ import annotations

import numpy as np

from app.services.trading.momentum_neural import meta_label as ml


def _std(a):
    return (a - a.mean(0)) / (a.std(0) + 1e-9)


def test_n_eff_report_deflates_clustered_days():
    # the REPORTED effective sample = min(n_distinct_days, Kish) -> all-one-day collapses to ~1,
    # all-distinct-days == n. (Raw Kish with equal weights does not deflate; the min(n_days) does.)
    rng = np.random.default_rng(0)
    feats = list(ml.DEFAULT_FEATURES)
    n = 12
    yv = np.array([1, 0] * (n // 2), dtype=float)
    Xs = _std(rng.standard_normal((n, len(feats))))
    same = ml._compute_diagnostics(Xs, yv, ["d"] * n, feats)                 # 1 distinct day
    distinct = ml._compute_diagnostics(Xs, yv, [f"d{i}" for i in range(n)], feats)
    assert same["n_eff_report"] == 1.0                                       # min(1 day, kish) = 1
    assert distinct["n_eff_report"] == float(n)                              # min(n days, n) = n


def test_compute_diagnostics_flags_leakage():
    rng = np.random.default_rng(0)
    feats = list(ml.DEFAULT_FEATURES)
    n = 60
    yv = np.array([1, 0] * (n // 2), dtype=float)
    X = rng.standard_normal((n, len(feats)))
    X[:, feats.index("ofi")] = yv * 5 + rng.standard_normal(n) * 0.01    # near-perfect univariate leak
    d = ml._compute_diagnostics(_std(X), yv, [f"d{i % 8}" for i in range(n)], feats, seed=1)
    assert "ofi" in (d.get("suspected_leak_features") or [])
    assert d["n_eff_report"] <= n


def test_compute_diagnostics_sign_stability_gated_below_6_days():
    rng = np.random.default_rng(0)
    feats = list(ml.DEFAULT_FEATURES)
    n = 20
    yv = np.array([1, 0] * (n // 2), dtype=float)
    d = ml._compute_diagnostics(_std(rng.standard_normal((n, len(feats)))), yv,
                                [f"d{i % 3}" for i in range(n)], feats)   # 3 day-groups < 6
    assert "insufficient day-groups" in str(d.get("coef_stability", ""))


def test_propose_research_agenda_propose_only_and_dedup(tmp_path):
    bl = str(tmp_path / "_backlog.json")
    gaps = ["SUSPECTED LEAKAGE: ['ofi'] separate near-perfectly ALONE (univariate AUC > ceiling)",
            "coefficient SIGN-UNSTABLE across day-folds for ['x'] (median coef ρ=0.1)"]
    a = ml._propose_research_agenda(gaps, backlog_path=bl)
    assert a["top"] is not None and a["top"]["priority"] == 1          # leakage ranks priority-1
    assert "PROPOSE-only" in a["note"]
    assert a["n_new"] == 2
    a2 = ml._propose_research_agenda(gaps, backlog_path=bl)            # dedup: same gaps -> none new
    assert a2["n_new"] == 0


def test_researcher_never_autolaunches():
    # structural guarantee: the proposer cannot trigger a heavy run — it only formulates + writes a
    # backlog. No launch primitive may appear (the docstring may MENTION deep-research; it must never
    # INVOKE one).
    import inspect
    src = inspect.getsource(ml._propose_research_agenda)
    for forbidden in ("Workflow(", "Agent(", "Skill(", "subprocess", "WebSearch", "spawn_task"):
        assert forbidden not in src, forbidden


def test_analyze_surfaces_diagnostics_block(monkeypatch):
    # train on data with a leaked feature + >=6 day-groups -> model carries diagnostics -> the critic
    # surfaces the leakage gap and a propose-only research agenda.
    rng = np.random.default_rng(2)
    feats = list(ml.DEFAULT_FEATURES)
    n, nd = 72, 8
    yv = np.zeros(n, dtype=float)
    yv[rng.choice(n, n // 2, replace=False)] = 1.0
    rows = []
    for i in range(n):
        fd = {f: float(rng.standard_normal()) for f in feats}
        fd["ofi"] = float(yv[i] * 5 + rng.standard_normal() * 0.01)    # leak
        rows.append({"features": fd, "run_r": 1.5 if yv[i] else -1.0, "day": f"2026-06-{10 + (i % nd):02d}"})
    monkeypatch.setattr(ml, "load_training_rows", lambda db, **k: rows)
    monkeypatch.setattr(ml, "load_model", lambda p: ml.train_meta_label(rows))
    rep = ml.analyze_learning_gaps(db=None, report_path=str(__import__("tempfile").mktemp()))
    assert "diagnostics" in rep and "research_agenda" in rep
    assert any("LEAKAGE" in g for g in rep["gaps"])
    assert "PROPOSE-only" in rep["research_agenda"]["note"]


def _emit_rows(n_win, n_loss, p_win, p_loss):
    feats = list(ml.DEFAULT_FEATURES)
    rows = []
    for i in range(n_win + n_loss):
        win = i < n_win
        rows.append({"features": {f: 0.0 for f in feats}, "run_r": 1.5 if win else -1.0,
                     "day": f"2026-06-{10 + i % 5:02d}", "meta_p": p_win if win else p_loss,
                     "meta_de_rate": 1.0})
    return rows


def test_calibration_gap_fires_when_anticalibrated(monkeypatch):
    rows = _emit_rows(6, 6, p_win=0.1, p_loss=0.9)            # anti-calibrated -> Brier >> base
    monkeypatch.setattr(ml, "load_training_rows", lambda db, **k: rows)
    monkeypatch.setattr(ml, "load_model", lambda p: None)
    rep = ml.analyze_learning_gaps(db=None, report_path=str(__import__("tempfile").mktemp()))
    assert any("CALIBRATION decay" in g for g in rep["gaps"])


def test_calibration_silent_when_insufficient(monkeypatch):
    rows = _emit_rows(3, 3, 0.1, 0.9)                          # 6 < ceil(1/0.10)=10 -> insufficient
    monkeypatch.setattr(ml, "load_training_rows", lambda db, **k: rows)
    monkeypatch.setattr(ml, "load_model", lambda p: None)
    rep = ml.analyze_learning_gaps(db=None, report_path=str(__import__("tempfile").mktemp()))
    assert not any("CALIBRATION decay" in g for g in rep["gaps"])


def test_output_shift_gap_fires(monkeypatch):
    rows = _emit_rows(12, 12, 0.5, 0.5)                        # 24 >= 2*10
    for i, r in enumerate(rows):
        r["meta_de_rate"] = 1.0 if i < 12 else 0.5            # emitted de-rate drifts 1.0 -> 0.5
    monkeypatch.setattr(ml, "load_training_rows", lambda db, **k: rows)
    monkeypatch.setattr(ml, "load_model", lambda p: None)
    rep = ml.analyze_learning_gaps(db=None, report_path=str(__import__("tempfile").mktemp()))
    assert any("OUTPUT de-rate SHIFT" in g for g in rep["gaps"])


def test_emission_detectors_silent_without_emissions(monkeypatch):
    rows = _emit_rows(8, 8, 0.5, 0.5)
    for r in rows:
        r.pop("meta_p"); r.pop("meta_de_rate")               # no live emissions yet -> silent
    monkeypatch.setattr(ml, "load_training_rows", lambda db, **k: rows)
    monkeypatch.setattr(ml, "load_model", lambda p: None)
    rep = ml.analyze_learning_gaps(db=None, report_path=str(__import__("tempfile").mktemp()))
    assert not any("CALIBRATION" in g or "OUTPUT de-rate" in g for g in rep["gaps"])
