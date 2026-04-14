"""Intercom domain schemas: voice messages and consent."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class IntercomMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    from_user_id: Optional[int] = None
    to_user_id: Optional[int] = None
    is_broadcast: bool = False
    audio_path: str
    duration_ms: int = 0
    delivered: bool = False
    read: bool = False
    created_at: datetime


class IntercomConsentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    consented_at: datetime
    revoked_at: Optional[datetime] = None
