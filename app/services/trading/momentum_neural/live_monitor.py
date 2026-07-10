"""Read-only live observer for the replay research surface.

The observer never calls a broker or market-data provider. It reads bounded runtime,
audit, outcome, fill, and quote-tape records that the trading processes already persist.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from ....models.trading import (
    MomentumAutomationOutcome,
    Trade,
    TradingAutomationEvent,
    TradingAutomationRuntimeSnapshot,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
)

logger = logging.getLogger(__name__)


LIVE_MONITOR_STATE_TTL_SECONDS = 4.5
LIVE_MONITOR_CHART_TTL_SECONDS = 15.0
LIVE_MONITOR_SESSION_SCAN_LIMIT = 96
LIVE_MONITOR_SYMBOL_LIMIT = 24
LIVE_MONITOR_EVENT_LIMIT = 400
LIVE_MONITOR_EVENTS_PER_SYMBOL = 10
LIVE_MONITOR_QUOTE_ROWS_PER_SYMBOL = 480
LIVE_MONITOR_CHART_LOOKBACK_MINUTES = 45

ACTIVE_STATES = frozenset(
    {
        "queued",
        "watching",
        "entry_candidate",
        "pending_entry",
        "entered",
        "scaling_out",
        "trailing",
        "bailout",
        "exited",
        "cooldown",
        "live_arm_pending",
        "armed_pending_runner",
        "queued_live",
        "watching_live",
        "live_entry_candidate",
        "live_pending_entry",
        "live_entered",
        "live_scaling_out",
        "live_trailing",
        "live_bailout",
        "live_exited",
        "live_cooldown",
    }
)

POSITION_STATES = frozenset(
    {
        "entered",
        "scaling_out",
        "trailing",
        "bailout",
        "live_entered",
        "live_scaling_out",
        "live_trailing",
        "live_bailout",
    }
)

_cache_lock = threading.RLock()
_build_locks: dict[int, threading.Lock] = {}
_state_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_chart_cache: dict[int, tuple[float, tuple[str, ...], dict[str, list[list[Any]]], str]] = {}


def clear_live_monitor_caches() -> None:
    """Test/deploy helper; the observer owns no durable state."""

    with _cache_lock:
        _state_cache.clear()
        _chart_cache.clear()
        _build_locks.clear()


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _et_day_bounds(now_utc: datetime) -> tuple[datetime, datetime]:
    et = ZoneInfo("America/New_York")
    aware = now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc)
    now_et = aware.astimezone(et)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end_et = start_et + timedelta(days=1)
    return (
        start_et.astimezone(timezone.utc).replace(tzinfo=None),
        end_et.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _lane_bucket(mode: str | None, execution_family: str | None) -> str:
    if str(mode or "").lower() != "live":
        return "paper"
    if str(execution_family or "").lower() == "alpaca_spot":
        return "paper_twin"
    return "live"


def _execution_state(session: Any) -> dict[str, Any]:
    snapshot = _mapping(session.risk_snapshot_json)
    key = "momentum_live_execution" if str(session.mode).lower() == "live" else "momentum_paper_execution"
    return _mapping(snapshot.get(key))


def _position_state(session: Any) -> dict[str, Any]:
    execution = _execution_state(session)
    raw = _mapping(execution.get("position"))
    quantity = _float_or_none(raw.get("quantity"))
    entry = _float_or_none(raw.get("avg_entry_price"))
    if entry is None:
        entry = _float_or_none(raw.get("entry_price"))
    mark = _float_or_none(execution.get("last_mid"))
    stop = _float_or_none(raw.get("stop_price"))
    target = _float_or_none(raw.get("target_price"))
    is_open = bool(raw) and bool(quantity and quantity > 0) and str(session.state) in POSITION_STATES
    unrealized = None
    if is_open and entry is not None and mark is not None:
        unrealized = (mark - entry) * float(quantity or 0.0)
    return {
        "is_open": is_open,
        "quantity": quantity,
        "entry": entry,
        "mark": mark,
        "stop": stop,
        "target": target,
        "unrealized_usd": unrealized,
        "realized_runtime_usd": _float_or_none(execution.get("realized_pnl_usd")),
        "last_tick_utc": execution.get("last_tick_utc"),
    }


def _session_priority(row: dict[str, Any]) -> tuple[int, int, float]:
    state = str(row.get("state") or "")
    state_rank = 4 if row.get("position", {}).get("is_open") else 0
    if "pending_entry" in state:
        state_rank = max(state_rank, 3)
    elif "entry_candidate" in state:
        state_rank = max(state_rank, 2)
    elif "watching" in state or "armed" in state or "queued" in state:
        state_rank = max(state_rank, 1)
    lane_rank = {"live": 3, "paper_twin": 2, "paper": 1}.get(str(row.get("lane")), 0)
    updated = row.get("updated_at")
    stamp = updated.timestamp() if isinstance(updated, datetime) else 0.0
    return state_rank, lane_rank, stamp


def _preferred_outcome_pnl(row: Any) -> float | None:
    broker = _float_or_none(row.broker_realized_pnl_usd)
    if broker is not None and str(row.broker_recon_status or "").lower() == "reconciled":
        return broker
    realized = _float_or_none(row.realized_pnl_usd)
    return realized if realized is not None else broker


def _event_detail(payload: dict[str, Any]) -> str | None:
    for key in (
        "reason",
        "wait_reason",
        "trigger_reason",
        "setup_reason",
        "exit_reason",
        "outcome_class",
        "window",
        "state",
        "status",
    ):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:120]
    rejects = payload.get("detector_rejects")
    if isinstance(rejects, dict) and rejects:
        key, value = next(iter(rejects.items()))
        return f"{key}: {value}"[:120]
    return None


def _event_kind(event_type: str | None) -> str:
    value = str(event_type or "").lower()
    if "entry" in value and any(token in value for token in ("fill", "entered", "confirmed")):
        return "entry"
    if any(token in value for token in ("exit", "exited", "flatten")) and any(
        token in value for token in ("fill", "exited", "flatten", "closed")
    ):
        return "exit"
    return "decision"


def _event_number(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(payload.get(key))
        if value is not None:
            return value
    return None


def _state_label(value: str | None) -> str:
    clean = str(value or "unknown")
    return clean.removeprefix("live_").replace("_", " ")


def _active_session_rows(db: Session, *, user_id: int) -> list[dict[str, Any]]:
    runtime_rows = (
        db.query(
            TradingAutomationRuntimeSnapshot.session_id,
            TradingAutomationRuntimeSnapshot.symbol,
            TradingAutomationRuntimeSnapshot.mode,
            TradingAutomationRuntimeSnapshot.lane,
            TradingAutomationRuntimeSnapshot.state,
            TradingAutomationRuntimeSnapshot.strategy_family,
            TradingAutomationRuntimeSnapshot.strategy_label,
            TradingAutomationRuntimeSnapshot.confidence,
            TradingAutomationRuntimeSnapshot.current_position_state,
            TradingAutomationRuntimeSnapshot.last_action,
            TradingAutomationRuntimeSnapshot.last_price,
            TradingAutomationRuntimeSnapshot.latest_levels_json,
            TradingAutomationRuntimeSnapshot.updated_at,
        )
        .filter(
            TradingAutomationRuntimeSnapshot.user_id == int(user_id),
            TradingAutomationRuntimeSnapshot.state.in_(tuple(ACTIVE_STATES)),
        )
        .order_by(TradingAutomationRuntimeSnapshot.updated_at.desc())
        .limit(LIVE_MONITOR_SESSION_SCAN_LIMIT)
        .all()
    )
    if not runtime_rows:
        return []
    session_ids = [int(row.session_id) for row in runtime_rows]
    session_rows = (
        db.query(
            TradingAutomationSession.id,
            TradingAutomationSession.user_id,
            TradingAutomationSession.execution_family,
            TradingAutomationSession.mode,
            TradingAutomationSession.symbol,
            TradingAutomationSession.state,
            TradingAutomationSession.started_at,
            TradingAutomationSession.created_at,
            TradingAutomationSession.updated_at,
        )
        .filter(
            TradingAutomationSession.id.in_(session_ids),
            TradingAutomationSession.user_id == int(user_id),
        )
        .all()
    )
    session_map = {int(row.id): row for row in session_rows}
    execution_ids = [
        int(row.id)
        for row in session_rows
        if str(row.state) in ACTIVE_STATES
        and (str(row.mode).lower() == "live" or str(row.state) in POSITION_STATES)
    ]
    risk_snapshots: dict[int, dict[str, Any]] = {}
    if execution_ids:
        risk_snapshots = {
            int(session_id): _mapping(snapshot)
            for session_id, snapshot in db.query(
                TradingAutomationSession.id,
                TradingAutomationSession.risk_snapshot_json,
            )
            .filter(TradingAutomationSession.id.in_(execution_ids))
            .all()
        }
    out: list[dict[str, Any]] = []
    for runtime in runtime_rows:
        raw_session = session_map.get(int(runtime.session_id))
        if raw_session is None or str(raw_session.state) not in ACTIVE_STATES:
            continue
        session = SimpleNamespace(
            id=int(raw_session.id),
            user_id=raw_session.user_id,
            execution_family=raw_session.execution_family,
            mode=raw_session.mode,
            symbol=raw_session.symbol,
            state=raw_session.state,
            started_at=raw_session.started_at,
            created_at=raw_session.created_at,
            updated_at=raw_session.updated_at,
            risk_snapshot_json=risk_snapshots.get(int(raw_session.id), {}),
        )
        position = _position_state(session)
        out.append(
            {
                "session": session,
                "session_id": int(session.id),
                "symbol": str(session.symbol or "").upper(),
                "mode": str(session.mode or "paper"),
                "lane": _lane_bucket(session.mode, session.execution_family),
                "state": str(session.state or "unknown"),
                "state_label": _state_label(session.state),
                "position": position,
                "last_action": runtime.last_action or session.state,
                "strategy": runtime.strategy_label or runtime.strategy_family,
                "confidence": _float_or_none(runtime.confidence),
                "updated_at": session.updated_at or runtime.updated_at,
                "runtime_updated_at": runtime.updated_at,
                "levels": _mapping(runtime.latest_levels_json),
            }
        )
    return out


def _broker_position_truth(
    db: Session,
    *,
    user_id: int,
    symbols: Iterable[str],
    now_utc: datetime,
) -> tuple[set[str], set[str]]:
    names = sorted({str(symbol).upper() for symbol in symbols if symbol})
    if not names:
        return set(), set()
    rows = (
        db.query(Trade.ticker, Trade.status)
        .filter(
            Trade.user_id == int(user_id),
            Trade.ticker.in_(names),
            or_(
                Trade.status == "open",
                (
                    Trade.status.in_(("closed", "cancelled"))
                    & Trade.exit_date.isnot(None)
                    & (Trade.exit_date >= now_utc - timedelta(days=2))
                ),
            ),
        )
        .all()
    )
    open_symbols = {str(ticker).upper() for ticker, status in rows if status == "open" and ticker}
    exited_symbols = {
        str(ticker).upper() for ticker, status in rows if status in ("closed", "cancelled") and ticker
    }
    return open_symbols, exited_symbols


def _pnl_rows(
    db: Session,
    *,
    user_id: int,
    active_rows: list[dict[str, Any]],
    now_utc: datetime,
) -> tuple[dict[str, dict[str, Any]], set[int]]:
    day_start, day_end = _et_day_bounds(now_utc)
    by_symbol: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "realized_usd": 0.0,
            "unrealized_usd": 0.0,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "by_lane": defaultdict(lambda: {"realized_usd": 0.0, "unrealized_usd": 0.0, "trades": 0}),
            "broker_unconfirmed": False,
        }
    )
    evidence_session_ids: set[int] = set()

    outcomes = (
        db.query(
            MomentumAutomationOutcome.session_id,
            MomentumAutomationOutcome.symbol,
            MomentumAutomationOutcome.mode,
            MomentumAutomationOutcome.realized_pnl_usd,
            MomentumAutomationOutcome.broker_realized_pnl_usd,
            MomentumAutomationOutcome.broker_recon_status,
            TradingAutomationSession.execution_family,
        )
        .join(TradingAutomationSession, TradingAutomationSession.id == MomentumAutomationOutcome.session_id)
        .filter(
            MomentumAutomationOutcome.user_id == int(user_id),
            MomentumAutomationOutcome.terminal_at >= day_start,
            MomentumAutomationOutcome.terminal_at < day_end,
            or_(
                MomentumAutomationOutcome.realized_pnl_usd.isnot(None),
                MomentumAutomationOutcome.broker_realized_pnl_usd.isnot(None),
            ),
        )
        .all()
    )
    for outcome in outcomes:
        pnl = _preferred_outcome_pnl(outcome)
        if pnl is None:
            continue
        symbol = str(outcome.symbol or "").upper()
        lane = _lane_bucket(outcome.mode, outcome.execution_family)
        cell = by_symbol[symbol]
        cell["realized_usd"] += pnl
        cell["trades"] += 1
        cell["wins"] += int(pnl > 0)
        cell["losses"] += int(pnl < 0)
        cell["by_lane"][lane]["realized_usd"] += pnl
        cell["by_lane"][lane]["trades"] += 1
        evidence_session_ids.add(int(outcome.session_id))

    fills = (
        db.query(
            TradingAutomationSimulatedFill.session_id,
            TradingAutomationSimulatedFill.symbol,
            TradingAutomationSimulatedFill.pnl_usd,
            TradingAutomationSession.mode,
            TradingAutomationSession.execution_family,
        )
        .join(TradingAutomationSession, TradingAutomationSession.id == TradingAutomationSimulatedFill.session_id)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSimulatedFill.ts >= day_start,
            TradingAutomationSimulatedFill.ts < day_end,
            TradingAutomationSimulatedFill.pnl_usd.isnot(None),
        )
        .all()
    )
    for fill in fills:
        pnl = float(fill.pnl_usd or 0.0)
        symbol = str(fill.symbol or "").upper()
        lane = _lane_bucket(fill.mode, fill.execution_family)
        cell = by_symbol[symbol]
        cell["realized_usd"] += pnl
        cell["trades"] += 1
        cell["wins"] += int(pnl > 0)
        cell["losses"] += int(pnl < 0)
        cell["by_lane"][lane]["realized_usd"] += pnl
        cell["by_lane"][lane]["trades"] += 1
        evidence_session_ids.add(int(fill.session_id))

    live_symbols = [row["symbol"] for row in active_rows if row["lane"] == "live"]
    open_broker_symbols, exited_broker_symbols = _broker_position_truth(
        db,
        user_id=user_id,
        symbols=live_symbols,
        now_utc=now_utc,
    )
    seen_runtime_realized: set[int] = set()
    for row in active_rows:
        session = row["session"]
        position = row["position"]
        symbol = row["symbol"]
        lane = row["lane"]
        cell = by_symbol[symbol]
        evidence_session_ids.add(int(session.id))
        broker_unconfirmed = (
            lane == "live"
            and symbol in exited_broker_symbols
            and symbol not in open_broker_symbols
        )
        if broker_unconfirmed:
            cell["broker_unconfirmed"] = True
        unrealized = position.get("unrealized_usd")
        if unrealized is not None and not broker_unconfirmed:
            cell["unrealized_usd"] += float(unrealized)
            cell["by_lane"][lane]["unrealized_usd"] += float(unrealized)
        if str(session.mode).lower() == "live" and int(session.id) not in seen_runtime_realized:
            started = session.started_at or session.created_at
            runtime_realized = position.get("realized_runtime_usd")
            if runtime_realized is not None and (started is None or started >= day_start):
                value = float(runtime_realized)
                cell["realized_usd"] += value
                cell["by_lane"][lane]["realized_usd"] += value
            seen_runtime_realized.add(int(session.id))

    normalized: dict[str, dict[str, Any]] = {}
    for symbol, cell in by_symbol.items():
        lanes = {}
        for lane, lane_cell in cell["by_lane"].items():
            lanes[lane] = {
                "realized_usd": round(float(lane_cell["realized_usd"]), 2),
                "unrealized_usd": round(float(lane_cell["unrealized_usd"]), 2),
                "trades": int(lane_cell["trades"]),
            }
        realized = round(float(cell["realized_usd"]), 2)
        unrealized = round(float(cell["unrealized_usd"]), 2)
        normalized[symbol] = {
            "realized_usd": realized,
            "unrealized_usd": unrealized,
            "total_usd": round(realized + unrealized, 2),
            "trades": int(cell["trades"]),
            "wins": int(cell["wins"]),
            "losses": int(cell["losses"]),
            "by_lane": lanes,
            "broker_unconfirmed": bool(cell["broker_unconfirmed"]),
        }
    return normalized, evidence_session_ids


def _recent_events(
    db: Session,
    *,
    session_ids: Iterable[int],
    session_symbols: dict[int, str],
    since_utc: datetime,
) -> dict[str, list[dict[str, Any]]]:
    ids = sorted({int(value) for value in session_ids if value})
    if not ids:
        return {}
    event_columns = (
        TradingAutomationEvent.id,
        TradingAutomationEvent.session_id,
        TradingAutomationEvent.ts,
        TradingAutomationEvent.event_type,
        TradingAutomationEvent.payload_json,
    )
    recent_rows = (
        db.query(*event_columns)
        .filter(
            TradingAutomationEvent.session_id.in_(ids),
            TradingAutomationEvent.ts >= since_utc,
        )
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(LIVE_MONITOR_EVENT_LIMIT)
        .all()
    )
    lifecycle_types = ("live_entry_filled", "live_exit_filled", "live_partial_exit_filled")
    lifecycle_rows = (
        db.query(*event_columns)
        .filter(
            TradingAutomationEvent.session_id.in_(ids),
            TradingAutomationEvent.ts >= since_utc,
            TradingAutomationEvent.event_type.in_(lifecycle_types),
        )
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(200)
        .all()
    )
    rows = sorted(
        {int(row.id): row for row in [*recent_rows, *lifecycle_rows]}.values(),
        key=lambda row: row.ts or datetime.min,
        reverse=True,
    )
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: dict[str, set[tuple[str, str | None]]] = defaultdict(set)
    for event in rows:
        symbol = session_symbols.get(int(event.session_id))
        if not symbol:
            continue
        payload = _mapping(event.payload_json)
        detail = _event_detail(payload)
        lifecycle = str(event.event_type) in lifecycle_types
        key = (str(event.event_type), str(event.id) if lifecycle else detail)
        if key in seen[symbol]:
            continue
        if len(by_symbol[symbol]) >= LIVE_MONITOR_EVENTS_PER_SYMBOL:
            if not lifecycle:
                continue
            replace_at = next(
                (
                    index
                    for index in range(len(by_symbol[symbol]) - 1, -1, -1)
                    if by_symbol[symbol][index].get("kind") == "decision"
                ),
                None,
            )
            if replace_at is None:
                continue
            by_symbol[symbol].pop(replace_at)
        seen[symbol].add(key)
        by_symbol[symbol].append(
            {
                "id": int(event.id),
                "session_id": int(event.session_id),
                "ts": _iso_utc(event.ts),
                "t": event.ts.strftime("%H:%M") if event.ts else None,
                "stage": str(event.event_type or "event"),
                "detail": detail,
                "kind": _event_kind(event.event_type),
                "price": _event_number(
                    payload,
                    "fill_price",
                    "avg",
                    "average_fill_price",
                    "entry_price",
                    "exit_price",
                    "price",
                    "mid",
                ),
                "quantity": _event_number(payload, "filled_size", "quantity", "qty"),
                "pnl_usd": _event_number(payload, "pnl_usd", "realized_pnl_usd"),
            }
        )
    for values in by_symbol.values():
        values.reverse()
    return dict(by_symbol)


def _build_state_snapshot(db: Session, *, user_id: int, now_utc: datetime) -> dict[str, Any]:
    active_rows = _active_session_rows(db, user_id=user_id)
    pnl_by_symbol, evidence_session_ids = _pnl_rows(
        db,
        user_id=user_id,
        active_rows=active_rows,
        now_utc=now_utc,
    )
    session_symbols = {int(row["session_id"]): row["symbol"] for row in active_rows}
    for session in (
        db.query(TradingAutomationSession.id, TradingAutomationSession.symbol)
        .filter(TradingAutomationSession.id.in_(tuple(evidence_session_ids) or (-1,)))
        .all()
    ):
        session_symbols.setdefault(int(session.id), str(session.symbol or "").upper())
    events = _recent_events(
        db,
        session_ids=evidence_session_ids,
        session_symbols=session_symbols,
        since_utc=_et_day_bounds(now_utc)[0],
    )

    sessions_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in active_rows:
        sessions_by_symbol[row["symbol"]].append(row)
    all_symbols = set(sessions_by_symbol) | set(pnl_by_symbol)
    ranked_symbols = sorted(
        all_symbols,
        key=lambda symbol: (
            0 if sessions_by_symbol.get(symbol) else 1,
            -max((_session_priority(row) for row in sessions_by_symbol.get(symbol, [])), default=(0, 0, 0))[0],
            -max((_session_priority(row) for row in sessions_by_symbol.get(symbol, [])), default=(0, 0, 0))[1],
            -max((_session_priority(row) for row in sessions_by_symbol.get(symbol, [])), default=(0, 0, 0))[2],
            -abs(float(pnl_by_symbol.get(symbol, {}).get("total_usd") or 0.0)),
            symbol,
        ),
    )[:LIVE_MONITOR_SYMBOL_LIMIT]

    symbols_out: list[dict[str, Any]] = []
    for symbol in ranked_symbols:
        session_rows = sorted(sessions_by_symbol.get(symbol, []), key=_session_priority, reverse=True)
        primary = session_rows[0] if session_rows else None
        lanes: dict[str, dict[str, Any]] = {}
        positions: list[dict[str, Any]] = []
        for row in session_rows:
            lane = row["lane"]
            lane_row = lanes.setdefault(
                lane,
                {
                    "lane": lane,
                    "state": row["state"],
                    "state_label": row["state_label"],
                    "session_count": 0,
                    "updated_at": _iso_utc(row["updated_at"]),
                },
            )
            lane_row["session_count"] += 1
            if row["position"].get("is_open"):
                positions.append(
                    {
                        "lane": lane,
                        "session_id": row["session_id"],
                        **{key: row["position"].get(key) for key in ("quantity", "entry", "mark", "stop", "target")},
                    }
                )
        pnl = pnl_by_symbol.get(
            symbol,
            {
                "realized_usd": 0.0,
                "unrealized_usd": 0.0,
                "total_usd": 0.0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "by_lane": {},
                "broker_unconfirmed": False,
            },
        )
        updated = primary.get("updated_at") if primary else None
        age_seconds = max(0.0, (now_utc - updated).total_seconds()) if isinstance(updated, datetime) else None
        symbols_out.append(
            {
                "symbol": symbol,
                "active": bool(session_rows),
                "armed": bool(session_rows) and not positions,
                "state": primary.get("state") if primary else "completed_today",
                "state_label": primary.get("state_label") if primary else "completed today",
                "primary_lane": primary.get("lane") if primary else None,
                "last_action": primary.get("last_action") if primary else "completed today",
                "strategy": primary.get("strategy") if primary else None,
                "confidence": primary.get("confidence") if primary else None,
                "last_price": primary.get("position", {}).get("mark") if primary else None,
                "updated_at": _iso_utc(updated),
                "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
                "stale": bool(age_seconds is not None and age_seconds > 30.0),
                "lanes": list(lanes.values()),
                "positions": positions,
                "pnl": pnl,
                "events": events.get(symbol, []),
            }
        )

    lane_summary = {
        lane: {
            "sessions": sum(1 for row in active_rows if row["lane"] == lane),
            "symbols": len({row["symbol"] for row in active_rows if row["lane"] == lane}),
            "open_positions": sum(1 for row in active_rows if row["lane"] == lane and row["position"].get("is_open")),
        }
        for lane in ("live", "paper_twin", "paper")
    }
    totals = {
        "realized_usd": round(sum(float(row.get("realized_usd") or 0.0) for row in pnl_by_symbol.values()), 2),
        "unrealized_usd": round(sum(float(row.get("unrealized_usd") or 0.0) for row in pnl_by_symbol.values()), 2),
        "trades": sum(int(row.get("trades") or 0) for row in pnl_by_symbol.values()),
    }
    totals["total_usd"] = round(totals["realized_usd"] + totals["unrealized_usd"], 2)
    latest_update = max(
        (row.get("updated_at") for row in active_rows if isinstance(row.get("updated_at"), datetime)),
        default=None,
    )
    return {
        "ok": True,
        "read_only": True,
        "as_of_utc": _iso_utc(now_utc),
        "latest_runtime_utc": _iso_utc(latest_update),
        "refresh_after_ms": 3000,
        "active_symbol_count": len({row["symbol"] for row in active_rows}),
        "open_position_count": sum(1 for row in active_rows if row["position"].get("is_open")),
        "totals": totals,
        "lanes": lane_summary,
        "symbols": symbols_out,
        "observer": {
            "source": "persisted_runtime_events_outcomes_and_quote_tape",
            "broker_calls": 0,
            "provider_calls": 0,
            "writes": 0,
            "state_cache_seconds": LIVE_MONITOR_STATE_TTL_SECONDS,
            "chart_cache_seconds": LIVE_MONITOR_CHART_TTL_SECONDS,
            "symbol_limit": LIVE_MONITOR_SYMBOL_LIMIT,
            "quote_row_cap_per_symbol": LIVE_MONITOR_QUOTE_ROWS_PER_SYMBOL,
        },
    }


def _minute_bar_series(rows: Iterable[Any]) -> dict[str, list[list[Any]]]:
    buckets: dict[str, dict[datetime, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        symbol = str(row[0] or "").upper()
        observed_at = row[1]
        if not symbol or not isinstance(observed_at, datetime):
            continue
        bid = _float_or_none(row[2])
        ask = _float_or_none(row[3])
        price = _float_or_none(row[4])
        if price is None and bid is not None and ask is not None:
            price = (bid + ask) / 2.0
        if price is None or price <= 0:
            continue
        if observed_at.tzinfo is not None:
            observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
        bucket = observed_at.replace(second=0, microsecond=0)
        day_volume = _float_or_none(row[5])
        candle = buckets[symbol].get(bucket)
        if candle is None:
            buckets[symbol][bucket] = {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume_min": day_volume,
                "volume_max": day_volume,
            }
            continue
        candle["high"] = max(float(candle["high"]), price)
        candle["low"] = min(float(candle["low"]), price)
        candle["close"] = price
        if day_volume is not None:
            candle["volume_min"] = day_volume if candle["volume_min"] is None else min(candle["volume_min"], day_volume)
            candle["volume_max"] = day_volume if candle["volume_max"] is None else max(candle["volume_max"], day_volume)

    out: dict[str, list[list[Any]]] = {}
    for symbol, values in buckets.items():
        bars: list[list[Any]] = []
        for bucket, candle in sorted(values.items()):
            volume_min = candle["volume_min"]
            volume_max = candle["volume_max"]
            volume = max(0.0, float(volume_max) - float(volume_min)) if volume_min is not None and volume_max is not None else 0.0
            bars.append(
                [
                    bucket.strftime("%H:%M"),
                    round(float(candle["open"]), 6),
                    round(float(candle["high"]), 6),
                    round(float(candle["low"]), 6),
                    round(float(candle["close"]), 6),
                    round(volume, 2),
                ]
            )
        out[symbol] = bars
    return out


def _load_chart_series(
    db: Session,
    *,
    symbols: tuple[str, ...],
    now_utc: datetime,
) -> dict[str, list[list[Any]]]:
    if not symbols:
        return {}
    since_utc = now_utc - timedelta(minutes=LIVE_MONITOR_CHART_LOOKBACK_MINUTES)
    rows = db.execute(
        text(
            "WITH syms(symbol) AS (SELECT unnest(CAST(:symbols AS text[]))) "
            "SELECT s.symbol, q.observed_at, q.bid, q.ask, q.mid, q.day_volume "
            "FROM syms s "
            "CROSS JOIN LATERAL ("
            " SELECT observed_at, bid, ask, mid, day_volume "
            " FROM momentum_nbbo_spread_tape t "
            " WHERE t.symbol = s.symbol AND t.observed_at >= :since_utc "
            " ORDER BY t.observed_at DESC LIMIT :row_limit"
            ") q ORDER BY s.symbol, q.observed_at"
        ),
        {
            "symbols": list(symbols),
            "since_utc": since_utc,
            "row_limit": LIVE_MONITOR_QUOTE_ROWS_PER_SYMBOL,
        },
    ).fetchall()
    return _minute_bar_series(rows)


def _cached_chart_series(
    db: Session,
    *,
    user_id: int,
    symbols: tuple[str, ...],
    now_utc: datetime,
    now_mono: float,
) -> tuple[dict[str, list[list[Any]]], str]:
    with _cache_lock:
        cached = _chart_cache.get(int(user_id))
        if cached and now_mono - cached[0] < LIVE_MONITOR_CHART_TTL_SECONDS and cached[1] == symbols:
            return cached[2], cached[3]
    try:
        series = _load_chart_series(db, symbols=symbols, now_utc=now_utc)
    except Exception:
        logger.warning("[live_monitor] bounded quote-tape read failed", exc_info=True)
        db.rollback()
        series = {}
    chart_as_of = _iso_utc(now_utc) or ""
    with _cache_lock:
        _chart_cache[int(user_id)] = (now_mono, symbols, series, chart_as_of)
    return series, chart_as_of


def live_monitor_snapshot(db: Session, *, user_id: int) -> dict[str, Any]:
    """Return one batched observer snapshot; single-flight and TTL bounded per user."""

    now_mono = time.monotonic()
    with _cache_lock:
        cached = _state_cache.get(int(user_id))
        if cached and now_mono - cached[0] < LIVE_MONITOR_STATE_TTL_SECONDS:
            return cached[1]
        build_lock = _build_locks.setdefault(int(user_id), threading.Lock())

    with build_lock:
        now_mono = time.monotonic()
        with _cache_lock:
            cached = _state_cache.get(int(user_id))
            if cached and now_mono - cached[0] < LIVE_MONITOR_STATE_TTL_SECONDS:
                return cached[1]
        started = time.perf_counter()
        now_utc = datetime.utcnow()
        payload = _build_state_snapshot(db, user_id=int(user_id), now_utc=now_utc)
        symbols = tuple(str(row.get("symbol") or "").upper() for row in payload.get("symbols", []) if row.get("symbol"))
        series, chart_as_of = _cached_chart_series(
            db,
            user_id=int(user_id),
            symbols=symbols,
            now_utc=now_utc,
            now_mono=now_mono,
        )
        payload["series"] = series
        payload["chart_as_of_utc"] = chart_as_of
        payload["observer"]["build_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        with _cache_lock:
            _state_cache[int(user_id)] = (time.monotonic(), payload)
        return payload
