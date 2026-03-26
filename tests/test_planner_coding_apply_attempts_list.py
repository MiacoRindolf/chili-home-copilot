"""Phase 18: metadata-only GET for apply attempts (readable access)."""
from __future__ import annotations

import pytest

from app.models import Device, PlanProject, PlanTask, ProjectMember, User
from app.models.coding_task import (
    CodingAgentSuggestion,
    CodingAgentSuggestionApply,
    PlanTaskCodingProfile,
)
from app.pairing import DEVICE_COOKIE_NAME


def _task_snap_audits(db, user: User, *, long_message: bool = False):
    p = PlanProject(user_id=user.id, name="P18L", key="P18L")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="t", reporter_id=user.id)
    db.add(t)
    db.flush()
    db.add(PlanTaskCodingProfile(task_id=t.id, repo_index=0, sub_path=""))
    snap = CodingAgentSuggestion(
        task_id=t.id,
        user_id=user.id,
        model="m",
        response_text="r",
        diffs_json='["x"]',
        files_changed_json="[]",
        validation_json="[]",
        context_used_json="{}",
    )
    db.add(snap)
    db.flush()
    msg = ("a" * 800) if long_message else "short"
    db.add(
        CodingAgentSuggestionApply(
            suggestion_id=snap.id,
            task_id=t.id,
            user_id=user.id,
            dry_run=True,
            status="failed",
            message="fail",
        )
    )
    db.add(
        CodingAgentSuggestionApply(
            suggestion_id=snap.id,
            task_id=t.id,
            user_id=user.id,
            dry_run=False,
            status="completed",
            message=msg,
        )
    )
    db.commit()
    db.refresh(snap)
    return t.id, snap.id


def test_apply_attempts_forbidden_guest(client, db):
    u = User(name="g18")
    db.add(u)
    db.commit()
    db.refresh(u)
    p = PlanProject(user_id=u.id, name="G18", key="G18")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=u.id, role="owner"))
    t = PlanTask(project_id=p.id, title="t", reporter_id=u.id)
    db.add(t)
    db.flush()
    s = CodingAgentSuggestion(
        task_id=t.id,
        user_id=u.id,
        model="m",
        response_text="r",
        diffs_json='[]',
        files_changed_json="[]",
        validation_json="[]",
        context_used_json="{}",
    )
    db.add(s)
    db.commit()
    r = client.get(f"/api/planner/tasks/{t.id}/coding/agent-suggestions/{s.id}/apply-attempts")
    assert r.status_code == 403


def test_apply_attempts_not_found(paired_client, db):
    client, user = paired_client
    r = client.get("/api/planner/tasks/999999/coding/agent-suggestions/1/apply-attempts")
    assert r.status_code == 404


def test_apply_attempts_wrong_suggestion(paired_client, db):
    client, user = paired_client
    tid, sid = _task_snap_audits(db, user)
    r = client.get(f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid + 999}/apply-attempts")
    assert r.status_code == 404


def test_apply_attempts_ok_order_and_preview(paired_client, db):
    client, user = paired_client
    tid, sid = _task_snap_audits(db, user, long_message=True)
    n_before = db.query(CodingAgentSuggestionApply).count()
    r = client.get(
        f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply-attempts",
        params={"limit": "1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert len(data["apply_attempts"]) == 1
    row = data["apply_attempts"][0]
    assert row["id"]
    assert row["user_id"] == user.id
    assert row["dry_run"] is False
    assert row["status"] == "completed"
    assert "message_preview" in row
    assert len(row["message_preview"].encode("utf-8")) <= 300
    n_after = db.query(CodingAgentSuggestionApply).count()
    assert n_after == n_before


def test_apply_attempts_viewer_readable(paired_client, db):
    client, owner = paired_client
    p = PlanProject(user_id=owner.id, name="PV18", key="PV18")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=owner.id, role="owner"))
    viewer = User(name="viewer18")
    db.add(viewer)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=viewer.id, role="viewer"))
    t = PlanTask(project_id=p.id, title="v", reporter_id=owner.id)
    db.add(t)
    db.flush()
    db.add(PlanTaskCodingProfile(task_id=t.id, repo_index=0, sub_path=""))
    s = CodingAgentSuggestion(
        task_id=t.id,
        user_id=owner.id,
        model="m",
        response_text="r",
        diffs_json='["x"]',
        files_changed_json="[]",
        validation_json="[]",
        context_used_json="{}",
    )
    db.add(s)
    db.flush()
    db.add(
        CodingAgentSuggestionApply(
            suggestion_id=s.id,
            task_id=t.id,
            user_id=owner.id,
            dry_run=False,
            status="completed",
            message="ok",
        )
    )
    db.commit()
    tid, sid = t.id, s.id

    token = "viewer-device-p18"
    db.add(Device(token=token, user_id=viewer.id, label="v", client_ip_last="127.0.0.1"))
    db.commit()
    client.cookies.set(DEVICE_COOKIE_NAME, token)

    r_apply = client.post(
        f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
        json={},
    )
    assert r_apply.status_code == 404

    r_list = client.get(
        f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply-attempts",
    )
    assert r_list.status_code == 200
    body = r_list.json()
    assert body["ok"] is True
    assert len(body["apply_attempts"]) >= 1