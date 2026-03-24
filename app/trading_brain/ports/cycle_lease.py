from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from ..schemas.cycle import BrainCycleLeaseDTO


class BrainCycleLeasePort(Protocol):
    def try_acquire(
        self,
        db: Session,
        *,
        scope_key: str,
        cycle_run_id: int | None,
        holder_id: str,
        lease_seconds: int,
    ) -> bool: ...

    def release(self, db: Session, *, scope_key: str, holder_id: str) -> None: ...

    def refresh(
        self,
        db: Session,
        *,
        scope_key: str,
        holder_id: str,
        lease_seconds: int,
    ) -> bool: ...

    def current_holder(self, db: Session, *, scope_key: str) -> BrainCycleLeaseDTO | None: ...
