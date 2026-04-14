"""Pattern-aware live position monitor.

Queries open Trades linked to pattern-imminent alerts, evaluates the
pattern's conditions against live indicators, and (when health changes
significantly) calls the LLM advisor for stop/target adjustments.
Dispatches Telegram alerts and logs decisions for learning feedback.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import (
    BreakoutAlert,
    PatternMonitorDecision,
    ScanPattern,
    Trade,
)
from .market_data import fetch_quote, get_indicator_snapshot
from .pattern_condition_monitor import (
    ConditionHealth,
    TradePlanHealth,
    evaluate_pattern_health,
    evaluate_trade_plan,
)
from .scanner import get_adaptive_weight

logger = logging.getLogger(__name__)

# In-memory cache of last health scores per trade_id to compute deltas.
_last_health: dict[int, float] = {}


def run_pattern_position_monitor(
    db: Session,
    user_id: int | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Main entry point — evaluate all pattern-linked positions for a user."""
    t0 = time.monotonic()

    query = db.query(Trade).filter(
        Trade.status == "open",
        Trade.related_alert_id.isnot(None),
    )
    if user_id is not None:
        query = query.filter(Trade.user_id == user_id)

    trades = query.all()
    if not trades:
        return {"ok": True, "evaluated": 0, "actions": 0, "skipped": 0}

    cooldown_min = get_adaptive_weight("monitor_cooldown_minutes")
    health_healthy = get_adaptive_weight("monitor_health_healthy")
    health_weakening = get_adaptive_weight("monitor_health_weakening")
    delta_urgent = get_adaptive_weight("monitor_delta_urgent")
    llm_conf_min = get_adaptive_weight("monitor_llm_confidence_min")

    evaluated = 0
    actions_taken = 0
    skipped = 0

    for trade in trades:
        try:
            result = _evaluate_single(
                db, trade,
                dry_run=dry_run,
                cooldown_min=cooldown_min,
                health_healthy=health_healthy,
                health_weakening=health_weakening,
                delta_urgent=delta_urgent,
                llm_conf_min=llm_conf_min,
            )
            evaluated += 1
            if result == "action":
                actions_taken += 1
            elif result == "skipped":
                skipped += 1
        except Exception:
            logger.warning("[pattern_monitor] error evaluating trade %s", trade.id, exc_info=True)

    db.commit()

    elapsed = time.monotonic() - t0
    summary = {
        "ok": True,
        "evaluated": evaluated,
        "actions": actions_taken,
        "skipped": skipped,
        "elapsed_s": round(elapsed, 2),
    }
    if evaluated:
        logger.info("[pattern_monitor] %s", summary)
    return summary


