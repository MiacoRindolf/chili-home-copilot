"""Imminent breakout alerts for active ScanPatterns.

Uses partial condition strength (0–1) from ``pattern_ml.compute_condition_strength`` to
estimate how close a ticker is to satisfying a pattern's rules, then dispatches an alert
when the heuristic ETA falls within a configurable window. ETA is **not** a guaranteed
time-to-breakout — see inline comments.

Stock patterns are only evaluated during regular US equity hours; crypto patterns run 24/7.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from ...config import settings
from ...models.trading import AlertHistory, ScanPattern
from .alerts import PATTERN_BREAKOUT_IMMINENT, dispatch_alert
from .market_data import DEFAULT_CRYPTO_TICKERS, DEFAULT_SCAN_TICKERS, fetch_ohlcv_df, is_crypto
from .pattern_engine import _condition_has_data, _eval_condition
from .pattern_ml import compute_condition_strength
from .portfolio import get_watchlist
from .scanner import _estimate_hold_duration, _score_ticker, classify_trade_type

logger = logging.getLogger(__name__)

_HOURS_PER_BAR = {
    "1m": 1 / 60,
    "5m": 5 / 60,
    "15m": 0.25,
    "30m": 0.5,
    "1h": 1.0,
    "4h": 4.0,
    "1d": 6.5,
    "1wk": 32.5,
}


def us_stock_session_open(now_utc: datetime | None = None) -> bool:
    """True during Mon–Fri US/Eastern 9:30–16:00 (inclusive start, exclusive end at 16:00)."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(ZoneInfo("US/Eastern"))
    if et.weekday() >= 5:
        return False
    t = et.time()
    open_t = time(9, 30)
    close_t = time(16, 0)
    return open_t <= t < close_t


def timeframe_to_hours_per_step(timeframe: str | None) -> float:
    tf = (timeframe or "1d").strip().lower()
    return _HOURS_PER_BAR.get(tf, 6.5)


def estimate_breakout_eta_hours(
    readiness: float,
    timeframe: str | None,
    *,
    k: float,
    max_eta_hours: float,
) -> tuple[float, float]:
    """Return ``(eta_low_hours, eta_high_hours)`` from readiness gap and pattern timeframe.

    Uses ``gap = 1 - readiness`` scaled by bars-per-timeframe; result is clamped to
    ``[5min, max_eta_hours]`` and widened to a 0.5×–1.5× band for messaging honesty.
    """
    h_step = timeframe_to_hours_per_step(timeframe)
    gap = max(0.0, min(1.0, 1.0 - readiness))
    center = k * gap * h_step
    min_h = 5 / 60
    center = max(min_h, min(max_eta_hours, center))
    low = max(min_h, center * 0.5)
    high = min(max_eta_hours, center * 1.5)
    if high < low:
        low, high = high, low
    return low, high


def format_eta_range(low_h: float, high_h: float) -> str:
    if high_h < 1:
        lo_m = max(1, int(low_h * 60))
        hi_m = max(lo_m, int(high_h * 60))
        return f"~{lo_m}–{hi_m} min" if lo_m != hi_m else f"~{lo_m} min"
    if high_h < 24:
        return f"~{low_h:.1f}–{high_h:.1f} hours"
    d_lo = low_h / 24
    d_hi = high_h / 24
    return f"~{d_lo:.1f}–{d_hi:.1f} days"


def recent_swing_resistance(ticker: str) -> float | None:
    """Proxy resistance: max high over last 20 daily bars (best-effort)."""
    try:
        df = fetch_ohlcv_df(ticker, period="3mo", interval="1d")
        if df is None or df.empty or "High" not in df.columns:
            return None
        hi = df["High"].tail(20)
        v = float(hi.max())
        return v if v > 0 else None
    except Exception:
        return None


def flat_indicators_from_score(
    score: dict[str, Any],
    *,
    resistance: float | None,
) -> dict[str, Any]:
    """Map swing ``_score_ticker`` payload to flat keys used in ``rules_json`` conditions."""
    price = float(score.get("price") or score.get("entry_price") or 0)
    ind = score.get("indicators") or {}
    flat: dict[str, Any] = {"price": price}

    rsi = ind.get("rsi")
    if rsi is not None:
        flat["rsi_14"] = float(rsi)

    for key in ("macd_hist", "adx", "atr", "ema_20", "ema_50", "ema_100", "stoch_k"):
        v = ind.get(key)
        if v is not None:
            flat[key] = float(v)

    vr = ind.get("vol_ratio")
    if vr is not None:
        flat["rel_vol"] = float(vr)

    bb_pct = ind.get("bb_pct")
    if bb_pct is not None:
        flat["bb_pct"] = float(bb_pct)

    if resistance and price > 0:
        flat["resistance"] = float(resistance)
        flat["dist_to_resistance_pct"] = round((resistance - price) / price * 100, 4)

    return flat


