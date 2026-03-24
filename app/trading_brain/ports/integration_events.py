from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session


class BrainIntegrationEventStore(Protocol):
    def try_insert_pending(
        self,
        db: Session,
        *,
        idempotency_key: str,
        event_id: str,
        event_type: str,
        payload_hash: str,
        payload_json: dict,
    ) -> bool: ...

    def mark_processed(self, db: Session, idempotency_key: str) -> None: ...
