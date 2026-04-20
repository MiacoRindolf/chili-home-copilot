"""Durable project-domain runs and analysis snapshots."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from ..db import Base


class ProjectDomainRun(Base):
    """Durable operator-facing run record for project-domain actions."""

    __tablename__ = "project_domain_runs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    repo_id = Column(Integer, ForeignKey("code_repos.id", ondelete="SET NULL"), nullable=True, index=True)
    run_kind = Column(String(32), nullable=False, index=True)
    status = Column(String(24), nullable=False, default="running", index=True)
    trigger_source = Column(String(32), nullable=False, default="manual")
    title = Column(String(200), nullable=True)
    detail_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProjectAnalysisSnapshot(Base):
    """Stored multi-perspective advisory report for the Project domain."""

    __tablename__ = "project_analysis_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    repo_id = Column(Integer, ForeignKey("code_repos.id", ondelete="SET NULL"), nullable=True, index=True)
    source_run_id = Column(Integer, ForeignKey("project_domain_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    status = Column(String(24), nullable=False, default="completed", index=True)
    summary_json = Column(Text, nullable=False, default="{}")
    perspectives_json = Column(Text, nullable=False, default="{}")
    timeline_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
