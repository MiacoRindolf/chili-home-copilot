"""Insert/update rows in brain_batch_jobs for scheduled batch work."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import BrainBatchJob

logger = logging.getLogger(__name__)


def brain_batch_job_record_completed(
    db: Session,
    job_type: str,
    *,
    ok: bool,
    user_id: int | None = None,
    meta: dict[str, Any] | None = None,
    payload_json: dict[str, Any] | None = None,
    error: str | None = None,
) -> str:
    """Single transaction: create a batch row and mark finished (API-triggered scans, heartbeats)."""
    jid = brain_batch_job_begin(db, job_type, user_id)
    db.flush()
    brain_batch_job_finish(
        db, jid, ok=ok, error=error, meta=meta, payload_json=payload_json,
    )
    return jid


def brain_batch_job_begin(db: Session, job_type: str, user_id: int | None = None) -> str:
    job_id = str(uuid.uuid4())
    row = BrainBatchJob(
        id=job_id,
        job_type=job_type,
        status="running",
        started_at=datetime.utcnow(),
        user_id=user_id,
    )
    db.add(row)
    db.flush()
    return job_id


def brain_batch_job_finish(
    db: Session,
    job_id: str,
    *,
    ok: bool,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
    payload_json: dict[str, Any] | None = None,
) -> None:
    row = db.query(BrainBatchJob).filter(BrainBatchJob.id == job_id).first()
    if not row:
        logger.warning("[brain_batch_job] missing row id=%s", job_id)
        return
    row.ended_at = datetime.utcnow()
    row.status = "ok" if ok else "error"
    row.error_message = (error[:2000] if error else None)
    row.meta_json = meta
    if payload_json is not None:
        row.payload_json = payload_json


def fetch_latest_ok_payload(
    db: Session,
    job_type: str,
) -> tuple[dict[str, Any] | None, datetime | None, dict[str, Any] | None]:
    """Return (payload_json, ended_at, meta_json) for latest successful job of this type."""
    row = (
        db.query(BrainBatchJob)
        .filter(
            BrainBatchJob.job_type == job_type,
            BrainBatchJob.status == "ok",
            BrainBatchJob.payload_json.isnot(None),
        )
        .order_by(BrainBatchJob.ended_at.desc())
        .first()
    )
    if not row:
        return None, None, None
    return row.payload_json, row.ended_at, row.meta_json


def fetch_batch_jobs_page(
    db: Session,
    *,
    limit: int = 100,
    offset: int = 0,
    job_type: str | None = None,
    status: str | None = None,
) -> tuple[list[BrainBatchJob], int]:
    q = db.query(BrainBatchJob)
    if job_type:
        q = q.filter(BrainBatchJob.job_type == job_type)
    if status:
        q = q.filter(BrainBatchJob.status == status)
    total = q.count()
    rows = (
        q.order_by(BrainBatchJob.started_at.desc())
        .offset(offset)
        .limit(min(limit, 500))
        .all()
    )
    return rows, total


def batch_job_summary(db: Session, *, hours: int = 168) -> list[dict[str, Any]]:
    """Per job_type aggregates for metrics UI (default 7 days)."""
    from datetime import timedelta
    from sqlalchemy import case, func

    since = datetime.utcnow() - timedelta(hours=hours)
    rows = (
        db.query(
            BrainBatchJob.job_type,
            func.count().label("n"),
            func.sum(case((BrainBatchJob.status == "ok", 1), else_=0)).label("ok_n"),
            func.max(BrainBatchJob.started_at).label("last_start"),
        )
        .filter(BrainBatchJob.started_at >= since)
        .group_by(BrainBatchJob.job_type)
        .order_by(func.count().desc())
        .all()
    )
    out = []
    for r in rows:
        out.append(
            {
                "job_type": r.job_type,
                "runs": int(r.n or 0),
                "ok_runs": int(r.ok_n or 0),
                "last_started_at": r.last_start.isoformat() if r.last_start else None,
            }
        )
    return out