def _parse_scope_tickers(pat: ScanPattern) -> list[str]:
    raw = getattr(pat, "scope_tickers", None) or ""
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip().upper() for x in data if x]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _build_global_ticker_universe(db: Session, user_id: int | None, cap: int) -> list[str]:
    seen: list[str] = []
    have: set[str] = set()

    def add(t: str) -> None:
        t = (t or "").strip().upper()
        if not t or t in have:
            return
        have.add(t)
        seen.append(t)

    try:
        for w in get_watchlist(db, user_id):
            add(getattr(w, "ticker", "") or "")
    except Exception:
        pass

    for t in DEFAULT_SCAN_TICKERS[:35]:
        add(t)
    for t in DEFAULT_CRYPTO_TICKERS[:20]:
        add(t)

    return seen[:cap]


def _tickers_for_pattern(
    pat: ScanPattern,
    global_universe: list[str],
    *,
    equity_open: bool,
) -> list[str]:
    scope = (getattr(pat, "ticker_scope", None) or "universal").strip().lower()
    ac = (getattr(pat, "asset_class", None) or "all").strip().lower()

    if scope == "ticker_specific":
        scoped = _parse_scope_tickers(pat)
        cap = max(1, int(settings.pattern_imminent_scope_tickers_cap))
        scoped = scoped[:cap]
        if not scoped:
            return []
        tickers = scoped
    else:
        tickers = list(global_universe)

    out: list[str] = []
    for t in tickers:
        cr = is_crypto(t)
        if ac == "crypto" and not cr:
            continue
        if ac == "stocks" and cr:
            continue
        if ac == "stocks" and not equity_open:
            continue
        if ac == "all":
            if not cr and not equity_open:
                continue
        out.append(t)
    return out


