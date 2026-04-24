from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.models import AgentMessage, PlanProject, PlanTask, ProjectAnalysisSnapshot, ProjectDomainRun, ProjectMember, User
from app.models.code_brain import (
    CodeDepAlert,
    CodeHotspot,
    CodeInsight,
    CodeLearningEvent,
    CodeQualitySnapshot,
    CodeRepo,
    CodeReview,
    CodeSearchEntry,
    CodeSnapshot,
)
from app.services.code_brain.learning import get_code_brain_metrics
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


def test_project_messages_and_analysis_merge_operator_feed_sources(paired_client, db):
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
        ProjectDomainRun(
            user_id=user.id,
            repo_id=repo.id,
            run_kind="analysis",
            status="completed",
            title="Run project analysis",
            started_at=datetime(2026, 4, 24, 14, 59, 0),
            finished_at=datetime(2026, 4, 24, 14, 59, 10),
            created_at=datetime(2026, 4, 24, 14, 59, 10),
        )
    )
    db.add(
        CodeLearningEvent(
            user_id=None,
            repo_id=repo.id,
            event_type="index_error",
            description="workspace: Registered workspace is not reachable from this runtime.",
            created_at=datetime(2026, 4, 24, 15, 0, 0),
        )
    )
    db.add(
        AgentMessage(
            from_agent="product_owner",
            to_agent="project_manager",
            user_id=user.id,
            message_type="finding",
            content_json='{"title":"Need auth hardening","severity":"critical"}',
            acknowledged=False,
            created_at=datetime(2026, 4, 24, 15, 1, 0),
        )
    )
    db.add(
        CodeLearningEvent(
            user_id=None,
            repo_id=None,
            event_type="cycle",
            description="Code learning cycle completed in 7.5s: 6 repos, 4 insights",
            created_at=datetime(2026, 4, 24, 15, 2, 0),
        )
    )
    db.commit()

    feed_response = client.get("/api/brain/project/messages")
    assert feed_response.status_code == 200
    messages = feed_response.json()["messages"]
    assert [row["source"] for row in messages[:4]] == [
        "code_learning",
        "agent_message",
        "code_learning",
        "project_run",
    ]
    assert messages[0]["summary"] == "Code learning cycle completed."
    assert messages[1]["summary"] == "Need auth hardening"
    assert messages[1]["status"] == "unread"
    assert messages[2]["status"] == "failed"
    assert messages[2]["repo_id"] == repo.id
    assert messages[3]["summary"] == "Run project analysis"

    latest_response = client.get("/api/brain/project/analysis/latest")
    assert latest_response.status_code == 200
    snapshot = latest_response.json()["snapshot"]
    assert snapshot["status"] == "ephemeral"
    assert snapshot["summary"]["timeline_count"] == 4
    assert snapshot["timeline"][0]["source"] == "code_learning"


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
        language_stats='{"python": 12}',
        framework_tags="fastapi",
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
    db.add(CodeDepAlert(repo_id=repo.id, package_name="fastapi", severity="warn", resolved=False))
    db.add(CodeReview(repo_id=repo.id, commit_hash="abc123", summary="stale review"))
    db.add(
        CodeQualitySnapshot(
            repo_id=repo.id,
            total_files=12,
            total_lines=300,
            avg_complexity=1.2,
            hotspot_count=1,
            insight_count=1,
        )
    )
    db.commit()

    result = run_code_learning_cycle(db)
    assert result["ok"] is True

    db.refresh(repo)
    assert repo.file_count == 0
    assert repo.last_index_error
    assert repo.language_stats is None
    assert repo.framework_tags is None
    assert db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo.id).count() == 0
    assert db.query(CodeHotspot).filter(CodeHotspot.repo_id == repo.id).count() == 0
    assert db.query(CodeSearchEntry).filter(CodeSearchEntry.repo_id == repo.id).count() == 0
    assert db.query(CodeDepAlert).filter(CodeDepAlert.repo_id == repo.id).count() == 0
    assert db.query(CodeReview).filter(CodeReview.repo_id == repo.id).count() == 0
    assert db.query(CodeQualitySnapshot).filter(CodeQualitySnapshot.repo_id == repo.id).count() == 0
    insight = db.query(CodeInsight).filter(CodeInsight.repo_id == repo.id).first()
    assert insight is not None
    assert insight.active is False


def test_code_learning_scheduler_owned_repo_rows_are_user_scoped(db):
    user = User(name="SchedulerOwner")
    db.add(user)
    db.flush()
    repo = CodeRepo(
        user_id=user.id,
        path="Z:\\missing-owned-project",
        host_path="Z:\\missing-owned-project",
        name="owned-missing",
        active=True,
    )
    db.add(repo)
    db.commit()

    result = run_code_learning_cycle(db)
    assert result["ok"] is True

    run_rows = (
        db.query(ProjectDomainRun)
        .filter(ProjectDomainRun.repo_id == repo.id, ProjectDomainRun.run_kind == "index")
        .all()
    )
    assert len(run_rows) == 1
    assert run_rows[0].user_id == user.id

    repo_events = (
        db.query(CodeLearningEvent)
        .filter(CodeLearningEvent.repo_id == repo.id, CodeLearningEvent.event_type == "index_error")
        .all()
    )
    assert len(repo_events) == 1
    assert repo_events[0].user_id == user.id

    cycle_events = db.query(CodeLearningEvent).filter(
        CodeLearningEvent.repo_id.is_(None),
        CodeLearningEvent.event_type == "cycle",
    ).all()
    assert len(cycle_events) == 1
    assert cycle_events[0].user_id == user.id


