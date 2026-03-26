"""Phase 19: POST apply with dry_run true — check-only, append-only audit."""
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
    p = PlanProject(user_id=user.id, name="P19", key="P19")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="dry", reporter_id=user.id)
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


def test_dry_run_success_one_git_check_only(paired_client, db):
    """Phase 19: dry_run uses a single git apply --check; no second apply."""
    from app.services.coding_task import snapshot_apply as snap_mod

    client, user = paired_client
    tid, sid = _task_and_snapshot(db, user, ["--- a\n+++ b\n"])
    root = Path(__file__).resolve().parents[1]
    calls: list[bool] = []

    def fake_run(cwd, patch, *, check_only: bool):
        calls.append(check_only)
        return (0, "")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(snap_mod, "_repo_root_for_task", lambda *a, **k: root)
    monkeypatch.setattr(snap_mod, "_run_git_apply", fake_run)
    try:
        r = client.post(
            f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
            json={"dry_run": True},
        )
    finally:
        monkeypatch.undo()
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data.get("dry_run") is True
    assert calls == [True]
    audits = db.query(CodingAgentSuggestionApply).filter(CodingAgentSuggestionApply.suggestion_id == sid).all()
    assert len(audits) == 1
    assert audits[0].dry_run is True
    assert audits[0].status == "completed"


def test_dry_run_check_fail_audit_failed(paired_client, db):
    from app.services.coding_task import snapshot_apply as snap_mod

    client, user = paired_client
    tid, sid = _task_and_snapshot(db, user, ["bad patch"])
    root = Path(__file__).resolve().parents[1]
    calls: list[bool] = []

    def fake_run(cwd, patch, *, check_only: bool):
        calls.append(check_only)
        return (1, "reject")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(snap_mod, "_repo_root_for_task", lambda *a, **k: root)
    monkeypatch.setattr(snap_mod, "_run_git_apply", fake_run)
    try:
        r = client.post(
            f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
            json={"dry_run": True},
        )
    finally:
        monkeypatch.undo()
    assert r.status_code == 400
    assert calls == [True]
    audits = db.query(CodingAgentSuggestionApply).filter(CodingAgentSuggestionApply.suggestion_id == sid).all()
    assert len(audits) == 1
    assert audits[0].dry_run is True
    assert audits[0].status == "failed"


def test_dry_run_viewer_not_found(paired_client, db):
    client, owner = paired_client
    p = PlanProject(user_id=owner.id, name="P19V", key="P19V")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=owner.id, role="owner"))
    viewer = User(name="viewer_p19")
    db.add(viewer)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=viewer.id, role="viewer"))
    t = PlanTask(project_id=p.id, title="t", reporter_id=owner.id)
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
    token = "viewer-device-p19-dry"
    db.add(Device(token=token, user_id=viewer.id, label="v", client_ip_last="127.0.0.1"))
    db.commit()
    client.cookies.set(DEVICE_COOKIE_NAME, token)
    r = client.post(
        f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
        json={"dry_run": True},
    )
    assert r.status_code == 404
