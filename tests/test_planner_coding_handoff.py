"""Phase 5: read-only JSON handoff projection (allowlist, latest-run artifacts only)."""
from __future__ import annotations

from sqlalchemy import text

from app.models import PlanProject, PlanTask, ProjectMember

_ALLOWED_HANDOFF_KEYS = frozenset(
    {
        "task",
        "brief",
        "profile",
        "ops_hints",
        "validation_latest",
        "blockers",
        "artifact_previews",
        "clarifications",
        "readiness_context",
    }
)
_ALLOWED_READINESS_CONTEXT_KEYS = frozenset(
    {"coding_readiness_state", "open_clarification_count", "brief_approved_at"}
)
_ALLOWED_CLARIFICATION_KEYS = frozenset(
    {"id", "question", "answer", "status", "sort_order", "created_at", "updated_at"}
)
_ALLOWED_TASK_KEYS = frozenset(
    {"id", "project_id", "title", "coding_readiness_state", "coding_workflow_mode"}
)
_ALLOWED_PROFILE_KEYS = frozenset(
    {"repo_index", "code_repo_id", "repo_name", "repo_path", "sub_path", "workspace_bound", "brief_approved_at"}
)
_ALLOWED_OPS_KEYS = frozenset(
    {
        "code_repos_configured_count",
        "repo_index_valid",
        "workspace_bound",
        "workspace_indexed",
        "workspace_reason",
        "cwd_resolvable",
    }
)
_ALLOWED_BRIEF_KEYS = frozenset({"id", "version", "body"})
_ALLOWED_VAL_KEYS = frozenset(
    {
        "id",
        "status",
        "trigger_source",
        "exit_code",
        "timed_out",
        "error_message",
        "started_at",
        "finished_at",
    }
)
_ALLOWED_BLOCKER_KEYS = frozenset({"category", "severity", "summary"})
_ALLOWED_ART_KEYS = frozenset({"step_key", "kind", "content_preview"})


def test_handoff_ok_shape_and_allowlist(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="H5", key="H5")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Handoff task", reporter_id=user.id)
    db.add(t)
    db.commit()
    tid = t.id

    r = client.get(f"/api/planner/tasks/{tid}/coding/handoff")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    h = data["handoff"]
    assert set(h.keys()) == _ALLOWED_HANDOFF_KEYS
    assert set(h["task"].keys()) == _ALLOWED_TASK_KEYS
    assert h["task"]["project_id"] == p.id
    assert set(h["profile"].keys()) == _ALLOWED_PROFILE_KEYS
    assert h["profile"]["workspace_bound"] is False
    assert set(h["ops_hints"].keys()) == _ALLOWED_OPS_KEYS
    assert h["ops_hints"]["workspace_bound"] is False
    assert h["brief"] is None
    assert h["validation_latest"] is None
    assert h["blockers"] == []
    assert h["artifact_previews"] == []
    assert h["clarifications"] == []
    assert set(h["readiness_context"].keys()) == _ALLOWED_READINESS_CONTEXT_KEYS
    assert h["readiness_context"]["open_clarification_count"] == 0
    assert h["readiness_context"]["brief_approved_at"] is None


def test_handoff_forbidden_guest(client, db):
    """client fixture has no paired cookie."""
    from app.models import PlanProject, PlanTask, ProjectMember, User

    u = User(name="Orphan")
    db.add(u)
    db.commit()
    db.refresh(u)
    p = PlanProject(user_id=u.id, name="HG", key="HG")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=u.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Guest task", reporter_id=u.id)
    db.add(t)
    db.commit()

    r = client.get(f"/api/planner/tasks/{t.id}/coding/handoff")
    assert r.status_code == 403


