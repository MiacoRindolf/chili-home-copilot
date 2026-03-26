"""Task-first implementation bridge: POST agent-suggest (schema-free v1)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.models import PlanProject, PlanTask, ProjectMember


def test_agent_suggest_forbidden_guest(client, db):
    from app.models import User

    u = User(name="U1")
    db.add(u)
    db.commit()
    db.refresh(u)
    p = PlanProject(user_id=u.id, name="ASG", key="ASG")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=u.id, role="owner"))
    t = PlanTask(project_id=p.id, title="T", reporter_id=u.id)
    db.add(t)
    db.commit()

    r = client.post(f"/api/planner/tasks/{t.id}/coding/agent-suggest", json={})
    assert r.status_code == 403


def test_agent_suggest_not_found(paired_client, db):
    client, user = paired_client
    r = client.post("/api/planner/tasks/999999/coding/agent-suggest", json={})
    assert r.status_code == 404


def test_agent_suggest_400_repo_resolution(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="ASR", key="ASR")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Repo bridge", reporter_id=user.id)
    db.add(t)
    db.commit()
    tid = t.id

    with patch(
        "app.routers.planner_coding.run_agent_suggest_for_task",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = {
            "error": (
                "No active Code Brain repository matches this task's coding profile (repo_index). "
                "Register an indexed repo whose path matches CHILI's code_brain_repos entry for "
                "that index, under your user, or adjust the task profile."
            )
        }
        r = client.post(f"/api/planner/tasks/{tid}/coding/agent-suggest", json={})
    assert r.status_code == 400
    data = r.json()
    assert data["ok"] is False
    assert "repository" in data["message"].lower() or "repo" in data["message"].lower()


def test_agent_suggest_200_mock_agent(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="AS200", key="AS200")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="Agent task", reporter_id=user.id)
    db.add(t)
    db.commit()
    tid = t.id

    fake = {
        "response": "## Analysis\nok",
        "model": "mock",
        "diffs": [],
        "files_changed": ["a.py"],
        "validation": [],
        "context_used": {"repos": 1, "insights": 0, "hotspots": 0, "relevant_files": 0, "files_in_plan": 0},
    }
    with patch(
        "app.routers.planner_coding.run_agent_suggest_for_task",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = fake
        r = client.post(
            f"/api/planner/tasks/{tid}/coding/agent-suggest",
            json={"extra_instructions": "Focus on tests."},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["response"] == fake["response"]
    assert data["model"] == "mock"
    assert data["files_changed"] == ["a.py"]
    m.assert_awaited_once()
