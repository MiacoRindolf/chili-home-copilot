"""Phase 20: optional trigger_source on POST validation/run; default manual; Brain uses post_apply."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.models import PlanProject, PlanTask, ProjectMember
from app.models.code_brain import CodeRepo
from app.models.coding_task import CodingTaskValidationRun
from app.services.coding_task.validator_runner import StepResult


def _fake_ok_steps() -> list[StepResult]:
    keys = ("ast_syntax", "ruff_check", "pytest_collect", "git_status", "git_diff_stat")
    return [
        StepResult(k, 0, False, "", "", False, None)
        for k in keys
    ]


def _ready_task(db, user):
    p = PlanProject(user_id=user.id, name="P20Trig", key="P20T")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Trig", reporter_id=user.id)
    db.add(t)
    db.commit()
    tid = t.id
    return tid


def _register_workspace(db, user, path: Path):
    repo = CodeRepo(user_id=user.id, path=str(path), name="workspace", active=True)
    db.add(repo)
    db.commit()
    return repo.id


def test_validation_run_post_no_json_defaults_manual_trigger(paired_client, db, tmp_path: Path):
    client, user = paired_client
    tid = _ready_task(db, user)
    tmp_path = tmp_path.resolve()
    (tmp_path / ".git").mkdir()
    repo_id = _register_workspace(db, user, tmp_path)
    client.put(f"/api/planner/tasks/{tid}/coding/profile", json={"code_repo_id": repo_id})
    client.put(f"/api/planner/tasks/{tid}/coding/brief", json={"body": "Brief."})
    client.post(f"/api/planner/tasks/{tid}/coding/brief/approve")

    with patch("app.services.coding_task.envelope.list_code_repo_roots", return_value=[tmp_path]):
        with patch("app.services.coding_task.service.run_phase1_validation", return_value=_fake_ok_steps()):
            r = client.post(f"/api/planner/tasks/{tid}/coding/validation/run")
    assert r.status_code == 200
    rid = r.json()["run"]["id"]
    row = db.query(CodingTaskValidationRun).filter(CodingTaskValidationRun.id == rid).first()
    assert row is not None
    assert row.trigger_source == "manual"


def test_validation_run_post_post_apply_trigger(paired_client, db, tmp_path: Path):
    client, user = paired_client
    tid = _ready_task(db, user)
    tmp_path = tmp_path.resolve()
    (tmp_path / ".git").mkdir()
    repo_id = _register_workspace(db, user, tmp_path)
    client.put(f"/api/planner/tasks/{tid}/coding/profile", json={"code_repo_id": repo_id})
    client.put(f"/api/planner/tasks/{tid}/coding/brief", json={"body": "Brief."})
    client.post(f"/api/planner/tasks/{tid}/coding/brief/approve")

    with patch("app.services.coding_task.envelope.list_code_repo_roots", return_value=[tmp_path]):
        with patch("app.services.coding_task.service.run_phase1_validation", return_value=_fake_ok_steps()):
            r = client.post(
                f"/api/planner/tasks/{tid}/coding/validation/run",
                json={"trigger_source": "post_apply"},
            )
    assert r.status_code == 200
    rid = r.json()["run"]["id"]
    row = db.query(CodingTaskValidationRun).filter(CodingTaskValidationRun.id == rid).first()
    assert row.trigger_source == "post_apply"


def test_validation_run_post_empty_json_manual(paired_client, db, tmp_path: Path):
    client, user = paired_client
    tid = _ready_task(db, user)
    tmp_path = tmp_path.resolve()
    (tmp_path / ".git").mkdir()
    repo_id = _register_workspace(db, user, tmp_path)
    client.put(f"/api/planner/tasks/{tid}/coding/profile", json={"code_repo_id": repo_id})
    client.put(f"/api/planner/tasks/{tid}/coding/brief", json={"body": "Brief."})
    client.post(f"/api/planner/tasks/{tid}/coding/brief/approve")

    with patch("app.services.coding_task.envelope.list_code_repo_roots", return_value=[tmp_path]):
        with patch("app.services.coding_task.service.run_phase1_validation", return_value=_fake_ok_steps()):
            r = client.post(f"/api/planner/tasks/{tid}/coding/validation/run", json={})
    assert r.status_code == 200
    rid = r.json()["run"]["id"]
    row = db.query(CodingTaskValidationRun).filter(CodingTaskValidationRun.id == rid).first()
    assert row.trigger_source == "manual"


def test_validation_run_post_extra_field_422(paired_client, db, tmp_path: Path):
    client, user = paired_client
    tid = _ready_task(db, user)
    tmp_path = tmp_path.resolve()
    (tmp_path / ".git").mkdir()
    client.put(f"/api/planner/tasks/{tid}/coding/brief", json={"body": "Brief."})
    client.post(f"/api/planner/tasks/{tid}/coding/brief/approve")

    r = client.post(
        f"/api/planner/tasks/{tid}/coding/validation/run",
        json={"trigger_source": "manual", "evil": 1},
    )
    assert r.status_code == 422
