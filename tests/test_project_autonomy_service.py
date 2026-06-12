from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app import migrations
from app.models import (
    ProjectAutonomyArchitectReview,
    ProjectAutonomyArtifact,
    ProjectAutonomyLease,
    ProjectAutonomyMessage,
    ProjectAutonomyRun,
    ProjectAutonomyStep,
    ProjectDomainRun,
    User,
)
from app.models.code_brain import CodeRepo
from app.services.code_brain import runtime as code_runtime
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
            ProjectAutonomyMessage.__table__,
            ProjectAutonomyStep.__table__,
            ProjectAutonomyArtifact.__table__,
            ProjectAutonomyArchitectReview.__table__,
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


def test_select_local_model_skips_timed_out_model_during_cooldown(monkeypatch):
    orchestrator._MODEL_COOLDOWNS.clear()
    try:
        timed_out_model = "qwen2.5-coder:3b-instruct-q8_0"
        monkeypatch.setattr(
            orchestrator.ollama_client,
            "list_models",
            lambda: [timed_out_model, "qwen3:4b"],
        )

        orchestrator._mark_model_cooldown(timed_out_model, "TimeoutError: timed out")
        selected = orchestrator.select_local_model()

        assert selected["available"] is True
        assert selected["model"] == "qwen3:4b"
        assert timed_out_model in selected["skipped_models"]
        assert selected["skipped_models"][timed_out_model]["reason"] == "The local planning model timed out."
    finally:
        orchestrator._MODEL_COOLDOWNS.clear()


def test_build_local_plan_uses_bounded_warm_ollama_options(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        target = tmp_path / "chili_mobile/lib/src/network/network_error_message.dart"
        target.parent.mkdir(parents=True)
        target.write_text("String userVisibleNetworkError(Object error) => '$error';\n", encoding="utf-8")
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_plan_options",
            repo_id=repo.id,
            prompt="Improve certificate failure messaging in the desktop app",
            status="running",
            current_stage="plan",
        )
        db.add(run)
        db.commit()
        captured = {}
        monkeypatch.setattr(orchestrator, "select_local_model", lambda: {"model": "qwen", "available": True})
        monkeypatch.setattr(
            orchestrator,
            "_gather_context",
            lambda *args, **kwargs: {"repos": [], "insights": [], "hotspots": [], "relevant_files": []},
        )

        def fake_chat(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                ok=True,
                text=(
                    '{"analysis":"ok","files":[{"path":"chili_mobile/lib/src/network/'
                    'network_error_message.dart","action":"modify"}],"notes":""}'
                ),
                latency_ms=1,
                error=None,
            )

        monkeypatch.setattr(orchestrator.ollama_client, "chat", fake_chat)

        plan = orchestrator.build_local_plan(db, run, repo)

        assert plan["files"][0]["path"] == "chili_mobile/lib/src/network/network_error_message.dart"
        assert captured["timeout_sec"] == orchestrator._PLAN_TIMEOUT_SEC
        assert captured["options"]["num_predict"] == orchestrator._PLAN_NUM_PREDICT
        assert captured["options"]["num_ctx"] == orchestrator._PLAN_NUM_CTX
        assert captured["options"]["keep_alive"] == orchestrator._OLLAMA_KEEP_ALIVE
    finally:
        db.close()


def test_build_local_plan_cools_down_model_after_timeout(monkeypatch, tmp_path):
    orchestrator._MODEL_COOLDOWNS.clear()
    db = _sqlite_autonomy_session()
    try:
        timed_out_model = "qwen2.5-coder:3b-instruct-q8_0"
        target = tmp_path / "chili_mobile/lib/src/network/network_error_message.dart"
        target.parent.mkdir(parents=True)
        target.write_text("String userVisibleNetworkError(Object error) => '$error';\n", encoding="utf-8")
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_plan_timeout_cooldown",
            repo_id=repo.id,
            prompt="Improve certificate failure messaging in the desktop app",
            status="running",
            current_stage="plan",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(
            orchestrator.ollama_client,
            "list_models",
            lambda: [timed_out_model, "qwen3:4b"],
        )
        monkeypatch.setattr(
            orchestrator,
            "_gather_context",
            lambda *args, **kwargs: {"repos": [], "insights": [], "hotspots": [], "relevant_files": []},
        )
        monkeypatch.setattr(
            orchestrator.ollama_client,
            "chat",
            lambda *args, **kwargs: SimpleNamespace(
                ok=False,
                text="",
                latency_ms=90001,
                error="TimeoutError: timed out",
            ),
        )

        plan = orchestrator.build_local_plan(db, run, repo)
        selected_after_timeout = orchestrator.select_local_model()

        assert plan["files"][0]["path"] == "chili_mobile/lib/src/network/network_error_message.dart"
        assert timed_out_model in orchestrator._MODEL_COOLDOWNS
        assert selected_after_timeout["model"] == "qwen3:4b"
    finally:
        orchestrator._MODEL_COOLDOWNS.clear()
        db.close()


def test_build_local_plan_uses_fallback_when_all_models_are_cooling_down(monkeypatch, tmp_path):
    orchestrator._MODEL_COOLDOWNS.clear()
    db = _sqlite_autonomy_session()
    try:
        cooled_model = "qwen2.5-coder:3b-instruct-q8_0"
        target = tmp_path / "chili_mobile/lib/src/network/network_error_message.dart"
        target.parent.mkdir(parents=True)
        target.write_text("String userVisibleNetworkError(Object error) => '$error';\n", encoding="utf-8")
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_all_models_cooling",
            repo_id=repo.id,
            prompt="Improve certificate failure messaging in the desktop app",
            status="running",
            current_stage="plan",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(orchestrator.ollama_client, "list_models", lambda: [cooled_model])
        orchestrator._mark_model_cooldown(cooled_model, "TimeoutError: timed out")
        monkeypatch.setattr(
            orchestrator,
            "_gather_context",
            lambda *args, **kwargs: {"repos": [], "insights": [], "hotspots": [], "relevant_files": []},
        )

        plan = orchestrator.build_local_plan(db, run, repo)

        assert plan["files"][0]["path"] == "chili_mobile/lib/src/network/network_error_message.dart"
        artifact = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.run_id == run.run_id, ProjectAutonomyArtifact.name == "heuristic_plan_fallback")
            .one()
        )
        assert artifact.content_json is not None
        assert "qwen2.5-coder:3b-instruct-q8_0" in artifact.content_json
    finally:
        orchestrator._MODEL_COOLDOWNS.clear()
        db.close()


def test_build_local_plan_uses_heuristic_fast_path_for_vague_small_request(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        target = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
        target.parent.mkdir(parents=True)
        target.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_fast_plan",
            repo_id=repo.id,
            prompt="find a small enhancement for the desktop app",
            status="running",
            current_stage="plan",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(orchestrator, "select_local_model", lambda: {"model": "qwen", "available": True})
        monkeypatch.setattr(
            orchestrator,
            "_gather_context",
            lambda *args, **kwargs: {"repos": [], "insights": [], "hotspots": [], "relevant_files": []},
        )
        monkeypatch.setattr(
            orchestrator.ollama_client,
            "chat",
            lambda *args, **kwargs: pytest.fail("vague small requests should not call the planner model"),
        )

        plan = orchestrator.build_local_plan(db, run, repo)

        assert plan["files"][0]["path"] == "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
        assert "Autopilot cockpit polish" in plan["analysis"]
        artifact = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.run_id == run.run_id, ProjectAutonomyArtifact.name == "heuristic_plan_fast_path")
            .one()
        )
        assert "Autopilot cockpit polish" in (artifact.content_json or "")
    finally:
        db.close()


def test_architect_review_model_payload_and_artifact(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo_file = tmp_path / "app/example.py"
        repo_file.parent.mkdir(parents=True)
        repo_file.write_text("VALUE = 1\n", encoding="utf-8")
        run = ProjectAutonomyRun(
            run_id="pa_review_model",
            prompt="change example",
            status="running",
            current_stage="plan",
        )
        db.add(run)
        db.commit()
        plan = {
            "analysis": "Change the example constant safely.",
            "files": [
                {
                    "path": "app/example.py",
                    "action": "modify",
                    "description": "Change the example constant in a focused way.",
                }
            ],
            "notes": "",
        }
        files = orchestrator._plan_files(plan)
        review = orchestrator._review_architect_plan(
            plan=plan,
            files=files,
            context={"relevant_files": [], "hotspots": [], "insights": [], "repos": []},
            repo_path=tmp_path,
            prompt=run.prompt,
            attempt_index=1,
        )

        row = orchestrator._record_architect_review(db, run, review)
        db.commit()
        payload = orchestrator.run_payload(db, run, include_events=True)

        assert row.status == "passed"
        assert payload["architect_review"]["status"] == "passed"
        assert payload["architect_review"]["score"] >= orchestrator.ARCHITECT_REVIEW_PASSING_SCORE
        assert any(a["artifact_type"] == "architect_review" for a in payload["artifacts"])
    finally:
        db.close()


def test_architect_review_migration_creates_table():
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        migrations._migration_279_project_autonomy_architect_reviews(conn)
        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(project_autonomy_architect_reviews)")).fetchall()
        }

        assert {
            "run_id",
            "attempt_index",
            "status",
            "score",
            "confidence",
            "dimensions_json",
            "alternatives_json",
            "critique_json",
            "selected_files_json",
            "blocking_reason",
        }.issubset(cols)
        conn.execute(
            text(
                "INSERT INTO project_autonomy_architect_reviews "
                "(run_id, attempt_index, status, score, confidence) "
                "VALUES ('pa_migration', 1, 'passed', 91, 'high')"
            )
        )
        row = conn.execute(
            text("SELECT status, score FROM project_autonomy_architect_reviews WHERE run_id='pa_migration'")
        ).one()
        assert row[0] == "passed"
        assert row[1] == 91


def test_approve_plan_requires_passed_architect_review(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_review_gate",
            repo_id=repo.id,
            prompt="find a small enhancement for the desktop app",
            status="awaiting_approval",
            current_stage="plan",
            execution_mode="plan_approval",
            plan_status="awaiting_approval",
            plan_json='{"analysis":"bad","files":[{"path":"chili_mobile/lib/src/network/chili_api_client.dart","action":"modify"}],"notes":""}',
        )
        db.add(run)
        db.commit()
        orchestrator._record_architect_review(
            db,
            run,
            {
                "attempt_index": 1,
                "status": "failed",
                "score": 40,
                "confidence": "low",
                "dimensions": {},
                "alternatives": [],
                "critique": {"blockers": ["mismatched_domain"]},
                "selected_files": [],
                "blocking_reason": "Plan quality gate failed: mismatched_domain",
            },
        )
        db.commit()

        with pytest.raises(ValueError, match="quality gate"):
            orchestrator.approve_plan(db, run.run_id)
    finally:
        db.close()


def test_plan_feedback_invalidates_previous_architect_review(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_feedback_invalidates_review",
            repo_id=repo.id,
            prompt="add image attachments to Autopilot prompts",
            status="awaiting_approval",
            current_stage="plan",
            execution_mode="plan_approval",
            plan_status="awaiting_approval",
            plan_json=(
                '{"analysis":"Add prompt attachments.",'
                '"files":[{"path":"chili_mobile/lib/src/brain/brain_dispatch_screen.dart",'
                '"action":"modify","description":"Add prompt attachment controls."}],'
                '"notes":""}'
            ),
            files_json='["chili_mobile/lib/src/brain/brain_dispatch_screen.dart"]',
            agents_json='[{"name":"architect","files":["chili_mobile/lib/src/brain/brain_dispatch_screen.dart"]}]',
        )
        db.add(run)
        db.commit()
        orchestrator._record_architect_review(
            db,
            run,
            {
                "attempt_index": 1,
                "status": "passed",
                "score": 92,
                "confidence": "high",
                "dimensions": {},
                "alternatives": [],
                "critique": {"blockers": [], "next_action": "approval_ready"},
                "selected_files": [
                    {
                        "path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                        "rationale": "Composer controls live here.",
                    }
                ],
                "blocking_reason": None,
            },
        )
        db.commit()

        payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content="That plan misses the drag-and-drop image path; revise it.",
        )

        assert payload["status"] == "queued"
        assert payload["plan_status"] == "revising"
        assert payload["plan"] == {}
        assert payload["files"] == []
        assert payload["architect_review"]["status"] == "needs_revision"
        assert payload["architect_review"]["score"] == 0
        assert "previous approval is no longer valid" in payload["messages"][-1]["content"]
    finally:
        db.close()


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


def test_live_monitoring_prompt_is_not_treated_as_repo_edit():
    assert orchestrator._looks_like_live_monitoring_prompt(
        "monitor it right now as I'm testing... debug the errors live"
    )
    assert not orchestrator._looks_like_live_monitoring_prompt(
        "while I'm testing, update chili_mobile/lib/src/brain/foo.dart to fix the layout"
    )


def test_plan_approval_run_stops_before_worktree(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo_file = tmp_path / "app/example.py"
        repo_file.parent.mkdir(parents=True)
        repo_file.write_text("VALUE = 1\n", encoding="utf-8")
        orchestrator._git(tmp_path, ["init"], timeout=60)
        orchestrator._git(tmp_path, ["add", "."], timeout=60)
        orchestrator._git(
            tmp_path,
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "init",
            ],
            timeout=60,
        )
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_plan_only",
            repo_id=repo.id,
            prompt="change example",
            status="queued",
            current_stage="queued",
            execution_mode="plan_approval",
            plan_status="drafting",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(
            orchestrator,
            "build_local_plan",
            lambda *args, **kwargs: {
                "analysis": "Change the example constant.",
                "files": [{"path": "app/example.py", "action": "modify"}],
                "notes": "",
            },
        )
        monkeypatch.setattr(
            orchestrator,
            "_create_run_worktree",
            lambda *args, **kwargs: pytest.fail("plan mode must not create a worktree before approval"),
        )

        payload = orchestrator.run_autonomy_sync(db, run.run_id)

        assert payload["status"] == "awaiting_approval"
        assert payload["plan_status"] == "awaiting_approval"
        assert payload["worktree_path"] is None
        assert any(m["message_type"] == "plan" for m in payload["messages"])
        assert not any(a["artifact_type"] == "diff" for a in payload["artifacts"])
    finally:
        db.close()


