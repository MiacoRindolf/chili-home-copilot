"""DB-backed aggregate learning status for dual-read parity (Phase 2; non-authoritative)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..schemas.cycle import CycleRunStatus
from ..schemas.status import LearningStatusDTO
from ..stage_catalog import TOTAL_STAGES
from ...models.trading_brain_phase1 import BrainLearningCycleRun


class SqlAlchemyBrainLearningStatusReader:
    """Reads latest `brain_learning_cycle_run` mirror fields from `meta_json`."""

    def get_aggregate_status(self, db: Session) -> LearningStatusDTO:
        row = (
            db.query(BrainLearningCycleRun)
            .order_by(BrainLearningCycleRun.id.desc())
            .first()
        )
        if not row:
            return LearningStatusDTO()
        mir = (row.meta_json or {}).get("legacy_mirror") or {}
        running = row.status == CycleRunStatus.running.value
        started_at: str | None = None
        if isinstance(mir.get("started_at"), str):
            started_at = mir["started_at"]
        elif row.started_at is not None:
            started_at = row.started_at.isoformat()
        return LearningStatusDTO(
            running=running,
            cycle_run_id=int(row.id),
            correlation_id=row.correlation_id,
            phase=str(mir.get("phase", "idle")),
            current_step=str(mir.get("current_step", "")),
            steps_completed=int(mir.get("steps_completed", 0)),
            total_steps=int(mir.get("total_steps", TOTAL_STAGES)),
            started_at=started_at,
            step_timings={
                k: float(v)
                for k, v in (mir.get("step_timings") or {}).items()
                if isinstance(v, (int, float))
            },
            data_provider=mir.get("data_provider")
            if isinstance(mir.get("data_provider"), str)
            else None,
            last_cycle_funnel=mir.get("last_cycle_funnel")
            if isinstance(mir.get("last_cycle_funnel"), dict)
            else None,
        )


def log_learning_status_parity(
    *,
    legacy: dict,
    db_view: LearningStatusDTO,
    logger,
) -> None:
    """Log WARNING on field mismatches; never mutates legacy."""
    fields = ("running", "phase", "current_step", "steps_completed")
    mismatches: list[str] = []
    for f in fields:
        lv = legacy.get(f)
        dv = getattr(db_view, f)
        if lv != dv:
            mismatches.append(f"{f} legacy={lv!r} db_mirror={dv!r}")
    lt = legacy.get("total_steps")
    if lt != db_view.total_steps:
        mismatches.append(
            f"total_steps legacy={lt!r} db_mirror={db_view.total_steps!r} "
            f"(catalog={TOTAL_STAGES})"
        )
    if not mismatches:
        return
    logger.warning(
        "[brain_status_dual_read] mirror mismatch correlation_id=%s cycle_run_id=%s: %s",
        db_view.correlation_id,
        db_view.cycle_run_id,
        "; ".join(mismatches),
    )
