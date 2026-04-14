"""Core domain schemas: users, devices, pairing, broker credentials."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: Optional[str] = None
    google_id: Optional[str] = None
    avatar_url: Optional[str] = None


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    token: str
    label: str
    client_ip_last: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    user_id: int


class PairCodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    user_id: int
    expires_at: datetime
    used: bool


class BrokerCredentialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    broker: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BrainWorkerControlOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    wake_requested: bool
    stop_requested: bool
    last_heartbeat_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
