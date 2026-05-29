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

_DEAD_RECOVERY_PAYLOAD_KEY = "transient_dead_recovery_count"
_DEAD_RECOVERY_EVENT_TYPES = ("backtest_requested",)
_DEAD_RECOVERY_ERROR_MARKERS = (
    "can't reconnect until invalid transaction is rolled back",
    "current transaction is aborted",
    "infailedsqltransaction",
    "server closed the connection unexpectedly",
    "connection already closed",
    "connection not open",
)
_DEAD_RECOVERY_DEFAULT_LIMIT = 8
_DEAD_RECOVERY_DEFAULT_MAX_PER_EVENT = 3
_DEAD_RECOVERY_DEFAULT_DELAY_SECONDS = 10
_DEAD_DEDUPE_SUPPRESSED = object()


def brain_work_ledger_enabled() -> bool:
    return bool(getattr(settings, "brain_work_ledger_enabled", True))


def _expected_evidence_value(ev: BrainWorkEvent) -> float:
    payload = ev.payload if isinstance(ev.payload, dict) else {}
    try:
        return float(payload.get("expected_evidence_value") or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
    payload_dict = dict(payload or {})
    recovered_dead_id = _reuse_retryable_dead_dedupe(
        db,
        event_type=event_type,
        dedupe_key=dedupe_key,
        payload=payload_dict,
        lease_scope=lease_scope,
        max_attempts=ma,
    )
    if recovered_dead_id is _DEAD_DEDUPE_SUPPRESSED:
        return None
    if recovered_dead_id is not None:
        return int(recovered_dead_id)
    ev = BrainWorkEvent(
        domain="trading",
        event_type=event_type,
        event_kind="work",
        payload=payload_dict,
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
    claimable: Optional[bool] = None,
) -> int | None:
    """Insert an outcome row (audit / UI). Idempotent on dedupe_key for outcomes.

    Phase 1b of f-adaptive-promotion-architecture (2026-05-11):
    When ``settings.chili_brain_outcome_claimable_enabled`` is True, new
    outcome rows are born as ``status='pending'`` / ``processed_at=NULL``
    so the unified ``claim_work_batch`` (event_kind-agnostic) can pick
    them up and run handler logic. Default-off: legacy terminal-at-insert
    behaviour preserved byte-identical.
    """
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
    should_claim = (
        bool(getattr(settings, "chili_brain_outcome_claimable_enabled", False))
        if claimable is None
        else bool(claimable)
    )
    if should_claim:
        status = "pending"
        processed_at = None
        max_attempts = int(getattr(settings, "brain_work_max_attempts_default", 5))
    else:
        status = "done"
        processed_at = now
        max_attempts = 0
    ev = BrainWorkEvent(
        domain="trading",
        event_type=event_type,
        event_kind="outcome",
        payload=dict(payload or {}),
        dedupe_key=dedupe_key,
        lease_scope="general",
        status=status,
        attempts=0,
        max_attempts=max_attempts,
        next_run_at=now,
        correlation_id=cid,
        parent_event_id=parent_event_id,
        processed_at=processed_at,
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


_CLAIM_SQL_WORK_ONLY = text(
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
        ORDER BY
          CASE
            WHEN (payload->>'expected_evidence_value') ~ '^-?[0-9]+(\\.[0-9]+)?$'
            THEN (payload->>'expected_evidence_value')::double precision
            ELSE 0.0
          END DESC,
          next_run_at ASC,
          created_at ASC
        LIMIT :lim
        FOR UPDATE SKIP LOCKED
    ) AS sub
    WHERE w.id = sub.id
    RETURNING w.id
    """
)

# Phase 1b of f-adaptive-promotion-architecture (2026-05-11): broadens
# the claim path so ``event_kind='outcome'`` rows born as pending are
# eligible too. Activated by chili_brain_outcome_claimable_enabled.
_CLAIM_SQL_ANY_KIND = text(
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
          AND event_type = :etype
          AND status IN ('pending', 'retry_wait')
          AND next_run_at <= CURRENT_TIMESTAMP
        ORDER BY
          CASE
            WHEN (payload->>'expected_evidence_value') ~ '^-?[0-9]+(\\.[0-9]+)?$'
            THEN (payload->>'expected_evidence_value')::double precision
            ELSE 0.0
          END DESC,
          next_run_at ASC,
          created_at ASC
        LIMIT :lim
        FOR UPDATE SKIP LOCKED
    ) AS sub
    WHERE w.id = sub.id
    RETURNING w.id
    """
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
    if getattr(settings, "chili_brain_outcome_claimable_enabled", False):
        sql = _CLAIM_SQL_ANY_KIND
    else:
        sql = _CLAIM_SQL_WORK_ONLY
    ids = [
        r[0]
        for r in db.execute(
            sql, {"lim": limit, "holder": holder_id, "lease_until": lease_until, "etype": event_type}
        ).fetchall()
    ]
    if not ids:
        return []
    db.expire_all()
    rows = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.id.in_(ids))
        .all()
    )
    rows.sort(
        key=lambda ev: (
            -_expected_evidence_value(ev),
            ev.next_run_at,
            ev.created_at,
            int(ev.id),
        )
    )
    return rows


