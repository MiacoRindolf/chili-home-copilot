from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.models import AgentMessage, CodeLearningEvent, PlanProject, PlanTask, ProjectMember
from app.models.code_brain import CodeRepo
from app.models.coding_task import PlanTaskCodingProfile
import app.services.coding_task.workspaces as workspace_mod


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
    assert data["workspace"]["selected_repo"]["id"] == repo.id
    assert data["workspace"]["selected_repo"]["source"] == "task_bound"
    assert data["planner_handoff"]["available"] is True
    profile = data["planner_handoff"]["summary"]["profile"]
    assert profile["code_repo_id"] == repo.id
    assert profile["repo_name"] == "workspace"
    assert profile["workspace_bound"] is True
    assert data["capabilities"]["suggest"]["enabled"] is True
    assert data["capabilities"]["apply"]["enabled"] is True
    assert data["capabilities"]["validate"]["enabled"] is True


def test_project_bootstrap_feed_merges_activity_sources_and_unread_count(paired_client, db):
    client, user = paired_client
    repo = CodeRepo(
        user_id=user.id,
        path=str(Path(__file__).resolve().parents[1]),
        host_path=str(Path(__file__).resolve().parents[1]),
        container_path="/workspace",
        name="workspace",
        active=True,
    )
    db.add(repo)
    db.flush()
    db.add(
        CodeLearningEvent(
            user_id=None,
            repo_id=None,
            event_type="cycle",
            description="Code learning cycle completed in 9.9s: 5 repos, 3 insights",
            created_at=datetime(2026, 4, 24, 15, 2, 0),
        )
    )
    db.add(
        AgentMessage(
            from_agent="qa",
            to_agent="project_manager",
            user_id=user.id,
            message_type="cycle_summary",
            content_json='{"type":"cycle_complete","test_cases":4,"bugs":2,"confidence":0.81}',
            acknowledged=False,
            created_at=datetime(2026, 4, 24, 15, 3, 0),
        )
    )
    db.commit()

    response = client.get("/api/brain/project/bootstrap")
    assert response.status_code == 200
    data = response.json()
    timeline = data["feed"]["timeline"]
    assert data["agents"]["unread_messages"] == 1
    assert data["feed"]["recent_count"] == 2
    assert [row["source"] for row in timeline[:2]] == ["agent_message", "code_learning"]
    assert timeline[0]["status"] == "unread"
    assert "QA cycle" in timeline[0]["summary"]
    assert timeline[1]["summary"] == "Code learning cycle completed."


def _seed_task_with_profile(
    db,
    user,
    *,
    repo: CodeRepo | None = None,
    repo_index: int = 0,
    sub_path: str = "",
) -> PlanTask:
    project = PlanProject(user_id=user.id, name="Proj", key="PRJ")
    db.add(project)
    db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
    task = PlanTask(project_id=project.id, title="T", reporter_id=user.id)
    db.add(task)
    db.flush()
    db.add(
        PlanTaskCodingProfile(
            task_id=task.id,
            code_repo_id=repo.id if repo is not None else None,
            repo_index=repo_index,
            sub_path=sub_path,
        )
    )
    db.commit()
    return task


def test_project_bootstrap_fails_closed_when_code_repo_deleted(paired_client, db):
    """FK uses SET NULL, but the bootstrap must still report workspace_bound=False
    and disable suggest/apply/validate when the referenced CodeRepo is gone.
    """
    client, user = paired_client
    repo = CodeRepo(
        user_id=user.id,
        path=str(Path(__file__).resolve().parents[1]),
        name="to-be-deleted",
        file_count=5,
        total_lines=50,
        last_indexed=datetime.utcnow(),
        active=True,
    )
    db.add(repo)
    db.flush()
    task = _seed_task_with_profile(db, user, repo=repo, repo_index=0)

    # Simulate a code repo deletion (FK SET NULL would null out code_repo_id;
    # here we delete and also null the profile to mirror the observed state).
    from app.models.coding_task import PlanTaskCodingProfile as _Profile

    db.query(_Profile).filter(_Profile.task_id == task.id).update(
        {_Profile.code_repo_id: None}
    )
    db.query(CodeRepo).filter(CodeRepo.id == repo.id).delete()
    db.commit()

    r = client.get(f"/api/brain/project/bootstrap?planner_task_id={task.id}")
    assert r.status_code == 200
    data = r.json()
    profile = data["planner_handoff"]["summary"]["profile"]
    assert profile["workspace_bound"] is False
    assert data["capabilities"]["suggest"]["enabled"] is False
    assert data["capabilities"]["apply"]["enabled"] is False
    assert data["capabilities"]["validate"]["enabled"] is False
    # Reason must be surfaced so the UI can show the operator what to do.
    ops_hints = data["planner_handoff"]["summary"]["ops_hints"]
    assert ops_hints["workspace_bound"] is False
    assert ops_hints["workspace_reason"]


