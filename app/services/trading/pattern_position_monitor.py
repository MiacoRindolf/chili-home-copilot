"""Pattern-aware live position monitor.

Evaluates open Trades that are either (1) linked to a BreakoutAlert + ScanPattern,
or (2) have saved stop/target from an AI/manual plan with no linked alert.
Logs PatternMonitorDecision rows, dispatches Telegram ``pattern_monitor`` alerts on
risk triggers, and may call the LLM advisor for full pattern-linked paths.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...models.trading import (
    AlertHistory,
    BreakoutAlert,
    PatternMonitorDecision,
    ScanPattern,
    StopDecision,
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
from .setup_vitals import (
    detect_multi_check_degradation,
    get_or_compute_ticker_vitals,
    momentum_drop_urgent,
    record_setup_vitals_history,
)

logger = logging.getLogger(__name__)

# In-memory cache of last health scores per trade_id to compute deltas.
_last_health: dict[int, float] = {}
# Last composite_health from vitals per trade (mesh threshold notifications).
_last_vitals_composite: dict[int, float] = {}

# Dedup Telegram pattern_monitor alerts: same ticker+pattern+action+body as last send → skip.
# exit_now always sends. Cleared opportunistically when oversized.
_last_monitor_alert_sig: dict[str, str] = {}
_MONITOR_ALERT_DEDUP_MAX_KEYS = 500


def _monitor_alert_dedup_key(ticker: str, scan_pattern_id: int | None, action: str) -> str:
    tid = (ticker or "").upper()
    pid = str(scan_pattern_id) if scan_pattern_id is not None else "none"
    return f"{tid}:{pid}:{action}"


def _monitor_alert_content_signature(
    *,
    new_stop: float | None,
    new_target: float | None,
    health_score: float,
    reasoning: str,
    invalidations: list | None,
    caution_changes: list | None,
    structural_support: float | None,
    structural_support_label: str,
) -> str:
    """Stable fingerprint for duplicate detection (ignores volatile price/PnL in message)."""
    def _nf(x: float | None) -> str:
        if x is None:
            return "none"
        return f"{float(x):.6g}"

    rs = " ".join((reasoning or "").split())[:500]
    inv = json.dumps(invalidations or [], sort_keys=True, default=str)
    cau = json.dumps(caution_changes or [], sort_keys=True, default=str)
    hb = f"{float(health_score):.3f}"
    sup = _nf(structural_support)
    slab = (structural_support_label or "").strip()[:120]
    return "|".join((_nf(new_stop), _nf(new_target), hb, rs, inv, cau, sup, slab))


def _pattern_monitor_content_digest(action: str, sig: str) -> str:
    """Stable short hash for AlertHistory.content_signature (includes action)."""
    payload = f"{action}|{sig}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:64]


def _pattern_monitor_duplicate_in_db(
    db: Session,
    *,
    trade: Trade,
    content_digest: str,
) -> bool:
    """True if a successful pattern_monitor with this digest was already logged for this context."""
    q = (
        db.query(AlertHistory.id)
        .filter(
            AlertHistory.alert_type == "pattern_monitor",
            AlertHistory.ticker == trade.ticker,
            AlertHistory.content_signature == content_digest,
            AlertHistory.success.is_(True),
        )
    )
    if trade.scan_pattern_id is None:
        q = q.filter(AlertHistory.scan_pattern_id.is_(None))
    else:
        q = q.filter(AlertHistory.scan_pattern_id == int(trade.scan_pattern_id))
    if trade.user_id is not None:
        q = q.filter(AlertHistory.user_id == trade.user_id)
    return q.limit(1).first() is not None


@dataclass
class PositionVerdict:
    """Unified view for AI context / alerts: reconciles monitor + stops + plan levels."""

    action: str  # exit_now | tighten_to_support | hold_with_stop | hold
    stop_level: float | None
    reasoning: str
    urgency: str  # critical | warning | info


def resolve_position_verdict(
    db: Session,
    trade: Trade,
    *,
    current_price: float | None = None,
) -> PositionVerdict | None:
    """Reconcile latest pattern monitor decision, stop-engine state, and trade-plan levels.

    Returns None if trade is not open. ``current_price`` may be fetched via ``fetch_quote``.
    """
    if trade.status != "open":
        return None

    if current_price is None or current_price <= 0:
        try:
            q = fetch_quote(trade.ticker)
            current_price = float(q.get("price") or q.get("last") or 0) if q else 0.0
        except Exception:
            current_price = 0.0

    tiny_float = current_price > 0 and current_price < 5.0

    last_mon = (
        db.query(PatternMonitorDecision)
        .filter(PatternMonitorDecision.trade_id == trade.id)
        .order_by(PatternMonitorDecision.created_at.desc())
        .first()
    )

    last_stop = (
        db.query(StopDecision)
        .filter(StopDecision.trade_id == trade.id)
        .order_by(StopDecision.as_of_ts.desc())
        .first()
    )

    plan_stop = None
    plan_early = None
    if trade.related_alert_id:
        alert = db.get(BreakoutAlert, trade.related_alert_id)
        if alert and isinstance(alert.trade_plan, dict):
            kl = alert.trade_plan.get("key_levels") or {}
            try:
                if kl.get("stop") is not None:
                    plan_stop = float(kl["stop"])
            except (TypeError, ValueError):
                pass
            try:
                if kl.get("early_warning") is not None:
                    plan_early = float(kl["early_warning"])
            except (TypeError, ValueError):
                pass

    bits: list[str] = []
    action = "hold"
    urgency = "info"
    stop_level = trade.stop_loss

    if last_mon:
        age_h = (datetime.utcnow() - last_mon.created_at).total_seconds() / 3600.0
        if last_mon.action == "exit_now" and age_h <= 48:
            action = "exit_now"
            urgency = "critical"
            bits.append(
                f"Pattern monitor: EXIT_NOW {age_h:.1f}h ago "
                f"(health {last_mon.health_score:.0%} at decision, "
                f"price @ decision ${last_mon.price_at_decision or 0:.4f})"
            )
        elif last_mon.action == "tighten_stop" and last_mon.new_stop is not None and age_h <= 48:
            action = "tighten_to_support"
            stop_level = last_mon.new_stop
            urgency = "warning"
            bits.append(
                f"Pattern monitor: TIGHTEN_STOP to ${last_mon.new_stop:.4f} ({age_h:.1f}h ago)"
            )
        elif last_mon.action in ("loosen_target", "hold") and age_h <= 24:
            bits.append(
                f"Pattern monitor: {last_mon.action.upper()} ({age_h:.1f}h ago, "
                f"health {last_mon.health_score:.0%})"
            )

    if trade.stop_loss is not None and current_price > 0:
        if action == "hold":
            action = "hold_with_stop"
        dist_pct = (current_price - trade.stop_loss) / current_price * 100
        bits.append(
            f"Trade stop on file: ${trade.stop_loss:.4f} (~{dist_pct:.1f}% below spot)"
        )

    if plan_stop is not None or plan_early is not None:
        bits.append(
            f"Plan key levels: stop ${plan_stop or 'n/a'}, early_warning ${plan_early or 'n/a'}"
        )

    if last_stop:
        st = (last_stop.state or "").lower()
        tr = (last_stop.trigger or "").upper()
        if st == "triggered" or tr in ("STOP_HIT", "STOP_BREACH"):
            bits.append(
                f"Stop engine: state={last_stop.state} trigger={last_stop.trigger!r}"
            )

    if tiny_float and urgency != "critical":
        bits.append("Risk note: sub-$5 float — higher gap/dilution risk; favor explicit stops.")

    reasoning = " | ".join(bits) if bits else "No recent pattern-monitor or stop-engine signals."

    return PositionVerdict(
        action=action,
        stop_level=stop_level,
        reasoning=reasoning,
        urgency=urgency,
    )


def _structural_support_for_graduated_exit(
    *,
    current_price: float,
    alert: BreakoutAlert,
    plan_health: TradePlanHealth,
) -> tuple[float | None, str]:
    """Nearest support below price from trade plan + pattern stop; label for messaging."""
    if current_price <= 0:
        return None, ""

    candidates: list[tuple[float, str]] = []
    if plan_health.nearest_support is not None and plan_health.nearest_support < current_price:
        candidates.append(
            (plan_health.nearest_support, plan_health.nearest_support_label or "trade_plan"),
        )
    if alert.stop_loss is not None:
        try:
            ps = float(alert.stop_loss)
        except (TypeError, ValueError):
            ps = None
        if ps is not None and 0 < ps < current_price:
            candidates.append((ps, "pattern_stop"))

    if not candidates:
        return None, ""

    # Highest below price = closest support from below (long).
    best_v, best_lab = max(candidates, key=lambda x: x[0])
    return best_v, best_lab


def _apply_graduated_critical_override(
    primary: Any,
    *,
    urgent_invalidation: bool,
    pnl_pct: float | None,
    current_price: float,
    alert: BreakoutAlert,
    plan_health: TradePlanHealth,
    trade: Trade,
) -> None:
    """Mutate ``primary`` (AdjustmentRecommendation) when thesis is critically broken + loss.

    Uses structural support within 3% below spot to prefer tighten_stop over market exit;
    catastrophic loss still forces exit_now.
    """
    if not urgent_invalidation or pnl_pct is None:
        return

    from .pattern_adjustment_advisor import AdjustmentRecommendation

    # Catastrophic: always exit regardless of nearby support.
    if pnl_pct < -15:
        primary.action = "exit_now"
        primary.reasoning = (primary.reasoning or "") + (
            " [Critical: catastrophic loss override — exit regardless of nearby support]"
        )
        return

    if pnl_pct >= -5:
        return

    sup, lab = _structural_support_for_graduated_exit(
        current_price=current_price, alert=alert, plan_health=plan_health,
    )
    if sup is not None and current_price > 0:
        pct_to_support = (current_price - sup) / current_price * 100.0
        # Long: tighten only if support is *above* current trade stop (tighter = higher stop).
        old_sl = trade.stop_loss
        widens = old_sl is not None and sup < old_sl
        if 0 <= pct_to_support <= 3.0 and not widens:
            primary.action = "tighten_stop"
            primary.new_stop = sup
            primary.reasoning = (primary.reasoning or "") + (
                f" [Graduated: structural support ({lab}) @ ${sup:.4f} within 3% — "
                f"tighten stop vs exit at market]"
            )
            return

    primary.action = "exit_now"
    primary.reasoning = (primary.reasoning or "") + (
        " [Critical invalidation + loss — exit (no usable nearby support or would widen stop)]"
    )


def run_pattern_position_monitor(
    db: Session,
    user_id: int | None = None,
    *,
    dry_run: bool = False,
    event_driven: bool = False,
) -> dict[str, Any]:
    """Main entry point — evaluate pattern-linked positions and plan-level (stop/target only) positions."""

    q_pat = db.query(Trade).filter(
        Trade.status == "open",
        Trade.related_alert_id.isnot(None),
    )
    q_plan = db.query(Trade).filter(
        Trade.status == "open",
        Trade.related_alert_id.is_(None),
        or_(Trade.stop_loss.isnot(None), Trade.take_profit.isnot(None)),
    )
    if user_id is not None:
        q_pat = q_pat.filter(Trade.user_id == user_id)
        q_plan = q_plan.filter(Trade.user_id == user_id)

    trades_pat = q_pat.all()
    pat_ids = {t.id for t in trades_pat}
    trades_plan = [t for t in q_plan.all() if t.id not in pat_ids]
    trades = trades_pat + trades_plan
    if not trades:
        return {"ok": True, "evaluated": 0, "actions": 0, "skipped": 0}

    return _run_for_trades(db, trades, dry_run=dry_run, event_driven=event_driven)


def run_pattern_position_monitor_for_trades(
    db: Session,
    trades: list[Trade],
    *,
    dry_run: bool = False,
    event_driven: bool = True,
) -> dict[str, Any]:
    """Event-driven entry: evaluate a specific set of trades."""
    if not trades:
        return {"ok": True, "evaluated": 0, "actions": 0, "skipped": 0}
    return _run_for_trades(db, trades, dry_run=dry_run, event_driven=event_driven)


def _run_for_trades(
    db: Session,
    trades: list[Trade],
    *,
    dry_run: bool = False,
    event_driven: bool = False,
) -> dict[str, Any]:
    """Shared evaluation loop."""
    t0 = time.monotonic()

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
            if trade.related_alert_id:
                result = _evaluate_single(
                    db, trade,
                    dry_run=dry_run,
                    cooldown_min=cooldown_min,
                    health_healthy=health_healthy,
                    health_weakening=health_weakening,
                    delta_urgent=delta_urgent,
                    llm_conf_min=llm_conf_min,
                    event_driven=event_driven,
                )
            else:
                result = _evaluate_plan_levels_trade(
                    db,
                    trade,
                    dry_run=dry_run,
                    cooldown_min=cooldown_min,
                    event_driven=event_driven,
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


def _evaluate_plan_levels_trade(
    db: Session,
    trade: Trade,
    *,
    dry_run: bool,
    cooldown_min: float,
    event_driven: bool = False,
) -> str:
    """Evaluate trades with saved stop/target but no linked BreakoutAlert (AI / manual plan).

    Logs PatternMonitorDecision rows so the Monitor tab shows health and last check, and
    dispatches Telegram ``pattern_monitor`` alerts when price breaches the plan stop zone.
    """
    from .monitor_rules_engine import should_evaluate
    from .pattern_adjustment_advisor import AdjustmentRecommendation

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

    last_any = (
        db.query(PatternMonitorDecision)
        .filter(PatternMonitorDecision.trade_id == trade.id)
        .order_by(PatternMonitorDecision.created_at.desc())
        .first()
    )

    stop = trade.stop_loss
    target = trade.take_profit
    if stop is None and target is None:
        return "skipped"

    try:
        quote = fetch_quote(trade.ticker)
        current_price = float(quote.get("price") or quote.get("last") or 0) if quote else 0
    except Exception:
        current_price = 0
    if not current_price:
        return "skipped"

    if event_driven:
        mat_ok, mat_reason = should_evaluate(
            current_price=current_price,
            last_price=last_any.price_at_decision if last_any else None,
            current_indicators=None,
            last_snapshot=last_any.conditions_snapshot if last_any else None,
            stop_price=stop,
            target_price=target,
            price_change_pct=get_adaptive_weight("monitor_price_change_pct"),
            danger_zone_pct=get_adaptive_weight("monitor_danger_zone_pct"),
        )
        if not mat_ok:
            return "skipped"
        logger.debug("[pattern_monitor] plan_levels materiality %s: %s", trade.ticker, mat_reason)

    entry = float(trade.entry_price or 0)
    direction = trade.direction or "long"
    is_long = direction == "long"
    pnl_pct = ((current_price - entry) / entry * 100) if entry else None

    health_score = 0.5
    health_delta = None
    if is_long and stop is not None and entry > stop:
        risk = entry - stop
        cushion = current_price - stop
        if risk > 0:
            health_score = max(0.0, min(1.0, cushion / risk))
    elif (not is_long) and stop is not None and stop > entry:
        risk = stop - entry
        cushion = stop - current_price
        if risk > 0:
            health_score = max(0.0, min(1.0, cushion / risk))

    prev = _last_health.get(trade.id)
    if prev is not None:
        health_delta = round(health_score - prev, 4)
    _last_health[trade.id] = health_score

    primary = AdjustmentRecommendation(action="hold", confidence=1.0, reasoning="")
    if is_long:
        if stop is not None and current_price <= stop:
            primary = AdjustmentRecommendation(
                action="exit_now",
                confidence=0.95,
                reasoning=(
                    f"Price ${current_price:.4f} is at or below your plan stop ${stop:.4f} "
                    f"(plan-levels monitor)."
                ),
            )
        elif stop is not None:
            dist_pct = (current_price - stop) / stop * 100
            if 0 < dist_pct <= 2.0:
                primary = AdjustmentRecommendation(
                    action="exit_now",
                    confidence=0.85,
                    reasoning=(
                        f"Price within {dist_pct:.1f}% of plan stop ${stop:.4f} — "
                        f"plan-levels risk trigger."
                    ),
                )
    else:
        if stop is not None and current_price >= stop:
            primary = AdjustmentRecommendation(
                action="exit_now",
                confidence=0.95,
                reasoning=(
                    f"Price ${current_price:.4f} is at or above your plan stop ${stop:.4f} "
                    f"(plan-levels monitor, short)."
                ),
            )
        elif stop is not None:
            dist_pct = (stop - current_price) / stop * 100 if stop else 0
            if 0 < dist_pct <= 2.0:
                primary = AdjustmentRecommendation(
                    action="exit_now",
                    confidence=0.85,
                    reasoning=(
                        f"Price within {dist_pct:.1f}% of plan stop ${stop:.4f} — "
                        f"plan-levels risk trigger (short)."
                    ),
                )

    conditions_snap = {
        "plan_levels_only": True,
        "health_proxy": "cushion_vs_entry_stop_risk",
        "stop_loss": stop,
        "take_profit": target,
        "pnl_pct": pnl_pct,
    }

    health = ConditionHealth(
        conditions=[],
        health_score=health_score,
        health_delta=health_delta,
        human_summary=(
            f"Plan-levels watch (no scan pattern): health≈{health_score:.0%} from price vs "
            f"your stop/target."
        ),
    )

    decision = PatternMonitorDecision(
        trade_id=trade.id,
        breakout_alert_id=None,
        scan_pattern_id=None,
        health_score=health_score,
        health_delta=health_delta,
        conditions_snapshot=conditions_snap,
        action=primary.action,
        old_stop=trade.stop_loss,
        new_stop=primary.new_stop,
        old_target=trade.take_profit,
        new_target=primary.new_target,
        llm_confidence=None,
        llm_reasoning=primary.reasoning if primary.reasoning else None,
        mechanical_action="plan_levels",
        mechanical_stop=None,
        mechanical_target=None,
        decision_source="plan_levels",
        price_at_decision=current_price,
    )
    db.add(decision)

    try:
        _dispatch_monitor_alert(
            db,
            trade=trade,
            pattern_name="Position plan (AI / manual)",
            rec=primary,
            health=health,
            plan_health=None,
            current_price=current_price,
            pnl_pct=pnl_pct,
            dry_run=dry_run,
        )
    except Exception:
        logger.debug("[pattern_monitor] plan_levels alert dispatch failed", exc_info=True)

    return "action" if primary.action != "hold" else "hold"


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
    event_driven: bool = False,
) -> str:
    """Evaluate one pattern-linked trade.  Returns 'action', 'hold', or 'skipped'.

    Uses the self-learning dual-path engine:
    - Simple patterns: mechanical rule is primary, LLM shadow-validates on sample basis.
    - Complex patterns: LLM is authoritative, mechanical rule shadow-learns.
    - Graduated rules: mechanical-only with rare LLM drift checks.
    """
    from .monitor_rules_engine import (
        apply_level_ratios,
        build_signal_snapshot,
        compute_signal_signature,
        get_graduation_status,
        heuristic_adjustment,
        is_pattern_simple,
        lookup_rule,
        should_evaluate,
        should_shadow_llm,
    )

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

    # Latest decision any time (for materiality gate; cooldown uses ``recent`` above).
    last_any = (
        db.query(PatternMonitorDecision)
        .filter(PatternMonitorDecision.trade_id == trade.id)
        .order_by(PatternMonitorDecision.created_at.desc())
        .first()
    )

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

    # ── Materiality gate (event-driven mode) ──
    if event_driven:
        last_snap = last_any.conditions_snapshot if last_any else None
        last_price = last_any.price_at_decision if last_any else None
        mat_ok, mat_reason = should_evaluate(
            current_price=current_price,
            last_price=last_price,
            current_indicators=flat,
            last_snapshot=last_snap,
            stop_price=trade.stop_loss or alert.stop_loss,
            target_price=trade.take_profit or alert.target_price,
            price_change_pct=get_adaptive_weight("monitor_price_change_pct"),
            danger_zone_pct=get_adaptive_weight("monitor_danger_zone_pct"),
        )
        if not mat_ok:
            return "skipped"
        logger.debug("[pattern_monitor] materiality gate passed for %s: %s", trade.ticker, mat_reason)

    pnl_pct = ((current_price - trade.entry_price) / trade.entry_price * 100) if trade.entry_price else None

    # ── Lazy trade plan generation ──
    _ensure_trade_plan(db, alert, pattern, flat, current_price)
    trade_plan = alert.trade_plan

    bar_interval = (getattr(pattern, "timeframe", None) or "1d").strip() or "1d"

    # ── Setup vitals (trajectory cache / snapshot history) ──
    vitals = get_or_compute_ticker_vitals(db, trade.ticker, bar_interval)
    vitals_degradation = detect_multi_check_degradation(
        db, trade.id, vitals.momentum_score, vitals.volume_score
    )
    mom_urgent = momentum_drop_urgent(db, trade.id, vitals.momentum_score)
    bearish_div = any(
        isinstance(d, dict) and d.get("type") == "bearish" for d in (vitals.divergences or [])
    )

    # ── Evaluate pattern health (static conditions) ──
    previous_health = _last_health.get(trade.id)
    health = evaluate_pattern_health(
        pattern.rules_json,
        flat,
        previous_health=previous_health,
    )
    _last_health[trade.id] = health.health_score

    # ── Evaluate trade plan (dynamic conditions) + vitals ──
    plan_health = evaluate_trade_plan(trade_plan, flat, current_price, vitals=vitals)

    # ── Decide whether action is needed ──
    needs_action = False
    urgent_invalidation = False

    if plan_health.has_critical_invalidation:
        needs_action = True
        urgent_invalidation = True
    elif plan_health.has_any_invalidation:
        needs_action = True
    elif plan_health.caution_signals_changed:
        needs_action = True

    if health.health_delta is not None and health.health_delta <= delta_urgent:
        needs_action = True
    elif health.health_score < health_weakening:
        needs_action = True
    elif previous_health is not None and abs(health.health_score - previous_health) >= 0.2:
        needs_action = True

    if vitals_degradation.get("degraded_3plus"):
        needs_action = True
    if mom_urgent:
        needs_action = True
    if vitals.overextension_risk > 0.8 and vitals.momentum_score < -0.2:
        needs_action = True
    if bearish_div:
        needs_action = True

    def _persist_vitals_history() -> None:
        try:
            record_setup_vitals_history(
                db,
                trade_id=trade.id,
                breakout_alert_id=alert.id,
                vitals=vitals,
                price=current_price,
                degradation_flags={**vitals_degradation, "mom_urgent": mom_urgent},
            )
        except Exception:
            logger.debug("[pattern_monitor] vitals history insert failed", exc_info=True)

    if not needs_action:
        _persist_vitals_history()
        return "hold"

    # ── Build signal snapshot for rules engine ──
    pattern_type = (pattern.name or f"pattern_{pattern.id}")[:120]
    sig_snap = build_signal_snapshot(
        plan_health=plan_health,
        condition_health=health,
        pnl_pct=pnl_pct,
        current_price=current_price,
        stop_price=trade.stop_loss or alert.stop_loss,
        target_price=trade.take_profit or alert.target_price,
        vitals=vitals,
    )
    signal_sig = compute_signal_signature(sig_snap)
    simple = is_pattern_simple(pattern.rules_json if isinstance(pattern.rules_json, dict) else None)
    grad_status = get_graduation_status(db, pattern_type, signal_sig)

    # ── Mechanical rule lookup ──
    mech = lookup_rule(db, pattern_type, signal_sig)
    if mech and mech.rule_id:
        mech = apply_level_ratios(
            mech, mech.rule_id, current_price,
            trade.stop_loss or alert.stop_loss, db,
        )

    # ── Decide which path is authoritative ──
    use_llm = True
    decision_source = "llm"

    if grad_status == "graduated" and mech:
        use_llm = should_shadow_llm(grad_status)
        decision_source = "mechanical"
    elif simple and mech and grad_status == "shadow":
        use_llm = should_shadow_llm(grad_status)
        decision_source = "mechanical"
    elif not simple and grad_status in ("bootstrap", "shadow", "demoted"):
        use_llm = True
        decision_source = "llm"
    elif simple and grad_status == "bootstrap":
        use_llm = True
        decision_source = "llm"

    # ── Heuristic pre-filter (skips LLM on unambiguous cases) ──
    heuristic_dec = None
    if use_llm:
        heuristic_dec = heuristic_adjustment(
            plan_health=plan_health,
            condition_health=health,
            pnl_pct=pnl_pct,
            current_price=current_price,
            current_stop=trade.stop_loss,
            current_target=trade.take_profit,
            pattern_stop=alert.stop_loss,
            delta_urgent=delta_urgent,
            health_healthy=health_healthy,
            trade_direction=trade.direction or "long",
            vitals=vitals,
            vitals_degradation=vitals_degradation,
        )
        if heuristic_dec is not None:
            use_llm = False
            decision_source = "heuristic"

    # ── LLM advisory (when needed) ──
    rec = None
    if use_llm:
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

    # ── Select primary recommendation ──
    if decision_source == "mechanical" and mech:
        from .pattern_adjustment_advisor import AdjustmentRecommendation
        primary = AdjustmentRecommendation(
            action=mech.action,
            new_stop=mech.new_stop,
            new_target=mech.new_target,
            confidence=mech.confidence,
            reasoning=mech.reasoning,
        )
    elif decision_source == "heuristic" and heuristic_dec is not None:
        from .pattern_adjustment_advisor import AdjustmentRecommendation
        primary = AdjustmentRecommendation(
            action=heuristic_dec.action,
            new_stop=heuristic_dec.new_stop,
            new_target=heuristic_dec.new_target,
            confidence=heuristic_dec.confidence,
            reasoning=heuristic_dec.reasoning,
        )
    elif rec:
        primary = rec
    else:
        from .pattern_adjustment_advisor import AdjustmentRecommendation
        primary = AdjustmentRecommendation(action="hold", confidence=0.0, reasoning="No decision available")

    # Critical invalidation + loss: graduated exit vs tighten to structural support.
    _apply_graduated_critical_override(
        primary,
        urgent_invalidation=urgent_invalidation,
        pnl_pct=pnl_pct,
        current_price=current_price,
        alert=alert,
        plan_health=plan_health,
        trade=trade,
    )

    if primary.confidence < llm_conf_min and primary.action not in ("exit_now", "tighten_stop"):
        primary.action = "hold"

    # ── Log decision with dual-path data ──
    conditions_snap = health.to_dict()
    conditions_snap["trade_plan"] = plan_health.to_dict()
    conditions_snap["pnl_pct"] = pnl_pct
    conditions_snap["price_vs_stop_pct"] = sig_snap.price_vs_stop_pct
    conditions_snap["price_vs_target_pct"] = sig_snap.price_vs_target_pct
    _atr = flat.get("atr")
    if _atr is None:
        _atr = flat.get("atr_14")
    try:
        conditions_snap["atr_snapshot"] = float(_atr) if _atr is not None else None
    except (TypeError, ValueError):
        conditions_snap["atr_snapshot"] = None
    if plan_health.nearest_support is not None:
        conditions_snap["nearest_support"] = plan_health.nearest_support
        conditions_snap["nearest_support_label"] = plan_health.nearest_support_label
    conditions_snap["vitals"] = vitals.to_dict()
    conditions_snap["vitals_degradation"] = vitals_degradation

    _persist_vitals_history()

    try:
        from .brain_neural_mesh.publisher import publish_setup_vitals_change

        publish_setup_vitals_change(
            db,
            trade_id=trade.id,
            ticker=trade.ticker,
            vitals=vitals,
            previous_composite=_last_vitals_composite.get(trade.id),
        )
        _last_vitals_composite[trade.id] = float(vitals.composite_health)
    except Exception:
        logger.debug("[pattern_monitor] vitals mesh publish skipped", exc_info=True)

    decision = PatternMonitorDecision(
        trade_id=trade.id,
        breakout_alert_id=alert.id,
        scan_pattern_id=pattern.id,
        health_score=health.health_score,
        health_delta=health.health_delta,
        conditions_snapshot=conditions_snap,
        vitals_composite=float(vitals.composite_health),
        action=primary.action,
        old_stop=trade.stop_loss,
        new_stop=primary.new_stop,
        old_target=trade.take_profit,
        new_target=primary.new_target,
        llm_confidence=rec.confidence if rec else None,
        llm_reasoning=rec.reasoning if rec else None,
        mechanical_action=(
            mech.action
            if mech
            else (heuristic_dec.action if heuristic_dec else None)
        ),
        mechanical_stop=(
            mech.new_stop
            if mech
            else (heuristic_dec.new_stop if heuristic_dec else None)
        ),
        mechanical_target=(
            mech.new_target
            if mech
            else (heuristic_dec.new_target if heuristic_dec else None)
        ),
        decision_source=decision_source,
        price_at_decision=current_price,
    )
    db.add(decision)

    # ── Apply adjustment ──
    applied = False
    if not dry_run:
        if primary.action == "tighten_stop" and primary.new_stop is not None:
            trade.stop_loss = primary.new_stop
            applied = True
        elif primary.action == "loosen_target" and primary.new_target is not None:
            trade.take_profit = primary.new_target
            applied = True
        elif primary.action == "exit_now":
            applied = True

    # ── Dispatch Telegram alert ──
    try:
        _dispatch_monitor_alert(
            db,
            trade=trade,
            pattern_name=pattern.name or f"Pattern #{pattern.id}",
            rec=primary,
            health=health,
            plan_health=plan_health,
            current_price=current_price,
            pnl_pct=pnl_pct,
            dry_run=dry_run,
        )
    except Exception:
        logger.debug("[pattern_monitor] alert dispatch failed for %s", trade.ticker, exc_info=True)

    return "action" if applied or primary.action != "hold" else "hold"


def _ensure_trade_plan(
    db: Session,
    alert: BreakoutAlert,
    pattern: ScanPattern,
    indicators: dict[str, Any],
    current_price: float,
) -> None:
    """Lazy-generate a trade plan for the alert using the hybrid path.

    Simple patterns (<5 conditions): mechanical plan is primary (no LLM).
    Complex patterns: rich mechanical plan first; LLM only if invalidations < 2.
    Both plans are stored for accuracy tracking.
    """
    if alert.trade_plan and alert.trade_plan_mechanical:
        return

    try:
        import json as _json
        from .monitor_rules_engine import get_complexity_band, is_pattern_simple
        from .trade_plan_extractor import extract_trade_plan, extract_trade_plan_mechanical

        rules = pattern.rules_json
        if isinstance(rules, str):
            rules = _json.loads(rules)
        conditions = (rules or {}).get("conditions", [])

        simple = is_pattern_simple(rules if isinstance(rules, dict) else None)
        entry = alert.entry_price or current_price
        stop = alert.stop_loss or 0
        target = alert.target_price or 0

        # Always generate mechanical plan (cheap, no LLM)
        if not alert.trade_plan_mechanical:
            mech_plan = extract_trade_plan_mechanical(
                pattern_conditions=conditions,
                entry_price=entry,
                stop_loss=stop,
                target_price=target,
                current_price=current_price,
                indicators=indicators,
            )
            alert.trade_plan_mechanical = mech_plan

        # For simple patterns: mechanical is primary, LLM as shadow
        if simple and not alert.trade_plan:
            alert.trade_plan = alert.trade_plan_mechanical
            logger.info(
                "[pattern_monitor] Mechanical trade plan for alert %s (%s) — simple pattern",
                alert.id, alert.ticker,
            )

        # Complex patterns: prefer mechanical when it has enough invalidations; else LLM
        if not simple and not alert.trade_plan:
            mech = alert.trade_plan_mechanical or {}
            inv_n = len(mech.get("invalidation_conditions") or [])
            if inv_n >= 2:
                alert.trade_plan = mech
                logger.info(
                    "[pattern_monitor] Mechanical trade plan for alert %s (%s) — complex (%s invalidations)",
                    alert.id, alert.ticker, inv_n,
                )

        if not alert.trade_plan:
            llm_plan = extract_trade_plan(
                ticker=alert.ticker,
                pattern_name=pattern.name or f"Pattern #{pattern.id}",
                pattern_description=pattern.description or "",
                pattern_conditions=conditions,
                entry_price=entry,
                stop_loss=stop,
                target_price=target,
                current_price=current_price,
                indicators=indicators,
            )
            if llm_plan:
                alert.trade_plan = llm_plan
                logger.info(
                    "[pattern_monitor] LLM trade plan for alert %s (%s) — sparse mechanical or fallback",
                    alert.id, alert.ticker,
                )
            elif alert.trade_plan_mechanical:
                alert.trade_plan = alert.trade_plan_mechanical
                logger.info(
                    "[pattern_monitor] LLM failed, using mechanical plan for alert %s (%s)",
                    alert.id, alert.ticker,
                )

        db.add(alert)
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
    """Send Telegram alert for critical pattern monitor actions only (exit_now)."""
    if rec.action != "exit_now":
        return

    from .alert_formatter import format_pattern_adjustment
    from .alerts import dispatch_alert

    invalidations = plan_health.invalidations_triggered if plan_health else []
    caution_changes = plan_health.caution_signals_changed if plan_health else []

    _sup = plan_health.nearest_support if plan_health else None
    _sup_lab = plan_health.nearest_support_label if plan_health else ""

    # Content-aware dedup: same ticker+pattern+action and same substantive payload → no second Telegram.
    # exit_now always sends (critical). In-memory + DB (survives restarts / multi-worker).
    monitor_digest: str | None = None
    if rec.action != "exit_now" and not dry_run:
        dedup_key = _monitor_alert_dedup_key(
            trade.ticker, trade.scan_pattern_id, str(rec.action)
        )
        sig = _monitor_alert_content_signature(
            new_stop=rec.new_stop,
            new_target=rec.new_target,
            health_score=health.health_score,
            reasoning=getattr(rec, "reasoning", "") or "",
            invalidations=invalidations,
            caution_changes=caution_changes,
            structural_support=_sup,
            structural_support_label=_sup_lab,
        )
        if _last_monitor_alert_sig.get(dedup_key) == sig:
            logger.debug(
                "[pattern_monitor] duplicate alert suppressed for %s key=%s",
                trade.ticker,
                dedup_key,
            )
            return
        monitor_digest = _pattern_monitor_content_digest(str(rec.action), sig)
        if _pattern_monitor_duplicate_in_db(db, trade=trade, content_digest=monitor_digest):
            logger.debug(
                "[pattern_monitor] duplicate alert suppressed (db) for %s key=%s",
                trade.ticker,
                dedup_key,
            )
            if len(_last_monitor_alert_sig) >= _MONITOR_ALERT_DEDUP_MAX_KEYS:
                _last_monitor_alert_sig.clear()
            _last_monitor_alert_sig[dedup_key] = sig
            return
        if len(_last_monitor_alert_sig) >= _MONITOR_ALERT_DEDUP_MAX_KEYS:
            _last_monitor_alert_sig.clear()
        _last_monitor_alert_sig[dedup_key] = sig

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
        structural_support=_sup,
        structural_support_label=_sup_lab,
    )

    dispatch_alert(
        db,
        alert_type="pattern_monitor",
        ticker=trade.ticker,
        message=msg,
        user_id=trade.user_id,
        scan_pattern_id=trade.scan_pattern_id,
        content_signature=monitor_digest,
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
        # Beneficial if price dropped meaningfully after exit (not noise).
        atr = None
        if d.conditions_snapshot and isinstance(d.conditions_snapshot, dict):
            atr = d.conditions_snapshot.get("atr_snapshot")
        try:
            atr_f = float(atr) if atr is not None else 0.0
        except (TypeError, ValueError):
            atr_f = 0.0
        if atr_f > 0:
            threshold = atr_f * 0.5
        else:
            threshold = max(abs(d.price_at_decision or 0) * 0.005, 0.01)
        d.was_beneficial = move < -threshold
    elif d.action == "hold":
        # Beneficial if price stayed roughly stable or went up.
        d.was_beneficial = move >= -abs(d.price_at_decision * 0.01)
