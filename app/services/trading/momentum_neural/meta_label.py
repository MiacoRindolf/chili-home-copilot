"""Meta-labeling de-rate for the momentum lane (2026-06-23, adaptive rev).

A SECONDARY model that SIZES the primary momentum signal (de-rate the low-probability /
loser profile, NEVER veto or flip the side) — the canonical fix for a high-recall /
low-precision (~17%-win) primary on a small, GROWING, imbalanced sample (Lopez de Prado;
see reference_meta_labeling_discriminator).

ADAPTIVE, NOT GATED (operator 2026-06-23: "wala dapat magic number ... wag ipanatili yung
maling base natin ngayon kung meron na tayong kaalaman"): there is NO hard sample-size /
AUC / p-value go-gate. Instead the de-rate is ALWAYS-ON but EVIDENCE-SCALED — a continuous
`confidence` in [0,1] derived from the data itself (permutation-significance x effect-size)
shrinks the de-rate toward neutral when evidence is thin and lets it strengthen SMOOTHLY as
trades accumulate. Thin data => gentle de-rate (uses what we know), never a frozen baseline.
The ONLY non-derived constant is DERATE_FLOOR — the ONE documented irreducible base (operator
rule: "irreducible base = ONE documented setting") that keeps the de-rate from ever zeroing a
position, preserving the rare below-VWAP explosive winner (CRVO/CLWT).

Macro-regime conditioning + the exact probability->size map are refined per the adaptive deep
research (wf wivz5wy94). Pure/offline train + a tiny pure scorer used live; re-runnable as the
dataset grows (scripts/train_meta_label.py).
"""
from __future__ import annotations

import json
import math
from typing import Any

# Mechanism-backed features (OFI #1). EXCLUDED on purpose: rr (label-coupled artifact),
# minute_vol (lookahead), price (absolute), partial/ws_tick (path-specific), px_vs_session_vwap
# (proven non-separating). Macro features (vix_pct, spy_trend, ...) are appended by the trainer
# when present in the rows; partial-pooling lets them borrow strength from the global fit.
DEFAULT_FEATURES = [
    "ofi", "micro_edge_bps",
    "vol_ratio", "sustained_rvol",
    "front_side_score", "vwap_dist_sigma",
    "day_range_pos", "retrace_from_hod",
    "spread_bps", "atr_pct", "stop_pct_eff", "dollar_vol", "liq_mult",
    "above_vwap", "is_backside", "premarket",
]
MACRO_FEATURES = ["spy_trend", "iwm_trend", "mkt_vol", "bear_x_vol"]

# Follow-through label = the trade made at least its OWN risk back (run_r >= 1.0). 1R is the
# RISK UNIT (entry-stop distance) — the natural instrument-relative breakeven, NOT a tunable cap.
WIN_RUN_R = 1.0
# The ONE documented irreducible base: smallest size the de-rate may shrink to. NEVER zero ->
# never a veto (preserves the explosive tail). Everything else is adaptive. Overridable via
# chili_momentum_meta_label_min_size.
DERATE_FLOOR = 0.4
# Math-degeneracy guard only (a logistic needs >=2 classes with a few each to fit at all). NOT a
# confidence gate: below it the fit can't run, so confidence stays 0 (neutral) — never "wait for N".
_MIN_FITTABLE_PER_CLASS = 3
_PERM_ITERS = 1000


def _label(row: dict) -> int | None:
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
    if isinstance(row.get("features"), dict):
        return row["features"]
    ers = row.get("entry_regime_snapshot_json")
    if isinstance(ers, dict) and isinstance(ers.get("features"), dict):
        return ers["features"]
    return row


def train_meta_label(rows: list[dict], *, feature_list: list[str] | None = None) -> dict:
    """Train + self-calibrate the meta-label model. Returns a JSON-able dict with the fitted
    coefficients, scaling stats, metrics, and a continuous CONFIDENCE in [0,1] (NOT a go-gate).
    Never raises on thin/degenerate data — returns status + confidence=0 (neutral) instead."""
    base_feats = list(feature_list or DEFAULT_FEATURES)
    import numpy as np

    # include macro features only if present in the data (partial pooling via shared fit)
    sample_fd = _features_of(rows[0]) if rows else {}
    feats = base_feats + [m for m in MACRO_FEATURES if isinstance(sample_fd, dict) and m in sample_fd]

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
    neg = n - pos
    if pos < _MIN_FITTABLE_PER_CLASS or neg < _MIN_FITTABLE_PER_CLASS:
        return {"status": "unfittable", "confidence": 0.0, "n": n, "positives": pos, "features": feats}

    X = np.array([[float(v) if v is not None else np.nan for v in row] for row in X_rows], dtype=float)
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

    # C shrinks with sample size (more data -> looser prior -> larger effects). This is the
    # Bayesian/ridge shrinkage, derived from n (no hand-tuned constant): C = n / (n + #features).
    n_feat = Xs.shape[1]
    C = max(1e-3, n / (n + n_feat))
    clf = LogisticRegression(C=C, class_weight="balanced", max_iter=4000)

    uniq = sorted(set(groups))
    auc = None
    perm_p = 1.0
    if len(uniq) >= 2:
        n_splits = min(5, len(uniq))
        try:
            oof = cross_val_predict(clf, Xs, yv, cv=GroupKFold(n_splits=n_splits),
                                    groups=np.array(groups), method="predict_proba")[:, 1]
            auc = float(roc_auc_score(yv, oof))
            base = abs(auc - 0.5)
            hits = 0
            for i in range(_PERM_ITERS):
                a = roc_auc_score(np.roll(yv, i * 3 + 1), oof)
                if abs(a - 0.5) >= base:
                    hits += 1
            perm_p = (hits + 1) / (_PERM_ITERS + 1)
        except Exception:
            auc = None

    clf.fit(Xs, yv)
    # CONFIDENCE = statistical-significance (1 - perm_p) x effect-size (2*(AUC-0.5) clipped).
    # Both derived from the data; perm_p inherently accounts for sample size (thin -> not
    # significant -> confidence ~0 -> de-rate ~neutral). NO magic threshold.
    eff = 0.0 if auc is None else max(0.0, min(1.0, 2.0 * (auc - 0.5)))
    confidence = float(max(0.0, 1.0 - perm_p) * eff)
    return {
        "status": "trained", "confidence": confidence, "n": n, "positives": pos,
        "base_rate": float(yv.mean()), "heldout_auc": auc, "perm_p": perm_p,
        "n_day_groups": len(uniq), "features": feats, "C": C,
        "coef": [float(c) for c in clf.coef_[0]], "intercept": float(clf.intercept_[0]),
        "mean": [float(x) for x in mu], "std": [float(x) for x in sd],
        "median": [float(x) for x in med],
    }


