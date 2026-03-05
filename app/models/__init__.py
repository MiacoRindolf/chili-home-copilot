"""SQLAlchemy models. Re-export all for backward compatibility: from app.models import User, Chore, ..."""
from .core import User, Device, PairCode
from .projects import Project, ProjectFile
from .chat import ChatLog, Conversation, ChatMessage
from .household import Chore, Birthday, ActivityLog, HousemateProfile, UserMemory, UserStatus
from .intercom import IntercomMessage, IntercomConsent
from .planner import (
    PlanProject,
    PlanTask,
    ProjectMember,
    TaskComment,
    TaskActivity,
    PlanLabel,
    TaskLabel,
    TaskWatcher,
)

__all__ = [
    "User",
    "Device",
    "PairCode",
    "Project",
    "ProjectFile",
    "ChatLog",
    "Conversation",
    "ChatMessage",
    "Chore",
    "Birthday",
    "ActivityLog",
    "HousemateProfile",
    "UserMemory",
    "UserStatus",
    "IntercomMessage",
    "IntercomConsent",
    "PlanProject",
    "PlanTask",
    "ProjectMember",
    "TaskComment",
    "TaskActivity",
    "PlanLabel",
    "TaskLabel",
    "TaskWatcher",
]
