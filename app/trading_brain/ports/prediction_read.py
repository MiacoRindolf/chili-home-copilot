"""Port: read prediction mirror rows (Phase 5)."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from ..schemas.prediction_read import PredictionSnapshotHeader


class BrainPredictionReadRepository(Protocol):
    def fetch_latest_snapshot_id(self, db: Session, *, universe_fingerprint: str) -> int | None: ...

    def fetch_snapshot_header(self, db: Session, *, snapshot_id: int) -> PredictionSnapshotHeader | None: ...

    def fetch_lines_for_snapshot(self, db: Session, *, snapshot_id: int): ...  # list[BrainPredictionLine]
