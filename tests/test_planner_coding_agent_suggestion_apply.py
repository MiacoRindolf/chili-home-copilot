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


def _record_dry_run_passed(db, *, task_id: int, suggestion_id: int, user_id: int) -> None:
    """Seed a completed dry-run audit so B1's workflow state machine sees
    ``dry_run_passed`` and allows the subsequent real apply.
    """
    db.add(
        CodingAgentSuggestionApply(
            suggestion_id=suggestion_id,
            task_id=task_id,
            user_id=user_id,
            dry_run=True,
            status="completed",
            message="prior dry-run (fixture)",
        )
    )
    db.commit()


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
    _record_dry_run_passed(db, task_id=tid, suggestion_id=sid, user_id=user.id)
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
    _record_dry_run_passed(db, task_id=tid, suggestion_id=sid, user_id=user.id)
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
    audits = (
        db.query(CodingAgentSuggestionApply)
        .filter(
            CodingAgentSuggestionApply.suggestion_id == sid,
            CodingAgentSuggestionApply.dry_run.is_(False),
        )
        .all()
    )
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


def test_apply_blocked_without_prior_dry_run(paired_client, db):
    """B1: real apply is rejected with HTTP 409 when no prior dry-run exists.
    The workflow state machine treats ``apply`` as requiring ``dry_run_passed``.
    """
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
            json={"dry_run": False},
        )
    finally:
        monkeypatch.undo()
    assert r.status_code == 409
    data = r.json()
    assert data.get("workflow_blocked") is True
    assert data.get("action") == "apply"
    assert data.get("required_state") == "dry_run_passed"
    # And no side effects: no completed real-apply audit should have been written.
    real_applies = (
        db.query(CodingAgentSuggestionApply)
        .filter(
            CodingAgentSuggestionApply.suggestion_id == sid,
            CodingAgentSuggestionApply.dry_run.is_(False),
        )
        .count()
    )
    assert real_applies == 0


def test_apply_dry_run_green_then_real_apply_red(paired_client, db):
    """C2: the dry-run succeeds (idempotent check passes), but the real apply
    fails mid-snapshot. Audit trail records both; state does not advance past
    ``dry_run_passed``.
    """
    from app.services.coding_task import snapshot_apply as snap_mod
    from app.services.coding_task.workflow_state import compute_state
    from app.models import PlanTask

    client, user = paired_client
    tid, sid = _task_and_snapshot(db, user, ["--- a\n+++ b\n"])
    root = Path(__file__).resolve().parents[1]

    # First: dry-run is green. ``_run_git_apply`` is called once with
    # ``check_only=True`` and returns 0.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(snap_mod, "_repo_root_for_task", lambda *a, **k: root)
    monkeypatch.setattr(snap_mod, "_run_git_apply", lambda *a, **k: (0, ""))
    try:
        r_dry = client.post(
            f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
            json={"dry_run": True},
        )
    finally:
        monkeypatch.undo()
    assert r_dry.status_code == 200
    assert r_dry.json()["ok"] is True

    # State is now ``dry_run_passed``.
    task = db.query(PlanTask).filter(PlanTask.id == tid).first()
    snap_state = compute_state(db, task, user_id=user.id)
    assert snap_state.state == "dry_run_passed"
    assert snap_state.last_dry_run_passed is True
    assert snap_state.last_real_apply_passed is False

    # Now: real apply fails on the post-check pass (simulating concurrent write).
    # The dry-run's ``_run_git_apply`` is called with check_only=True and returns 0.
    # The real apply calls it twice: check (0) then actual apply (1).
    call_log: list[bool] = []

    def _flaky_apply(cwd, patch, *, check_only: bool):
        call_log.append(check_only)
        if check_only:
            return 0, ""
        return 1, "hunk rejected after concurrent write"

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(snap_mod, "_repo_root_for_task", lambda *a, **k: root)
    monkeypatch.setattr(snap_mod, "_run_git_apply", _flaky_apply)
    try:
        r_real = client.post(
            f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
            json={"dry_run": False},
        )
    finally:
        monkeypatch.undo()
    assert r_real.status_code == 400
    assert call_log == [True, False]
    audits = (
        db.query(CodingAgentSuggestionApply)
        .filter(CodingAgentSuggestionApply.suggestion_id == sid)
        .order_by(CodingAgentSuggestionApply.id.asc())
        .all()
    )
    assert [a.status for a in audits] == ["completed", "failed"]
    assert [a.dry_run for a in audits] == [True, False]
    assert "hunk rejected" in (audits[-1].message or "")

    # State stays at ``dry_run_passed`` — a failed real apply does not advance.
    db.expire_all()
    snap_state_after = compute_state(db, task, user_id=user.id)
    assert snap_state_after.state == "dry_run_passed"
    assert snap_state_after.last_real_apply_passed is False


def test_apply_blocked_when_workspace_unbound_returns_409(paired_client, db):
    """D1 + B1 interaction: if the bound repo is gone, apply returns 409 with
    ``workspace_unbound=True`` and does NOT advance any state — even if a
    prior dry-run succeeded earlier.
    """
    from app.services.coding_task import snapshot_apply as snap_mod

    client, user = paired_client
    tid, sid = _task_and_snapshot(db, user, ["--- a\n+++ b\n"])
    _record_dry_run_passed(db, task_id=tid, suggestion_id=sid, user_id=user.id)
    # Simulate the repo going away by forcing lookup to return None.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(snap_mod, "_repo_root_for_task", lambda *a, **k: None)
    try:
        r = client.post(
            f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}/apply",
            json={"dry_run": False},
        )
    finally:
        monkeypatch.undo()
    assert r.status_code == 409
    data = r.json()
    assert data.get("workspace_unbound") is True
    assert data.get("workspace_reason")