def _evaluate_single(
    db: Session,
    trade: Trade,
    *,
    dry_run: bool,
    cooldown_min: float,
    health_healthy: float,
    health_weakening: float,
    delta_urgent: float,
    llm_conf_min: float,
) -> str:
    """Evaluate one pattern-linked trade. Returns 'action', 'hold', or 'skipped'."""

    # Cooldown: skip if we made a decision recently.
    recent_cutoff = datetime.utcnow() - timedelta(minutes=cooldown_min)
    recent = (
        db.query(PatternMonitorDecision)
        .filter(
            PatternMonitorDecision.trade_id == trade.id,
            PatternMonitorDecision.created_at >= recent_cutoff,
        )
        .first()
    )
    if recent:
        return "skipped"

    # Resolve the alert and pattern.
    alert: BreakoutAlert | None = db.get(BreakoutAlert, trade.related_alert_id) if trade.related_alert_id else None
    if not alert:
        return "skipped"

    pattern_id = trade.scan_pattern_id or alert.scan_pattern_id
    pattern: ScanPattern | None = db.get(ScanPattern, pattern_id) if pattern_id else None
    if not pattern or not pattern.rules_json:
        return "skipped"

    # Fetch live indicators.
    timeframe = getattr(pattern, "timeframe", None) or "1d"
    try:
        indicators = get_indicator_snapshot(trade.ticker, timeframe)
    except Exception:
        logger.debug("[pattern_monitor] indicator fetch failed for %s", trade.ticker)
        return "skipped"

    if not indicators:
        return "skipped"

    flat = _flatten_indicators(indicators)

    # Get quote for current price.
    try:
        quote = fetch_quote(trade.ticker)
        current_price = float(quote.get("price") or quote.get("last") or 0) if quote else 0
    except Exception:
        current_price = 0
    if not current_price:
        return "skipped"

    pnl_pct = ((current_price - trade.entry_price) / trade.entry_price * 100) if trade.entry_price else None

    # ── Lazy trade plan generation ──
    _ensure_trade_plan(db, alert, pattern, flat, current_price)
    trade_plan = alert.trade_plan

    # ── Evaluate pattern health (static conditions) ──
    previous_health = _last_health.get(trade.id)
    health = evaluate_pattern_health(
        pattern.rules_json,
        flat,
        previous_health=previous_health,
    )
    _last_health[trade.id] = health.health_score

    # ── Evaluate trade plan (dynamic conditions) ──
    plan_health = evaluate_trade_plan(trade_plan, flat, current_price)

    # ── Decide whether LLM consultation is needed ──
    needs_llm = False
    urgent_invalidation = False

    # Trade plan invalidation always triggers action.
    if plan_health.has_critical_invalidation:
        needs_llm = True
        urgent_invalidation = True
    elif plan_health.has_any_invalidation:
        needs_llm = True
    elif plan_health.caution_signals_changed:
        needs_llm = True

    # Pattern health triggers (existing logic).
    if health.health_delta is not None and health.health_delta <= delta_urgent:
        needs_llm = True
    elif health.health_score < health_weakening:
        needs_llm = True
    elif previous_health is not None and abs(health.health_score - previous_health) >= 0.2:
        needs_llm = True

    if not needs_llm:
        return "hold"

    # ── Call LLM advisor with combined context ──
    from .pattern_adjustment_advisor import get_adjustment

    combined_summary = health.human_summary
    if plan_health.human_summary:
        combined_summary += "\n\n--- Trade Plan Status ---\n" + plan_health.human_summary

    rec = get_adjustment(
        ticker=trade.ticker,
        pattern_name=pattern.name or f"Pattern #{pattern.id}",
        pattern_description=pattern.description or "",
        health_summary=combined_summary,
        health_score=health.health_score,
        health_delta=health.health_delta,
        current_price=current_price,
        entry_price=trade.entry_price,
        current_stop=trade.stop_loss,
        current_target=trade.take_profit,
        pattern_stop=alert.stop_loss,
        pattern_target=alert.target_price,
        pnl_pct=pnl_pct,
        trade_plan_health=plan_health,
    )

    # Critical invalidation with loss overrides to exit_now regardless of LLM.
    if urgent_invalidation and pnl_pct is not None and pnl_pct < -5:
        rec.action = "exit_now"
        rec.reasoning = (rec.reasoning or "") + " [Critical invalidation + loss override]"

    if rec.confidence < llm_conf_min and rec.action != "exit_now":
        rec.action = "hold"

    # ── Log decision ──
    conditions_snap = health.to_dict()
    conditions_snap["trade_plan"] = plan_health.to_dict()

    decision = PatternMonitorDecision(
        trade_id=trade.id,
        breakout_alert_id=alert.id,
        scan_pattern_id=pattern.id,
        health_score=health.health_score,
        health_delta=health.health_delta,
        conditions_snapshot=conditions_snap,
        action=rec.action,
        old_stop=trade.stop_loss,
        new_stop=rec.new_stop,
        old_target=trade.take_profit,
        new_target=rec.new_target,
        llm_confidence=rec.confidence,
        llm_reasoning=rec.reasoning,
        price_at_decision=current_price,
    )
    db.add(decision)

    # ── Apply adjustment ──
    applied = False
    if not dry_run:
        if rec.action == "tighten_stop" and rec.new_stop is not None:
            trade.stop_loss = rec.new_stop
            applied = True
        elif rec.action == "loosen_target" and rec.new_target is not None:
            trade.take_profit = rec.new_target
            applied = True
        elif rec.action == "exit_now":
            applied = True

    # ── Dispatch Telegram alert ──
    try:
        _dispatch_monitor_alert(
            db,
            trade=trade,
            pattern_name=pattern.name or f"Pattern #{pattern.id}",
            rec=rec,
            health=health,
            plan_health=plan_health,
            current_price=current_price,
            pnl_pct=pnl_pct,
            dry_run=dry_run,
        )
    except Exception:
        logger.debug("[pattern_monitor] alert dispatch failed for %s", trade.ticker, exc_info=True)

    return "action" if applied or rec.action != "hold" else "hold"


