"""Planner task-centric coding workflow: PO v2 clarifications, briefs, validation runs (Phase 1)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship

from ..db import Base


class PlanTaskCodingProfile(Base):
    __tablename__ = "plan_task_coding_profile"

    task_id = Column(Integer, ForeignKey("plan_tasks.id", ondelete="CASCADE"), primary_key=True)
    repo_index = Column(Integer, nullable=False, default=0)
    code_repo_id = Column(Integer, ForeignKey("code_repos.id", ondelete="SET NULL"), nullable=True, index=True)
    sub_path = Column(Text, nullable=False, default="")
    brief_approved_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    task = relationship("PlanTask", back_populates="coding_profile")
    code_repo = relationship("CodeRepo")


class TaskClarification(Base):
    __tablename__ = "task_clarification"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="open")
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    task = relationship("PlanTask", back_populates="clarifications")


class CodingTaskBrief(Base):
    __tablename__ = "coding_task_brief"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    body = Column(Text, nullable=False, default="")
    version = Column(Integer, nullable=False, default=1)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    task = relationship("PlanTask", back_populates="coding_briefs")


class CodingTaskValidationRun(Base):
    __tablename__ = "coding_task_validation_run"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    trigger_source = Column(String(24), nullable=False, default="manual")
    status = Column(String(24), nullable=False, default="pending")
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    exit_code = Column(Integer, nullable=True)
    timed_out = Column(Boolean, nullable=False, default=False)
    error_message = Column(Text, nullable=True)

    task = relationship("PlanTask", back_populates="validation_runs")
    artifacts = relationship(
        "CodingValidationArtifact",
        back_populates="run",
        cascade="all, delete-orphan",
    )


class CodingValidationArtifact(Base):
    __tablename__ = "coding_validation_artifact"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(
        Integer,
        ForeignKey("coding_task_validation_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_key = Column(String(64), nullable=False)
    kind = Column(String(32), nullable=False)
    content = Column(Text, nullable=True)
    byte_length = Column(Integer, nullable=False, default=0)

    run = relationship("CodingTaskValidationRun", back_populates="artifacts")


class CodingAgentSuggestion(Base):
    """Phase 16: append-only snapshot of a successful Phase 15 agent-suggest result (bounded, user-triggered save)."""

    __tablename__ = "coding_agent_suggestion"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    model = Column(String(200), nullable=False, default="")
    response_text = Column(Text, nullable=False, default="")
    diffs_json = Column(Text, nullable=False, default="[]")
    files_changed_json = Column(Text, nullable=False, default="[]")
    validation_json = Column(Text, nullable=False, default="[]")
    context_used_json = Column(Text, nullable=False, default="{}")
    truncation_flags_json = Column(Text, nullable=True)

    task = relationship("PlanTask", back_populates="agent_suggestions")
    apply_attempts = relationship(
        "CodingAgentSuggestionApply",
        back_populates="suggestion",
        cascade="all, delete-orphan",
    )


class CodingAgentSuggestionApply(Base):
    """Phase 17: append-only audit of snapshot apply attempts (all-or-nothing git apply at repo root)."""

    __tablename__ = "coding_agent_suggestion_apply"

    id = Column(Integer, primary_key=True, index=True)
    suggestion_id = Column(
        Integer,
        ForeignKey("coding_agent_suggestion.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    task_id = Column(Integer, ForeignKey("plan_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    dry_run = Column(Boolean, nullable=False, default=False)
    status = Column(String(24), nullable=False)
    message = Column(Text, nullable=False, default="")

    suggestion = relationship("CodingAgentSuggestion", back_populates="apply_attempts")


class CodingBlockerReport(Base):
    __tablename__ = "coding_blocker_report"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(
        Integer,
        ForeignKey("coding_task_validation_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    category = Column(String(64), nullable=False, default="validation")
    severity = Column(String(24), nullable=False, default="error")
    summary = Column(Text, nullable=False)
    detail_json = Column(Text, nullable=True)

    task = relationship("PlanTask", back_populates="blocker_reports")
