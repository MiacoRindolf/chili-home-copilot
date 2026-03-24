from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CycleRunStatus(StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    stale_failed = "stale_failed"


class StageJobStatus(StrEnum):
    queued = "queued"
    leased = "leased"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    dead = "dead"


class StageDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_key: str
    ordinal: int
    optional: bool = False


class BrainLearningCycleRunDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    correlation_id: str
    universe_id: str | None
    status: CycleRunStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    meta_json: dict[str, Any] = Field(default_factory=dict)


class BrainStageJobDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    cycle_run_id: int
    stage_key: str
    ordinal: int
    status: StageJobStatus
    attempt: int = 0
    lease_until: datetime | None = None
    worker_id: str | None = None
    input_artifact_refs: list[str] = Field(default_factory=list)
    output_artifact_refs: list[str] = Field(default_factory=list)
    error_detail: str | None = None
    skip_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class BrainCycleLeaseDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_key: str
    cycle_run_id: int | None = None
    holder_id: str
    acquired_at: datetime | None = None
    expires_at: datetime | None = None
