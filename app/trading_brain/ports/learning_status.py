from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from ..schemas.status import LearningStatusDTO


class BrainLearningStatusPort(Protocol):
    def get_aggregate_status(self, db: Session) -> LearningStatusDTO: ...
