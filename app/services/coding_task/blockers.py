"""Normalize failed validation steps into coding_blocker_report rows."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ...models import CodingBlockerReport
from .validator_runner import StepResult


def record_blockers_for_run(
    db: Session,
    *,
    task_id: int,
    run_id: int,
    steps: list[StepResult],
) -> None:
    for s in steps:
        if s.skipped:
            continue
        if s.timed_out or s.exit_code != 0:
            detail = {
                "step_key": s.step_key,
                "exit_code": s.exit_code,
                "timed_out": s.timed_out,
                "stderr_preview": (s.stderr or "")[:2000],
            }
            summary = (
                f"Step {s.step_key} failed (exit {s.exit_code})"
                if not s.timed_out
                else f"Step {s.step_key} timed out"
            )
            db.add(
                CodingBlockerReport(
                    task_id=task_id,
                    run_id=run_id,
                    category="validation",
                    severity="error" if s.exit_code != 0 or s.timed_out else "info",
                    summary=summary,
                    detail_json=json.dumps(detail),
                )
            )
    db.flush()
