"""Phase 2: planner coding read-path idempotence and approve/validation preflight (release gates)."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models import PlanProject, PlanTask, ProjectMember


def test_get_coding_summary_is_idempotent(paired_client, db):
    """
    Hard release gate: GET /coding/summary must not persist any change to plan_tasks
    or create plan_task_coding_profile rows.
    """
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="Phase2Idem", key="P2I")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(
        project_id=p.id,
        title="Idem task",
        reporter_id=user.id,
        coding_readiness_state="not_started",
        coding_workflow_mode="tracked",
    )
    db.add(t)
    db.commit()
    tid = t.id

    st0 = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id = :tid"),
        {"tid": tid},
    ).scalar()
    pc0 = db.execute(
        text("SELECT COUNT(*) FROM plan_task_coding_profile WHERE task_id = :tid"),
        {"tid": tid},
    ).scalar()

    r1 = client.get(f"/api/planner/tasks/{tid}/coding/summary")
    assert r1.status_code == 200
    st1 = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id = :tid"),
        {"tid": tid},
    ).scalar()
    pc1 = db.execute(
        text("SELECT COUNT(*) FROM plan_task_coding_profile WHERE task_id = :tid"),
        {"tid": tid},
    ).scalar()

    r2 = client.get(f"/api/planner/tasks/{tid}/coding/summary")
    assert r2.status_code == 200
    st2 = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id = :tid"),
        {"tid": tid},
    ).scalar()
    pc2 = db.execute(
        text("SELECT COUNT(*) FROM plan_task_coding_profile WHERE task_id = :tid"),
        {"tid": tid},
    ).scalar()

    assert st0 == st1 == st2 == "not_started"
    assert pc0 == pc1 == pc2 == 0

    body = r2.json()
    assert body["ok"] is True
    assert body["summary"]["coding_readiness_state"] == "needs_clarification"
    assert "ops_hints" in body["summary"]


def test_approve_brief_preflight_open_clarifications(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="Phase2Appr", key="P2A")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Appr task", reporter_id=user.id)
    db.add(t)
    db.commit()
    tid = t.id

    r = client.post(
        f"/api/planner/tasks/{tid}/coding/clarifications",
        json={"question": "Open?"},
    )
    assert r.status_code == 200

    r2 = client.post(f"/api/planner/tasks/{tid}/coding/brief/approve")
    assert r2.status_code == 400
    data = r2.json()
    assert "error" in data
    assert data.get("open_clarification_count", 0) >= 1
    assert isinstance(data.get("open_clarification_ids"), list)


def test_validation_run_preflight_open_clarifications(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="Phase2Val", key="P2V")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Val task", reporter_id=user.id)
    db.add(t)
    db.commit()
    tid = t.id

    client.post(
        f"/api/planner/tasks/{tid}/coding/clarifications",
        json={"question": "Still open?"},
    )
    r = client.post(f"/api/planner/tasks/{tid}/coding/validation/run")
    assert r.status_code == 400
    data = r.json()
    assert "error" in data
    assert data.get("open_clarification_count", 0) >= 1
