"""Insert/update rows in brain_batch_jobs for scheduled batch work."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import BrainBatchJob

logger = logging.getLogger(__name__)


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
) -> None:
    row = db.query(BrainBatchJob).filter(BrainBatchJob.id == job_id).first()
    if not row:
        logger.warning("[brain_batch_job] missing row id=%s", job_id)
        return
    row.ended_at = datetime.utcnow()
    row.status = "ok" if ok else "error"
    row.error_message = (error[:2000] if error else None)
    row.meta_json = meta
