from typing import Optional, Literal, List

from pydantic import BaseModel


class MobileChatRequest(BaseModel):
    """Request body for mobile chat API."""

    message: str
    conversation_id: Optional[int] = None
    planner_project_id: Optional[int] = None
    planner_project_name: Optional[str] = None


class MobileChatResponse(BaseModel):
    """Standardized response for mobile chat API.

    Mirrors the existing /api/chat JSON shape so clients can share handling.
    """

    trace_id: str
    user: str
    is_guest: bool
    action_type: str
    executed: bool
    reply: str
    model_used: str
    conversation_id: Optional[int] = None
    rag_sources: List[str] = []
    personality_used: bool = False


class MobileChatMessage(BaseModel):
    """Lightweight message representation for potential future mobile history APIs."""

    id: Optional[int] = None
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: Optional[str] = None
    model_used: Optional[str] = None
    action_type: Optional[str] = None

"""Chat-related Pydantic schemas (if any). Reserved for future use."""
# No chat-specific request/response schemas yet; chat uses generic JSON.
