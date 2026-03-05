from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from ..db import Base


class MarketplaceModule(Base):
    """Metadata for a third-party module installed via the marketplace."""

    __tablename__ = "marketplace_modules"

    id: int = Column(Integer, primary_key=True, index=True)

    # Identity
    slug: str = Column(String(255), unique=True, index=True, nullable=False)
    name: str = Column(String(255), nullable=False)
    version: str = Column(String(64), nullable=False)

    # Human-facing info
    summary: str = Column(String(512), nullable=True)
    description: str = Column(String(4096), nullable=True)
    icon_url: str = Column(String(1024), nullable=True)
    homepage_url: str = Column(String(1024), nullable=True)
    repo_url: str = Column(String(1024), nullable=True)

    # Local installation
    local_path: str = Column(String(1024), nullable=False)
    source: str = Column(String(255), nullable=True)  # e.g. "registry", "manual"
    enabled: bool = Column(Boolean, default=True, nullable=False)

    # Registry / update metadata
    checksum: str = Column(String(255), nullable=True)
    last_checked_at: Optional[datetime] = Column(DateTime, nullable=True)

    installed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)

    def local_path_obj(self) -> Path:
        return Path(self.local_path)

