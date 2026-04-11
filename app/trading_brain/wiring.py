"""Factories for trading-brain repositories + Phase 2 shadow helpers (no global singletons)."""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from ..models.trading_brain_phase1 import BrainLearningCycleRun, BrainStageJob
from .infrastructure.integration_sqlalchemy import SqlAlchemyBrainIntegrationEventStore
from .infrastructure.lease_sqlalchemy import SqlAlchemyBrainCycleLeasePort
from .infrastructure.learning_status_sqlalchemy import (
    SqlAlchemyBrainLearningStatusReader,
    log_learning_status_parity,
)
from .infrastructure.repositories.cycle_sqlalchemy import (
    SqlAlchemyBrainLearningCycleRunRepository,
    SqlAlchemyBrainStageJobRepository,
)
from .infrastructure.repositories.prediction_read_sqlalchemy import (
    SqlAlchemyBrainPredictionReadRepository,
)
from .infrastructure.repositories.prediction_snapshot_sqlalchemy import (
    SqlAlchemyBrainPredictionSnapshotRepository,
)
from .schemas.cycle import CycleRunStatus, StageDefinition, StageJobStatus
from .schemas.status import LearningStatusDTO
from .stage_catalog import STAGE_KEYS

logger = logging.getLogger(__name__)

_SCOPE_GLOBAL = "global"


def make_cycle_run_repo() -> SqlAlchemyBrainLearningCycleRunRepository:
    return SqlAlchemyBrainLearningCycleRunRepository()


def make_stage_job_repo() -> SqlAlchemyBrainStageJobRepository:
    return SqlAlchemyBrainStageJobRepository()


def make_lease_port() -> SqlAlchemyBrainCycleLeasePort:
    return SqlAlchemyBrainCycleLeasePort()


def make_integration_store() -> SqlAlchemyBrainIntegrationEventStore:
    return SqlAlchemyBrainIntegrationEventStore()


def make_learning_status_reader() -> SqlAlchemyBrainLearningStatusReader:
    return SqlAlchemyBrainLearningStatusReader()


def make_prediction_snapshot_repo() -> SqlAlchemyBrainPredictionSnapshotRepository:
    return SqlAlchemyBrainPredictionSnapshotRepository()


def make_prediction_read_repo() -> SqlAlchemyBrainPredictionReadRepository:
    return SqlAlchemyBrainPredictionReadRepository()


def _holder_id() -> str:
    return f"{os.getpid()}@{socket.gethostname()}"


def _mirror_payload(learning_status: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": learning_status.get("phase") or "",
        "current_step": learning_status.get("current_step") or "",
        "steps_completed": int(learning_status.get("steps_completed") or 0),
        "total_steps": learning_status.get("total_steps"),
        "started_at": learning_status.get("started_at"),
        "data_provider": learning_status.get("data_provider"),
        "step_timings": dict(learning_status.get("step_timings") or {}),
        "last_cycle_funnel": learning_status.get("last_cycle_funnel"),
        "last_cycle_budget": learning_status.get("last_cycle_budget"),
    }


def _persist_run_meta(
    db: Session, run_id: int, learning_status: dict[str, Any], correlation_id: str
) -> None:
    row = (
        db.query(BrainLearningCycleRun)
        .filter(BrainLearningCycleRun.id == int(run_id))
        .first()
    )
    if not row:
        return
    meta = dict(row.meta_json or {})
    meta["legacy_mirror"] = _mirror_payload(learning_status)
    meta["correlation_id_echo"] = correlation_id
    row.meta_json = meta


