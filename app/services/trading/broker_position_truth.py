"""Broker-position truth helpers for live Trade management surfaces.

These helpers protect UI/monitor readers from stale local ``Trade`` rows.
``trading_trades`` is a management envelope; ``trading_positions`` is the
broker-authoritative inventory snapshot. When the two disagree, do not show
or act on the stale envelope after the short post-fill grace window.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from ...models.trading import Trade, TradingPosition

BROKER_POSITION_TRUTH_SOURCES = frozenset({"coinbase", "robinhood"})
DEFAULT_BROKER_TRUTH_GRACE_SECONDS = 15 * 60

_UNUSABLE_PENDING_EXIT_REASONS = frozenset({
    "",
    "missing",
    "unknown",
    "pending_exit",
    "broker_reconcile_close",
    "broker_reconcile_position_gone",
    "broker_reconcile_no_exit_price",
    "coinbase_position_sync_gone",
})


def _source(trade: Trade) -> str:
    return (getattr(trade, "broker_source", None) or "").strip().lower()


def pending_exit_thesis_reason(trade: Any) -> str | None:
    """Return a high-confidence pending exit thesis before reconcile clears it."""
    reason = str(getattr(trade, "pending_exit_reason", "") or "").strip()
    normalized = reason.lower()
    if normalized in _UNUSABLE_PENDING_EXIT_REASONS:
        return None
    if normalized.startswith("broker_reconcile_") or normalized.startswith(
        "coinbase_position_sync"
    ):
        return None
    has_pending_metadata = any(
        getattr(trade, attr, None)
        for attr in (
            "pending_exit_order_id",
            "pending_exit_status",
            "pending_exit_requested_at",
        )
    )
    if not has_pending_metadata:
        return None
    return reason[:50]


def reconcile_exit_reason_preserving_pending_thesis(
    trade: Any,
    *,
    fallback: str,
) -> str:
    current = str(getattr(trade, "exit_reason", "") or "").strip()
    if current:
        return current[:50]
    return pending_exit_thesis_reason(trade) or fallback


def _is_live_broker_trade(trade: Trade) -> bool:
    return _source(trade) in BROKER_POSITION_TRUTH_SOURCES


def _is_option_trade(trade: Trade) -> bool:
    try:
        from .autopilot_scope import is_option_trade

        return bool(is_option_trade(trade))
    except Exception:
        return False


def _positive_qty(pos: TradingPosition | None) -> bool:
    if pos is None:
        return False
    try:
        return float(pos.current_quantity or 0.0) > 0.0
    except (TypeError, ValueError):
        return False


def _within_grace(
    trade: Trade,
    *,
    now: datetime,
    grace_seconds: int,
) -> bool:
    refs = [
        getattr(trade, "filled_at", None),
        getattr(trade, "submitted_at", None),
        getattr(trade, "entry_date", None),
    ]
    latest = max((r for r in refs if r is not None), default=None)
    if latest is None:
        return False
    return (now - latest).total_seconds() < max(0, int(grace_seconds))


def _snapshot(
    trade: Trade,
    *,
    reason: str,
    position: TradingPosition | None = None,
    reconciled_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "kind": "trade",
        "id": int(getattr(trade, "id", 0) or 0),
        "ticker": getattr(trade, "ticker", None),
        "broker_source": getattr(trade, "broker_source", None),
        "position_id": int(getattr(trade, "position_id", 0) or 0) or None,
        "position_state": getattr(position, "state", None) if position else None,
        "position_quantity": (
            float(position.current_quantity)
            if position is not None and position.current_quantity is not None
            else None
        ),
        "position_envelope_id": (
            int(position.current_envelope_id)
            if position is not None and position.current_envelope_id is not None
            else None
        ),
        "reason": reason,
        "broker_truth_status": "stale",
        "broker_truth_reason": reason,
        "stale_broker_position": True,
        "stale_reconciled_at": (
            reconciled_at.isoformat() if reconciled_at is not None else None
        ),
        "broker_sync_missing_streak": int(
            getattr(trade, "broker_sync_missing_streak", 0) or 0
        ),
        "entry_date": (
            trade.entry_date.isoformat()
            if getattr(trade, "entry_date", None) is not None
            else None
        ),
        "last_broker_sync": (
            trade.last_broker_sync.isoformat()
            if getattr(trade, "last_broker_sync", None) is not None
            else None
        ),
    }


def _natural_key_position(db: Session, trade: Trade) -> TradingPosition | None:
    broker = _source(trade)
    ticker = (getattr(trade, "ticker", None) or "").strip()
    if not broker or not ticker:
        return None
    direction = (getattr(trade, "direction", None) or "long").strip().lower()
    uid = getattr(trade, "user_id", None)
    q = db.query(TradingPosition).filter(
        TradingPosition.broker_source == broker,
        TradingPosition.ticker == ticker,
        TradingPosition.direction == direction,
        TradingPosition.state == "open",
    )
    if uid is not None:
        q = q.filter(or_(TradingPosition.user_id == uid, TradingPosition.user_id.is_(None)))
    else:
        q = q.filter(TradingPosition.user_id.is_(None))
    return q.order_by(TradingPosition.id.desc()).first()


def broker_stale_open_trade_snapshot(
    db: Session,
    trade: Trade,
    *,
    grace_seconds: int = DEFAULT_BROKER_TRUTH_GRACE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a stale-position snapshot when an open broker Trade is not live.

    ``None`` means the trade is safe for UI/monitor readers to surface. A dict
    means the local management row no longer has a matching open broker
    position according to the position-identity table.
    """
    if getattr(trade, "status", None) != "open" or not _is_live_broker_trade(trade):
        return None

    if _source(trade) == "coinbase":
        # Coinbase position snapshots have proven partial during API/egress
        # failures. The Coinbase sync layer owns the close decision and now
        # requires a confirming sell fill before changing Trade.status.
        # Until then, fail open so the monitoring desk and live exit monitor
        # keep visible ownership instead of hiding possible real exposure.
        return None
    if _is_option_trade(trade):
        # Robinhood options are contract positions, but TradingPosition is the
        # spot/crypto identity table. Judging an option by the underlying's
        # share row hides real option exposure after the grace window.
        return None

    now = now or datetime.utcnow()
    pos: TradingPosition | None = None
    pos_id = getattr(trade, "position_id", None)
    if pos_id is not None:
        try:
            pos = db.get(TradingPosition, int(pos_id))
        except Exception:
            pos = None
        if pos is None:
            if _within_grace(trade, now=now, grace_seconds=grace_seconds):
                return None
            return _snapshot(trade, reason="position_identity_missing", position=None)
        if (pos.state or "").lower() != "open":
            if _within_grace(trade, now=now, grace_seconds=grace_seconds):
                return None
            return _snapshot(trade, reason="position_identity_closed", position=pos)
        if not _positive_qty(pos):
            if _within_grace(trade, now=now, grace_seconds=grace_seconds):
                return None
            return _snapshot(trade, reason="position_identity_zero_qty", position=pos)
        return None

    pos = _natural_key_position(db, trade)
    if pos is not None and _positive_qty(pos):
        return None
    if _within_grace(trade, now=now, grace_seconds=grace_seconds):
        return None
    return _snapshot(trade, reason="position_identity_missing", position=pos)