def save_model(model: dict, path: str) -> None:
    with open(path, "w") as fh:
        json.dump(model, fh)


def load_training_rows(db, *, replay_path: str = "/app/data/_disc_dataset.json") -> list[dict]:
    """The labeled dataset = replay bootstrap (run_r) + LIVE outcomes (return_bps + entry
    features). Reused by the scheduled re-train and the CLI trainer."""
    import os

    rows: list[dict] = []
    if os.path.exists(replay_path):
        try:
            rows.extend(json.load(open(replay_path)))
        except Exception:
            pass
    try:
        from app.models.trading import MomentumAutomationOutcome as _MAO

        eq = ["robinhood_spot", "alpaca_spot", "robinhood_agentic_mcp"]
        q = db.query(_MAO).filter(_MAO.execution_family.in_(eq), _MAO.return_bps.isnot(None))
        for o in q.limit(20000):
            ers = o.entry_regime_snapshot_json
            if isinstance(ers, dict) and isinstance(ers.get("features"), dict) and ers["features"]:
                rows.append({
                    "return_bps": float(o.return_bps), "features": ers["features"],
                    "day": (str(o.terminal_at)[:10] if o.terminal_at else ""), "sym": o.symbol,
                })
    except Exception:
        pass
    return rows


def maybe_retrain_meta_label(db, *, model_path: str = "/app/data/_meta_label_model.json",
                             marker_path: str = "/app/data/_meta_label_last_train.json") -> dict:
    """DATA-DRIVEN re-train (operator: triggered when may sapat na BAGONG outcomes — NOT a fixed
    clock). Re-fits ONLY when the labeled dataset GREW since the last train; the logistic is <1s on
    a few-hundred rows so re-fitting on any growth is cheap. Saves the model the live sizing reads
    -> it AUTO-UPDATES. Best-effort (returns a status dict, never raises). This is the learning-
    cadence step that makes the de-rate self-improve as trades accumulate."""
    rows = load_training_rows(db)
    cur = len(rows)
    last = 0
    try:
        last = int(json.load(open(marker_path)).get("n", 0))
    except Exception:
        last = 0
    if cur <= last:
        return {"status": "skip_no_new_data", "n": cur, "last": last}
    model = train_meta_label(rows)
    save_model(model, model_path)
    try:
        with open(marker_path, "w") as fh:
            json.dump({"n": cur, "confidence": model.get("confidence"), "model_status": model.get("status")}, fh)
    except Exception:
        pass
    return {"status": "retrained", "n": cur, "grew_from": last,
            "confidence": model.get("confidence"), "model_status": model.get("status")}


def load_model(path: str) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def score_probability(features: dict, model: dict | None) -> float | None:
    if not model or model.get("status") != "trained":
        return None
    try:
        feats, coef = model["features"], model["coef"]
        mu, sd, med = model["mean"], model["std"], model["median"]
        z = float(model["intercept"])
        for i, k in enumerate(feats):
            v = features.get(k)
            if v is None:
                v = med[i]
            z += coef[i] * ((float(v) - mu[i]) / (sd[i] if sd[i] else 1.0))
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
    except Exception:
        return None


def size_multiplier(features: dict, model: dict | None, *, floor: float = DERATE_FLOOR) -> float:
    """Bounded, EVIDENCE-SCALED de-rate in [floor, 1.0]. De-rates ONLY the below-base-rate
    (loser) profile, PROPORTIONAL to how far below it sits, then SHRUNK toward neutral by the
    model's confidence (thin/unproven data -> ~neutral; proven -> full). Average-or-better -> 1.0.
    NEVER zero -> never a veto (the explosive tail is sized-down at worst, never killed).
    1.0 (no effect) when there is no trained model."""
    p = score_probability(features, model)
    if p is None:
        return 1.0
    base = float(model.get("base_rate") or 0.0)
    conf = float(model.get("confidence") or 0.0)
    if base <= 0 or p >= base or conf <= 0:
        return 1.0
    raw = max(0.0, min(1.0, p / base))                 # proportional de-rate (0=clear loser)
    eff = 1.0 - conf * (1.0 - raw)                      # shrink toward neutral by confidence
    return float(max(floor, min(1.0, eff)))
