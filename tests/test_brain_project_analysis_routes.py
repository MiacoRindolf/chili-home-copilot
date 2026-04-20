from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.models import PlanProject, PlanTask, ProjectAnalysisSnapshot, ProjectDomainRun, ProjectMember
from app.models.code_brain import CodeRepo, CodeHotspot, CodeInsight, CodeSearchEntry, CodeSnapshot
from app.services.code_brain.learning import run_code_learning_cycle


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
