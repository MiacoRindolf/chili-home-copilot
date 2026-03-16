"""Machine learning prediction engine for the AI Brain.

Uses GradientBoostingClassifier trained on historical snapshot outcomes
to predict whether a ticker will go up in the next 5 days.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_MODEL_PATH = _DATA_DIR / "ml_model.pkl"
_MIN_SAMPLES = 50

_model = None
_model_stats: dict[str, Any] = {}
_model_lock = threading.Lock()

FEATURE_NAMES = [
    "rsi", "macd_hist", "macd_crossover", "ema_alignment",
    "bb_position", "stoch_k", "adx", "atr_pct",
    "obv_direction", "vol_ratio", "is_crypto", "vix",
    "spy_momentum_5d", "regime_numeric",
    "news_sentiment", "news_count", "pe_ratio_log",
]


def extract_features(
    indicator_data: dict,
    close_price: float | None = None,
    vix: float | None = None,
    regime: dict | None = None,
) -> dict[str, float]:
    """Extract ML features from an indicator_data dict."""
    rsi_data = indicator_data.get("rsi") or {}
    macd_data = indicator_data.get("macd") or {}
    bb_data = indicator_data.get("bbands") or {}
    stoch_data = indicator_data.get("stoch") or {}
    adx_data = indicator_data.get("adx") or {}
    atr_data = indicator_data.get("atr") or {}
    obv_data = indicator_data.get("obv") or {}
    ema20_data = indicator_data.get("ema_20") or {}
    ema50_data = indicator_data.get("ema_50") or {}
    ema100_data = indicator_data.get("ema_100") or {}

    rsi = rsi_data.get("value")
    macd_hist = macd_data.get("histogram")
    macd_line = macd_data.get("macd")
    macd_sig = macd_data.get("signal")
    stoch_k = stoch_data.get("k")
    adx_val = adx_data.get("adx")
    atr_val = atr_data.get("value")

    e20 = ema20_data.get("value")
    e50 = ema50_data.get("value")
    e100 = ema100_data.get("value")
    ema_align = 0.0
    if e20 is not None and e50 is not None and e100 is not None:
        if e20 > e50 > e100:
            ema_align = 1.0
        elif e20 < e50 < e100:
            ema_align = -1.0
        elif e20 > e50:
            ema_align = 0.5
        elif e20 < e50:
            ema_align = -0.5

    bb_upper = bb_data.get("upper")
    bb_lower = bb_data.get("lower")
    bb_pos = 0.5
    if bb_upper and bb_lower and bb_upper > bb_lower:
        price = close_price or e20 or 0
        if price > 0:
            bb_pos = max(0, min(1, (price - bb_lower) / (bb_upper - bb_lower)))

    atr_pct = 0.0
    if atr_val and close_price and close_price > 0:
        atr_pct = atr_val / close_price * 100

    macd_cross = 0.0
    if macd_line is not None and macd_sig is not None:
        macd_cross = 1.0 if macd_line > macd_sig else -1.0

    obv_dir = obv_data.get("5d_direction")
    obv_val = 0.0
    if obv_dir == "rising":
        obv_val = 1.0
    elif obv_dir == "falling":
        obv_val = -1.0

    ticker = indicator_data.get("ticker", "")
    is_crypto = 1.0 if (isinstance(ticker, str) and ticker.endswith("-USD")) else 0.0

    spy_mom = 0.0
    regime_num = 0.0
    if regime:
        spy_mom = regime.get("spy_momentum_5d", 0.0) or 0.0
        regime_num = float(regime.get("regime_numeric", 0))
    elif indicator_data.get("spy_momentum_5d") is not None:
        spy_mom = float(indicator_data["spy_momentum_5d"])
        regime_num = float(indicator_data.get("regime_numeric", 0))

    import math
    news_sent = indicator_data.get("news_sentiment")
    news_cnt = indicator_data.get("news_count")
    pe_raw = indicator_data.get("pe_ratio")
    mcap_raw = indicator_data.get("market_cap_b")
    pe_log = math.log1p(pe_raw) if pe_raw and pe_raw > 0 else 0.0

    return {
        "rsi": rsi if rsi is not None else 50.0,
        "macd_hist": macd_hist if macd_hist is not None else 0.0,
        "macd_crossover": macd_cross,
        "ema_alignment": ema_align,
        "bb_position": bb_pos,
        "stoch_k": stoch_k if stoch_k is not None else 50.0,
        "adx": adx_val if adx_val is not None else 15.0,
        "atr_pct": atr_pct,
        "obv_direction": obv_val,
        "vol_ratio": indicator_data.get("vol_ratio", 1.0),
        "is_crypto": is_crypto,
        "vix": vix if vix is not None else 18.0,
        "spy_momentum_5d": spy_mom,
        "regime_numeric": regime_num,
        "news_sentiment": float(news_sent) if news_sent is not None else 0.0,
        "news_count": float(news_cnt) if news_cnt is not None else 0.0,
        "pe_ratio_log": pe_log,
    }


def _features_to_array(features: dict[str, float]) -> np.ndarray:
    return np.array([[features.get(f, 0.0) for f in FEATURE_NAMES]])


def train_model(db) -> dict[str, Any]:
    """Train a GradientBoostingClassifier on labeled snapshots."""
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import cross_val_score
    from sklearn.metrics import accuracy_score, precision_score, recall_score

    from ...models.trading import MarketSnapshot

    global _model, _model_stats

    snaps = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
        MarketSnapshot.indicator_data.isnot(None),
    ).order_by(MarketSnapshot.snapshot_date.desc()).limit(5000).all()

    if len(snaps) < _MIN_SAMPLES:
        logger.info(f"[ml_engine] Not enough labeled snapshots ({len(snaps)}/{_MIN_SAMPLES})")
        return {
            "ok": False,
            "reason": "Not enough training data yet",
            "labeled_snapshots": len(snaps),
            "needed": _MIN_SAMPLES,
            "tip": "Run a full market scan, then wait 1-2 days for outcomes to be verified. "
                   "Snapshots need at least 5 trading days before their predictions can be checked.",
        }

    def _extract_one(snap_data: dict) -> tuple[list[float], int] | None:
        try:
            ind_data = json.loads(snap_data["indicator_data"]) if snap_data["indicator_data"] else {}
            if not ind_data:
                return None
            ind_data["ticker"] = snap_data["ticker"]
            for k in ("news_sentiment", "news_count", "pe_ratio", "market_cap_b"):
                if snap_data.get(k) is not None:
                    ind_data[k] = snap_data[k]
            features = extract_features(
                ind_data,
                close_price=snap_data["close_price"],
                vix=snap_data.get("vix_at_snapshot"),
                regime=None,
            )
            x = [features.get(f, 0.0) for f in FEATURE_NAMES]
            y = 1 if (snap_data.get("future_return_5d") or 0) > 0 else 0
            return (x, y)
        except Exception:
            return None

    snap_dicts = [
        {
            "indicator_data": s.indicator_data,
            "ticker": s.ticker,
            "close_price": s.close_price,
            "future_return_5d": s.future_return_5d,
            "news_sentiment": getattr(s, "news_sentiment", None),
            "news_count": getattr(s, "news_count", None),
            "pe_ratio": getattr(s, "pe_ratio", None),
            "market_cap_b": getattr(s, "market_cap_b", None),
            "vix_at_snapshot": getattr(s, "vix_at_snapshot", None),
        }
        for s in snaps
    ]

    from concurrent.futures import ThreadPoolExecutor
    X_rows = []
    y_rows = []
    _n_workers = min(max(4, (os.cpu_count() or 4)), len(snap_dicts) // 50 + 1)
    with ThreadPoolExecutor(max_workers=_n_workers) as pool:
        for result in pool.map(_extract_one, snap_dicts):
            if result is not None:
                X_rows.append(result[0])
                y_rows.append(result[1])

    if len(X_rows) < _MIN_SAMPLES:
        return {
            "ok": False,
            "reason": "Not enough usable samples after filtering",
            "labeled_snapshots": len(snaps),
            "usable_samples": len(X_rows),
            "needed": _MIN_SAMPLES,
            "tip": "Some snapshots had missing indicator data. Run more scans to collect better data.",
        }

    X = np.array(X_rows)
    y = np.array(y_rows)

    clf = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        subsample=0.8,
        min_samples_leaf=5,
        random_state=42,
    )

    cv_scores = cross_val_score(clf, X, y, cv=min(5, max(2, len(X) // 20)), scoring="accuracy", n_jobs=-1)

    clf.fit(X, y)

    y_pred = clf.predict(X)
    train_acc = round(accuracy_score(y, y_pred) * 100, 1)
    cv_acc = round(cv_scores.mean() * 100, 1)
    precision = round(precision_score(y, y_pred, zero_division=0) * 100, 1)
    recall = round(recall_score(y, y_pred, zero_division=0) * 100, 1)

    importances = {
        FEATURE_NAMES[i]: round(float(clf.feature_importances_[i]), 4)
        for i in range(len(FEATURE_NAMES))
    }
    importances = dict(sorted(importances.items(), key=lambda x: x[1], reverse=True))

    with _model_lock:
        _model = clf
        _model_stats = {
            "trained_at": datetime.utcnow().isoformat(),
            "samples": len(X),
            "positive_rate": round(y.mean() * 100, 1),
            "train_accuracy": train_acc,
            "cv_accuracy": cv_acc,
            "precision": precision,
            "recall": recall,
            "feature_importances": importances,
        }

    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_MODEL_PATH, "wb") as f:
            pickle.dump({"model": clf, "stats": _model_stats, "features": FEATURE_NAMES}, f)
        logger.info(f"[ml_engine] Model saved to {_MODEL_PATH}")
    except Exception as e:
        logger.warning(f"[ml_engine] Could not save model: {e}")

    logger.info(
        f"[ml_engine] Trained on {len(X)} samples: "
        f"CV accuracy={cv_acc}%, precision={precision}%, recall={recall}%"
    )

    return {"ok": True, **_model_stats}


def load_model() -> bool:
    """Load persisted model from disk."""
    global _model, _model_stats
    if not _MODEL_PATH.exists():
        return False
    try:
        with open(_MODEL_PATH, "rb") as f:
            data = pickle.load(f)
        with _model_lock:
            _model = data["model"]
            _model_stats = data.get("stats", {})
        logger.info(f"[ml_engine] Model loaded ({_model_stats.get('samples', '?')} samples)")
        return True
    except Exception as e:
        logger.warning(f"[ml_engine] Could not load model: {e}")
        return False


def predict_ml(features: dict[str, float]) -> float | None:
    """Return probability (0-1) that the ticker goes up in 5 days.

    Returns None if no model is available.
    """
    with _model_lock:
        model = _model
    if model is None:
        return None
    try:
        X = _features_to_array(features)
        prob = float(model.predict_proba(X)[0][1])
        return round(prob, 4)
    except Exception:
        return None


def get_model_stats() -> dict[str, Any]:
    """Return current model stats for the dashboard."""
    return dict(_model_stats) if _model_stats else {
        "trained_at": None,
        "samples": 0,
        "cv_accuracy": 0,
    }


def is_model_ready() -> bool:
    with _model_lock:
        return _model is not None
