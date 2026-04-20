"""Planner: projects, tasks, members, comments, activity, labels, watchers."""
from sqlalchemy import Column, Integer, String, Text, Date, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from ..db import Base


class PlanProject(Base):
    __tablename__ = "plan_projects"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    key = Column(String, nullable=True)  # short prefix e.g. "KIT" for task IDs
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String, default="active")  # active | completed | archived
    color = Column(String, default="#6366f1")
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    tasks = relationship("PlanTask", back_populates="project", cascade="all, delete-orphan")
    members = relationship("ProjectMember", back_populates="project", cascade="all, delete-orphan")
    labels = relationship("PlanLabel", back_populates="project", cascade="all, delete-orphan")


class ProjectMember(Base):
    __tablename__ = "project_members"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("plan_projects.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String, default="editor", nullable=False)  # owner | editor | viewer
    joined_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("PlanProject", back_populates="members")
    user = relationship("User")


class PlanTask(Base):
    __tablename__ = "plan_tasks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("plan_projects.id"), nullable=False, index=True)
    parent_id = Column(Integer, ForeignKey("plan_tasks.id"), nullable=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String, default="todo")  # todo | in_progress | done | blocked
    priority = Column(String, default="medium")  # low | medium | high | critical
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    reporter_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    depends_on = Column(Integer, ForeignKey("plan_tasks.id"), nullable=True)
    progress = Column(Integer, default=0)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    coding_workflow_mode = Column(String(32), default="tracked", nullable=False)
    coding_readiness_state = Column(String(40), default="not_started", nullable=False)
    coding_workflow_state = Column(String(40), default="unbound", nullable=False)
    coding_workflow_state_updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    project = relationship("PlanProject", back_populates="tasks")
    assignee = relationship("User", foreign_keys=[assigned_to])
    reporter = relationship("User", foreign_keys=[reporter_id])
    dependency = relationship("PlanTask", remote_side=[id], foreign_keys=[depends_on])
    subtasks = relationship("PlanTask", foreign_keys=[parent_id])
    comments = relationship("TaskComment", back_populates="task", cascade="all, delete-orphan")
    activities = relationship("TaskActivity", back_populates="task", cascade="all, delete-orphan")
    watchers = relationship("TaskWatcher", back_populates="task", cascade="all, delete-orphan")
    task_labels = relationship("TaskLabel", back_populates="task", cascade="all, delete-orphan")
    coding_profile = relationship(
        "PlanTaskCodingProfile", back_populates="task", uselist=False, cascade="all, delete-orphan"
    )
    clarifications = relationship(
        "TaskClarification", back_populates="task", cascade="all, delete-orphan"
    )
    coding_briefs = relationship(
        "CodingTaskBrief", back_populates="task", cascade="all, delete-orphan"
    )
    validation_runs = relationship(
        "CodingTaskValidationRun", back_populates="task", cascade="all, delete-orphan"
    )
    blocker_reports = relationship(
        "CodingBlockerReport", back_populates="task", cascade="all, delete-orphan"
    )
    agent_suggestions = relationship(
        "CodingAgentSuggestion", back_populates="task", cascade="all, delete-orphan"
    )


class TaskComment(Base):
    __tablename__ = "task_comments"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    task = relationship("PlanTask", back_populates="comments")
    user = relationship("User")


class TaskActivity(Base):
    __tablename__ = "task_activities"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String, nullable=False)  # created | status_changed | assigned | comment_added | label_added | ...
    detail = Column(Text, nullable=True)  # JSON with old/new values
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("PlanTask", back_populates="activities")
    user = relationship("User")


class PlanLabel(Base):
    __tablename__ = "plan_labels"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("plan_projects.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    color = Column(String, default="#6366f1")
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("PlanProject", back_populates="labels")


class TaskLabel(Base):
    __tablename__ = "task_labels"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id"), nullable=False, index=True)
    label_id = Column(Integer, ForeignKey("plan_labels.id"), nullable=False, index=True)

    task = relationship("PlanTask", back_populates="task_labels")
    label = relationship("PlanLabel")


class TaskWatcher(Base):
    __tablename__ = "task_watchers"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("plan_tasks.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("PlanTask", back_populates="watchers")
    user = relationship("User")