def _ensure_trade_plan(
    db: Session,
    alert: BreakoutAlert,
    pattern: ScanPattern,
    indicators: dict[str, Any],
    current_price: float,
) -> None:
    """Lazy-generate a trade plan for the alert if one doesn't exist yet."""
    if alert.trade_plan:
        return

    try:
        import json as _json
        from .trade_plan_extractor import extract_trade_plan

        rules = pattern.rules_json
        if isinstance(rules, str):
            rules = _json.loads(rules)
        conditions = (rules or {}).get("conditions", [])

        plan = extract_trade_plan(
            ticker=alert.ticker,
            pattern_name=pattern.name or f"Pattern #{pattern.id}",
            pattern_description=pattern.description or "",
            pattern_conditions=conditions,
            entry_price=alert.entry_price or current_price,
            stop_loss=alert.stop_loss or 0,
            target_price=alert.target_price or 0,
            current_price=current_price,
            indicators=indicators,
        )
        if plan:
            alert.trade_plan = plan
            db.add(alert)
            logger.info("[pattern_monitor] Generated trade plan for alert %s (%s)", alert.id, alert.ticker)
    except Exception:
        logger.debug("[pattern_monitor] trade plan generation failed for alert %s", alert.id, exc_info=True)


def _dispatch_monitor_alert(
    db: Session,
    *,
    trade: Trade,
    pattern_name: str,
    rec: Any,
    health: ConditionHealth,
    plan_health: TradePlanHealth | None = None,
    current_price: float,
    pnl_pct: float | None,
    dry_run: bool,
) -> None:
    """Send Telegram alert for a pattern monitor action."""
    if rec.action == "hold":
        return

    from .alert_formatter import format_pattern_adjustment
    from .alerts import dispatch_alert

    invalidations = plan_health.invalidations_triggered if plan_health else []
    caution_changes = plan_health.caution_signals_changed if plan_health else []

    msg = format_pattern_adjustment(
        ticker=trade.ticker,
        pattern_name=pattern_name,
        action=rec.action,
        health_score=health.health_score,
        health_delta=health.health_delta,
        old_stop=trade.stop_loss,
        new_stop=rec.new_stop,
        old_target=trade.take_profit,
        new_target=rec.new_target,
        current_price=current_price,
        entry_price=trade.entry_price,
        pnl_pct=pnl_pct,
        reasoning=rec.reasoning,
        dry_run=dry_run,
        invalidations=invalidations,
        caution_changes=caution_changes,
    )

    dispatch_alert(
        db,
        alert_type="pattern_monitor",
        ticker=trade.ticker,
        message=msg,
        user_id=trade.user_id,
        scan_pattern_id=trade.scan_pattern_id,
    )


