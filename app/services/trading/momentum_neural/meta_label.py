"""Meta-labeling de-rate for the momentum lane (2026-06-23).

A SECONDARY model that SIZES the primary momentum signal (de-rate the low-probability /
loser profile, NEVER veto or flip the side) — the canonical fix for a high-recall /
low-precision (~17%-win) primary on a small, imbalanced sample (Lopez de Prado; see
reference_meta_labeling_discriminator). Trains a calibrated logistic on the captured
(entry-features, outcome) dataset behind a MIN-SAMPLE GATE + per-day-grouped CV +
permutation-p, so it stays NEUTRAL (multiplier 1.0, zero effect) until the data AND
validation actually support a de-rate. The apply is a BOUNDED size multiplier in
[floor, 1.0]: a below-VWAP explosive winner is sized-down at worst, never killed.

Pure/offline training (no live deps) + a tiny pure scorer used live. Re-runnable as the
dataset grows: scripts/train_meta_label.py -> writes the model JSON; the live/replay
sizing reads it and applies size_multiplier() only when the flag is on AND the model
PASSED its gate.
"""
from __future__ import annotations

import json
import math
from typing import Any

# Features the model may use. EXCLUDED on purpose: rr (label-coupled artifact, bootstrap
# 2026-06-23), minute_vol (lookahead), price (absolute), partial/ws_tick (path-specific),
# px_vs_session_vwap (proven non-separating AUC~0.51). Mechanism-backed first (OFI #1).
DEFAULT_FEATURES = [
    "ofi", "micro_edge_bps",                 # order-flow (research #1)
    "vol_ratio", "sustained_rvol",           # volume thrust
    "front_side_score", "vwap_dist_sigma",   # session structure (weigh, never veto)
    "day_range_pos", "retrace_from_hod",
    "spread_bps", "atr_pct", "stop_pct_eff", "dollar_vol", "liq_mult",
    "above_vwap", "is_backside", "premarket",
]

# Gate: do NOT ship a de-rate until the data + validation support it (anti-overfit / L3).
MIN_SAMPLES = 120
MIN_POSITIVES = 20
MIN_HELDOUT_AUC = 0.60
MAX_PERM_P = 0.05
WIN_RUN_R = 1.0          # follow-through label threshold on run_r
DERATE_FLOOR = 0.5       # smallest size multiplier (never zero -> never a veto)


def _label(row: dict) -> int | None:
    """Binary follow-through label. Prefer run_r (replay); fall back to return_bps (live)."""
    rr = row.get("run_r")
    if rr is not None:
        try:
            return 1 if float(rr) >= WIN_RUN_R else 0
        except Exception:
            return None
    rb = row.get("return_bps")
    if rb is not None:
        try:
            return 1 if float(rb) > 0 else 0
        except Exception:
            return None
    return None


def _features_of(row: dict) -> dict:
    """The feature sub-dict — replay rows carry features flat; live rows nest them under
    entry_regime_snapshot_json['features'] (or pass the features dict directly)."""
    if isinstance(row.get("features"), dict):
        return row["features"]
    ers = row.get("entry_regime_snapshot_json")
    if isinstance(ers, dict) and isinstance(ers.get("features"), dict):
        return ers["features"]
    return row


