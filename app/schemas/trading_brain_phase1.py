"""Trading Brain Phase 1 schemas: learning cycles, stage jobs, predictions."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class BrainLearningCycleRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    correlation_id: str
    universe_id: Optional[str] = None
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    meta_json: dict[str, Any] = {}
    created_at: datetime


class BrainStageJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cycle_run_id: int
    stage_key: str
    ordinal: int
    status: str
    attempt: int = 0
    worker_id: Optional[str] = None
    error_detail: Optional[str] = None
    skip_reason: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class BrainPredictionSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    as_of_ts: datetime
    universe_fingerprint: str
    ticker_count: int
    source_tag: str = "legacy_get_current_predictions"
    correlation_id: str


class BrainPredictionLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    snapshot_id: int
    sort_rank: int
    ticker: str
    score: float
    confidence: Optional[int] = None
    direction: Optional[str] = None
    price: Optional[float] = None
    meta_ml_probability: Optional[float] = None
    vix_regime: Optional[str] = None
    suggested_stop: Optional[float] = None
    suggested_target: Optional[float] = None
    risk_reward: Optional[float] = None
    position_size_pct: Optional[float] = None


class BrainIntegrationEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    idempotency_key: str
    event_id: str
    event_type: str
    status: str
    received_at: datetime
    processed_at: Optional[datetime] = None