_RELEASE_STALE_SQL_WORK_ONLY = text(
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

# Phase 1b of f-adaptive-promotion-architecture (2026-05-11): symmetric
# with the flag-on claim path. Without this, outcome rows claimed under
# the broadened path could strand in 'processing' if the lease expires
# (handler hang / container restart). Flag-off: byte-identical.
_RELEASE_STALE_SQL_ANY_KIND = text(
    """
    UPDATE brain_work_events
    SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'retry_wait' END,
        lease_holder = NULL,
        lease_expires_at = NULL,
        next_run_at = CURRENT_TIMESTAMP,
        processed_at = CASE WHEN attempts >= max_attempts THEN CURRENT_TIMESTAMP ELSE processed_at END,
        updated_at = CURRENT_TIMESTAMP
    WHERE status = 'processing'
      AND lease_expires_at IS NOT NULL
      AND lease_expires_at < CURRENT_TIMESTAMP
    """
)


def release_stale_leases(db: Session) -> int:
    """Reset expired processing leases to retry_wait or dead."""
    if getattr(settings, "chili_brain_outcome_claimable_enabled", False):
        sql = _RELEASE_STALE_SQL_ANY_KIND
    else:
        sql = _RELEASE_STALE_SQL_WORK_ONLY
    r = db.execute(sql)
    db.flush()
    return int(r.rowcount or 0)


def coalesce_duplicate_open_work(
    db: Session,
    *,
    event_types: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Retire duplicate open work rows with the same logical dedupe key.

    Duplicate open rows can appear after historical dead-letter recovery. Keeping
    one logical row preserves the work while preventing one pattern/config from
    consuming multiple queue slots.
    """
    types = tuple(event_types or ())
    q = db.query(BrainWorkEvent).filter(
        BrainWorkEvent.domain == "trading",
        BrainWorkEvent.event_kind == "work",
        BrainWorkEvent.status.in_(("pending", "processing", "retry_wait")),
        BrainWorkEvent.dedupe_key.isnot(None),
    )
    if types:
        q = q.filter(BrainWorkEvent.event_type.in_(types))
    rows = q.all()

    def _rank(row: BrainWorkEvent) -> tuple[int, int, datetime, datetime, int]:
        status_rank = {"processing": 0, "pending": 1, "retry_wait": 2}.get(
            str(row.status or ""),
            3,
        )
        return (
            status_rank,
            int(row.attempts or 0),
            row.next_run_at or datetime.min,
            row.created_at or datetime.min,
            int(row.id),
        )

    groups: dict[tuple[str, str], list[BrainWorkEvent]] = {}
    for row in rows:
        groups.setdefault((str(row.event_type), str(row.dedupe_key)), []).append(row)

    now = datetime.utcnow()
    ids: list[int] = []
    for group_rows in groups.values():
        if len(group_rows) <= 1:
            continue
        group_rows.sort(key=_rank)
        for row in group_rows[1:]:
            if row.status == "processing":
                continue
            payload = dict(row.payload or {}) if isinstance(row.payload, dict) else {}
            payload["duplicate_open_work_suppressed"] = True
            payload["duplicate_open_work_suppressed_at"] = now.isoformat()
            row.payload = payload
            row.status = "done"
            row.processed_at = row.processed_at or now
            row.lease_holder = None
            row.lease_expires_at = None
            row.updated_at = now
            ids.append(int(row.id))
    db.flush()
    return {"ok": True, "coalesced": len(ids), "ids": ids}


def _dead_recovery_marker(error: str | None) -> str | None:
    text_l = (error or "").lower()
    for marker in _DEAD_RECOVERY_ERROR_MARKERS:
        if marker in text_l:
            return marker
    return None


def _payload_int(payload: dict[str, Any], key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _dead_recovery_max_per_event(value: int | None = None) -> int:
    return int(
        value
        if value is not None
        else getattr(
            settings,
            "brain_work_dead_letter_recovery_max_per_event",
            _DEAD_RECOVERY_DEFAULT_MAX_PER_EVENT,
        )
    )


def _dead_recovery_delay_seconds(value: int | None = None) -> int:
    return int(
        value
        if value is not None
        else getattr(
            settings,
            "brain_work_dead_letter_recovery_delay_seconds",
            _DEAD_RECOVERY_DEFAULT_DELAY_SECONDS,
        )
    )


def _recover_retryable_dead_row(
    row: BrainWorkEvent,
    *,
    now: datetime,
    marker: str,
    recovery_count: int,
    max_recoveries: int,
    delay_seconds: int,
    payload_updates: dict[str, Any] | None = None,
    lease_scope: str | None = None,
    max_attempts: int | None = None,
) -> bool:
    if recovery_count >= max(0, max_recoveries):
        return False
    effective_max_attempts = max(
        1,
        int(max_attempts if max_attempts is not None else row.max_attempts or 1),
    )
    payload = dict(row.payload or {}) if isinstance(row.payload, dict) else {}
    if payload_updates:
        payload.update(payload_updates)
    payload[_DEAD_RECOVERY_PAYLOAD_KEY] = recovery_count + 1
    payload["transient_dead_recovered_at"] = now.isoformat()
    payload["transient_dead_recovery_marker"] = marker
    row.payload = payload
    row.status = "retry_wait"
    row.attempts = min(int(row.attempts or 0), effective_max_attempts - 1)
    row.max_attempts = effective_max_attempts
    if lease_scope:
        row.lease_scope = (lease_scope or "general")[:32]
    row.lease_holder = None
    row.lease_expires_at = None
    row.processed_at = None
    row.next_run_at = now + timedelta(seconds=max(0, delay_seconds))
    row.updated_at = now
    return True


def _reuse_retryable_dead_dedupe(
    db: Session,
    *,
    event_type: str,
    dedupe_key: str,
    payload: dict[str, Any],
    lease_scope: str,
    max_attempts: int,
) -> int | object | None:
    if not bool(getattr(settings, "brain_work_dead_letter_reuse_dedupe_enabled", True)):
        return None
    row = (
        db.query(BrainWorkEvent)
        .filter(
            BrainWorkEvent.dedupe_key == dedupe_key,
            BrainWorkEvent.event_type == event_type,
            BrainWorkEvent.event_kind == "work",
            BrainWorkEvent.status == "dead",
        )
        .order_by(
            BrainWorkEvent.updated_at.desc().nullslast(),
            BrainWorkEvent.id.desc(),
        )
        .first()
    )
    if row is None:
        return None
    marker = _dead_recovery_marker(row.last_error)
    if marker is None:
        return None

    row_payload = dict(row.payload or {}) if isinstance(row.payload, dict) else {}
    recovery_count = _payload_int(row_payload, _DEAD_RECOVERY_PAYLOAD_KEY)
    now = datetime.utcnow()
    recovered = _recover_retryable_dead_row(
        row,
        now=now,
        marker=marker,
        recovery_count=recovery_count,
        max_recoveries=_dead_recovery_max_per_event(),
        delay_seconds=_dead_recovery_delay_seconds(),
        payload_updates=payload,
        lease_scope=lease_scope,
        max_attempts=max_attempts,
    )
    if recovered:
        logger.info(
            "%s recovered dead dedupe type=%s id=%s dedupe=%s marker=%s",
            LOG_PREFIX,
            event_type,
            row.id,
            dedupe_key,
            marker,
        )
        db.flush()
        return int(row.id)

    logger.info(
        "%s suppressed duplicate retryable-dead dedupe type=%s id=%s dedupe=%s "
        "recovery_count=%s",
        LOG_PREFIX,
        event_type,
        row.id,
        dedupe_key,
        recovery_count,
    )
    return _DEAD_DEDUPE_SUPPRESSED


def recover_retryable_dead_work(
    db: Session,
    *,
    event_types: tuple[str, ...] | None = None,
    limit: int | None = None,
    max_recoveries_per_event: int | None = None,
    delay_seconds: int | None = None,
) -> dict[str, Any]:
    """Requeue dead work that died from known transient infra/session failures.

    This intentionally does not recover semantic handler failures. The primary
    use case is old backtest work stranded by infrastructure issues after the
    dispatcher/session bug has been fixed.
    """
    if not bool(getattr(settings, "brain_work_dead_letter_recovery_enabled", True)):
        return {"ok": True, "enabled": False, "recovered": 0, "ids": []}

    types = tuple(event_types or _DEAD_RECOVERY_EVENT_TYPES)
    if not types:
        return {"ok": True, "enabled": True, "recovered": 0, "ids": []}

    lim = int(
        limit
        if limit is not None
        else getattr(
            settings,
            "brain_work_dead_letter_recovery_limit",
            _DEAD_RECOVERY_DEFAULT_LIMIT,
        )
    )
    if lim <= 0:
        return {"ok": True, "enabled": True, "recovered": 0, "ids": []}

    max_recoveries = int(
        max_recoveries_per_event
        if max_recoveries_per_event is not None
        else getattr(
            settings,
            "brain_work_dead_letter_recovery_max_per_event",
            _DEAD_RECOVERY_DEFAULT_MAX_PER_EVENT,
        )
    )
    delay = int(
        delay_seconds
        if delay_seconds is not None
        else getattr(
            settings,
            "brain_work_dead_letter_recovery_delay_seconds",
            _DEAD_RECOVERY_DEFAULT_DELAY_SECONDS,
        )
    )
    now = datetime.utcnow()
    rows = (
        db.query(BrainWorkEvent)
        .filter(
            BrainWorkEvent.domain == "trading",
            BrainWorkEvent.event_kind == "work",
            BrainWorkEvent.status == "dead",
            BrainWorkEvent.event_type.in_(types),
        )
        .order_by(
            BrainWorkEvent.processed_at.asc().nullsfirst(),
            BrainWorkEvent.id.asc(),
        )
        .limit(lim * 4)
        .all()
    )

    recovered_ids: list[int] = []
    recovered_by_marker: dict[str, int] = {}
    skipped_non_retryable = 0
    skipped_max_recoveries = 0
    skipped_duplicate_dedupe = 0
    open_dedupe_keys = {
        (str(event_type), str(dedupe_key))
        for event_type, dedupe_key in (
            db.query(BrainWorkEvent.event_type, BrainWorkEvent.dedupe_key)
            .filter(
                BrainWorkEvent.domain == "trading",
                BrainWorkEvent.event_kind == "work",
                BrainWorkEvent.status.in_(("pending", "processing", "retry_wait")),
                BrainWorkEvent.event_type.in_(types),
            )
            .all()
        )
        if dedupe_key is not None
    }
    recovered_dedupe_keys: set[tuple[str, str]] = set()
    for row in rows:
        if len(recovered_ids) >= lim:
            break
        dedupe_key = (
            (str(row.event_type), str(row.dedupe_key))
            if row.dedupe_key is not None
            else None
        )
        if dedupe_key is not None and (
            dedupe_key in open_dedupe_keys or dedupe_key in recovered_dedupe_keys
        ):
            skipped_duplicate_dedupe += 1
            continue
        marker = _dead_recovery_marker(row.last_error)
        if marker is None:
            skipped_non_retryable += 1
            continue
        payload = dict(row.payload or {}) if isinstance(row.payload, dict) else {}
        recovery_count = _payload_int(payload, _DEAD_RECOVERY_PAYLOAD_KEY)
        if recovery_count >= max(0, max_recoveries):
            skipped_max_recoveries += 1
            continue

        max_attempts = max(1, int(row.max_attempts or 1))
        payload[_DEAD_RECOVERY_PAYLOAD_KEY] = recovery_count + 1
        payload["transient_dead_recovered_at"] = now.isoformat()
        payload["transient_dead_recovery_marker"] = marker
        row.payload = payload
        row.status = "retry_wait"
        row.attempts = min(int(row.attempts or 0), max_attempts - 1)
        row.max_attempts = max_attempts
        row.lease_holder = None
        row.lease_expires_at = None
        row.processed_at = None
        row.next_run_at = now + timedelta(seconds=max(0, delay))
        row.updated_at = now
        recovered_ids.append(int(row.id))
        if dedupe_key is not None:
            recovered_dedupe_keys.add(dedupe_key)
        recovered_by_marker[marker] = recovered_by_marker.get(marker, 0) + 1

    db.flush()
    return {
        "ok": True,
        "enabled": True,
        "event_types": list(types),
        "recovered": len(recovered_ids),
        "ids": recovered_ids,
        "recovered_by_marker": recovered_by_marker,
        "skipped_non_retryable": skipped_non_retryable,
        "skipped_max_recoveries": skipped_max_recoveries,
        "skipped_duplicate_dedupe": skipped_duplicate_dedupe,
    }


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


def _payload_dict(ev: BrainWorkEvent) -> dict[str, Any]:
    return ev.payload if isinstance(ev.payload, dict) else {}


def _last_done_timestamps_by_type(
    db: Session,
    event_types: list[str],
) -> dict[str, datetime]:
    if not event_types:
        return {}
    rows = (
        db.query(BrainWorkEvent.event_type, func.max(BrainWorkEvent.processed_at))
        .filter(
            BrainWorkEvent.domain == "trading",
            BrainWorkEvent.status == "done",
            BrainWorkEvent.event_type.in_(event_types),
        )
        .group_by(BrainWorkEvent.event_type)
        .all()
    )
    return {str(event_type): processed_at for event_type, processed_at in rows if event_type and processed_at}


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
    dead_by_type_rows = (
        db.query(BrainWorkEvent.event_type, func.count(BrainWorkEvent.id))
        .filter(
            dom,
            BrainWorkEvent.status == "dead",
            BrainWorkEvent.processed_at >= now - timedelta(hours=24),
        )
        .group_by(BrainWorkEvent.event_type)
        .all()
    )
    dead_by_type_24h = {str(et): int(c) for et, c in dead_by_type_rows}

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
    last_done_timestamps = _last_done_timestamps_by_type(db, event_types)
    last_done_by_type: dict[str, str | None] = {
        et_s: (
            last_done_timestamps[et_s].isoformat()
            if last_done_timestamps.get(et_s)
            else None
        )
        for et_s in event_types
    }

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
    recent_dead = (
        db.query(BrainWorkEvent)
        .filter(dom, BrainWorkEvent.status == "dead")
        .order_by(
            BrainWorkEvent.processed_at.desc().nullslast(),
            BrainWorkEvent.updated_at.desc().nullslast(),
            BrainWorkEvent.id.desc(),
        )
        .limit(min(recent_limit, 12))
        .all()
    )

    exec_outcome_types = ("live_trade_closed", "broker_fill_closed", "paper_trade_closed")
    cut24 = now - timedelta(hours=24)
    exec_24_rows = (
        db.query(BrainWorkEvent.event_type, func.count(BrainWorkEvent.id))
        .filter(
            dom,
            BrainWorkEvent.event_kind == "outcome",
            BrainWorkEvent.status == "done",
            BrainWorkEvent.event_type.in_(exec_outcome_types),
            BrainWorkEvent.processed_at >= cut24,
        )
        .group_by(BrainWorkEvent.event_type)
        .all()
    )
    execution_outcomes_24h = {str(et): int(c) for et, c in exec_24_rows}

    pulse_types = ("live_trade_closed", "broker_fill_closed")
    pulse_row = (
        db.query(BrainWorkEvent)
        .filter(
            dom,
            BrainWorkEvent.event_kind == "outcome",
            BrainWorkEvent.status == "done",
            BrainWorkEvent.event_type.in_(pulse_types),
        )
        .order_by(BrainWorkEvent.processed_at.desc().nullslast(), BrainWorkEvent.id.desc())
        .first()
    )
    execution_pulse: dict[str, Any] | None = None
    if pulse_row:
        plp = pulse_row.payload if isinstance(pulse_row.payload, dict) else {}
        execution_pulse = {
            "event_type": pulse_row.event_type,
            "ticker": plp.get("ticker"),
            "scan_pattern_id": plp.get("scan_pattern_id"),
            "pnl": plp.get("pnl"),
            "broker_source": plp.get("broker_source"),
            "source": plp.get("source"),
            "trade_id": plp.get("trade_id"),
            "processed_at": pulse_row.processed_at.isoformat() if pulse_row.processed_at else None,
        }

    return {
        "enabled": brain_work_ledger_enabled(),
        "pending_work": int(pending),
        "retry_wait": int(retry_wait),
        "dead_last_24h": int(dead_24h),
        "dead_by_type_24h": dead_by_type_24h,
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
        "recent_dead_letters": [
            {
                "id": int(r.id),
                "event_type": r.event_type,
                "event_kind": r.event_kind,
                "lease_scope": getattr(r, "lease_scope", None) or "general",
                "scan_pattern_id": _payload_dict(r).get("scan_pattern_id"),
                "source": _payload_dict(r).get("source"),
                "attempts": int(r.attempts or 0),
                "max_attempts": int(r.max_attempts or 0),
                "last_error": (r.last_error or "")[:500],
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "processed_at": r.processed_at.isoformat() if r.processed_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in recent_dead
        ],
        "execution_outcomes_24h": execution_outcomes_24h,
        "execution_pulse": execution_pulse,
    }