def test_code_metrics_recent_events_exclude_foreign_cycle_rows(paired_client, db):
    _client, user = paired_client
    other = User(name="OtherMetricsUser")
    db.add(other)
    db.flush()
    repo = CodeRepo(
        user_id=user.id,
        path=str(Path(__file__).resolve().parents[1]),
        host_path=str(Path(__file__).resolve().parents[1]),
        container_path="/workspace",
        name="workspace",
        file_count=4,
        total_lines=40,
        last_indexed=datetime.utcnow(),
        active=True,
    )
    db.add(repo)
    db.flush()
    db.add(
        CodeLearningEvent(
            user_id=other.id,
            repo_id=None,
            event_type="cycle",
            description="Other user cycle",
            created_at=datetime(2026, 4, 24, 17, 0, 0),
        )
    )
    db.add(
        CodeLearningEvent(
            user_id=user.id,
            repo_id=None,
            event_type="cycle",
            description="Current user cycle",
            created_at=datetime(2026, 4, 24, 17, 1, 0),
        )
    )
    db.commit()

    metrics = get_code_brain_metrics(db, user.id)
    events = metrics["recent_events"]
    descriptions = [row["description"] for row in events]
    assert "Current user cycle" in descriptions
    assert "Other user cycle" not in descriptions


def test_project_router_requires_pairing(client, db):
    response = client.get("/api/brain/project/status")
    assert response.status_code == 403
    detail = response.json().get("detail") or {}
    assert "Pair this device" in (detail.get("message") or "")


def test_project_status_and_latest_analysis_exclude_global_rows(paired_client, db):
    client, user = paired_client
    global_repo = CodeRepo(
        user_id=None,
        path=str(Path(__file__).resolve().parents[1]),
        host_path=str(Path(__file__).resolve().parents[1]),
        container_path="/workspace",
        name="global-snapshot-repo",
        active=True,
    )
    db.add(global_repo)
    db.flush()
    db.add(
        ProjectDomainRun(
            user_id=None,
            run_kind="analysis",
            status="completed",
            title="Global run",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
    )
    db.add(
        ProjectAnalysisSnapshot(
            user_id=None,
            task_id=None,
            repo_id=global_repo.id,
            status="completed",
            summary_json=f'{{"repo_id": {global_repo.id}}}',
            perspectives_json="{}",
            timeline_json="[]",
        )
    )
    db.commit()

    status_response = client.get("/api/brain/project/status")
    assert status_response.status_code == 200
    status_body = status_response.json()
    assert status_body["last_run"] is None
    assert status_body["run_kind"] is None

    latest_response = client.get("/api/brain/project/analysis/latest")
    assert latest_response.status_code == 200
    latest_body = latest_response.json()
    snapshot = latest_body["snapshot"]
    assert snapshot["status"] == "ephemeral"
    assert snapshot["summary"]["repo_id"] == global_repo.id
    assert snapshot["summary"]["repo_source"] == "reachable_fallback"


def test_code_routes_exclude_foreign_repo_data(paired_client, db):
    client, user = paired_client
    other = User(name="OtherBrainUser")
    db.add(other)
    db.flush()

    own_repo = CodeRepo(
        user_id=user.id,
        path=str(Path(__file__).resolve().parents[1]),
        host_path=str(Path(__file__).resolve().parents[1]),
        container_path="/workspace",
        name="own-repo",
        active=True,
    )
    foreign_repo = CodeRepo(
        user_id=other.id,
        path=str(Path(__file__).resolve().parents[1] / "foreign-missing"),
        host_path=str(Path(__file__).resolve().parents[1] / "foreign-missing"),
        name="foreign-repo",
        active=True,
    )
    db.add_all([own_repo, foreign_repo])
    db.flush()
    db.add(CodeHotspot(repo_id=own_repo.id, file_path="app/own.py", churn_score=0.7, complexity_score=0.6, combined_score=0.65))
    db.add(CodeHotspot(repo_id=foreign_repo.id, file_path="app/foreign.py", churn_score=0.9, complexity_score=0.9, combined_score=0.81))
    db.add(CodeSearchEntry(repo_id=own_repo.id, file_path="app/own.py", symbol_name="own_fn", symbol_type="function"))
    db.add(CodeSearchEntry(repo_id=foreign_repo.id, file_path="app/foreign.py", symbol_name="foreign_fn", symbol_type="function"))
    db.commit()

    hotspots_response = client.get("/api/brain/code/hotspots")
    assert hotspots_response.status_code == 200
    hotspot_files = [row["file"] for row in hotspots_response.json()["hotspots"]]
    assert "app/own.py" in hotspot_files
    assert "app/foreign.py" not in hotspot_files

    search_response = client.post("/api/brain/code/search", json={"query": "fn"})
    assert search_response.status_code == 200
    result_files = [row["file"] for row in search_response.json()["results"]]
    assert "app/own.py" in result_files
    assert "app/foreign.py" not in result_files

    foreign_graph = client.get(f"/api/brain/code/graph?repo_id={foreign_repo.id}")
    assert foreign_graph.status_code == 404
