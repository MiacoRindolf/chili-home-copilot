"""Phase 1 brain orchestration tables (`brain_*` prefix). See canonical blueprint Part L."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from ..db import Base


class BrainLearningCycleRun(Base):
    __tablename__ = "brain_learning_cycle_run"

    id: int = Column(Integer, primary_key=True, index=True)
    correlation_id: str = Column(String(64), nullable=False, index=True)
    universe_id: Optional[str] = Column(String(64), nullable=True, index=True)
    status: str = Column(String(24), nullable=False)
    started_at: Optional[datetime] = Column(DateTime, nullable=True)
    finished_at: Optional[datetime] = Column(DateTime, nullable=True)
    meta_json: dict[str, Any] = Column(JSONB, nullable=False, default=lambda: {})
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)

    stage_jobs = relationship(
        "BrainStageJob",
        back_populates="cycle_run",
        cascade="all, delete-orphan",
    )


class BrainStageJob(Base):
    __tablename__ = "brain_stage_job"

    id: int = Column(Integer, primary_key=True, index=True)
    cycle_run_id: int = Column(
        Integer,
        ForeignKey("brain_learning_cycle_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage_key: str = Column(String(64), nullable=False)
    ordinal: int = Column(Integer, nullable=False)
    status: str = Column(String(24), nullable=False)
    attempt: int = Column(Integer, nullable=False, default=0)
    lease_until: Optional[datetime] = Column(DateTime, nullable=True)
    worker_id: Optional[str] = Column(String(128), nullable=True)
    input_artifact_refs: list[Any] = Column(JSONB, nullable=False, default=lambda: [])
    output_artifact_refs: list[Any] = Column(JSONB, nullable=False, default=lambda: [])
    error_detail: Optional[str] = Column(Text, nullable=True)
    skip_reason: Optional[str] = Column(String(255), nullable=True)
    started_at: Optional[datetime] = Column(DateTime, nullable=True)
    finished_at: Optional[datetime] = Column(DateTime, nullable=True)

    cycle_run = relationship("BrainLearningCycleRun", back_populates="stage_jobs")


class BrainCycleLease(Base):
    __tablename__ = "brain_cycle_lease"

    scope_key: str = Column(String(64), primary_key=True)
    cycle_run_id: Optional[int] = Column(
        Integer,
        ForeignKey("brain_learning_cycle_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    holder_id: str = Column(String(128), nullable=False)
    acquired_at: Optional[datetime] = Column(DateTime, nullable=True)
    expires_at: Optional[datetime] = Column(DateTime, nullable=True)


class BrainPredictionSnapshot(Base):
    """Phase 4: append-only mirror header for legacy `get_current_predictions` (not read-authoritative)."""

    __tablename__ = "brain_prediction_snapshot"

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    as_of_ts: datetime = Column(DateTime, nullable=False)
    universe_fingerprint: str = Column(String(64), nullable=False, index=True)
    ticker_count: int = Column(Integer, nullable=False)
    source_tag: str = Column(String(64), nullable=False, default="legacy_get_current_predictions")
    correlation_id: str = Column(String(40), nullable=False)

    lines = relationship(
        "BrainPredictionLine",
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )


class BrainPredictionLine(Base):
    """One ticker line under a prediction mirror snapshot."""

    __tablename__ = "brain_prediction_line"

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: int = Column(
        BigInteger,
        ForeignKey("brain_prediction_snapshot.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sort_rank: int = Column(Integer, nullable=False)
    ticker: str = Column(String(32), nullable=False)
    score: float = Column(Float, nullable=False)
    confidence: Optional[int] = Column(Integer, nullable=True)
    direction: Optional[str] = Column(String(32), nullable=True)
    price: Optional[float] = Column(Float, nullable=True)
    meta_ml_probability: Optional[float] = Column(Float, nullable=True)
    vix_regime: Optional[str] = Column(String(32), nullable=True)
    signals_json: list[Any] = Column(JSONB, nullable=False, default=lambda: [])
    matched_patterns_json: list[Any] = Column(JSONB, nullable=False, default=lambda: [])
    suggested_stop: Optional[float] = Column(Float, nullable=True)
    suggested_target: Optional[float] = Column(Float, nullable=True)
    risk_reward: Optional[float] = Column(Float, nullable=True)
    position_size_pct: Optional[float] = Column(Float, nullable=True)

    snapshot = relationship("BrainPredictionSnapshot", back_populates="lines")


class BrainIntegrationEvent(Base):
    __tablename__ = "brain_integration_event"

    idempotency_key: str = Column(String(256), primary_key=True)
    event_id: str = Column(String(64), nullable=False)
    event_type: str = Column(String(64), nullable=False)
    payload_hash: str = Column(String(128), nullable=False)
    payload_json: dict[str, Any] = Column(JSONB, nullable=False)
    received_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at: Optional[datetime] = Column(DateTime, nullable=True)
    status: str = Column(String(24), nullable=False)
