"""Pattern position monitor — active setups dashboard API."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from sqlalchemy import and_, or_

from ...deps import get_db, get_identity_ctx
from ...models.trading import BreakoutAlert, PatternMonitorDecision, ScanPattern, Trade
from ...services import trading_service as ts
from ...services.trading.pattern_position_monitor import run_pattern_position_monitor_for_trades
from ._utils import json_safe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading-monitor"])


def _user_trade_filter(query, user_id: int | None):
    if user_id is not None:
        return query.filter(Trade.user_id == user_id)
    return query.filter(Trade.user_id.is_(None))


def _monitored_open_trades_query(db: Session, user_id: int | None):
    q = db.query(Trade).filter(
        Trade.status == "open",
        Trade.entry_price > 0,
        or_(
            Trade.related_alert_id.isnot(None),
            Trade.broker_source.isnot(None),
            and_(
                Trade.related_alert_id.is_(None),
                or_(Trade.stop_loss.isnot(None), Trade.take_profit.isnot(None)),
            ),
        ),
    )
    return _user_trade_filter(q, user_id)


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
    return {
        "id": d.id,
        "trade_id": d.trade_id,
        "breakout_alert_id": d.breakout_alert_id,
        "scan_pattern_id": d.scan_pattern_id,
        "health_score": json_safe(d.health_score),
        "health_score_pct": json_safe(_fraction_to_health_percent(d.health_score)),
        "health_delta": json_safe(d.health_delta) if d.health_delta is not None else None,
        "health_delta_pts": json_safe(_fraction_to_delta_points(d.health_delta)),
        "conditions_snapshot": json_safe(d.conditions_snapshot) if d.conditions_snapshot else None,
        "action": d.action,
        "old_stop": json_safe(d.old_stop) if d.old_stop is not None else None,
        "new_stop": json_safe(d.new_stop) if d.new_stop is not None else None,
        "old_target": json_safe(d.old_target) if d.old_target is not None else None,
        "new_target": json_safe(d.new_target) if d.new_target is not None else None,
        "llm_confidence": json_safe(d.llm_confidence) if d.llm_confidence is not None else None,
        "llm_reasoning": d.llm_reasoning,
        "mechanical_action": d.mechanical_action,
        "mechanical_stop": json_safe(d.mechanical_stop) if d.mechanical_stop is not None else None,
        "mechanical_target": json_safe(d.mechanical_target) if d.mechanical_target is not None else None,
        "decision_source": d.decision_source,
        "price_at_decision": json_safe(d.price_at_decision) if d.price_at_decision is not None else None,
        "price_after_1h": json_safe(d.price_after_1h) if d.price_after_1h is not None else None,
        "price_after_4h": json_safe(d.price_after_4h) if d.price_after_4h is not None else None,
        "was_beneficial": d.was_beneficial,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


@router.get("/monitor/active")
@router.get("/active-setups")
def api_monitor_active(
    request: Request,
    db: Session = Depends(get_db),
):
    """Open trades linked to alerts (pattern monitor scope) + latest decisions and quotes."""
    ctx = get_identity_ctx(request, db)
    user_id = ctx["user_id"]

    trades = _monitored_open_trades(db, user_id)
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
                },
                "setups": [],
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

    tickers = list({t.ticker.upper() for t in trades})
    quotes_map: dict[str, dict[str, Any]] = {}
    try:
        quotes_map = ts.fetch_quotes_batch(tickers, allow_provider_fallback=True)
    except Exception:
        logger.warning("[monitor] fetch_quotes_batch failed", exc_info=True)

    setups: list[dict[str, Any]] = []
    health_scores: list[float] = []

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

        q = quotes_map.get(trade.ticker.upper()) or quotes_map.get(trade.ticker)
        cur = _quote_price(q)
        entry = float(trade.entry_price)
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

        setups.append(
            {
                "trade_id": trade.id,
                "ticker": trade.ticker,
                "direction": trade.direction,
                "pattern_name": pat.name if pat else None,
                "plan_label": plan_label,
                "pattern_id": trade.scan_pattern_id or (latest.scan_pattern_id if latest else None),
                "timeframe": pat.timeframe if pat else None,
                "entry_price": json_safe(trade.entry_price),
                "stop_loss": json_safe(eff_sl) if eff_sl is not None else None,
                "take_profit": json_safe(eff_tp) if eff_tp is not None else None,
                "entry_date": trade.entry_date.isoformat() if trade.entry_date else None,
                "current_price": json_safe(cur) if cur is not None else None,
                "pnl_pct": json_safe(pnl_pct) if pnl_pct is not None else None,
                "latest_decision": _serialize_decision(latest) if latest else None,
                "decision_count": len(decs),
                "recent_decisions": recent,
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
            },
            "setups": json_safe(setups),
        }
    )


def _monitored_open_trades(db: Session, user_id: int | None) -> list[Trade]:
    return _monitored_open_trades_query(db, user_id).order_by(Trade.entry_date.desc()).all()


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
