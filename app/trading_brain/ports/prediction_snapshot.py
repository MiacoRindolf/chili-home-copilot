"""Port: persist legacy prediction rows as snapshot + lines (Phase 4 mirror)."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from ..schemas.prediction_snapshot import PredictionLineWriteDTO, PredictionSnapshotSealDTO


class BrainPredictionSnapshotRepository(Protocol):
    def seal_snapshot(
        self,
        db: Session,
        *,
        header: PredictionSnapshotSealDTO,
        lines: list[PredictionLineWriteDTO],
    ) -> int:
        """Insert snapshot + lines; return new snapshot id. Caller supplies committed session."""