def test_project_bootstrap_fails_closed_when_code_repo_inactive(paired_client, db):
    """Marking a repo ``active=False`` must flip the task to unbound; we do not
    silently fall back to some previously-matched repo.
    """
    client, user = paired_client
    repo = CodeRepo(
        user_id=user.id,
        path=str(Path(__file__).resolve().parents[1]),
        name="inactive",
        file_count=5,
        total_lines=50,
        last_indexed=datetime.utcnow(),
        active=True,
    )
    db.add(repo)
    db.flush()
    task = _seed_task_with_profile(db, user, repo=repo, repo_index=0)

    repo.active = False
    db.commit()

    r = client.get(f"/api/brain/project/bootstrap?planner_task_id={task.id}")
    assert r.status_code == 200
    data = r.json()
    profile = data["planner_handoff"]["summary"]["profile"]
    assert profile["workspace_bound"] is False
    assert data["capabilities"]["suggest"]["enabled"] is False
    assert data["capabilities"]["apply"]["enabled"] is False
    assert data["capabilities"]["validate"]["enabled"] is False


def test_project_bootstrap_legacy_repo_index_without_code_repo(paired_client, db):
    """Profile with only legacy repo_index (no code_repo_id) is treated as unbound
    at the capability layer — legacy fallback is for path resolution, not write enablement.
    """
    client, user = paired_client
    task = _seed_task_with_profile(db, user, repo=None, repo_index=0)

    r = client.get(f"/api/brain/project/bootstrap?planner_task_id={task.id}")
    assert r.status_code == 200
    data = r.json()
    profile = data["planner_handoff"]["summary"]["profile"]
    assert profile["workspace_bound"] is False
    assert data["capabilities"]["suggest"]["enabled"] is False
    assert data["capabilities"]["apply"]["enabled"] is False
    assert data["capabilities"]["validate"]["enabled"] is False


def test_project_bootstrap_legacy_repo_index_matching_repo_stays_read_only(paired_client, db, monkeypatch):
    client, user = paired_client
    repo_root = Path(__file__).resolve().parents[1]
    repo = CodeRepo(
        user_id=user.id,
        path=str(repo_root),
        host_path=str(repo_root),
        container_path="/workspace",
        name="legacy-match",
        file_count=9,
        total_lines=90,
        last_indexed=datetime.utcnow(),
        active=True,
    )
    db.add(repo)
    db.flush()
    task = _seed_task_with_profile(db, user, repo=None, repo_index=0)
    monkeypatch.setattr(workspace_mod, "list_workspace_roots", lambda: [repo_root])

    r = client.get(f"/api/brain/project/bootstrap?planner_task_id={task.id}")
    assert r.status_code == 200
    data = r.json()
    profile = data["planner_handoff"]["summary"]["profile"]
    assert profile["workspace_bound"] is False
    assert profile["code_repo_id"] is None
    assert data["workspace"]["selected_repo"]["id"] == repo.id
    assert data["workspace"]["selected_repo"]["source"] == "legacy_profile_fallback"
    assert "legacy repo_index binding" in (data["workspace"]["selected_repo"]["reason"] or "")
    assert data["capabilities"]["suggest"]["enabled"] is False
    assert data["capabilities"]["apply"]["enabled"] is False
    assert data["capabilities"]["validate"]["enabled"] is False


def test_project_bootstrap_disabled_by_feature_flag(client, db, monkeypatch):
    """Kill switch: when ``settings.project_domain_enabled`` is False the
    bootstrap endpoint must return 503 so the front end fails closed without a redeploy.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "project_domain_enabled", False)
    r = client.get("/api/brain/project/bootstrap")
    assert r.status_code == 503
    payload = r.json()
    detail = payload.get("detail") if isinstance(payload, dict) else None
    assert isinstance(detail, dict)
    assert detail.get("disabled") is True
    assert detail.get("domain") == "project"


def test_project_bootstrap_selects_reachable_fallback_when_bound_repo_is_unreachable(paired_client, db):
    client, user = paired_client
    unreachable_repo = CodeRepo(
        user_id=user.id,
        path="Z:\\brain-missing-bound",
        host_path="Z:\\brain-missing-bound",
        name="bound-missing",
        active=True,
    )
    reachable_repo = CodeRepo(
        user_id=user.id,
        path=str(Path(__file__).resolve().parents[1]),
        host_path=str(Path(__file__).resolve().parents[1]),
        container_path="/workspace",
        name="reachable-workspace",
        file_count=7,
        total_lines=70,
        last_indexed=datetime.utcnow(),
        active=True,
    )
    db.add_all([unreachable_repo, reachable_repo])
    db.flush()
    task = _seed_task_with_profile(db, user, repo=unreachable_repo, repo_index=0)

    r = client.get(f"/api/brain/project/bootstrap?planner_task_id={task.id}")
    assert r.status_code == 200
    data = r.json()
    selected_repo = data["workspace"]["selected_repo"]
    assert selected_repo["id"] == reachable_repo.id
    assert selected_repo["source"] == "reachable_fallback"
    assert "first reachable workspace" in (selected_repo["reason"] or "")

    analysis = client.get(f"/api/brain/project/analysis/latest?planner_task_id={task.id}")
    assert analysis.status_code == 200
    snapshot = analysis.json()["snapshot"]
    assert snapshot["summary"]["repo_id"] == reachable_repo.id
    assert snapshot["summary"]["repo_source"] == "reachable_fallback"
