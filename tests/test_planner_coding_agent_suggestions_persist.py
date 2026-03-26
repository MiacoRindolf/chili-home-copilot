"""Phase 16: explicit save / metadata list / bounded detail for agent-suggest snapshots."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.models import PlanProject, PlanTask, ProjectMember, User
from app.models.coding_task import CodingAgentSuggestion


def _payload():
    return {
        "response": "## Analysis\nok",
        "model": "mock-model",
        "diffs": ["--- a\n+++ b\n"],
        "files_changed": ["x.py"],
        "validation": [{"file": "x.py", "valid": True, "warnings": []}],
        "context_used": {"repos": 1, "insights": 0, "hotspots": 0, "relevant_files": 0, "files_in_plan": 0},
    }


def test_agent_suggest_does_not_auto_persist_snapshot(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="P16A", key="P16A")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="T", reporter_id=user.id)
    db.add(t)
    db.commit()
    tid = t.id
    fake = _payload()
    with patch(
        "app.routers.planner_coding.run_agent_suggest_for_task",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = fake
        r = client.post(f"/api/planner/tasks/{tid}/coding/agent-suggest", json={})
    assert r.status_code == 200
    n = db.query(CodingAgentSuggestion).filter(CodingAgentSuggestion.task_id == tid).count()
    assert n == 0


def test_save_suggestion_forbidden_guest(client, db):
    u = User(name="Ug")
    db.add(u)
    db.commit()
    db.refresh(u)
    p = PlanProject(user_id=u.id, name="G", key="G")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=u.id, role="owner"))
    t = PlanTask(project_id=p.id, title="T", reporter_id=u.id)
    db.add(t)
    db.commit()
    r = client.post(f"/api/planner/tasks/{t.id}/coding/agent-suggestions", json=_payload())
    assert r.status_code == 403


def test_save_suggestion_not_found(paired_client, db):
    client, user = paired_client
    r = client.post("/api/planner/tasks/999999/coding/agent-suggestions", json=_payload())
    assert r.status_code == 404


def test_save_suggestion_extra_key_422(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="EX", key="EX")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="T", reporter_id=user.id)
    db.add(t)
    db.commit()
    body = _payload()
    body["evil"] = 1
    r = client.post(f"/api/planner/tasks/{t.id}/coding/agent-suggestions", json=body)
    assert r.status_code == 422


def test_save_list_detail_roundtrip(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="RT", key="RT")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="T", reporter_id=user.id)
    db.add(t)
    db.commit()
    tid = t.id
    r = client.post(f"/api/planner/tasks/{tid}/coding/agent-suggestions", json=_payload())
    assert r.status_code == 201
    data = r.json()
    assert data["ok"] is True
    sid = data["id"]

    lr = client.get(f"/api/planner/tasks/{tid}/coding/agent-suggestions")
    assert lr.status_code == 200
    lj = lr.json()
    assert lj["ok"] is True
    assert len(lj["suggestions"]) == 1
    meta = lj["suggestions"][0]
    assert meta["id"] == sid
    assert meta["model"] == "mock-model"
    assert meta["diffs_count"] == 1
    assert meta["files_changed_count"] == 1
    assert "response" not in meta

    dr = client.get(f"/api/planner/tasks/{tid}/coding/agent-suggestions/{sid}")
    assert dr.status_code == 200
    dj = dr.json()
    s = dj["suggestion"]
    assert s["model"] == "mock-model"
    assert s["response"] == "## Analysis\nok"
    assert s["diffs"] == ["--- a\n+++ b\n"]
    assert s["files_changed"] == ["x.py"]
    assert s["validation"][0]["file"] == "x.py"
    assert s["context_used"]["repos"] == 1


def test_detail_wrong_id_404(paired_client, db):
    client, user = paired_client
    p = PlanProject(user_id=user.id, name="NF", key="NF")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="T", reporter_id=user.id)
    db.add(t)
    db.commit()
    tid = t.id
    r = client.get(f"/api/planner/tasks/{tid}/coding/agent-suggestions/999999")
    assert r.status_code == 404