def test_planning_revises_failed_architect_review_before_approval(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        presenter = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
        api = tmp_path / "chili_mobile/lib/src/network/chili_api_client.dart"
        presenter.parent.mkdir(parents=True)
        presenter.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
        api.parent.mkdir(parents=True)
        api.write_text("class ChiliApiClient {}\n", encoding="utf-8")
        orchestrator._git(tmp_path, ["init"], timeout=60)
        orchestrator._git(tmp_path, ["add", "."], timeout=60)
        orchestrator._git(
            tmp_path,
            ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
            timeout=60,
        )
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_review_revise",
            repo_id=repo.id,
            prompt="find a small enhancement for the desktop app",
            status="queued",
            current_stage="queued",
            execution_mode="plan_approval",
            plan_status="drafting",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(
            orchestrator,
            "_gather_context",
            lambda *args, **kwargs: {"repos": [], "insights": [], "hotspots": [], "relevant_files": []},
        )
        monkeypatch.setattr(
            orchestrator,
            "build_local_plan",
            lambda *args, **kwargs: {
                "analysis": "Change API client for broad desktop request.",
                "files": [
                    {
                        "path": "chili_mobile/lib/src/network/chili_api_client.dart",
                        "action": "modify",
                        "description": "Change API client for the broad desktop enhancement.",
                    }
                ],
                "notes": "",
            },
        )

        payload = orchestrator.run_autonomy_sync(db, run.run_id)

        assert payload["status"] == "awaiting_approval"
        assert payload["architect_review"]["status"] == "passed"
        assert payload["files"] == ["chili_mobile/lib/src/brain/autonomy_run_presenter.dart"]
        reviews = db.query(ProjectAutonomyArchitectReview).filter_by(run_id=run.run_id).all()
        assert len(reviews) >= 2
        assert reviews[0].status == "failed"
        assert reviews[-1].status == "passed"
    finally:
        db.close()


def test_planning_asks_for_clarification_after_failed_review_attempts(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        (tmp_path / "README.md").write_text("test repo\n", encoding="utf-8")
        orchestrator._git(tmp_path, ["init"], timeout=60)
        orchestrator._git(tmp_path, ["add", "."], timeout=60)
        orchestrator._git(
            tmp_path,
            ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
            timeout=60,
        )
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_review_clarify",
            repo_id=repo.id,
            prompt="find a small enhancement for the desktop app",
            status="queued",
            current_stage="queued",
            execution_mode="plan_approval",
            plan_status="drafting",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(
            orchestrator,
            "_gather_context",
            lambda *args, **kwargs: {"repos": [], "insights": [], "hotspots": [], "relevant_files": []},
        )
        monkeypatch.setattr(
            orchestrator,
            "build_local_plan",
            lambda *args, **kwargs: {"analysis": "No concrete plan yet.", "files": [], "notes": ""},
        )

        payload = orchestrator.run_autonomy_sync(db, run.run_id)

        assert payload["status"] == "awaiting_clarification"
        assert payload["plan_status"] == "awaiting_clarification"
        assert payload["architect_review"]["status"] == "needs_clarification"
        assert any(m["message_type"] == "clarification" for m in payload["messages"])
        plan_messages = [m["content"] for m in payload["messages"] if m["message_type"] == "plan"]
        assert plan_messages
        assert "won't ask for approval yet" in plan_messages[-1]
        assert "waiting for your approval" not in plan_messages[-1]
        with pytest.raises(ValueError):
            orchestrator.approve_plan(db, run.run_id)
    finally:
        db.close()


def test_approved_plan_resumes_implementation_phase(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_approved",
            repo_id=repo.id,
            prompt="change example",
            status="queued",
            current_stage="implement",
            execution_mode="plan_approval",
            plan_status="approved",
            plan_json='{"analysis":"ok","files":[{"path":"app/example.py","action":"modify"}],"notes":""}',
        )
        db.add(run)
        db.commit()
        called = {}

        def fake_impl(db_arg, run_arg, repo_arg, repo_path_arg):
            called["run_id"] = run_arg.run_id
            return {"run_id": run_arg.run_id, "status": "merged"}

        monkeypatch.setattr(orchestrator, "_run_implementation_phase", fake_impl)
        monkeypatch.setattr(orchestrator, "resolve_repo_runtime_path", lambda repo_arg: tmp_path)

        payload = orchestrator.run_autonomy_sync(db, run.run_id)

        assert called["run_id"] == "pa_approved"
        assert payload["status"] == "merged"
    finally:
        db.close()


def test_visual_validation_records_video_skip_as_non_blocking_artifact():
    db = _sqlite_autonomy_session()
    try:
        run = ProjectAutonomyRun(
            run_id="pa_visual",
            prompt="review the UI",
            status="awaiting_approval",
            current_stage="plan",
            execution_mode="plan_approval",
            plan_status="awaiting_approval",
        )
        db.add(run)
        db.commit()

        payload = orchestrator.record_visual_validation(db, run.run_id, kind="video")

        assert payload is not None
        video = next(a for a in payload["artifacts"] if a["artifact_type"] == "visual_video")
        assert video["content_json"]["skipped"] is True
        assert any(a["artifact_type"] == "ux_review" for a in payload["artifacts"])
        assert payload["messages"][-1]["message_type"] == "validation"
    finally:
        db.close()


def test_visual_validation_accepts_safe_absolute_desktop_screenshot_path():
    db = _sqlite_autonomy_session()
    try:
        run = ProjectAutonomyRun(
            run_id="pa_visual_path",
            prompt="review the UI",
            status="awaiting_approval",
            current_stage="plan",
            execution_mode="plan_approval",
            plan_status="awaiting_approval",
        )
        db.add(run)
        db.commit()
        screenshot_path = r"D:\captures\chili_focus.png"

        payload = orchestrator.record_visual_validation(
            db,
            run.run_id,
            kind="screenshot",
            path=screenshot_path,
        )

        assert payload is not None
        screenshot = next(a for a in payload["artifacts"] if a["artifact_type"] == "visual_screenshot")
        assert screenshot["content_json"]["path"] == screenshot_path
        assert screenshot["content_json"]["source"] == "desktop"
        assert screenshot["content_json"]["skipped"] is False
    finally:
        db.close()


def test_visual_validation_rejects_unsafe_path_without_persisting_raw_value():
    db = _sqlite_autonomy_session()
    try:
        run = ProjectAutonomyRun(
            run_id="pa_visual_unsafe",
            prompt="review the UI",
            status="awaiting_approval",
            current_stage="plan",
            execution_mode="plan_approval",
            plan_status="awaiting_approval",
        )
        db.add(run)
        db.commit()
        unsafe_path = "../secrets/chili.env"

        payload = orchestrator.record_visual_validation(
            db,
            run.run_id,
            kind="screenshot",
            path=unsafe_path,
        )

        assert payload is not None
        screenshot = next(a for a in payload["artifacts"] if a["artifact_type"] == "visual_screenshot")
        artifact_json = json.dumps(screenshot["content_json"])
        assert screenshot["content_json"]["path"] is None
        assert screenshot["content_json"]["skipped"] is True
        assert screenshot["content_json"]["path_rejected"] is True
        assert "rejected" in screenshot["content_json"]["skip_reason"]
        assert unsafe_path not in artifact_json
        assert any("rejected" in m["content"] for m in payload["messages"])
    finally:
        db.close()


def test_events_after_includes_chat_messages():
    db = _sqlite_autonomy_session()
    try:
        run = ProjectAutonomyRun(
            run_id="pa_events",
            prompt="hello",
            status="awaiting_approval",
            current_stage="plan",
        )
        db.add(run)
        db.commit()
        orchestrator._record_message(db, run, "assistant", "Plan ready.", message_type="plan")

        events = orchestrator.events_after(db, run.run_id)

        assert events["messages"][0]["content"] == "Plan ready."
        assert events["after_message_id"] == events["messages"][0]["id"]
    finally:
        db.close()


def test_create_run_defaults_to_chatting_without_planning_worker(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="hi", repo_id=repo.id)

        payload = orchestrator.run_payload(db, run, include_events=True)

        assert payload["status"] == "chatting"
        assert payload["plan_status"] == "chatting"
        assert payload["steps"][0]["stage"] == "chat"
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][1]["role"] == "assistant"
        assert "won't scan or edit" in payload["messages"][1]["content"]
    finally:
        db.close()


def test_autopilot_chat_stores_image_attachments_in_message_metadata(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        image = tmp_path / "autopilot_prompt.png"
        image.write_bytes(b"not really a png, but path metadata only")
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()

        run = orchestrator.create_run(
            db,
            prompt="Please review this UI state.",
            repo_id=repo.id,
            attachments=[
                {
                    "kind": "image",
                    "path": str(image),
                    "name": image.name,
                    "mime_type": "image/png",
                }
            ],
        )
        payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content="Use this screenshot when you draft the plan.",
            attachments=[
                {
                    "kind": "image",
                    "path": str(image),
                    "name": "second.png",
                    "mime_type": "image/png",
                }
            ],
        )

        user_messages = [m for m in payload["messages"] if m["role"] == "user"]
        assert user_messages[0]["metadata"]["attachments"][0]["name"] == image.name
        assert user_messages[-1]["metadata"]["attachments"][0]["name"] == "second.png"
        conversation_prompt = orchestrator._conversation_prompt(db, run)
        assert "Attached images:" in conversation_prompt
        assert "local desktop image" in conversation_prompt
        assert str(image) not in conversation_prompt
        artifacts = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.run_id == run.run_id, ProjectAutonomyArtifact.artifact_type == "prompt_image")
            .all()
        )
        assert len(artifacts) == 2
    finally:
        db.close()


def test_autopilot_chat_rejects_unsafe_image_attachment_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.gateway_chat",
        lambda **k: {"reply": "ok", "model": "m"},
    )
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()

        run = orchestrator.create_run(
            db,
            prompt="Please review this UI state.",
            repo_id=repo.id,
            attachments=[
                {
                    "kind": "image",
                    "path": "../secrets/chili.png",
                    "name": "leak.png",
                    "mime_type": "image/png",
                },
                {
                    "kind": "image",
                    "url": "file:///C:/Users/rindo/secret.png",
                    "name": "secret.png",
                    "mime_type": "image/png",
                },
            ],
        )

        payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content="Use this screenshot instead.",
            attachments=[
                {
                    "kind": "image",
                    "url": "https://getchili.app/captures/autopilot.png?cache=1",
                    "name": "autopilot.png",
                    "mime_type": "image/png",
                }
            ],
        )

        first_user = [m for m in payload["messages"] if m["role"] == "user"][0]
        latest_user = [m for m in payload["messages"] if m["role"] == "user"][-1]
        assert "attachments" not in first_user["metadata"]
        assert latest_user["metadata"]["attachments"][0]["url"].startswith("https://getchili.app/")
        conversation_prompt = orchestrator._conversation_prompt(db, run)
        assert "remote image URL" in conversation_prompt
        assert "https://getchili.app/captures/autopilot.png" not in conversation_prompt
        serialized = json.dumps(payload)
        assert "../secrets/chili.png" not in serialized
        assert "file:///C:/Users/rindo/secret.png" not in serialized
        artifacts = [
            a for a in payload["artifacts"] if a["artifact_type"] == "prompt_image"
        ]
        assert len(artifacts) == 1
        assert artifacts[0]["content_json"]["url"].startswith("https://getchili.app/")
    finally:
        db.close()


def test_plan_message_hides_raw_local_model_errors():
    plan = {
        "analysis": "Local model planning was unavailable (http://ollama:11434: TimeoutError: timed out).",
        "files": [{"path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"}],
        "notes": "URLError: <urlopen error [Errno 111] Connection refused>",
    }

    message = orchestrator._plan_message(
        plan,
        plan["files"],
        [{"name": "architect"}, {"name": "ui"}],
    )

    assert "http://" not in message
    assert "URLError" not in message
    assert "local planning model" in message.lower()
    assert "brain_dispatch_screen.dart" in message


def test_run_payload_hides_raw_local_model_plan_errors():
    db = _sqlite_autonomy_session()
    try:
        run = ProjectAutonomyRun(
            run_id="pa_safe_plan",
            prompt="plan a small UI fix",
            status="awaiting_approval",
            current_stage="plan",
            plan_json=(
                '{"analysis":"Local model planning was unavailable '
                '(http://ollama:11434: TimeoutError: timed out).",'
                '"files":[{"path":"chili_mobile/lib/src/brain/'
                'brain_dispatch_screen.dart"}],'
                '"notes":"URLError: <urlopen error [Errno 111] Connection refused>"}'
            ),
        )
        db.add(run)
        db.commit()

        payload = orchestrator.run_payload(db, run)

        plan_text = f"{payload['plan']['analysis']} {payload['plan']['notes']}"
        assert "http://" not in plan_text
        assert "URLError" not in plan_text
        assert "local planning model" in plan_text.lower()
    finally:
        db.close()


def test_run_payload_emits_backend_pursuing_goal_contract_without_model_goal():
    db = _sqlite_autonomy_session()
    try:
        run = ProjectAutonomyRun(
            run_id="pa_pursuing_goal_backend_contract",
            prompt="Add a durable Pursuing goal contract to Autopilot.",
            status="awaiting_approval",
            current_stage="plan",
            plan_json=json.dumps(
                {
                    "analysis": (
                        "Keep the active objective, current step, next action, "
                        "and completion gate visible without expanding context."
                    ),
                    "files": [
                        {"path": "app/services/project_autonomy/orchestrator.py"}
                    ],
                }
            ),
        )
        db.add(run)
        db.commit()

        payload = orchestrator.run_payload(db, run)
        goal = payload["pursuing_goal"]

        assert goal["schema"] == "chili.project-autopilot.pursuing-goal.v1"
        assert goal["source"] == "backend_run_state"
        assert goal["objective"] == "Add a durable Pursuing goal contract to Autopilot."
        assert goal["status_label"] == "Pursuing goal"
        assert goal["current_step"] == "Draft and review the architect plan"
        assert goal["next_action"] == "Review the architect plan, then approve it or send feedback."
        assert goal["completion_gate"] == "The architect quality gate must pass before implementation starts."
        assert goal["progress_percent"] == 42
        assert goal["receipt_sections"] == [
            "Objective",
            "Current evidence",
            "Checks",
            "Next gate",
        ]
        assert "Every agent report must name objective-tied evidence" in goal[
            "agent_prompt_contract"
        ]
        assert "Permission boundary" in goal["context_handoff_copy"]
        assert "does not authorize source/test edits" in goal["context_handoff_copy"]
    finally:
        db.close()