def brain_shadow_begin_cycle(
    db: Session,
    *,
    ctx: dict[str, Any],
    full_universe: bool,
    data_provider: str,
    learning_status: dict[str, Any],
) -> None:
    ctx.clear()
    from ..config import settings

    if not getattr(settings, "brain_cycle_shadow_write_enabled", False):
        return
    try:
        cycle_repo = make_cycle_run_repo()
        stage_repo = make_stage_job_repo()
        correlation_id = uuid4().hex[:32]
        universe_id = "full" if full_universe else "partial"
        meta = {
            "legacy_mirror": _mirror_payload(learning_status),
            "data_provider_at_start": data_provider,
        }
        run_id = cycle_repo.create(
            db,
            correlation_id=correlation_id,
            universe_id=universe_id,
            meta_json=meta,
        )
        stages = [
            StageDefinition(stage_key=key, ordinal=i + 1)
            for i, key in enumerate(STAGE_KEYS)
        ]
        stage_repo.create_jobs_for_cycle(db, cycle_run_id=run_id, stages=stages)
        jobs = stage_repo.get_jobs_for_cycle(db, run_id)
        job_by_key = {j.stage_key: j.id for j in jobs}
        ctx.update(
            run_id=run_id,
            correlation_id=correlation_id,
            job_by_key=job_by_key,
            prev_k=0,
        )
        if getattr(settings, "brain_lease_shadow_write_enabled", False) and not getattr(
            settings, "brain_cycle_lease_enforcement_enabled", False
        ):
            try:
                lease = make_lease_port()
                hid = _holder_id()
                lease.try_acquire(
                    db,
                    scope_key=_SCOPE_GLOBAL,
                    cycle_run_id=run_id,
                    holder_id=hid,
                    lease_seconds=max(60, int(getattr(settings, "learning_cycle_stale_seconds", 10800))),
                )
                ctx["lease_holder_id"] = hid
            except Exception as e:
                logger.warning("[brain_shadow] lease mirror acquire failed (ignored): %s", e)
        db.flush()
    except Exception as e:
        logger.warning("[brain_shadow] begin_cycle failed (ignored): %s", e, exc_info=True)
        ctx.clear()


def brain_shadow_before_commit(
    db: Session,
    *,
    ctx: dict[str, Any],
    learning_status: dict[str, Any],
) -> None:
    from ..config import settings

    if not getattr(settings, "brain_cycle_shadow_write_enabled", False):
        return
    run_id = ctx.get("run_id")
    job_by_key: dict[str, int] = ctx.get("job_by_key") or {}
    if not run_id or not job_by_key:
        return
    try:
        stage_repo = make_stage_job_repo()
        prev_k = int(ctx.get("prev_k", 0))
        k = int(learning_status.get("steps_completed") or 0)
        phase = str(learning_status.get("phase") or "")

        if k > prev_k:
            if k > prev_k + 1:
                for idx in range(prev_k, k):
                    key = STAGE_KEYS[idx]
                    jid = job_by_key.get(key)
                    if jid is not None:
                        stage_repo.update_job(
                            db,
                            jid,
                            status=StageJobStatus.skipped,
                            skip_reason="legacy_jump",
                        )
            else:
                key = STAGE_KEYS[k - 1]
                jid = job_by_key.get(key)
                if jid is not None:
                    stage_repo.update_job(db, jid, status=StageJobStatus.succeeded)
            ctx["prev_k"] = k

        cur_sid = str(learning_status.get("current_step_sid") or "")
        if phase == "backtesting" and cur_sid == "bt_insights":
            jid = job_by_key.get("bt_insights")
            if jid is not None:
                stage_repo.update_job(db, jid, status=StageJobStatus.running)
        elif phase == "pattern_engine" and cur_sid == "pattern_engine":
            jid = job_by_key.get("pattern_engine")
            if jid is not None:
                stage_repo.update_job(db, jid, status=StageJobStatus.running)

        _persist_run_meta(db, int(run_id), learning_status, str(ctx.get("correlation_id", "")))

        if (
            getattr(settings, "brain_lease_shadow_write_enabled", False)
            and ctx.get("lease_holder_id")
            and not getattr(settings, "brain_cycle_lease_enforcement_enabled", False)
        ):
            try:
                make_lease_port().refresh(
                    db,
                    scope_key=_SCOPE_GLOBAL,
                    holder_id=str(ctx["lease_holder_id"]),
                    lease_seconds=max(
                        60,
                        int(getattr(settings, "learning_cycle_stale_seconds", 10800)),
                    ),
                )
            except Exception as e:
                logger.warning("[brain_shadow] lease refresh failed (ignored): %s", e)
        db.flush()
    except Exception as e:
        logger.warning("[brain_shadow] before_commit failed (ignored): %s", e, exc_info=True)


