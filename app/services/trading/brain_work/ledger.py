"""Postgres-backed durable work queue: enqueue, claim, complete, fail (retries + dead-letter)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import BrainWorkEvent

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work_ledger]"


def brain_work_ledger_enabled() -> bool:
    return bool(getattr(settings, "brain_work_ledger_enabled", True))


def enqueue_work_event(
    db: Session,
    *,
    event_type: str,
    dedupe_key: str,
    payload: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    parent_event_id: Optional[int] = None,
    max_attempts: Optional[int] = None,
    lease_scope: str = "general",
) -> int | None:
    """Insert a pending work item. Returns id, or None if an open row with same dedupe_key exists or disabled."""
    if not brain_work_ledger_enabled():
        return None
    ma = max_attempts if max_attempts is not None else int(
        getattr(settings, "brain_work_max_attempts_default", 5)
    )
    cid = correlation_id or str(uuid.uuid4())
    open_exists = (
        db.query(BrainWorkEvent.id)
        .filter(
            BrainWorkEvent.dedupe_key == dedupe_key,
            BrainWorkEvent.status.in_(("pending", "processing", "retry_wait")),
        )
        .first()
    )
    if open_exists:
        return None
    ev = BrainWorkEvent(
        domain="trading",
        event_type=event_type,
        event_kind="work",
        payload=dict(payload or {}),
        dedupe_key=dedupe_key,
        lease_scope=(lease_scope or "general")[:32],
        status="pending",
        attempts=0,
        max_attempts=ma,
        next_run_at=datetime.utcnow(),
        correlation_id=cid,
        parent_event_id=parent_event_id,
    )
    db.add(ev)
    db.flush()
    logger.debug("%s enqueued type=%s id=%s dedupe=%s", LOG_PREFIX, event_type, ev.id, dedupe_key)
    return int(ev.id)


def enqueue_outcome_event(
    db: Session,
    *,
    event_type: str,
    dedupe_key: str,
    payload: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    parent_event_id: Optional[int] = None,
) -> int | None:
    """Insert a completed outcome row (audit / UI). Idempotent on dedupe_key for outcomes."""
    if not brain_work_ledger_enabled():
        return None
    ex = (
        db.query(BrainWorkEvent.id)
        .filter(
            BrainWorkEvent.dedupe_key == dedupe_key,
            BrainWorkEvent.event_kind == "outcome",
        )
        .first()
    )
    if ex:
        return int(ex[0])
    cid = correlation_id or str(uuid.uuid4())
    now = datetime.utcnow()
    ev = BrainWorkEvent(
        domain="trading",
        event_type=event_type,
        event_kind="outcome",
        payload=dict(payload or {}),
        dedupe_key=dedupe_key,
        lease_scope="general",
        status="done",
        attempts=0,
        max_attempts=0,
        next_run_at=now,
        correlation_id=cid,
        parent_event_id=parent_event_id,
        processed_at=now,
    )
    db.add(ev)
    db.flush()
    return int(ev.id)


def enqueue_or_refresh_debounced_work(
    db: Session,
    *,
    event_type: str,
    dedupe_key: str,
    payload: dict[str, Any],
    debounce_seconds: int,
    lease_scope: str = "general",
    max_attempts: Optional[int] = None,
) -> int | None:
    """Merge payload and push *next_run_at* out for debounced handler work (same open dedupe_key)."""
    if not brain_work_ledger_enabled():
        return None
    now = datetime.utcnow()
    due = now + timedelta(seconds=max(1, int(debounce_seconds)))
    row = (
        db.query(BrainWorkEvent)
        .filter(
            BrainWorkEvent.dedupe_key == dedupe_key,
            BrainWorkEvent.status.in_(("pending", "processing", "retry_wait")),
        )
        .first()
    )
    scope = (lease_scope or "general")[:32]
    if row:
        base = dict(row.payload or {}) if isinstance(row.payload, dict) else {}
        for k, v in (payload or {}).items():
            base[k] = v
        row.payload = base
        row.next_run_at = due
        row.lease_scope = scope
        row.updated_at = now
        db.flush()
        return int(row.id)
    return enqueue_work_event(
        db,
        event_type=event_type,
        dedupe_key=dedupe_key,
        payload=payload,
        lease_scope=scope,
        max_attempts=max_attempts,
    )


def claim_work_batch(
    db: Session,
    *,
    limit: int,
    lease_seconds: int,
    holder_id: str,
    event_type: str = "backtest_requested",
) -> list[BrainWorkEvent]:
    """Claim pending/retry_wait rows due now (SKIP LOCKED). Increments attempts."""
    if limit <= 0:
        return []
    lease_until = datetime.utcnow() + timedelta(seconds=max(5, lease_seconds))
    sql = text(
        """
        UPDATE brain_work_events AS w
        SET status = 'processing',
            lease_holder = :holder,
            lease_expires_at = :lease_until,
            attempts = w.attempts + 1,
            updated_at = CURRENT_TIMESTAMP
        FROM (
            SELECT id FROM brain_work_events
            WHERE domain = 'trading'
              AND event_kind = 'work'
              AND event_type = :etype
              AND status IN ('pending', 'retry_wait')
              AND next_run_at <= CURRENT_TIMESTAMP
            ORDER BY created_at ASC
            LIMIT :lim
            FOR UPDATE SKIP LOCKED
        ) AS sub
        WHERE w.id = sub.id
        RETURNING w.id
        """
    )
    ids = [
        r[0]
        for r in db.execute(
            sql, {"lim": limit, "holder": holder_id, "lease_until": lease_until, "etype": event_type}
        ).fetchall()
    ]
    if not ids:
        return []
    db.expire_all()
    return (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.id.in_(ids))
        .order_by(BrainWorkEvent.created_at.asc())
        .all()
    )


def release_stale_leases(db: Session) -> int:
    """Reset expired processing leases to retry_wait or dead."""
    r = db.execute(
        text(
            """
            UPDATE brain_work_events
            SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'retry_wait' END,
                lease_holder = NULL,
                lease_expires_at = NULL,
                next_run_at = CURRENT_TIMESTAMP,
                processed_at = CASE WHEN attempts >= max_attempts THEN CURRENT_TIMESTAMP ELSE processed_at END,
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing'
              AND event_kind = 'work'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < CURRENT_TIMESTAMP
            """
        )
    )
    db.flush()
    return int(r.rowcount or 0)


def mark_work_done(db: Session, event_id: int) -> None:
    ev = db.query(BrainWorkEvent).filter(BrainWorkEvent.id == event_id).one_or_none()
    if not ev:
        return
    now = datetime.utcnow()
    ev.status = "done"
    ev.processed_at = now
    ev.lease_holder = None
    ev.lease_expires_at = None
    ev.last_error = None
    ev.updated_at = now


def mark_work_retry_or_dead(db: Session, event_id: int, error: str) -> None:
    ev = db.query(BrainWorkEvent).filter(BrainWorkEvent.id == event_id).one_or_none()
    if not ev:
        return
    now = datetime.utcnow()
    ev.last_error = (error or "")[:4000]
    ev.lease_holder = None
    ev.lease_expires_at = None
    ev.updated_at = now
    base = int(getattr(settings, "brain_work_retry_base_seconds", 30))
    mult = int(getattr(settings, "brain_work_retry_multiplier", 2))
    delay = min(3600, base * (mult ** max(0, ev.attempts - 1)))
    if ev.attempts >= ev.max_attempts:
        ev.status = "dead"
        ev.processed_at = now
    else:
        ev.status = "retry_wait"
        ev.next_run_at = now + timedelta(seconds=delay)


def get_work_ledger_summary(db: Session, *, recent_limit: int = 20) -> dict[str, Any]:
    """Counts, per-type pending/retry, processing leases, and recent outcomes for API/UI."""
    now = datetime.utcnow()
    wk = BrainWorkEvent.event_kind == "work"
    dom = BrainWorkEvent.domain == "trading"

    pending = (
        db.query(func.count(BrainWorkEvent.id))
        .filter(dom, wk, BrainWorkEvent.status.in_(("pending", "processing", "retry_wait")))
        .scalar()
        or 0
    )
    retry_wait = (
        db.query(func.count(BrainWorkEvent.id))
        .filter(dom, wk, BrainWorkEvent.status == "retry_wait")
        .scalar()
        or 0
    )
    dead_24h = (
        db.query(func.count(BrainWorkEvent.id))
        .filter(
            dom,
            BrainWorkEvent.status == "dead",
            BrainWorkEvent.processed_at >= now - timedelta(hours=24),
        )
        .scalar()
        or 0
    )

    pending_by_type_rows = (
        db.query(BrainWorkEvent.event_type, func.count(BrainWorkEvent.id))
        .filter(dom, wk, BrainWorkEvent.status.in_(("pending", "processing", "retry_wait")))
        .group_by(BrainWorkEvent.event_type)
        .all()
    )
    pending_by_type = {str(et): int(c) for et, c in pending_by_type_rows}

    processing = (
        db.query(BrainWorkEvent)
        .filter(dom, wk, BrainWorkEvent.status == "processing")
        .order_by(BrainWorkEvent.lease_expires_at.asc().nullslast())
        .limit(24)
        .all()
    )

    type_rows = db.query(BrainWorkEvent.event_type).filter(dom).distinct().all()
    event_types = [str(r[0]) for r in type_rows if r[0] is not None]
    last_done_by_type: dict[str, str | None] = {}
    for et_s in event_types:
        row = (
            db.query(BrainWorkEvent)
            .filter(
                dom,
                BrainWorkEvent.status == "done",
                BrainWorkEvent.event_type == et_s,
            )
            .order_by(BrainWorkEvent.processed_at.desc().nullslast(), BrainWorkEvent.id.desc())
            .first()
        )
        last_done_by_type[et_s] = row.processed_at.isoformat() if row and row.processed_at else None

    recent = (
        db.query(BrainWorkEvent)
        .filter(dom)
        .filter(BrainWorkEvent.status == "done")
        .order_by(BrainWorkEvent.processed_at.desc().nullslast(), BrainWorkEvent.id.desc())
        .limit(recent_limit)
        .all()
    )
    recent_outcomes = (
        db.query(BrainWorkEvent)
        .filter(dom, BrainWorkEvent.event_kind == "outcome")
        .order_by(BrainWorkEvent.processed_at.desc().nullslast(), BrainWorkEvent.id.desc())
        .limit(min(recent_limit, 12))
        .all()
    )
    return {
        "enabled": brain_work_ledger_enabled(),
        "pending_work": int(pending),
        "retry_wait": int(retry_wait),
        "dead_last_24h": int(dead_24h),
        "pending_by_type": pending_by_type,
        "processing": [
            {
                "id": r.id,
                "event_type": r.event_type,
                "lease_scope": getattr(r, "lease_scope", None) or "general",
                "lease_holder": r.lease_holder,
                "lease_expires_at": r.lease_expires_at.isoformat() if r.lease_expires_at else None,
                "attempts": r.attempts,
            }
            for r in processing
        ],
        "last_done_by_type": last_done_by_type,
        "recent_completions": [
            {
                "id": r.id,
                "event_type": r.event_type,
                "event_kind": r.event_kind,
                "payload": r.payload or {},
                "processed_at": r.processed_at.isoformat() if r.processed_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in recent
        ],
        "recent_meaningful_outcomes": [
            {
                "id": r.id,
                "event_type": r.event_type,
                "payload": r.payload or {},
                "processed_at": r.processed_at.isoformat() if r.processed_at else None,
            }
            for r in recent_outcomes
        ],
    }
