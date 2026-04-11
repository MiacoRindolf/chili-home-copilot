"""Phase 3: post-validation summary visibility + read-path idempotence (no new API surface)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text

from app.models import PlanProject, PlanTask, ProjectMember
from app.models.code_brain import CodeRepo
from app.services.coding_task.validator_runner import StepResult


def _fake_ok_steps() -> list[StepResult]:
    keys = ("ast_syntax", "ruff_check", "pytest_collect", "git_status", "git_diff_stat")
    return [
        StepResult(k, 0, False, "", "", False, None)
        for k in keys
    ]


def test_post_validation_summary_shows_run_and_read_path_idempotent(paired_client, db, tmp_path: Path):
    """After POST validation, summary lists the run; two GET summaries do not mutate stored state."""
    tmp_path = tmp_path.resolve()
    (tmp_path / ".git").mkdir()
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P3Vis", key="P3V")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Vis task", reporter_id=user.id)
    db.add(t)
    db.add(CodeRepo(user_id=user.id, path=str(tmp_path), name="workspace", active=True))
    db.commit()
    tid = t.id

    client.put(f"/api/planner/tasks/{tid}/coding/brief", json={"body": "Brief for validation."})
    client.post(f"/api/planner/tasks/{tid}/coding/brief/approve")

    with patch("app.services.coding_task.envelope.list_code_repo_roots", return_value=[tmp_path]):
        with patch("app.services.coding_task.service.run_phase1_validation", return_value=_fake_ok_steps()):
            r = client.post(f"/api/planner/tasks/{tid}/coding/validation/run")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    run_id = body["run"]["id"]
    assert body["coding_readiness_state"] == "ready_for_future_impl"

    s1 = client.get(f"/api/planner/tasks/{tid}/coding/summary")
    assert s1.status_code == 200
    runs = s1.json()["summary"]["validation_runs"]
    assert runs and runs[0]["id"] == run_id

    st_before = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id=:i"),
        {"i": tid},
    ).scalar()
    client.get(f"/api/planner/tasks/{tid}/coding/summary")
    client.get(f"/api/planner/tasks/{tid}/coding/summary")
    st_after = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id=:i"),
        {"i": tid},
    ).scalar()
    assert st_before == st_after == "ready_for_future_impl"

    rd = client.get(f"/api/planner/tasks/{tid}/coding/validation/runs/{run_id}")
    assert rd.status_code == 200
    assert rd.json()["ok"] is True
    assert rd.json()["run"]["id"] == run_id
    assert len(rd.json()["run"]["artifacts"]) >= 1


def test_validation_preflight_error_then_summary_still_idempotent(paired_client, db):
    """400 validation (no repos) does not break; GET summary remains idempotent."""
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P3Pre", key="P3P")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Pre task", reporter_id=user.id)
    db.add(t)
    db.commit()
    tid = t.id

    client.put(f"/api/planner/tasks/{tid}/coding/brief", json={"body": "Brief."})
    client.post(f"/api/planner/tasks/{tid}/coding/brief/approve")

    r = client.post(f"/api/planner/tasks/{tid}/coding/validation/run")
    assert r.status_code == 400

    st0 = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id=:i"),
        {"i": tid},
    ).scalar()
    client.get(f"/api/planner/tasks/{tid}/coding/summary")
    client.get(f"/api/planner/tasks/{tid}/coding/summary")
    st1 = db.execute(
        text("SELECT coding_readiness_state FROM plan_tasks WHERE id=:i"),
        {"i": tid},
    ).scalar()
    assert st0 == st1
