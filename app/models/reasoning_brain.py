"""Reasoning Brain models: long-lived user reasoning state.

These tables store the synthesized user model, weighted interests,
background web research, anticipations, and learning events.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text

from ..db import Base


class ReasoningUserModel(Base):
    __tablename__ = "reasoning_user_models"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: int = Column(Integer, index=True, nullable=False)
    decision_style: str = Column(String, nullable=True)  # e.g. conservative|exploratory
    risk_tolerance: str = Column(String, nullable=True)  # e.g. low|medium|high
    communication_prefs: Optional[str] = Column(Text, nullable=True)  # JSON blob
    active_goals: Optional[str] = Column(Text, nullable=True)  # JSON blob
    knowledge_gaps: Optional[str] = Column(Text, nullable=True)  # JSON blob
    source_memory_count: int = Column(Integer, default=0)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    active: bool = Column(Boolean, default=True, nullable=False)


class ReasoningInterest(Base):
    __tablename__ = "reasoning_interests"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: int = Column(Integer, index=True, nullable=False)
    topic: str = Column(String, nullable=False, index=True)
    category: str = Column(String, nullable=False)  # explicit|inferred_trading|inferred_code|inferred_chat
    weight: float = Column(Float, default=0.0, nullable=False)
    related_topics: Optional[str] = Column(Text, nullable=True)  # JSON list
    source: Optional[str] = Column(String, nullable=True)  # optional freeform source label
    last_seen: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    active: bool = Column(Boolean, default=True, nullable=False)


class ReasoningResearch(Base):
    __tablename__ = "reasoning_research"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: int = Column(Integer, index=True, nullable=False)
    topic: str = Column(String, nullable=False, index=True)
    summary: str = Column(Text, nullable=False)
    sources: Optional[str] = Column(Text, nullable=True)  # JSON list of {title, url}
    relevance_score: float = Column(Float, default=0.0, nullable=False)
    searched_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    stale: bool = Column(Boolean, default=False, nullable=False)


class ReasoningAnticipation(Base):
    __tablename__ = "reasoning_anticipations"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: int = Column(Integer, index=True, nullable=False)
    description: str = Column(Text, nullable=False)
    domain: Optional[str] = Column(String, nullable=True)  # trading|code|general|...
    context: Optional[str] = Column(Text, nullable=True)  # JSON blob
    confidence: float = Column(Float, default=0.5, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    acted_on: bool = Column(Boolean, default=False, nullable=False)
    dismissed: bool = Column(Boolean, default=False, nullable=False)


class ReasoningEvent(Base):
    __tablename__ = "reasoning_events"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, index=True, nullable=True)
    event_type: str = Column(String, nullable=False)  # cycle|web_research|model_update|anticipation|error
    description: str = Column(Text, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class ReasoningLearningGoal(Base):
    __tablename__ = "reasoning_learning_goals"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: int = Column(Integer, index=True, nullable=False)
    dimension: str = Column(String, nullable=False)  # e.g. risk_tolerance, work_schedule
    description: str = Column(Text, nullable=False)
    status: str = Column(String, nullable=False, default="pending")  # pending|active|completed|stale
    confidence_before: float = Column(Float, nullable=True)
    confidence_after: float = Column(Float, nullable=True)
    evidence_count: int = Column(Integer, default=0, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at: datetime | None = Column(DateTime, nullable=True)


class ReasoningHypothesis(Base):
    __tablename__ = "reasoning_hypotheses"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: int = Column(Integer, index=True, nullable=False)
    claim: str = Column(Text, nullable=False)
    domain: str = Column(String, nullable=True)  # trading|code|general|life|other
    confidence: float = Column(Float, default=0.5, nullable=False)
    evidence_for: int = Column(Integer, default=0, nullable=False)
    evidence_against: int = Column(Integer, default=0, nullable=False)
    tested_at: datetime | None = Column(DateTime, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    active: bool = Column(Boolean, default=True, nullable=False)


class ReasoningConfidenceSnapshot(Base):
    __tablename__ = "reasoning_confidence_snapshots"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: int = Column(Integer, index=True, nullable=False)
    dimension: str = Column(String, nullable=False)
    confidence_value: float = Column(Float, default=0.0, nullable=False)
    snapshot_date: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)

