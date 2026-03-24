from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from sqlalchemy.orm import Session

from ..schemas.cycle import (
    BrainLearningCycleRunDTO,
    BrainStageJobDTO,
    CycleRunStatus,
    StageDefinition,
    StageJobStatus,
)


class BrainLearningCycleRunRepository(Protocol):
    def create(
        self,
        db: Session,
        *,
        correlation_id: str,
        universe_id: str | None,
        meta_json: dict | None,
    ) -> int: ...

    def get(self, db: Session, run_id: int) -> BrainLearningCycleRunDTO | None: ...

    def update_status(
        self,
        db: Session,
        run_id: int,
        *,
        status: CycleRunStatus,
        finished_at: datetime | None = None,
    ) -> None: ...

    def list_recent(self, db: Session, *, limit: int = 20) -> list[BrainLearningCycleRunDTO]: ...


class BrainStageJobRepository(Protocol):
    def create_jobs_for_cycle(
        self,
        db: Session,
        *,
        cycle_run_id: int,
        stages: Sequence[StageDefinition],
    ) -> None: ...

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
    ) -> None: ...

    def get_jobs_for_cycle(self, db: Session, cycle_run_id: int) -> list[BrainStageJobDTO]: ...

    def claim_next_runnable(
        self,
        db: Session,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> BrainStageJobDTO | None: ...
