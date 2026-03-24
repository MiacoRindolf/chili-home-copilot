"""SQLAlchemy implementation of `BrainCycleLeasePort` (Phase 2 telemetry + Phase 3 enforcement)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ...models.trading_brain_phase1 import BrainCycleLease
from ..schemas.cycle import BrainCycleLeaseDTO


class SqlAlchemyBrainCycleLeasePort:
    """Row-level lease for `brain_cycle_lease`. Phase 3: `try_acquire` denies when another holder has a valid lease."""

    def try_acquire(
        self,
        db: Session,
        *,
        scope_key: str,
        cycle_run_id: int | None,
        holder_id: str,
        lease_seconds: int,
    ) -> bool:
        row = (
            db.query(BrainCycleLease)
            .filter(BrainCycleLease.scope_key == scope_key)
            .with_for_update()
            .first()
        )
        now = datetime.utcnow()
        ttl = max(1, int(lease_seconds))
        if not row:
            # Tests (and some DBs) TRUNCATE this table; migration seed is not re-applied.
            db.add(
                BrainCycleLease(
                    scope_key=scope_key,
                    holder_id=holder_id,
                    cycle_run_id=int(cycle_run_id) if cycle_run_id is not None else None,
                    acquired_at=now,
                    expires_at=now + timedelta(seconds=ttl),
                )
            )
            db.flush()
            return True
        current = (row.holder_id or "").strip()
        exp = row.expires_at
        expired = exp is None or exp <= now
        if not current or expired:
            row.holder_id = holder_id
            row.cycle_run_id = int(cycle_run_id) if cycle_run_id is not None else None
            row.acquired_at = now
            row.expires_at = now + timedelta(seconds=ttl)
            return True
        if current == holder_id:
            if cycle_run_id is not None:
                row.cycle_run_id = int(cycle_run_id)
            row.acquired_at = now
            row.expires_at = now + timedelta(seconds=ttl)
            return True
        return False

    def release(self, db: Session, *, scope_key: str, holder_id: str) -> None:
        row = (
            db.query(BrainCycleLease)
            .filter(BrainCycleLease.scope_key == scope_key)
            .with_for_update()
            .first()
        )
        if not row or (row.holder_id or "").strip() != holder_id:
            return
        row.cycle_run_id = None
        row.holder_id = ""
        row.acquired_at = None
        row.expires_at = None

    def refresh(
        self,
        db: Session,
        *,
        scope_key: str,
        holder_id: str,
        lease_seconds: int,
    ) -> bool:
        row = (
            db.query(BrainCycleLease)
            .filter(BrainCycleLease.scope_key == scope_key)
            .with_for_update()
            .first()
        )
        if not row or (row.holder_id or "").strip() != holder_id:
            return False
        now = datetime.utcnow()
        row.expires_at = now + timedelta(seconds=max(1, int(lease_seconds)))
        return True

    def current_holder(self, db: Session, *, scope_key: str) -> BrainCycleLeaseDTO | None:
        row = (
            db.query(BrainCycleLease)
            .filter(BrainCycleLease.scope_key == scope_key)
            .first()
        )
        if not row:
            return None
        return BrainCycleLeaseDTO(
            scope_key=row.scope_key,
            cycle_run_id=row.cycle_run_id,
            holder_id=row.holder_id or "",
            acquired_at=row.acquired_at,
            expires_at=row.expires_at,
        )
