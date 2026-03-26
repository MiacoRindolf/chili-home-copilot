"""Phase 8: GET /coding/validation/runs — metadata-only, id DESC, truncated error_message."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from app.models import (
    CodingTaskValidationRun,
    PlanProject,
    PlanTask,
    ProjectMember,
)

_ALLOWED_RUN_KEYS = frozenset(
    {
        "id",
        "status",
        "trigger_source",
        "started_at",
        "finished_at",
        "exit_code",
        "timed_out",
        "error_message",
    }
)


def test_validation_runs_list_shape_order_and_default_limit(paired_client, db):
    from app.services.coding_task import service as coding_service

    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P8R", key="P8R")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Runs", reporter_id=user.id)
    db.add(t)
    db.flush()
    for i in range(3):
        db.add(
            CodingTaskValidationRun(
                task_id=t.id,
                trigger_source="manual",
                status="completed",
                started_at=datetime.utcnow(),
                finished_at=datetime.utcnow(),
                exit_code=0,
                timed_out=False,
                error_message=None,
            )
        )
    db.commit()
    tid = t.id

    r = client.get(f"/api/planner/tasks/{tid}/coding/validation/runs")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    runs = data["runs"]
    assert len(runs) == 3
    ids = [x["id"] for x in runs]
    assert ids == sorted(ids, reverse=True)
    for row in runs:
        assert set(row.keys()) == _ALLOWED_RUN_KEYS

    r2 = client.get(f"/api/planner/tasks/{tid}/coding/validation/runs?limit=1")
    assert len(r2.json()["runs"]) == 1
    assert r2.json()["runs"][0]["id"] == max(ids)

    r3 = client.get(f"/api/planner/tasks/{tid}/coding/validation/runs?limit={coding_service._VALIDATION_RUNS_LIST_MAX_LIMIT + 10}")
    assert len(r3.json()["runs"]) == 3


def test_validation_runs_list_limit_clamp_many_rows(paired_client, db):
    from app.services.coding_task import service as coding_service

    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P8M", key="P8M")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Many runs", reporter_id=user.id)
    db.add(t)
    db.flush()
    mx = coding_service._VALIDATION_RUNS_LIST_MAX_LIMIT + 5
    for _ in range(mx):
        db.add(
            CodingTaskValidationRun(
                task_id=t.id,
                trigger_source="manual",
                status="completed",
                started_at=datetime.utcnow(),
                finished_at=datetime.utcnow(),
                exit_code=0,
                timed_out=False,
                error_message=None,
            )
        )
    db.commit()
    tid = t.id

    r = client.get(f"/api/planner/tasks/{tid}/coding/validation/runs")
    assert len(r.json()["runs"]) == coding_service._VALIDATION_RUNS_LIST_DEFAULT_LIMIT

    r2 = client.get(f"/api/planner/tasks/{tid}/coding/validation/runs?limit={mx}")
    assert len(r2.json()["runs"]) == coding_service._VALIDATION_RUNS_LIST_MAX_LIMIT


def test_validation_runs_list_truncates_error_message_summary_stays_raw(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P8T", key="P8T")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Err", reporter_id=user.id)
    db.add(t)
    db.flush()
    huge = "E" * 20_000
    db.add(
        CodingTaskValidationRun(
            task_id=t.id,
            trigger_source="manual",
            status="failed",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
            exit_code=1,
            timed_out=False,
            error_message=huge,
        )
    )
    db.commit()
    tid = t.id

    lst = client.get(f"/api/planner/tasks/{tid}/coding/validation/runs").json()["runs"]
    assert len(lst) == 1
    assert lst[0]["error_message"].endswith("\n…[truncated]")
    assert len(lst[0]["error_message"]) < len(huge)

    summ = client.get(f"/api/planner/tasks/{tid}/coding/summary").json()["summary"]
    vr = summ["validation_runs"]
    assert len(vr) == 1
    assert vr[0]["error_message"] == huge


def test_validation_runs_list_invalid_limit_uses_default(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P8I", key="P8I")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Bad lim", reporter_id=user.id)
    db.add(t)
    db.flush()
    for _ in range(5):
        db.add(
            CodingTaskValidationRun(
                task_id=t.id,
                trigger_source="manual",
                status="completed",
                started_at=datetime.utcnow(),
                finished_at=datetime.utcnow(),
                exit_code=0,
                timed_out=False,
                error_message=None,
            )
        )
    db.commit()
    tid = t.id

    r = client.get(f"/api/planner/tasks/{tid}/coding/validation/runs?limit=not-a-number")
    assert r.status_code == 200
    assert len(r.json()["runs"]) == 5

    r2 = client.get(f"/api/planner/tasks/{tid}/coding/validation/runs?limit=0")
    assert r2.status_code == 200
    assert len(r2.json()["runs"]) == 5


def test_validation_runs_list_get_is_non_mutating(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P8N", key="P8N")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Idem", reporter_id=user.id)
    db.add(t)
    db.flush()
    db.add(
        CodingTaskValidationRun(
            task_id=t.id,
            trigger_source="manual",
            status="completed",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
            exit_code=0,
            timed_out=False,
            error_message=None,
        )
    )
    db.commit()
    tid = t.id

    def counts():
        return (
            db.execute(
                text("SELECT coding_readiness_state FROM plan_tasks WHERE id=:i"),
                {"i": tid},
            ).scalar(),
            db.execute(
                text("SELECT COUNT(*) FROM plan_task_coding_profile WHERE task_id=:i"),
                {"i": tid},
            ).scalar(),
            db.execute(
                text("SELECT COUNT(*) FROM task_clarification WHERE task_id=:i"),
                {"i": tid},
            ).scalar(),
            db.execute(
                text("SELECT COUNT(*) FROM coding_task_validation_run WHERE task_id=:i"),
                {"i": tid},
            ).scalar(),
        )

    before = counts()
    client.get(f"/api/planner/tasks/{tid}/coding/validation/runs")
    client.get(f"/api/planner/tasks/{tid}/coding/validation/runs")
    after = counts()
    assert before == after


def test_validation_runs_list_forbidden_guest(client, db):
    from app.models import User

    u = User(name="GuestProj")
    db.add(u)
    db.commit()
    db.refresh(u)
    p = PlanProject(user_id=u.id, name="P8G", key="P8G")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=u.id, role="owner"))
    t = PlanTask(project_id=p.id, title="G", reporter_id=u.id)
    db.add(t)
    db.commit()

    r = client.get(f"/api/planner/tasks/{t.id}/coding/validation/runs")
    assert r.status_code == 403
