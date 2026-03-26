"""Phase 6: reopen-from-blocked only; no profile creation; frozen target states."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from app.models import (
    CodingTaskBrief,
    PlanProject,
    PlanTask,
    PlanTaskCodingProfile,
    ProjectMember,
)


def test_reopen_from_blocked_no_brief_targets_not_started_no_profile_created(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P6A", key="P6A")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(
        project_id=p.id,
        title="blocked no brief",
        reporter_id=user.id,
        coding_readiness_state="blocked",
    )
    db.add(t)
    db.commit()
    tid = t.id

    pc0 = db.execute(
        text("SELECT COUNT(*) FROM plan_task_coding_profile WHERE task_id = :tid"),
        {"tid": tid},
    ).scalar()

    r = client.post(f"/api/planner/tasks/{tid}/coding/readiness/reopen-from-blocked")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["coding_readiness_state"] == "not_started"

    st = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id = :tid"),
        {"tid": tid},
    ).scalar()
    assert st == "not_started"

    pc1 = db.execute(
        text("SELECT COUNT(*) FROM plan_task_coding_profile WHERE task_id = :tid"),
        {"tid": tid},
    ).scalar()
    assert pc0 == pc1 == 0


def test_reopen_from_blocked_with_brief_targets_brief_ready_no_profile_created(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P6B", key="P6B")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(
        project_id=p.id,
        title="blocked with brief",
        reporter_id=user.id,
        coding_readiness_state="blocked",
    )
    db.add(t)
    db.flush()
    db.add(CodingTaskBrief(task_id=t.id, body="  do the thing  ", version=1, created_by=user.id))
    db.commit()
    tid = t.id

    r = client.post(f"/api/planner/tasks/{tid}/coding/readiness/reopen-from-blocked")
    assert r.status_code == 200
    assert r.json()["coding_readiness_state"] == "brief_ready"

    st = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id = :tid"),
        {"tid": tid},
    ).scalar()
    assert st == "brief_ready"

    pc = db.execute(
        text("SELECT COUNT(*) FROM plan_task_coding_profile WHERE task_id = :tid"),
        {"tid": tid},
    ).scalar()
    assert pc == 0


def test_reopen_from_blocked_clears_brief_approved_when_profile_exists(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P6C", key="P6C")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(
        project_id=p.id,
        title="blocked profile",
        reporter_id=user.id,
        coding_readiness_state="blocked",
    )
    db.add(t)
    db.flush()
    db.add(CodingTaskBrief(task_id=t.id, body="x", version=1, created_by=user.id))
    prof = PlanTaskCodingProfile(task_id=t.id, repo_index=0, sub_path="")
    db.add(prof)
    db.flush()
    prof.brief_approved_at = datetime.utcnow()
    db.commit()
    tid = t.id

    r = client.post(f"/api/planner/tasks/{tid}/coding/readiness/reopen-from-blocked")
    assert r.status_code == 200
    assert r.json()["coding_readiness_state"] == "brief_ready"

    db.refresh(prof)
    assert prof.brief_approved_at is None


def test_reopen_from_non_blocked_returns_400(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P6D", key="P6D")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(
        project_id=p.id,
        title="not blocked",
        reporter_id=user.id,
        coding_readiness_state="brief_ready",
    )
    db.add(t)
    db.commit()

    r = client.post(f"/api/planner/tasks/{t.id}/coding/readiness/reopen-from-blocked")
    assert r.status_code == 400
    assert "error" in r.json()


def test_get_coding_summary_still_idempotent_after_reopen_setup(paired_client, db):
    """Regression: reopen path must not weaken GET /coding/summary read-only contract."""
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P6E", key="P6E")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(
        project_id=p.id,
        title="idem after reopen scenario",
        reporter_id=user.id,
        coding_readiness_state="brief_ready",
    )
    db.add(t)
    db.commit()
    tid = t.id

    st0 = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id = :tid"),
        {"tid": tid},
    ).scalar()
    client.get(f"/api/planner/tasks/{tid}/coding/summary")
    st1 = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id = :tid"),
        {"tid": tid},
    ).scalar()
    assert st0 == st1 == "brief_ready"
