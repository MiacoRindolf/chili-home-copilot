from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.models import PlanProject, PlanTask, ProjectMember
from app.models.code_brain import CodeRepo
from app.models.coding_task import PlanTaskCodingProfile


def test_project_bootstrap_guest_is_read_only(client, db):
    r = client.get("/api/brain/project/bootstrap")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["is_guest"] is True
    assert data["capabilities"]["register_repo"]["enabled"] is False
    assert data["capabilities"]["suggest"]["enabled"] is False
    assert data["capabilities"]["apply"]["enabled"] is False
    assert data["capabilities"]["validate"]["enabled"] is False


def test_project_bootstrap_with_bound_workspace(paired_client, db):
    client, user = paired_client
    repo = CodeRepo(
        user_id=user.id,
        path=str(Path(__file__).resolve().parents[1]),
        name="workspace",
        file_count=12,
        total_lines=345,
        last_indexed=datetime.utcnow(),
        active=True,
    )
    db.add(repo)
    db.flush()

    project = PlanProject(user_id=user.id, name="Proj", key="PRJ")
    db.add(project)
    db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
    task = PlanTask(project_id=project.id, title="Bound task", reporter_id=user.id)
    db.add(task)
    db.flush()
    db.add(
        PlanTaskCodingProfile(
            task_id=task.id,
            code_repo_id=repo.id,
            repo_index=0,
            sub_path="app",
        )
    )
    db.commit()

    r = client.get(f"/api/brain/project/bootstrap?planner_task_id={task.id}")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["workspace"]["repo_count"] == 1
    assert data["workspace"]["indexed_repo_count"] == 1
    assert data["planner_handoff"]["available"] is True
    profile = data["planner_handoff"]["summary"]["profile"]
    assert profile["code_repo_id"] == repo.id
    assert profile["repo_name"] == "workspace"
    assert profile["workspace_bound"] is True
    assert data["capabilities"]["suggest"]["enabled"] is True
    assert data["capabilities"]["apply"]["enabled"] is True
    assert data["capabilities"]["validate"]["enabled"] is True