def test_handoff_get_is_non_mutating(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="H5I", key="H5I")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(
        project_id=p.id,
        title="Idem handoff",
        reporter_id=user.id,
        coding_readiness_state="brief_ready",
    )
    db.add(t)
    db.commit()
    tid = t.id

    from app.models import TaskClarification

    db.add(
        TaskClarification(
            task_id=tid,
            question="Open Q?",
            status="open",
            sort_order=1,
        )
    )
    db.commit()

    st0 = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id=:i"),
        {"i": tid},
    ).scalar()
    pc0 = db.execute(
        text("SELECT COUNT(*) FROM plan_task_coding_profile WHERE task_id=:i"),
        {"i": tid},
    ).scalar()
    cc0 = db.execute(
        text("SELECT COUNT(*) FROM task_clarification WHERE task_id=:i"),
        {"i": tid},
    ).scalar()

    client.get(f"/api/planner/tasks/{tid}/coding/handoff")
    client.get(f"/api/planner/tasks/{tid}/coding/handoff")

    st1 = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id=:i"),
        {"i": tid},
    ).scalar()
    pc1 = db.execute(
        text("SELECT COUNT(*) FROM plan_task_coding_profile WHERE task_id=:i"),
        {"i": tid},
    ).scalar()
    cc1 = db.execute(
        text("SELECT COUNT(*) FROM task_clarification WHERE task_id=:i"),
        {"i": tid},
    ).scalar()

    assert st0 == st1
    assert pc0 == pc1
    assert cc0 == cc1


def test_handoff_brief_and_validation_latest_allowlists(paired_client, db):
    from datetime import datetime

    from app.models import (
        CodingTaskBrief,
        CodingTaskValidationRun,
        CodingValidationArtifact,
        PlanTaskCodingProfile,
    )

    client, user = paired_client
    p = PlanProject(user_id=user.id, name="H5F", key="H5F")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Full handoff", reporter_id=user.id)
    db.add(t)
    db.flush()
    db.add(PlanTaskCodingProfile(task_id=t.id, repo_index=0, sub_path="app"))
    db.add(CodingTaskBrief(task_id=t.id, body="Hello brief", version=1, created_by=user.id))
    run = CodingTaskValidationRun(
        task_id=t.id,
        trigger_source="manual",
        status="completed",
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
        exit_code=0,
        timed_out=False,
        error_message=None,
    )
    db.add(run)
    db.flush()
    db.add(
        CodingValidationArtifact(
            run_id=run.id,
            step_key="ast_syntax",
            kind="log",
            content="x" * 100,
            byte_length=100,
        )
    )
    db.commit()
    tid = t.id

    r = client.get(f"/api/planner/tasks/{tid}/coding/handoff")
    h = r.json()["handoff"]
    assert set(h["brief"].keys()) == _ALLOWED_BRIEF_KEYS
    assert h["brief"]["body"] == "Hello brief"
    assert set(h["profile"].keys()) == _ALLOWED_PROFILE_KEYS
    assert set(h["validation_latest"].keys()) == _ALLOWED_VAL_KEYS
    assert h["validation_latest"]["id"] == run.id
    assert len(h["artifact_previews"]) == 1
    assert set(h["artifact_previews"][0].keys()) == _ALLOWED_ART_KEYS
    for b in h["blockers"]:
        assert set(b.keys()) == _ALLOWED_BLOCKER_KEYS
    assert set(h["readiness_context"].keys()) == _ALLOWED_READINESS_CONTEXT_KEYS
    assert h["readiness_context"]["open_clarification_count"] == 0
    assert h["readiness_context"]["brief_approved_at"] is None
    assert h["clarifications"] == []