def brain_shadow_finally(
    db: Session,
    *,
    ctx: dict[str, Any],
    learning_status: dict[str, Any],
    interrupted: bool,
    report_error: str | None,
) -> None:
    from ..config import settings

    if not getattr(settings, "brain_cycle_shadow_write_enabled", False):
        ctx.clear()
        return
    run_id = ctx.get("run_id")
    if not run_id:
        ctx.clear()
        return
    try:
        cycle_repo = make_cycle_run_repo()
        stage_repo = make_stage_job_repo()
        _persist_run_meta(db, int(run_id), learning_status, str(ctx.get("correlation_id", "")))

        if report_error:
            final = CycleRunStatus.failed
        elif interrupted:
            final = CycleRunStatus.cancelled
        else:
            final = CycleRunStatus.succeeded

        rows = (
            db.query(BrainStageJob)
            .filter(BrainStageJob.cycle_run_id == int(run_id))
            .filter(BrainStageJob.status == StageJobStatus.queued.value)
            .all()
        )
        for row in rows:
            stage_repo.update_job(
                db,
                int(row.id),
                status=StageJobStatus.skipped,
                skip_reason="cycle_end_or_abort",
            )

        cycle_repo.update_status(
            db,
            int(run_id),
            status=final,
            finished_at=datetime.utcnow(),
        )

        if (
            getattr(settings, "brain_lease_shadow_write_enabled", False)
            and ctx.get("lease_holder_id")
            and not getattr(settings, "brain_cycle_lease_enforcement_enabled", False)
        ):
            try:
                make_lease_port().release(
                    db,
                    scope_key=_SCOPE_GLOBAL,
                    holder_id=str(ctx["lease_holder_id"]),
                )
            except Exception as e:
                logger.warning("[brain_shadow] lease release failed (ignored): %s", e)
        # Explicit guard: only commit when this block attached pending ORM state.
        # After flush(), dirty/new/deleted are cleared, so snapshot before flush.
        _shadow_finally_has_pending = bool(db.dirty or db.new or db.deleted)
        db.flush()
        if _shadow_finally_has_pending:
            try:
                db.commit()
            except Exception as e:
                logger.warning("[brain_shadow] finally commit failed (ignored): %s", e)
    except Exception as e:
        logger.warning("[brain_shadow] finally failed (ignored): %s", e, exc_info=True)
    finally:
        ctx.clear()


def dual_read_compare_status(legacy: dict[str, Any], db: Session) -> None:
    from ..config import settings

    if not getattr(settings, "brain_status_dual_read_enabled", False):
        return
    try:
        reader = make_learning_status_reader()
        dto = reader.get_aggregate_status(db)
        log_learning_status_parity(legacy=legacy, db_view=dto, logger=logger)
    except Exception as e:
        logger.warning("[brain_status_dual_read] read failed (ignored): %s", e)


__all__ = [
    "brain_shadow_begin_cycle",
    "brain_shadow_before_commit",
    "brain_shadow_finally",
    "dual_read_compare_status",
    "log_learning_status_parity",
    "make_cycle_run_repo",
    "make_integration_store",
    "make_lease_port",
    "make_learning_status_reader",
    "make_stage_job_repo",
    "LearningStatusDTO",
]

# Re-export Phase 3 lease helpers (implementation lives in lease_dedicated_session).
from .infrastructure.lease_dedicated_session import (  # noqa: E402
    brain_lease_enforcement_log_peer_on_denial,
    brain_lease_enforcement_refresh_soft_dedicated,
    brain_lease_enforcement_release_dedicated,
    brain_lease_enforcement_try_acquire_dedicated,
    brain_lease_holder_id,
)

__all__ += [
    "brain_lease_enforcement_log_peer_on_denial",
    "brain_lease_enforcement_refresh_soft_dedicated",
    "brain_lease_enforcement_release_dedicated",
    "brain_lease_enforcement_try_acquire_dedicated",
    "brain_lease_holder_id",
]
