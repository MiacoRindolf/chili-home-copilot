"""Reasoning Brain domain schemas: user models, interests, research, anticipations."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ReasoningUserModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    decision_style: Optional[str] = None
    risk_tolerance: Optional[str] = None
    communication_prefs: Optional[str] = None
    active_goals: Optional[str] = None
    knowledge_gaps: Optional[str] = None
    source_memory_count: int = 0
    created_at: datetime
    active: bool


class ReasoningInterestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    topic: str
    category: Optional[str] = None
    weight: float = 0.0
    related_topics: Optional[str] = None
    source: Optional[str] = None
    last_seen: Optional[datetime] = None
    created_at: datetime
    active: bool


class ReasoningResearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    topic: str
    summary: Optional[str] = None
    sources: Optional[str] = None
    relevance_score: float = 0.0
    searched_at: Optional[datetime] = None
    stale: bool = False


class ReasoningAnticipationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    description: str
    domain: Optional[str] = None
    context: Optional[str] = None
    confidence: float = 0.5
    created_at: datetime
    acted_on: bool = False
    dismissed: bool = False


class ReasoningHypothesisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    claim: str
    domain: Optional[str] = None
    confidence: float = 0.5
    evidence_for: int = 0
    evidence_against: int = 0
    tested_at: Optional[datetime] = None
    created_at: datetime
    active: bool


class ReasoningLearningGoalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    dimension: str
    description: str
    status: str = "pending"
    confidence_before: Optional[float] = None
    confidence_after: Optional[float] = None
    evidence_count: int = 0
    created_at: datetime
    completed_at: Optional[datetime] = None
