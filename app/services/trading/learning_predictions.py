"""Live prediction pipeline: ticker universe, per-ticker scoring, SWR cache, mirror hooks.

Extracted from ``learning.py`` to isolate ``get_current_predictions`` and promoted-cache
paths. SWR cache and scheduler entrypoints remain on ``learning``; this module holds the core impl.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import ScanResult
from .market_data import (
    DEFAULT_CRYPTO_TICKERS,
    DEFAULT_SCAN_TICKERS,
    _use_massive,
    _use_polygon,
    fetch_quote,
    fetch_quotes_batch,
    get_indicator_snapshot,
    get_vix,
    get_volatility_regime,
)
from .portfolio import get_watchlist

logger = logging.getLogger(__name__)

_CPU_COUNT = os.cpu_count() or 4
_IO_WORKERS_HIGH = min(80, max(24, _CPU_COUNT * 3))
_IO_WORKERS_MED = min(48, max(16, _CPU_COUNT * 2))

def predict_direction(score: float) -> str:
    """Convert prediction score to a human-readable direction."""
    if score >= 3.0:
        return "bullish"
    elif score >= 1.0:
        return "slightly_bullish"
    elif score <= -3.0:
        return "bearish"
    elif score <= -1.0:
        return "slightly_bearish"
    return "neutral"


def predict_confidence(score: float) -> int:
    """Convert absolute prediction score to a confidence percentage (0-100)."""
    return min(100, int(abs(score) * 10))


def _build_prediction_tickers(db: Session, explicit: list[str] | None) -> list[str]:
    """Build a diverse ticker list for predictions from multiple sources."""
    if explicit:
        return explicit

    seen: set[str] = set()
    result: list[str] = []

    def _add(t: str):
        u = t.upper()
        if u not in seen:
            seen.add(u)
            result.append(u)

    recent_scans = (
        db.query(ScanResult.ticker, ScanResult.score)
        .order_by(ScanResult.scanned_at.desc())
        .limit(500)
        .all()
    )
    top_scanned = sorted(set((r.ticker, r.score) for r in recent_scans), key=lambda x: x[1], reverse=True)
    for ticker, _ in top_scanned[:60]:
        _add(ticker)

    try:
        from .prescreener import get_trending_crypto
        for t in get_trending_crypto()[:30]:
            _add(t)
    except Exception:
        pass

    try:
        from ..ticker_universe import get_all_crypto_tickers
        for t in get_all_crypto_tickers(n=120)[:40]:
            _add(t)
    except Exception:
        for t in DEFAULT_CRYPTO_TICKERS[:20]:
            _add(t)

    try:
        wl_items = get_watchlist(db, user_id=None)
        for item in wl_items[:20]:
            _add(item.ticker)
    except Exception:
        pass

    if len(result) < 40:
        for t in DEFAULT_SCAN_TICKERS[:30]:
            _add(t)
        for t in DEFAULT_CRYPTO_TICKERS[:15]:
            _add(t)

    return result


def _indicator_data_to_flat_snapshot(
    ind_data: dict[str, Any], price: float | None,
) -> dict[str, Any]:
    """Convert nested ``get_indicator_snapshot()`` output to flat dict for ``evaluate_patterns()``."""
    snap: dict[str, Any] = {}
    if price is not None:
        snap["price"] = price

    rsi_val = (ind_data.get("rsi") or {}).get("value")
    if rsi_val is not None:
        snap["rsi_14"] = rsi_val

    for ema_key in ("ema_20", "ema_50", "ema_100"):
        v = (ind_data.get(ema_key) or {}).get("value")
        if v is not None:
            snap[ema_key] = v

    sma20 = (ind_data.get("sma_20") or {}).get("value")
    if sma20 is not None:
        snap["sma_20"] = sma20

    macd_hist = (ind_data.get("macd") or {}).get("histogram")
    if macd_hist is not None:
        snap["macd_hist"] = macd_hist

    adx_val = (ind_data.get("adx") or {}).get("adx")
    if adx_val is not None:
        snap["adx"] = adx_val

    atr_val = (ind_data.get("atr") or {}).get("value")
    if atr_val is not None:
        snap["atr"] = atr_val

    bb = ind_data.get("bbands") or {}
    bb_upper = bb.get("upper")
    bb_lower = bb.get("lower")
    if bb_upper and bb_lower and bb_upper > bb_lower:
        bandwidth = (bb_upper - bb_lower) / ((bb_upper + bb_lower) / 2)
        snap["bb_squeeze"] = bandwidth < 0.04

    obv = (ind_data.get("obv") or {}).get("value")
    if obv is not None:
        snap["obv"] = obv

    stoch_k = (ind_data.get("stoch") or {}).get("k")
    if stoch_k is not None:
        snap["stoch_k"] = stoch_k

    rsi7 = (ind_data.get("rsi_7") or {}).get("value")
    if rsi7 is not None:
        snap["rsi_7"] = rsi7

    vz = (ind_data.get("volume_z_20") or {}).get("value")
    if vz is not None:
        snap["vol_z_20"] = vz

    rv = (ind_data.get("realized_vol_20") or {}).get("value")
    if rv is not None:
        snap["realized_vol_20"] = rv

    roc10 = (ind_data.get("roc_10") or {}).get("value")
    if roc10 is not None:
        snap["roc_10"] = roc10

    bb_pb = (ind_data.get("bb_pct_b") or {}).get("value")
    if bb_pb is not None:
        snap["bb_pct_b"] = bb_pb

    atrp = (ind_data.get("atr_percentile_60") or {}).get("value")
    if atrp is not None:
        snap["atr_percentile_60"] = atrp

    eq = ind_data.get("equity_regime")
    if isinstance(eq, dict) and eq.get("regime"):
        snap["regime"] = eq.get("regime")

    lv1 = ind_data.get("learned_v1")
    if isinstance(lv1, dict) and lv1.get("schema_version") == 1:
        snap["learned_v1_skew"] = lv1.get("return_skew_20")
        snap["learned_v1_range_pct"] = lv1.get("range_pct_20d")

    return snap


def _explain_prediction(
    matched_patterns: list[dict] | None,
    score: float,
) -> list[str]:
    """Generate human-readable explanations from matched patterns."""
    reasons: list[str] = []

    if matched_patterns:
        for mp in matched_patterns:
            wr = mp.get("win_rate")
            met = mp.get("conditions_met")
            total = mp.get("conditions_total")
            strength = mp.get("avg_strength")
            label = mp["name"]
            parts: list[str] = []
            if wr is not None:
                parts.append(f"{wr}% WR")
            if met is not None and total is not None and met < total:
                parts.append(f"{met}/{total} conditions")
            if strength is not None and strength < 1.0:
                parts.append(f"{round(strength * 100)}% strength")
            if parts:
                label += f" ({', '.join(parts)})"
            reasons.append(f"Pattern: {label}")

    if not reasons:
        if abs(score) < 0.5:
            reasons.append("No active patterns matched — neutral")
        else:
            reasons.append("Weak pattern signals")

    return reasons


def _predict_single_ticker(
    ticker: str,
    quotes_map: dict[str, dict],
    vix: float | None,
    vol_regime: dict,
    meta_learner_ready: bool,
    meta_predict_fn,
    active_patterns: list | None = None,
) -> dict | None:
    """Predict a single ticker using the pattern-driven ML brain.

    Scoring tiers (graceful degradation):
      1. Meta-learner trained  -> probability from pattern feature model
      2. Patterns exist, no ML -> weighted soft-match fallback
      3. No patterns            -> neutral (score=0)
    """
    from .pattern_engine import evaluate_patterns_with_strength
    from .pattern_ml import extract_pattern_features

    try:
        snapshot = get_indicator_snapshot(ticker)
        if not snapshot or len(snapshot) < 3:
            return None
        ind_data = {k: v for k, v in snapshot.items() if k not in ("ticker", "interval")}

        quote = quotes_map.get(ticker)
        if not quote:
            quote = fetch_quote(ticker)
        price = quote["price"] if quote else None

        if not active_patterns:
            return None

        from .backtest_engine import TICKER_TO_SECTOR as _T2S
        _ticker_sector = _T2S.get(ticker)
        applicable_patterns = []
        for _pat in active_patterns:
            _scope = getattr(_pat, "ticker_scope", "universal") or "universal"
            if _scope == "universal":
                applicable_patterns.append(_pat)
            elif _scope == "ticker_specific":
                try:
                    _st = json.loads(getattr(_pat, "scope_tickers", None) or "[]")
                except (json.JSONDecodeError, TypeError):
                    _st = []
                if ticker in _st:
                    applicable_patterns.append(_pat)
            elif _scope == "sector":
                if _ticker_sector:
                    try:
                        _ss = json.loads(getattr(_pat, "scope_tickers", None) or "[]")
                    except (json.JSONDecodeError, TypeError):
                        _ss = []
                    if _ticker_sector in _ss:
                        applicable_patterns.append(_pat)
                else:
                    applicable_patterns.append(_pat)
            else:
                applicable_patterns.append(_pat)

        if not applicable_patterns:
            return None

        flat_snap = _indicator_data_to_flat_snapshot(ind_data, price) if price else {}
        if not flat_snap:
            return None

        matches = evaluate_patterns_with_strength(flat_snap, applicable_patterns)
        matched_patterns: list[dict] = []
        for m in matches:
            raw_wr = m.get("win_rate")
            wr_pct = round(raw_wr) if raw_wr is not None and raw_wr > 1 else (round(raw_wr * 100) if raw_wr else None)
            matched_patterns.append({
                "name": m["name"],
                "win_rate": wr_pct,
                "pattern_id": m.get("pattern_id"),
                "match_quality": m.get("match_quality"),
                "conditions_met": m.get("conditions_met"),
                "conditions_total": m.get("conditions_total"),
                "avg_strength": m.get("avg_strength"),
            })

        meta_prob = None
        if meta_learner_ready:
            pat_features = extract_pattern_features(active_patterns, flat_snap)
            meta_prob = meta_predict_fn(pat_features)

        if meta_prob is not None:
            blended_score = round((meta_prob - 0.5) * 20, 2)
        elif matched_patterns:
            pattern_score = 0.0
            for m in matches:
                raw_wr = m.get("win_rate") or 0.5
                wr = raw_wr / 100.0 if raw_wr > 1 else raw_wr
                quality = m.get("match_quality", 1.0)
                strength = m.get("avg_strength", 0.5)
                contrib = m.get("score_boost", 1.0) * max(0.5, wr) * quality * max(0.3, strength)
                pattern_score += contrib
            blended_score = max(-10.0, min(10.0, round(pattern_score, 2)))
        else:
            blended_score = 0.0

        regime = vol_regime.get("regime", "normal")
        if regime == "extreme":
            blended_score *= 0.6
        elif regime == "elevated":
            if abs(blended_score) < 3:
                blended_score *= 0.8

        blended_score = max(-10.0, min(10.0, round(blended_score, 2)))
        direction = predict_direction(blended_score)
        confidence = predict_confidence(blended_score)

        atr_val = (ind_data.get("atr") or {}).get("value")
        _cr = ticker.upper().endswith("-USD")
        _rd = 8 if _cr else 6
        stop = target = rr = pos_size_pct = None
        _vol_pct = (atr_val / price * 100) if price and atr_val else 0
        _stop_mult = 2.5 if _vol_pct > 3 else 2.0
        if price and atr_val and atr_val > 0:
            if blended_score > 0:
                stop = round(price - atr_val * _stop_mult, _rd)
                target = round(price + atr_val * 3.0, _rd)
            elif blended_score < 0:
                stop = round(price + atr_val * _stop_mult, _rd)
                target = round(price - atr_val * 3.0, _rd)
            if stop is not None and target is not None:
                risk = abs(price - stop)
                reward = abs(target - price)
                rr = round(reward / risk, 2) if risk > 0 else 0
                pos_size_pct = round(min(5.0, 1.0 / (risk / price * 100)) * 100 / 100, 2) if price > 0 else None

        return {
            "ticker": ticker,
            "price": price,
            "score": blended_score,
            "meta_ml_probability": round(meta_prob, 4) if meta_prob is not None else None,
            "direction": direction,
            "confidence": confidence,
            "signals": _explain_prediction(matched_patterns, blended_score),
            "matched_patterns": matched_patterns or [],
            "vix_regime": regime,
            "suggested_stop": stop,
            "suggested_target": target,
            "risk_reward": rr,
            "position_size_pct": pos_size_pct,
        }
    except Exception:
        return None


def _get_current_predictions_impl(
    db: Session,
    tickers: list[str] | None,
    *,
    explicit_api_tickers: bool = False,
    active_patterns_override: list | None = None,
    max_ticker_batch: int = 400,
) -> list[dict]:
    """Core prediction logic (no cache).  Pattern-driven ML pipeline.

    ``active_patterns_override``: when set, use instead of ``get_active_patterns(db)``.
    ``max_ticker_batch``: cap after universe build (fast eval may use a lower cap).
    """
    from .pattern_engine import get_active_patterns
    from .pattern_ml import get_meta_learner

    if tickers is None or not tickers:
        explicit_api_tickers = False

    tickers = _build_prediction_tickers(db, tickers)
    _cap = max(1, min(int(max_ticker_batch), 800))
    ticker_batch = tickers[:_cap]

    vix = get_vix()
    vol_regime = get_volatility_regime(vix)

    meta = get_meta_learner()
    meta_ready = meta.is_ready()

    if active_patterns_override is not None:
        _active_patterns = list(active_patterns_override)
    else:
        try:
            _active_patterns = get_active_patterns(db)
        except Exception:
            _active_patterns = []

    if not _active_patterns:
        return []

    quotes_map = fetch_quotes_batch(ticker_batch)

    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    results = []
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futures = {
            pool.submit(
                _predict_single_ticker,
                t, quotes_map, vix, vol_regime,
                meta_ready, meta.predict,
                _active_patterns,
            ): t
            for t in ticker_batch
        }
        for fut in as_completed(futures):
            entry = fut.result()
            if entry is not None:
                results.append(entry)

    results.sort(key=lambda x: abs(x["score"]), reverse=True)

    from ...config import settings as _pred_mirror_settings
    from ...trading_brain.infrastructure.prediction_ops_log import (
        DUAL_WRITE_FAIL,
        DUAL_WRITE_NA,
        DUAL_WRITE_OK,
        DUAL_WRITE_SKIP_EMPTY,
        READ_ERROR,
        READ_NA,
        format_chili_prediction_ops_line,
        universe_fingerprint_fp16,
    )

    dual_write_outcome = DUAL_WRITE_NA
    dw_enabled = False
    try:
        dw_enabled = bool(getattr(_pred_mirror_settings, "brain_prediction_dual_write_enabled", False))
        if not dw_enabled:
            pass
        elif not results:
            dual_write_outcome = DUAL_WRITE_SKIP_EMPTY
        else:
            from ...trading_brain.infrastructure.prediction_line_mapper import (
                prediction_universe_fingerprint,
            )
            from ...trading_brain.infrastructure.prediction_mirror_session import (
                brain_prediction_mirror_write_dedicated,
            )

            _fp = prediction_universe_fingerprint(ticker_batch)
            brain_prediction_mirror_write_dedicated(
                legacy_rows=results,
                universe_fingerprint=_fp,
                ticker_count=len(ticker_batch),
            )
            dual_write_outcome = DUAL_WRITE_OK
    except Exception:
        logger.warning("[brain_prediction_dual_write] hook failed (legacy return preserved)", exc_info=True)
        if dw_enabled and results:
            dual_write_outcome = DUAL_WRITE_FAIL

    _fp16 = universe_fingerprint_fp16(ticker_batch)
    from ...trading_brain.infrastructure.prediction_read_phase5 import (
        PredictionReadOpsMeta,
        phase5_apply_prediction_read,
    )

    read_meta = PredictionReadOpsMeta(read=READ_NA, fp16=_fp16)
    try:
        results, read_meta = phase5_apply_prediction_read(
            results=results,
            ticker_batch=ticker_batch,
            explicit_api_tickers=explicit_api_tickers,
        )
    except Exception:
        logger.warning("[brain_prediction_read] hook_failed legacy preserved", exc_info=True)
        read_meta = PredictionReadOpsMeta(read=READ_ERROR, fp16=_fp16)

    if getattr(_pred_mirror_settings, "brain_prediction_ops_log_enabled", False):
        logging.getLogger("app.services.trading.learning").info(
            format_chili_prediction_ops_line(
                dual_write=dual_write_outcome,
                read=read_meta.read,
                explicit_api_tickers=explicit_api_tickers,
                fp16=read_meta.fp16,
                snapshot_id=read_meta.snapshot_id,
                line_count=read_meta.line_count,
            )
        )

    return results