def test_run_payload_prefers_explicit_pursuing_goal_contract_over_prompt():
    db = _sqlite_autonomy_session()
    try:
        run = ProjectAutonomyRun(
            run_id="pa_pursuing_goal_explicit_contract",
            prompt="generic scheduled report pass",
            status="running",
            current_stage="validate",
            plan_json=json.dumps(
                {
                    "goal_contract": {
                        "objective": "Make scheduled reports prove active-goal progress.",
                        "current_step": "Validate goal receipt parser",
                        "next_action": "Run focused backend and Flutter checks",
                        "completion_gate": (
                            "Every trusted report names objective, evidence path, "
                            "and next gate."
                        ),
                        "progress_percent": 71,
                        "success_criteria": [
                            "Goal contract appears in run payloads.",
                            "Agents cannot claim progress without objective-tied evidence.",
                        ],
                    }
                }
            ),
            learning_json=json.dumps(
                {
                    "scheduled_quality": {
                        "goal_contract": {
                            "score": 64,
                            "affected_report_count": 1,
                        }
                    }
                }
            ),
        )
        db.add(run)
        db.commit()

        payload = orchestrator.run_payload(db, run)
        goal = payload["pursuing_goal"]

        assert goal["source"] == "explicit_goal_contract"
        assert goal["objective"] == "Make scheduled reports prove active-goal progress."
        assert goal["current_step"] == "Validate goal receipt parser"
        assert goal["next_action"] == "Run focused backend and Flutter checks"
        assert goal["completion_gate"].startswith("Every trusted report names objective")
        assert goal["progress_percent"] == 71
        assert "Goal contract appears in run payloads." in goal["success_criteria"]
        assert "generic scheduled report pass" not in goal["context_handoff_copy"]
        assert "Make scheduled reports prove active-goal progress." in goal[
            "agent_prompt_contract"
        ]
    finally:
        db.close()


def test_run_payload_surfaces_scheduled_quality_receipts():
    db = _sqlite_autonomy_session()
    try:
        learning_run = ProjectAutonomyRun(
            run_id="pa_quality_learning",
            prompt="keep pursuing the current goal until proof is attached",
            status="completed",
            current_stage="learn",
            learning_json=json.dumps(
                {
                    "scheduled_quality": {
                        "goal_contract": {"score": 72},
                        "report_quality": {"affected_report_count": 1},
                    }
                }
            ),
        )
        agent_run = ProjectAutonomyRun(
            run_id="pa_quality_agent",
            prompt="coordinate coding agents and hold PR receipt quality",
            status="running",
            current_stage="coordinate",
            agents_json=json.dumps(
                [
                    {
                        "name": "architect",
                        "scheduled_quality_pressure": {
                            "goal_drift": {"affected_report_count": 1},
                        },
                    }
                ]
            ),
        )
        artifact_run = ProjectAutonomyRun(
            run_id="pa_quality_artifact",
            prompt="repair the PR creation flow",
            status="awaiting_approval",
            current_stage="review",
        )
        db.add_all([learning_run, agent_run, artifact_run])
        db.commit()
        orchestrator._add_artifact(
            db,
            artifact_run.run_id,
            "goal_receipt",
            "pr_receipt_gate",
            content_json={
                "goal_contract": {"score": 64},
                "pr_receipt": {"missing": True},
            },
        )

        learning_payload = orchestrator.run_payload(db, learning_run)
        agent_payload = orchestrator.run_payload(db, agent_run)
        artifact_payload = orchestrator.run_payload(db, artifact_run, include_events=True)

        assert learning_payload["scheduled_quality"]["goal_contract"]["score"] == 72
        assert agent_payload["scheduled_quality"]["goal_drift"]["affected_report_count"] == 1
        assert artifact_payload["scheduled_quality"]["pr_receipt"]["missing"] is True
    finally:
        db.close()


def test_run_payload_surfaces_delivery_quality_bar_without_scheduled_conflation():
    db = _sqlite_autonomy_session()
    try:
        learning_run = ProjectAutonomyRun(
            run_id="pa_quality_bar_learning",
            prompt="hold PR publication until current-head proof exists",
            status="running",
            current_stage="review",
            learning_json=json.dumps(
                {
                    "quality_bar": {
                        "delivery_blocker_groups": [
                            {
                                "key": "pr_blocker_train",
                                "count": 2,
                                "gate_label": "Blocked until PR proof",
                            }
                        ]
                    }
                }
            ),
        )
        agent_run = ProjectAutonomyRun(
            run_id="pa_quality_bar_agent",
            prompt="surface recovery brake state",
            status="running",
            current_stage="coordinate",
            agents_json=json.dumps(
                [
                    {
                        "name": "agentops",
                        "quality_bar": {
                            "delivery_blocker_groups": [
                                {
                                    "key": "recovery_brake",
                                    "count": 1,
                                    "decision": "hold_emergency_brake",
                                }
                            ]
                        },
                    }
                ]
            ),
        )
        artifact_run = ProjectAutonomyRun(
            run_id="pa_quality_bar_artifact",
            prompt="route stable inbox delivery blockers",
            status="awaiting_approval",
            current_stage="plan",
        )
        db.add_all([learning_run, agent_run, artifact_run])
        db.commit()
        orchestrator._add_artifact(
            db,
            artifact_run.run_id,
            "delivery_blocker_quality",
            "coordination_queue_quality_bar",
            content_json={
                "quality_bar": {
                    "delivery_blocker_groups": [
                        {
                            "key": "coordination_queue",
                            "count": 4,
                            "stable_inbox_from": "AgentOps",
                            "stable_inbox_to": "PM",
                        }
                    ]
                }
            },
        )

        learning_payload = orchestrator.run_payload(db, learning_run)
        agent_payload = orchestrator.run_payload(db, agent_run)
        artifact_payload = orchestrator.run_payload(db, artifact_run, include_events=True)

        assert learning_payload["scheduled_quality"] == {}
        pr_group = learning_payload["quality_bar"]["delivery_blocker_groups"][0]
        assert pr_group["key"] == "pr_blocker_train"
        assert pr_group["pr_publish_verdict"] == "not_publishable"
        assert pr_group["pr_publish_gate_state"] == "blocked_until_current_head_publication_receipt"
        assert "current_head_check_receipt" in pr_group["publication_receipt"]["missing_evidence"]
        assert pr_group["publication_receipt"]["publication_proof_ready"] is False
        assert "push_or_pr_creation" in pr_group["pr_publish_forbidden_actions"]
        assert "Project Autopilot PR publication decision packet" in pr_group["pr_publish_packet_copy"]
        assert "does not authorize source/test edits" in pr_group["pr_publish_packet_copy"]
        assert agent_payload["quality_bar"]["delivery_blocker_groups"][0]["decision"] == "hold_emergency_brake"
        assert artifact_payload["quality_bar"]["delivery_blocker_groups"][0]["stable_inbox_to"] == "PM"
    finally:
        db.close()


def test_pr_blocker_quality_bar_names_recovery_decision_before_pr_movement():
    packet = orchestrator._quality_bar_from_mapping(
        {
            "quality_bar": {
                "delivery_blocker_groups": [
                    {
                        "key": "pr_blocker_train",
                        "count": 1,
                        "top_pr": "134",
                        "top_branch": "codex/brain-work-done-marker-recovery",
                        "top_merge": "DIRTY",
                        "top_ci": "no checks",
                        "gate_state": "pm_operator_disposition_required",
                        "next_action_detail": (
                            "Keep the PR frozen until PM/operator accepts "
                            "close/recreate, clean rebuild, current-head gates, "
                            "or one named repair path."
                        ),
                    }
                ]
            }
        }
    )

    pr_group = packet["delivery_blocker_groups"][0]

    assert pr_group["pr_publish_verdict"] == "not_publishable"
    assert pr_group["pr_recovery_decision"] == "wait_for_operator_disposition"
    assert pr_group["pr_recovery_decision_label"] == "Wait for PM/operator PR disposition"
    assert pr_group["pr_publish_first_action_owner"] == "PM/operator"
    assert "choose keep blocked, close/recreate, clean rebuild" in pr_group[
        "pr_publish_first_action_label"
    ]
    assert pr_group["pr_publish_action_plan"][0]["decision"] == "wait_for_operator_disposition"
    assert "push_or_pr_creation" in pr_group["pr_publish_forbidden_actions"]
    assert "Recovery decision:" in pr_group["pr_publish_packet_copy"]
    assert "Wait for PM/operator PR disposition" in pr_group["pr_publish_packet_copy"]
    assert "does not authorize source/test edits" in pr_group["pr_publish_packet_copy"]


def test_pr_blocker_health_scan_promotes_current_head_recovery_packet():
    packet = orchestrator._quality_bar_from_mapping(
        {
            "generated_utc": "2026-06-02T05:12:45Z",
            "repo": "rindo/chili-home-copilot",
            "checked_open_pr_count": 2,
            "ci_blocked_count": 2,
            "items": [
                {
                    "number": 109,
                    "title": "Release trust route hardening",
                    "url": "https://github.com/rindo/chili-home-copilot/pull/109",
                    "draft": True,
                    "branch": "codex/sswe/release-trust-hardening",
                    "head_ref_oid": "abc123def4567890",
                    "base": "main",
                    "merge_state": "CLEAN",
                    "ci_state": "failing",
                    "ci_summary": "flutter test:FAILURE",
                    "blocker_kind": "ci_failing",
                    "blocked": True,
                    "failing_checks": [
                        {
                            "name": "flutter test",
                            "url": "https://github.com/rindo/chili-home-copilot/actions/1",
                        }
                    ],
                },
                {
                    "number": 134,
                    "branch": "codex/brain-work-done-marker-recovery",
                    "head_ref_oid": "fed9876543210",
                    "merge_state": "DIRTY",
                    "ci_state": "no_checks",
                    "ci_summary": "no checks",
                    "blocker_kind": "ci_missing_checks",
                    "blocked": True,
                },
            ],
        }
    )

    pr_group = packet["delivery_blocker_groups"][0]

    assert packet["source"] == "agent_pr_blocker_health"
    assert packet["ci_blocked_count"] == 2
    assert pr_group["top_pr"] == "109"
    assert pr_group["top_branch"] == "codex/sswe/release-trust-hardening"
    assert pr_group["head_sha"] == "abc123def4567890"
    assert pr_group["pr_recovery_decision"] == "repair_current_head_ci"
    assert pr_group["pr_recovery_decision_label"] == "Run one named owner repair path"
    assert "current_head_ci_failing" in pr_group["pr_publish_blockers"]
    assert pr_group["failing_check_names"] == ["flutter test"]
    assert "head abc123def456" in pr_group["publication_receipt"]["proof_items"]
    assert "Project Autopilot PR publication decision packet" in pr_group[
        "pr_publish_packet_copy"
    ]


def test_pr_blocker_health_pending_checks_get_wait_decision():
    packet = orchestrator._quality_bar_from_mapping(
        {
            "agent_pr_blocker_health": {
                "checked_open_pr_count": 1,
                "ci_blocked_count": 1,
                "items": [
                    {
                        "number": 191,
                        "branch": "codex/desktop-pr-publish-flow",
                        "head_ref_oid": "456789abcdef0000",
                        "merge_state": "CLEAN",
                        "ci_state": "pending",
                        "ci_summary": "desktop build:PENDING",
                        "blocker_kind": "ci_pending",
                        "pending_checks": [
                            {
                                "name": "desktop build",
                                "url": "https://github.com/rindo/chili-home-copilot/actions/2",
                            }
                        ],
                    }
                ],
            }
        }
    )

    pr_group = packet["delivery_blocker_groups"][0]

    assert pr_group["pr_recovery_decision"] == "wait_for_current_head_checks"
    assert pr_group["pr_recovery_decision_label"] == "Wait for current-head checks"
    assert "current_head_ci_pending" in pr_group["pr_publish_blockers"]
    assert "wait for or refresh exact-head check receipts" in pr_group[
        "pr_publish_first_action_label"
    ]
    assert pr_group["pending_check_names"] == ["desktop build"]


def test_run_payload_promotes_agent_pr_blocker_health_artifact_quality_bar():
    db = _sqlite_autonomy_session()
    try:
        run = ProjectAutonomyRun(
            run_id="pa_pr_blocker_health_artifact",
            prompt="inspect live PR blocker health",
            status="running",
            current_stage="review",
        )
        db.add(run)
        db.commit()
        orchestrator._add_artifact(
            db,
            run.run_id,
            "agent_pr_blocker_health",
            "current_pr_blocker_scan",
            content_json={
                "quality_bar": {
                    "generated_utc": "2026-06-02T05:12:45Z",
                    "blocker_count": 1,
                    "items": [
                        {
                            "pr_number": "134",
                            "pr_branch": "codex/brain-work-done-marker-recovery",
                            "head_sha": "fed9876543210",
                            "merge_state": "CLEAN",
                            "ci_state": "no_checks",
                            "ci_summary": "no checks",
                            "blocker_kind": "ci_missing_checks",
                            "path": "project_ws/SRE/OUT/_state/agent-pr-blocker-health.json",
                        }
                    ],
                }
            },
        )

        payload = orchestrator.run_payload(db, run, include_events=True)
        pr_group = payload["quality_bar"]["delivery_blocker_groups"][0]

        assert payload["quality_bar"]["source"] == "agent_pr_blocker_health"
        assert pr_group["top_pr"] == "134"
        assert pr_group["top_branch"] == "codex/brain-work-done-marker-recovery"
        assert pr_group["pr_recovery_decision"] == "attach_current_head_checks"
        assert pr_group["pr_publish_first_action_owner"] == "PR owner"
        assert "current_head_checks_missing" in pr_group["pr_publish_blockers"]
        assert "Attach current-head checks" in pr_group["pr_publish_packet_copy"]
        assert (
            pr_group["next_action_path"]
            == "project_ws/SRE/OUT/_state/agent-pr-blocker-health.json"
        )
    finally:
        db.close()


