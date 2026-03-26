"""Phase 17: explicit apply of stored snapshot diffs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import Device, PlanProject, PlanTask, ProjectMember, User
from app.models.coding_task import (
    CodingAgentSuggestion,
    CodingAgentSuggestionApply,
    PlanTaskCodingProfile,
)
from app.pairing import DEVICE_COOKIE_NAME


def _task_and_snapshot(db, user: User, diffs: list[str]) -> tuple[int, int]:
    p = PlanProject(user_id=user.id, name="P17", key="P17")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="apply", reporter_id=user.id)
    db.add(t)
    db.flush()
    db.add(PlanTaskCodingProfile(task_id=t.id, repo_index=0, sub_path=""))
    snap = CodingAgentSuggestion(
        task_id=t.id,
        user_id=user.id,
        model="m",
        response_text="r",
        diffs_json=json.dumps(diffs),
        files_changed_json="[]",
        validation_json="[]",
        context_used_json="{}",
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return t.id, snap.id


def test_apply_forbidden_guest(client, db):
    u = User(name="g")
    db.add(u)
    db.commit()
    db.refresh(u)
    p = PlanProject(user_id=u.id, name="G", key="G")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=u.id, role="owner"))
    t = PlanTask(project_id=p.id, title="t", reporter_id=u.id)
    db.add(t)
    db.flush()
    db.add(PlanTaskCodingProfile(task_id=t.id, repo_index=0, sub_path=""))
    s = CodingAgentSuggestion(
        task_id=t.id,
        user_id=u.id,
        model="m",
        response_text="r",
        diffs_json='["diff"]',
        files_changed_json="[]",
        validation_json="[]",
        context_used_json="{}",
    )
    db.add(s)
    db.commit()
    r = client.post(f"/api/planner/tasks/{t.id}/coding/agent-suggestions/{s.id}/apply", json={})
    assert r.status_code == 403


def test_apply_extra_field_422(paired_client, db):
    from app.services.coding_task import snapshot_apply as snap_mod

    client, user = paired_client
    tid, sid = _task_and_snapshot(db, user, ["--- a\n+++ b\n"])
    root = Path(__file__).resolve().parents[1]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(snap_mod, "_repo_root_for_task", lambda *a, **k: root)
    monkeypatch.setattr(snap_mod, "_run_git_apply", lambda *a, **k: (0, ""))
    try:
        r = client.post(
            f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
            json={"dry_run": False, "patch": "evil"},
        )
    finally:
        monkeypatch.undo()
    assert r.status_code == 422


def test_apply_success_records_audit(paired_client, db):
    from app.services.coding_task import snapshot_apply as snap_mod

    client, user = paired_client
    tid, sid = _task_and_snapshot(db, user, ["--- a\n+++ b\n"])
    root = Path(__file__).resolve().parents[1]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(snap_mod, "_repo_root_for_task", lambda *a, **k: root)
    monkeypatch.setattr(snap_mod, "_run_git_apply", lambda *a, **k: (0, ""))
    try:
        before = db.query(CodingAgentSuggestion).filter(CodingAgentSuggestion.id == sid).first()
        assert before is not None
        raw = before.diffs_json
        r = client.post(
            f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
            json={"dry_run": False},
        )
    finally:
        monkeypatch.undo()
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data.get("audit_id")
    after = db.query(CodingAgentSuggestion).filter(CodingAgentSuggestion.id == sid).first()
    assert after.diffs_json == raw
    audits = db.query(CodingAgentSuggestionApply).filter(CodingAgentSuggestionApply.suggestion_id == sid).all()
    assert len(audits) == 1
    assert audits[0].status == "completed"
    assert audits[0].dry_run is False


def test_apply_check_fail_audit_failed(paired_client, db):
    from app.services.coding_task import snapshot_apply as snap_mod

    client, user = paired_client
    tid, sid = _task_and_snapshot(db, user, ["bad patch"])
    root = Path(__file__).resolve().parents[1]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(snap_mod, "_repo_root_for_task", lambda *a, **k: root)
    monkeypatch.setattr(snap_mod, "_run_git_apply", lambda *a, **k: (1, "reject"))
    try:
        r = client.post(
            f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
            json={"dry_run": False},
        )
    finally:
        monkeypatch.undo()
    assert r.status_code == 400
    audits = db.query(CodingAgentSuggestionApply).filter(CodingAgentSuggestionApply.suggestion_id == sid).all()
    assert len(audits) == 1
    assert audits[0].status == "failed"


def test_apply_viewer_not_found(paired_client, db):
    from app.services.coding_task import snapshot_apply as snap_mod

    client, owner = paired_client
    p = PlanProject(user_id=owner.id, name="PV", key="PV")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=owner.id, role="owner"))
    viewer = User(name="viewer_u")
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
    db.commit()
    tid, sid = t.id, s.id

    token = "viewer-device-p17"
    db.add(Device(token=token, user_id=viewer.id, label="v", client_ip_last="127.0.0.1"))
    db.commit()
    client.cookies.set(DEVICE_COOKIE_NAME, token)

    root = Path(__file__).resolve().parents[1]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(snap_mod, "_repo_root_for_task", lambda *a, **k: root)
    monkeypatch.setattr(snap_mod, "_run_git_apply", lambda *a, **k: (0, ""))
    try:
        r = client.post(
            f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
            json={},
        )
    finally:
        monkeypatch.undo()
    assert r.status_code == 404