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
