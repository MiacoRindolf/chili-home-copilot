from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ProjectAutonomyLease, ProjectAutonomyRun, ProjectDomainRun, User
from app.models.code_brain import CodeRepo
from app.services.project_autonomy import orchestrator


def _sqlite_autonomy_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            CodeRepo.__table__,
            ProjectDomainRun.__table__,
            ProjectAutonomyRun.__table__,
            ProjectAutonomyLease.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def test_select_local_model_prefers_evidence_gated_current_model(monkeypatch):
    monkeypatch.setattr(
        orchestrator.ollama_client,
        "list_models",
        lambda: ["llama3.2:1b", "qwen3:4b", "chili-coder:current"],
    )

    selected = orchestrator.select_local_model()

    assert selected["available"] is True
    assert selected["model"] == "chili-coder:current"


def test_select_local_model_prefers_coder_prefix_before_later_exact(monkeypatch):
    monkeypatch.setattr(
        orchestrator.ollama_client,
        "list_models",
        lambda: ["llama3.2:1b", "qwen2.5-coder:3b-instruct-q8_0"],
    )

    selected = orchestrator.select_local_model()

    assert selected["available"] is True
    assert selected["model"] == "qwen2.5-coder:3b-instruct-q8_0"


def test_select_local_model_recommends_coder_model_when_empty(monkeypatch):
    monkeypatch.setattr(orchestrator.ollama_client, "list_models", lambda: [])

    selected = orchestrator.select_local_model()

    assert selected["available"] is False
    assert selected["model"] is None
    assert "qwen2.5-coder:7b" in selected["recommendation"]


def test_command_policy_allows_repo_scripts_and_blocks_installs(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"lint":"eslint .","test":"vitest","build":"vite build"}}',
        encoding="utf-8",
    )

    assert orchestrator.command_allowed(["npm", "run", "lint"], tmp_path) == (True, None)
    ok, reason = orchestrator.command_allowed(["pip", "install", "pytest"], tmp_path)
    assert ok is False
    assert "require escalation" in reason


def test_file_leases_block_overlapping_autonomy_runs():
    db = _sqlite_autonomy_session()
    repo = CodeRepo(path="C:/tmp/autonomy-test", name="autonomy-test", active=True)
    try:
        db.add(repo)
        db.flush()
        first = ProjectAutonomyRun(
            run_id="pa_first",
            repo_id=repo.id,
            prompt="change router",
            status="running",
            current_stage="implement",
        )
        second = ProjectAutonomyRun(
            run_id="pa_second",
            repo_id=repo.id,
            prompt="change router too",
            status="running",
            current_stage="implement",
        )
        db.add_all([first, second])
        db.commit()

        orchestrator.acquire_file_leases(db, first, repo.id, ["app/routers/brain_project.py"])
        db.commit()

        with pytest.raises(orchestrator.AutonomyBlocked):
            orchestrator.acquire_file_leases(db, second, repo.id, ["app/routers/brain_project.py"])

        orchestrator.release_run_leases(db, first.run_id)
        db.commit()
        leases = orchestrator.acquire_file_leases(db, second, repo.id, ["app/routers/brain_project.py"])
        assert len(leases) == 1
    finally:
        db.close()


def test_agent_lane_assignment_keeps_architect_lead():
    lanes = orchestrator.assign_agent_lanes(
        [
            {"path": "app/routers/brain_project.py"},
            {"path": "app/static/components/brain-project-domain.js"},
            {"path": "tests/test_project_autonomy_service.py"},
        ]
    )

    names = [lane["name"] for lane in lanes]
    assert names[0] == "architect"
    assert {"backend", "frontend", "qa"}.issubset(set(names))


def test_integration_branch_name_avoids_nested_ref_conflicts():
    assert orchestrator.integration_branch_name("pa_abc123") == "project-auto-pa_abc123"
    assert "/" not in orchestrator.integration_branch_name("pa/nested")


def test_heuristic_plan_fallback_uses_desktop_candidates(tmp_path):
    desktop_file = tmp_path / "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
    desktop_file.parent.mkdir(parents=True)
    desktop_file.write_text("// desktop brain screen\n", encoding="utf-8")
    context = {
        "repos": [{"name": "repo", "runtime_path": str(tmp_path)}],
        "insights": [],
        "hotspots": [],
        "relevant_files": [],
    }

    plan = orchestrator._fallback_plan_from_context(
        context,
        tmp_path,
        "find a small enhancement for the desktop app",
        "TimeoutError: timed out",
    )

    assert plan["files"]
    assert plan["files"][0]["path"] == "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
