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
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Mechanism-backed features (OFI #1). EXCLUDED on purpose: rr (label-coupled artifact),
# minute_vol (lookahead), price (absolute), partial/ws_tick (path-specific), px_vs_session_vwap
# (proven non-separating). Macro features (vix_pct, spy_trend, ...) are appended by the trainer
# when present in the rows; partial-pooling lets them borrow strength from the global fit.
DEFAULT_FEATURES = [
    "ofi", "micro_edge_bps", "book_imbalance", "trade_flow",
    "vol_ratio", "sustained_rvol",
    "front_side_score", "vwap_dist_sigma",
    "day_range_pos", "retrace_from_hod", "range_contraction",
    "spread_bps", "atr_pct", "stop_pct_eff", "dollar_vol", "liq_mult",
    "above_vwap", "is_backside", "premarket",
]
MACRO_FEATURES = ["spy_trend", "iwm_trend", "mkt_vol", "bear_x_vol", "vix_slope", "fomc_even_week"]

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

# ===== DATA-SNOOPING-CORRECTED FEATURE SCREEN (CALIB-BY-DayRestrict, keep-all-dominant) =====
# Designed + adversarially hardened 2026-06-23 (workflow wf_faa694d3). The screen picks ONLY the
# logistic's column subset S; DERATE_FLOOR / the [floor,1] clip / confidence / side are FROZEN
# downstream, so NEVER-VETO holds by construction. The explosive below-VWAP winner is protected on
# TWO axes: (1) PROTECTED_TAIL_FEATURES are unioned into S unconditionally (BY-FDR ranks by MARGINAL
# correlation and is biased to drop these CONDITIONAL discriminators), (2) a tested tail-monotone
# revert. The ONE documented irreducible base is the FDR rate SCREEN_Q_BASE; every other threshold
# is derived from n / n_pos / the family size.
PROTECTED_TAIL_FEATURES = ("above_vwap", "vwap_dist_sigma", "is_backside", "retrace_from_hod")
SCREEN_Q_BASE = 0.20
SCREEN_NULL_DISCLAIMER = "within-day restricted marginal-preserving permutation (weak day-regime null)"
# The ONE documented self-critic FDR base (inherited from edge_evidence.apply_fdr_correction's q=0.10
# default — NOT a new scattered magic number). Used by the self-monitoring DIAGNOSTICS below.
SELFCRITIC_FDR_Q = 0.10


def _screen_enabled() -> bool:
    try:
        from ....config import settings
        return bool(settings.chili_momentum_meta_label_feature_screen_enabled)
    except Exception:
        return True


def _within_day_permute(yv, day_ids, rng):
    """Marginal-PRESERVING grouped null: shuffle labels WITHIN each day, holding every day's #wins
    EXACTLY fixed -> global positive count fixed -> null marginal == observed (the fix for the
    broadcast-null 0.22->0.54 marginal-inflation defect the adversary found). Preserves day-level
    regime structure (a 0-win day stays all-zero; a k-win day keeps k)."""
    import numpy as np

    out = np.asarray(yv, dtype=float).copy()
    day_ids = np.asarray(day_ids)
    for d in np.unique(day_ids):
        idx = np.where(day_ids == d)[0]
        if idx.size > 1:
            out[idx] = rng.permutation(out[idx])
    return out


def _feature_clusters(Xs, rho_thr):
    """y-INDEPENDENT Spearman-rank correlation clusters (rank corr catches sign/monotone duplicates
    like above_vwap=sign(vwap_dist_sigma) that Pearson misses). |rho|>=rho_thr -> same cluster."""
    import numpy as np
    from scipy.stats import rankdata

    p = Xs.shape[1]
    if p <= 1:
        return [[0]] if p == 1 else []
    R = np.column_stack([rankdata(Xs[:, j]) for j in range(p)])
    with np.errstate(all="ignore"):
        Cmat = np.nan_to_num(np.corrcoef(R, rowvar=False))
    assigned = [False] * p
    clusters = []
    for j in range(p):
        if assigned[j]:
            continue
        cl = [j]
        assigned[j] = True
        for k in range(j + 1, p):
            if not assigned[k] and abs(Cmat[j, k]) >= rho_thr:
                cl.append(k)
                assigned[k] = True
        clusters.append(cl)
    return clusters


def _screen_select(Xs, yv, day_ids, clusters, protected_idx, q, B, rng):
    """One screen pass -> (sorted keep-idx, pruned_bool). Vectorized |point-biserial| (= Pearson on
    standardized cols), within-day permutation null, Phipson-Smyth p, BY-FDR(q) under arbitrary
    dependence, protected-tail union. Pure column selection; protected-union guarantees it never
    empties and never prunes the tail discriminators."""
    import numpy as np
    from scipy.stats import false_discovery_control

    p = Xs.shape[1]
    yv = np.asarray(yv, dtype=float)
    n = len(yv)
    ybar = yv.mean()
    sd_y = yv.std()
    if sd_y <= 0 or p == 0:
        return list(range(p)), False
    # all-feature |point-biserial| in ONE matmul (Xs standardized -> denom = n*sd_y)
    pb = np.abs((yv - ybar) @ Xs) / (n * sd_y)
    # cluster representative: a protected member if any, else max |point-biserial|
    reps = []
    for cl in clusters:
        prot = [c for c in cl if c in protected_idx]
        reps.append(prot[0] if prot else int(max(cl, key=lambda c: pb[c])))
    reps = sorted(set(reps))
    T_obs = pb[reps]
    Xr = Xs[:, reps]
    # within-day permutation null, stacked over B then one matmul
    Yc = np.empty((n, B))
    for b in range(B):
        Yc[:, b] = _within_day_permute(yv, day_ids, rng) - ybar
    nulls = np.abs((Xr.T @ Yc) / (n * sd_y))          # (|reps| x B)
    cnt = (nulls >= (T_obs[:, None] - 1e-12)).sum(axis=1)
    pvals = (1.0 + cnt) / (B + 1.0)                    # Phipson-Smyth (edge_evidence idiom)
    try:
        adj = np.asarray(false_discovery_control(pvals, method="by"))  # BY: arbitrary dependence
    except Exception:
        adj = pvals
    survivors = {reps[i] for i in range(len(reps)) if adj[i] <= q}
    keep = survivors | set(protected_idx)
    if not keep:
        return list(range(p)), False
    return sorted(keep), (len(keep) < p)


def _calibrate_false_prune(Xs, yv, day_ids, clusters, protected_idx, q, B, C_calib, rng):
    """Empirical type-I control: run the SAME screen on within-day pure-null replays; the fraction
    that prune = false-prune-rate. If it exceeds q the screen is anti-conservative on THIS design
    (the adversary measured ~38% at n~43) -> the caller keeps ALL features."""
    pruned = 0
    for _ in range(C_calib):
        ynull = _within_day_permute(yv, day_ids, rng)
        _, pr = _screen_select(Xs, ynull, day_ids, clusters, protected_idx, q, B, rng)
        if pr:
            pruned += 1
    return pruned / max(1, C_calib)


def _feature_screen(Xs, yv, groups, feats, *, seed: int = 12345):
    """Orchestrate the keep-all-DOMINANT screen. Returns (keep_idx, telemetry). keep_idx == all
    features UNLESS every guard clears: the pre-test power gate AND empirical self-calibration<=q."""
    import math as _m

    import numpy as np

    p = Xs.shape[1]
    yv = np.asarray(yv, dtype=float)
    day_ids = np.asarray(groups)
    n = len(yv)
    n_pos = int(yv.sum())
    n_eff = len({day_ids[i] for i in range(n) if yv[i] == 1})     # distinct WIN-days (grouped n)
    n_days = len(set(day_ids.tolist()))
    q = min(SCREEN_Q_BASE, 1.0 / _m.sqrt(max(1, n_pos)))          # self-tightens when wins scarce
    rho_thr = 1.0 - 1.0 / _m.sqrt(max(2, n))                      # data-adaptive de-dup cutoff
    clusters = _feature_clusters(Xs, rho_thr)
    p_fam = len(clusters)
    protected_idx = {i for i, f in enumerate(feats) if f in PROTECTED_TAIL_FEATURES}
    tel = {"enabled": True, "n": n, "n_pos": n_pos, "n_eff": n_eff, "p_fam": p_fam,
           "q": round(q, 4), "kept": p, "calibration_passed": False, "false_prune_rate": None,
           "null": SCREEN_NULL_DISCLAIMER}
    n_pos_min = _m.ceil(1.0 / q)
    n_eff_min = _m.ceil(_m.sqrt(max(1, p_fam)))
    if n_pos < n_pos_min or n_eff < n_eff_min or n_pos in (0, n) or n_days < 2:
        tel["reason"] = f"pre-test keep-all (n_pos={n_pos}<{n_pos_min} or n_eff={n_eff}<{n_eff_min})"
        return list(range(p)), tel
    B = int(max(_PERM_ITERS, _m.ceil(20.0 / q)))
    C_calib = int(max(200, _m.ceil(1.0 / (q * q))))
    rng = np.random.default_rng(seed)
    fpr = _calibrate_false_prune(Xs, yv, day_ids, clusters, protected_idx, q, B, C_calib, rng)
    tel["false_prune_rate"] = round(fpr, 4)
    if fpr > q:
        tel["reason"] = f"self-calibration keep-all (false_prune_rate={fpr:.3f}>q={q:.3f})"
        return list(range(p)), tel
    keep_idx, pruned = _screen_select(Xs, yv, day_ids, clusters, protected_idx, q, B, rng)
    tel["calibration_passed"] = True
    tel["kept"] = len(keep_idx)
    tel["reason"] = "pruned" if pruned else "screen kept all"
    return keep_idx, tel


def _tail_monotone_ok(rows, y, model_pruned, model_keepall, *, frac: float = 0.10) -> bool:
    """The explosive-tail-survives invariant, MEASURED: the pruned model must not assign any realized
    top-decile-R WIN a smaller de-rate than keep-all would. True if too few wins to assess (the
    protected-union already structurally guards the tail discriminators)."""
    wins = []
    for i, lab in enumerate(y):
        if lab != 1:
            continue
        rr = rows[i].get("run_r")
        if rr is None:
            continue
        try:
            wins.append((i, float(rr)))
        except Exception:
            continue
    if len(wins) < 3:
        return True
    wins.sort(key=lambda t: t[1], reverse=True)
    k = max(1, int(len(wins) * frac))
    top = [i for i, _ in wins[:k]]
    mp = min(size_multiplier(_features_of(rows[i]), model_pruned) for i in top)
    mk = min(size_multiplier(_features_of(rows[i]), model_keepall) for i in top)
    return mp >= mk - 1e-6


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

    X_rows, y, groups, kept_rows = [], [], [], []
    for r in rows:
        lab = _label(r)
        if lab is None:
            continue
        fd = _features_of(r)
        X_rows.append([fd.get(k) for k in feats])
        y.append(lab)
        groups.append(str(r.get("day") or r.get("terminal_at") or ""))
        kept_rows.append(r)
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

    def _fit(idx):
        """Fit + score a model dict on the column subset ``idx`` (feature indices). C = n/(n+|idx|)
        is the ridge shrinkage from sample size + survivor count (no hand-tuned constant); the
        confidence/floor/clip are computed identically regardless of |idx| -> never-veto preserved."""
        Xsub = Xs[:, idx]
        nf = Xsub.shape[1]
        C = max(1e-3, n / (n + nf))
        clf = LogisticRegression(C=C, class_weight="balanced", max_iter=4000)
        uniq = sorted(set(groups))
        auc = None
        perm_p = 1.0
        if len(uniq) >= 2:
            n_splits = min(5, len(uniq))
            try:
                oof = cross_val_predict(clf, Xsub, yv, cv=GroupKFold(n_splits=n_splits),
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
        clf.fit(Xsub, yv)
        # CONFIDENCE = significance (1 - perm_p) x effect-size (2*(AUC-0.5) clipped); both from the
        # data, perm_p inherently accounts for sample size. NO magic threshold. FROZEN downstream.
        eff = 0.0 if auc is None else max(0.0, min(1.0, 2.0 * (auc - 0.5)))
        confidence = float(max(0.0, 1.0 - perm_p) * eff)
        return {
            "status": "trained", "confidence": confidence, "n": n, "positives": pos,
            "base_rate": float(yv.mean()), "heldout_auc": auc, "perm_p": perm_p,
            "n_day_groups": len(uniq), "features": [feats[i] for i in idx], "C": C,
            "coef": [float(c) for c in clf.coef_[0]], "intercept": float(clf.intercept_[0]),
            "mean": [float(mu[i]) for i in idx], "std": [float(sd[i]) for i in idx],
            "median": [float(med[i]) for i in idx],
        }

    all_idx = list(range(Xs.shape[1]))
    # ---- DATA-SNOOPING-CORRECTED FEATURE SCREEN (kill-switchable, keep-all-dominant) ----
    keep_idx = all_idx
    screen_tel = {"enabled": False, "reason": "off", "kept": len(all_idx)}
    if _screen_enabled():
        try:
            keep_idx, screen_tel = _feature_screen(Xs, yv, groups, feats)
        except Exception:
            keep_idx = all_idx
            screen_tel = {"enabled": True, "reason": "screen error -> keep all", "kept": len(all_idx)}

    model = _fit(keep_idx)
    tail_revert = False
    if len(keep_idx) < len(all_idx):
        # TAIL-MONOTONE invariant: pruning must NOT lower the de-rate of any realized top-decile-R
        # winner vs keep-all; else revert (explosive-tail-survives, MEASURED not assumed).
        try:
            model_all = _fit(all_idx)
            if not _tail_monotone_ok(kept_rows, y, model, model_all):
                model, keep_idx, tail_revert = model_all, all_idx, True
                screen_tel["reason"] = "tail-monotone revert -> keep all"
                screen_tel["kept"] = len(all_idx)
        except Exception:
            pass
    screen_tel["tail_revert"] = tail_revert
    model["feature_screen"] = screen_tel
    # SELF-MONITORING DIAGNOSTICS (analysis-only; the self-critic reads model["diagnostics"] — NEVER
    # feeds sizing). Computed on the FINAL fitted columns. Best-effort.
    try:
        model["diagnostics"] = _compute_diagnostics(Xs[:, keep_idx], yv, groups, [feats[i] for i in keep_idx])
    except Exception:
        model["diagnostics"] = {}
    try:
        logger.info("[meta_label_screen] %s", {k: screen_tel.get(k) for k in (
            "enabled", "reason", "kept", "n_pos", "n_eff", "p_fam", "q",
            "false_prune_rate", "calibration_passed", "tail_revert") if k in screen_tel})
    except Exception:
        pass
    return model


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
                row = {
                    "return_bps": float(o.return_bps), "features": ers["features"],
                    "day": (str(o.terminal_at)[:10] if o.terminal_at else ""), "sym": o.symbol,
                }
                _emit = ers.get("meta_label_emit")
                if isinstance(_emit, dict):
                    if _emit.get("p") is not None:
                        row["meta_p"] = float(_emit["p"])
                    if _emit.get("de_rate") is not None:
                        row["meta_de_rate"] = float(_emit["de_rate"])
                rows.append(row)
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


def _kish_n_eff(groups) -> float:
    """Kish EFFECTIVE sample size over day clusters: down-weights same-day (correlated) rows so the
    self-critic's CIs are honest. n_eff = (Σw)^2/Σw^2, w_i = 1/size(day of row i). Conservative,
    n_eff<=n; cannot false-alarm (it only inflates other bands)."""
    import numpy as np

    g = np.asarray(groups)
    sizes = {d: int((g == d).sum()) for d in set(g.tolist())}
    w = np.array([1.0 / max(1, sizes[d]) for d in g], dtype=float)
    ss = float((w * w).sum())
    return float((w.sum() ** 2) / ss) if ss > 0 else float(len(g))


def _compute_diagnostics(Xs, yv, groups, feats, *, seed: int = 12345) -> dict:
    """Self-monitoring DIAGNOSTICS for the self-critic (ANALYSIS-ONLY; NEVER feeds sizing). Designed
    from the self-monitoring research (wf_a7af66e3); each detector is cheap + tiny-n-safe (DEGRADES
    to 'insufficient' rather than false-alarming):
      - n_eff (Kish, day-clustered) + deflation defl=n/n_eff (re-expresses every CI honestly),
      - target-LEAKAGE univariate-AUC soft-flag (a feature that separates near-perfectly ALONE,
        above the permutation-null max-AUC ceiling — the ASYMMETRIC catastrophe for a never-veto
        sizer, which would over-size on a phantom edge),
      - coefficient SIGN/RANK STABILITY across the per-day GroupKFold (the tiny-n-VALID overfit
        detector — uses sign/rank not magnitude, rides the day-grouping; gated OFF when day-groups<6).
    """
    import numpy as np

    n = len(yv)
    n_days = len(set(np.asarray(groups).tolist()))
    n_eff = _kish_n_eff(groups)
    n_eff_report = min(float(n_days), n_eff)
    diag = {"n_eff": round(n_eff, 2), "n_eff_report": round(n_eff_report, 2),
            "deflation": round(n / max(n_eff_report, 1.0), 3), "n_day_groups": n_days}

    # LEAKAGE univariate-AUC soft-flag vs a permutation-null max-AUC ceiling
    try:
        from sklearn.metrics import roc_auc_score

        rng = np.random.default_rng(seed)
        aucs = {}
        for j, f in enumerate(feats):
            try:
                a = float(roc_auc_score(yv, Xs[:, j]))
                aucs[f] = max(a, 1.0 - a)
            except Exception:
                continue
        ceil_draws = []
        for _ in range(200):
            yp = rng.permutation(yv)
            best = 0.5
            for j in range(Xs.shape[1]):
                try:
                    a = float(roc_auc_score(yp, Xs[:, j]))
                    best = max(best, a, 1.0 - a)
                except Exception:
                    continue
            ceil_draws.append(best)
        ceiling = float(np.quantile(ceil_draws, 1.0 - SELFCRITIC_FDR_Q)) if ceil_draws else 1.0
        diag["leakage_ceiling"] = round(ceiling, 3)
        diag["suspected_leak_features"] = [f for f, a in aucs.items() if a > ceiling]
    except Exception:
        diag["suspected_leak_features"] = []

    # coefficient SIGN/RANK STABILITY across the per-day folds (gate OFF below 6 day-groups)
    if n_days >= 6:
        try:
            from scipy.stats import binom, spearmanr
            from sklearn.linear_model import LogisticRegression
            from sklearn.model_selection import GroupKFold

            ga = np.asarray(groups)
            K = min(5, n_days)
            coefs = []
            for tr, _te in GroupKFold(n_splits=K).split(Xs, yv, groups=ga):
                if len(set(yv[tr].tolist())) < 2:
                    continue
                c = LogisticRegression(C=max(1e-3, n / (n + Xs.shape[1])),
                                       class_weight="balanced", max_iter=4000)
                c.fit(Xs[tr], yv[tr])
                coefs.append(c.coef_[0])
            if len(coefs) >= 3:
                Cf = np.array(coefs)
                Kf = len(coefs)
                lo = binom.ppf(SELFCRITIC_FDR_Q, Kf, 0.5)        # sign-agreement lower tail
                unstable = []
                for j, f in enumerate(feats):
                    pos = int((Cf[:, j] > 0).sum())
                    if max(pos, Kf - pos) <= lo:
                        unstable.append(f)
                rhos = []
                for a in range(Kf):
                    for b in range(a + 1, Kf):
                        try:
                            r, _ = spearmanr(Cf[a], Cf[b])
                            if np.isfinite(r):
                                rhos.append(float(r))
                        except Exception:
                            continue
                diag["coef_folds"] = Kf
                diag["coef_sign_unstable"] = unstable
                diag["coef_median_rho"] = round(float(np.median(rhos)), 3) if rhos else None
        except Exception:
            pass
    else:
        diag["coef_stability"] = f"insufficient day-groups ({n_days}<6)"
    return diag


def _propose_research_agenda(gaps: list, *, backlog_path: str = "/app/data/_research_backlog.json") -> dict:
    """The RESEARCHER phase — PROPOSE-only, NEVER auto-launch (decision from wf_a7af66e3: a deep-
    research run is ~5M tokens, and a single false-positive gap would burn it on noise, so the heavy
    launch stays OPERATOR-GATED). The self-critic turns its top ACTIONABLE gap into a bounded,
    concrete research question + enhancement, ranks them, and dedup-appends to an operator-reviewed
    backlog. The operator launches the deep-research; the critic only makes the budget high-yield."""
    import json as _json
    import os as _os

    proposals = []
    for g in gaps:
        gl = g.lower()
        if "leakage" in gl:
            proposals.append({"gap": g, "priority": 1,
                "research_question": "audit + point-in-time lookahead-test the suspected-leakage features for the momentum meta-label: confirm each feature's compute-window-end <= the decision timestamp; quantify the univariate edge that VANISHES under a strict point-in-time recompute",
                "enhancement": "instrument per-feature lineage timestamps; drop/repair any feature that fails the point-in-time assertion"})
        elif "zero coverage" in gl:
            proposals.append({"gap": g, "priority": 1, "research_question": None,
                "enhancement": "fix the live capture path for the zero-coverage feature (a capture bug, not a research question)"})
        elif "sign-unstable" in gl or "sign unstable" in gl or "not improving" in gl or "may not separate" in gl:
            proposals.append({"gap": g, "priority": 2,
                "research_question": "what lookahead-free entry-moment signals separate intraday small-cap momentum WINNERS from LOSERS that CHILI's current meta-label feature set is MISSING (the current features do not separate / are sign-unstable across days)?",
                "enhancement": "research + engineer a new lookahead-free signal category as a capture feature; the meta-label learns its weight"})
    proposals.sort(key=lambda x: x["priority"])

    backlog = []
    if _os.path.exists(backlog_path):
        try:
            backlog = _json.load(open(backlog_path))
        except Exception:
            backlog = []
    seen = {(b.get("gap") or "")[:80] for b in backlog if isinstance(b, dict)}
    n_new = 0
    for p in proposals:
        key = (p["gap"] or "")[:80]
        if key not in seen:
            backlog.append(p)
            seen.add(key)
            n_new += 1
    try:
        with open(backlog_path, "w") as fh:
            _json.dump(backlog[-50:], fh)
    except Exception:
        pass
    return {"top": proposals[0] if proposals else None, "n_open": len(backlog), "n_new": n_new,
            "note": "PROPOSE-only — the operator launches the deep-research (heavy spend is operator-gated)"}


def analyze_learning_gaps(db, *, model_path: str = "/app/data/_meta_label_model.json",
                          report_path: str = "/app/data/_learning_self_report.json") -> dict:
    """SELF-CRITIC / gap-analyst (operator's "open-minded critical thinker"): a data-driven
    critical review of the lane's LEARNING health. It does NOT just compute scores — it FINDS
    the system's GAPS and PROPOSES enhancement steps:
      - dataset thinness / class imbalance (can the model even fit?),
      - feature COVERAGE holes (a 0-coverage feature => a capture bug or dead data source;
        a feature << the best-covered => a sparse/unreliable signal),
      - statistical significance per the DATA-SNOOPING discipline (a not-yet-significant model
        means its weights are spurious-shrunk to ~neutral — correct, but don't trust them yet),
      - confidence TREND vs the last report (is the learning actually improving?),
      - known MISSING signal categories (the deferred engineerable features).
    Deterministic + offline + best-effort. Emits a structured report (logged + saved) so the
    operator (and the watch) can see the self-critique. v1 is rule-based; a later 'researcher'
    phase can auto-launch deep-research for the top gap. Thresholds are RELATIVE/natural
    (0-coverage, <0.5x-best-coverage, perm_p>0.5=worse-than-coin-flip), not magic caps."""
    import json as _json
    import os as _os

    rows = load_training_rows(db)
    n = len(rows)
    pos = sum(1 for r in rows if _label(r) == 1)
    model = load_model(model_path)
    feats = (model or {}).get("features") or (DEFAULT_FEATURES + MACRO_FEATURES)
    cov: dict = {}
    for ft in feats:
        present = sum(1 for r in rows if isinstance(_features_of(r).get(ft), (int, float)))
        cov[ft] = round(present / n, 3) if n else 0.0
    maxcov = max(cov.values()) if cov else 0.0

    gaps: list = []
    proposals: list = []
    if pos < _MIN_FITTABLE_PER_CLASS or (n - pos) < _MIN_FITTABLE_PER_CLASS:
        gaps.append(f"thin dataset: n={n}, positives={pos} — cannot fit a stable model")
        proposals.append("the lane must TRADE to grow the labeled set; the de-rate stays neutral until then")
    for ft, c in cov.items():
        if maxcov > 0 and c == 0.0:
            gaps.append(f"feature '{ft}' has ZERO coverage — likely a capture bug or dead data source")
            proposals.append(f"investigate the live capture path for '{ft}' (is the signal computed at the fill?)")
        elif maxcov > 0 and 0.0 < c < 0.5 * maxcov:
            gaps.append(f"feature '{ft}' coverage {c} << best {maxcov} — sparse/unreliable signal")

    conf = float((model or {}).get("confidence") or 0.0)
    perm_p = (model or {}).get("perm_p")
    auc = (model or {}).get("heldout_auc")
    status = (model or {}).get("status", "none")
    if status == "trained":
        if perm_p is not None and perm_p > 0.5:
            gaps.append(f"model NOT statistically significant (perm_p={perm_p}, AUC={auc}) — "
                        "weights are data-snooping-shrunk to ~neutral (correct; de-rate inert)")
            proposals.append("accumulate more data; do NOT trust individual feature weights yet (spurious-fit risk)")
        elif conf < 0.3:
            proposals.append(f"model emerging (confidence={conf}); keep accumulating to firm up the weights")
        else:
            proposals.append(f"model has real signal (confidence={conf}); monitor the de-rate's live A/B effect")

    prev = None
    if _os.path.exists(report_path):
        try:
            prev = _json.load(open(report_path))
        except Exception:
            prev = None
    conf_trend = None
    if prev and isinstance(prev.get("confidence"), (int, float)):
        conf_trend = round(conf - float(prev["confidence"]), 4)
        if n > int(prev.get("n_samples") or 0) and conf_trend is not None and conf_trend <= 0:
            gaps.append(f"confidence NOT improving despite more data (Δconf={conf_trend}) — current features "
                        "may not separate; consider a new signal category")

    # ---- SELF-MONITORING DIAGNOSTICS (analysis-only; from model["diagnostics"], computed at train) ----
    diag = (model or {}).get("diagnostics") or {}
    n_eff_r = diag.get("n_eff_report")
    if n_eff_r is not None and n_eff_r < math.ceil(1.0 / SELFCRITIC_FDR_Q):
        # estimability honesty re-expressed in EFFECTIVE (day-clustered) sample size — so the operator
        # knows the inert de-rate is CORRECT, not broken. Floor derived from the ONE FDR base.
        gaps.append(f"the sizer is NOT yet statistically estimable: n_eff≈{n_eff_r} (Kish day-clustered) "
                    f"over {diag.get('n_day_groups')} day-groups < {math.ceil(1.0 / SELFCRITIC_FDR_Q)} — the "
                    "INERT de-rate is CORRECT not a bug; same-day trades are correlated")
    leak = diag.get("suspected_leak_features") or []
    if leak:
        gaps.append(f"SUSPECTED LEAKAGE: {leak} separate near-perfectly ALONE (univariate AUC > perm-null "
                    f"ceiling {diag.get('leakage_ceiling')}) — a never-veto sizer OVER-sizes on a phantom "
                    "edge; verify each feature's compute-window-end <= the decision timestamp")
        proposals.append(f"audit {leak} for lookahead/leakage (lineage assertion) BEFORE trusting their weight")
    unstable = diag.get("coef_sign_unstable") or []
    if unstable:
        gaps.append(f"coefficient SIGN-UNSTABLE across day-folds for {unstable} (median coef ρ="
                    f"{diag.get('coef_median_rho')}) — they don't replicate across days; the de-rate is "
                    "injecting SIZE NOISE from them (the tiny-n-valid overfit signal)")
        proposals.append(f"down-weight/deprioritize {unstable}; they do not generalize across trading days")

    # ---- EMISSION-BASED detectors (priority-1 sizer self-monitoring; need the live-logged emitted
    #      (p, de_rate); DEGRADE silently to 'insufficient' until live trades populate them) ----
    _need = math.ceil(1.0 / SELFCRITIC_FDR_Q)
    emit = [(float(r["meta_p"]), 1 if _label(r) == 1 else 0)
            for r in rows if r.get("meta_p") is not None and _label(r) is not None]
    if len(emit) >= _need:
        import numpy as _np

        ps = _np.array([e[0] for e in emit], dtype=float)
        ys = _np.array([e[1] for e in emit], dtype=float)
        brier = float(_np.mean((ps - ys) ** 2))
        base_brier = float(ys.mean() * (1.0 - ys.mean()))          # no-skill base-rate Brier (data-derived)
        if brier >= base_brier:
            gaps.append(f"CALIBRATION decay: live Brier {brier:.3f} >= no-skill base-rate Brier "
                        f"{base_brier:.3f} on {len(emit)} matched (p,y) — the de-rate is mis-SIZING "
                        "capital (it is a monotone fn of p), even if ranking/AUC is intact")
            proposals.append("recalibrate the meta-label probabilities (Platt/temperature) — the SIZE "
                             "map is the single most consequential decay mode for a sizing head")
    de_rates = [float(r["meta_de_rate"]) for r in rows if r.get("meta_de_rate") is not None]
    if len(de_rates) >= 2 * _need:
        import numpy as _np
        from scipy.stats import norm as _norm

        half = len(de_rates) // 2
        ref = _np.array(de_rates[:half], dtype=float)
        rec = _np.array(de_rates[half:], dtype=float)
        band = float(_norm.ppf(1.0 - SELFCRITIC_FDR_Q)) * float((ref.var() / len(ref) + rec.var() / len(rec)) ** 0.5)
        if abs(float(rec.mean()) - float(ref.mean())) > band:
            gaps.append(f"OUTPUT de-rate SHIFT: emitted de-rate mean moved {ref.mean():.3f}->{rec.mean():.3f} "
                        f"(> SE band {band:.3f}) — the sizer's emitted behavior DRIFTED; check regime/feature drift")

    proposals.append("deferred engineerable features (prioritize when ready): tick-level trade-flow "
                     "(Massive WS tape), multi-level OFI FLOW (needs iqfeed raw-ladder infra), opening-range-RVOL rank")

    # ---- RESEARCHER phase: PROPOSE (NEVER auto-launch) a ranked research agenda from the gaps ----
    agenda = _propose_research_agenda(gaps)

    report = {"n_samples": n, "positives": pos, "model_status": status,
              "confidence": conf, "confidence_trend": conf_trend, "heldout_auc": auc, "perm_p": perm_p,
              "feature_coverage": cov, "diagnostics": diag, "research_agenda": agenda,
              "n_gaps": len(gaps), "gaps": gaps, "proposals": proposals}
    try:
        with open(report_path, "w") as fh:
            _json.dump(report, fh)
    except Exception:
        pass
    return report


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
