"""SQLAlchemy implementation of `BrainIntegrationEventStore` (Phase 2)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ...models.trading_brain_phase1 import BrainIntegrationEvent

_PENDING = "pending"
_PROCESSED = "processed"


class SqlAlchemyBrainIntegrationEventStore:
    def try_insert_pending(
        self,
        db: Session,
        *,
        idempotency_key: str,
        event_id: str,
        event_type: str,
        payload_hash: str,
        payload_json: dict,
    ) -> bool:
        stmt = (
            insert(BrainIntegrationEvent)
            .values(
                idempotency_key=idempotency_key,
                event_id=event_id,
                event_type=event_type,
                payload_hash=payload_hash,
                payload_json=dict(payload_json),
                status=_PENDING,
            )
            .on_conflict_do_nothing(index_elements=[BrainIntegrationEvent.idempotency_key])
            .returning(BrainIntegrationEvent.idempotency_key)
        )
        row = db.execute(stmt).fetchone()
        return row is not None

    def mark_processed(self, db: Session, idempotency_key: str) -> None:
        row = (
            db.query(BrainIntegrationEvent)
            .filter(BrainIntegrationEvent.idempotency_key == idempotency_key)
            .first()
        )
        if not row:
            return
        if row.processed_at is not None and row.status == _PROCESSED:
            return
        row.processed_at = datetime.utcnow()
        row.status = _PROCESSED