def test_run_payload_surfaces_pr_publication_receipt_quality_bar_artifact():
    db = _sqlite_autonomy_session()
    try:
        run = ProjectAutonomyRun(
            run_id="pa_pr_publication_receipt",
            prompt="inspect PR publication proof",
            status="running",
            current_stage="review",
        )
        db.add(run)
        db.commit()
        orchestrator._add_artifact(
            db,
            run.run_id,
            "delivery_pr_publication_receipt",
            "current_head_pr_publication_receipt",
            content_json={
                "publication_receipt": {
                    "schema": "chili.execution-pr-publication-receipt.v1",
                    "status": "warning",
                    "publication_proof_ready": False,
                    "missing_evidence": ["current_head_check_receipt"],
                }
            },
        )

        payload = orchestrator.run_payload(db, run, include_events=True)

        assert payload["scheduled_quality"] == {}
        assert payload["quality_bar"]["publication_receipt"]["status"] == "warning"
        assert payload["quality_bar"]["publication_receipt"]["missing_evidence"] == [
            "current_head_check_receipt"
        ]
    finally:
        db.close()


def test_start_plan_transitions_chat_to_queued_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.gateway_chat",
        lambda **k: {"reply": "ok", "model": "m"},
    )
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="hi", repo_id=repo.id)
        orchestrator.append_user_message(
            db,
            run.run_id,
            content="Let's implement a safer settings flow.",
        )

        payload = orchestrator.start_plan(db, run.run_id)

        assert payload is not None
        assert payload["status"] == "queued"
        assert payload["plan_status"] == "drafting"
        assert "Let's implement a safer settings flow." in payload["prompt"]
        assert payload["messages"][-1]["message_type"] == "status"
    finally:
        db.close()


def test_plan_intent_message_starts_plan_without_chat_model(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="hi", repo_id=repo.id)
        monkeypatch.setattr(
            orchestrator,
            "_chat_reply",
            lambda *args, **kwargs: pytest.fail(
                "plan intent should not call brainstorm chat"
            ),
        )

        payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content="create a plan for it",
        )

        assert payload is not None
        assert payload["status"] == "queued"
        assert payload["plan_status"] == "drafting"
        assert payload["messages"][-1]["message_type"] == "status"
        assert "draft a plan" in payload["messages"][-1]["content"]
        assert "create a plan for it" in payload["prompt"]
    finally:
        db.close()


def test_recover_orphaned_runs_blocks_pre_restart_active_run(monkeypatch):
    db = _sqlite_autonomy_session()
    try:
        before_restart = datetime.utcnow() - timedelta(minutes=5)
        monkeypatch.setattr(orchestrator, "_PROCESS_STARTED_AT", datetime.utcnow())
        run = ProjectAutonomyRun(
            run_id="pa_orphaned",
            prompt="make a change",
            status="running",
            current_stage="implement",
            merge_status="pending",
            started_at=before_restart,
            updated_at=before_restart,
        )
        db.add(run)
        db.commit()

        recovered = orchestrator.recover_orphaned_runs(db)
        db.refresh(run)

        assert recovered == 1
        assert run.status == "blocked"
        assert run.merge_status == "blocked"
        assert "interrupted" in (run.error_message or "")
    finally:
        db.close()


def test_heuristic_plan_fallback_uses_desktop_candidates(tmp_path):
    presenter_file = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
    desktop_file = tmp_path / "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
    error_file = tmp_path / "chili_mobile/lib/src/network/network_error_message.dart"
    presenter_file.parent.mkdir(parents=True)
    presenter_file.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
    desktop_file.write_text("// desktop brain screen\n", encoding="utf-8")
    error_file.parent.mkdir(parents=True)
    error_file.write_text("String userVisibleNetworkError(Object error) => '$error';\n", encoding="utf-8")
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
    assert len(plan["files"]) == 1
    assert plan["files"][0]["path"] == "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
    assert "Autopilot cockpit polish" in plan["analysis"]


def test_heuristic_plan_revision_overrides_incidental_network_words(tmp_path):
    presenter_file = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
    api_file = tmp_path / "chili_mobile/lib/src/network/chili_api_client.dart"
    presenter_file.parent.mkdir(parents=True)
    presenter_file.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
    api_file.parent.mkdir(parents=True)
    api_file.write_text("class ChiliApiClient {}\n", encoding="utf-8")
    context = {"repos": [], "insights": [], "hotspots": [], "relevant_files": []}

    plan = orchestrator._fallback_plan_from_context(
        context,
        tmp_path,
        (
            "find a small enhancement for the desktop app. revise to an Autopilot cockpit "
            "plan presentation and do not target API/network errors"
        ),
        "fast path",
    )

    assert plan["files"][0]["path"] == "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
    assert "Autopilot cockpit polish" in plan["analysis"]


def test_deterministic_small_desktop_diff_updates_presenter_plan_copy():
    content = (
        "class AutonomyRunPresenter {\n"
        "  static String planBody(Map<String, dynamic> plan) {\n"
        "    if (plan.isEmpty) return '';\n"
        "    final analysis = _safePlanText(plan['analysis']);\n"
        "    final notes = _safePlanText(plan['notes']);\n"
        f"{orchestrator.PRESENTER_PLAN_BODY_OLD_SNIPPET}"
        "    if (notes.isNotEmpty && notes != analysis) parts.add(notes);\n"
        "    return parts.join('\\n\\n');\n"
        "  }\n"
        "}\n"
    )

    diff = orchestrator._deterministic_small_desktop_diff(
        orchestrator.DESKTOP_AUTOPILOT_PRESENTER_FILE,
        content,
        "find a small enhancement for the desktop app",
    )

    assert diff is not None
    assert "final fileItems = _mapList(plan['files']);" in diff
    assert "Plan: ${_listSummary(changes, limit: 3)}." in diff


def test_heuristic_plan_fallback_prefers_available_deterministic_desktop_patch(tmp_path):
    error_file = tmp_path / "chili_mobile/lib/src/network/network_error_message.dart"
    api_file = tmp_path / "chili_mobile/lib/src/network/chili_api_client.dart"
    error_file.parent.mkdir(parents=True)
    error_file.write_text(
        "String userVisibleNetworkError(Object error) {\n"
        "  final s = error.toString();\n"
        "  if (s.contains('HandshakeException') || s.contains('CERTIFICATE_VERIFY_FAILED')) {}\n"
        "  if (s.contains('FormatException') || s.contains('Unexpected character')) {}\n"
        "  return s;\n"
        "}\n"
        "String userMessageForHttpStatus(int statusCode) {\n"
        "  switch (statusCode) {\n"
        "    case 401:\n"
        "    case 403:\n"
        "    case 502:\n"
        "      return 'handled';\n"
        "    default:\n"
        "      return 'Unexpected HTTP $statusCode from server.';\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    api_file.write_text(
        "Map<String, dynamic> parseResponse(response, decoded) {\n"
        "  final err = decoded?['error'] ?? decoded?['detail'] ?? response.body;\n"
        "  return {'ok': false, 'error': err};\n"
        "}\n",
        encoding="utf-8",
    )
    context = {"repos": [], "insights": [], "hotspots": [], "relevant_files": []}

    plan = orchestrator._fallback_plan_from_context(
        context,
        tmp_path,
        "find a small enhancement for the desktop app API error handling",
        "fast path",
    )

    assert plan["files"][0]["path"] == "chili_mobile/lib/src/network/chili_api_client.dart"


def test_autopilot_prompt_attachment_fallback_targets_cockpit_file(tmp_path):
    presenter_file = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
    cockpit_file = tmp_path / "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
    presenter_file.parent.mkdir(parents=True)
    presenter_file.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
    cockpit_file.write_text("class BrainDispatchScreen {}\n", encoding="utf-8")
    context = {"repos": [], "insights": [], "hotspots": [], "relevant_files": []}

    plan = orchestrator._fallback_plan_from_context(
        context,
        tmp_path,
        "please add a way to add image to a prompt here in autopilot just like claude and codex",
        "planner unavailable",
    )

    assert plan["files"][0]["path"] == "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
    assert "prompt attachments" in plan["files"][0]["description"]


def test_architect_review_blocks_attachment_plan_in_presenter(tmp_path):
    presenter_file = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
    cockpit_file = tmp_path / "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
    presenter_file.parent.mkdir(parents=True)
    presenter_file.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
    cockpit_file.write_text("class BrainDispatchScreen {}\n", encoding="utf-8")
    prompt = "please add a way to add image to a prompt here in autopilot just like claude and codex"
    context = {"repos": [], "insights": [], "hotspots": [], "relevant_files": []}
    bad_plan = {
        "analysis": "Improve Autopilot plan text.",
        "files": [
            {
                "path": "chili_mobile/lib/src/brain/autonomy_run_presenter.dart",
                "action": "modify",
                "description": "Improve Autopilot plan presentation.",
            }
        ],
        "notes": "",
    }
    bad_review = orchestrator._review_architect_plan(
        plan=bad_plan,
        files=orchestrator._plan_files(bad_plan),
        context=context,
        repo_path=tmp_path,
        prompt=prompt,
        attempt_index=1,
    )

    assert bad_review["status"] == "failed"
    assert "mismatched_domain" in bad_review["critique"]["blockers"]

    good_plan = {
        "analysis": "Add image attachment controls to the Autopilot chat composer.",
        "files": [
            {
                "path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                "action": "modify",
                "description": "Add image attachment controls to the Autopilot prompt composer.",
            }
        ],
        "notes": "",
    }
    good_review = orchestrator._review_architect_plan(
        plan=good_plan,
        files=orchestrator._plan_files(good_plan),
        context=context,
        repo_path=tmp_path,
        prompt=prompt,
        attempt_index=1,
    )

    assert good_review["status"] == "passed"
    assert good_review["score"] >= orchestrator.ARCHITECT_REVIEW_PASSING_SCORE


def test_vague_small_plan_is_narrowed_away_from_large_desktop_file(tmp_path):
    large_file = tmp_path / "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
    small_file = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
    large_file.parent.mkdir(parents=True)
    large_file.write_text("\n".join("// line" for _ in range(800)), encoding="utf-8")
    small_file.parent.mkdir(parents=True, exist_ok=True)
    small_file.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
    context = {"relevant_files": [], "hotspots": [], "insights": [], "repos": []}
    plan = {
        "analysis": "Improve loading feedback.",
        "files": [{"path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart", "action": "modify"}],
        "notes": "",
    }

    narrowed = orchestrator._narrow_plan_for_local_model(
        plan,
        context,
        tmp_path,
        "find a small enhancement for the desktop app",
    )

    assert narrowed["files"][0]["path"] == "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"


def test_runtime_resolves_container_aliases_to_host_workspace():
    repo = CodeRepo(path="/app", container_path="/workspace", name="workspace", active=True)

    resolved = code_runtime.resolve_repo_runtime_path(repo)

    assert resolved == Path(__file__).resolve().parents[1]


def test_runtime_prefers_container_workspace_when_available(monkeypatch, tmp_path):
    host_root = tmp_path / "app"
    workspace_root = tmp_path / "workspace"
    host_root.mkdir()
    workspace_root.mkdir()
    repo = CodeRepo(path="/app", name="workspace", active=True)
    monkeypatch.setattr(code_runtime, "_HOST_WORKSPACE_ROOT", host_root)
    monkeypatch.setattr(code_runtime, "_CONTAINER_WORKSPACE_ROOT", workspace_root)

    resolved = code_runtime.resolve_repo_runtime_path(repo)

    assert resolved == workspace_root


def test_windows_tmp_worktree_env_uses_real_temp_dir(monkeypatch):
    monkeypatch.setenv("CHILI_PROJECT_AUTOPILOT_WORKTREE_DIR", "/tmp")

    root = orchestrator._local_worktree_root()

    if os.name == "nt":
        assert root == Path(orchestrator.tempfile.gettempdir())
    else:
        assert root == Path("/tmp")