def _pattern_conditions(pat: ScanPattern) -> list[dict[str, Any]]:
    try:
        rules = json.loads(pat.rules_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    conds = rules.get("conditions") or []
    return conds if isinstance(conds, list) else []


def evaluate_imminent_readiness(
    conditions: list[dict[str, Any]],
    flat: dict[str, Any],
    *,
    evaluable_ratio_floor: float,
    min_evaluable_for_wide_patterns: int = 2,
) -> tuple[float | None, bool, float]:
    """Return ``(readiness, all_strict_pass, evaluable_ratio)`` or ``(None, …)`` if unusable.

    Accepts either enough *coverage* (ratio >= floor) or at least two evaluable clauses
    (typical brain patterns with many boolean/missing fields).
    """
    if not conditions:
        return None, False, 0.0

    evaluable = [c for c in conditions if _condition_has_data(c, flat)]
    ratio = len(evaluable) / len(conditions)
    if not evaluable:
        return None, False, ratio
    if ratio < evaluable_ratio_floor and len(evaluable) < min_evaluable_for_wide_patterns:
        return None, False, ratio

    strengths = [compute_condition_strength(c, flat) for c in evaluable]
    readiness = sum(strengths) / len(strengths) if strengths else 0.0
    all_pass = all(_eval_condition(c, flat) for c in evaluable)
    return readiness, all_pass, ratio


def _cooldown_active(
    db: Session,
    user_id: int | None,
    ticker: str,
    pattern_id: int,
    hours: float,
) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    q = (
        db.query(AlertHistory)
        .filter(
            AlertHistory.alert_type == PATTERN_BREAKOUT_IMMINENT,
            AlertHistory.ticker == ticker,
            AlertHistory.created_at >= cutoff,
        )
    )
    if user_id is not None:
        q = q.filter(AlertHistory.user_id == user_id)
    rows = q.order_by(AlertHistory.created_at.desc()).limit(25).all()
    for r in rows:
        spid = getattr(r, "scan_pattern_id", None)
        if spid is not None and int(spid) == int(pattern_id):
            return True
    return False


def run_pattern_imminent_scan(
    db: Session,
    user_id: int | None,
    *,
    equity_session_open: bool | None = None,
) -> dict[str, Any]:
    """Evaluate active patterns against a capped universe; dispatch imminent alerts."""
    if not getattr(settings, "pattern_imminent_alert_enabled", True):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    eq_open = equity_session_open if equity_session_open is not None else us_stock_session_open()

    max_eta = float(settings.pattern_imminent_max_eta_hours)
    min_rd = float(settings.pattern_imminent_min_readiness)
    cap_rd = float(settings.pattern_imminent_readiness_cap)
    max_alerts = int(settings.pattern_imminent_max_per_run)
    max_tickers = int(settings.pattern_imminent_max_tickers_per_run)
    cooldown_h = float(settings.pattern_imminent_cooldown_hours)
    eval_floor = float(settings.pattern_imminent_evaluable_ratio_floor)
    k_eta = float(settings.pattern_imminent_eta_scale_k)

    patterns = db.query(ScanPattern).filter(ScanPattern.active.is_(True)).all()
    global_uni = _build_global_ticker_universe(db, user_id, max_tickers)

    candidates: list[dict[str, Any]] = []
    patterns_tried = 0
    tickers_scored = 0
    skip: dict[str, int] = {
        "pattern_no_tickers": 0,
        "pattern_no_conditions": 0,
        "score_failed": 0,
        "readiness_unusable": 0,
        "all_conditions_met": 0,
        "readiness_outside_band": 0,
        "eta_too_long": 0,
    }

    for pat in patterns:
        tickers = _tickers_for_pattern(pat, global_uni, equity_open=eq_open)
        if not tickers:
            skip["pattern_no_tickers"] += 1
            continue

        conditions = _pattern_conditions(pat)
        if not conditions:
            skip["pattern_no_conditions"] += 1
            continue

        patterns_tried += 1
        for ticker in tickers:
            score = _score_ticker(ticker, skip_fundamentals=True)
            if not score:
                skip["score_failed"] += 1
                continue
            tickers_scored += 1

            res = recent_swing_resistance(ticker)
            flat = flat_indicators_from_score(score, resistance=res)
            readiness, all_pass, _ratio = evaluate_imminent_readiness(
                conditions, flat, evaluable_ratio_floor=eval_floor,
            )
            if readiness is None:
                skip["readiness_unusable"] += 1
                continue
            if all_pass:
                skip["all_conditions_met"] += 1
                continue
            if readiness < min_rd or readiness >= cap_rd:
                skip["readiness_outside_band"] += 1
                continue

            eta_lo, eta_hi = estimate_breakout_eta_hours(
                readiness, pat.timeframe, k=k_eta, max_eta_hours=max_eta,
            )
            if eta_hi > max_eta:
                skip["eta_too_long"] += 1
                continue

            entry = score.get("entry_price") or score.get("price")
            stop = score.get("stop_loss")
            target = score.get("take_profit")
            price = score.get("price")
            atr_f = (flat.get("atr") or 0) or 0
            adx_f = flat.get("adx")
            try:
                entry_f = float(entry or 0)
                tgt_f = float(target or 0)
                atr_use = float(atr_f) if atr_f else (entry_f * 0.02 if entry_f else 0.01)
                hold_est = _estimate_hold_duration(
                    entry_f, tgt_f, atr_use,
                    (pat.timeframe or "1d"), adx_f,
                )
            except (TypeError, ValueError):
                hold_est = {"label": "n/a", "hours_low": 0, "hours_high": 0}

            ind_for_class = {
                "adx": adx_f,
                "atr": atr_f,
                "rsi": flat.get("rsi_14"),
            }
            tc = classify_trade_type(
                score.get("signals") or [],
                hold_est,
                ind_for_class,
                is_crypto=is_crypto(ticker),
            )

            candidates.append({
                "pattern": pat,
                "ticker": ticker,
                "readiness": readiness,
                "eta_lo": eta_lo,
                "eta_hi": eta_hi,
                "score": score,
                "hold_label": hold_est.get("label") or "",
                "trade_type": tc.get("type"),
                "duration_estimate": tc.get("duration") or hold_est.get("label"),
            })

    candidates.sort(key=lambda x: (x["eta_hi"], -x["readiness"]))

    sent = 0
    skipped_cd = 0
    for c in candidates:
        if sent >= max_alerts:
            break
        pat = c["pattern"]
        ticker = c["ticker"]
        if _cooldown_active(db, user_id, ticker, pat.id, cooldown_h):
            skipped_cd += 1
            continue

        eta_txt = format_eta_range(c["eta_lo"], c["eta_hi"])
        sc = c["score"]
        desc = (pat.description or "")[:120].replace("\n", " ")
        hold_line = c.get("duration_estimate") or c.get("hold_label") or ""
        sigs = "; ".join((sc.get("signals") or [])[:4])

        msg = (
            f"IMMINENT PATTERN: {pat.name} (#{pat.id})\n"
            f"{ticker} @ ${sc.get('price')} | readiness {c['readiness']:.0%}\n"
            f"Breakout ETA: {eta_txt}\n"
            f"Hold after entry: {hold_line}\n"
            f"Entry ${sc.get('entry_price')} | Stop ${sc.get('stop_loss')} | "
            f"Target ${sc.get('take_profit')}\n"
            + (f"{desc}\n" if desc else "")
            + (f"{sigs}" if sigs else "")
        )

        dispatch_alert(
            db,
            user_id,
            PATTERN_BREAKOUT_IMMINENT,
            ticker,
            msg,
            price=sc.get("price"),
            trade_type=c.get("trade_type"),
            duration_estimate=hold_line[:60] if hold_line else None,
            scan_pattern_id=pat.id,
        )
        sent += 1

    summary = {
        "ok": True,
        "patterns_active": len(patterns),
        "patterns_with_tickers": patterns_tried,
        "global_ticker_universe": len(global_uni),
        "tickers_scored": tickers_scored,
        "candidates": len(candidates),
        "alerts_sent": sent,
        "cooldown_skipped": skipped_cd,
        "equity_session_open": eq_open,
        "skip_reasons": skip,
        "thresholds": {
            "min_readiness": min_rd,
            "readiness_cap": cap_rd,
            "max_eta_hours": max_eta,
            "evaluable_ratio_floor": eval_floor,
        },
    }
    logger.info("[pattern_imminent] %s", summary)
    return summary
