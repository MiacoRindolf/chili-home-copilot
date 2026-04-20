from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.models import PlanProject, PlanTask, ProjectAnalysisSnapshot, ProjectDomainRun, ProjectMember, User
from app.models.code_brain import CodeRepo, CodeHotspot, CodeInsight, CodeSearchEntry, CodeSnapshot
from app.models.project_brain import POQuestion
from app.services.code_brain.learning import run_code_learning_cycle


def test_project_router_requires_pairing(client, db):
    response = client.get("/api/brain/project/status")
    assert response.status_code == 403
    body = response.json()
    assert body["detail"]["message"] == "Pair this device to use the project workspace."


def test_project_status_reads_durable_runs(paired_client, db):
    client, user = paired_client
    db.add(
        ProjectDomainRun(
            user_id=user.id,
            run_kind="analysis",
            status="completed",
            title="Run project analysis",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
    )
    db.commit()

    response = client.get("/api/brain/project/status")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["running"] is False
    assert body["run_kind"] == "analysis"
    assert body["last_run"] is not None


def test_project_analysis_run_and_latest_snapshot(paired_client, db):
    client, user = paired_client
    repo = CodeRepo(
        user_id=user.id,
        path=str(Path(__file__).resolve().parents[1]),
        host_path=str(Path(__file__).resolve().parents[1]),
        container_path="/workspace",
        name="workspace",
        file_count=8,
        total_lines=120,
        last_indexed=datetime.utcnow(),
        last_successful_indexed_at=datetime.utcnow(),
        last_successful_file_count=8,
        active=True,
    )
    db.add(repo)
    db.flush()
    db.add(CodeSnapshot(repo_id=repo.id, file_path="app/main.py", language="python", line_count=30))
    db.add(CodeHotspot(repo_id=repo.id, file_path="app/main.py", churn_score=0.8, complexity_score=0.7, combined_score=0.75))
    db.add(CodeInsight(repo_id=repo.id, category="architecture", description="Router-service split", confidence=0.8, active=True))
    db.add(CodeSearchEntry(repo_id=repo.id, file_path="app/main.py", symbol_name="app", symbol_type="function"))

    project = PlanProject(user_id=user.id, name="Proj", key="PRJ")
    db.add(project)
    db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
    task = PlanTask(project_id=project.id, title="Bound task", reporter_id=user.id)
    db.add(task)
    db.commit()

    response = client.post(
        "/api/brain/project/analysis/run",
        json={"planner_task_id": task.id},
    )
    assert response.status_code == 200
    body = response.json()
    snapshot = body["snapshot"]
    assert set(snapshot["perspectives"].keys()) == {
        "product",
        "architecture",
        "backend",
        "frontend",
        "qa",
        "security",
        "ops",
        "ai",
    }
    assert db.query(ProjectAnalysisSnapshot).count() == 1

    latest = client.get(f"/api/brain/project/analysis/latest?planner_task_id={task.id}")
    assert latest.status_code == 200
    latest_body = latest.json()
    assert latest_body["snapshot"]["summary"]["planner_task_id"] == task.id


def test_project_status_excludes_global_runs_for_paired_user(paired_client, db):
    client, user = paired_client
    db.add(
        ProjectDomainRun(
            user_id=None,
            run_kind="analysis",
            status="completed",
            title="Global analysis",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
    )
    db.add(
        ProjectDomainRun(
            user_id=user.id,
            run_kind="analysis",
            status="completed",
            title="User analysis",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
    )
    db.commit()

    response = client.get("/api/brain/project/messages")
    assert response.status_code == 200
    body = response.json()
    assert [msg["summary"] for msg in body["messages"]] == ["User analysis"]


def test_analysis_latest_ignores_global_snapshot_for_paired_user(paired_client, db):
    client, _user = paired_client
    db.add(
        ProjectAnalysisSnapshot(
            user_id=None,
            status="completed",
            summary_json='{"repo_id": 999}',
            perspectives_json="{}",
            timeline_json="[]",
        )
    )
    db.commit()

    response = client.get("/api/brain/project/analysis/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["status"] == "ephemeral"


def test_code_hotspots_and_search_exclude_foreign_repos(paired_client, db):
    client, user = paired_client
    other_user = User(name="OtherUser")
    db.add(other_user)
    db.flush()

    owned_repo = CodeRepo(user_id=user.id, path="C:/owned", host_path="C:/owned", name="owned", active=True)
    shared_repo = CodeRepo(user_id=None, path="C:/shared", host_path="C:/shared", name="shared", active=True)
    foreign_repo = CodeRepo(user_id=other_user.id, path="C:/foreign", host_path="C:/foreign", name="foreign", active=True)
    db.add_all([owned_repo, shared_repo, foreign_repo])
    db.flush()
    db.add_all(
        [
            CodeHotspot(repo_id=owned_repo.id, file_path="app/owned.py", churn_score=0.8, complexity_score=0.7, combined_score=0.75),
            CodeHotspot(repo_id=shared_repo.id, file_path="app/shared.py", churn_score=0.7, complexity_score=0.6, combined_score=0.65),
            CodeHotspot(repo_id=foreign_repo.id, file_path="app/foreign.py", churn_score=0.9, complexity_score=0.8, combined_score=0.85),
            CodeSearchEntry(repo_id=owned_repo.id, file_path="app/owned.py", symbol_name="owned_symbol", symbol_type="function"),
            CodeSearchEntry(repo_id=shared_repo.id, file_path="app/shared.py", symbol_name="shared_symbol", symbol_type="function"),
            CodeSearchEntry(repo_id=foreign_repo.id, file_path="app/foreign.py", symbol_name="foreign_symbol", symbol_type="function"),
        ]
    )
    db.commit()

    hotspots = client.get("/api/brain/code/hotspots")
    assert hotspots.status_code == 200
    hotspot_repo_ids = {row["repo_id"] for row in hotspots.json()["hotspots"]}
    assert owned_repo.id in hotspot_repo_ids
    assert shared_repo.id in hotspot_repo_ids
    assert foreign_repo.id not in hotspot_repo_ids

    search = client.post("/api/brain/code/search", json={"query": "symbol"})
    assert search.status_code == 200
    files = {row["file"] for row in search.json()["results"]}
    assert "app/owned.py" in files
    assert "app/shared.py" in files
    assert "app/foreign.py" not in files


def test_code_repo_specific_route_rejects_foreign_repo(paired_client, db):
    client, user = paired_client
    other_user = User(name="OtherUser")
    db.add(other_user)
    db.flush()
    foreign_repo = CodeRepo(user_id=other_user.id, path="C:/foreign", host_path="C:/foreign", name="foreign", active=True)
    owned_repo = CodeRepo(user_id=user.id, path="C:/owned", host_path="C:/owned", name="owned", active=True)
    db.add_all([foreign_repo, owned_repo])
    db.commit()

    response = client.get(f"/api/brain/code/graph?repo_id={foreign_repo.id}")
    assert response.status_code == 404
    assert response.json()["message"] == "Repo not found"


def test_planner_projects_route_only_lists_visible_projects(paired_client, db):
    client, user = paired_client
    other_user = User(name="OtherUser")
    db.add(other_user)
    db.flush()

    visible_project = PlanProject(user_id=user.id, name="Visible", key="VIS")
    hidden_project = PlanProject(user_id=other_user.id, name="Hidden", key="HID")
    db.add_all([visible_project, hidden_project])
    db.flush()
    db.add_all(
        [
            ProjectMember(project_id=visible_project.id, user_id=user.id, role="owner"),
            ProjectMember(project_id=hidden_project.id, user_id=other_user.id, role="owner"),
        ]
    )
    db.commit()

    response = client.get("/api/brain/project/planner-projects")
    assert response.status_code == 200
    projects = response.json()["projects"]
    assert [project["id"] for project in projects] == [visible_project.id]


def test_po_answer_rejects_foreign_question(paired_client, db):
    client, _user = paired_client
    other_user = User(name="OtherUser")
    db.add(other_user)
    db.flush()
    question = POQuestion(user_id=other_user.id, question="What should we build?", category="vision", priority=9)
    db.add(question)
    db.commit()

    response = client.post(
        f"/api/brain/project/agent/product_owner/question/{question.id}/answer",
        json={"answer": "A hidden answer"},
    )
    assert response.status_code == 404
    db.refresh(question)
    assert question.status == "pending"
    assert question.answer is None


def test_register_repo_sets_runtime_fields(paired_client, db):
    client, _user = paired_client
    response = client.post(
        "/api/brain/code/repos",
        json={"path": str(Path(__file__).resolve().parents[1]), "name": "workspace"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["host_path"]
    assert body["container_path"] is not None


def test_code_learning_cycle_marks_unreachable_repo_stale(db):
    repo = CodeRepo(
        path="Z:\\missing-project",
        host_path="Z:\\missing-project",
        name="missing",
        file_count=12,
        total_lines=300,
        last_indexed=datetime.utcnow(),
        last_successful_indexed_at=datetime.utcnow(),
        last_successful_file_count=12,
        active=True,
    )
    db.add(repo)
    db.flush()
    db.add(CodeSnapshot(repo_id=repo.id, file_path="old.py", language="python", line_count=10))
    db.add(CodeHotspot(repo_id=repo.id, file_path="old.py", churn_score=0.5, complexity_score=0.4, combined_score=0.45))
    db.add(CodeSearchEntry(repo_id=repo.id, file_path="old.py", symbol_name="old_fn", symbol_type="function"))
    db.add(CodeInsight(repo_id=repo.id, category="quality", description="Stale insight", confidence=0.6, active=True))
    db.commit()

    result = run_code_learning_cycle(db)
    assert result["ok"] is True

    db.refresh(repo)
    assert repo.file_count == 0
    assert repo.last_index_error
    assert db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo.id).count() == 0
    assert db.query(CodeHotspot).filter(CodeHotspot.repo_id == repo.id).count() == 0
    assert db.query(CodeSearchEntry).filter(CodeSearchEntry.repo_id == repo.id).count() == 0
    insight = db.query(CodeInsight).filter(CodeInsight.repo_id == repo.id).first()
    assert insight is not None
    assert insight.active is False
