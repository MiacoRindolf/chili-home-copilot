"""Durable project-domain runs, analysis snapshots, and autonomous runs."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

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


class ProjectAutonomyRun(Base):
    """A durable Project Brain Local Autopilot run."""

    __tablename__ = "project_autonomy_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), nullable=False, unique=True, index=True)
    project_run_id = Column(
        Integer,
        ForeignKey("project_domain_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    repo_id = Column(Integer, ForeignKey("code_repos.id", ondelete="SET NULL"), nullable=True, index=True)
    agent_profile_id = Column(
        Integer,
        ForeignKey("project_autonomy_agent_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    parent_run_id = Column(String(64), nullable=True, index=True)
    agent_snapshot_json = Column(Text, nullable=False, default="{}")
    prompt = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="queued", index=True)
    current_stage = Column(String(40), nullable=False, default="queued", index=True)
    autonomy_level = Column(String(40), nullable=False, default="full_local")
    execution_mode = Column(String(40), nullable=False, default="plan_approval")
    plan_status = Column(String(40), nullable=False, default="drafting", index=True)
    chat_title = Column(String(200), nullable=True)
    model_policy = Column(String(40), nullable=False, default="local_first")
    target_branch = Column(String(200), nullable=True)
    base_branch = Column(String(200), nullable=True)
    base_sha = Column(String(64), nullable=True)
    integration_branch = Column(String(200), nullable=True)
    worktree_path = Column(Text, nullable=True)
    merge_status = Column(String(40), nullable=False, default="pending")
    merge_message = Column(Text, nullable=True)
    plan_json = Column(Text, nullable=False, default="{}")
    agents_json = Column(Text, nullable=False, default="[]")
    files_json = Column(Text, nullable=False, default="[]")
    commands_json = Column(Text, nullable=False, default="[]")
    validation_json = Column(Text, nullable=False, default="[]")
    learning_json = Column(Text, nullable=False, default="{}")
    error_message = Column(Text, nullable=True)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    archived_at = Column(DateTime, nullable=True, index=True)
    archive_reason = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ProjectAutonomyAgentProfile(Base):
    """Repo-scoped durable agent identity and default operating policy."""

    __tablename__ = "project_autonomy_agent_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    repo_id = Column(Integer, ForeignKey("code_repos.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_key = Column(String(80), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    role = Column(String(80), nullable=False, index=True)
    tier = Column(String(24), nullable=False, default="micro", index=True)
    status = Column(String(24), nullable=False, default="paused", index=True)
    model_policy = Column(String(40), nullable=False, default="local_first")
    prompt_setting_json = Column(Text, nullable=False, default="{}")
    permissions_json = Column(Text, nullable=False, default="{}")
    schedule_enabled = Column(Boolean, nullable=False, default=False)
    schedule_json = Column(Text, nullable=False, default="{}")
    parent_profile_id = Column(
        Integer,
        ForeignKey("project_autonomy_agent_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    generated = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ProjectAutonomyAgentSchedule(Base):
    """Bounded recurring work configuration for one agent profile."""

    __tablename__ = "project_autonomy_agent_schedules"

    id = Column(Integer, primary_key=True, index=True)
    profile_id = Column(
        Integer,
        ForeignKey("project_autonomy_agent_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(String(24), nullable=False, default="paused", index=True)
    rrule = Column(String(240), nullable=True)
    budget_json = Column(Text, nullable=False, default="{}")
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ProjectAutonomyDelegation(Base):
    """Parent/child relationship when a macro agent delegates work."""

    __tablename__ = "project_autonomy_delegations"

    id = Column(Integer, primary_key=True, index=True)
    parent_run_id = Column(String(64), nullable=False, index=True)
    child_run_id = Column(String(64), nullable=False, index=True)
    parent_agent_profile_id = Column(
        Integer,
        ForeignKey("project_autonomy_agent_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    child_agent_profile_id = Column(
        Integer,
        ForeignKey("project_autonomy_agent_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = Column(String(24), nullable=False, default="planned", index=True)
    intent = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ProjectAutonomyOperatorQuestion(Base):
    """Question an Autopilot agent needs the operator to answer."""

    __tablename__ = "project_autonomy_operator_questions"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), nullable=True, index=True)
    agent_profile_id = Column(
        Integer,
        ForeignKey("project_autonomy_agent_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    repo_id = Column(Integer, ForeignKey("code_repos.id", ondelete="SET NULL"), nullable=True, index=True)
    question = Column(Text, nullable=False)
    context_json = Column(Text, nullable=False, default="{}")
    status = Column(String(24), nullable=False, default="pending", index=True)
    answer = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    answered_at = Column(DateTime, nullable=True)


class ProjectAutonomyMessage(Base):
    """Persistent user/assistant chat messages for one Autopilot instance."""

    __tablename__ = "project_autonomy_messages"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), nullable=False, index=True)
    role = Column(String(24), nullable=False, default="assistant", index=True)
    message_type = Column(String(40), nullable=False, default="chat", index=True)
    content = Column(Text, nullable=False, default="")
    metadata_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProjectAutonomyStep(Base):
    """One visible stage or agent lane update for an autonomous run."""

    __tablename__ = "project_autonomy_steps"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), nullable=False, index=True)
    step_index = Column(Integer, nullable=False, default=0)
    stage = Column(String(40), nullable=False, index=True)
    agent_name = Column(String(80), nullable=False, default="architect")
    status = Column(String(24), nullable=False, default="running")
    title = Column(String(240), nullable=False)
    detail_json = Column(Text, nullable=False, default="{}")
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProjectAutonomyArtifact(Base):
    """Payloads produced by autonomy runs: plans, diffs, commands, validation, learning."""

    __tablename__ = "project_autonomy_artifacts"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), nullable=False, index=True)
    artifact_type = Column(String(40), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    content = Column(Text, nullable=True)
    content_json = Column(Text, nullable=True)
    byte_length = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProjectAutonomyArchitectReview(Base):
    """Durable architect-quality gate result for an Autopilot plan attempt."""

    __tablename__ = "project_autonomy_architect_reviews"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), nullable=False, index=True)
    attempt_index = Column(Integer, nullable=False, default=1)
    status = Column(String(40), nullable=False, default="failed", index=True)
    score = Column(Integer, nullable=False, default=0)
    confidence = Column(String(40), nullable=False, default="low")
    dimensions_json = Column(Text, nullable=False, default="{}")
    alternatives_json = Column(Text, nullable=False, default="[]")
    critique_json = Column(Text, nullable=False, default="{}")
    selected_files_json = Column(Text, nullable=False, default="[]")
    blocking_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProjectAutonomyLease(Base):
    """Repo/file/merge leases that keep concurrent autonomous runs from colliding."""

    __tablename__ = "project_autonomy_leases"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), nullable=False, index=True)
    repo_id = Column(Integer, ForeignKey("code_repos.id", ondelete="CASCADE"), nullable=False, index=True)
    lease_key = Column(String(700), nullable=False, index=True)
    file_path = Column(Text, nullable=True)
    holder = Column(String(80), nullable=False, default="architect")
    status = Column(String(24), nullable=False, default="active", index=True)
    acquired_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    released_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)


class ProjectAutonomyLearningSample(Base):
    """Evidence-gated learning data from autonomous coding trajectories."""

    __tablename__ = "project_autonomy_learning_samples"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), nullable=False, index=True)
    repo_id = Column(Integer, ForeignKey("code_repos.id", ondelete="SET NULL"), nullable=True, index=True)
    sample_type = Column(String(40), nullable=False, index=True)
    prompt = Column(Text, nullable=True)
    outcome = Column(String(40), nullable=False, default="observed", index=True)
    payload_json = Column(Text, nullable=False, default="{}")
    promoted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
