"""Project Brain models: autonomous agent state, findings, research, goals, evolution, and PO-specific tables."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from ..db import Base


class ProjectAgentState(Base):
    """Current knowledge and beliefs of a project brain agent."""
    __tablename__ = "project_agent_states"

    id: int = Column(Integer, primary_key=True, index=True)
    agent_name: str = Column(String(50), nullable=False, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    state_json: Optional[str] = Column(Text, nullable=True)
    confidence: float = Column(Float, default=0.0)
    last_cycle_at: Optional[datetime] = Column(DateTime, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class AgentFinding(Base):
    """A discovery, recommendation, or insight produced by an agent."""
    __tablename__ = "agent_findings"

    id: int = Column(Integer, primary_key=True, index=True)
    agent_name: str = Column(String(50), nullable=False, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category: str = Column(String(50), nullable=False)
    title: str = Column(String(300), nullable=False)
    description: str = Column(Text, nullable=False)
    severity: str = Column(String(20), nullable=False, default="info")  # info, warn, critical
    evidence_json: Optional[str] = Column(Text, nullable=True)
    status: str = Column(String(20), nullable=False, default="new")  # new, acknowledged, in_progress, done, dismissed
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class AgentResearch(Base):
    """Web research conducted by an agent."""
    __tablename__ = "agent_research"

    id: int = Column(Integer, primary_key=True, index=True)
    agent_name: str = Column(String(50), nullable=False, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    topic: str = Column(String(300), nullable=False)
    query: str = Column(String(500), nullable=False)
    summary: str = Column(Text, nullable=False)
    sources_json: Optional[str] = Column(Text, nullable=True)
    relevance_score: float = Column(Float, default=0.0)
    searched_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    stale: bool = Column(Boolean, default=False, nullable=False)


class AgentGoal(Base):
    """What an agent is trying to learn or achieve."""
    __tablename__ = "agent_goals"

    id: int = Column(Integer, primary_key=True, index=True)
    agent_name: str = Column(String(50), nullable=False, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    description: str = Column(Text, nullable=False)
    goal_type: str = Column(String(30), nullable=False, default="learn")  # learn, improve, research, deliver
    status: str = Column(String(20), nullable=False, default="active")    # active, completed, cancelled
    progress: float = Column(Float, default=0.0)
    evidence_count: int = Column(Integer, default=0)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at: Optional[datetime] = Column(DateTime, nullable=True)


class AgentEvolution(Base):
    """Log of how an agent's understanding has changed over time."""
    __tablename__ = "agent_evolutions"

    id: int = Column(Integer, primary_key=True, index=True)
    agent_name: str = Column(String(50), nullable=False, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    dimension: str = Column(String(100), nullable=False)
    description: str = Column(Text, nullable=False)
    confidence_before: float = Column(Float, default=0.0)
    confidence_after: float = Column(Float, default=0.0)
    trigger: str = Column(String(200), nullable=False, default="cycle")
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class AgentMessage(Base):
    """Inter-agent communication on the message bus."""
    __tablename__ = "agent_messages"

    id: int = Column(Integer, primary_key=True, index=True)
    from_agent: str = Column(String(50), nullable=False, index=True)
    to_agent: str = Column(String(50), nullable=False, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    message_type: str = Column(String(50), nullable=False)  # finding, requirement, question, alert
    content_json: str = Column(Text, nullable=False)
    acknowledged: bool = Column(Boolean, default=False, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


# ── Product Owner specific ────────────────────────────────────────────

class POQuestion(Base):
    """Questions the Product Owner agent asks the user."""
    __tablename__ = "po_questions"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    question: str = Column(Text, nullable=False)
    context: Optional[str] = Column(Text, nullable=True)
    category: str = Column(String(50), nullable=False, default="general")  # vision, features, priorities, tech_stack, users, success_criteria
    priority: int = Column(Integer, default=5)
    status: str = Column(String(20), nullable=False, default="pending")  # pending, answered, skipped
    options: Optional[str] = Column(Text, nullable=True)
    answer: Optional[str] = Column(Text, nullable=True)
    asked_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    answered_at: Optional[datetime] = Column(DateTime, nullable=True)


class PORequirement(Base):
    """Structured requirements/user stories gathered by the PO agent."""
    __tablename__ = "po_requirements"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: str = Column(String(300), nullable=False)
    description: str = Column(Text, nullable=False)
    priority: str = Column(String(20), nullable=False, default="medium")  # critical, high, medium, low
    status: str = Column(String(20), nullable=False, default="draft")     # draft, refined, ready, in_planner, done
    acceptance_criteria: Optional[str] = Column(Text, nullable=True)
    source_questions_json: Optional[str] = Column(Text, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


# ── QA Engineer specific ─────────────────────────────────────────────

class QATestCase(Base):
    """Test scenario generated by the QA agent."""
    __tablename__ = "qa_test_cases"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: str = Column(String(300), nullable=False)
    steps_json: Optional[str] = Column(Text, nullable=True)
    expected_json: Optional[str] = Column(Text, nullable=True)
    priority: str = Column(String(20), nullable=False, default="medium")
    status: str = Column(String(20), nullable=False, default="active")  # active, disabled, archived
    last_run_at: Optional[datetime] = Column(DateTime, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class QATestRun(Base):
    """Execution record for a QA test."""
    __tablename__ = "qa_test_runs"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    test_name: str = Column(String(300), nullable=False)
    passed: bool = Column(Boolean, default=False, nullable=False)
    errors_json: Optional[str] = Column(Text, nullable=True)
    duration_ms: int = Column(Integer, default=0)
    screenshot_path: Optional[str] = Column(String(500), nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class QABugReport(Base):
    """Bug detected by the QA agent."""
    __tablename__ = "qa_bug_reports"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: str = Column(String(300), nullable=False)
    description: str = Column(Text, nullable=False, default="")
    severity: str = Column(String(20), nullable=False, default="warn")  # info, warn, critical
    screenshot_path: Optional[str] = Column(String(500), nullable=True)
    reproduction_steps: Optional[str] = Column(Text, nullable=True)
    status: str = Column(String(20), nullable=False, default="open")  # open, confirmed, fixed, wontfix
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
