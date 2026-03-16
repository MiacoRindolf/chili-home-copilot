"""Learning event logger -- extracted to break the journal <-> learning cycle.

Both journal.py and learning.py import from this module instead of each other.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...models.trading import LearningEvent


def log_learning_event(
    db: Session, user_id: int | None,
    event_type: str, description: str,
    confidence_before: float | None = None,
    confidence_after: float | None = None,
    related_insight_id: int | None = None,
) -> LearningEvent:
    ev = LearningEvent(
        user_id=user_id,
        event_type=event_type,
        description=description,
        confidence_before=confidence_before,
        confidence_after=confidence_after,
        related_insight_id=related_insight_id,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def get_learning_events(db: Session, user_id: int | None, limit: int = 50) -> list[LearningEvent]:
    return db.query(LearningEvent).filter(
        LearningEvent.user_id == user_id,
    ).order_by(LearningEvent.created_at.desc()).limit(limit).all()
