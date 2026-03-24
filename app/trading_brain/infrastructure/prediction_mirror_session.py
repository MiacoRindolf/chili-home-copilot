"""Dedicated SessionLocal for prediction mirror writes (Phase 4; isolated from legacy prediction Session)."""

from __future__ import annotations

import logging
from uuid import uuid4

from ..schemas.prediction_snapshot import PredictionSnapshotSealDTO
from .prediction_line_mapper import legacy_prediction_rows_to_dtos
from .repositories.prediction_snapshot_sqlalchemy import SqlAlchemyBrainPredictionSnapshotRepository

logger = logging.getLogger(__name__)


def brain_prediction_mirror_write_dedicated(
    *,
    legacy_rows: list[dict],
    universe_fingerprint: str,
    ticker_count: int,
) -> int | None:
    """Persist mirror snapshot + lines in a short-lived session. Returns snapshot_id or None on skip/failure."""
    if not legacy_rows:
        return None
    from ...config import settings

    if not getattr(settings, "brain_prediction_dual_write_enabled", False):
        return None

    from ...db import SessionLocal

    header = PredictionSnapshotSealDTO(
        universe_fingerprint=universe_fingerprint,
        ticker_count=ticker_count,
        correlation_id=str(uuid4()),
    )
    lines = legacy_prediction_rows_to_dtos(legacy_rows)
    repo = SqlAlchemyBrainPredictionSnapshotRepository()
    ls = SessionLocal()
    try:
        sid = repo.seal_snapshot(ls, header=header, lines=lines)
        ls.commit()
        return sid
    except Exception:
        ls.rollback()
        logger.warning("[brain_prediction_dual_write] mirror write failed", exc_info=True)
        return None
    finally:
        ls.close()


__all__ = ["brain_prediction_mirror_write_dedicated"]
