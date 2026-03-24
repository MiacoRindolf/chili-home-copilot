"""Phase 5 read-side types (internal; no API surface change)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PredictionSnapshotHeader:
    """Minimal header for freshness checks."""

    id: int
    as_of_ts: datetime
