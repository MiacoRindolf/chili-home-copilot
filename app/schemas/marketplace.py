"""Marketplace domain schemas: module registry."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class MarketplaceModuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    version: str
    summary: Optional[str] = None
    description: Optional[str] = None
    icon_url: Optional[str] = None
    homepage_url: Optional[str] = None
    repo_url: Optional[str] = None
    local_path: str
    source: Optional[str] = None
    enabled: bool = True
    checksum: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    installed_at: datetime