def test_generate_diffs_reports_rejected_model_output(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo_file = tmp_path / "foo.txt"
        repo_file.write_text("hello\n", encoding="utf-8")
        run = ProjectAutonomyRun(
            run_id="pa_reject",
            prompt="change foo",
            status="running",
            current_stage="implement",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(orchestrator, "select_local_model", lambda: {"model": "qwen", "available": True})
        monkeypatch.setattr(orchestrator.insights_mod, "get_insights", lambda *args, **kwargs: [])
        # The edit engine now goes through the gateway (local-first code
        # tier) instead of direct ollama_client — mock at the gateway so the
        # test never touches a real LLM.
        monkeypatch.setattr(
            "app.services.context_brain.llm_gateway.gateway_chat",
            lambda *args, **kwargs: {"reply": "I cannot produce a patch.", "model": "m"},
        )

        with pytest.raises(orchestrator.AutonomyBlocked) as exc:
            orchestrator.generate_diffs_from_plan(
                db,
                run,
                tmp_path,
                [{"path": "foo.txt", "description": "make a small change"}],
            )

        assert "neither edit blocks nor a unified diff" in str(exc.value)
    finally:
        db.close()


def test_small_desktop_fallback_diff_handles_bad_model_patch(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo_file = tmp_path / "chili_mobile/lib/src/network/network_error_message.dart"
        repo_file.parent.mkdir(parents=True)
        repo_file.write_text(
            "String userMessageForHttpStatus(int statusCode) {\n"
            "  switch (statusCode) {\n"
            "    case 502:\n"
            "      return 'bad gateway';\n"
            "    default:\n"
            "      return 'Unexpected HTTP $statusCode from server.';\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        orchestrator._git(tmp_path, ["init"], timeout=60)
        run = ProjectAutonomyRun(
            run_id="pa_fallback",
            prompt="find a small enhancement for the desktop app",
            status="running",
            current_stage="implement",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(orchestrator, "select_local_model", lambda: {"model": "qwen", "available": True})
        monkeypatch.setattr(orchestrator.insights_mod, "get_insights", lambda *args, **kwargs: [])
        monkeypatch.setattr(
            orchestrator.ollama_client,
            "chat",
            lambda *args, **kwargs: SimpleNamespace(
                ok=True,
                text="```diff\n--- a/bad\n+++ b/bad\n@@ broken\n```",
                error=None,
                latency_ms=1,
            ),
        )

        diffs = orchestrator.generate_diffs_from_plan(
            db,
            run,
            tmp_path,
            [
                {
                    "path": "chili_mobile/lib/src/network/network_error_message.dart",
                    "description": "make a small desktop enhancement",
                }
            ],
        )

        assert diffs
        assert "Access denied (403)" in diffs[0]
        assert "Authentication failed (401)" in diffs[0]
    finally:
        db.close()


def test_git_apply_check_accepts_stdin_patch_on_windows(tmp_path):
    rel = "chili_mobile/lib/src/network/network_error_message.dart"
    repo_file = tmp_path / rel
    repo_file.parent.mkdir(parents=True)
    repo_file.write_text(
        "String userMessageForHttpStatus(int statusCode) {\n"
        "  switch (statusCode) {\n"
        "    case 502:\n"
        "      return 'bad gateway';\n"
        "    default:\n"
        "      return 'Unexpected HTTP $statusCode from server.';\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
        newline="\n",
    )
    orchestrator._git(tmp_path, ["init"], timeout=60)
    diff = orchestrator._deterministic_small_desktop_diff(
        rel,
        repo_file.read_text(encoding="utf-8"),
        "find a small enhancement for the desktop app",
    )

    proc = orchestrator._git(tmp_path, ["apply", "--check"], input_text=diff, timeout=60)

    assert proc.returncode == 0, proc.stderr


def test_small_desktop_fallback_finds_second_network_enhancement(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo_file = tmp_path / "chili_mobile/lib/src/network/network_error_message.dart"
        repo_file.parent.mkdir(parents=True)
        repo_file.write_text(
            "String userVisibleNetworkError(Object error) {\n"
            "  final s = error.toString();\n"
            "  if (s.contains('Failed host lookup') || s.contains('getaddrinfo')) {\n"
            "    return 'Could not resolve the server hostname. Check internet and the Backend URL in Settings.';\n"
            "  }\n"
            "  if (s.contains('Connection refused')) {\n"
            "    return 'Connection refused.';\n"
            "  }\n"
            "  return s;\n"
            "}\n"
            "\n"
            "String userMessageForHttpStatus(int statusCode) {\n"
            "  switch (statusCode) {\n"
            "    case 401:\n"
            "      return 'Authentication failed (401). Pair this desktop app again or check the Backend URL in Settings.';\n"
            "    case 403:\n"
            "      return 'Access denied (403). Pair this desktop app again, or check that Settings points at your local CHILI backend.';\n"
            "    case 502:\n"
            "      return 'bad gateway';\n"
            "    default:\n"
            "      return 'Unexpected HTTP $statusCode from server.';\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        orchestrator._git(tmp_path, ["init"], timeout=60)
        run = ProjectAutonomyRun(
            run_id="pa_fallback_second",
            prompt="find a small enhancement for the desktop app",
            status="running",
            current_stage="implement",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(orchestrator, "select_local_model", lambda: {"model": "qwen", "available": True})
        monkeypatch.setattr(orchestrator.insights_mod, "get_insights", lambda *args, **kwargs: [])
        monkeypatch.setattr(
            orchestrator.ollama_client,
            "chat",
            lambda *args, **kwargs: SimpleNamespace(
                ok=True,
                text="```diff\n--- a/bad\n+++ b/bad\n@@ broken\n```",
                error=None,
                latency_ms=1,
            ),
        )

        diffs = orchestrator.generate_diffs_from_plan(
            db,
            run,
            tmp_path,
            [
                {
                    "path": "chili_mobile/lib/src/network/network_error_message.dart",
                    "description": "make a small desktop enhancement",
                }
            ],
        )

        assert diffs
        assert "HandshakeException" in diffs[0]
        assert "CERTIFICATE_VERIFY_FAILED" in diffs[0]
    finally:
        db.close()


def test_small_desktop_fallback_finds_third_network_enhancement(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo_file = tmp_path / "chili_mobile/lib/src/network/network_error_message.dart"
        repo_file.parent.mkdir(parents=True)
        repo_file.write_text(
            "String userVisibleNetworkError(Object error) {\n"
            "  final s = error.toString();\n"
            "  if (s.contains('HandshakeException') ||\n"
            "      s.contains('CERTIFICATE_VERIFY_FAILED')) {\n"
            "    return 'Secure connection failed. Check that the Backend URL uses the right http/https scheme and that any local certificate is trusted.';\n"
            "  }\n"
            "  if (s.contains('Connection refused')) {\n"
            "    return 'Connection refused.';\n"
            "  }\n"
            "  return s;\n"
            "}\n"
            "\n"
            "String userMessageForHttpStatus(int statusCode) {\n"
            "  switch (statusCode) {\n"
            "    case 401:\n"
            "      return 'Authentication failed (401). Pair this desktop app again or check the Backend URL in Settings.';\n"
            "    case 403:\n"
            "      return 'Access denied (403). Pair this desktop app again, or check that Settings points at your local CHILI backend.';\n"
            "    case 502:\n"
            "      return 'bad gateway';\n"
            "    default:\n"
            "      return 'Unexpected HTTP $statusCode from server.';\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        orchestrator._git(tmp_path, ["init"], timeout=60)
        run = ProjectAutonomyRun(
            run_id="pa_fallback_third",
            prompt="find a small enhancement for the desktop app",
            status="running",
            current_stage="implement",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(orchestrator, "select_local_model", lambda: {"model": "qwen", "available": True})
        monkeypatch.setattr(orchestrator.insights_mod, "get_insights", lambda *args, **kwargs: [])
        monkeypatch.setattr(
            orchestrator.ollama_client,
            "chat",
            lambda *args, **kwargs: SimpleNamespace(
                ok=True,
                text="```diff\n--- a/bad\n+++ b/bad\n@@ broken\n```",
                error=None,
                latency_ms=1,
            ),
        )

        diffs = orchestrator.generate_diffs_from_plan(
            db,
            run,
            tmp_path,
            [
                {
                    "path": "chili_mobile/lib/src/network/network_error_message.dart",
                    "description": "make a small desktop enhancement",
                }
            ],
        )

        assert diffs
        assert "FormatException" in diffs[0]
        assert "Unexpected character" in diffs[0]
        assert "login page or proxy error page" in diffs[0]
    finally:
        db.close()


def test_small_desktop_fallback_handles_non_json_chat_response(tmp_path):
    rel = "chili_mobile/lib/src/network/chili_api_client.dart"
    before = (
        "import 'network_error_message.dart' show userMessageForHttpStatus;\n"
        "void handle(response, decoded) {\n"
        "  final err = decoded?['error'] ?? decoded?['detail'] ?? response.body;\n"
        "  throw Exception(err is String ? err : 'HTTP ${response.statusCode}');\n"
        "}\n"
    )
    repo_file = tmp_path / rel
    repo_file.parent.mkdir(parents=True)
    repo_file.write_text(before, encoding="utf-8", newline="\n")
    orchestrator._git(tmp_path, ["init"], timeout=60)

    diff = orchestrator._deterministic_small_desktop_diff(
        rel,
        before,
        "find a small enhancement for the desktop app",
    )

    assert diff is not None
    assert "userMessageForHttpStatus(response.statusCode)" in diff
    proc = orchestrator._git(tmp_path, ["apply", "--check"], input_text=diff, timeout=60)
    assert proc.returncode == 0, proc.stderr


def _write_agentops_scorecard(root: Path, rel_path: str, body: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")


def _write_promotion_ready_agentops(root: Path) -> None:
    capabilities = ", ".join(orchestrator.AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES)
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
        "\n".join(
            [
                "# CHILI Coding Benchmark Scorecard",
                "",
                "- Generated UTC: " + datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "- Status: passed",
                "- Overall score: 100/100",
                "- Scenarios: 6",
                "- Pass rate: 6/6",
                "- Source stability: stable",
                "- Source changes during run: 0",
                "- Capability coverage: " + capabilities,
            ]
        ),
    )
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 7\n- Evidence mode: real_manifest\n",
    )
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
        "- Status: passed\n- Cases: 6\n- Evidence mode: real_artifacts\n",
    )
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 18\n- Evidence mode: real_inventory\n- Missing checks: none\n- Promotion eligible: true\n",
    )
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_SYNTHETIC_REPO_REPAIR_SCORECARD_REL_PATH,
        "- Status: passed\n",
    )
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH,
        "- Status: passed\n",
    )


def test_frontier_model_evidence_intake_status_tracks_required_source_files(tmp_path):
    manifest_path = tmp_path / orchestrator.AGENT_FRONTIER_PROMPT_PACK_MANIFEST_REL_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text('{"schema":"test"}\n', encoding="utf-8")
    codex_root = (
        tmp_path
        / orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_RAW_SOURCES_REL_PATH
        / "codex"
    )
    codex_raw = codex_root / "raw"
    codex_raw.mkdir(parents=True)
    for filename in orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_REQUIRED_SOURCE_FILES:
        (codex_root / filename).write_text("ok\n", encoding="utf-8")
    (codex_raw / "candidate.json").write_text("{}\n", encoding="utf-8")
    claude_root = (
        tmp_path
        / orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_RAW_SOURCES_REL_PATH
        / "claude"
    )
    claude_root.mkdir(parents=True)
    (claude_root / "metadata.json").write_text("{}\n", encoding="utf-8")

    status = orchestrator._frontier_model_evidence_intake_status(tmp_path)

    assert status["status"] == "partial"
    assert status["prompt_pack_manifest_present"] is True
    assert status["required_source_count"] == 3
    assert status["ready_source_count"] == 1
    assert status["prepared_source_count"] == 1
    assert status["missing_source_count"] == 2
    sources = {source["source_kind"]: source for source in status["sources"]}
    assert sources["codex"]["status"] == "ready"
    assert sources["codex"]["raw_drop_count"] == 1
    assert sources["claude"]["status"] == "partial"
    assert "raw_sources/claude/prompt_pack.md" in sources["claude"]["missing_files"][0]
    assert sources["local_model"]["status"] == "missing"
    assert "claude, local_model" in status["next_action"]
    assert status["setup_command"] == orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_SETUP_COMMAND
    assert status["setup_action_label"] == "Prepare frontier intake folders"
    assert status["setup_safe"] is True
    assert status["frontier_source_collection_packet_command"] == orchestrator.AGENT_FRONTIER_SOURCE_COLLECTION_PACKET_COMMAND
    assert status["frontier_source_collection_packet_action_label"] == "Build source collection packets"
    assert status["frontier_source_collection_packet_safe"] is True
    assert status["frontier_source_record_command"] == orchestrator.AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_COMMAND
    assert status["frontier_source_record_action_label"] == "Record frontier source evidence"
    assert status["frontier_source_record_safe"] is True
    assert (
        status["frontier_source_record_all_cases_command"]
        == orchestrator.AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_ALL_CASES_COMMAND
    )
    assert status["frontier_source_record_all_cases_action_label"] == "Record all-cases frontier source evidence"
    assert status["frontier_source_record_all_cases_safe"] is True
    assert status["local_model_candidate_run_command"] == orchestrator.AGENT_LOCAL_MODEL_CANDIDATE_RUN_COMMAND
    assert status["local_model_candidate_run_action_label"] == "Run local-model candidate suite"
    assert status["local_model_candidate_run_safe"] is True
    assert status["local_model_record_command"] == orchestrator.AGENT_LOCAL_MODEL_EVIDENCE_RECORD_COMMAND
    assert status["local_model_record_action_label"] == "Record local-model evidence"
    assert status["local_model_record_safe"] is True
    assert "no source/runtime/git/PR/live action" in status["permission_boundary"]


def test_frontier_model_evidence_intake_status_attaches_preflight_recovery_route(tmp_path):
    manifest_path = tmp_path / orchestrator.AGENT_FRONTIER_PROMPT_PACK_MANIFEST_REL_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text('{"schema":"test"}\n', encoding="utf-8")
    raw_root = tmp_path / orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_RAW_SOURCES_REL_PATH
    for source_kind in orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_SOURCE_KINDS:
        source_dir = raw_root / source_kind
        source_dir.mkdir(parents=True)
        (source_dir / "raw").mkdir()
        (source_dir / "prompt_pack.md").write_text(
            f"prepared {source_kind} prompt pack\n",
            encoding="utf-8",
        )
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_FRONTIER_EVIDENCE_PREFLIGHT_LIVE_REL_PATH,
        "\n".join(
            [
                "# CHILI Frontier Evidence Preflight",
                "",
                "- Schema: chili.frontier-evidence-preflight.v1",
                "- Status: warning",
                "- Checks: 1",
                "- Blockers: 1",
                "",
                "| Check | Status | Required | Actual | Evidence | Next action |",
                "| --- | --- | --- | --- | --- | --- |",
                "| claude_opus48_live_probe | warning | usable Claude completion | auth failure | claude -p | refresh auth |",
                "",
                "## Recovery Routes",
                "",
                "| Source | Blocker | Action | All-cases command | Single-case fallback | Boundary |",
                "| --- | --- | --- | --- | --- | --- |",
                (
                    "| claude | claude_opus48_live_probe | Import saved claude response | "
                    "python scripts/autopilot_frontier_source_evidence_recorder.py "
                    "--source-kind claude --all-cases --response <claude-all-cases-response.txt> "
                    "--run-id <real-claude-run-id> "
                    "--source-command <exact-claude-command-or-session-export> --json | "
                    "python scripts/autopilot_frontier_source_evidence_recorder.py "
                    "--source-kind claude --case-id real-chili-preflight-candidate-wins "
                    "--response <claude-response.txt> --run-id <real-claude-run-id> "
                    "--source-command <exact-claude-command-or-session-export> --json | "
                    "collection and evidence import only |"
                ),
            ]
        ),
    )

    status = orchestrator._frontier_model_evidence_intake_status(tmp_path)
    sources = {source["source_kind"]: source for source in status["sources"]}
    claude = sources["claude"]

    assert status["frontier_preflight_recovery_route_count"] == 1
    assert claude["status"] == "partial"
    assert claude["preflight_recovery_action_label"] == "Import saved claude response"
    assert "claude_all_cases_response.txt" in claude[
        "preflight_recovery_response_staging_file"
    ]
    assert "claude_all_cases_response.txt" in claude[
        "preflight_recovery_dry_run_command"
    ]
    assert "--json --no-write" in claude["preflight_recovery_dry_run_command"]
    assert "--source-kind claude" in claude["preflight_recovery_all_cases_command"]
    assert "--all-cases" in claude["preflight_recovery_all_cases_command"]
    assert "claude_all_cases_response.txt" in claude[
        "preflight_recovery_all_cases_command"
    ]
    assert "--no-write" not in claude["preflight_recovery_all_cases_command"]
    assert "--case-id real-chili-preflight-candidate-wins" in claude[
        "preflight_recovery_single_case_command"
    ]
    assert "claude_single_case_response.txt" in claude[
        "preflight_recovery_single_case_command"
    ]
    assert "--allow-partial --json --no-write" in claude[
        "preflight_recovery_validation_command"
    ]
    assert orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_RAW_SOURCES_REL_PATH in claude[
        "preflight_recovery_validation_command"
    ]
    assert "--publish-scorecards --json" in claude[
        "preflight_recovery_publish_command"
    ]
    assert "Preflight recovery: Import saved claude response" in claude["next_action"]
    assert "Save all-cases response to:" in claude["next_action"]
    assert "Dry-run import first:" in claude["next_action"]
    assert "After import validation:" in claude["next_action"]
    assert "Publish only when all sources are ready:" in claude["next_action"]
    assert "collection and evidence import only" in claude[
        "preflight_recovery_boundary"
    ]
    assert "Preflight recovery available for claude" in status["next_action"]
    assert "dry-run" in status["next_action"]
    assert "Validate after import" in status["next_action"]


def test_frontier_model_evidence_intake_status_guides_after_safe_setup(tmp_path):
    from scripts.autopilot_frontier_prompt_pack_bundle import build_prompt_pack_bundle

    bundle_root = tmp_path / orchestrator.AGENT_FRONTIER_PROMPT_PACK_MANIFEST_REL_PATH
    bundle_root = bundle_root.parent
    build_prompt_pack_bundle(output_dir=bundle_root)
    raw_root = tmp_path / orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_RAW_SOURCES_REL_PATH
    for source_kind in orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_SOURCE_KINDS:
        source_dir = raw_root / source_kind
        source_dir.mkdir(parents=True)
        (source_dir / "raw").mkdir()
        (source_dir / "prompt_pack.md").write_text(
            (bundle_root / source_kind / "prompt_pack.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    status = orchestrator._frontier_model_evidence_intake_status(tmp_path)

    assert status["status"] == "partial"
    assert status["prepared_source_count"] == 3
    assert status["ready_source_count"] == 0
    assert status["missing_source_count"] == 3
    assert "Record real metadata.json, transcript.jsonl" in status["next_action"]
    assert "codex, claude, local_model" in status["next_action"]
    assert "autopilot_frontier_source_collection_packet.py" in status["next_action"]
    assert "autopilot_frontier_source_evidence_recorder.py" in status["next_action"]
    assert "--all-cases" in status["next_action"]
    assert "complete hosted Codex/Claude/source drops" in status["next_action"]
    assert "only when a hosted source produced one case" in status["next_action"]
    assert "autopilot_local_model_candidate_runner.py" in status["next_action"]
    assert "autopilot_local_model_evidence_recorder.py" in status["next_action"]


def test_validation_merge_evidence_requires_non_collect_only_signal():
    collect_only = [
        {
            "step_key": "pytest_targeted",
            "exit_code": 0,
            "targeted": False,
            "fallback_collect_only": True,
            "command": "pytest --collect-only",
        }
    ]
    syntax_plus_collect = [
        {"step_key": "ast_syntax", "exit_code": 0, "changed_files": ["app/services/example.py"]},
        collect_only[0],
    ]

    assert orchestrator.validation_merge_evidence(collect_only, ["app/services/example.py"])["passed"] is False
    assert orchestrator.validation_merge_evidence(syntax_plus_collect, ["app/services/example.py"])["passed"] is True


def test_behavior_validation_evidence_requires_targeted_tests():
    syntax_only = [
        {"step_key": "ast_syntax", "exit_code": 0, "changed_files": ["app/services/example.py"]}
    ]
    targeted = [
        {
            "step_key": "pytest_targeted",
            "exit_code": 0,
            "targeted": True,
            "test_files": ["tests/test_example.py"],
            "validation_scope": "targeted_tests",
        }
    ]

    assert orchestrator.behavior_validation_evidence(syntax_only, ["app/services/example.py"])["passed"] is False
    assert orchestrator.behavior_validation_evidence(targeted, ["app/services/example.py"])["passed"] is True


def test_blast_radius_and_patch_self_review_gates_block_unplanned_or_large_changes():
    plan = {"files": [{"path": "app/services/example.py", "action": "modify"}]}

    assert orchestrator.change_blast_radius_gate(plan, ["app/services/example.py"])["passed"] is True
    assert orchestrator.change_blast_radius_gate(plan, ["app/services/other.py"])["passed"] is False
    assert (
        orchestrator.patch_self_review_gate(
            plan,
            ["app/services/example.py"],
            numstat_text="500\t240\tapp/services/example.py\n",
            name_status_text="M\tapp/services/example.py\n",
        )["passed"]
        is False
    )


def test_domain_behavior_validation_evidence_requires_trading_invariant_tests():
    generic = [
        {
            "step_key": "pytest_targeted",
            "exit_code": 0,
            "targeted": True,
            "test_files": ["tests/test_trading_runtime.py"],
            "test_selection": [
                {
                    "source_file": "app/services/trading/pdt_guard.py",
                    "test_file": "tests/test_trading_runtime.py",
                    "reason": "imports changed module",
                }
            ],
        }
    ]
    invariant = [
        {
            "step_key": "pytest_targeted",
            "exit_code": 0,
            "targeted": True,
            "test_files": ["tests/test_pdt_intraday_margin_cutover.py"],
            "test_selection": [
                {
                    "source_file": "app/services/trading/pdt_guard.py",
                    "test_file": "tests/test_pdt_intraday_margin_cutover.py",
                    "reason": "covers PDT day-trade margin invariant",
                }
            ],
        }
    ]

    assert orchestrator.domain_behavior_validation_evidence(generic, ["app/services/trading/pdt_guard.py"])["passed"] is False
    assert orchestrator.domain_behavior_validation_evidence(invariant, ["app/services/trading/pdt_guard.py"])["passed"] is True


def test_semantic_patch_review_blocks_public_contract_change():
    diff = "--- a/app/routers/orders.py\n+++ b/app/routers/orders.py\n@@\n-def create_order(payload):\n+def create_order(payload, user_id):\n"

    result = orchestrator.semantic_patch_review_gate(
        {"files": [{"path": "app/routers/orders.py"}]},
        ["app/routers/orders.py"],
        diff_text=diff,
        validation=[
            {
                "step_key": "pytest_targeted",
                "exit_code": 0,
                "targeted": True,
                "test_files": ["tests/test_orders_unit.py"],
            }
        ],
    )

    assert result["passed"] is False
    assert result["public_contract_change"] is True


def test_implementation_blocks_public_contract_change_without_contract_tests():
    diff = "--- a/app/routers/orders.py\n+++ b/app/routers/orders.py\n@@\n-def create_order(payload):\n+def create_order(payload, user_id):\n"

    result = orchestrator.semantic_patch_review_gate(
        {"files": [{"path": "app/routers/orders.py"}]},
        ["app/routers/orders.py"],
        diff_text=diff,
        validation=[
            {
                "step_key": "pytest_targeted",
                "exit_code": 0,
                "targeted": True,
                "test_files": ["tests/test_orders_api_contract.py"],
            }
        ],
    )

    assert result["passed"] is True
    assert "tests/test_orders_api_contract.py" in result["contract_tests"]


def test_validation_repair_context_text_preserves_targeted_failure():
    context = orchestrator.validation_repair_context(
        [
            {
                "step_key": "pytest_targeted",
                "exit_code": 1,
                "command": "pytest tests/test_example.py -q",
                "stdout": "FAILED tests/test_example.py::test_behavior",
                "test_files": ["tests/test_example.py"],
            }
        ],
        changed_files=["app/services/example.py"],
        plan_files=[{"path": "app/services/example.py"}],
    )

    text = orchestrator.validation_repair_context_text(context)

    assert "schema: chili.validation-repair-context.v1" in text
    assert "pytest tests/test_example.py -q" in text
    assert "FAILED tests/test_example.py::test_behavior" in text


def test_run_needs_visual_qa_for_visible_ui_plan():
    run = SimpleNamespace(status=orchestrator.RUN_STATUS_COMPLETED, prompt="Make the screen easier to scan.")
    plan = {"files": [{"path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"}]}

    assert orchestrator._run_needs_visual_qa(run, plan) is True
    assert orchestrator._run_needs_visual_qa(run, {"files": [{"path": "app/services/example.py"}]}) is False


def test_coding_benchmark_scorecard_rejects_model_shadow_self_test_mode(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 7\n- Evidence mode: self_test\n",
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["model_shadow"]["evidence_mode"] == "self_test"
    assert "real shadow evidence" in signal["frontier_evidence_gap_labels"]


def test_coding_benchmark_scorecard_rejects_hosted_pr_repair_self_test_mode(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 18\n- Evidence mode: self_test\n- Missing checks: none\n- Promotion eligible: false\n",
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["hosted_pr_repair"]["evidence_mode"] == "self_test"
    assert signal["hosted_pr_repair"]["metadata_values"]["promotion eligible"] == "false"
    assert "real PR repair inventory" in signal["frontier_evidence_gap_labels"]


def test_coding_benchmark_scorecard_rejects_hosted_pr_repair_without_promotion_flag(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 18\n- Evidence mode: real_inventory\n- Missing checks: none\n- Promotion eligible: false\n",
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["hosted_pr_repair"]["evidence_mode"] == "real_inventory"
    assert signal["hosted_pr_repair"]["metadata_values"]["promotion eligible"] == "false"


def test_coding_benchmark_scorecard_rejects_source_changes_after_generation(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    changed_file = tmp_path / "app" / "services" / "current_change.py"
    changed_file.parent.mkdir(parents=True)
    changed_file.write_text("VALUE = 1\n", encoding="utf-8")
    os.utime(changed_file, (4102444800, 4102444800))

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["scorecard_freshness"] == "stale"
    assert signal["source_changes_after_scorecard"] == 1
    assert "source freshness" in signal["frontier_evidence_gap_labels"]
    assert "app/services/current_change.py" in signal["source_change_preview_after_scorecard"]
    assert "changed after scorecard generation" in signal["detail"]
    assert (
        signal["source_churn_diagnostics_path"]
        == orchestrator.AGENT_SOURCE_CHURN_DIAGNOSTICS_REL_PATH
    )
    assert "autopilot_source_churn_diagnostics.py" in signal[
        "source_churn_diagnostics_command"
    ]
    source_gap = next(
        gap for gap in signal["frontier_evidence_gaps"] if gap["gate"] == "source_freshness"
    )
    assert "autopilot_source_churn_diagnostics.py" in source_gap["next_action"]
    assert signal["source_churn_diagnostics"]["status"] == "missing"


def test_coding_benchmark_signal_uses_latest_source_churn_diagnostics(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    changed_file = tmp_path / "app" / "services" / "current_change.py"
    changed_file.parent.mkdir(parents=True)
    changed_file.write_text("VALUE = 1\n", encoding="utf-8")
    os.utime(changed_file, (4102444800, 4102444800))
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_SOURCE_CHURN_DIAGNOSTICS_REL_PATH,
        "\n".join(
            [
                "# CHILI Source Churn Diagnostics",
                "",
                "- Schema: chili.source-churn-diagnostics.v1",
                "- Generated UTC: 2026-06-03T13:42:27Z",
                "- Status: warning",
                "- Promotion impact: blocked",
                "- Rerun readiness: ready_for_benchmark_rerun",
                "- Scorecard: project_ws/AgentOps/CODING_BENCHMARK_SCORECARD.md",
                "- Scorecard status: failed",
                "- Scorecard generated UTC: 2026-06-03T13:23:01Z",
                "- Scorecard source stability: changed",
                "- Source changes during scorecard: 12",
                "- Current source freshness: stale",
                "- Source changes after scorecard: 1",
                "- Watch status: stable",
                "- Watch seconds: 5.0",
                "- Source changes during watch: 0",
                "- Next action: The tree was quiet during this diagnostic window; rerun the full coding benchmark with a source quiet preflight.",
                "- Safety: read-only source/test diagnostics only",
                "",
                "## Files Newer Than Scorecard",
                "",
                "| Path | Modified UTC | Seconds after scorecard | Size |",
                "| --- | --- | ---: | ---: |",
                "| app/services/current_change.py | 2026-06-03T13:40:23Z | 1041.893 | 10 |",
                "",
                "## Files Changed During Watch",
                "",
                "| Path | Change | Before UTC | After UTC | Before size | After size |",
                "| --- | --- | --- | --- | ---: | ---: |",
                "| none |  |  |  |  |  |",
            ]
        ),
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    diagnostics = signal["source_churn_diagnostics"]
    assert diagnostics["present"] is True
    assert diagnostics["status"] == "warning"
    assert diagnostics["rerun_readiness"] == "ready_for_benchmark_rerun"
    assert diagnostics["watch_status"] == "stable"
    assert diagnostics["changed_files"] == ["app/services/current_change.py"]
    assert "current_change.py" in diagnostics["changed_file_preview"]
    source_gap = next(
        gap for gap in signal["frontier_evidence_gaps"] if gap["gate"] == "source_freshness"
    )
    assert source_gap["path"] == orchestrator.AGENT_SOURCE_CHURN_DIAGNOSTICS_REL_PATH
    assert "diagnostic warning" in source_gap["actual"]
    assert "rerun ready_for_benchmark_rerun" in source_gap["actual"]
    assert "watch stable" in source_gap["actual"]
    assert "Latest diagnostic" in source_gap["next_action"]
    assert "tree was quiet" in source_gap["next_action"]
    handoff = signal["frontier_evidence_handoff_copy"]
    assert orchestrator.AGENT_SOURCE_CHURN_DIAGNOSTICS_REL_PATH in handoff
    assert "diagnostic changed files: app/services/current_change.py" in handoff


def test_coding_benchmark_signal_names_frontier_evidence_gaps_for_operator(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 7\n- Evidence mode: self_test\n",
    )
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
        "- Status: passed\n- Cases: 6\n",
    )
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 18\n- Evidence mode: self_test\n- Missing checks: none\n- Promotion eligible: false\n",
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["frontier_evidence_gap_count"] == 3
    assert signal["frontier_evidence_gap_labels"] == [
        "real shadow evidence",
        "real tournament artifacts",
        "real PR repair inventory",
    ]
    assert signal["frontier_evidence_gaps"][0]["required"] == "real_manifest"
    assert signal["frontier_evidence_gaps"][1]["actual"] == "missing"
    assert "promotion eligible false" in signal["frontier_evidence_gaps"][2]["actual"]
    assert "Close source intake first" in signal["frontier_evidence_next_action"]
    assert "autopilot_frontier_model_evidence_intake.py" in signal[
        "frontier_evidence_next_action"
    ]
    assert "--publish-scorecards --json" in signal["frontier_evidence_next_action"]
    assert signal["frontier_evidence_handoff_label"] == "Copy frontier proof packet"
    intake = signal["frontier_model_evidence_intake"]
    assert intake["status"] == "missing"
    assert intake["required_source_count"] == 3
    assert intake["ready_source_count"] == 0
    assert intake["raw_source_root"] == orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_RAW_SOURCES_REL_PATH
    assert intake["setup_command"] == orchestrator.AGENT_FRONTIER_MODEL_EVIDENCE_SETUP_COMMAND
    assert intake["frontier_source_collection_packet_command"] == orchestrator.AGENT_FRONTIER_SOURCE_COLLECTION_PACKET_COMMAND
    assert intake["frontier_source_record_command"] == orchestrator.AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_COMMAND
    assert (
        intake["frontier_source_record_all_cases_command"]
        == orchestrator.AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_ALL_CASES_COMMAND
    )
    assert intake["local_model_candidate_run_command"] == orchestrator.AGENT_LOCAL_MODEL_CANDIDATE_RUN_COMMAND
    assert intake["local_model_record_command"] == orchestrator.AGENT_LOCAL_MODEL_EVIDENCE_RECORD_COMMAND
    handoff = signal["frontier_evidence_handoff_copy"]
    assert "Project Autopilot frontier evidence proof packet" in handoff
    assert "real_manifest" in handoff
    assert "real_artifacts" in handoff
    assert "real_inventory" in handoff
    assert "MODEL_SHADOW_EVIDENCE_BENCHMARK.md" in handoff
    assert "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md" in handoff
    assert orchestrator.AGENT_FRONTIER_PROMPT_PACK_MANIFEST_REL_PATH in handoff
    assert "raw_sources/codex/metadata.json" in handoff
    assert "raw_sources/claude/transcript.jsonl" in handoff
    assert "raw_sources/local_model/prompt_pack.md" in handoff
    assert "autopilot_frontier_model_evidence_setup.py --json" in handoff
    assert "autopilot_frontier_source_collection_packet.py" in handoff
    assert "autopilot_frontier_source_evidence_recorder.py" in handoff
    assert "--all-cases" in handoff
    assert "autopilot_local_model_candidate_runner.py" in handoff
    assert "autopilot_local_model_evidence_recorder.py" in handoff
    assert "autopilot_frontier_prompt_pack_bundle.py --validate --json" in handoff
    assert "--input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources" in handoff
    assert "--publish-scorecards --json" in handoff
    assert "--manifest-dir project_ws/AgentOps/frontier_model_evidence_intake/manifests" in handoff
    assert "--drop-dir project_ws/AgentOps/frontier_model_evidence_intake/collected" in handoff
    assert "--require-provenance --no-write --json" in handoff
    assert "does not authorize source/test edits" in handoff


def test_coding_benchmark_signal_surfaces_local_model_candidate_timeout_recovery(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
        "- Status: passed\n- Cases: 6\n",
    )
    diagnostics_rel = (
        "project_ws/AgentOps/local_model_candidate_runs/"
        "local-suite-timeout/suite_diagnostics.json"
    )
    prompt_rel = (
        "project_ws/AgentOps/local_model_candidate_runs/local-suite-timeout/"
        "cases/real-chili-preflight-candidate-wins.prompt.md"
    )
    diagnostics_path = tmp_path / diagnostics_rel
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    retry_command = (
        "python scripts/autopilot_local_model_candidate_runner.py "
        f"--retry-from-diagnostics {diagnostics_rel} "
        "--timeout-seconds 300 --json"
    )
    import_command = (
        "python scripts/autopilot_local_model_candidate_runner.py "
        f"--retry-from-diagnostics {diagnostics_rel} "
        "--response-file <local-model-real-chili-preflight-candidate-wins-response.txt> "
        "--run-id <real-local-run-id> "
        "--source-command <exact-local-model-command> --json"
    )
    diagnostics_path.write_text(
        json.dumps(
            {
                "schema": "chili.local-model-suite-diagnostics.v1",
                "status": "failed",
                "failure_stage": "model",
                "failure_reason": (
                    "real-chili-preflight-candidate-wins: local model timed out after 60s"
                ),
                "failed_case_id": "real-chili-preflight-candidate-wins",
                "case_results": [
                    {
                        "case_id": "real-chili-preflight-candidate-wins",
                        "status": "model_failed",
                        "prompt": prompt_rel,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_LOCAL_MODEL_CANDIDATE_RUN_REL_PATH,
        "\n".join(
            [
                "# CHILI Local Model Candidate Run",
                "",
                "- Status: failed",
                "- Case: all",
                "- Cases: 6",
                "- Model: qwen3:4b",
                "- Failure stage: model",
                (
                    "- Failure reason: real-chili-preflight-candidate-wins: "
                    "local model timed out after 60s"
                ),
                "- Failed case: real-chili-preflight-candidate-wins",
                "",
                "| Artifact | Path |",
                "| --- | --- |",
                "| full_prompt_pack | project_ws/AgentOps/local_model_candidate_runs/local-suite-timeout/full_prompt_pack.md |",
                "| response | project_ws/AgentOps/local_model_candidate_runs/local-suite-timeout/model_response.txt |",
                f"| diagnostics | {diagnostics_rel} |",
            ]
        ),
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert "real tournament artifacts" in signal["frontier_evidence_gap_labels"]
    assert "local model candidate diagnostics" in signal["frontier_evidence_gap_labels"]
    intake = signal["frontier_model_evidence_intake"]
    local_run = intake["local_model_candidate_run"]
    assert local_run["status"] == "failed"
    assert local_run["failed_case_id"] == "real-chili-preflight-candidate-wins"
    assert local_run["diagnostics"] == diagnostics_rel
    assert local_run["full_prompt_pack"].endswith("full_prompt_pack.md")
    assert local_run["response"].endswith("model_response.txt")
    assert local_run["failed_prompt"] == prompt_rel
    assert local_run["recovery_route_count"] == 1
    assert local_run["recovery_routes"][0]["prompt_path"] == prompt_rel
    assert retry_command in local_run["next_action"]
    assert import_command in local_run["next_action"]
    local_gap = next(
        gap
        for gap in signal["frontier_evidence_gaps"]
        if gap["gate"] == "local_model_candidate_run"
    )
    assert "local model timed out after 60s" in local_gap["actual"]
    assert retry_command in local_gap["next_action"]
    assert local_gap["path"] == diagnostics_rel
    assert retry_command in signal["frontier_evidence_handoff_copy"]
    assert diagnostics_rel in signal["frontier_evidence_handoff_copy"]
    assert "does not authorize source/test edits" in signal["frontier_evidence_handoff_copy"]


def test_coding_benchmark_signal_surfaces_local_model_timeout_salvage(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    salvaged_case = "real-chili-preflight-candidate-wins"
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_LOCAL_MODEL_CANDIDATE_RUN_REL_PATH,
        "\n".join(
            [
                "# CHILI Local Model Candidate Run",
                "",
                "- Status: passed",
                "- Case: all",
                "- Cases: 6",
                "- Model: qwen3:4b",
                "- Run id: local-suite-timeout-salvage",
                "- Promotion ready: False",
                "- Timeout salvaged count: 1",
                f"- Timeout salvaged cases: {salvaged_case}",
                "",
                "| Artifact | Path |",
                "| --- | --- |",
                "| full_prompt_pack | project_ws/AgentOps/local_model_candidate_runs/local-suite-timeout-salvage/full_prompt_pack.md |",
                "| response | project_ws/AgentOps/local_model_candidate_runs/local-suite-timeout-salvage/model_response.txt |",
            ]
        ),
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    local_run = signal["frontier_model_evidence_intake"]["local_model_candidate_run"]
    assert local_run["status"] == "passed"
    assert local_run["timeout_salvaged_case_count"] == 1
    assert local_run["timeout_salvaged_cases"] == [salvaged_case]


def test_coding_benchmark_signal_surfaces_frontier_preflight_recovery_routes(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 7\n- Evidence mode: self_test\n",
    )
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_FRONTIER_EVIDENCE_PREFLIGHT_LIVE_REL_PATH,
        "\n".join(
            [
                "# CHILI Frontier Evidence Preflight",
                "",
                "- Schema: chili.frontier-evidence-preflight.v1",
                "- Generated UTC: 2026-06-03T11:13:01Z",
                "- Status: warning",
                "- Checks: 15",
                "- Blockers: 1",
                "",
                "| Check | Status | Required | Actual | Evidence | Next action |",
                "| --- | --- | --- | --- | --- | --- |",
                (
                    "| claude_opus48_live_probe | warning | Claude Opus 4.8 print mode "
                    "can return a usable completion | exit_code=1; auth_failure_detected=true "
                    "| claude -p ... | refresh auth |"
                ),
                "",
                "## Recovery Routes",
                "",
                "| Source | Blocker | Action | All-cases command | Single-case fallback | Boundary |",
                "| --- | --- | --- | --- | --- | --- |",
                (
                    "| claude | claude_opus48_live_probe | Import saved claude response | "
                    "python scripts/autopilot_frontier_source_evidence_recorder.py "
                    "--source-kind claude --all-cases --response <claude-all-cases-response.txt> "
                    "--run-id <real-claude-run-id> "
                    "--source-command <exact-claude-command-or-session-export> --json | "
                    "python scripts/autopilot_frontier_source_evidence_recorder.py "
                    "--source-kind claude --case-id real-chili-preflight-candidate-wins "
                    "--response <claude-response.txt> --run-id <real-claude-run-id> "
                    "--source-command <exact-claude-command-or-session-export> --json | "
                    "collection and evidence import only |"
                ),
            ]
        ),
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    preflight = signal["frontier_evidence_preflight"]
    assert preflight["status"] == "warning"
    assert preflight["path"] == orchestrator.AGENT_FRONTIER_EVIDENCE_PREFLIGHT_LIVE_REL_PATH
    assert preflight["blocker_ids"] == ["claude_opus48_live_probe"]
    assert "Source" not in preflight["blocker_ids"]
    assert "claude" not in preflight["blocker_ids"]
    assert preflight["recovery_route_count"] == 1
    route = preflight["recovery_routes"][0]
    assert route["source_kind"] == "claude"
    assert route["blocker_id"] == "claude_opus48_live_probe"
    assert route["action_label"] == "Import saved claude response"
    assert "autopilot_frontier_source_collection_packet.py" in route[
        "collection_packet_command"
    ]
    assert "--source-kind claude" in route["response_import_command"]
    assert "--all-cases" in route["response_import_command"]
    assert "claude_all_cases_response.txt" in route["response_staging_file"]
    assert "claude_all_cases_response.txt" in route["response_import_command"]
    assert "claude_all_cases_response.txt" in route["dry_run_response_import_command"]
    assert "--json --no-write" in route["dry_run_response_import_command"]
    assert route["response_import_command"] == route["all_cases_response_import_command"]
    assert "--case-id real-chili-preflight-candidate-wins" in route[
        "single_case_response_import_command"
    ]
    assert "claude_single_case_response.txt" in route[
        "single_case_response_import_command"
    ]
    assert "--case-id real-chili-preflight-candidate-wins" not in route[
        "response_import_command"
    ]
    assert signal["frontier_preflight_recovery_route_count"] == 1
    assert signal["frontier_preflight_recovery_routes"] == preflight["recovery_routes"]
    handoff = signal["frontier_evidence_handoff_copy"]
    assert "Hosted-source preflight recovery routes" in handoff
    assert "Import saved claude response" in handoff
    assert "autopilot_frontier_source_evidence_recorder.py" in handoff
    assert "Save all-cases response to" in handoff
    assert "Dry-run response import" in handoff
    assert "All-cases response import" in handoff
    assert "claude_all_cases_response.txt" in handoff
    assert "Single-case fallback" in handoff
    assert "does not run models" in handoff


def test_coding_benchmark_signal_surfaces_hosted_pr_repair_candidate_reports(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 18\n- Evidence mode: self_test\n- Missing checks: none\n- Promotion eligible: false\n",
    )
    _write_agentops_scorecard(
        tmp_path,
        "project_ws/AgentOps/PR_282_CI_REPAIR.md",
        "\n".join(
            [
                "# PR 282 CI Repair Evidence",
                "",
                "- Schema: chili.hosted-pr-local-repair-evidence.v1",
                "- Generated UTC: 2026-06-03T10:40:00Z",
                "- Updated UTC: 2026-06-03T11:12:00Z",
                "- PR: https://github.com/MiacoRindolf/chili-home-copilot/pull/282",
                "- Branch: codex/stock-momentum-context-gate",
                "- Head SHA inspected: 6350638afc6f8624d6635f22669f1a28ce02136f",
                "- Current head SHA observed: 6160d0f82d749fc04d0f74ea7030d2fd482b3e6d",
                "- Hosted run inspected: 26877331577",
                "- Current hosted green run observed: 26879809423",
                "- Evidence status: local_repair_verified; current hosted check success observed",
                "- Promotion status: not real_inventory; publication/current-head proof has not been replayed through the transcript-bound hosted PR repair artifact contract.",
                "",
                "## Remaining Hosted Evidence",
                "",
                "- bind the publication/current-head proof to transcript-bound real inventory;",
                "- collect or archive post-repair PR/check status bound to the repaired commit;",
                "- replay those artifacts through scripts/autopilot_hosted_pr_repair_artifact_benchmark.py in real_inventory mode.",
            ]
        ),
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    candidates = signal["hosted_pr_repair_candidates"]
    assert candidates["status"] == "candidate_reports_present"
    assert candidates["candidate_count"] == 1
    assert signal["hosted_pr_repair_candidate_report_count"] == 1
    latest = candidates["latest"]
    assert latest["path"] == "project_ws/AgentOps/PR_282_CI_REPAIR.md"
    assert latest["pr_url"].endswith("/pull/282")
    assert latest["current_head_sha_observed"] == "6160d0f82d749fc04d0f74ea7030d2fd482b3e6d"
    assert latest["current_hosted_green_run_observed"] == "26879809423"
    assert "current hosted check success observed" in latest["evidence_status"]
    assert candidates["missing_evidence_count"] == 3
    assert "publication/current-head proof" in candidates["missing_evidence"][0]
    assert "autopilot_hosted_pr_repair_artifact_benchmark.py" in candidates[
        "validation_command"
    ]
    assert "autopilot_hosted_pr_repair_collection_packet.py" in candidates[
        "collection_packet_command"
    ]
    assert "--candidate-report project_ws/AgentOps/PR_282_CI_REPAIR.md" in candidates[
        "collection_packet_command"
    ]
    assert "autopilot_hosted_pr_repair_evidence_collector.py" in candidates[
        "evidence_collector_command"
    ]
    assert "--candidate-report project_ws/AgentOps/PR_282_CI_REPAIR.md" in candidates[
        "evidence_collector_command"
    ]
    assert "autopilot_hosted_pr_repair_artifact_assembler.py" in candidates[
        "artifact_assembler_command"
    ]
    assert "--candidate-report project_ws/AgentOps/PR_282_CI_REPAIR.md" in candidates[
        "artifact_assembler_command"
    ]
    assert candidates["collection_packet_action_label"] == (
        "Build hosted PR repair collection packet"
    )
    assert candidates["collection_packet_safe"] is True
    assert candidates["evidence_collector_action_label"] == (
        "Collect hosted PR repair evidence"
    )
    assert candidates["evidence_collector_safe"] is True
    assert candidates["artifact_assembler_action_label"] == (
        "Assemble hosted PR repair artifact"
    )
    assert candidates["artifact_assembler_safe"] is True
    handoff = signal["frontier_evidence_handoff_copy"]
    assert "Hosted PR repair candidate reports" in handoff
    assert "PR_282_CI_REPAIR.md" in handoff
    assert "26879809423" in handoff
    assert "autopilot_hosted_pr_repair_collection_packet.py" in handoff
    assert "autopilot_hosted_pr_repair_evidence_collector.py" in handoff
    assert "autopilot_hosted_pr_repair_artifact_assembler.py" in handoff
    assert "transcript-bound hosted PR repair artifacts" in handoff
    assert "no git/PR mutation" in handoff


def test_coding_benchmark_scorecard_requires_model_promotion_gate(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    (tmp_path / orchestrator.AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH).unlink()

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert orchestrator.AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH in signal["detail"]


def test_autopilot_quality_bar_requires_coding_benchmark_scorecard(tmp_path):
    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["score"] == 0
    assert "missing" in signal["detail"].lower()


def test_coding_benchmark_scorecard_can_satisfy_quality_gate(tmp_path):
    _write_promotion_ready_agentops(tmp_path)

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
    assert signal["promotion_status"] == "passed"
    assert signal["selected_scenarios_status"] == "passed"
    assert signal["promotion_scope"] == "full"
    assert signal["score"] == 100
    assert signal["passed_count"] == signal["scenario_count"]
    assert signal["frontier_evidence_gap_count"] == 0
    assert signal["frontier_evidence_gap_labels"] == []
    assert signal["frontier_evidence_handoff_copy"] == ""


def test_coding_benchmark_signal_marks_selected_smoke_only(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    capabilities = ", ".join(orchestrator.AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
        "\n".join(
            [
                "# CHILI Coding Benchmark Scorecard",
                "",
                "- Profile: custom",
                "- Generated UTC: " + datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "- Status: failed",
                "- Selected scenarios status: passed",
                "- Overall score: 100/100",
                "- Scenarios: 1",
                "- Pass rate: 1/1",
                "- Source stability: stable",
                "- Source changes during run: 0",
                "- Capability coverage: " + capabilities,
            ]
        ),
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["promotion_status"] == "failed"
    assert signal["selected_scenarios_status"] == "passed"
    assert signal["selected_scenario_passed_only"] is True
    assert signal["promotion_scope"] == "selected_smoke_only"
    assert signal["profile"] == "custom"
    assert "selected scenarios passed only" in signal["detail"]


def test_coding_benchmark_signal_marks_unstable_full_evidence(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    capabilities = ", ".join(orchestrator.AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
        "\n".join(
            [
                "# CHILI Coding Benchmark Scorecard",
                "",
                "- Profile: core",
                "- Generated UTC: " + datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "- Status: failed",
                "- Selected scenarios status: passed",
                "- Overall score: 100/100",
                "- Scenarios: 56",
                "- Pass rate: 56/56",
                "- Source stability: changed",
                "- Source changes during run: 4",
                "- Source change preview: app/services/project_autonomy/orchestrator.py",
                "- Capability coverage: " + capabilities,
            ]
        ),
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["promotion_status"] == "failed"
    assert signal["selected_scenarios_status"] == "passed"
    assert signal["selected_scenario_passed_only"] is False
    assert signal["promotion_scope"] == "unstable_full_evidence"
    assert signal["profile"] == "core"
    assert signal["pass_rate"] == "56/56"
    assert signal["effective_pass_rate"] == "56/56"
    assert signal["missing_capabilities"] == []
    assert "selected scenarios passed only" not in signal["detail"]
    assert "source/test files changed during benchmark run" in signal["detail"]


def test_coding_benchmark_signal_surfaces_runner_environment_issues(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    capabilities = ", ".join(orchestrator.AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES)
    recovery = (
        "wait for active benchmark/build workers to drain, then rerun the same "
        "scenario before judging coding quality"
    )
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
        "\n".join(
            [
                "# CHILI Coding Benchmark Scorecard",
                "",
                "- Generated UTC: " + datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "- Status: failed",
                "- Selected scenarios status: failed",
                "- Overall score: 100/100",
                "- Scenarios: 6",
                "- Pass rate: 6/6",
                "- Runner/environment issues: 1",
                f"- Runner/environment recovery: {recovery}",
                "- Source stability: stable",
                "- Source changes during run: 0",
                "- Capability coverage: " + capabilities,
            ]
        ),
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["runner_environment_issues"] == 1
    assert signal["runner_environment_recovery"] == recovery
    assert "runner/environment issue(s) require rerun" in signal["detail"]
    assert "before judging coding quality" in signal["detail"]


def test_coding_benchmark_signal_treats_repaired_replay_rows_as_clean(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_SYNTHETIC_REPO_REPAIR_SCORECARD_REL_PATH,
        "- Status: passed\n\n| case | status |\n| --- | --- |\n| repaired-scope | repaired |\n",
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED


def test_coding_benchmark_signal_uses_newer_repaired_failed_rows(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    missing_capability = "hosted pr repair evidence collector"
    capabilities = ", ".join(
        capability
        for capability in orchestrator.AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES
        if capability != missing_capability
    )
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
        "\n".join(
            [
                "# CHILI Coding Benchmark Scorecard",
                "",
                "- Generated UTC: 2026-06-03T06:00:00Z",
                "- Status: failed",
                "- Overall score: 91/100",
                "- Scenarios: 6",
                "- Pass rate: 3/6",
                "- Source stability: stable",
                "- Source changes during run: 0",
                "- Capability coverage: " + capabilities,
                "",
                "| Scenario ID | Scenario | Result |",
                "| --- | --- | --- |",
                "| stale-behavior | Behavior evidence | failed |",
                "| stale-review | Semantic review | failed |",
                "| stale-env | Worktree isolation | environment_blocked |",
            ]
        ),
    )
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_CODING_BENCHMARK_REPAIRED_ROWS_REL_PATH,
        "\n".join(
            [
                "# CHILI Coding Benchmark Scorecard",
                "",
                "- Generated UTC: 2026-06-03T06:30:00Z",
                "- Status: failed",
                "- Selected scenarios status: passed",
                "- Overall score: 100/100",
                "- Scenarios: 3",
                "- Pass rate: 3/3",
                "- Source stability: changed",
                "- Source changes during run: 1",
                "",
                "| Scenario ID | Scenario | Capability | Result |",
                "| --- | --- | --- | --- |",
                (
                    "| stale-behavior | Behavior evidence | targeted behavior evidence gate | "
                    "passed |"
                ),
                (
                    "| stale-review | Semantic review | "
                    f"semantic patch review gate, {missing_capability} | passed |"
                ),
                "| stale-env | Worktree isolation | execution worktree isolation | passed |",
            ]
        ),
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["pass_rate"] == "3/6"
    assert signal["effective_pass_rate"] == "6/6"
    assert signal["repaired_failed_rows"]["covers_all_failed_rows"] is True
    assert signal["repaired_failed_rows"]["covered_ids"] == [
        "stale-behavior",
        "stale-env",
        "stale-review",
    ]
    assert signal["missing_capabilities"] == []
    assert signal["primary_missing_capabilities"] == [missing_capability]
    assert signal["covered_missing_capabilities"] == [missing_capability]
    assert signal["repaired_failed_rows"]["covers_all_missing_capabilities"] is True
    assert "not all scenarios passed" not in signal["detail"]
    assert "missing required capability coverage" not in signal["detail"]
    assert "pass rate 6/6 (raw 3/6)" in signal["detail"]
    assert "stale failed scenarios repaired" in signal["detail"]
    assert "Targeted repair also covered missing capability coverage" in signal["detail"]


def test_agent_os_readiness_operator_inbox_names_goal_receipt_quality(tmp_path):
    _write_promotion_ready_agentops(tmp_path)

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
    assert "required capability coverage is present" in signal["detail"]


def test_coding_replay_debt_inbox_item_surfaces_source_guard_routes(tmp_path):
    import importlib.util
    import sys

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "autopilot_replay_debt_router.py"
    scripts_dir = str(script_path.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("autopilot_replay_debt_router", script_path)
    assert spec is not None
    assert spec.loader is not None
    router = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = router
    spec.loader.exec_module(router)

    items = [
        router.ReplayDebtItem(
            source="report_replay",
            path=f"project_ws/AgentOps/OUT/20260602-0{index}0000Z-thin.md",
            agent="AgentOps",
            score=74,
            status="warning",
            missing=("pursuing goal or scope anchor", "evidence check marker"),
            sha256=str(index) * 64,
            evidence_markers=(),
        )
        for index in (1, 2)
    ]

    route = router.build_routes(
        items,
        root=tmp_path,
        created_utc="2026-06-02T02:10:00Z",
        write=False,
    )[0]

    assert route.source_guard_required is True
    assert route.coordination_resolution == "durable_source_guard_required"
    assert "## Recurrence Guard" in route.request_markdown
    assert "Required source guard:" in route.request_markdown
    assert "No source, runtime, git, database, broker" in route.request_markdown


def test_validation_policy_blocker_reaches_global_readiness_actions():
    validation = [
        {
            "step_key": "npm_run_build",
            "exit_code": 0,
            "blocker": {"can_rerun": False, "raw_reason": "Policy-blocked script tried to write outside the repo."},
        }
    ]

    blocker = validation[0]["blocker"]

    assert blocker["can_rerun"] is False
    assert orchestrator.validation_merge_evidence(validation, ["app/services/example.py"])["passed"] is False


def test_runtime_control_blocked_run_guides_review_instead_of_rerun():
    prompt = "Start live trading now and keep buying while I am away."

    assert orchestrator._looks_like_live_monitoring_prompt(prompt) is True
    assert orchestrator._looks_like_plan_start_prompt(prompt) is False


def test_runtime_control_prompt_is_not_treated_as_repo_edit():
    prompt = "Start live trading now and keep buying while I am away."

    assert orchestrator._looks_like_live_monitoring_prompt(prompt) is True
    assert orchestrator._looks_like_plan_start_prompt(prompt) is False
    reply = orchestrator._initial_chat_reply(prompt)
    assert "live monitoring/debugging request" in reply
    assert "won't scan or edit the repo" in reply


def test_planning_phase_blocks_runtime_control_prompt_before_model(monkeypatch, tmp_path):
    calls: list[str] = []

    class FakeDb:
        def commit(self) -> None:
            pass

    run = SimpleNamespace(
        run_id="pa_live_control",
        prompt="Start live trading now and keep buying while I am away.",
        status=None,
        plan_status=None,
        started_at=None,
    )
    repo = SimpleNamespace(name="repo")

    monkeypatch.setattr(orchestrator, "_record_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_check_cancel", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_ensure_git_repo", lambda *args, **kwargs: calls.append("git"))
    monkeypatch.setattr(
        orchestrator,
        "_build_reviewed_plan",
        lambda *args, **kwargs: calls.append("model"),
    )

    with pytest.raises(orchestrator.AutonomyBlocked) as exc:
        orchestrator._run_planning_phase(FakeDb(), run, repo, tmp_path)

    assert "live monitoring/debugging request" in str(exc.value)
    assert calls == []


def test_manual_merge_fails_closed_for_non_completed_runs(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        row = ProjectAutonomyRun(
            run_id="pa_merge_blocked_running",
            user_id=1,
            prompt="Change a backend helper.",
            status=orchestrator.RUN_STATUS_RUNNING,
            current_stage=orchestrator.STAGE_IMPLEMENT,
            repo_id=None,
            integration_branch="autopilot/test",
            files_json=json.dumps(["app/services/example.py"]),
        )
        db.add(row)
        db.commit()

        payload = orchestrator.merge_run(db, row.run_id, user_id=1)

        assert payload is not None
        assert payload["merge_status"] == "blocked"
        assert "completes validation" in payload["merge_message"]
        assert payload["merge_result"]["ok"] is False
    finally:
        db.close()
