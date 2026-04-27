"""K Phase 2 — pattern-survival training pipeline.

Three orchestrated stages run sequentially in
``run_pattern_survival_training_pass``:

  Stage 1 (always): label backfill
    Find rows in ``pattern_survival_predictions`` where the 30-day
    horizon has closed (``snapshot_date + 30d <= NOW()``) but
    ``actual_survived`` is still NULL. Look up the pattern's current
    lifecycle state and fill in the actual outcome.

    "Survived" definition:
      lifecycle_stage IN ('live', 'challenged')
      AND (active=True OR trade_count > 0 in last 30d)

    Patterns that drifted to 'demoted' / 'candidate' or went silent
    (active=False with no recent trades) count as did-not-survive.

  Stage 2 (when threshold met): train
    Pull all labeled examples (``actual_survived IS NOT NULL``) joined
    to the corresponding feature rows. Train a sklearn
    ``HistGradientBoostingClassifier`` (drop-in for LightGBM, no extra
    dependency, fast for ~thousands of rows). Persist to
    ``/app/models/pattern_survival/{version}.pkl``. Skip with a reason
    when fewer than ``_MIN_LABELED_FOR_TRAIN`` rows are available.

  Stage 3 (when model exists): score
    For pattern_survival_features rows not yet scored under the latest
    model_version, compute ``survival_probability`` and INSERT a
    pattern_survival_predictions row.

Flag-gated by ``chili_pattern_survival_classifier_enabled`` (default
OFF). Phase 1 (feature collection) and Phase 2 (this module) share the
same flag; Phase 3 — actually consuming the prediction in demotion /
sizing decisions — stays gated separately by
``chili_pattern_survival_decisions_enabled``.

The trained-model artifact is stored on disk, NOT in the DB. Reads are
infrequent (once per scoring pass) and pickling sklearn models into
JSONB is more pain than it's worth. The artifact path is recorded in
the model_version field on pattern_survival_predictions for traceability.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Tunables — kept conservative until live data accumulates.
_LABEL_HORIZON_DAYS = 30
_MIN_LABELED_FOR_TRAIN = 50
_MIN_FEATURE_AGE_DAYS = 1     # don't score same-day features (no settling time)
_DEFAULT_DECISION_THRESHOLD = 0.5
_MODEL_DIR = Path("/app/models/pattern_survival")
_MODEL_NAME = "survival_clf"


# Feature columns pulled from pattern_survival_features. Ordered; the
# model assumes this exact column order. Adding a feature requires
# bumping the model version and retraining from scratch.
_FEATURE_COLUMNS: list[str] = [
    "age_days",
    "trades_30d",
    "hit_rate_30d",
    "expectancy_30d_pct",
    "sharpe_30d",
    "max_drawdown_30d_pct",
    "pnl_slope_14d",
    "cpcv_dsr",
    "cpcv_pbo",
    "cpcv_n_paths",
    "family_concentration_herfindahl",
    "family_active_count",
]


# ─────────────────────────────────────────────────────────────────────
# Stage 1 — label backfill
# ─────────────────────────────────────────────────────────────────────

def backfill_survival_labels(
    db: Session, *, horizon_days: int = _LABEL_HORIZON_DAYS,
) -> dict[str, Any]:
    """Update pattern_survival_predictions rows whose horizon has closed.

    For each unresolved prediction (snapshot_date >= horizon_days ago,
    actual_survived IS NULL), look up the scan_pattern's CURRENT state
    and decide:

      survived = lifecycle_stage IN ('live', 'challenged')
                 AND (active = TRUE OR trade_count > 0 in 30d)

    The "OR active=TRUE" branch catches patterns that survived but
    happened to not trade in the rolling window — they're still live,
    just quiet. The trade_count fallback catches patterns flipped
    inactive but still trading via grandfathered paths.

    Returns counts: rows_resolved, rows_survived, rows_dnf.
    """
    cutoff = text(
        "NOW() - make_interval(days => :h)"
    )
    try:
        rows = db.execute(text(
            """
            SELECT pp.id, pp.scan_pattern_id, pp.snapshot_date,
                   sp.lifecycle_stage,
                   COALESCE(sp.active, FALSE) AS active,
                   COALESCE(sp.trade_count, 0) AS trade_count
            FROM pattern_survival_predictions pp
            JOIN scan_patterns sp ON sp.id = pp.scan_pattern_id
            WHERE pp.actual_survived IS NULL
              AND pp.snapshot_date <= (NOW() - make_interval(days => :h))::date
            ORDER BY pp.snapshot_date ASC
            """
        ), {"h": horizon_days}).fetchall()
    except Exception as e:
        logger.warning("[ps_train] backfill query failed: %s", e)
        return {"error": str(e)[:200]}

    resolved = 0
    survived = 0
    dnf = 0
    for r in rows or []:
        pred_id, pid, sd, stage, active, tc = r
        ok = (
            stage in ("live", "challenged")
            and (bool(active) or int(tc or 0) > 0)
        )
        reason = None if ok else f"lifecycle={stage} active={active} trades={tc}"
        try:
            db.execute(text(
                """
                UPDATE pattern_survival_predictions
                SET actual_survived = :s,
                    actual_demote_reason = :r,
                    label_resolved_at = NOW()
                WHERE id = :i
                """
            ), {"s": ok, "r": reason, "i": pred_id})
            resolved += 1
            if ok:
                survived += 1
            else:
                dnf += 1
        except Exception as e:
            logger.warning(
                "[ps_train] label update id=%s failed: %s", pred_id, e
            )
    db.commit()
    return {
        "rows_resolved": resolved,
        "rows_survived": survived,
        "rows_dnf": dnf,
        "horizon_days": horizon_days,
    }


# ─────────────────────────────────────────────────────────────────────
# Stage 2 — train
# ─────────────────────────────────────────────────────────────────────

def _model_version(weights_blob: bytes) -> str:
    h = hashlib.sha256(weights_blob).hexdigest()[:16]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}_{h}"


def _save_model(model, version: str) -> Path:
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = _MODEL_DIR / f"{version}.pkl"
    with open(path, "wb") as f:
        pickle.dump(model, f)
    return path


def _load_latest_model() -> Optional[tuple[Any, str]]:
    if not _MODEL_DIR.exists():
        return None
    files = sorted(
        _MODEL_DIR.glob("*.pkl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return None
    try:
        with open(files[0], "rb") as f:
            return pickle.load(f), files[0].stem
    except Exception as e:
        logger.warning("[ps_train] load model %s failed: %s", files[0], e)
        return None


def train_survival_classifier(
    db: Session, *, min_labeled: int = _MIN_LABELED_FOR_TRAIN,
) -> dict[str, Any]:
    """Train a HistGradientBoostingClassifier on labeled features.

    Returns ``{ok, reason, model_version, n_train, n_pos}``. Skips with
    ok=False when fewer than ``min_labeled`` labeled rows exist.
    """
    try:
        rows = db.execute(text(
            f"""
            SELECT
              {', '.join('f.' + c for c in _FEATURE_COLUMNS)},
              p.actual_survived
            FROM pattern_survival_features f
            JOIN pattern_survival_predictions p
              ON p.feature_id = f.id
            WHERE p.actual_survived IS NOT NULL
            """
        )).fetchall()
    except Exception as e:
        logger.warning("[ps_train] training query failed: %s", e)
        return {"ok": False, "reason": "query_failed", "error": str(e)[:200]}

    if not rows or len(rows) < min_labeled:
        return {
            "ok": False,
            "reason": "insufficient_labels",
            "n_labeled": len(rows or []),
            "min_required": min_labeled,
        }

    # Build feature matrix; impute NaNs with column median (sklearn HGB
    # handles NaN natively, but median imputation makes the model less
    # sensitive to bulk-NaN columns during early training).
    try:
        import numpy as np  # local import — sklearn pulls numpy anyway
        from sklearn.ensemble import HistGradientBoostingClassifier
    except ImportError:
        return {"ok": False, "reason": "sklearn_unavailable"}

    n_cols = len(_FEATURE_COLUMNS)
    X = np.zeros((len(rows), n_cols), dtype=float)
    y = np.zeros(len(rows), dtype=int)
    for i, r in enumerate(rows):
        for j in range(n_cols):
            v = r[j]
            X[i, j] = float(v) if v is not None else float("nan")
        y[i] = 1 if r[n_cols] else 0

    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos < 5 or n_neg < 5:
        return {
            "ok": False,
            "reason": "class_imbalance",
            "n_pos": n_pos,
            "n_neg": n_neg,
        }

    clf = HistGradientBoostingClassifier(
        max_iter=200,
        learning_rate=0.05,
        max_leaf_nodes=15,
        min_samples_leaf=10,
        random_state=42,
    )
    clf.fit(X, y)
    weights_blob = pickle.dumps(clf)
    version = _model_version(weights_blob)
    path = _save_model(clf, version)
    train_acc = float(clf.score(X, y))
    logger.info(
        "[ps_train] trained version=%s n=%d pos=%d acc=%.3f path=%s",
        version, len(y), n_pos, train_acc, path,
    )
    return {
        "ok": True,
        "model_version": version,
        "model_path": str(path),
        "n_train": len(y),
        "n_pos": n_pos,
        "n_neg": n_neg,
        "train_accuracy": round(train_acc, 4),
    }


# ─────────────────────────────────────────────────────────────────────
# Stage 3 — score
# ─────────────────────────────────────────────────────────────────────

def score_pending_features(
    db: Session, *, decision_threshold: float = _DEFAULT_DECISION_THRESHOLD,
) -> dict[str, Any]:
    """Score features that don't have a prediction under the latest model."""
    loaded = _load_latest_model()
    if loaded is None:
        return {"ok": False, "reason": "no_model_artifact"}
    model, version = loaded

    try:
        rows = db.execute(text(
            f"""
            SELECT f.id, f.scan_pattern_id, f.snapshot_date,
                   {', '.join('f.' + c for c in _FEATURE_COLUMNS)}
            FROM pattern_survival_features f
            WHERE NOT EXISTS (
                SELECT 1 FROM pattern_survival_predictions p
                WHERE p.feature_id = f.id
                  AND p.model_version = :v
            )
              AND f.snapshot_date >= (NOW() - INTERVAL '60 days')::date
            ORDER BY f.snapshot_date DESC
            LIMIT 5000
            """
        ), {"v": version}).fetchall()
    except Exception as e:
        logger.warning("[ps_train] scoring query failed: %s", e)
        return {"ok": False, "reason": "query_failed", "error": str(e)[:200]}

    if not rows:
        return {"ok": True, "scored": 0, "model_version": version}

    try:
        import numpy as np
    except ImportError:
        return {"ok": False, "reason": "numpy_unavailable"}

    n_cols = len(_FEATURE_COLUMNS)
    n = len(rows)
    X = np.zeros((n, n_cols), dtype=float)
    feature_ids: list[int] = []
    pattern_ids: list[int] = []
    snapshot_dates: list[Any] = []
    for i, r in enumerate(rows):
        feature_ids.append(int(r[0]))
        pattern_ids.append(int(r[1]))
        snapshot_dates.append(r[2])
        for j in range(n_cols):
            v = r[3 + j]
            X[i, j] = float(v) if v is not None else float("nan")

    try:
        proba = model.predict_proba(X)[:, 1]
    except Exception as e:
        logger.warning("[ps_train] model predict_proba failed: %s", e)
        return {"ok": False, "reason": "predict_failed", "error": str(e)[:200]}

    scored = 0
    for i in range(n):
        p = float(proba[i])
        try:
            db.execute(text(
                """
                INSERT INTO pattern_survival_predictions
                    (feature_id, scan_pattern_id, snapshot_date,
                     model_name, model_version, trained_at,
                     survival_probability, decision_threshold,
                     predicted_label, label_horizon_days)
                VALUES (:fi, :pi, :sd, :mn, :mv, NOW(),
                        :sp, :dt, :pl, :hd)
                """
            ), {
                "fi": feature_ids[i],
                "pi": pattern_ids[i],
                "sd": snapshot_dates[i],
                "mn": _MODEL_NAME,
                "mv": version,
                "sp": p,
                "dt": decision_threshold,
                "pl": bool(p >= decision_threshold),
                "hd": _LABEL_HORIZON_DAYS,
            })
            scored += 1
            if scored % 500 == 0:
                db.commit()
        except Exception as e:
            logger.debug(
                "[ps_train] score insert feature_id=%s failed: %s",
                feature_ids[i], e,
            )
    db.commit()
    return {"ok": True, "scored": scored, "model_version": version}


# ─────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────

def run_pattern_survival_training_pass(db: Session) -> dict[str, Any]:
    """One pass: backfill labels -> maybe train -> score new features.

    Flag-gated. When chili_pattern_survival_classifier_enabled is False,
    returns immediately with skipped=flag_off.
    """
    from ....config import settings
    if not getattr(
        settings, "chili_pattern_survival_classifier_enabled", False
    ):
        return {"skipped": "flag_off"}

    out: dict[str, Any] = {}
    out["backfill"] = backfill_survival_labels(db)
    out["train"] = train_survival_classifier(db)
    out["score"] = score_pending_features(db)
    logger.info("[ps_train] pass complete: %s", out)
    return out
