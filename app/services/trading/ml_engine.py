"""DEPRECATED — Legacy ML engine.

Replaced by ``pattern_ml.PatternMetaLearner`` which trains on pattern
features rather than raw indicators.  Functions here are kept as stubs
so existing imports don't break.
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
    """DEPRECATED — delegates to PatternMetaLearner."""
    logger.warning("[ml_engine] train_model is deprecated; use PatternMetaLearner.train()")
    from .pattern_ml import get_meta_learner
    return get_meta_learner().train(db)


def load_model() -> bool:
    """DEPRECATED — loads PatternMetaLearner instead."""
    from .pattern_ml import load_meta_learner
    return load_meta_learner()


def predict_ml(features: dict[str, float]) -> float | None:
    """DEPRECATED — returns None. Use PatternMetaLearner.predict()."""
    return None


def get_model_stats() -> dict[str, Any]:
    """DEPRECATED — returns PatternMetaLearner stats."""
    from .pattern_ml import get_meta_learner
    return get_meta_learner().get_stats()


def is_model_ready() -> bool:
    """DEPRECATED — checks PatternMetaLearner readiness."""
    from .pattern_ml import get_meta_learner
    return get_meta_learner().is_ready()
