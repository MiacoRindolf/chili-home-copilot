"""Imminent breakout alerts for ScanPatterns (promoted/live main channel by default).

Uses shared scoring in ``opportunity_scoring`` (composite = quality first, ETA secondary).
Stock patterns use US session gates; crypto runs 24/7. ETA is heuristic, not guaranteed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from ...config import settings
from ...models.trading import AlertHistory, BreakoutAlert, ScanPattern, ScanResult
from .alert_formatter import format_pattern_imminent
from .alerts import PATTERN_BREAKOUT_IMMINENT, dispatch_alert
from .market_data import DEFAULT_CRYPTO_TICKERS, DEFAULT_SCAN_TICKERS, fetch_ohlcv_df, is_crypto
from .opportunity_scoring import (
    compute_composite_score,
    eta_timeliness_score,
    evaluate_readiness_with_gates,
    feature_coverage_detail,
    overextension_penalty,
    pattern_quality_score,
    risk_reward_score,
    scan_pattern_eligible_main_imminent,
    parse_pattern_conditions,
)
from .pattern_engine import _condition_has_data, _eval_condition
from .pattern_ml import compute_condition_strength
from .portfolio import get_watchlist
from .prescreen_job import load_active_global_candidate_tickers
from .scanner import _estimate_hold_duration, _score_ticker, classify_trade_type
from .learning_predictions import _build_prediction_tickers

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


def us_stock_extended_session_open(now_utc: datetime | None = None) -> bool:
    """True during Mon–Fri US/Eastern 04:00–20:00 — pre + RTH + post.

    Robinhood's 24/5 window covers ~04:00–20:00 ET for most tickers (plus
    narrower overnight sessions for a subset). This helper is intentionally
    conservative: it keeps the AutoTrader away from weekends entirely and
    gates on standard extended hours so entries/exits can attempt fills
    outside RTH when ``chili_autotrader_allow_extended_hours`` is set. The
    adapter itself decides whether market-order vs limit-order is appropriate
    and surfaces rejection as a ``sell_fail`` / ``error`` without corrupting
    position state.
    """
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(ZoneInfo("US/Eastern"))
    if et.weekday() >= 5:
        return False
    t = et.time()
    return time(4, 0) <= t < time(20, 0)


def describe_us_session_context(now_utc: datetime | None = None) -> dict[str, Any]:
    """US equity session label for UI (premarket / regular / after_hours / closed)."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(ZoneInfo("US/Eastern"))
    if et.weekday() >= 5:
        return {
            "us_session": "closed",
            "label": "US stocks: weekend (closed)",
            "equity_evaluation_active": False,
        }
    t = et.time()
    pre_open = time(4, 0)
    reg_open = time(9, 30)
    reg_close = time(16, 0)
    post_close = time(20, 0)
    if t < pre_open or t >= post_close:
        return {
            "us_session": "closed",
            "label": "US stocks: session closed",
            "equity_evaluation_active": False,
        }
    if pre_open <= t < reg_open:
        return {
            "us_session": "premarket",
            "label": "US stocks: premarket",
            "equity_evaluation_active": False,
        }
    if reg_open <= t < reg_close:
        return {
            "us_session": "regular_hours",
            "label": "US stocks: regular session",
            "equity_evaluation_active": True,
        }
    return {
        "us_session": "after_hours",
        "label": "US stocks: after hours",
        "equity_evaluation_active": False,
    }


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


def evaluate_imminent_readiness(
    conditions: list[dict[str, Any]],
    flat: dict[str, Any],
    *,
    evaluable_ratio_floor: float,
    min_evaluable_for_wide_patterns: int = 2,
) -> tuple[float | None, bool, float]:
    """Backward-compatible readiness; delegates to shared gates."""
    readiness, all_pass, ratio, _miss = evaluate_readiness_with_gates(
        conditions,
        flat,
        min_coverage_ratio=evaluable_ratio_floor,
        min_evaluable_absolute=min_evaluable_for_wide_patterns,
        allow_shortcut_two_evaluable=True,
    )
    return readiness, all_pass, ratio


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


