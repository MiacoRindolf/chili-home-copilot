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
_DEAD_RECOVERY_CAP_RESET_PAYLOAD_KEY = "transient_dead_recovery_cap_reset_count"
_DEAD_RECOVERY_TOTAL_PAYLOAD_KEY = "transient_dead_recovery_total_count"
_DEAD_RECOVERY_DEFAULT_CAP_RESET_DELAY_SECONDS = 3600
_DEAD_RECOVERY_DEFAULT_MAX_CAP_RESETS = 2


def brain_work_ledger_enabled() -> bool:
    return bool(getattr(settings, "brain_work_ledger_enabled", True))


def _expected_evidence_value(ev: BrainWorkEvent) -> float:
    payload = ev.payload if isinstance(ev.payload, dict) else {}
    try:
        return float(payload.get("expected_evidence_value") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _transient_recovery_claim_rank(ev: BrainWorkEvent) -> int:
    payload = ev.payload if isinstance(ev.payload, dict) else {}
    if (
        str(ev.event_type or "") == "backtest_requested"
        and str(ev.status or "") == "processing"
        and _DEAD_RECOVERY_PAYLOAD_KEY in payload
    ):
        return 0
    return 1


def _recent_done_dedupe_cutoff() -> datetime | None:
    """Return the central completed-work dedupe cutoff, or None when disabled."""
    try:
        minutes = int(getattr(settings, "brain_work_recent_done_dedupe_minutes", 120))
    except (TypeError, ValueError):
        minutes = 120
    if minutes <= 0:
        return None
    return datetime.utcnow() - timedelta(minutes=minutes)


def _recent_completed_work_exists(
    db: Session,
    *,
    event_type: str,
    dedupe_key: str,
) -> bool:
    """True when identical fingerprinted work already completed recently.

    Dedupe keys are expected to include the evidence/config fingerprint. A
    recent done row therefore means rerunning the same work would only churn
    the queue until new evidence changes the key.
    """
    cutoff = _recent_done_dedupe_cutoff()
    if cutoff is None:
        return False
    return (
        db.query(BrainWorkEvent.id)
        .filter(
            BrainWorkEvent.domain == "trading",
            BrainWorkEvent.event_kind == "work",
            BrainWorkEvent.event_type == event_type,
            BrainWorkEvent.dedupe_key == dedupe_key,
            BrainWorkEvent.status == "done",
            BrainWorkEvent.updated_at >= cutoff,
        )
        .order_by(BrainWorkEvent.updated_at.desc().nullslast(), BrainWorkEvent.id.desc())
        .first()
        is not None
    )


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
    """Insert a pending work item.

    Returns id, or None if an equivalent open/recently-completed row exists or
    the ledger is disabled.
    """
    if not brain_work_ledger_enabled():
        return None
    ma = max_attempts if max_attempts is not None else int(
        getattr(settings, "brain_work_max_attempts_default", 5)
    )
    cid = correlation_id or str(uuid.uuid4())
    open_exists = (
        db.query(BrainWorkEvent.id)
        .filter(
            BrainWorkEvent.domain == "trading",
            BrainWorkEvent.event_kind == "work",
            BrainWorkEvent.event_type == event_type,
            BrainWorkEvent.dedupe_key == dedupe_key,
            BrainWorkEvent.status.in_(("pending", "processing", "retry_wait")),
        )
        .first()
    )
    if open_exists:
        return None
    if _recent_completed_work_exists(
        db,
        event_type=event_type,
        dedupe_key=dedupe_key,
    ):
        logger.debug(
            "%s suppressed recent completed duplicate type=%s dedupe=%s",
            LOG_PREFIX,
            event_type,
            dedupe_key,
        )
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
          CASE
            WHEN event_type = 'backtest_requested'
             AND status = 'retry_wait'
             AND payload::jsonb ? 'transient_dead_recovery_count'
            THEN 0
            ELSE 1
          END ASC,
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
          CASE
            WHEN event_type = 'backtest_requested'
             AND status = 'retry_wait'
             AND payload::jsonb ? 'transient_dead_recovery_count'
            THEN 0
            ELSE 1
          END ASC,
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
            _transient_recovery_claim_rank(ev),
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
    if not types or "market_snapshots_batch" in types:
        # ``market_snapshots_batch`` is born from the outcome lane, but when
        # claimable outcomes are enabled it behaves as executable work.
        rows.extend(
            db.query(BrainWorkEvent)
            .filter(
                BrainWorkEvent.domain == "trading",
                BrainWorkEvent.event_kind == "outcome",
                BrainWorkEvent.event_type == "market_snapshots_batch",
                BrainWorkEvent.status.in_(("pending", "processing", "retry_wait")),
                BrainWorkEvent.dedupe_key.isnot(None),
            )
            .all()
        )

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
    reasons: dict[str, int] = {}

    def _retire_duplicate(row: BrainWorkEvent, *, reason: str, keep_id: int | None = None) -> None:
        payload = dict(row.payload or {}) if isinstance(row.payload, dict) else {}
        payload["duplicate_open_work_suppressed"] = True
        payload["duplicate_open_work_suppressed_at"] = now.isoformat()
        payload["duplicate_open_work_suppressed_reason"] = reason
        if keep_id is not None:
            payload["duplicate_open_work_kept_event_id"] = int(keep_id)
        row.payload = payload
        row.status = "done"
        row.processed_at = row.processed_at or now
        row.lease_holder = None
        row.lease_expires_at = None
        row.updated_at = now
        ids.append(int(row.id))
        reasons[reason] = reasons.get(reason, 0) + 1

    for group_rows in groups.values():
        if len(group_rows) <= 1:
            continue
        group_rows.sort(key=_rank)
        keep_id = int(group_rows[0].id)
        for row in group_rows[1:]:
            if row.status == "processing":
                continue
            _retire_duplicate(row, reason="same_dedupe_key", keep_id=keep_id)

    # Recert rescue refreshes can be emitted by cash deployment, reliability
    # snapshots, and live signal fast-lanes. Different evidence fingerprints are
    # useful over time, but concurrent open refreshes for the same pattern/asset
    # all diagnose the same live-trading blocker. Keep one so the edge lane
    # cannot be crowded by one recert debt pocket.
    recert_refresh_groups: dict[tuple[int, str], list[BrainWorkEvent]] = {}
    for row in rows:
        if row.status not in ("pending", "processing", "retry_wait"):
            continue
        if str(row.event_type or "") != "recert_rescue_refresh":
            continue
        payload = row.payload if isinstance(row.payload, dict) else {}
        try:
            pid = int(payload.get("scan_pattern_id") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0:
            continue
        asset = str(payload.get("asset_class") or "all").strip().lower() or "all"
        recert_refresh_groups.setdefault((pid, asset), []).append(row)

    def _recert_refresh_rank(row: BrainWorkEvent) -> tuple[int, int, float, int]:
        status_rank = 0 if row.status == "processing" else 1
        updated = row.updated_at or row.created_at or datetime.min
        return (
            status_rank,
            int(row.attempts or 0),
            -updated.timestamp(),
            -int(row.id),
        )

    for group_rows in recert_refresh_groups.values():
        if len(group_rows) <= 1:
            continue
        group_rows.sort(key=_recert_refresh_rank)
        keep_id = int(group_rows[0].id)
        for row in group_rows[1:]:
            if row.status == "processing":
                continue
            _retire_duplicate(
                row,
                reason="recert_rescue_refresh_pattern_asset_superseded",
                keep_id=keep_id,
            )

    # Recert rescue backtests are fingerprinted by evidence slice so a genuinely
    # new evidence snapshot can request fresh work. In production those refreshes
    # can race for the same pattern/asset while a previous refresh is still open,
    # which lets one recert parent consume many scarce backtest slots. Keep one
    # open row per pattern/asset and let the next reliability pass enqueue a new
    # fingerprint after the current refresh completes.
    recert_groups: dict[tuple[str, str], list[BrainWorkEvent]] = {}
    for row in rows:
        if row.status not in ("pending", "processing", "retry_wait"):
            continue
        if str(row.event_type or "") != "backtest_requested":
            continue
        payload = row.payload if isinstance(row.payload, dict) else {}
        if str(payload.get("source") or "") != "recert_rescue_refresh":
            continue
        try:
            pid = int(payload.get("scan_pattern_id") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0:
            continue
        asset = str(payload.get("asset_class") or "all").strip().lower() or "all"
        recert_groups.setdefault((str(pid), asset), []).append(row)

    def _recert_rank(row: BrainWorkEvent) -> tuple[int, int, float, int]:
        # Keep any already-running item; otherwise keep the freshest low-attempt
        # request so a stale fingerprint does not crowd out newer evidence.
        status_rank = 0 if row.status == "processing" else 1
        updated = row.updated_at or row.created_at or datetime.min
        return (
            status_rank,
            int(row.attempts or 0),
            -updated.timestamp(),
            -int(row.id),
        )

    for group_rows in recert_groups.values():
        if len(group_rows) <= 1:
            continue
        group_rows.sort(key=_recert_rank)
        keep_id = int(group_rows[0].id)
        for row in group_rows[1:]:
            if row.status == "processing":
                continue
            _retire_duplicate(
                row,
                reason="recert_rescue_pattern_asset_superseded",
                keep_id=keep_id,
            )

    # Generic operator boosts are exploration work; recert rescue is a targeted
    # graduation unblocker. If both are open for the same pattern, keep the
    # recert row and retire any non-running generic boost so the backtest queue
    # spends its next slot on the blocker that can actually move live eligibility.
    recert_by_pattern: dict[int, BrainWorkEvent] = {}
    for group_rows in recert_groups.values():
        group_rows.sort(key=_recert_rank)
        keep = group_rows[0]
        payload = keep.payload if isinstance(keep.payload, dict) else {}
        try:
            pid = int(payload.get("scan_pattern_id") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            existing = recert_by_pattern.get(pid)
            if existing is None or _recert_rank(keep) < _recert_rank(existing):
                recert_by_pattern[pid] = keep

    for row in rows:
        if row.status not in ("pending", "retry_wait"):
            continue
        if str(row.event_type or "") != "backtest_requested":
            continue
        payload = row.payload if isinstance(row.payload, dict) else {}
        if str(payload.get("source") or "") != "operator_boost":
            continue
        try:
            pid = int(payload.get("scan_pattern_id") or 0)
        except (TypeError, ValueError):
            pid = 0
        recert_keep = recert_by_pattern.get(pid)
        if recert_keep is None:
            continue
        _retire_duplicate(
            row,
            reason="operator_boost_backtest_superseded_by_recert_rescue",
            keep_id=int(recert_keep.id),
        )

    # Exit-variant refreshes may be born from asset-sliced reliability, cash
    # deployment, or execution-block evidence. The current ScanPattern evolution
    # handler still forks children at the parent-pattern level, so running more
    # than one open exit refresh for the same parent just repeats the same
    # lineage mutation pressure and burns scarce queue slots. Keep one open row
    # per pattern, preferring any in-flight work and otherwise the row with the
    # highest expected evidence value / best calibrated edge.
    exit_variant_groups: dict[int, list[BrainWorkEvent]] = {}
    for row in rows:
        if row.status not in ("pending", "processing", "retry_wait"):
            continue
        if str(row.event_type or "") != "exit_variant_refresh":
            continue
        payload = row.payload if isinstance(row.payload, dict) else {}
        try:
            pid = int(payload.get("scan_pattern_id") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0:
            continue
        exit_variant_groups.setdefault(pid, []).append(row)

    def _exit_variant_edge_value(row: BrainWorkEvent) -> float:
        payload = row.payload if isinstance(row.payload, dict) else {}
        for key in (
            "calibrated_ev_after_cost_pct",
            "calibrated_ev_pct",
            "expected_net_pct",
        ):
            try:
                return float(payload.get(key))
            except (TypeError, ValueError):
                continue
        return 0.0

    def _exit_variant_rank(row: BrainWorkEvent) -> tuple[int, float, float, int, float, int]:
        status_rank = 0 if row.status == "processing" else 1
        updated = row.updated_at or row.created_at or now
        return (
            status_rank,
            -_expected_evidence_value(row),
            -_exit_variant_edge_value(row),
            int(row.attempts or 0),
            -updated.timestamp(),
            -int(row.id),
        )

    for group_rows in exit_variant_groups.values():
        if len(group_rows) <= 1:
            continue
        group_rows.sort(key=_exit_variant_rank)
        keep = group_rows[0]
        keep_id = int(keep.id)
        reason = (
            "exit_variant_pattern_superseded_by_processing"
            if keep.status == "processing"
            else "exit_variant_pattern_superseded"
        )
        for row in group_rows[1:]:
            if row.status == "processing":
                continue
            _retire_duplicate(row, reason=reason, keep_id=keep_id)

    # Snapshot-triggered mining is global-universe work. When snapshot batches
    # arrive faster than a mine can finish, queued batches inside the handler's
    # obsolete-event grace window mostly replay the same expensive discovery
    # pass. Keep only queued batches beyond that coverage horizon.
    queued_mine_rows = [
        row
        for row in rows
        if str(row.event_type or "") == "market_snapshots_batch"
        and row.status in ("pending", "retry_wait")
    ]
    processing_mine_rows = [
        row
        for row in rows
        if str(row.event_type or "") == "market_snapshots_batch"
        and row.status == "processing"
    ]

    def _mine_rank(row: BrainWorkEvent) -> tuple[float, int, int]:
        created = row.created_at or row.updated_at or datetime.min
        return (-created.timestamp(), int(row.attempts or 0), -int(row.id))

    if processing_mine_rows and queued_mine_rows:
        processing_mine_rows.sort(key=_mine_rank)
        newest_processing = processing_mine_rows[0]
        newest_processing_at = newest_processing.created_at or newest_processing.updated_at or datetime.min
        try:
            grace_seconds = max(
                0,
                int(getattr(settings, "brain_mine_handler_obsolete_event_grace_seconds", 900)),
            )
        except (TypeError, ValueError):
            grace_seconds = 900
        processing_coverage_until = newest_processing_at + timedelta(seconds=grace_seconds)
        for row in list(queued_mine_rows):
            row_created = row.created_at or row.updated_at or datetime.min
            if row_created <= processing_coverage_until:
                _retire_duplicate(
                    row,
                    reason="market_snapshot_batch_superseded_by_processing",
                    keep_id=int(newest_processing.id),
                )
                queued_mine_rows.remove(row)

    if len(queued_mine_rows) > 1:
        queued_mine_rows.sort(key=_mine_rank)
        keep_id = int(queued_mine_rows[0].id)
        for row in queued_mine_rows[1:]:
            _retire_duplicate(
                row,
                reason="market_snapshot_batch_superseded",
                keep_id=keep_id,
            )
    db.flush()
    return {"ok": True, "coalesced": len(ids), "ids": ids, "reasons": reasons}


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


def _dead_recovery_cap_reset_delay_seconds(value: int | None = None) -> int:
    return int(
        value
        if value is not None
        else getattr(
            settings,
            "brain_work_dead_letter_recovery_cap_reset_delay_seconds",
            _DEAD_RECOVERY_DEFAULT_CAP_RESET_DELAY_SECONDS,
        )
    )


def _dead_recovery_max_cap_resets(value: int | None = None) -> int:
    return int(
        value
        if value is not None
        else getattr(
            settings,
            "brain_work_dead_letter_recovery_max_cap_resets",
            _DEAD_RECOVERY_DEFAULT_MAX_CAP_RESETS,
        )
    )


def _dead_recovery_cap_reset_updates(
    row: BrainWorkEvent,
    *,
    now: datetime,
    payload: dict[str, Any],
    recovery_count: int,
    delay_seconds: int,
    max_resets: int,
) -> dict[str, Any] | None:
    if max_resets <= 0:
        return None
    reset_count = _payload_int(payload, _DEAD_RECOVERY_CAP_RESET_PAYLOAD_KEY)
    if reset_count >= max_resets:
        return None
    last_dead_at = row.updated_at or row.processed_at or row.created_at
    if last_dead_at is None:
        return None
    if last_dead_at.tzinfo is not None:
        last_dead_at = last_dead_at.replace(tzinfo=None)
    if (now - last_dead_at).total_seconds() < max(0, delay_seconds):
        return None
    total_count = _payload_int(payload, _DEAD_RECOVERY_TOTAL_PAYLOAD_KEY)
    return {
        _DEAD_RECOVERY_CAP_RESET_PAYLOAD_KEY: reset_count + 1,
        _DEAD_RECOVERY_TOTAL_PAYLOAD_KEY: total_count + max(0, recovery_count),
        "transient_dead_recovery_cap_reset_at": now.isoformat(),
        "transient_dead_recovery_prior_count": recovery_count,
    }


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
    max_recoveries = _dead_recovery_max_per_event()
    payload_updates = dict(payload)
    now = datetime.utcnow()
    if recovery_count >= max(0, max_recoveries):
        reset_updates = _dead_recovery_cap_reset_updates(
            row,
            now=now,
            payload=row_payload,
            recovery_count=recovery_count,
            delay_seconds=_dead_recovery_cap_reset_delay_seconds(),
            max_resets=_dead_recovery_max_cap_resets(),
        )
        if reset_updates is None:
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
        recovery_count = 0
        payload_updates.update(reset_updates)
    recovered = _recover_retryable_dead_row(
        row,
        now=now,
        marker=marker,
        recovery_count=recovery_count,
        max_recoveries=max_recoveries,
        delay_seconds=_dead_recovery_delay_seconds(),
        payload_updates=payload_updates,
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
    recovered_after_cap_reset = 0
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
        payload_updates: dict[str, Any] | None = None
        if recovery_count >= max(0, max_recoveries):
            payload_updates = _dead_recovery_cap_reset_updates(
                row,
                now=now,
                payload=payload,
                recovery_count=recovery_count,
                delay_seconds=_dead_recovery_cap_reset_delay_seconds(),
                max_resets=_dead_recovery_max_cap_resets(),
            )
            if payload_updates is None:
                skipped_max_recoveries += 1
                continue
            recovery_count = 0
            recovered_after_cap_reset += 1

        recovered = _recover_retryable_dead_row(
            row,
            now=now,
            marker=marker,
            recovery_count=recovery_count,
            max_recoveries=max_recoveries,
            delay_seconds=delay,
            payload_updates=payload_updates,
        )
        if recovered:
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
        "recovered_after_cap_reset": recovered_after_cap_reset,
        "skipped_duplicate_dedupe": skipped_duplicate_dedupe,
    }


def mark_work_done(db: Session, event_id: int) -> None:
    now = datetime.utcnow()
    db.query(BrainWorkEvent).filter(BrainWorkEvent.id == event_id).update(
        {
            BrainWorkEvent.status: "done",
            BrainWorkEvent.processed_at: now,
            BrainWorkEvent.lease_holder: None,
            BrainWorkEvent.lease_expires_at: None,
            BrainWorkEvent.last_error: None,
            BrainWorkEvent.updated_at: now,
        },
        synchronize_session=False,
    )


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
