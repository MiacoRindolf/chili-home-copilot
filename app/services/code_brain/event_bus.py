"""Code Brain event bus — durable, DB-backed event queue.

This replaces the dumb 60-second timer that drove the old
``run_code_learning_cycle`` job. Instead of polling LLMs on a fixed
interval, the brain reacts to *events*:

  * ``plan_task_ready``    — a plan_tasks row hit ``ready_for_dispatch``
  * ``source_changed``     — files in the bound repo were modified
  * ``validation_failed``  — a validation run flagged regressions
  * ``ci_failed``          — external CI dispatch reported failure
  * ``pattern_drift``      — pattern_miner observed teacher/student divergence
  * ``operator_request``   — a human pressed "run now" in the Brain UI
  * ``debt_aged``          — TODO/FIXME crossed an age threshold

Events are written to ``code_brain_events`` (durable across restarts).
A single processor thread claims them with ``SELECT ... FOR UPDATE SKIP
LOCKED`` so we can safely run multiple workers later if we choose.

Mirrors the trading brain's ``brain_io_concurrency`` and event-driven
patterns (Coinbase WS → autotrader, alert bus → pattern_position_monitor).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Canonical event-type vocabulary. Add here, never magic-string in callers.
EVENT_PLAN_TASK_READY = "plan_task_ready"
EVENT_SOURCE_CHANGED = "source_changed"
EVENT_VALIDATION_FAILED = "validation_failed"
EVENT_CI_FAILED = "ci_failed"
EVENT_PATTERN_DRIFT = "pattern_drift"
EVENT_OPERATOR_REQUEST = "operator_request"
EVENT_DEBT_AGED = "debt_aged"
EVENT_REPLAY_DECISION = "replay_decision"  # operator-driven re-route of an old run

KNOWN_EVENT_TYPES = frozenset({
    EVENT_PLAN_TASK_READY,
    EVENT_SOURCE_CHANGED,
    EVENT_VALIDATION_FAILED,
    EVENT_CI_FAILED,
    EVENT_PATTERN_DRIFT,
    EVENT_OPERATOR_REQUEST,
    EVENT_DEBT_AGED,
    EVENT_REPLAY_DECISION,
})


@dataclass
class CodeBrainEvent:
    id: int
    event_type: str
    subject_kind: Optional[str]
    subject_id: Optional[int]
    payload: dict
    priority: int
    enqueued_at: datetime


def _coerce_payload(payload: Optional[dict]) -> str:
    if payload is None:
        return "{}"
    return json.dumps(payload, ensure_ascii=False, default=str)


def enqueue(
    db: Session,
    *,
    event_type: str,
    subject_kind: Optional[str] = None,
    subject_id: Optional[int] = None,
    payload: Optional[dict] = None,
    priority: int = 5,
    dedupe: bool = True,
) -> int:
    """Insert an event. Returns the new event id.

    With ``dedupe=True`` (default), if an unclaimed event with the same
    (event_type, subject_kind, subject_id) is already queued, do not insert
    a duplicate — return the existing id. This prevents an over-eager
    trigger watcher from spamming the queue with the same event each tick.
    """
    if event_type not in KNOWN_EVENT_TYPES:
        raise ValueError(f"unknown event_type {event_type!r}")
    if not isinstance(priority, int) or priority < 0 or priority > 9:
        raise ValueError(f"priority must be 0..9, got {priority!r}")

    if dedupe and subject_kind and subject_id is not None:
        existing = db.execute(
            text(
                "SELECT id FROM code_brain_events "
                "WHERE event_type = :t "
                "  AND subject_kind = :k "
                "  AND subject_id = :s "
                "  AND claimed_at IS NULL "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"t": event_type, "k": subject_kind, "s": subject_id},
        ).fetchone()
        if existing:
            return int(existing[0])

    row = db.execute(
        text(
            "INSERT INTO code_brain_events "
            "(event_type, subject_kind, subject_id, payload, priority) "
            "VALUES (:t, :k, :s, CAST(:p AS jsonb), :pr) "
            "RETURNING id"
        ),
        {
            "t": event_type,
            "k": subject_kind,
            "s": subject_id,
            "p": _coerce_payload(payload),
            "pr": int(priority),
        },
    ).fetchone()
    db.commit()
    new_id = int(row[0])
    logger.info(
        "[code_brain.event_bus] enqueued id=%d type=%s subject=%s/%s priority=%d",
        new_id, event_type, subject_kind, subject_id, priority,
    )
    return new_id


def claim_next(
    db: Session,
    *,
    worker_id: str,
    event_types: Optional[list[str]] = None,
) -> Optional[CodeBrainEvent]:
    """Atomically claim the highest-priority unclaimed event.

    Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so multiple workers can
    safely race on the queue (we run only one processor today, but the
    semantics keep us future-proof). Caller is expected to ``mark_processed``
    once the routing decision and action complete.

    Returns ``None`` if the queue is empty.
    """
    if event_types is not None and not all(t in KNOWN_EVENT_TYPES for t in event_types):
        raise ValueError(f"invalid event_types filter: {event_types}")

    where_extra = ""
    params: dict[str, Any] = {"w": worker_id}
    if event_types:
        where_extra = " AND event_type = ANY(:types)"
        params["types"] = list(event_types)

    sql = (
        "WITH claimed AS ("
        "  SELECT id FROM code_brain_events "
        "  WHERE claimed_at IS NULL"
        f"{where_extra}"
        "  ORDER BY priority ASC, enqueued_at ASC "
        "  LIMIT 1 FOR UPDATE SKIP LOCKED"
        ") "
        "UPDATE code_brain_events e "
        "SET claimed_at = NOW(), claimed_by = :w "
        "FROM claimed "
        "WHERE e.id = claimed.id "
        "RETURNING e.id, e.event_type, e.subject_kind, e.subject_id, "
        "          e.payload, e.priority, e.enqueued_at"
    )
    row = db.execute(text(sql), params).fetchone()
    db.commit()
    if not row:
        return None

    payload = row[4]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    ev = CodeBrainEvent(
        id=int(row[0]),
        event_type=str(row[1]),
        subject_kind=(row[2] if row[2] else None),
        subject_id=(int(row[3]) if row[3] is not None else None),
        payload=payload,
        priority=int(row[5]),
        enqueued_at=row[6],
    )
    logger.info(
        "[code_brain.event_bus] claimed id=%d type=%s by=%s",
        ev.id, ev.event_type, worker_id,
    )
    return ev


def mark_processed(
    db: Session,
    event_id: int,
    *,
    outcome: str,
    error_message: Optional[str] = None,
) -> None:
    """Finalize an event after the routed action completes.

    ``outcome`` is one of: ``success``, ``failure``, ``escalated``, ``skipped``.
    """
    if outcome not in {"success", "failure", "escalated", "skipped"}:
        raise ValueError(f"invalid outcome {outcome!r}")
    db.execute(
        text(
            "UPDATE code_brain_events "
            "SET processed_at = NOW(), outcome = :o, error_message = :e "
            "WHERE id = :id"
        ),
        {
            "o": outcome,
            "e": (error_message[:4000] if error_message else None),
            "id": int(event_id),
        },
    )
    db.commit()
    logger.info(
        "[code_brain.event_bus] marked id=%d outcome=%s%s",
        event_id, outcome,
        f" error={error_message[:120]!r}" if error_message else "",
    )


def reap_stuck_claims(db: Session, *, age_minutes: int = 30) -> int:
    """Re-open events that were claimed but never processed.

    Mirrors the trading reaper pattern. A worker that crashes mid-event
    leaves ``claimed_at`` set but ``processed_at`` NULL. We unclaim those
    so another worker can retry. Returns the count of reaped events.
    """
    row = db.execute(
        text(
            "UPDATE code_brain_events "
            "SET claimed_at = NULL, claimed_by = NULL "
            "WHERE claimed_at IS NOT NULL "
            "  AND processed_at IS NULL "
            "  AND claimed_at < NOW() - (:m || ' minutes')::interval "
            "RETURNING id"
        ),
        {"m": int(age_minutes)},
    ).fetchall()
    db.commit()
    n = len(row or [])
    if n:
        logger.warning(
            "[code_brain.event_bus] reaped %d stuck events older than %dm",
            n, age_minutes,
        )
    return n


def queue_depth(db: Session) -> int:
    """Number of unclaimed events. Used by status endpoint."""
    row = db.execute(
        text("SELECT COUNT(*) FROM code_brain_events WHERE claimed_at IS NULL")
    ).fetchone()
    return int(row[0]) if row else 0
