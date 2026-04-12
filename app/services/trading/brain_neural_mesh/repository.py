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


def pending_queue_depth(db: Session) -> int:
    return db.query(BrainActivationEvent).filter(BrainActivationEvent.status == "pending").count()


def correlation_event_count(db: Session, correlation_id: str) -> int:
    return (
        db.query(BrainActivationEvent)
        .filter(BrainActivationEvent.correlation_id == correlation_id)
        .count()
    )


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

    # Circuit breaker: global queue depth
    if pending_queue_depth(db) >= MAX_PENDING_QUEUE_DEPTH:
        _log.warning(
            "%s enqueue rejected — queue depth >= %s (source=%s cause=%s)",
            LOG_PREFIX, MAX_PENDING_QUEUE_DEPTH, source_node_id, cause,
        )
        return -1

    # Circuit breaker: per-correlation-id cap
    if correlation_event_count(db, cid) >= MAX_EVENTS_PER_CORRELATION:
        _log.warning(
            "%s enqueue rejected — correlation %s has >= %s events (source=%s)",
            LOG_PREFIX, cid, MAX_EVENTS_PER_CORRELATION, source_node_id,
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
) -> Sequence[BrainGraphNode]:
    return (
        db.query(BrainGraphNode)
        .filter(BrainGraphNode.domain == domain, BrainGraphNode.graph_version == graph_version)
        .order_by(BrainGraphNode.layer.asc(), BrainGraphNode.id.asc())
        .all()
    )


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
