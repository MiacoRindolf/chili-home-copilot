"""Project Brain domain schemas: agents, findings, research, goals, PO, QA."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ProjectAgentStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_name: str
    user_id: Optional[int] = None
    state_json: Optional[str] = None
    confidence: float = 0.0
    last_cycle_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class AgentFindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_name: str
    user_id: Optional[int] = None
    category: str
    title: str
    description: str
    severity: str = "info"
    status: str = "new"
    created_at: datetime
    updated_at: datetime


class AgentResearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_name: str
    user_id: Optional[int] = None
    topic: str
    query: str
    summary: Optional[str] = None
    relevance_score: float = 0.0
    searched_at: Optional[datetime] = None
    stale: bool = False


class AgentGoalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_name: str
    user_id: Optional[int] = None
    description: str
    goal_type: str = "learn"
    status: str = "active"
    progress: float = 0.0
    evidence_count: int = 0
    created_at: datetime
    completed_at: Optional[datetime] = None


class POQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    question: str
    context: Optional[str] = None
    category: str = "general"
    priority: int = 5
    status: str = "pending"
    options: Optional[str] = None
    answer: Optional[str] = None
    asked_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None


class PORequirementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    priority: str = "medium"
    status: str = "draft"
    acceptance_criteria: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class QATestCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    name: str
    priority: str = "medium"
    status: str = "active"
    last_run_at: Optional[datetime] = None
    created_at: datetime


class QATestRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    test_name: str
    passed: bool = False
    duration_ms: int = 0
    created_at: datetime


class QABugReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    title: str
    description: str = ""
    severity: str = "warn"
    status: str = "open"
    created_at: datetime
    updated_at: datetime
