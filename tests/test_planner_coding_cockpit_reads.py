"""Planner Parity v1: read-only GET chain used by planner coding cockpit (no template tests)."""
from __future__ import annotations

import json

from app.models import PlanProject, PlanTask, ProjectMember, User
from app.models.coding_task import (
    CodingAgentSuggestion,
    CodingAgentSuggestionApply,
    PlanTaskCodingProfile,
)


def _task_with_snapshot_and_apply(db, user: User):
    p = PlanProject(user_id=user.id, name="PCock", key="PCock")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="c", reporter_id=user.id)
    db.add(t)
    db.flush()
    db.add(PlanTaskCodingProfile(task_id=t.id, repo_index=0, sub_path=""))
    snap = CodingAgentSuggestion(
        task_id=t.id,
        user_id=user.id,
        model="m",
        response_text="r",
        diffs_json=json.dumps(["x"]),
        files_changed_json="[]",
        validation_json="[]",
        context_used_json="{}",
    )
    db.add(snap)
    db.flush()
    db.add(
        CodingAgentSuggestionApply(
            suggestion_id=snap.id,
            task_id=t.id,
            user_id=user.id,
            dry_run=False,
            status="completed",
            message="ok",
        )
    )
    db.commit()
    return t.id, snap.id


def test_cockpit_get_chain_summary_suggestions_apply_attempts(paired_client, db):
    client, user = paired_client
    tid, sid = _task_with_snapshot_and_apply(db, user)

    rs = client.get(f"/api/planner/tasks/{tid}/coding/summary")
    assert rs.status_code == 200
    body = rs.json()
    assert body.get("ok") is True
    assert "summary" in body
    assert "validation_runs" in body["summary"]

    r1 = client.get(f"/api/planner/tasks/{tid}/coding/agent-suggestions?limit=1")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1.get("ok") is True
    assert len(d1["suggestions"]) == 1
    assert d1["suggestions"][0]["id"] == sid

    r2 = client.get(f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply-attempts")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2.get("ok") is True
    assert len(d2["apply_attempts"]) >= 1
    row = d2["apply_attempts"][0]
    assert set(row.keys()) >= {"id", "dry_run", "status", "message_preview"}


def test_cockpit_suggestions_empty_task(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="PEmp", key="PEmp")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="e", reporter_id=user.id)
    db.add(t)
    db.commit()

    r = client.get(f"/api/planner/tasks/{t.id}/coding/agent-suggestions?limit=1")
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert d["suggestions"] == []
