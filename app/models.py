from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime, timedelta
from .db import Base

class Chore(Base):
    __tablename__ = "chores"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    done = Column(Boolean, default=False)
    priority = Column(String, default="medium")  # low | medium | high
    due_date = Column(Date, nullable=True)
    recurrence = Column(String, default="none")  # none | daily | weekly | monthly
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    assignee = relationship("User", foreign_keys=[assigned_to])

class Birthday(Base):
    __tablename__ = "birthdays"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    date = Column(Date, nullable=False)

class ChatLog(Base):
    __tablename__ = "chat_logs"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    client_ip = Column(String, nullable=False)
    trace_id = Column(String, nullable=False)
    message = Column(String, nullable=False)
    action_type = Column(String, nullable=False)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=True)

    devices = relationship("Device", back_populates="user")


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, nullable=False, index=True)
    label = Column(String, nullable=False)
    client_ip_last = Column(String, nullable=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user = relationship("User", back_populates="devices")


class PairCode(Base):
    __tablename__ = "pair_codes"

    code = Column(String, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False)


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    color = Column(String, default="#6366f1")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    files = relationship("ProjectFile", back_populates="project", cascade="all, delete-orphan")
    conversations = relationship("Conversation", back_populates="project")


class ProjectFile(Base):
    __tablename__ = "project_files"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    original_name = Column(String, nullable=False)
    stored_name = Column(String, nullable=False)
    content_type = Column(String, nullable=False)
    file_size = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="files")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    convo_key = Column(String, index=True, nullable=False)
    title = Column(String, default="New Chat")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="conversations")
    messages = relationship("ChatMessage", back_populates="conversation", cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    convo_key = Column(String, index=True, nullable=False)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True, index=True)

    role = Column(String, nullable=False)   # "user" or "assistant"
    content = Column(Text, nullable=False)

    trace_id = Column(String, nullable=True)
    action_type = Column(String, nullable=True)
    model_used = Column(String, nullable=True)
    image_path = Column(String, nullable=True)

    conversation = relationship("Conversation", back_populates="messages")


class HousemateProfile(Base):
    __tablename__ = "housemate_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    interests = Column(Text, nullable=True)
    dietary = Column(String, nullable=True)
    tone = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    last_extracted_at = Column(DateTime, nullable=True)
    message_count_at_extraction = Column(Integer, default=0)

    user = relationship("User")


class UserStatus(Base):
    __tablename__ = "user_statuses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    status = Column(String, default="available", nullable=False)  # available | dnd
    dnd_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")


class IntercomMessage(Base):
    __tablename__ = "intercom_messages"

    id = Column(Integer, primary_key=True, index=True)
    from_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    to_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    is_broadcast = Column(Boolean, default=False)
    audio_path = Column(String, nullable=False)
    duration_ms = Column(Integer, default=0)
    delivered = Column(Boolean, default=False)
    read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    from_user = relationship("User", foreign_keys=[from_user_id])
    to_user = relationship("User", foreign_keys=[to_user_id])


class UserMemory(Base):
    __tablename__ = "user_memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    category = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    source_message_id = Column(Integer, ForeignKey("chat_messages.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    superseded = Column(Boolean, default=False)

    user = relationship("User")


class IntercomConsent(Base):
    __tablename__ = "intercom_consents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    consented_at = Column(DateTime, default=datetime.utcnow)
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User")


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    user_name = Column(String, nullable=True)
    event_type = Column(String, nullable=False)  # chore_added | chore_done | birthday_added | chat_started | memory_added
    description = Column(Text, nullable=False)
    icon = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


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

    project = relationship("PlanProject", back_populates="tasks")
    assignee = relationship("User", foreign_keys=[assigned_to])
    reporter = relationship("User", foreign_keys=[reporter_id])
    dependency = relationship("PlanTask", remote_side=[id], foreign_keys=[depends_on])
    subtasks = relationship("PlanTask", foreign_keys=[parent_id])
    comments = relationship("TaskComment", back_populates="task", cascade="all, delete-orphan")
    activities = relationship("TaskActivity", back_populates="task", cascade="all, delete-orphan")
    watchers = relationship("TaskWatcher", back_populates="task", cascade="all, delete-orphan")
    task_labels = relationship("TaskLabel", back_populates="task", cascade="all, delete-orphan")


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
