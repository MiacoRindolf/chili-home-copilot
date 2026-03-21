"""Optional ML on PatternTradeRow (tabular baseline; LightGBM if installed)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from sqlalchemy.orm import Session

from ...models.trading import PatternTradeRow

logger = logging.getLogger(__name__)

_MIN_ROWS = 40


def train_on_pattern_trades(
    db: Session,
    scan_pattern_id: int,
    *,
    window_days: int = 365,
) -> dict[str, Any]:
    """Train a classifier: features from features_json -> label_win. Uses sklearn or LightGBM."""
    since = datetime.utcnow() - timedelta(days=window_days)
    rows = (
        db.query(PatternTradeRow)
        .filter(PatternTradeRow.scan_pattern_id == scan_pattern_id)
        .filter(PatternTradeRow.as_of_ts >= since)
        .filter(PatternTradeRow.label_win.isnot(None))
        .all()
    )
    if len(rows) < _MIN_ROWS:
        return {
            "ok": False,
            "reason": f"need >= {_MIN_ROWS} labeled rows",
            "n": len(rows),
        }

    # Flatten numeric features
    keys: set[str] = set()
    for r in rows:
        fj = r.features_json or {}
        if isinstance(fj, dict):
            for k, v in fj.items():
                if isinstance(v, (int, float)) and k != "schema":
                    keys.add(k)
    feat_names = sorted(keys)
    if len(feat_names) < 2:
        return {"ok": False, "reason": "not enough numeric feature keys", "keys": feat_names}

    X: list[list[float]] = []
    y: list[int] = []
    for r in rows:
        fj = r.features_json or {}
        row = [float(fj.get(k, 0.0) or 0.0) for k in feat_names]
        X.append(row)
        y.append(1 if r.label_win else 0)

    Xa = np.array(X, dtype=float)
    ya = np.array(y, dtype=int)

    try:
        import lightgbm as lgb  # type: ignore

        clf = lgb.LGBMClassifier(
            n_estimators=80, max_depth=4, learning_rate=0.08, random_state=42, verbose=-1
        )
        clf.fit(Xa, ya)
        model_name = "lightgbm"
    except Exception:
        from sklearn.ensemble import GradientBoostingClassifier

        clf = GradientBoostingClassifier(
            random_state=42, max_depth=3, n_estimators=80, learning_rate=0.08
        )
        clf.fit(Xa, ya)
        model_name = "sklearn_gbc"

    imp = dict(zip(feat_names, [float(x) for x in clf.feature_importances_]))
    imp = dict(sorted(imp.items(), key=lambda x: -x[1])[:20])

    return {
        "ok": True,
        "model": model_name,
        "samples": len(rows),
        "positive_rate": round(100.0 * float(ya.mean()), 1),
        "feature_importances": imp,
    }
