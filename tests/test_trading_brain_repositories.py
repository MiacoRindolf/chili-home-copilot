"""Phase 2: trading-brain SQLAlchemy repositories (PostgreSQL)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.models.trading_brain_phase1 import BrainLearningCycleRun, BrainStageJob
from app.trading_brain.infrastructure.integration_sqlalchemy import (
    SqlAlchemyBrainIntegrationEventStore,
)
from app.trading_brain.infrastructure.lease_sqlalchemy import SqlAlchemyBrainCycleLeasePort
from app.trading_brain.infrastructure.repositories.cycle_sqlalchemy import (
    SqlAlchemyBrainLearningCycleRunRepository,
    SqlAlchemyBrainStageJobRepository,
)
from app.trading_brain.schemas.cycle import (
    CycleRunStatus,
    StageDefinition,
    StageJobStatus,
)
from app.trading_brain.stage_catalog import STAGE_KEYS
from app.trading_brain.infrastructure.learning_status_sqlalchemy import (
    log_learning_status_parity,
)
from app.trading_brain.wiring import (
    brain_shadow_before_commit,
    brain_shadow_begin_cycle,
    brain_shadow_finally,
    make_learning_status_reader,
)


def test_cycle_run_crud_and_stage_cascade(db: Session) -> None:
    cr = SqlAlchemyBrainLearningCycleRunRepository()
    sj = SqlAlchemyBrainStageJobRepository()
    rid = cr.create(
        db,
        correlation_id="c1",
        universe_id="full",
        meta_json={"x": 1},
    )
    sj.create_jobs_for_cycle(
        db,
        cycle_run_id=rid,
        stages=[StageDefinition(stage_key="a", ordinal=1)],
    )
    db.commit()
    dto = cr.get(db, rid)
    assert dto is not None
    assert dto.correlation_id == "c1"
    assert dto.status == CycleRunStatus.running
    jobs = sj.get_jobs_for_cycle(db, rid)
    assert len(jobs) == 1
    cr.update_status(db, rid, status=CycleRunStatus.succeeded, finished_at=datetime.utcnow())
    db.commit()
    run = db.query(BrainLearningCycleRun).filter(BrainLearningCycleRun.id == rid).first()
    assert run is not None
    db.delete(run)
    db.commit()
    remaining = db.query(BrainStageJob).filter(BrainStageJob.cycle_run_id == rid).count()
    assert remaining == 0


def test_stage_job_claim_next_runnable(db: Session) -> None:
    cr = SqlAlchemyBrainLearningCycleRunRepository()
    sj = SqlAlchemyBrainStageJobRepository()
    r1 = cr.create(db, correlation_id="a", universe_id=None, meta_json={})
    r2 = cr.create(db, correlation_id="b", universe_id=None, meta_json={})
    sj.create_jobs_for_cycle(
        db,
        cycle_run_id=r1,
        stages=[StageDefinition(stage_key="s1", ordinal=1)],
    )
    sj.create_jobs_for_cycle(
        db,
        cycle_run_id=r2,
        stages=[StageDefinition(stage_key="s2", ordinal=1)],
    )
    db.commit()
    claimed = sj.claim_next_runnable(db, worker_id="w1", lease_seconds=60)
    assert claimed is not None
    assert claimed.cycle_run_id == r1
    assert claimed.status == StageJobStatus.leased
    db.commit()


def test_lease_port_roundtrip(db: Session) -> None:
    lp = SqlAlchemyBrainCycleLeasePort()
    cr = SqlAlchemyBrainLearningCycleRunRepository()
    rid = cr.create(db, correlation_id="l1", universe_id=None, meta_json={})
    db.commit()
    assert lp.try_acquire(
        db,
        scope_key="global",
        cycle_run_id=rid,
        holder_id="h1",
        lease_seconds=120,
    )
    db.commit()
    cur = lp.current_holder(db, scope_key="global")
    assert cur is not None
    assert cur.holder_id == "h1"
    assert cur.cycle_run_id == rid
    assert lp.refresh(db, scope_key="global", holder_id="h1", lease_seconds=300)
    db.commit()
    lp.release(db, scope_key="global", holder_id="h1")
    db.commit()
    after = lp.current_holder(db, scope_key="global")
    assert after is not None
    assert after.holder_id == ""


def test_integration_event_idempotent_insert_and_mark_processed(db: Session) -> None:
    store = SqlAlchemyBrainIntegrationEventStore()
    assert store.try_insert_pending(
        db,
        idempotency_key="ik1",
        event_id="e1",
        event_type="t1",
        payload_hash="h1",
        payload_json={"a": 1},
    )
    db.commit()
    assert not store.try_insert_pending(
        db,
        idempotency_key="ik1",
        event_id="e1",
        event_type="t1",
        payload_hash="h1",
        payload_json={"a": 1},
    )
    store.mark_processed(db, "ik1")
    db.commit()
    store.mark_processed(db, "ik1")
    db.commit()


def test_shadow_hooks_happy_path_increment(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal parity: begin → before_commit steps 0→1 → finally."""
    from app.config import settings

    monkeypatch.setattr(settings, "brain_cycle_shadow_write_enabled", True, raising=False)
    ctx: dict = {}
    ls = {
        "running": True,
        "phase": "pre-filtering",
        "current_step": "x",
        "steps_completed": 0,
        "total_steps": 25,
        "started_at": datetime.utcnow().isoformat(),
        "step_timings": {},
        "data_provider": "yfinance",
        "last_cycle_funnel": None,
        "last_cycle_budget": None,
    }
    brain_shadow_begin_cycle(
        db,
        ctx=ctx,
        full_universe=True,
        data_provider="yfinance",
        learning_status=ls,
    )
    db.commit()
    assert ctx.get("run_id")
    ls["steps_completed"] = 1
    brain_shadow_before_commit(db, ctx=ctx, learning_status=ls)
    db.commit()
    sj = SqlAlchemyBrainStageJobRepository()
    jobs = sj.get_jobs_for_cycle(db, ctx["run_id"])
    pre = next(j for j in jobs if j.stage_key == STAGE_KEYS[0])
    assert pre.status == StageJobStatus.succeeded
    ls["running"] = False
    ls["phase"] = "idle"
    ls["current_step"] = ""
    run_id = int(ctx["run_id"])
    brain_shadow_finally(
        db,
        ctx=ctx,
        learning_status=ls,
        interrupted=False,
        report_error=None,
    )
    db.commit()
    cr = SqlAlchemyBrainLearningCycleRunRepository()
    final = cr.get(db, run_id)
    assert final is not None
    assert final.status == CycleRunStatus.succeeded


def test_learning_status_parity_logger_no_crash(db: Session) -> None:
    import logging

    reader = make_learning_status_reader()
    dto = reader.get_aggregate_status(db)
    assert isinstance(dto.steps_completed, int)
    log_learning_status_parity(
        legacy={
            "running": False,
            "phase": "idle",
            "steps_completed": 0,
            "total_steps": 14,
            "current_step": "",
        },
        db_view=dto,
        logger=logging.getLogger("test_brain_parity"),
    )