def build_imminent_ticker_universe(
    db: Session,
    user_id: int | None,
    cap: int,
) -> tuple[list[str], dict[str, int]]:
    """Merge watchlist, prescreen, predictions, scanner, defaults; dedupe; source counts."""
    seen: list[str] = []
    have: set[str] = set()
    counts: dict[str, int] = {}

    def add(t: str) -> None:
        t = (t or "").strip().upper()
        if not t or t in have:
            return
        have.add(t)
        seen.append(t)

    n0 = len(seen)
    try:
        for w in get_watchlist(db, user_id):
            add(getattr(w, "ticker", "") or "")
    except Exception:
        pass
    counts["watchlist"] = len(seen) - n0

    n1 = len(seen)
    if getattr(settings, "pattern_imminent_use_prescreener_universe", True):
        try:
            pre = load_active_global_candidate_tickers(db)
            m = max(0, int(getattr(settings, "pattern_imminent_max_prescreener_tickers", 80)))
            for t in pre[:m]:
                add(t)
        except Exception:
            pass
    counts["prescreener"] = len(seen) - n1

    n2 = len(seen)
    if getattr(settings, "pattern_imminent_use_predictions_universe", True):
        try:
            preds = _build_prediction_tickers(db, None)
            m = max(0, int(getattr(settings, "pattern_imminent_max_prediction_tickers", 40)))
            for t in preds[:m]:
                add(t)
        except Exception:
            pass
    counts["predictions"] = len(seen) - n2

    n3 = len(seen)
    if getattr(settings, "pattern_imminent_use_scanner_universe", True):
        try:
            m = max(0, int(getattr(settings, "pattern_imminent_max_scanner_tickers", 50)))
            q = db.query(ScanResult.ticker).order_by(desc(ScanResult.scanned_at)).limit(m)
            for (tk,) in q.all():
                add(tk or "")
        except Exception:
            pass
    counts["scanner"] = len(seen) - n3

    n4 = len(seen)
    for t in DEFAULT_SCAN_TICKERS[:35]:
        add(t)
    for t in DEFAULT_CRYPTO_TICKERS[:20]:
        add(t)
    counts["defaults"] = len(seen) - n4

    return seen[:cap], counts


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
    return parse_pattern_conditions(pat.rules_json)


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
            AlertHistory.success.is_(True),
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


def _insert_imminent_breakout_alert(
    db: Session,
    user_id: int | None,
    pat: ScanPattern,
    ticker: str,
    score: dict[str, Any],
    flat: dict[str, Any],
    *,
    composite: float,
    score_breakdown: dict[str, float],
    readiness: float,
    coverage_ratio: float,
    eta_lo: float,
    eta_hi: float,
) -> None:
    price = float(score.get("price") or 0)
    snap = {
        "flat_indicators": {k: v for k, v in flat.items() if v is not None},
        "imminent_scorecard": {
            "composite": composite,
            "breakdown": score_breakdown,
            "readiness": readiness,
            "feature_coverage": coverage_ratio,
            "eta_hours": [eta_lo, eta_hi],
            "lifecycle_stage": getattr(pat, "lifecycle_stage", None),
            "promotion_status": getattr(pat, "promotion_status", None),
        },
    }
    asset = "crypto" if is_crypto(ticker) else "stock"
    row = BreakoutAlert(
        ticker=ticker,
        asset_type=asset,
        alert_tier="pattern_imminent",
        score_at_alert=composite,
        indicator_snapshot=snap,
        price_at_alert=price,
        entry_price=score.get("entry_price"),
        stop_loss=score.get("stop_loss"),
        target_price=score.get("take_profit"),
        signals_snapshot={"signals": (score.get("signals") or [])[:12]},
        outcome="pending",
        user_id=user_id,
        scan_pattern_id=pat.id,
        timeframe=(pat.timeframe or "1d")[:10],
    )
    db.add(row)
    db.commit()


