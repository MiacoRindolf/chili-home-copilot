"""Phase 3: `brain_cycle_lease` enforcement semantics (Postgres)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.trading_brain_phase1 import BrainCycleLease
from app.trading_brain.infrastructure.lease_dedicated_session import (
    brain_lease_enforcement_release_dedicated,
    brain_lease_enforcement_try_acquire_dedicated,
)
from app.trading_brain.infrastructure.lease_sqlalchemy import SqlAlchemyBrainCycleLeasePort


def test_lease_second_holder_denied_until_released(db: Session) -> None:
    lp = SqlAlchemyBrainCycleLeasePort()
    assert lp.try_acquire(
        db,
        scope_key="global",
        cycle_run_id=None,
        holder_id="holder-a",
        lease_seconds=3600,
    )
    db.commit()

    s2 = SessionLocal()
    try:
        assert not lp.try_acquire(
            s2,
            scope_key="global",
            cycle_run_id=None,
            holder_id="holder-b",
            lease_seconds=3600,
        )
        s2.rollback()
    finally:
        s2.close()

    assert not brain_lease_enforcement_try_acquire_dedicated(
        holder_id="holder-b",
        lease_seconds=60,
    )

    brain_lease_enforcement_release_dedicated(holder_id="holder-a")
    assert brain_lease_enforcement_try_acquire_dedicated(
        holder_id="holder-b",
        lease_seconds=60,
    )
    brain_lease_enforcement_release_dedicated(holder_id="holder-b")


def test_lease_expired_allows_takeover(db: Session) -> None:
    lp = SqlAlchemyBrainCycleLeasePort()
    assert lp.try_acquire(
        db,
        scope_key="global",
        cycle_run_id=None,
        holder_id="stale-holder",
        lease_seconds=3600,
    )
    row = db.query(BrainCycleLease).filter_by(scope_key="global").first()
    assert row is not None
    row.expires_at = datetime.utcnow() - timedelta(seconds=30)
    db.commit()

    assert brain_lease_enforcement_try_acquire_dedicated(
        holder_id="new-holder",
        lease_seconds=120,
    )
    brain_lease_enforcement_release_dedicated(holder_id="new-holder")
