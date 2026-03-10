"""SQLAlchemy models. Re-export all for backward compatibility: from app.models import User, Chore, ..."""

from .core import Device, PairCode, User
from .projects import Project, ProjectFile
from .chat import ChatLog, ChatMessage, Conversation
from .household import ActivityLog, Birthday, Chore, HousemateProfile, UserMemory, UserStatus
from .intercom import IntercomConsent, IntercomMessage
from .planner import (
    PlanLabel,
    PlanProject,
    PlanTask,
    ProjectMember,
    TaskActivity,
    TaskComment,
    TaskLabel,
    TaskWatcher,
)
from .marketplace import MarketplaceModule
from .trading import (
    BacktestResult,
    JournalEntry,
    MarketSnapshot,
    ScanResult,
    Trade,
    TradingInsight,
    WatchlistItem,
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
    "MarketplaceModule",
    "WatchlistItem",
    "Trade",
    "JournalEntry",
    "TradingInsight",
    "ScanResult",
    "BacktestResult",
    "MarketSnapshot",
]

