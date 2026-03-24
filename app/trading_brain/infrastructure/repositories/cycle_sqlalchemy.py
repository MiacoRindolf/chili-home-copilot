"""SQLAlchemy implementations of cycle run + stage job repositories (Phase 2)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy.orm import Session

from ....models.trading_brain_phase1 import BrainLearningCycleRun, BrainStageJob
from ...schemas.cycle import (
    BrainLearningCycleRunDTO,
    BrainStageJobDTO,
    CycleRunStatus,
    StageDefinition,
    StageJobStatus,
)


def _run_to_dto(row: BrainLearningCycleRun) -> BrainLearningCycleRunDTO:
    return BrainLearningCycleRunDTO(
        id=row.id,
        correlation_id=row.correlation_id,
        universe_id=row.universe_id,
        status=CycleRunStatus(row.status),
        started_at=row.started_at,
        finished_at=row.finished_at,
        meta_json=dict(row.meta_json or {}),
    )


def _refs_to_str_list(raw: object) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for x in raw:  # type: ignore[union-attr]
        out.append(x if isinstance(x, str) else str(x))
    return out


def _job_to_dto(row: BrainStageJob) -> BrainStageJobDTO:
    return BrainStageJobDTO(
        id=row.id,
        cycle_run_id=row.cycle_run_id,
        stage_key=row.stage_key,
        ordinal=row.ordinal,
        status=StageJobStatus(row.status),
        attempt=int(row.attempt or 0),
        lease_until=row.lease_until,
        worker_id=row.worker_id,
        input_artifact_refs=_refs_to_str_list(row.input_artifact_refs),
        output_artifact_refs=_refs_to_str_list(row.output_artifact_refs),
        error_detail=row.error_detail,
        skip_reason=row.skip_reason,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class SqlAlchemyBrainLearningCycleRunRepository:
    def create(
        self,
        db: Session,
        *,
        correlation_id: str,
        universe_id: str | None,
        meta_json: dict | None,
    ) -> int:
        row = BrainLearningCycleRun(
            correlation_id=correlation_id,
            universe_id=universe_id,
            status=CycleRunStatus.running.value,
            started_at=datetime.utcnow(),
            finished_at=None,
            meta_json=dict(meta_json or {}),
        )
        db.add(row)
        db.flush()
        return int(row.id)

    def get(self, db: Session, run_id: int) -> BrainLearningCycleRunDTO | None:
        row = (
            db.query(BrainLearningCycleRun)
            .filter(BrainLearningCycleRun.id == int(run_id))
            .first()
        )
        return _run_to_dto(row) if row else None

    def update_status(
        self,
        db: Session,
        run_id: int,
        *,
        status: CycleRunStatus,
        finished_at: datetime | None = None,
    ) -> None:
        row = (
            db.query(BrainLearningCycleRun)
            .filter(BrainLearningCycleRun.id == int(run_id))
            .first()
        )
        if not row:
            return
        row.status = status.value
        if finished_at is not None:
            row.finished_at = finished_at

    def list_recent(self, db: Session, *, limit: int = 20) -> list[BrainLearningCycleRunDTO]:
        rows = (
            db.query(BrainLearningCycleRun)
            .order_by(BrainLearningCycleRun.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )
        return [_run_to_dto(r) for r in rows]


class SqlAlchemyBrainStageJobRepository:
    def create_jobs_for_cycle(
        self,
        db: Session,
        *,
        cycle_run_id: int,
        stages: Sequence[StageDefinition],
    ) -> None:
        for st in stages:
            db.add(
                BrainStageJob(
                    cycle_run_id=int(cycle_run_id),
                    stage_key=st.stage_key,
                    ordinal=int(st.ordinal),
                    status=StageJobStatus.queued.value,
                    attempt=0,
                )
            )
        db.flush()

    def update_job(
        self,
        db: Session,
        job_id: int,
        *,
        status: StageJobStatus,
        lease_until: datetime | None = None,
        error_detail: str | None = None,
        output_artifact_refs: list[str] | None = None,
        skip_reason: str | None = None,
    ) -> None:
        row = db.query(BrainStageJob).filter(BrainStageJob.id == int(job_id)).first()
        if not row:
            return
        now = datetime.utcnow()
        row.status = status.value
        if lease_until is not None:
            row.lease_until = lease_until
        if error_detail is not None:
            row.error_detail = error_detail
        if output_artifact_refs is not None:
            row.output_artifact_refs = list(output_artifact_refs)
        if skip_reason is not None:
            row.skip_reason = skip_reason
        if status == StageJobStatus.running:
            if row.started_at is None:
                row.started_at = now
        if status in (
            StageJobStatus.succeeded,
            StageJobStatus.failed,
            StageJobStatus.skipped,
            StageJobStatus.dead,
        ):
            row.finished_at = now
            if row.started_at is None:
                row.started_at = now
        if status == StageJobStatus.leased:
            if row.started_at is None:
                row.started_at = now

    def get_jobs_for_cycle(self, db: Session, cycle_run_id: int) -> list[BrainStageJobDTO]:
        rows = (
            db.query(BrainStageJob)
            .filter(BrainStageJob.cycle_run_id == int(cycle_run_id))
            .order_by(BrainStageJob.ordinal.asc())
            .all()
        )
        return [_job_to_dto(r) for r in rows]

    def claim_next_runnable(
        self,
        db: Session,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> BrainStageJobDTO | None:
        now = datetime.utcnow()
        job = (
            db.query(BrainStageJob)
            .filter(BrainStageJob.status == StageJobStatus.queued.value)
            .order_by(BrainStageJob.cycle_run_id.asc(), BrainStageJob.ordinal.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if not job:
            return None
        job.status = StageJobStatus.leased.value
        job.worker_id = worker_id
        job.lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        job.attempt = int(job.attempt or 0) + 1
        if job.started_at is None:
            job.started_at = now
        db.flush()
        return _job_to_dto(job)
