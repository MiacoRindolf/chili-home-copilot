from __future__ import annotations

from pathlib import Path

from app.models import PlanProject, PlanTask, ProjectMember
from app.models.code_brain import CodeRepo


def test_put_coding_profile_prefers_code_repo_id_and_exposes_workspace_fields(paired_client, db):
    client, user = paired_client
    repo = CodeRepo(
        user_id=user.id,
        path=str(Path(__file__).resolve().parents[1]),
        name="workspace",
        file_count=1,
        total_lines=10,
        active=True,
    )
    db.add(repo)
    db.flush()

    project = PlanProject(user_id=user.id, name="Profile", key="PRO")
    db.add(project)
    db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
    task = PlanTask(project_id=project.id, title="Bind me", reporter_id=user.id)
    db.add(task)
    db.commit()

    r = client.put(
        f"/api/planner/tasks/{task.id}/coding/profile",
        json={"code_repo_id": repo.id, "sub_path": "app/services"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    profile = data["profile"]
    assert profile["code_repo_id"] == repo.id
    assert profile["repo_name"] == "workspace"
    assert profile["workspace_bound"] is True
    assert profile["sub_path"] == "app/services"