def _flatten_indicators(snap: dict[str, Any]) -> dict[str, Any]:
    """Flatten a nested indicator snapshot into a single-level dict.

    get_indicator_snapshot returns {'rsi': {'value': 50, ...}, 'macd': {...}, ...}.
    The condition evaluator expects flat keys like 'rsi_14', 'macd_hist', etc.
    """
    flat: dict[str, Any] = {}
    for key, val in snap.items():
        if isinstance(val, dict):
            inner_val = val.get("value")
            if inner_val is not None:
                flat[key] = inner_val
            for k2, v2 in val.items():
                if k2 != "value":
                    composite_key = f"{key}_{k2}" if not k2.startswith(key) else k2
                    flat[composite_key] = v2
        else:
            flat[key] = val
    # Common aliases the condition evaluator may look for.
    if "rsi" in flat and "rsi_14" not in flat:
        flat["rsi_14"] = flat["rsi"]
    if "macd_histogram" in flat and "macd_hist" not in flat:
        flat["macd_hist"] = flat["macd_histogram"]
    if "price" not in flat:
        for pk in ("close", "last", "current_price"):
            if pk in flat:
                flat["price"] = flat[pk]
                break
    return flat


# ── Decision outcome review ─────────────────────────────────────────────

def review_monitor_decisions(db: Session) -> dict[str, Any]:
    """Fill price_after_1h / price_after_4h / was_beneficial on past decisions.

    Run hourly by the scheduler.  For each decision older than 1h (or 4h)
    that hasn't been filled yet, fetch the current price and score whether
    the adjustment was beneficial.
    """
    now = datetime.utcnow()
    filled_1h = filled_4h = 0

    # Decisions needing 1h review (created > 1h ago, price_after_1h is null).
    cutoff_1h = now - timedelta(hours=1)
    need_1h = (
        db.query(PatternMonitorDecision)
        .filter(
            PatternMonitorDecision.created_at <= cutoff_1h,
            PatternMonitorDecision.price_after_1h.is_(None),
            PatternMonitorDecision.price_at_decision.isnot(None),
        )
        .limit(50)
        .all()
    )
    for d in need_1h:
        try:
            q = fetch_quote(db.get(Trade, d.trade_id).ticker if d.trade_id else "")
            if q and q.get("price"):
                d.price_after_1h = float(q["price"])
                filled_1h += 1
        except Exception:
            pass

    # Decisions needing 4h review.
    cutoff_4h = now - timedelta(hours=4)
    need_4h = (
        db.query(PatternMonitorDecision)
        .filter(
            PatternMonitorDecision.created_at <= cutoff_4h,
            PatternMonitorDecision.price_after_4h.is_(None),
            PatternMonitorDecision.price_at_decision.isnot(None),
        )
        .limit(50)
        .all()
    )
    for d in need_4h:
        try:
            q = fetch_quote(db.get(Trade, d.trade_id).ticker if d.trade_id else "")
            if q and q.get("price"):
                d.price_after_4h = float(q["price"])
                _score_benefit(d)
                filled_4h += 1
        except Exception:
            pass

    if filled_1h or filled_4h:
        db.commit()

    return {"filled_1h": filled_1h, "filled_4h": filled_4h}


def _score_benefit(d: PatternMonitorDecision) -> None:
    """Determine if an adjustment decision was beneficial based on price movement."""
    if d.price_at_decision is None or d.price_after_4h is None:
        return
    move = d.price_after_4h - d.price_at_decision

    if d.action == "tighten_stop":
        # Beneficial if price continued down (stop saved money) or at least
        # didn't rally significantly (stop wasn't premature).
        d.was_beneficial = move <= 0 or (d.new_stop is not None and d.price_after_4h < d.new_stop * 1.02)
    elif d.action == "loosen_target":
        # Beneficial if price went up toward the new target.
        d.was_beneficial = move > 0
    elif d.action == "exit_now":
        # Beneficial if price continued to drop.
        d.was_beneficial = move < 0
    elif d.action == "hold":
        # Beneficial if price stayed roughly stable or went up.
        d.was_beneficial = move >= -abs(d.price_at_decision * 0.01)
