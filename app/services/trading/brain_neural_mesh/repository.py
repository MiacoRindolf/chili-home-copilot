"""DB access: enqueue, claim activation events, load edges/states."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....models.trading import (
    BrainActivationEvent,
    BrainGraphEdge,
    BrainGraphNode,
    BrainNodeState,
)
from .schema import DEFAULT_DOMAIN, DEFAULT_GRAPH_VERSION, LOG_PREFIX

_log = logging.getLogger(__name__)


_REQUIRED_PAYLOAD_FIELDS: dict[str, set[str]] = {
    "momentum_context_refresh": {"signal_type"},
    "brain_market_snapshots": {"signal_type"},
    "learning_cycle_completed": {"signal_type"},
    "brain_work_outcome": {"signal_type", "outcome_type"},
    "fired": {"signal_type", "from_edge"},
}


def _validate_payload(cause: str, payload: Optional[dict[str, Any]]) -> None:
    required = _REQUIRED_PAYLOAD_FIELDS.get(cause)
    if required is None or payload is None:
        return
    missing = required - set(payload.keys())
    if missing:
        _log.warning("%s enqueue payload for cause=%s missing keys: %s", LOG_PREFIX, cause, missing)


MAX_PENDING_QUEUE_DEPTH = 500
MAX_EVENTS_PER_CORRELATION = 100
QUEUE_PRESSURE_PROTECTED_CAUSES = frozenset({
    "brain_market_snapshots",
    "momentum_context_refresh",
})
QUEUE_PRESSURE_SHEDDABLE_CAUSES = frozenset({
    "imminent_eval",
})
QUEUE_PRESSURE_SHED_MIN_AGE_SECONDS = 30 * 60


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def pending_queue_depth(db: Session) -> int:
    return db.query(BrainActivationEvent).filter(BrainActivationEvent.status == "pending").count()


def correlation_event_count(db: Session, correlation_id: str) -> int:
    return (
        db.query(BrainActivationEvent)
        .filter(BrainActivationEvent.correlation_id == correlation_id)
        .count()
    )


def _shed_stale_low_priority_pending_event(
    db: Session,
    *,
    incoming_cause: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Replace stale low-priority backlog with protected refresh events.

    This keeps the global queue cap fixed. Under full-queue pressure, a fresh
    market/momentum context refresh can mark one stale ``imminent_eval`` dead so
    high-volume alert chatter cannot starve context propagation. The row remains
    auditable and is later reaped by normal retention.
    """
    if incoming_cause not in QUEUE_PRESSURE_PROTECTED_CAUSES:
        return None
    now_dt = _naive_utc(now or datetime.now(timezone.utc))
    cutoff = now_dt - timedelta(seconds=QUEUE_PRESSURE_SHED_MIN_AGE_SECONDS)
    stale = (
        db.query(BrainActivationEvent)
        .filter(
            BrainActivationEvent.status == "pending",
            BrainActivationEvent.cause.in_(QUEUE_PRESSURE_SHEDDABLE_CAUSES),
            BrainActivationEvent.created_at <= cutoff,
        )
        .order_by(BrainActivationEvent.created_at.asc(), BrainActivationEvent.id.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if stale is None:
        return None
    created_at = getattr(stale, "created_at", None)
    age_seconds: float | None = None
    if isinstance(created_at, datetime):
        age_seconds = max(
            0.0,
            (now_dt - created_at.replace(tzinfo=None)).total_seconds(),
        )
    info = {
        "event_id": int(stale.id),
        "cause": stale.cause,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
    }
    audit_payload = dict(stale.payload or {})
    audit_payload["_queue_pressure_shed"] = {
        "shed_for_cause": incoming_cause,
        "shed_at": now_dt.isoformat(),
        "age_seconds": info["age_seconds"],
    }
    stale.payload = audit_payload
    stale.status = "dead"
    stale.processed_at = now_dt
    db.flush()
    return info


def enqueue_activation(
    db: Session,
    *,
    source_node_id: Optional[str],
    cause: str,
    payload: Optional[dict[str, Any]] = None,
    confidence_delta: float = 0.0,
    propagation_depth: int = 0,
    correlation_id: Optional[str] = None,
    domain: str = DEFAULT_DOMAIN,
    graph_version: int = DEFAULT_GRAPH_VERSION,
) -> int:
    """Insert a pending activation event. Returns new row id.

    Circuit breakers:
    - Rejects if the pending queue exceeds MAX_PENDING_QUEUE_DEPTH (500).
    - Rejects if the correlation_id already has MAX_EVENTS_PER_CORRELATION (100) events.
    Returns -1 when rejected.
    """
    _validate_payload(cause, payload)
    cid = correlation_id or str(uuid.uuid4())

    # Circuit breaker: per-correlation-id cap
    if correlation_event_count(db, cid) >= MAX_EVENTS_PER_CORRELATION:
        _log.warning(
            "%s enqueue rejected — correlation %s has >= %s events (source=%s)",
            LOG_PREFIX, cid, MAX_EVENTS_PER_CORRELATION, source_node_id,
        )
        return -1

    # Circuit breaker: global queue depth
    queue_depth = pending_queue_depth(db)
    if queue_depth == MAX_PENDING_QUEUE_DEPTH:
        shed = _shed_stale_low_priority_pending_event(db, incoming_cause=cause)
        if shed is not None:
            _log.warning(
                "%s queue pressure shed stale pending event id=%s cause=%s "
                "age_seconds=%s for protected cause=%s",
                LOG_PREFIX,
                shed.get("event_id"),
                shed.get("cause"),
                shed.get("age_seconds"),
                cause,
            )
    if pending_queue_depth(db) >= MAX_PENDING_QUEUE_DEPTH:
        _log.warning(
            "%s enqueue rejected — queue depth >= %s (source=%s cause=%s)",
            LOG_PREFIX, MAX_PENDING_QUEUE_DEPTH, source_node_id, cause,
        )
        return -1

    ev = BrainActivationEvent(
        source_node_id=source_node_id,
        cause=cause,
        payload=payload,
        confidence_delta=float(confidence_delta),
        propagation_depth=int(propagation_depth),
        correlation_id=cid,
        status="pending",
    )
    db.add(ev)
    db.flush()
    _log.debug("%s enqueued event id=%s source=%s cause=%s", LOG_PREFIX, ev.id, source_node_id, cause)
    return int(ev.id)


def claim_pending_batch(db: Session, limit: int = 24) -> list[BrainActivationEvent]:
    """Claim up to ``limit`` pending rows using SKIP LOCKED."""
    sql = text(
        """
        UPDATE brain_activation_events AS e
        SET status = 'processing'
        FROM (
            SELECT id FROM brain_activation_events
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT :lim
            FOR UPDATE SKIP LOCKED
        ) AS sub
        WHERE e.id = sub.id
        RETURNING e.id
        """
    )
    row_ids = [r[0] for r in db.execute(sql, {"lim": limit}).fetchall()]
    if not row_ids:
        return []
    # Expire cached ORM objects so the subsequent query reflects the
    # status='processing' written by the raw SQL UPDATE above.
    db.expire_all()
    return (
        db.query(BrainActivationEvent)
        .filter(BrainActivationEvent.id.in_(row_ids))
        .order_by(BrainActivationEvent.created_at.asc())
        .all()
    )


def mark_event_status(
    db: Session,
    event_id: int,
    status: str,
    *,
    processed_at: Optional[datetime] = None,
) -> None:
    ev = db.query(BrainActivationEvent).filter(BrainActivationEvent.id == event_id).one_or_none()
    if not ev:
        return
    ev.status = status
    if processed_at is not None:
        ev.processed_at = processed_at
    elif status in ("done", "dead"):
        ev.processed_at = datetime.now(timezone.utc)


def outbound_edges(
    db: Session,
    source_node_id: str,
    *,
    graph_version: int = DEFAULT_GRAPH_VERSION,
) -> Sequence[BrainGraphEdge]:
    return (
        db.query(BrainGraphEdge)
        .filter(
            BrainGraphEdge.source_node_id == source_node_id,
            BrainGraphEdge.enabled.is_(True),
            BrainGraphEdge.graph_version == graph_version,
        )
        .order_by(BrainGraphEdge.id.asc())
        .all()
    )


def get_node(db: Session, node_id: str) -> Optional[BrainGraphNode]:
    return db.query(BrainGraphNode).filter(BrainGraphNode.id == node_id).one_or_none()


def get_or_create_state(db: Session, node_id: str) -> BrainNodeState:
    st = db.query(BrainNodeState).filter(BrainNodeState.node_id == node_id).one_or_none()
    if st:
        return st
    st = BrainNodeState(node_id=node_id, activation_score=0.0, confidence=0.5, local_state={})
    db.add(st)
    db.flush()
    return st


def nodes_for_domain(
    db: Session,
    *,
    domain: str = DEFAULT_DOMAIN,
    graph_version: int = DEFAULT_GRAPH_VERSION,
    include_disabled: bool = False,
) -> Sequence[BrainGraphNode]:
    q = db.query(BrainGraphNode).filter(
        BrainGraphNode.domain == domain,
        BrainGraphNode.graph_version == graph_version,
    )
    if not include_disabled:
        q = q.filter(BrainGraphNode.enabled.is_(True))
    return q.order_by(BrainGraphNode.layer.asc(), BrainGraphNode.id.asc()).all()


def reap_dead_events(db: Session, *, older_than_hours: int = 72) -> int:
    """Delete processed/dead activation events older than the retention window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    n = (
        db.query(BrainActivationEvent)
        .filter(
            BrainActivationEvent.status.in_(("dead", "done")),
            BrainActivationEvent.processed_at.isnot(None),
            BrainActivationEvent.processed_at < cutoff,
        )
        .delete(synchronize_session=False)
    )
    if n:
        _log.info("%s reaped %s old activation events (cutoff=%sh)", LOG_PREFIX, n, older_than_hours)
    return n
