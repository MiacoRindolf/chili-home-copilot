"""Helpers for broker position sync dedupe and concurrency control."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import Trade

logger = logging.getLogger(__name__)

_BROKER_SYNC_LOCK_CLASSIDS = {
    "robinhood": 29001,
    "coinbase": 29002,
}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _is_meaningful_number(value: Any) -> bool:
    num = _safe_float(value)
    return num is not None and num > 0


def _epoch_or_zero(value: datetime | None) -> float:
    if value is None:
        return 0.0
    return float(value.timestamp())


def _trade_rank_key(trade: Trade) -> tuple[float, ...]:
    return (
        1.0 if _has_value(getattr(trade, "broker_order_id", None)) else 0.0,
        1.0 if _has_value(getattr(trade, "related_alert_id", None)) else 0.0,
        1.0 if _has_value(getattr(trade, "scan_pattern_id", None)) else 0.0,
        1.0 if _has_value(getattr(trade, "stop_loss", None)) else 0.0,
        1.0 if _has_value(getattr(trade, "take_profit", None)) else 0.0,
        1.0 if _has_value(getattr(trade, "indicator_snapshot", None)) else 0.0,
        1.0 if _is_meaningful_number(getattr(trade, "entry_price", None)) else 0.0,
        1.0 if _is_meaningful_number(getattr(trade, "quantity", None)) else 0.0,
        _epoch_or_zero(getattr(trade, "last_broker_sync", None)),
        -_epoch_or_zero(getattr(trade, "entry_date", None)),
        -float(getattr(trade, "id", 0) or 0),
    )


def _merge_duplicate_into_canonical(canonical: Trade, duplicate: Trade) -> None:
    if not _has_value(canonical.related_alert_id) and _has_value(duplicate.related_alert_id):
        canonical.related_alert_id = duplicate.related_alert_id
    if not _has_value(canonical.scan_pattern_id) and _has_value(duplicate.scan_pattern_id):
        canonical.scan_pattern_id = duplicate.scan_pattern_id
    if not _has_value(canonical.stop_loss) and _has_value(duplicate.stop_loss):
        canonical.stop_loss = duplicate.stop_loss
    if not _has_value(canonical.take_profit) and _has_value(duplicate.take_profit):
        canonical.take_profit = duplicate.take_profit
    if not _has_value(canonical.pattern_tags) and _has_value(duplicate.pattern_tags):
        canonical.pattern_tags = duplicate.pattern_tags
    if not _has_value(canonical.stop_model) and _has_value(duplicate.stop_model):
        canonical.stop_model = duplicate.stop_model
    if not _has_value(canonical.trade_type) and _has_value(duplicate.trade_type):
        canonical.trade_type = duplicate.trade_type
    if not _has_value(canonical.auto_trader_version) and _has_value(duplicate.auto_trader_version):
        canonical.auto_trader_version = duplicate.auto_trader_version
    if not _has_value(canonical.indicator_snapshot) and _has_value(duplicate.indicator_snapshot):
        canonical.indicator_snapshot = duplicate.indicator_snapshot
    if not _has_value(canonical.broker_order_id) and _has_value(duplicate.broker_order_id):
        canonical.broker_order_id = duplicate.broker_order_id
    if not _has_value(canonical.broker_status) and _has_value(duplicate.broker_status):
        canonical.broker_status = duplicate.broker_status
    if not _is_meaningful_number(canonical.entry_price) and _is_meaningful_number(duplicate.entry_price):
        canonical.entry_price = duplicate.entry_price
    if not _is_meaningful_number(canonical.quantity) and _is_meaningful_number(duplicate.quantity):
        canonical.quantity = duplicate.quantity

    dup_entry = getattr(duplicate, "entry_date", None)
    can_entry = getattr(canonical, "entry_date", None)
    if dup_entry is not None and (can_entry is None or dup_entry < can_entry):
        canonical.entry_date = dup_entry

    dup_sync = getattr(duplicate, "last_broker_sync", None)
    can_sync = getattr(canonical, "last_broker_sync", None)
    if dup_sync is not None and (can_sync is None or dup_sync > can_sync):
        canonical.last_broker_sync = dup_sync


def acquire_broker_position_sync_lock(
    db: Session, *, broker_source: str, user_id: int | None,
) -> None:
    """Serialize broker position sync per broker+user transaction."""
    broker_key = (broker_source or "").strip().lower()
    classid = _BROKER_SYNC_LOCK_CLASSIDS.get(broker_key)
    if classid is None:
        raise ValueError(f"Unsupported broker_source for sync lock: {broker_source!r}")
    objid = int(user_id or 0)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:classid, :objid)"),
        {"classid": int(classid), "objid": objid},
    )


def collapse_open_broker_position_duplicates(
    db: Session,
    *,
    broker_source: str,
    user_id: int | None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Cancel duplicate open broker position rows, preserving one canonical trade."""
    broker_key = (broker_source or "").strip().lower()
    now = now or datetime.utcnow()
    rows = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.broker_source == broker_key,
            Trade.status == "open",
        )
        .order_by(Trade.entry_date.asc(), Trade.id.asc())
        .all()
    )

    by_ticker: dict[str, list[Trade]] = defaultdict(list)
    for trade in rows:
        ticker = (trade.ticker or "").upper().strip()
        if not ticker:
            continue
        by_ticker[ticker].append(trade)

    groups = 0
    cancelled = 0
    for ticker, trades in by_ticker.items():
        if len(trades) <= 1:
            continue
        groups += 1
        canonical = max(trades, key=_trade_rank_key)
        duplicates = [trade for trade in trades if trade.id != canonical.id]
        duplicate_ids = [int(trade.id) for trade in duplicates]
        for duplicate in duplicates:
            _merge_duplicate_into_canonical(canonical, duplicate)
            duplicate.status = "cancelled"
            duplicate.exit_date = now
            duplicate.exit_reason = "sync_duplicate"
            note = (
                f"Cancelled as duplicate {broker_key} sync artifact on "
                f"{now.strftime('%Y-%m-%d %H:%M')}. Canonical trade_id={canonical.id}."
            )
            duplicate.notes = ((duplicate.notes or "").rstrip() + "\n" + note).strip()
            cancelled += 1
        if duplicate_ids:
            merge_note = (
                f"Merged duplicate {broker_key} sync rows on "
                f"{now.strftime('%Y-%m-%d %H:%M')}: {duplicate_ids}."
            )
            canon_notes = canonical.notes or ""
            if merge_note not in canon_notes:
                canonical.notes = (canon_notes.rstrip() + "\n" + merge_note).strip()
        logger.warning(
            "[broker_sync] collapsed %d open %s duplicate(s) for user_id=%s ticker=%s -> kept trade_id=%s",
            len(duplicates),
            broker_key,
            user_id,
            ticker,
            canonical.id,
        )

    return {"groups": groups, "cancelled": cancelled}


def dedupe_positions_by_ticker(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate broker position payloads by uppercase ticker."""
    deduped: dict[str, dict[str, Any]] = {}
    for raw in positions or []:
        ticker = str((raw or {}).get("ticker") or "").strip().upper()
        if not ticker:
            continue
        current = dict(raw)
        current["ticker"] = ticker
        existing = deduped.get(ticker)
        if existing is None:
            deduped[ticker] = current
            continue

        existing_qty = _safe_float(existing.get("quantity")) or 0.0
        current_qty = _safe_float(current.get("quantity")) or 0.0
        winner = current if current_qty > existing_qty else existing
        loser = existing if winner is current else current
        for key in (
            "average_buy_price",
            "current_price",
            "equity",
            "percent_change",
            "equity_change",
            "asset_type",
        ):
            if not _has_value(winner.get(key)) and _has_value(loser.get(key)):
                winner[key] = loser.get(key)
        deduped[ticker] = winner
    return list(deduped.values())
