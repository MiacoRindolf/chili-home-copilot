"""Pydantic DTOs for Phase 4 prediction mirror (dual-write only; not authoritative for reads)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PredictionLineWriteDTO(BaseModel):
    """One persisted line mirroring a legacy `get_current_predictions` dict entry."""

    sort_rank: int = Field(ge=0)
    ticker: str
    score: float
    confidence: int | None = None
    direction: str | None = None
    price: float | None = None
    meta_ml_probability: float | None = None
    vix_regime: str | None = None
    signals: list[str] = Field(default_factory=list)
    matched_patterns: list[dict] = Field(default_factory=list)
    suggested_stop: float | None = None
    suggested_target: float | None = None
    risk_reward: float | None = None
    position_size_pct: float | None = None


class PredictionSnapshotSealDTO(BaseModel):
    """Header fields before insert; `as_of_ts` and `correlation_id` set at seal time."""

    universe_fingerprint: str
    ticker_count: int
    source_tag: str = "legacy_get_current_predictions"
    as_of_ts: datetime | None = None
    correlation_id: str | None = None
