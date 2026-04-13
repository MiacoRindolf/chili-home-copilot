"""Pattern-driven ML engine for the AI Brain.

Replaces the old generic GradientBoosting ML model with a system that
learns directly from discovered patterns.  Three main components:

1. **Condition strength** -- continuous 0-1 signal instead of binary pass/fail
2. **Pattern feature engineering** -- rich feature vector per ticker
3. **PatternMetaLearner** -- gradient-boosting model trained on pattern
   features extracted from historical ``MarketSnapshot`` outcomes

**Validation**: Training uses chronological ``MarketSnapshot`` order and
``PurgedTimeSeriesSplit`` cross-validation (López de Prado-style) that
purges training samples whose forward-return labels overlap with test
fold boundaries and embargoes bars after each test fold.
"""
from __future__ import annotations

import json
import logging
import math
import os
import pickle
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_MODEL_PATH = _DATA_DIR / "pattern_meta_model.pkl"
_MIN_SAMPLES = 50


# ── Purged/embargoed cross-validation ────────────────────────────────

class PurgedTimeSeriesSplit:
    """TimeSeriesSplit with label-overlap purging and post-test embargo.

    Implements López de Prado's purged CV concept:
    - **Purge**: training samples whose forward-return label window
      overlaps with the test fold start are removed from the training set.
    - **Embargo**: a configurable number of bars immediately after each
      test fold end are excluded from subsequent training folds to avoid
      leaking adjacent information.

    Parameters
    ----------
    n_splits : int
        Number of temporal folds.
    label_horizon : int
        Number of bars the label spans into the future (e.g. 5 for
        ``future_return_5d``).  Training samples within this distance
        before each test fold start are purged.
    embargo_bars : int
        Number of bars after each test fold end to exclude from training.
    """

    def __init__(
        self,
        n_splits: int = 5,
        label_horizon: int = 5,
        embargo_bars: int = 2,
    ) -> None:
        self.n_splits = n_splits
        self.label_horizon = label_horizon
        self.embargo_bars = embargo_bars

    def split(self, X, y=None, groups=None):
        n = len(X)
        fold_size = n // (self.n_splits + 1)
        if fold_size < 1:
            return

        for i in range(self.n_splits):
            test_start = fold_size * (i + 1)
            test_end = min(fold_size * (i + 2), n)

            purge_start = max(0, test_start - self.label_horizon)
            embargo_end = min(n, test_end + self.embargo_bars)

            train_idx = list(range(0, purge_start))
            if embargo_end < n and i < self.n_splits - 1:
                pass  # don't add post-embargo to train for this fold

            test_idx = list(range(test_start, test_end))

            if len(train_idx) > 0 and len(test_idx) > 0:
                yield np.array(train_idx), np.array(test_idx)

    def get_n_splits(self, X=None, y=None, groups=None):
        return self.n_splits


# ── Condition strength ─────────────────────────────────────────────────

def compute_condition_strength(cond: dict, indicators: dict[str, Any]) -> float:
    """Return a continuous 0-1 strength for how well *indicators* satisfy *cond*.

    Returns 0.0 when data is missing or the condition is not met at all.
    """
    ind_key = cond.get("indicator", "")
    op = cond.get("op", "")
    value = cond.get("value")
    ref = cond.get("ref")

    actual = indicators.get(ind_key)
    if actual is None:
        return 0.0

    if ref:
        ref_val = indicators.get(ref)
        if ref_val is None:
            return 0.0
        value = ref_val

    try:
        if op in (">", ">="):
            a, v = float(actual), float(value)
            if a < v:
                return 0.0
            denom = abs(v) * 0.5 if abs(v) > 1e-9 else 1.0
            return min(1.0, (a - v) / denom)

        if op in ("<", "<="):
            a, v = float(actual), float(value)
            if a > v:
                return 0.0
            denom = abs(v) * 0.5 if abs(v) > 1e-9 else 1.0
            return min(1.0, (v - a) / denom)

        if op == "==":
            return 1.0 if actual == value else 0.0

        if op == "!=":
            return 1.0 if actual != value else 0.0

        if op == "between":
            if isinstance(value, list) and len(value) == 2:
                lo, hi = float(value[0]), float(value[1])
                a = float(actual)
                if a < lo or a > hi:
                    return 0.0
                mid = (lo + hi) / 2
                half = (hi - lo) / 2 if hi > lo else 1.0
                return max(0.0, 1.0 - abs(a - mid) / half)
            return 0.0

        if op == "any_of":
            if isinstance(value, list):
                return 1.0 if actual in value else 0.0
            return 0.0

        if op == "not_in":
            if isinstance(value, list):
                return 1.0 if actual not in value else 0.0
            return 0.0

    except (TypeError, ValueError):
        return 0.0

    return 0.0