def train_meta_label(rows: list[dict], *, feature_list: list[str] | None = None) -> dict:
    """Train + validate the meta-label model. Returns a JSON-able dict with status,
    coefficients (standardized-space), feature means/stds for live scoring, metrics, and
    a GO/NO-GO verdict. NEVER raises on thin/degenerate data — returns status instead."""
    feats = list(feature_list or DEFAULT_FEATURES)
    import numpy as np

    X_rows, y, groups = [], [], []
    for r in rows:
        lab = _label(r)
        if lab is None:
            continue
        fd = _features_of(r)
        X_rows.append([fd.get(k) for k in feats])
        y.append(lab)
        groups.append(str(r.get("day") or r.get("terminal_at") or ""))
    n = len(y)
    pos = int(sum(y))
    if n < MIN_SAMPLES or pos < MIN_POSITIVES or (n - pos) < MIN_POSITIVES:
        return {"status": "insufficient_data", "go": False, "n": n, "positives": pos,
                "need": {"samples": MIN_SAMPLES, "per_class": MIN_POSITIVES}, "features": feats}

    X = np.array([[float(v) if v is not None else np.nan for v in row] for row in X_rows], dtype=float)
    # median-impute missing (OFI absent on some rows) using TRAIN medians
    med = np.nanmedian(X, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    inds = np.where(np.isnan(X))
    X[inds] = np.take(med, inds[1])
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd = np.where(sd <= 0, 1.0, sd)
    Xs = (X - mu) / sd
    yv = np.array(y)

    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupKFold, cross_val_predict
    from sklearn.metrics import roc_auc_score

    clf = LogisticRegression(C=0.5, class_weight="balanced", max_iter=2000)
    # per-day-grouped out-of-sample probabilities (purged-ish: a day is never in its own train)
    uniq_groups = sorted(set(groups))
    n_splits = min(5, len(uniq_groups)) if len(uniq_groups) > 1 else 0
    heldout_auc = None
    perm_p = None
    if n_splits >= 2:
        gkf = GroupKFold(n_splits=n_splits)
        try:
            oof = cross_val_predict(clf, Xs, yv, cv=gkf, groups=np.array(groups),
                                    method="predict_proba")[:, 1]
            heldout_auc = float(roc_auc_score(yv, oof))
            # permutation null on the held-out AUC
            rngless = np.argsort(np.sin(np.arange(n)))  # deterministic shuffle seed-free
            hits = 0
            base = abs(heldout_auc - 0.5)
            for i in range(400):
                perm = np.roll(yv, i * 7 + 1)
                try:
                    a = roc_auc_score(perm, oof)
                    if abs(a - 0.5) >= base:
                        hits += 1
                except Exception:
                    pass
            perm_p = (hits + 1) / (400 + 1)
        except Exception:
            heldout_auc = None

    clf.fit(Xs, yv)
    base_rate = float(yv.mean())
    go = bool(heldout_auc is not None and heldout_auc >= MIN_HELDOUT_AUC
              and perm_p is not None and perm_p <= MAX_PERM_P)
    return {
        "status": "trained", "go": go, "n": n, "positives": pos, "base_rate": base_rate,
        "heldout_auc": heldout_auc, "perm_p": perm_p, "n_day_groups": len(uniq_groups),
        "features": feats, "coef": [float(c) for c in clf.coef_[0]],
        "intercept": float(clf.intercept_[0]),
        "mean": [float(x) for x in mu], "std": [float(x) for x in sd],
        "median": [float(x) for x in med],
        "gate": {"min_auc": MIN_HELDOUT_AUC, "max_perm_p": MAX_PERM_P},
    }


def save_model(model: dict, path: str) -> None:
    with open(path, "w") as fh:
        json.dump(model, fh)


def load_model(path: str) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def score_probability(features: dict, model: dict | None) -> float | None:
    """Calibrated P(follow-through) for a feature vector. None if no usable model."""
    if not model or not model.get("go") or model.get("status") != "trained":
        return None
    try:
        feats = model["features"]
        coef = model["coef"]
        mu = model["mean"]
        sd = model["std"]
        med = model["median"]
        z = float(model["intercept"])
        for i, k in enumerate(feats):
            v = features.get(k)
            if v is None:
                v = med[i]
            xs = (float(v) - mu[i]) / (sd[i] if sd[i] else 1.0)
            z += coef[i] * xs
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
    except Exception:
        return None


def size_multiplier(features: dict, model: dict | None, *, floor: float = DERATE_FLOOR) -> float:
    """Bounded de-rate in [floor, 1.0]. ONLY shrinks trades the model rates BELOW the base
    rate (the loser profile); average-or-better -> 1.0. Never zeroes -> never a veto, so a
    below-VWAP explosive winner is sized-down at worst, preserving the tail. 1.0 (no effect)
    when there is no gated model or P is unavailable."""
    p = score_probability(features, model)
    if p is None:
        return 1.0
    base = float(model.get("base_rate") or 0.0)
    if base <= 0 or p >= base:
        return 1.0
    frac = max(0.0, min(1.0, p / base))
    return float(floor + (1.0 - floor) * frac)