def filter_broker_stale_open_trades(
    db: Session,
    trades: list[Trade],
    *,
    grace_seconds: int = DEFAULT_BROKER_TRUTH_GRACE_SECONDS,
) -> tuple[list[Trade], list[dict[str, Any]]]:
    """Split trades into broker-live and stale-management-envelope rows."""
    now = datetime.utcnow()
    live: list[Trade] = []
    stale: list[dict[str, Any]] = []
    for trade in trades:
        snap = broker_stale_open_trade_snapshot(
            db,
            trade,
            grace_seconds=grace_seconds,
            now=now,
        )
        if snap is None:
            live.append(trade)
        else:
            stale.append(snap)
    return live, stale


def reconcile_stale_robinhood_open_trade(
    db: Session,
    trade: Trade,
    *,
    snapshot: dict[str, Any] | None = None,
    source: str = "broker_position_truth",
    exit_price_resolver: Callable[[Trade], float | None] | None = None,
) -> dict[str, Any] | None:
    """Close a stale Robinhood local management envelope without trading.

    Robinhood's position identity table is the inventory authority for stock
    positions. If the broker-side position is closed/zero/missing past the
    grace window, the local ``Trade`` row must stop participating in stop
    alerts, risk exposure, and cash allocation. This helper performs only local
    reconciliation; it never submits a broker order and it never fabricates PnL.
    """
    snap = snapshot or broker_stale_open_trade_snapshot(db, trade)
    if not snap:
        return None
    if _source(trade) != "robinhood":
        return None
    if getattr(trade, "status", None) != "open":
        return None

    now = datetime.utcnow()
    pending_exit_reason = pending_exit_thesis_reason(trade)
    resolved_reconcile_exit_reason = reconcile_exit_reason_preserving_pending_thesis(
        trade,
        fallback="broker_reconcile_position_gone",
    )
    trade.status = "closed"
    trade.exit_date = now
    trade.pending_exit_order_id = None
    trade.pending_exit_status = None
    trade.pending_exit_requested_at = None
    trade.pending_exit_reason = None
    trade.pending_exit_limit_price = None
    trade.last_broker_sync = now
    trade.broker_status = "no_position"
    if not getattr(trade, "management_scope", None) and "sync" in source:
        try:
            from .management_scope import MANAGEMENT_SCOPE_BROKER_SYNC

            trade.management_scope = MANAGEMENT_SCOPE_BROKER_SYNC
        except Exception:
            pass

    resolved_exit: float | None = None
    if exit_price_resolver is not None:
        try:
            candidate = exit_price_resolver(trade)
            resolved_exit = float(candidate) if candidate and candidate > 0 else None
        except Exception:
            resolved_exit = None

    if resolved_exit is not None:
        trade.exit_price = float(resolved_exit)
        entry = float(getattr(trade, "entry_price", 0.0) or 0.0)
        qty = float(getattr(trade, "quantity", 0.0) or 0.0)
        if entry > 0.0 and qty > 0.0:
            if (getattr(trade, "direction", "long") or "long").lower() == "short":
                trade.pnl = round((entry - resolved_exit) * qty, 2)
            else:
                trade.pnl = round((resolved_exit - entry) * qty, 2)
        if not getattr(trade, "exit_reason", None):
            trade.exit_reason = resolved_reconcile_exit_reason
    else:
        trade.exit_price = None
        trade.pnl = None
        if not getattr(trade, "exit_reason", None):
            trade.exit_reason = "broker_reconcile_no_exit_price"

    note = (
        f"\nAuto-closed local envelope: Robinhood position truth is stale "
        f"({snap.get('reason')}) via {source} at {now.strftime('%Y-%m-%d %H:%M')} UTC. "
    )
    if resolved_exit is not None:
        note += f"Exit ${resolved_exit:.4f} resolved from broker/order history."
    else:
        note += "Exit price unknown; PnL left NULL."
    trade.notes = (getattr(trade, "notes", None) or "") + note
    db.add(trade)

    try:
        from .brain_work.execution_hooks import on_broker_reconciled_close

        on_broker_reconciled_close(db, trade, source=source)
    except Exception:
        pass

    try:
        from .execution_audit import record_execution_event

        record_execution_event(
            db,
            user_id=trade.user_id,
            ticker=trade.ticker,
            trade=trade,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            broker_source="robinhood",
            event_type="broker_reconcile_gone_close",
            status="filled",
            average_fill_price=trade.exit_price,
            cumulative_filled_quantity=float(getattr(trade, "quantity", 0.0) or 0.0),
            payload_json={
                "side": "sell",
                "source": source,
                "synthetic": True,
                "trade_id": int(getattr(trade, "id", 0) or 0),
                "exit_reason": trade.exit_reason,
                "pending_exit_reason": pending_exit_reason,
                "broker_reconcile_exit_reason": (
                    "broker_reconcile_position_gone"
                    if resolved_exit is not None
                    else "broker_reconcile_no_exit_price"
                ),
                "broker_truth_reason": snap.get("reason"),
            },
        )
    except Exception:
        pass

    try:
        from .bracket_intent_writer import mark_closed

        ids = db.execute(
            text(
                "SELECT id FROM trading_bracket_intents "
                "WHERE trade_id = :tid AND intent_state <> 'closed'"
            ),
            {"tid": int(getattr(trade, "id", 0) or 0)},
        ).scalars().all()
        for intent_id in ids:
            mark_closed(
                db,
                int(intent_id),
                reason=str(trade.exit_reason or "broker_reconcile_close")[:128],
            )
    except Exception:
        pass

    db.flush()
    out = dict(snap)
    out.update(
        {
            "broker_truth_status": "reconciled_stale",
            "stale_reconciled_at": now.isoformat(),
            "exit_reason": trade.exit_reason,
            "exit_price": trade.exit_price,
        }
    )
    return out