def test_handoff_phase7_clarifications_order_cap_and_allowlist(paired_client, db):
    from app.models import TaskClarification
    from app.services.coding_task import service as coding_service

    client, user = paired_client
    p = PlanProject(user_id=user.id, name="H7", key="H7")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Clar handoff", reporter_id=user.id)
    db.add(t)
    db.flush()
    lim = coding_service._HANDOFF_CLARIFICATIONS_LIMIT
    for so in range(1, lim + 2):
        db.add(
            TaskClarification(
                task_id=t.id,
                question=f"Q{so}",
                status="open",
                sort_order=so,
            )
        )
    db.commit()
    tid = t.id

    r = client.get(f"/api/planner/tasks/{tid}/coding/handoff")
    h = r.json()["handoff"]
    assert len(h["clarifications"]) == lim
    for row in h["clarifications"]:
        assert set(row.keys()) == _ALLOWED_CLARIFICATION_KEYS
    assert [row["sort_order"] for row in h["clarifications"]] == list(range(1, lim + 1))


def test_handoff_phase7_clarifications_sort_order_matches_list_clarifications(paired_client, db):
    from app.models import TaskClarification

    client, user = paired_client
    p = PlanProject(user_id=user.id, name="H7S", key="H7S")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Order", reporter_id=user.id)
    db.add(t)
    db.flush()
    db.add(TaskClarification(task_id=t.id, question="third", status="open", sort_order=30))
    db.add(TaskClarification(task_id=t.id, question="first", status="open", sort_order=10))
    db.add(TaskClarification(task_id=t.id, question="mid", status="open", sort_order=20))
    db.commit()

    r = client.get(f"/api/planner/tasks/{t.id}/coding/handoff")
    orders = [row["sort_order"] for row in r.json()["handoff"]["clarifications"]]
    assert orders == [10, 20, 30]


def test_handoff_phase7_clarification_truncation(paired_client, db):
    from app.models import TaskClarification
    from app.services.coding_task import service as coding_service

    client, user = paired_client
    p = PlanProject(user_id=user.id, name="H7T", key="H7T")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Trunc", reporter_id=user.id)
    db.add(t)
    db.flush()
    huge_q = "Q" * 50_000
    huge_a = "Z" * 50_000
    db.add(
        TaskClarification(
            task_id=t.id,
            question=huge_q,
            answer=huge_a,
            status="open",
            sort_order=1,
        )
    )
    db.commit()

    r = client.get(f"/api/planner/tasks/{t.id}/coding/handoff")
    row = r.json()["handoff"]["clarifications"][0]
    assert row["question"].endswith("\n…[truncated]")
    assert row["answer"].endswith("\n…[truncated]")
    assert len(row["question"].encode("utf-8")) <= coding_service._HANDOFF_CLAR_QUESTION_MAX_BYTES
    assert len(row["answer"].encode("utf-8")) <= coding_service._HANDOFF_CLAR_ANSWER_MAX_BYTES


def test_handoff_readiness_context_open_count(paired_client, db):
    from app.models import PlanTaskCodingProfile, TaskClarification
    from datetime import datetime

    client, user = paired_client
    p = PlanProject(user_id=user.id, name="H7R", key="H7R")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(
        project_id=p.id,
        title="RC",
        reporter_id=user.id,
        coding_readiness_state="blocked",
    )
    db.add(t)
    db.flush()
    prof = PlanTaskCodingProfile(task_id=t.id, repo_index=0, sub_path="")
    prof.brief_approved_at = datetime.utcnow()
    db.add(prof)
    db.add(
        TaskClarification(task_id=t.id, question="o1", status="open", sort_order=1),
    )
    db.add(
        TaskClarification(task_id=t.id, question="o2", status="open", sort_order=2),
    )
    db.add(
        TaskClarification(task_id=t.id, question="r1", answer="x", status="resolved", sort_order=3),
    )
    db.commit()
    tid = t.id

    r = client.get(f"/api/planner/tasks/{tid}/coding/handoff")
    rc = r.json()["handoff"]["readiness_context"]
    assert set(rc.keys()) == _ALLOWED_READINESS_CONTEXT_KEYS
    assert rc["coding_readiness_state"] == "blocked"
    assert rc["open_clarification_count"] == 2
    assert rc["brief_approved_at"] is not None