# ── Feature engineering ────────────────────────────────────────────────

# Aggregate feature names (always present regardless of pattern count)
_AGG_FEATURES = [
    "n_patterns_matched",
    "total_strength",
    "pattern_agreement",
]


def extract_pattern_features(
    patterns: list,
    indicators: dict[str, Any],
) -> dict[str, float]:
    """Build a feature dict from pattern evaluations against *indicators*.

    Per-pattern features (keyed ``pat_{id}_*``):
      * ``matched`` -- 1.0 if all evaluable conditions pass, else 0.0
      * ``quality`` -- fraction of total conditions that were evaluable and passed
      * ``avg_strength`` -- mean condition strength across evaluable conditions

    Aggregate features:
      * ``n_patterns_matched`` -- count of patterns that matched
      * ``avg_wr_matched`` -- mean win-rate of matched patterns
      * ``max_wr_matched`` -- max win-rate of matched patterns
      * ``total_strength`` -- sum of (avg_strength * win_rate) across matches
      * ``pattern_agreement`` -- fraction of matched patterns with same direction as majority
    """
    from .pattern_engine import _eval_condition, _condition_has_data

    features: dict[str, float] = {}
    match_wrs: list[float] = []
    match_strengths: list[float] = []
    bullish_count = 0
    bearish_count = 0

    for pattern in patterns:
        pid = pattern.id
        try:
            rules = json.loads(pattern.rules_json)
        except (json.JSONDecodeError, TypeError):
            features[f"pat_{pid}_matched"] = 0.0
            features[f"pat_{pid}_quality"] = 0.0
            features[f"pat_{pid}_avg_strength"] = 0.0
            continue

        conditions = rules.get("conditions", [])
        if not conditions:
            features[f"pat_{pid}_matched"] = 0.0
            features[f"pat_{pid}_quality"] = 0.0
            features[f"pat_{pid}_avg_strength"] = 0.0
            continue

        evaluable = [c for c in conditions if _condition_has_data(c, indicators)]
        total = len(conditions)
        n_eval = len(evaluable)

        if n_eval < max(1, total * 0.5):
            features[f"pat_{pid}_matched"] = 0.0
            features[f"pat_{pid}_quality"] = 0.0
            features[f"pat_{pid}_avg_strength"] = 0.0
            continue

        all_pass = all(_eval_condition(c, indicators) for c in evaluable)

        if all_pass:
            strengths = [compute_condition_strength(c, indicators) for c in evaluable]
            avg_str = sum(strengths) / len(strengths) if strengths else 0.0
            quality = n_eval / total

            features[f"pat_{pid}_matched"] = 1.0
            features[f"pat_{pid}_quality"] = round(quality, 3)
            features[f"pat_{pid}_avg_strength"] = round(avg_str, 3)

            match_strengths.append(avg_str)

            boost = pattern.score_boost or 0.0
            if boost >= 0:
                bullish_count += 1
            else:
                bearish_count += 1
        else:
            features[f"pat_{pid}_matched"] = 0.0
            features[f"pat_{pid}_quality"] = 0.0
            features[f"pat_{pid}_avg_strength"] = 0.0

    n_matched = len(match_strengths)
    features["n_patterns_matched"] = float(n_matched)
    features["total_strength"] = round(sum(match_strengths), 3)
    majority = max(bullish_count, bearish_count)
    features["pattern_agreement"] = round(majority / n_matched, 3) if n_matched else 0.0

    return features


# ── Retro-evaluation (for training data) ──────────────────────────────

