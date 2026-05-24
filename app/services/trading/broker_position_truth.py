"""Broker-position truth helpers for live Trade management surfaces.

These helpers protect UI/monitor readers from stale local ``Trade`` rows.
``trading_trades`` is a management envelope; ``trading_positions`` is the
broker-authoritative inventory snapshot. When the two disagree, do not show
or act on the stale envelope after the short post-fill grace window.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...models.trading import Trade, TradingPosition

BROKER_POSITION_TRUTH_SOURCES = frozenset({"coinbase", "robinhood"})
DEFAULT_BROKER_TRUTH_GRACE_SECONDS = 15 * 60


def _source(trade: Trade) -> str:
    return (getattr(trade, "broker_source", None) or "").strip().lower()


def _is_live_broker_trade(trade: Trade) -> bool:
    return _source(trade) in BROKER_POSITION_TRUTH_SOURCES


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
            return _snapshot(trade, reason="position_identity_closed", position=pos)
        if not _positive_qty(pos):
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
