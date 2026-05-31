#!/usr/bin/env python
"""Read-only Phase 5AA active-setup runtime-adapter parity probe.

The active-setup card endpoint is risk-facing display. This probe compares
the current ``Trade`` ORM object path with candidate objects loaded from
``trading_management_envelopes`` before any endpoint conversion.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")

from app.db import SessionLocal  # noqa: E402
from app.models.trading import BreakoutAlert, PatternMonitorDecision, ScanPattern, Trade  # noqa: E402
from app.routers.trading_sub import monitor  # noqa: E402


ACTIVE_SETUP_FIELDS = (
    "trade_id",
    "ticker",
    "direction",
    "pattern_name",
    "plan_label",
    "pattern_id",
    "timeframe",
    "entry_price",
    "quantity",
    "stop_loss",
    "take_profit",
    "entry_date",
    "current_price",
    "quote_source",
    "pnl_pct",
    "broker_truth_entry_price",
    "broker_truth_quantity",
    "broker_truth_position_id",
    "broker_truth_current_envelope_id",
    "broker_truth_metrics_source",
    "latest_decision",
    "decision_count",
    "recent_decisions",
    "execution_state",
    "execution_label",
    "execution_reason",
    "pending_exit_status",
    "pending_exit_order_id",
    "pending_exit_limit_price",
    "next_eligible_session_at",
)


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _relation_kind(db, relation_name: str) -> str | None:
    return db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = :name"),
        {"name": relation_name},
    ).scalar()


def load_trade_objects(db, user_id: int | None) -> tuple[list[Any], list[dict[str, Any]]]:
    return monitor._monitored_live_trades_with_suppressed(db, user_id)


def load_envelope_objects(db, user_id: int | None) -> tuple[list[Any], list[dict[str, Any]]]:
    from app.services.trading.broker_position_truth import filter_broker_stale_open_trades
    from app.services.trading.management_envelopes import _envelope_runtime_object

    rows = db.execute(
        text(
            """
            SELECT *
              FROM trading_management_envelopes
             WHERE user_id IS NOT DISTINCT FROM :uid
               AND status = 'open'
               AND entry_price > 0
             ORDER BY entry_date DESC, id DESC
            """
        ),
        {"uid": user_id},
    ).mappings().all()
    return filter_broker_stale_open_trades(
        db,
        [_envelope_runtime_object(dict(row)) for row in rows],
    )


def _fetch_quotes_for_tickers(
    tickers: list[str],
    *,
    batch_cache: dict[tuple[str, ...], dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    key = tuple(sorted({str(t or "").upper() for t in tickers if str(t or "").strip()}))
    if not key:
        return {}
    if key not in batch_cache:
        try:
            batch_cache[key] = monitor.ts.fetch_quotes_batch(
                list(key),
                allow_provider_fallback=True,
            )
        except Exception:
            batch_cache[key] = {}
    return batch_cache[key]


def _broker_quote_for_trade(
    trade: Any,
    *,
    trade_is_option: bool,
    quote_cache: dict[tuple[str, str, str], dict[str, Any] | None],
) -> dict[str, Any] | None:
    broker_source = str(getattr(trade, "broker_source", None) or "")
    ticker = str(getattr(trade, "ticker", "") or "").upper()
    key = (broker_source, ticker, "option" if trade_is_option else "standard")
    if key not in quote_cache:
        try:
            from app.services.trading.broker_quotes import broker_quote_for_trade

            quote_cache[key] = broker_quote_for_trade(trade, purpose="display")
        except Exception:
            quote_cache[key] = None
    return quote_cache[key]


def serialize_active_setups(
    db,
    trades: list[Any],
    suppressed_stale_trades: list[dict[str, Any]],
    *,
    quote_cache: dict[tuple[str, str, str], dict[str, Any] | None],
    batch_cache: dict[tuple[str, ...], dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    from app.services.trading.autopilot_scope import is_option_trade
    from app.services.trading.broker_position_truth import broker_position_display_metrics
    from app.services.trading.robinhood_exit_execution import describe_trade_execution_state

    if not trades:
        return {
            "summary": {
                "active_count": 0,
                "avg_health": None,
                "actions_today": 0,
                "benefit_rate": None,
                "last_check": None,
                "suppressed_stale_count": len(suppressed_stale_trades),
            },
            "setups": [],
            "suppressed_stale_trades": _normalize(suppressed_stale_trades),
        }

    trade_ids = [int(t.id) for t in trades]
    alert_ids = [int(t.related_alert_id) for t in trades if getattr(t, "related_alert_id", None)]
    alerts_by_id: dict[int, BreakoutAlert] = {}
    if alert_ids:
        for ba in db.query(BreakoutAlert).filter(BreakoutAlert.id.in_(alert_ids)).all():
            alerts_by_id[int(ba.id)] = ba

    pattern_ids = {int(t.scan_pattern_id) for t in trades if getattr(t, "scan_pattern_id", None)}
    patterns: dict[int, ScanPattern] = {}
    if pattern_ids:
        for p in db.query(ScanPattern).filter(ScanPattern.id.in_(pattern_ids)).all():
            patterns[int(p.id)] = p

    all_decisions = (
        db.query(PatternMonitorDecision)
        .filter(PatternMonitorDecision.trade_id.in_(trade_ids))
        .order_by(PatternMonitorDecision.created_at.desc())
        .all()
    )
    by_trade: dict[int, list[PatternMonitorDecision]] = {}
    for d in all_decisions:
        by_trade.setdefault(int(d.trade_id), []).append(d)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    day_ago = now - timedelta(hours=24)
    actions_today = sum(
        1
        for d in all_decisions
        if d.created_at and d.created_at >= day_ago and d.action and d.action != "hold"
    )
    beneficial = [d for d in all_decisions if d.was_beneficial is not None]
    benefit_rate = (
        sum(1 for d in beneficial if d.was_beneficial) / len(beneficial)
        if beneficial
        else None
    )
    last_check = max((d.created_at for d in all_decisions if d.created_at), default=None)

    tickers = [
        str(t.ticker).upper()
        for t in trades
        if getattr(t, "ticker", None) and not is_option_trade(t)
    ]
    quotes_map = _fetch_quotes_for_tickers(tickers, batch_cache=batch_cache)

    setups: list[dict[str, Any]] = []
    health_scores: list[float] = []
    for trade in trades:
        trade_id = int(trade.id)
        decs = by_trade.get(trade_id, [])
        latest = decs[0] if decs else None
        if latest is not None:
            hpct = monitor._fraction_to_health_percent(latest.health_score)
            if hpct is not None:
                health_scores.append(float(hpct))

        pat = patterns.get(int(trade.scan_pattern_id)) if getattr(trade, "scan_pattern_id", None) else None
        if pat is None and latest and latest.scan_pattern_id:
            pid = int(latest.scan_pattern_id)
            if pid not in patterns:
                p2 = db.query(ScanPattern).filter(ScanPattern.id == pid).first()
                if p2:
                    patterns[pid] = p2
                    pat = p2

        trade_is_option = is_option_trade(trade)
        q = None
        if (getattr(trade, "broker_source", None) or "").strip() or trade_is_option:
            q = _broker_quote_for_trade(
                trade,
                trade_is_option=trade_is_option,
                quote_cache=quote_cache,
            )
        ticker = str(getattr(trade, "ticker", "") or "")
        if (not q or q.get("price") is None) and not trade_is_option:
            q = quotes_map.get(ticker.upper()) or quotes_map.get(ticker)
        cur = monitor._quote_price(q)
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

        recent = [monitor._serialize_decision(x) for x in decs[:5]]
        eff_sl = trade.stop_loss
        eff_tp = trade.take_profit
        linked = alerts_by_id.get(int(trade.related_alert_id)) if getattr(trade, "related_alert_id", None) else None
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
        row = {
            "trade_id": trade_id,
            "ticker": ticker,
            "direction": trade.direction,
            "pattern_name": pat.name if pat else None,
            "plan_label": plan_label,
            "pattern_id": trade.scan_pattern_id or (latest.scan_pattern_id if latest else None),
            "timeframe": pat.timeframe if pat else None,
            "entry_price": display_entry,
            "quantity": display_quantity,
            "stop_loss": eff_sl,
            "take_profit": eff_tp,
            "entry_date": trade.entry_date.isoformat() if trade.entry_date else None,
            "current_price": cur,
            "quote_source": q.get("source") if isinstance(q, dict) else None,
            "pnl_pct": pnl_pct,
            "broker_truth_entry_price": broker_metrics.get("entry_price"),
            "broker_truth_quantity": broker_metrics.get("quantity"),
            "broker_truth_position_id": broker_metrics.get("position_id"),
            "broker_truth_current_envelope_id": broker_metrics.get("current_envelope_id"),
            "broker_truth_metrics_source": broker_metrics.get("source"),
            "latest_decision": monitor._serialize_decision(latest) if latest else None,
            "decision_count": len(decs),
            "recent_decisions": recent,
            "execution_state": exec_meta.get("execution_state"),
            "execution_label": exec_meta.get("execution_label"),
            "execution_reason": exec_meta.get("execution_reason"),
            "pending_exit_status": exec_meta.get("pending_exit_status"),
            "pending_exit_order_id": exec_meta.get("pending_exit_order_id"),
            "pending_exit_limit_price": exec_meta.get("pending_exit_limit_price"),
            "next_eligible_session_at": exec_meta.get("next_eligible_session_at"),
        }
        setups.append({field: row.get(field) for field in ACTIVE_SETUP_FIELDS})

    avg_health = sum(health_scores) / len(health_scores) if health_scores else None
    setups.sort(key=lambda row: int(row.get("trade_id") or 0))
    return {
        "summary": {
            "active_count": len(trades),
            "avg_health": avg_health,
            "actions_today": actions_today,
            "benefit_rate": benefit_rate,
            "last_check": last_check.isoformat() if last_check else None,
            "suppressed_stale_count": len(suppressed_stale_trades),
        },
        "setups": _normalize(setups),
        "suppressed_stale_trades": sorted(
            (_normalize(row) for row in suppressed_stale_trades),
            key=lambda row: int(row.get("id") or 0),
        ),
    }


def run_probe(user_id: int | None = 1) -> dict[str, Any]:
    db = SessionLocal()
    try:
        relation_kinds = {
            "trading_management_envelopes": _relation_kind(db, "trading_management_envelopes"),
            "trading_trades": _relation_kind(db, "trading_trades"),
        }
        quote_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        batch_cache: dict[tuple[str, ...], dict[str, dict[str, Any]]] = {}
        old_trades, old_suppressed = load_trade_objects(db, user_id)
        new_trades, new_suppressed = load_envelope_objects(db, user_id)
        old_payload = serialize_active_setups(
            db,
            old_trades,
            old_suppressed,
            quote_cache=quote_cache,
            batch_cache=batch_cache,
        )
        new_payload = serialize_active_setups(
            db,
            new_trades,
            new_suppressed,
            quote_cache=quote_cache,
            batch_cache=batch_cache,
        )
        matched = old_payload == new_payload
        return {
            "status": "COMPLETE_POSITIVE" if matched else "MISMATCH",
            "matched": matched,
            "user_id": user_id,
            "old_setups": len(old_payload["setups"]),
            "new_setups": len(new_payload["setups"]),
            "old_suppressed": old_payload["summary"]["suppressed_stale_count"],
            "new_suppressed": new_payload["summary"]["suppressed_stale_count"],
            "quote_cache_entries": len(quote_cache),
            "batch_cache_entries": len(batch_cache),
            "relation_kinds": relation_kinds,
            "first_mismatch": None if matched else {
                "old": old_payload,
                "new": new_payload,
            },
        }
    finally:
        db.close()


def main() -> int:
    user_id_env = os.getenv("PHASE5AA_USER_ID", "1").strip()
    user_id = None if user_id_env.lower() in {"", "none", "null"} else int(user_id_env)
    payload = run_probe(user_id=user_id)
    print(f"VERDICT_STATUS={payload['status']}")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