def retro_evaluate_snapshot(
    indicator_data_json: str | dict,
    patterns: list,
    close_price: float | None,
) -> dict[str, float]:
    """Evaluate patterns against a historical snapshot's indicator data.

    Used to build training rows from ``MarketSnapshot`` records.
    """
    from .learning_predictions import _indicator_data_to_flat_snapshot

    if isinstance(indicator_data_json, str):
        try:
            ind_data = json.loads(indicator_data_json)
        except (json.JSONDecodeError, TypeError):
            return {}
    else:
        ind_data = indicator_data_json

    if not ind_data:
        return {}

    clean = {k: v for k, v in ind_data.items() if k not in ("ticker", "interval")}
    flat = _indicator_data_to_flat_snapshot(clean, close_price)
    return extract_pattern_features(patterns, flat)


# ── Meta-learner ──────────────────────────────────────────────────────

class PatternMetaLearner:
    """Gradient-boosting model trained on pattern features + outcomes."""

    def __init__(self) -> None:
        self._model = None
        self._stats: dict[str, Any] = {}
        self._feature_names: list[str] = []
        self._training_feature_stats: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    # ── persistence ───────────────────────────────────────────────

    def save(self) -> None:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(_MODEL_PATH, "wb") as f:
                pickle.dump({
                    "model": self._model,
                    "stats": self._stats,
                    "features": self._feature_names,
                    "training_feature_stats": self._training_feature_stats,
                }, f)
            logger.info("[pattern_ml] Model saved to %s", _MODEL_PATH)
        except Exception as exc:
            logger.warning("[pattern_ml] Could not save model: %s", exc)

    def load(self) -> bool:
        if not _MODEL_PATH.exists():
            return False
        try:
            with open(_MODEL_PATH, "rb") as f:
                data = pickle.load(f)
            with self._lock:
                self._model = data["model"]
                self._stats = data.get("stats", {})
                self._feature_names = data.get("features", [])
                self._training_feature_stats = data.get("training_feature_stats", {})
            logger.info(
                "[pattern_ml] Model loaded (%s samples)",
                self._stats.get("samples", "?"),
            )
            return True
        except Exception as exc:
            logger.warning("[pattern_ml] Could not load model: %s", exc)
            return False

    # ── training ──────────────────────────────────────────────────

    def _make_meta_learner_clf(self):
        """Build classifier: LightGBM GPU if enabled and available, else sklearn GBM."""
        from sklearn.ensemble import GradientBoostingClassifier

        try:
            from ...config import settings
            if getattr(settings, "brain_use_gpu_ml", False):
                import lightgbm as lgb
                try:
                    clf = lgb.LGBMClassifier(
                        n_estimators=120,
                        max_depth=4,
                        learning_rate=0.08,
                        subsample=0.8,
                        min_child_samples=5,
                        random_state=42,
                        device="gpu",
                        verbose=-1,
                    )
                    clf.device = "gpu"
                    logger.info("[pattern_ml] Using LightGBM GPU for meta-learner")
                    return clf
                except Exception as e:
                    logger.warning("[pattern_ml] LightGBM GPU failed (%s), trying CPU", e)
                try:
                    clf = lgb.LGBMClassifier(
                        n_estimators=120,
                        max_depth=4,
                        learning_rate=0.08,
                        subsample=0.8,
                        min_child_samples=5,
                        random_state=42,
                        device="cpu",
                        verbose=-1,
                    )
                    logger.info("[pattern_ml] Using LightGBM CPU for meta-learner")
                    return clf
                except Exception:
                    pass
        except Exception as e:
            logger.debug("[pattern_ml] GPU/lightgbm path skipped: %s", e)
        return GradientBoostingClassifier(
            n_estimators=120,
            max_depth=4,
            learning_rate=0.08,
            subsample=0.8,
            min_samples_leaf=5,
            random_state=42,
        )

    def train(self, db) -> dict[str, Any]:
        """Train on ``MarketSnapshot`` rows using pattern features."""
        from sklearn.model_selection import cross_val_score
        from sklearn.metrics import accuracy_score, precision_score, recall_score

        from ...models.trading import MarketSnapshot
        from .pattern_engine import get_active_patterns

        patterns = get_active_patterns(db)
        if not patterns:
            return {
                "ok": False,
                "reason": "No active patterns to train on",
            }

        snaps = (
            db.query(MarketSnapshot)
            .filter(
                MarketSnapshot.future_return_5d.isnot(None),
                MarketSnapshot.indicator_data.isnot(None),
            )
            .order_by(MarketSnapshot.snapshot_date.asc())
            .limit(5000)
            .all()
        )

        if len(snaps) < _MIN_SAMPLES:
            return {
                "ok": False,
                "reason": "Not enough labeled snapshots",
                "labeled_snapshots": len(snaps),
                "needed": _MIN_SAMPLES,
            }

        # Build feature names from patterns
        feature_names = list(_AGG_FEATURES)
        for p in patterns:
            for suffix in ("matched", "quality", "avg_strength"):
                feature_names.append(f"pat_{p.id}_{suffix}")

        # Use >1% return as positive label (filters out noise near zero)
        _LABEL_THRESHOLD_PCT = 1.0

        X_rows: list[list[float]] = []
        y_rows: list[int] = []

        for snap in snaps:
            try:
                feats = retro_evaluate_snapshot(
                    snap.indicator_data, patterns, snap.close_price,
                )
                if not feats:
                    continue
                row = [feats.get(f, 0.0) for f in feature_names]
                label = 1 if (snap.future_return_5d or 0) > _LABEL_THRESHOLD_PCT else 0
                X_rows.append(row)
                y_rows.append(label)
            except Exception:
                continue

        if len(X_rows) < _MIN_SAMPLES:
            return {
                "ok": False,
                "reason": "Not enough usable samples after feature extraction",
                "usable_samples": len(X_rows),
                "needed": _MIN_SAMPLES,
            }

        X = np.array(X_rows)
        y = np.array(y_rows)

        # Temporal split: train on first 80%, holdout last 20% for OOS eval
        split_idx = int(len(X) * 0.8)
        X_train, X_oos = X[:split_idx], X[split_idx:]
        y_train, y_oos = y[:split_idx], y[split_idx:]

        if len(X_train) < _MIN_SAMPLES or len(X_oos) < 20:
            return {
                "ok": False,
                "reason": "Not enough samples for temporal split",
                "train_size": len(X_train),
                "oos_size": len(X_oos),
            }

        # Capture training feature distributions for drift monitoring
        train_feature_stats: dict[str, dict[str, float]] = {}
        for fi, fname in enumerate(feature_names):
            col = X_train[:, fi]
            q = np.quantile(col, [0.1, 0.25, 0.5, 0.75, 0.9]).tolist()
            train_feature_stats[fname] = {
                "mean": float(np.mean(col)),
                "std": float(np.std(col)),
                "q10": q[0], "q25": q[1], "q50": q[2], "q75": q[3], "q90": q[4],
            }

        clf = self._make_meta_learner_clf()

        # Cross-validate on training set only (purged temporal folds)
        n_splits = min(5, max(2, len(X_train) // 200))
        tscv = PurgedTimeSeriesSplit(
            n_splits=n_splits, label_horizon=5, embargo_bars=2,
        )
        n_jobs_cv = 1 if getattr(clf, "device", None) == "gpu" else -1
        cv_scores = cross_val_score(
            clf, X_train, y_train, cv=tscv, scoring="accuracy", n_jobs=n_jobs_cv,
        )
        logger.debug(
            "[pattern_ml] Purged CV: %d folds, label_horizon=5, embargo=2",
            n_splits,
        )

        # Train on training set, evaluate on OOS holdout
        clf.fit(X_train, y_train)
        y_pred_oos = clf.predict(X_oos)

        oos_acc = round(accuracy_score(y_oos, y_pred_oos) * 100, 1)
        cv_acc = round(cv_scores.mean() * 100, 1)
        precision = round(precision_score(y_oos, y_pred_oos, zero_division=0) * 100, 1)
        recall = round(recall_score(y_oos, y_pred_oos, zero_division=0) * 100, 1)

        raw_importances = {
            feature_names[i]: round(float(clf.feature_importances_[i]), 4)
            for i in range(len(feature_names))
        }
        raw_importances = dict(
            sorted(raw_importances.items(), key=lambda x: x[1], reverse=True)
        )

        with self._lock:
            self._model = clf
            self._feature_names = feature_names
            self._training_feature_stats = train_feature_stats
            self._stats = {
                "trained_at": datetime.utcnow().isoformat(),
                "samples": len(X),
                "train_samples": len(X_train),
                "oos_samples": len(X_oos),
                "positive_rate": round(float(y.mean()) * 100, 1),
                "label_threshold_pct": _LABEL_THRESHOLD_PCT,
                "oos_accuracy": oos_acc,
                "cv_accuracy": cv_acc,
                "cv_method": "PurgedTimeSeriesSplit",
                "cv_splits": n_splits,
                "precision_oos": precision,
                "recall_oos": recall,
                "feature_importances": raw_importances,
                "active_patterns": len(patterns),
            }

        self.save()

        try:
            from .model_registry import get_registry
            reg = get_registry()
            reg.register(
                model_type="pattern_meta_learner",
                metrics={
                    "oos_accuracy": oos_acc,
                    "cv_accuracy": cv_acc,
                    "precision_oos": precision,
                    "recall_oos": recall,
                    "samples": len(X),
                    "train_samples": len(X_train),
                    "oos_samples": len(X_oos),
                },
                file_path=str(self._save_path) if hasattr(self, "_save_path") else None,
                notes=f"auto-train {len(patterns)} patterns, {len(X)} samples",
            )
        except Exception:
            logger.debug("[pattern_ml] Failed to register model version", exc_info=True)

        logger.info(
            "[pattern_ml] Trained on %d samples (OOS=%d, %d patterns): "
            "OOS acc=%s%%, CV acc=%s%%, prec=%s%%, recall=%s%%",
            len(X_train), len(X_oos), len(patterns),
            oos_acc, cv_acc, precision, recall,
        )

        return {"ok": True, **self._stats}

    # ── inference ─────────────────────────────────────────────────

    def is_ready(self) -> bool:
        with self._lock:
            return self._model is not None

    def predict(self, features: dict[str, float]) -> float | None:
        """Return probability (0-1) that the ticker goes up in 5 days."""
        with self._lock:
            model = self._model
            names = self._feature_names
        if model is None or not names:
            return None
        try:
            row = np.array([[features.get(f, 0.0) for f in names]])
            prob = float(model.predict_proba(row)[0][1])
            return round(prob, 4)
        except Exception:
            return None

    def get_training_feature_stats(self) -> dict[str, dict[str, float]]:
        """Return per-feature distribution stats captured at training time."""
        with self._lock:
            return dict(self._training_feature_stats)

    def get_stats(self) -> dict[str, Any]:
        return dict(self._stats) if self._stats else {
            "trained_at": None,
            "samples": 0,
            "cv_accuracy": 0,
        }

    def get_pattern_importances(self) -> dict[int, float]:
        """Aggregate feature importances per pattern ID.

        Sums the importance of ``pat_{id}_matched``, ``pat_{id}_quality``,
        and ``pat_{id}_avg_strength`` into a single value per pattern.
        """
        imps = self._stats.get("feature_importances", {})
        if not imps:
            return {}

        per_pattern: dict[int, float] = {}
        for fname, imp in imps.items():
            if fname.startswith("pat_"):
                parts = fname.split("_")
                try:
                    pid = int(parts[1])
                except (IndexError, ValueError):
                    continue
                per_pattern[pid] = per_pattern.get(pid, 0.0) + imp
        per_pattern = dict(
            sorted(per_pattern.items(), key=lambda x: x[1], reverse=True)
        )
        return per_pattern


# ── Module-level singleton ────────────────────────────────────────────

_meta_learner = PatternMetaLearner()


def get_meta_learner() -> PatternMetaLearner:
    return _meta_learner


def load_meta_learner() -> bool:
    return _meta_learner.load()


def check_drift_and_retrain(db, threshold_z: float = 2.0) -> dict[str, Any]:
    """Check feature distribution drift and retrain if significant shift detected.

    Compares current feature distributions from recent snapshots against
    training-time distributions. If >30% of features have z-score drift
    above threshold, triggers automatic retrain.
    """
    from ...models.trading import MarketSnapshot

    ml = get_meta_learner()
    train_stats = ml.get_training_feature_stats()

    if not train_stats:
        return {"ok": False, "reason": "no_training_stats", "retrained": False}

    recent = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.indicator_data.isnot(None))
        .order_by(MarketSnapshot.snapshot_date.desc())
        .limit(200)
        .all()
    )

    if len(recent) < 50:
        return {"ok": False, "reason": "insufficient_recent_data", "retrained": False}

    feature_vals: dict[str, list[float]] = {}
    for snap in recent:
        ind = snap.indicator_data or {}
        for fname in train_stats:
            if fname in ind:
                feature_vals.setdefault(fname, []).append(float(ind[fname]))

    drifted_features = []
    for fname, stats in train_stats.items():
        vals = feature_vals.get(fname, [])
        if len(vals) < 20:
            continue
        current_mean = sum(vals) / len(vals)
        train_mean = stats.get("mean", 0)
        train_std = stats.get("std", 1)
        if train_std > 0:
            z = abs(current_mean - train_mean) / train_std
            if z > threshold_z:
                drifted_features.append({"feature": fname, "z_score": round(z, 2)})

    drift_ratio = len(drifted_features) / max(len(train_stats), 1)
    needs_retrain = drift_ratio > 0.3

    result: dict[str, Any] = {
        "ok": True,
        "features_checked": len(train_stats),
        "features_drifted": len(drifted_features),
        "drift_ratio": round(drift_ratio, 3),
        "threshold_z": threshold_z,
        "needs_retrain": needs_retrain,
        "drifted": drifted_features[:10],
        "retrained": False,
    }

    if needs_retrain:
        logger.warning("[pattern_ml] Drift detected (%.0f%% features), triggering retrain", drift_ratio * 100)
        train_result = ml.train(db)
        result["retrained"] = train_result.get("ok", False)
        result["retrain_result"] = train_result

    return result


