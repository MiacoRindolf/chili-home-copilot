"""Pattern position monitor — active setups dashboard API."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from ...config import settings
from ...deps import get_db, get_identity_ctx
from ...models.trading import (
    AutoTraderRun,
    BrainWorkEvent,
    BreakoutAlert,
    PatternMonitorDecision,
    ScanPattern,
    Trade,
)
from ...services import trading_service as ts
from ...services.trading.broker_position_truth import (
    broker_position_display_metrics,
    filter_broker_stale_open_trades,
)
from ...services.trading.cash_deployment import (
    annotate_cash_deployment_row,
    cash_deployment_null_lineage_candidates,
    cash_deployment_rows,
    cash_deployment_snapshot_rows,
    cash_deployment_summary,
)
from ...services.trading.edge_reliability import (
    EXIT_VARIANT_DIAGNOSTIC,
    EXIT_VARIANT_REFRESH,
    RECERT_RESCUE_DIAGNOSTIC,
    RECERT_RESCUE_REFRESH,
    edge_supply_rows,
    edge_supply_snapshot_rows,
    edge_supply_summary,
    latest_edge_reliability_snapshot_slices,
)
from ...services.trading.pattern_position_monitor import run_pattern_position_monitor_for_trades
from ...services.trading.robinhood_exit_execution import describe_trade_execution_state
from ._utils import json_safe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading-monitor"])

_STRUCTURAL_EXIT_NOOP_REASONS = frozenset(
    {
        "duplicate_learned_exit_label",
        "missing_parent_payoff_geometry",
        "no_loss_report",
        "no_parent_returns",
    }
)
_STRUCTURAL_EXIT_NOOP_PREFIXES = (
    "edge_debt_too_negative_for_exit_child:",
    "insufficient_parent_payoff_samples:",
    "reward_risk_below_floor:",
)
_RECENT_RECERT_BLOCKER_ACTIONS = frozenset(
    {
        "inspect_recert_backtest_no_oos_evidence_keep_live_blocked",
        "wait_for_recert_backtest_cooldown_keep_live_blocked",
        "live_blocked_recert_debt_no_refresh",
    }
)
_RECENT_RECERT_BLOCKER_REASONS = frozenset(
    {
        "recent_recert_backtest_cooldown",
        "recert_backtest_refresh_already_open",
        "no_recert_refresh_needed",
    }
)


def _user_trade_filter(query, user_id: int | None):
    if user_id is not None:
        return query.filter(Trade.user_id == user_id)
    return query.filter(Trade.user_id.is_(None))


def _monitored_open_trades_query(db: Session, user_id: int | None):
    q = db.query(Trade).filter(
        Trade.status == "open",
        Trade.entry_price > 0,
    )
    return _user_trade_filter(q, user_id)


def _monitored_live_trades_with_suppressed(
    db: Session,
    user_id: int | None,
) -> tuple[list[Trade], list[dict[str, Any]]]:
    """Return monitor-eligible trades after broker-position truth filtering.

    ``Trade`` rows are management envelopes. For live broker-backed positions,
    the broker-position identity row is the inventory truth. If a Robinhood
    position row is already closed/zero/missing past the grace window, the
    Monitoring tab must not keep rendering it as an active card.
    """
    rows = (
        _monitored_open_trades_query(db, user_id)
        .order_by(Trade.entry_date.desc())
        .all()
    )
    return filter_broker_stale_open_trades(db, rows)


def _fraction_to_health_percent(score: float | None) -> float | None:
    """Pattern monitor stores health_score on 0–1 (condition match ratio). UI uses 0–100."""
    if score is None:
        return None
    try:
        x = float(score)
    except (TypeError, ValueError):
        return None
    if x <= 1.5:
        return max(0.0, min(100.0, round(x * 100.0, 2)))
    return max(0.0, min(100.0, round(x, 2)))


def _fraction_to_delta_points(delta: float | None) -> float | None:
    """health_delta is change on same 0–1 scale → points on 0–100 health scale."""
    if delta is None:
        return None
    try:
        x = float(delta)
    except (TypeError, ValueError):
        return None
    if abs(x) <= 1.5:
        return round(x * 100.0, 3)
    return round(x, 3)


def _quote_price(q: dict[str, Any] | None) -> float | None:
    if not q:
        return None
    p = q.get("price") if q.get("price") is not None else q.get("last_price")
    if p is None:
        return None
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def _serialize_decision(d: PatternMonitorDecision) -> dict[str, Any]:
    snap = d.conditions_snapshot if isinstance(d.conditions_snapshot, dict) else {}
    health_source = snap.get("monitor_health_source") if snap else None
    decision_source = d.decision_source
    llm_reasoning = d.llm_reasoning
    llm_unavailable = False
    if (
        str(llm_reasoning or "").strip().lower() in {"llm unavailable", "llm unavailable."}
        and (d.llm_confidence is None or float(d.llm_confidence or 0.0) == 0.0)
    ):
        llm_reasoning = None
        llm_unavailable = True
        if decision_source == "llm":
            decision_source = "llm_unavailable"
    if health_source and health_source != "static_conditions":
        health_label = "Setup health"
        health_hint = "Using live trade-plan and setup-vitals health; entry-condition retention is in details"
    else:
        health_label = "Pattern health"
        health_hint = "Share of evaluable pattern conditions still satisfied"
    return {
        "id": d.id,
        "trade_id": d.trade_id,
        "breakout_alert_id": d.breakout_alert_id,
        "scan_pattern_id": d.scan_pattern_id,
        "health_score": json_safe(d.health_score),
        "health_score_pct": json_safe(_fraction_to_health_percent(d.health_score)),
        "health_source": health_source,
        "health_label": health_label,
        "health_hint": health_hint,
        "health_delta": json_safe(d.health_delta) if d.health_delta is not None else None,
        "health_delta_pts": json_safe(_fraction_to_delta_points(d.health_delta)),
        "conditions_snapshot": json_safe(d.conditions_snapshot) if d.conditions_snapshot else None,
        "action": d.action,
        "old_stop": json_safe(d.old_stop) if d.old_stop is not None else None,
        "new_stop": json_safe(d.new_stop) if d.new_stop is not None else None,
        "old_target": json_safe(d.old_target) if d.old_target is not None else None,
        "new_target": json_safe(d.new_target) if d.new_target is not None else None,
        "llm_confidence": json_safe(d.llm_confidence) if d.llm_confidence is not None else None,
        "llm_reasoning": llm_reasoning,
        "llm_unavailable": llm_unavailable or decision_source == "llm_unavailable",
        "mechanical_action": d.mechanical_action,
        "mechanical_stop": json_safe(d.mechanical_stop) if d.mechanical_stop is not None else None,
        "mechanical_target": json_safe(d.mechanical_target) if d.mechanical_target is not None else None,
        "decision_source": decision_source,
        "price_at_decision": json_safe(d.price_at_decision) if d.price_at_decision is not None else None,
        "price_after_1h": json_safe(d.price_after_1h) if d.price_after_1h is not None else None,
        "price_after_4h": json_safe(d.price_after_4h) if d.price_after_4h is not None else None,
        "was_beneficial": d.was_beneficial,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


IMMINENT_SHADOW_OBSERVATION_SIGNAL_LANES = frozenset({
    "shadow_near_miss",
    "hard_recert_shadow",
    "equity_session_shadow",
})


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _canonical_imminent_asset_class(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"stock", "stocks", "equity", "equities"}:
        return "stock"
    if raw in {"crypto", "cryptocurrency", "coin", "coinbase_spot"}:
        return "crypto"
    if raw in {"option", "options"}:
        return "options"
    return "all"


def _autotrader_snapshot(run: AutoTraderRun | None) -> dict[str, Any]:
    snap = getattr(run, "rule_snapshot", None) if run is not None else None
    return snap if isinstance(snap, dict) else {}


def _entry_edge_snapshot(run: AutoTraderRun | None) -> dict[str, Any]:
    snap = _autotrader_snapshot(run)
    edge = snap.get("entry_edge")
    return edge if isinstance(edge, dict) else {}


def _alert_signal_lane(alert: BreakoutAlert) -> str | None:
    snap = alert.indicator_snapshot if isinstance(alert.indicator_snapshot, dict) else {}
    scorecard = snap.get("imminent_scorecard") if isinstance(snap, dict) else {}
    if not isinstance(scorecard, dict):
        return None
    lane = str(scorecard.get("signal_lane") or "").strip().lower()
    return lane or None


def _snapshot_text(run: AutoTraderRun | None, key: str) -> str | None:
    value = _autotrader_snapshot(run).get(key)
    text = str(value or "").strip()
    return text or None


def _snapshot_float(run: AutoTraderRun | None, key: str) -> float | None:
    return _safe_float(_autotrader_snapshot(run).get(key))


def _snapshot_bool(run: AutoTraderRun | None, key: str) -> bool | None:
    value = _autotrader_snapshot(run).get(key)
    return value if isinstance(value, bool) else None


def _edge_float(edge: dict[str, Any], key: str) -> float | None:
    return _safe_float(edge.get(key))


def _slippage_reprice_next_action(run: AutoTraderRun | None) -> str | None:
    reason = str(getattr(run, "reason", "") or "").strip().lower() if run else ""
    if reason not in {"missed_entry_slippage", "slippage_reprice_cooldown"}:
        return None
    if reason == "slippage_reprice_cooldown":
        return "wait_for_reprice_cooldown_or_fresh_quote"
    positive = _snapshot_bool(run, "slippage_reprice_positive_edge")
    if positive is True:
        return "retry_if_current_quote_still_positive_after_costs"
    if positive is False:
        return "wait_for_fresh_entry_or_tighter_price"
    if _snapshot_text(run, "slippage_reprice_error"):
        return "inspect_reprice_diagnostic_error"
    return "wait_for_fresh_entry_or_reprice"


def _imminent_blocker_category(
    run: AutoTraderRun | None,
    pat: ScanPattern | None,
    signal_lane: str | None,
) -> str:
    lifecycle = (getattr(pat, "lifecycle_stage", None) or "").strip().lower()
    recert_required = bool(getattr(pat, "recert_required", False))
    if run is None:
        if (
            lifecycle in {"live", "promoted", "pilot_promoted"}
            and not recert_required
            and signal_lane not in IMMINENT_SHADOW_OBSERVATION_SIGNAL_LANES
        ):
            return "live_eligible_candidate"
        return "pending_autotrader"

    reason = str(getattr(run, "reason", "") or "").strip().lower()
    decision = str(getattr(run, "decision", "") or "").strip().lower()
    edge = _entry_edge_snapshot(run)
    expected_net = _edge_float(edge, "expected_net_pct")
    positive_edge = expected_net is not None and expected_net > 0.0

    if decision == "placed":
        return "placed"
    if decision == "error":
        return "autotrader_execution_error"
    if reason == "non_positive_expected_edge":
        return "negative_expected_edge"
    if reason in {"missed_entry_slippage", "slippage_reprice_cooldown"}:
        return "missed_entry_slippage"
    if reason == "llm_unavailable":
        return "llm_provider_unavailable"
    if reason == "llm_not_viable":
        return "llm_revalidation_block"
    if (
        signal_lane == "hard_recert_shadow"
        or reason == "pattern_recert_required"
        or (recert_required and reason == "selector:shadow_observation_signal_lane")
    ):
        return "positive_edge_recert_debt" if positive_edge else "recert_required"
    if (
        signal_lane in IMMINENT_SHADOW_OBSERVATION_SIGNAL_LANES
        or reason in {
            "selector:shadow_observation_signal_lane",
            "selector:shadow_promoted_pattern_eval",
        }
    ):
        return "positive_edge_shadow_only" if positive_edge else "shadow_observation"
    if reason.startswith("broker:") or "adapter" in reason or "venue_" in reason:
        return "broker_execution_reject"
    if positive_edge and lifecycle in {"live", "promoted", "pilot_promoted"}:
        return "positive_edge_other_block"
    return "other"


def _imminent_next_action(category: str) -> str:
    return {
        "negative_expected_edge": "collect_shadow_evidence_and_evolve_pattern",
        "positive_edge_shadow_only": "shadow_collecting_ev_before_live",
        "shadow_observation": "continue_observation_only",
        "positive_edge_recert_debt": "complete_recert_before_live",
        "recert_required": "complete_recert_before_live",
        "missed_entry_slippage": "wait_for_fresh_entry_or_reprice",
        "broker_execution_reject": "fix_execution_lane",
        "llm_provider_unavailable": "restore_llm_provider_or_disable_revalidation",
        "llm_revalidation_block": "review_llm_revalidation_reason",
        "autotrader_execution_error": "inspect_autotrader_exception",
        "live_eligible_candidate": "await_autotrader_processing",
        "placed": "already_placed",
        "pending_autotrader": "await_autotrader_processing",
        "positive_edge_other_block": "inspect_secondary_gate",
    }.get(category, "inspect_secondary_gate")


def _safe_int(value: Any) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out


def _structural_exit_noop_reason(reason: Any) -> bool:
    value = str(reason or "").strip().lower()
    return value in _STRUCTURAL_EXIT_NOOP_REASONS or any(
        value.startswith(prefix) for prefix in _STRUCTURAL_EXIT_NOOP_PREFIXES
    )


def _recommended_work_status(
    db: Session,
    supply: dict[str, Any],
) -> dict[str, Any]:
    event_type = str(supply.get("recommended_work_event") or "").strip()
    pid = _safe_int(supply.get("scan_pattern_id"))
    if not event_type:
        return {"event_type": None, "actionable": None, "blocker": None}
    if pid is None or pid <= 0:
        return {"event_type": event_type, "actionable": True, "blocker": None}

    minutes = _safe_int(
        getattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360)
    )
    if minutes is None or minutes <= 0:
        return {"event_type": event_type, "actionable": True, "blocker": None}
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)

    if event_type == EXIT_VARIANT_REFRESH:
        fingerprint = str(supply.get("evidence_fingerprint") or "")
        rows = (
            db.query(BrainWorkEvent)
            .filter(BrainWorkEvent.event_kind == "outcome")
            .filter(BrainWorkEvent.event_type == EXIT_VARIANT_DIAGNOSTIC)
            .filter(BrainWorkEvent.created_at >= cutoff)
            .filter(BrainWorkEvent.payload["scan_pattern_id"].astext == str(pid))
            .order_by(BrainWorkEvent.created_at.desc(), BrainWorkEvent.id.desc())
            .limit(20)
            .all()
        )
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if _safe_int(payload.get("created_count")) != 0:
                continue
            same_fingerprint = (
                bool(fingerprint)
                and str(payload.get("evidence_fingerprint") or "") == fingerprint
            )
            if same_fingerprint or _structural_exit_noop_reason(payload.get("skip_reason")):
                return {
                    "event_type": event_type,
                    "actionable": False,
                    "blocker": "recent_exit_noop_diagnostic",
                    "blocker_detail": payload.get("skip_reason"),
                }

    if event_type == RECERT_RESCUE_REFRESH:
        rows = (
            db.query(BrainWorkEvent)
            .filter(BrainWorkEvent.event_kind == "outcome")
            .filter(BrainWorkEvent.event_type == RECERT_RESCUE_DIAGNOSTIC)
            .filter(BrainWorkEvent.created_at >= cutoff)
            .filter(BrainWorkEvent.payload["scan_pattern_id"].astext == str(pid))
            .order_by(BrainWorkEvent.created_at.desc(), BrainWorkEvent.id.desc())
            .limit(20)
            .all()
        )
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            action = str(payload.get("recommended_next_action") or "").strip().lower()
            if action in _RECENT_RECERT_BLOCKER_ACTIONS:
                return {
                    "event_type": event_type,
                    "actionable": False,
                    "blocker": "recent_recert_blocker_diagnostic",
                    "blocker_detail": action,
                }
            refresh = payload.get("recert_backtest_refresh")
            if isinstance(refresh, dict):
                reason = str(refresh.get("reason") or "").strip().lower()
                if reason in _RECENT_RECERT_BLOCKER_REASONS:
                    return {
                        "event_type": event_type,
                        "actionable": False,
                        "blocker": "recent_recert_blocker_diagnostic",
                        "blocker_detail": reason,
                    }

    return {"event_type": event_type, "actionable": True, "blocker": None}


def _empty_imminent_summary() -> dict[str, int]:
    return {
        "total": 0,
        "negative_expected_edge": 0,
        "positive_edge_shadow_only": 0,
        "positive_edge_recert_debt": 0,
        "missed_entry_slippage": 0,
        "broker_execution_rejects": 0,
        "llm_provider_unavailable": 0,
        "llm_revalidation_blocks": 0,
        "autotrader_execution_errors": 0,
        "live_eligible_candidates": 0,
        "other": 0,
    }


def _bump_imminent_summary(summary: dict[str, int], category: str) -> None:
    summary["total"] = int(summary.get("total", 0)) + 1
    if category in {
        "negative_expected_edge",
        "positive_edge_shadow_only",
        "positive_edge_recert_debt",
        "missed_entry_slippage",
    }:
        summary[category] = int(summary.get(category, 0)) + 1
    elif category == "broker_execution_reject":
        summary["broker_execution_rejects"] = (
            int(summary.get("broker_execution_rejects", 0)) + 1
        )
    elif category == "llm_provider_unavailable":
        summary["llm_provider_unavailable"] = (
            int(summary.get("llm_provider_unavailable", 0)) + 1
        )
    elif category == "llm_revalidation_block":
        summary["llm_revalidation_blocks"] = (
            int(summary.get("llm_revalidation_blocks", 0)) + 1
        )
    elif category == "autotrader_execution_error":
        summary["autotrader_execution_errors"] = (
            int(summary.get("autotrader_execution_errors", 0)) + 1
        )
    elif category == "live_eligible_candidate":
        summary["live_eligible_candidates"] = (
            int(summary.get("live_eligible_candidates", 0)) + 1
        )
    else:
        summary["other"] = int(summary.get("other", 0)) + 1


@router.get("/monitor/active")
@router.get("/active-setups")
def api_monitor_active(
    request: Request,
    db: Session = Depends(get_db),
):
    """Open trades linked to alerts (pattern monitor scope) + latest decisions and quotes."""
    ctx = get_identity_ctx(request, db)
    user_id = ctx["user_id"]

    trades, suppressed_stale_trades = _monitored_live_trades_with_suppressed(
        db, user_id,
    )
    if not trades:
        return JSONResponse(
            {
                "ok": True,
                "summary": {
                    "active_count": 0,
                    "avg_health": None,
                    "actions_today": 0,
                    "benefit_rate": None,
                    "last_check": None,
                    "suppressed_stale_count": len(suppressed_stale_trades),
                },
                "setups": [],
                "suppressed_stale_trades": json_safe(suppressed_stale_trades),
            }
        )

    trade_ids = [t.id for t in trades]
    alert_ids = [t.related_alert_id for t in trades if t.related_alert_id]
    alerts_by_id: dict[int, BreakoutAlert] = {}
    if alert_ids:
        for ba in db.query(BreakoutAlert).filter(BreakoutAlert.id.in_(alert_ids)).all():
            alerts_by_id[ba.id] = ba

    pattern_ids = {t.scan_pattern_id for t in trades if t.scan_pattern_id}
    patterns: dict[int, ScanPattern] = {}
    if pattern_ids:
        for p in db.query(ScanPattern).filter(ScanPattern.id.in_(pattern_ids)).all():
            patterns[p.id] = p

    all_decisions = (
        db.query(PatternMonitorDecision)
        .filter(PatternMonitorDecision.trade_id.in_(trade_ids))
        .order_by(PatternMonitorDecision.created_at.desc())
        .all()
    )
    by_trade: dict[int, list[PatternMonitorDecision]] = {}
    for d in all_decisions:
        by_trade.setdefault(d.trade_id, []).append(d)

    now = datetime.utcnow()
    day_ago = now - timedelta(hours=24)
    actions_today = sum(
        1
        for d in all_decisions
        if d.created_at and d.created_at >= day_ago and d.action and d.action != "hold"
    )

    beneficial = [d for d in all_decisions if d.was_beneficial is not None]
    if beneficial:
        benefit_rate = sum(1 for d in beneficial if d.was_beneficial) / len(beneficial)
    else:
        benefit_rate = None

    last_check = max((d.created_at for d in all_decisions if d.created_at), default=None)

    try:
        from ...services.trading.autopilot_scope import is_option_trade
    except Exception:
        def is_option_trade(_trade: Trade) -> bool:  # type: ignore[no-redef]
            return False

    tickers = list({t.ticker.upper() for t in trades if not is_option_trade(t)})
    quotes_map: dict[str, dict[str, Any]] = {}
    if tickers:
        try:
            quotes_map = ts.fetch_quotes_batch(tickers, allow_provider_fallback=True)
        except Exception:
            logger.warning("[monitor] fetch_quotes_batch failed", exc_info=True)

    setups: list[dict[str, Any]] = []
    health_scores: list[float] = []
    try:
        from ...services.trading.broker_quotes import broker_quote_for_trade
    except Exception:
        broker_quote_for_trade = None  # type: ignore[assignment]

    for trade in trades:
        decs = by_trade.get(trade.id, [])
        latest = decs[0] if decs else None
        if latest is not None:
            hpct = _fraction_to_health_percent(latest.health_score)
            if hpct is not None:
                health_scores.append(float(hpct))

        pat = patterns.get(trade.scan_pattern_id) if trade.scan_pattern_id else None
        if pat is None and latest and latest.scan_pattern_id:
            pid = latest.scan_pattern_id
            if pid not in patterns:
                p2 = db.query(ScanPattern).filter(ScanPattern.id == pid).first()
                if p2:
                    patterns[pid] = p2
                    pat = p2

        trade_is_option = is_option_trade(trade)
        q = None
        if broker_quote_for_trade is not None and (
            (trade.broker_source or "").strip() or trade_is_option
        ):
            q = broker_quote_for_trade(trade, purpose="display")
        if (not q or q.get("price") is None) and not trade_is_option:
            q = quotes_map.get(trade.ticker.upper()) or quotes_map.get(trade.ticker)
        cur = _quote_price(q)
        broker_metrics = (
            broker_position_display_metrics(db, trade)
            if not trade_is_option
            else None
        ) or {}
        display_entry = broker_metrics.get("entry_price") or trade.entry_price
        display_quantity = broker_metrics.get("quantity") or trade.quantity
        entry = float(display_entry)
        pnl_pct = None
        if cur is not None and entry:
            if trade.direction == "short":
                pnl_pct = (entry - cur) / entry * 100.0
            else:
                pnl_pct = (cur - entry) / entry * 100.0

        recent = [_serialize_decision(x) for x in decs[:5]]

        eff_sl = trade.stop_loss
        eff_tp = trade.take_profit
        linked = alerts_by_id.get(trade.related_alert_id) if trade.related_alert_id else None
        if linked is not None:
            if eff_tp is None and linked.target_price is not None:
                eff_tp = float(linked.target_price)
            if eff_sl is None and linked.stop_loss is not None:
                eff_sl = float(linked.stop_loss)
        if eff_tp is None and latest is not None and latest.new_target is not None:
            eff_tp = float(latest.new_target)

        plan_label = pat.name if pat else None
        if plan_label is None and (eff_sl is not None or eff_tp is not None):
            plan_label = "Position plan (AI / manual)"

        exec_meta = describe_trade_execution_state(
            trade,
            latest_monitor_action=(latest.action if latest is not None else None),
        )

        setups.append(
            {
                "trade_id": trade.id,
                "ticker": trade.ticker,
                "direction": trade.direction,
                "pattern_name": pat.name if pat else None,
                "plan_label": plan_label,
                "pattern_id": trade.scan_pattern_id or (latest.scan_pattern_id if latest else None),
                "timeframe": pat.timeframe if pat else None,
                "entry_price": json_safe(display_entry),
                "quantity": json_safe(display_quantity),
                "stop_loss": json_safe(eff_sl) if eff_sl is not None else None,
                "take_profit": json_safe(eff_tp) if eff_tp is not None else None,
                "entry_date": trade.entry_date.isoformat() if trade.entry_date else None,
                "current_price": json_safe(cur) if cur is not None else None,
                "quote_source": q.get("source") if isinstance(q, dict) else None,
                "pnl_pct": json_safe(pnl_pct) if pnl_pct is not None else None,
                "broker_truth_entry_price": json_safe(broker_metrics.get("entry_price")),
                "broker_truth_quantity": json_safe(broker_metrics.get("quantity")),
                "broker_truth_position_id": broker_metrics.get("position_id"),
                "broker_truth_current_envelope_id": broker_metrics.get("current_envelope_id"),
                "broker_truth_metrics_source": broker_metrics.get("source"),
                "latest_decision": _serialize_decision(latest) if latest else None,
                "decision_count": len(decs),
                "recent_decisions": recent,
                "execution_state": exec_meta.get("execution_state"),
                "execution_label": exec_meta.get("execution_label"),
                "execution_reason": exec_meta.get("execution_reason"),
                "pending_exit_status": exec_meta.get("pending_exit_status"),
                "pending_exit_order_id": exec_meta.get("pending_exit_order_id"),
                "pending_exit_limit_price": json_safe(exec_meta.get("pending_exit_limit_price")),
                "next_eligible_session_at": exec_meta.get("next_eligible_session_at"),
            }
        )

    avg_health = sum(health_scores) / len(health_scores) if health_scores else None

    def _health_sort_key(s: dict[str, Any]) -> float:
        ld = s.get("latest_decision")
        if not ld:
            return 999.0
        p = ld.get("health_score_pct")
        if p is not None:
            try:
                return float(p)
            except (TypeError, ValueError):
                pass
        if ld.get("health_score") is None:
            return 999.0
        try:
            alt = _fraction_to_health_percent(float(ld["health_score"]))
            return float(alt) if alt is not None else 999.0
        except (TypeError, ValueError):
            return 999.0

    setups.sort(key=_health_sort_key)

    return JSONResponse(
        {
            "ok": True,
            "summary": {
                "active_count": len(trades),
                "avg_health": json_safe(avg_health) if avg_health is not None else None,
                "actions_today": actions_today,
                "benefit_rate": json_safe(benefit_rate) if benefit_rate is not None else None,
                "last_check": last_check.isoformat() if last_check else None,
                "suppressed_stale_count": len(suppressed_stale_trades),
            },
            "setups": json_safe(setups),
            "suppressed_stale_trades": json_safe(suppressed_stale_trades),
        }
    )


def _monitored_open_trades(db: Session, user_id: int | None) -> list[Trade]:
    trades, _suppressed = _monitored_live_trades_with_suppressed(db, user_id)
    return trades


@router.get("/monitor/decisions")
@router.get("/active-setups/decisions")
def api_monitor_decisions(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: str | None = Query(None, description="Filter by action e.g. hold, tighten_stop"),
):
    """Paginated pattern monitor decisions for the current user's trades."""
    ctx = get_identity_ctx(request, db)
    user_id = ctx["user_id"]

    q = db.query(PatternMonitorDecision).join(
        Trade, Trade.id == PatternMonitorDecision.trade_id,
    )
    q = _user_trade_filter(q, user_id)
    if action:
        q = q.filter(PatternMonitorDecision.action == action.strip())

    total = q.count()
    rows = (
        q.order_by(PatternMonitorDecision.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    t_ids = list({d.trade_id for d in rows})
    trade_map: dict[int, Trade] = {}
    if t_ids:
        tq = db.query(Trade).filter(Trade.id.in_(t_ids))
        trade_map = {t.id: t for t in tq.all()}

    out = []
    for d in rows:
        tr = trade_map.get(d.trade_id)
        out.append(
            {
                **_serialize_decision(d),
                "ticker": tr.ticker if tr else None,
                "direction": tr.direction if tr else None,
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "total": total,
            "limit": limit,
            "offset": offset,
            "decisions": json_safe(out),
        }
    )


@router.post("/monitor/run")
@router.post("/active-setups/run")
def api_monitor_run(
    request: Request,
    db: Session = Depends(get_db),
):
    """Run one monitor cycle: pattern-linked trades and plan-level (stop/target only) positions."""
    ctx = get_identity_ctx(request, db)
    user_id = ctx["user_id"]

    trades = _monitored_open_trades(db, user_id)
    if not trades:
        return JSONResponse(
            {"ok": True, "message": "No monitored open positions", "evaluated": 0},
        )

    try:
        summary = run_pattern_position_monitor_for_trades(
            db, trades, dry_run=False, event_driven=True,
        )
    except Exception as e:
        logger.exception("[monitor] run failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({"ok": True, **summary})


@router.get("/monitor/imminent-alerts")
def api_monitor_imminent_alerts(
    request: Request,
    db: Session = Depends(get_db),
    hours: int = Query(72, ge=1, le=168, description="Look-back window in hours"),
):
    """Imminent breakout alerts that are still viable (pending outcome) and not yet acted on."""
    ctx = get_identity_ctx(request, db)
    user_id = ctx["user_id"]

    cutoff = datetime.utcnow() - timedelta(hours=hours)

    user_filter = Trade.user_id == user_id if user_id is not None else Trade.user_id.is_(None)
    actioned_subq = exists().where(
        and_(
            Trade.related_alert_id == BreakoutAlert.id,
            Trade.status.in_(["open", "closed"]),
            user_filter,
        )
    )

    q = (
        db.query(BreakoutAlert)
        .filter(
            BreakoutAlert.alert_tier == "pattern_imminent",
            BreakoutAlert.outcome == "pending",
            BreakoutAlert.alerted_at >= cutoff,
        )
        .filter(~actioned_subq)
    )
    if user_id is not None:
        # Imminent scan rows often have user_id NULL when brain_default_user_id is unset
        # (Telegram still dispatches). Surface those global brain alerts in Monitor too.
        q = q.filter(or_(BreakoutAlert.user_id == user_id, BreakoutAlert.user_id.is_(None)))
    else:
        q = q.filter(BreakoutAlert.user_id.is_(None))

    alerts = q.order_by(BreakoutAlert.alerted_at.desc()).limit(30).all()

    pat_ids = {a.scan_pattern_id for a in alerts if a.scan_pattern_id}
    patterns: dict[int, ScanPattern] = {}
    if pat_ids:
        for p in db.query(ScanPattern).filter(ScanPattern.id.in_(pat_ids)).all():
            patterns[p.id] = p
    edge_supply_by_pattern_asset: dict[tuple[int, str], dict[str, Any]] = {}
    if pat_ids:
        try:
            snapshots = latest_edge_reliability_snapshot_slices(
                db,
                scan_pattern_ids=pat_ids,
            )
            for key, row in snapshots.items():
                edge_supply_by_pattern_asset[key] = annotate_cash_deployment_row(
                    db,
                    row,
                    user_id=user_id,
                )
        except Exception:
            logger.debug("[monitor] cached edge supply diagnostics failed", exc_info=True)
    alert_ids = [int(a.id) for a in alerts]
    runs_by_alert: dict[int, AutoTraderRun] = {}
    if alert_ids:
        runs = (
            db.query(AutoTraderRun)
            .filter(AutoTraderRun.breakout_alert_id.in_(alert_ids))
            .order_by(AutoTraderRun.breakout_alert_id.asc(), AutoTraderRun.created_at.desc())
            .all()
        )
        for run in runs:
            aid = int(run.breakout_alert_id)
            if aid not in runs_by_alert:
                runs_by_alert[aid] = run

    items: list[dict[str, Any]] = []
    summary = _empty_imminent_summary()
    for a in alerts:
        pat = patterns.get(a.scan_pattern_id) if a.scan_pattern_id else None
        run = runs_by_alert.get(int(a.id))
        lifecycle = (pat.lifecycle_stage if pat else None) or None
        edge = _entry_edge_snapshot(run)
        signal_lane = (
            _snapshot_text(run, "paper_observation_signal_lane")
            or _alert_signal_lane(a)
        )
        blocker_category = _imminent_blocker_category(run, pat, signal_lane)
        _bump_imminent_summary(summary, blocker_category)
        supply: dict[str, Any] = {}
        if a.scan_pattern_id:
            pid = int(a.scan_pattern_id)
            asset_key = _canonical_imminent_asset_class(a.asset_type)
            supply = (
                edge_supply_by_pattern_asset.get((pid, asset_key))
                or edge_supply_by_pattern_asset.get((pid, "all"))
                or {}
            )
        work_status = _recommended_work_status(db, supply)
        items.append(
            {
                "id": a.id,
                "ticker": a.ticker,
                "asset_type": a.asset_type,
                "score": json_safe(a.score_at_alert),
                "price_at_alert": json_safe(a.price_at_alert),
                "entry_price": json_safe(a.entry_price),
                "stop_loss": json_safe(a.stop_loss),
                "target_price": json_safe(a.target_price),
                "alerted_at": a.alerted_at.isoformat() if a.alerted_at else None,
                "timeframe": a.timeframe,
                "regime": a.regime_at_alert,
                "pattern_id": a.scan_pattern_id,
                "pattern_name": pat.name if pat else None,
                "lifecycle_stage": lifecycle,
                "broker_eligible": lifecycle in ("live", "promoted", "pilot_promoted"),
                "recert_required": (
                    bool(getattr(pat, "recert_required", False)) if pat else False
                ),
                "recert_reason": getattr(pat, "recert_reason", None) if pat else None,
                "autotrader_decision": run.decision if run else None,
                "autotrader_reason": run.reason if run else None,
                "autotrader_processed_at": (
                    run.created_at.isoformat() if run and run.created_at else None
                ),
                "entry_edge_expected_net_pct": json_safe(
                    _edge_float(edge, "expected_net_pct")
                ),
                "entry_edge_probability": json_safe(
                    _edge_float(edge, "probability")
                ),
                "entry_edge_breakeven_probability": json_safe(
                    _edge_float(edge, "breakeven_probability")
                ),
                "entry_edge_probability_source": edge.get("probability_source"),
                "entry_slippage_pct": json_safe(
                    _snapshot_float(run, "entry_slippage_pct")
                ),
                "entry_slippage_signed_pct": json_safe(
                    _snapshot_float(run, "entry_slippage_signed_pct")
                ),
                "entry_slippage_direction": _snapshot_text(
                    run,
                    "entry_slippage_direction",
                ),
                "slippage_tolerance_pct": json_safe(
                    _snapshot_float(run, "slippage_tolerance_pct")
                ),
                "slippage_source": _snapshot_text(run, "slippage_source"),
                "slippage_reprice_original_entry_price": json_safe(
                    _snapshot_float(run, "slippage_reprice_original_entry_price")
                ),
                "slippage_reprice_current_price": json_safe(
                    _snapshot_float(run, "slippage_reprice_current_price")
                ),
                "slippage_reprice_expected_net_pct": json_safe(
                    _snapshot_float(run, "slippage_reprice_expected_net_pct")
                ),
                "slippage_reprice_positive_edge": _snapshot_bool(
                    run,
                    "slippage_reprice_positive_edge",
                ),
                "slippage_reprice_positive_edge_enabled": _snapshot_bool(
                    run,
                    "slippage_reprice_positive_edge_enabled",
                ),
                "slippage_reprice_max_pct": json_safe(
                    _snapshot_float(run, "slippage_reprice_max_pct")
                ),
                "slippage_reprice_accepted": _snapshot_bool(
                    run,
                    "slippage_reprice_accepted",
                ),
                "slippage_reprice_edge_reason": _snapshot_text(
                    run,
                    "slippage_reprice_edge_reason",
                ),
                "slippage_reprice_error": _snapshot_text(
                    run,
                    "slippage_reprice_error",
                ),
                "slippage_reprice_cooldown_active": _snapshot_bool(
                    run,
                    "slippage_reprice_cooldown_active",
                ),
                "slippage_reprice_cooldown_count": json_safe(
                    _snapshot_float(run, "slippage_reprice_cooldown_count")
                ),
                "slippage_reprice_cooldown_threshold": json_safe(
                    _snapshot_float(run, "slippage_reprice_cooldown_threshold")
                ),
                "slippage_reprice_cooldown_minutes": json_safe(
                    _snapshot_float(run, "slippage_reprice_cooldown_minutes")
                ),
                "slippage_reprice_cooldown_until": _snapshot_text(
                    run,
                    "slippage_reprice_cooldown_until",
                ),
                "slippage_reprice_cooldown_reason": _snapshot_text(
                    run,
                    "slippage_reprice_cooldown_reason",
                ),
                "slippage_reprice_next_action": _slippage_reprice_next_action(run),
                "autotrader_error_type": _snapshot_text(run, "error_type"),
                "autotrader_error_classification": _snapshot_text(
                    run,
                    "error_classification",
                ),
                "autotrader_error_phase": _snapshot_text(run, "error_phase"),
                "paper_observation_signal_lane": signal_lane,
                "autotrader_blocker_category": blocker_category,
                "autotrader_next_action": _imminent_next_action(blocker_category),
                "calibrated_ev_pct": json_safe(supply.get("calibrated_ev_pct")),
                "calibrated_ev_after_cost_pct": json_safe(
                    supply.get("calibrated_ev_after_cost_pct")
                ),
                "realized_ev_pct": json_safe(supply.get("realized_ev_pct")),
                "ev_calibration_error": json_safe(supply.get("ev_calibration_error")),
                "brier_score": json_safe(supply.get("brier_score")),
                "closed_evidence_count": supply.get("closed_evidence_count"),
                "paper_live_gap_pct": json_safe(supply.get("paper_live_gap_pct")),
                "graduation_blocker": supply.get("graduation_blocker"),
                "recommended_work_event": work_status.get("event_type"),
                "recommended_work_actionable": work_status.get("actionable"),
                "recommended_work_blocker": work_status.get("blocker"),
                "recommended_work_blocker_detail": work_status.get("blocker_detail"),
                "cash_deployment_rank": supply.get("cash_deployment_rank"),
                "edge_reliability_snapshot_event_id": supply.get("snapshot_event_id"),
                "edge_reliability_snapshot_at": supply.get("snapshot_created_at"),
                "allocation_score": json_safe(supply.get("allocation_score")),
                "max_safe_notional": json_safe(supply.get("max_safe_notional")),
                "venue_readiness": supply.get("venue_readiness"),
                "correlation_bucket": supply.get("correlation_bucket"),
                "exposure_blocker": supply.get("exposure_blocker"),
                "execution_blocker": supply.get("execution_blocker"),
                "recert_blocker": supply.get("recert_blocker"),
                "broker_truth_status": supply.get("broker_truth_status"),
                "broker_truth_reason": supply.get("broker_truth_reason"),
                "stale_broker_position": supply.get("stale_broker_position"),
                "stale_reconciled_at": supply.get("stale_reconciled_at"),
                "trade_plan": a.trade_plan,
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "alerts": json_safe(items),
            "total": len(items),
            "summary": summary,
        }
    )


@router.get("/monitor/edge-supply")
def api_monitor_edge_supply(
    request: Request,
    db: Session = Depends(get_db),
    window_days: int = Query(30, ge=1, le=120),
    limit: int = Query(25, ge=1, le=100),
    fresh: bool = Query(False, description="Recompute diagnostics instead of reading cached snapshots"),
):
    """Pattern-level edge reliability and live-candidate supply diagnostics."""
    ctx = get_identity_ctx(request, db)
    try:
        source = "computed" if fresh else "edge_reliability_snapshot"
        edge_reader = edge_supply_rows if fresh else edge_supply_snapshot_rows
        edge_rows = edge_reader(db, window_days=window_days, limit=limit)
        rows = [
            annotate_cash_deployment_row(db, row, user_id=ctx["user_id"])
            for row in edge_rows
        ]
        null_lineage = cash_deployment_null_lineage_candidates(
            db,
            window_days=window_days,
            limit=10,
        )
    except Exception as exc:
        logger.exception("[monitor] edge supply failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse(
        {
            "ok": True,
            "window_days": window_days,
            "data_source": source,
            "rows": json_safe(rows),
            "summary": json_safe(edge_supply_summary(rows)),
            "cash_deployment_summary": json_safe(cash_deployment_summary(rows)),
            "null_lineage_research_candidates": json_safe(null_lineage),
        }
    )


@router.get("/monitor/cash-deployment")
def api_monitor_cash_deployment(
    request: Request,
    db: Session = Depends(get_db),
    window_days: int = Query(30, ge=1, le=120),
    limit: int = Query(25, ge=1, le=100),
    fresh: bool = Query(False, description="Recompute diagnostics instead of reading cached snapshots"),
):
    """All-asset cash-deployment funnel: deployable candidates vs safe work."""
    ctx = get_identity_ctx(request, db)
    try:
        source = "computed" if fresh else "edge_reliability_snapshot"
        row_reader = cash_deployment_rows if fresh else cash_deployment_snapshot_rows
        rows = row_reader(
            db,
            user_id=ctx["user_id"],
            window_days=window_days,
            limit=limit,
        )
        null_lineage = cash_deployment_null_lineage_candidates(
            db,
            window_days=window_days,
            limit=10,
        )
    except Exception as exc:
        logger.exception("[monitor] cash deployment failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    summary = cash_deployment_summary(rows)
    if null_lineage:
        summary["needs_provenance"] = int(summary.get("needs_provenance", 0)) + len(null_lineage)
        cats = dict(summary.get("categories") or {})
        cats["needs_provenance"] = int(cats.get("needs_provenance", 0)) + len(null_lineage)
        summary["categories"] = cats

    return JSONResponse(
        {
            "ok": True,
            "window_days": window_days,
            "data_source": source,
            "rows": json_safe(rows),
            "summary": json_safe(summary),
            "null_lineage_research_candidates": json_safe(null_lineage),
        }
    )
