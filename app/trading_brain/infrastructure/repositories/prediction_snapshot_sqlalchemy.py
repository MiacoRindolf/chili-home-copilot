"""SQLAlchemy mirror repository for prediction snapshots (Phase 4)."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from ....models.trading_brain_phase1 import BrainPredictionLine, BrainPredictionSnapshot
from ...schemas.prediction_snapshot import PredictionLineWriteDTO, PredictionSnapshotSealDTO


class SqlAlchemyBrainPredictionSnapshotRepository:
    def seal_snapshot(
        self,
        db: Session,
        *,
        header: PredictionSnapshotSealDTO,
        lines: list[PredictionLineWriteDTO],
    ) -> int:
        now = datetime.utcnow()
        corr = header.correlation_id or str(uuid4())
        snap = BrainPredictionSnapshot(
            as_of_ts=header.as_of_ts or now,
            universe_fingerprint=header.universe_fingerprint,
            ticker_count=int(header.ticker_count),
            source_tag=header.source_tag or "legacy_get_current_predictions",
            correlation_id=corr[:40],
        )
        db.add(snap)
        db.flush()
        sid = int(snap.id)
        for dto in lines:
            db.add(
                BrainPredictionLine(
                    snapshot_id=sid,
                    sort_rank=dto.sort_rank,
                    ticker=dto.ticker[:32],
                    score=float(dto.score),
                    confidence=dto.confidence,
                    direction=(dto.direction[:32] if dto.direction else None),
                    price=dto.price,
                    meta_ml_probability=dto.meta_ml_probability,
                    vix_regime=(dto.vix_regime[:32] if dto.vix_regime else None),
                    signals_json=list(dto.signals),
                    matched_patterns_json=list(dto.matched_patterns),
                    suggested_stop=dto.suggested_stop,
                    suggested_target=dto.suggested_target,
                    risk_reward=dto.risk_reward,
                    position_size_pct=dto.position_size_pct,
                )
            )
        db.flush()
        return sid