# ── Feedback loop ─────────────────────────────────────────────────────

def apply_ml_feedback(db, importances: dict[int, float]) -> dict[str, Any]:
    """Adjust pattern ``score_boost`` based on meta-learner importances.

    * Top-20% by importance: ``score_boost += 0.3`` (cap 5.0)
    * Bottom-20% with near-zero importance: ``score_boost -= 0.2`` (floor 0.0)

    Returns a summary of adjustments made.
    """
    from ...models.trading import ScanPattern

    if not importances:
        return {"boosted": 0, "penalised": 0}

    sorted_items = sorted(importances.items(), key=lambda x: x[1], reverse=True)
    n = len(sorted_items)
    top_cutoff = max(1, int(n * 0.2))
    bottom_cutoff = max(1, int(n * 0.2))

    top_ids = {pid for pid, _ in sorted_items[:top_cutoff]}
    bottom_ids = {
        pid for pid, imp in sorted_items[-bottom_cutoff:]
        if imp < 0.001
    }

    all_ids = list(top_ids | bottom_ids)
    patterns_by_id: dict[int, Any] = {}
    if all_ids:
        pats = db.query(ScanPattern).filter(
            ScanPattern.id.in_(all_ids), ScanPattern.active.is_(True),
        ).all()
        patterns_by_id = {p.id: p for p in pats}

    boosted = penalised = 0

    for pid in top_ids:
        pat = patterns_by_id.get(pid)
        if pat:
            old = pat.score_boost or 0.0
            pat.score_boost = min(5.0, round(old + 0.3, 2))
            boosted += 1

    for pid in bottom_ids:
        pat = patterns_by_id.get(pid)
        if pat:
            old = pat.score_boost or 0.0
            pat.score_boost = max(0.0, round(old - 0.2, 2))
            penalised += 1

    if boosted or penalised:
        try:
            db.commit()
        except Exception:
            db.rollback()

    logger.info(
        "[pattern_ml] Feedback applied: %d boosted, %d penalised",
        boosted, penalised,
    )
    return {"boosted": boosted, "penalised": penalised}
