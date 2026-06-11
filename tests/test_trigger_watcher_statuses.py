"""trigger_watcher must watch the SAME readiness states as the miner.

It hardcoded 'ready_for_dispatch' — a state no workflow transition ever
produces — so code_brain_events stayed empty forever and the reactive
dispatch loop never fired (0 rows ever, verified live 2026-06-11).
"""

from __future__ import annotations

import pytest

from app.models.planner import PlanProject, PlanTask
from app.services.code_brain.trigger_watcher import (
    _dispatch_task_statuses,
    watch_plan_tasks,
)


def test_statuses_come_from_env(monkeypatch):
    monkeypatch.setenv("CHILI_DISPATCH_TASK_STATUSES", "brief_ready, ready_for_dispatch")
    assert _dispatch_task_statuses() == ["brief_ready", "ready_for_dispatch"]

    monkeypatch.delenv("CHILI_DISPATCH_TASK_STATUSES", raising=False)
    assert _dispatch_task_statuses() == ["ready_for_dispatch"]


@pytest.fixture
def project(db, paired_client):
    from sqlalchemy import text

    # code_brain_events is migration-created (not in ORM metadata), so the
    # truncating db fixture does NOT clear it — stale events from previous
    # runs would suppress enqueues and poison these tests.
    db.execute(text("DELETE FROM code_brain_events"))
    _client, user = paired_client
    p = PlanProject(user_id=user.id, name="WatcherProj", key="WCH")
    db.add(p)
    db.flush()
    return p


def test_watch_plan_tasks_enqueues_for_configured_state(db, project, monkeypatch):
    monkeypatch.setenv("CHILI_DISPATCH_TASK_STATUSES", "brief_ready")
    db.add(PlanTask(project_id=project.id, title="Implement the thing",
                    coding_readiness_state="brief_ready"))
    db.commit()

    enqueued = watch_plan_tasks(db)
    assert enqueued == 1

    # Idempotent on the second tick (unclaimed event already present).
    assert watch_plan_tasks(db) == 0


def test_watch_plan_tasks_ignores_non_configured_states(db, project, monkeypatch):
    monkeypatch.setenv("CHILI_DISPATCH_TASK_STATUSES", "brief_ready")
    db.add(PlanTask(project_id=project.id, title="not ready", coding_readiness_state="not_started"))
    db.add(PlanTask(project_id=project.id, title="blocked", coding_readiness_state="blocked"))
    db.commit()

    assert watch_plan_tasks(db) == 0


def test_no_reenqueue_after_failed_attempt_until_task_changes(db, project, monkeypatch):
    """The live spam loop: a task whose dispatch terminally fails (e.g. no
    workspace binding) was re-enqueued every 30s forever, because the dedupe
    only looked at UNCLAIMED events. A processed attempt now suppresses
    re-enqueue until the task row changes."""
    from datetime import datetime, timedelta

    from sqlalchemy import text

    monkeypatch.setenv("CHILI_DISPATCH_TASK_STATUSES", "brief_ready")
    task = PlanTask(project_id=project.id, title="no workspace", coding_readiness_state="brief_ready")
    db.add(task)
    db.commit()

    assert watch_plan_tasks(db) == 1

    # Simulate the processor finishing (escalated) AFTER the task's last update.
    db.execute(text(
        "UPDATE code_brain_events SET claimed_at = now(), processed_at = now(), "
        "outcome = 'escalated', error_message = 'draft_failed' "
        "WHERE subject_kind = 'plan_task' AND subject_id = :s"
    ), {"s": task.id})
    db.commit()

    # Same task state -> suppressed (this was the infinite loop).
    assert watch_plan_tasks(db) == 0

    # Operator touches the task (e.g. binds a workspace) -> eligible again.
    task.updated_at = datetime.utcnow() + timedelta(seconds=1)
    db.commit()
    assert watch_plan_tasks(db) == 1