def gather_imminent_candidate_rows(
    db: Session,
    user_id: int | None,
    *,
    equity_session_open: bool | None = None,
    all_active_patterns: bool = False,
    apply_main_dispatch_filters: bool = False,
    for_opportunity_board: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Score (pattern × ticker) rows using shared composite math.

    *all_active_patterns*: when False, only promoted/live (or legacy promoted) patterns.
    *apply_main_dispatch_filters*: when True, enforce main Telegram coverage + composite floors.
    *for_opportunity_board*: when True, apply tighter universe/per-pattern/total score caps so
      the HTTP board stays within latency budgets. Does **not** change main imminent dispatch
      (call with ``for_opportunity_board=False`` there).
    """
    eq_open = equity_session_open if equity_session_open is not None else us_stock_session_open()
    max_eta = float(settings.pattern_imminent_max_eta_hours)
    min_rd = float(settings.pattern_imminent_min_readiness)
    cap_rd = float(settings.pattern_imminent_readiness_cap)
    max_tickers = int(settings.pattern_imminent_max_tickers_per_run)
    if for_opportunity_board:
        cap_u = int(getattr(settings, "opportunity_board_max_universe_cap", 80))
        max_tickers = max(1, min(max_tickers, cap_u))
    eval_floor_board = float(settings.pattern_imminent_evaluable_ratio_floor)
    min_cov_main = float(getattr(settings, "pattern_imminent_min_feature_coverage_main", 0.45))
    min_comp_main = float(getattr(settings, "pattern_imminent_min_composite_main", 0.42))
    allow_shortcut = bool(getattr(settings, "pattern_imminent_allow_evaluable_shortcut", True))
    k_eta = float(settings.pattern_imminent_eta_scale_k)

    patterns = db.query(ScanPattern).filter(ScanPattern.active.is_(True)).all()
    global_uni, uni_counts = build_imminent_ticker_universe(db, user_id, max_tickers)

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
        "excluded_promotion_lifecycle": 0,
        "insufficient_coverage_main": 0,
        "below_composite_main": 0,
    }
    suppressed: list[dict[str, Any]] = []

    per_pat_cap = 10**9
    score_budget = 10**9
    if for_opportunity_board:
        per_pat_cap = max(1, int(getattr(settings, "opportunity_board_max_tickers_per_pattern", 10)))
        score_budget = max(1, int(getattr(settings, "opportunity_board_max_ticker_scores_per_request", 360)))

    board_budget_hit = False

    for pat in patterns:
        if not all_active_patterns and not scan_pattern_eligible_main_imminent(pat):
            skip["excluded_promotion_lifecycle"] += 1
            continue

        tickers = _tickers_for_pattern(pat, global_uni, equity_open=eq_open)
        if not tickers:
            skip["pattern_no_tickers"] += 1
            continue

        conditions = _pattern_conditions(pat)
        if not conditions:
            skip["pattern_no_conditions"] += 1
            continue

        patterns_tried += 1
        if for_opportunity_board and len(tickers) > per_pat_cap:
            tickers = tickers[:per_pat_cap]
        for ticker in tickers:
            if for_opportunity_board and tickers_scored >= score_budget:
                board_budget_hit = True
                break
            score = _score_ticker(ticker, skip_fundamentals=True)
            if not score:
                skip["score_failed"] += 1
                continue
            tickers_scored += 1

            res = recent_swing_resistance(ticker)
            flat = flat_indicators_from_score(score, resistance=res)

            readiness, all_pass, ratio, missing = evaluate_readiness_with_gates(
                conditions,
                flat,
                min_coverage_ratio=eval_floor_board,
                min_evaluable_absolute=2,
                allow_shortcut_two_evaluable=allow_shortcut,
            )
            if readiness is None:
                skip["readiness_unusable"] += 1
                if len(suppressed) < 40:
                    suppressed.append({
                        "ticker": ticker,
                        "pattern_id": pat.id,
                        "reason": "readiness_unusable",
                        "coverage": ratio,
                        "missing_indicators": missing[:8],
                    })
                continue

            if apply_main_dispatch_filters and ratio < min_cov_main:
                skip["insufficient_coverage_main"] += 1
                if len(suppressed) < 40:
                    suppressed.append({
                        "ticker": ticker,
                        "pattern_id": pat.id,
                        "reason": "insufficient_coverage_main",
                        "coverage": ratio,
                        "missing_indicators": missing[:8],
                    })
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

            pq = pattern_quality_score(pat)
            entry = score.get("entry_price") or score.get("price")
            stop = score.get("stop_loss")
            target = score.get("take_profit")
            rr = risk_reward_score(
                float(entry) if entry else None,
                float(stop) if stop else None,
                float(target) if target else None,
            )
            oxp = overextension_penalty(flat)
            eta_s = eta_timeliness_score(eta_hi, max_eta)
            comp, breakdown = compute_composite_score(
                readiness=readiness,
                coverage_ratio=ratio,
                pattern_quality=pq,
                rr_score=rr,
                eta_score=eta_s,
                overext_subtract=oxp,
            )
            if apply_main_dispatch_filters and comp < min_comp_main:
                skip["below_composite_main"] += 1
                if len(suppressed) < 40:
                    suppressed.append({
                        "ticker": ticker,
                        "pattern_id": pat.id,
                        "reason": "below_composite_main",
                        "composite": round(comp, 4),
                        "coverage": ratio,
                    })
                continue

            atr_f = (flat.get("atr") or 0) or 0
            adx_f = flat.get("adx")
            try:
                entry_f = float(entry or 0)
                tgt_f = float(target or 0)
                atr_use = float(atr_f) if atr_f else (entry_f * 0.02 if entry_f else 0.01)
                _rvol_f = flat.get("rvol") or flat.get("volume_ratio")
                hold_est = _estimate_hold_duration(
                    entry_f, tgt_f, atr_use,
                    (pat.timeframe or "1d"), adx_f,
                    rvol=_rvol_f,
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
                "flat": flat,
                "hold_label": hold_est.get("label") or "",
                "trade_type": tc.get("type"),
                "duration_estimate": tc.get("duration") or hold_est.get("label"),
                "composite": comp,
                "score_breakdown": breakdown,
                "coverage_ratio": ratio,
                "missing_indicators": missing,
            })

        if board_budget_hit:
            break

    candidates.sort(key=lambda x: (-x["composite"], x["eta_hi"]))
    meta = {
        "patterns_active": len(patterns),
        "patterns_with_tickers_evaluated": patterns_tried,
        "global_ticker_universe": len(global_uni),
        "universe_by_source": uni_counts,
        "tickers_scored": tickers_scored,
        "skip_reasons": skip,
        "top_suppressed": suppressed,
        "equity_session_open": eq_open,
    }
    if for_opportunity_board:
        meta["for_opportunity_board"] = True
        meta["board_eval_budget_hit"] = board_budget_hit
        meta["board_per_pattern_cap"] = per_pat_cap
        meta["board_score_budget"] = score_budget
    return candidates, meta


def run_pattern_imminent_scan(
    db: Session,
    user_id: int | None,
    *,
    equity_session_open: bool | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Evaluate promoted/live patterns; dispatch imminent alerts by composite rank."""
    if not getattr(settings, "pattern_imminent_alert_enabled", True):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    eq_open = equity_session_open if equity_session_open is not None else us_stock_session_open()
    do_dry = dry_run if dry_run is not None else bool(
        getattr(settings, "pattern_imminent_debug_dry_run", False)
    )

    max_alerts = int(settings.pattern_imminent_max_per_run)
    cooldown_h = float(settings.pattern_imminent_cooldown_hours)
    max_per_ticker = max(1, int(getattr(settings, "pattern_imminent_max_per_ticker_per_run", 2)))
    max_per_pattern = max(1, int(getattr(settings, "pattern_imminent_max_per_pattern_per_run", 3)))
    max_eta = float(settings.pattern_imminent_max_eta_hours)
    min_rd = float(settings.pattern_imminent_min_readiness)
    cap_rd = float(settings.pattern_imminent_readiness_cap)
    eval_floor_board = float(settings.pattern_imminent_evaluable_ratio_floor)
    min_cov_main = float(getattr(settings, "pattern_imminent_min_feature_coverage_main", 0.45))
    min_comp_main = float(getattr(settings, "pattern_imminent_min_composite_main", 0.42))

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id,
        equity_session_open=eq_open,
        all_active_patterns=False,
        apply_main_dispatch_filters=True,
    )

    sent = 0
    delivery_failed = 0
    skipped_cd = 0
    per_ticker: dict[str, int] = {}
    per_pattern: dict[int, int] = {}
    diversity_skipped = 0

    for c in candidates:
        if sent >= max_alerts:
            break
        pat = c["pattern"]
        ticker = c["ticker"]
        if _cooldown_active(db, user_id, ticker, pat.id, cooldown_h):
            skipped_cd += 1
            continue
        if per_ticker.get(ticker, 0) >= max_per_ticker:
            diversity_skipped += 1
            continue
        if per_pattern.get(pat.id, 0) >= max_per_pattern:
            diversity_skipped += 1
            continue

        eta_txt = format_eta_range(c["eta_lo"], c["eta_hi"])
        sc = c["score"]
        desc = (pat.description or "")[:120].replace("\n", " ")
        hold_line = c.get("duration_estimate") or c.get("hold_label") or ""
        sigs = "; ".join((sc.get("signals") or [])[:4])

        msg = format_pattern_imminent(
            ticker=ticker,
            pattern_name=pat.name,
            pattern_id=pat.id,
            price=sc.get("price"),
            readiness=c["readiness"],
            composite_score=c["composite"],
            eta_txt=eta_txt,
            hold_line=hold_line,
            entry_price=sc.get("entry_price"),
            stop_loss=sc.get("stop_loss"),
            take_profit=sc.get("take_profit"),
            description=desc,
            signals=sigs,
        )

        # Publish to mesh sensor (nm_imminent_eval) for aggregation
        try:
            from .brain_neural_mesh.publisher import publish_imminent_eval
            publish_imminent_eval(
                db,
                scan_pattern_id=pat.id,
                ticker=ticker,
                composite_score=float(c["composite"]),
                readiness=float(c["readiness"]),
                eta_lo=float(c["eta_lo"]),
                eta_hi=float(c["eta_hi"]),
                price=sc.get("price", 0),
                user_id=user_id,
            )
        except Exception:
            logger.debug("[pattern_imminent] mesh publish failed for %s", ticker, exc_info=True)

        delivered = do_dry
        if not do_dry:
            # Persist the BreakoutAlert row BEFORE attempting external
            # delivery. The autotrader consumes these rows directly — its
            # availability must not depend on Telegram/SMS egress. Prior
            # behavior gated persistence on dispatch_alert's bool, so a
            # network-unreachable Telegram silently starved the autotrader.
            try:
                _insert_imminent_breakout_alert(
                    db,
                    user_id,
                    pat,
                    ticker,
                    sc,
                    c["flat"],
                    composite=float(c["composite"]),
                    score_breakdown=dict(c["score_breakdown"]),
                    readiness=float(c["readiness"]),
                    coverage_ratio=float(c["coverage_ratio"]),
                    eta_lo=float(c["eta_lo"]),
                    eta_hi=float(c["eta_hi"]),
                )
            except Exception as e:
                logger.warning("[pattern_imminent] BreakoutAlert insert failed: %s", e)

            delivered = dispatch_alert(
                db,
                user_id,
                PATTERN_BREAKOUT_IMMINENT,
                ticker,
                msg,
                price=sc.get("price"),
                trade_type=c.get("trade_type"),
                duration_estimate=hold_line[:60] if hold_line else None,
                scan_pattern_id=pat.id,
                confidence=min(0.95, 0.55 + 0.5 * float(c["composite"])),
            )
            if not delivered:
                delivery_failed += 1
        per_ticker[ticker] = per_ticker.get(ticker, 0) + 1
        per_pattern[pat.id] = per_pattern.get(pat.id, 0) + 1
        # Count as sent whenever we persisted the DB row — that is the
        # autotrader's contract. delivery_failed separately tracks the
        # external-channel outcome for observability.
        sent += 1

    summary: dict[str, Any] = {
        **meta,
        "ok": True,
        "dry_run": do_dry,
        "candidates": len(candidates),
        "alerts_sent": sent,
        "delivery_failed": delivery_failed,
        "cooldown_skipped": skipped_cd,
        "diversity_skipped": diversity_skipped,
        "us_session_context": describe_us_session_context(),
        "thresholds": {
            "min_readiness": min_rd,
            "readiness_cap": cap_rd,
            "max_eta_hours": max_eta,
            "evaluable_ratio_floor": eval_floor_board,
            "min_feature_coverage_main": min_cov_main,
            "min_composite_main": min_comp_main,
        },
    }
    logger.info("[pattern_imminent] %s", summary)
    return summary


# Re-export for tests / pattern_engine consumers
__all__ = [
    "build_imminent_ticker_universe",
    "gather_imminent_candidate_rows",
    "describe_us_session_context",
    "estimate_breakout_eta_hours",
    "evaluate_imminent_readiness",
    "flat_indicators_from_score",
    "format_eta_range",
    "recent_swing_resistance",
    "run_pattern_imminent_scan",
    "timeframe_to_hours_per_step",
    "us_stock_session_open",
    "us_stock_extended_session_open",
]
