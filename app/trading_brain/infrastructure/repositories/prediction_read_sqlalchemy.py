"""SQLAlchemy read repository for prediction mirror (Phase 5)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ....models.trading_brain_phase1 import BrainPredictionLine, BrainPredictionSnapshot
from ...schemas.prediction_read import PredictionSnapshotHeader


class SqlAlchemyBrainPredictionReadRepository:
    def fetch_latest_snapshot_id(self, db: Session, *, universe_fingerprint: str) -> int | None:
        row = (
            db.query(BrainPredictionSnapshot.id)
            .filter(BrainPredictionSnapshot.universe_fingerprint == universe_fingerprint)
            .order_by(BrainPredictionSnapshot.id.desc())
            .limit(1)
            .first()
        )
        return int(row[0]) if row else None

    def fetch_snapshot_header(self, db: Session, *, snapshot_id: int) -> PredictionSnapshotHeader | None:
        row = (
            db.query(BrainPredictionSnapshot.id, BrainPredictionSnapshot.as_of_ts)
            .filter(BrainPredictionSnapshot.id == int(snapshot_id))
            .first()
        )
        if not row:
            return None
        return PredictionSnapshotHeader(id=int(row[0]), as_of_ts=row[1])

    def fetch_lines_for_snapshot(self, db: Session, *, snapshot_id: int) -> list[BrainPredictionLine]:
        return (
            db.query(BrainPredictionLine)
            .filter(BrainPredictionLine.snapshot_id == int(snapshot_id))
            .order_by(BrainPredictionLine.sort_rank.asc())
            .all()
        )
