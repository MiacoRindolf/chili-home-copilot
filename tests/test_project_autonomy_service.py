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
    ProjectAutonomyAgentProfile,
    ProjectAutonomyAgentSchedule,
    ProjectAutonomyArchitectReview,
    ProjectAutonomyArtifact,
    ProjectAutonomyDelegation,
    ProjectAutonomyLearningSample,
    ProjectAutonomyLease,
    ProjectAutonomyMessage,
    ProjectAutonomyOperatorQuestion,
    ProjectAutonomyRun,
    ProjectAutonomyStep,
    ProjectDomainRun,
    User,
)
from app.models.code_brain import CodeRepo
from app.services.code_brain import indexer as code_indexer
from app.services.code_brain import runtime as code_runtime
from app.services.project_autonomy import agent_scheduler
from app.services.project_autonomy import orchestrator


def _sqlite_autonomy_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            CodeRepo.__table__,
            ProjectDomainRun.__table__,
            ProjectAutonomyAgentProfile.__table__,
            ProjectAutonomyAgentSchedule.__table__,
            ProjectAutonomyRun.__table__,
            ProjectAutonomyMessage.__table__,
            ProjectAutonomyStep.__table__,
            ProjectAutonomyArtifact.__table__,
            ProjectAutonomyArchitectReview.__table__,
            ProjectAutonomyDelegation.__table__,
            ProjectAutonomyOperatorQuestion.__table__,
            ProjectAutonomyLearningSample.__table__,
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


def test_approve_plan_rejects_stale_architect_review(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_review_stale",
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
        assert orchestrator.run_payload(db, run)["architect_review"]["stale"] is False

        run.prompt = "add image attachments and drag-drop previews to Autopilot prompts"
        db.commit()

        payload = orchestrator.run_payload(db, run)
        assert payload["architect_review"]["stale"] is True
        with pytest.raises(ValueError, match="stale"):
            orchestrator.approve_plan(db, run.run_id)
    finally:
        db.close()


def test_plan_chat_approval_message_approves_current_plan(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        plan = {
            "analysis": "Add image attachments.",
            "files": [
                {
                    "path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                    "action": "modify",
                    "description": "Add prompt attachment controls.",
                }
            ],
            "notes": "",
        }
        run = ProjectAutonomyRun(
            run_id="pa_chat_approve",
            repo_id=repo.id,
            prompt="add image attachments to Autopilot prompts",
            status="awaiting_approval",
            current_stage="plan",
            execution_mode="plan_approval",
            plan_status="awaiting_approval",
            plan_json=json.dumps(plan),
        )
        db.add(run)
        db.commit()
        orchestrator._record_architect_review(
            db,
            run,
            {
                "attempt_index": 1,
                "status": "passed",
                "score": 93,
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
            content="Looks good, approve and implement.",
        )

        assert payload["status"] == "queued"
        assert payload["plan_status"] == "approved"
        assert payload["current_stage"] == "implement"
        assert payload["plan"] == plan
        assert payload["architect_review"]["status"] == "passed"
        assert payload["messages"][-1]["message_type"] == "status"
        assert "Plan approved" in payload["messages"][-1]["content"]
    finally:
        db.close()


def test_scheduled_agent_plan_only_permission_blocks_implementation_approval(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        architect = next(
            agent
            for agent in orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
            if agent["profile_key"] == "architect"
        )
        plan = {
            "analysis": "Improve Autopilot status copy.",
            "files": [
                {
                    "path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                    "action": "modify",
                    "description": "Improve status copy.",
                }
            ],
            "notes": "",
        }
        run = orchestrator.create_run(
            db,
            prompt="scheduled agent should draft only",
            repo_id=repo.id,
            agent_profile_id=architect["id"],
            autonomy_level=orchestrator.AUTONOMY_LEVEL_SCHEDULED_AGENT,
        )
        run.status = orchestrator.RUN_STATUS_AWAITING_APPROVAL
        run.current_stage = orchestrator.STAGE_PLAN
        run.plan_status = orchestrator.PLAN_STATUS_AWAITING_APPROVAL
        run.plan_json = json.dumps(plan)
        db.add(run)
        db.commit()
        orchestrator._record_architect_review(
            db,
            run,
            {
                "attempt_index": 1,
                "status": "passed",
                "score": 93,
                "confidence": "high",
                "dimensions": {},
                "alternatives": [],
                "critique": {"blockers": [], "next_action": "approval_ready"},
                "selected_files": [
                    {
                        "path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                        "rationale": "Status copy lives in the cockpit screen.",
                    }
                ],
                "blocking_reason": None,
            },
        )
        db.commit()

        with pytest.raises(ValueError, match="permission gate"):
            orchestrator.approve_plan(db, run.run_id)

        payload = orchestrator.run_payload(db, run, include_events=True)
        assert payload["status"] == orchestrator.RUN_STATUS_AWAITING_APPROVAL
        assert payload["plan_status"] == orchestrator.PLAN_STATUS_AWAITING_APPROVAL
        assert payload["agent_snapshot"]["permissions"]["worktree"] is False
    finally:
        db.close()


def test_scheduled_agent_worktree_permission_allows_implementation_approval(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        architect = next(
            agent
            for agent in orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
            if agent["profile_key"] == "architect"
        )
        orchestrator.update_agent_profile(
            db,
            architect["id"],
            permissions={orchestrator.AGENT_PERMISSION_WORKTREE: True},
        )
        plan = {
            "analysis": "Improve Autopilot status copy.",
            "files": [
                {
                    "path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                    "action": "modify",
                    "description": "Improve status copy.",
                }
            ],
            "notes": "",
        }
        run = orchestrator.create_run(
            db,
            prompt="scheduled agent may implement after enablement",
            repo_id=repo.id,
            agent_profile_id=architect["id"],
            autonomy_level=orchestrator.AUTONOMY_LEVEL_SCHEDULED_AGENT,
        )
        run.status = orchestrator.RUN_STATUS_AWAITING_APPROVAL
        run.current_stage = orchestrator.STAGE_PLAN
        run.plan_status = orchestrator.PLAN_STATUS_AWAITING_APPROVAL
        run.plan_json = json.dumps(plan)
        db.add(run)
        db.commit()
        orchestrator._record_architect_review(
            db,
            run,
            {
                "attempt_index": 1,
                "status": "passed",
                "score": 93,
                "confidence": "high",
                "dimensions": {},
                "alternatives": [],
                "critique": {"blockers": [], "next_action": "approval_ready"},
                "selected_files": [
                    {
                        "path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                        "rationale": "Status copy lives in the cockpit screen.",
                    }
                ],
                "blocking_reason": None,
            },
        )
        db.commit()

        payload = orchestrator.approve_plan(db, run.run_id)

        assert payload["status"] == orchestrator.RUN_STATUS_QUEUED
        assert payload["plan_status"] == orchestrator.PLAN_STATUS_APPROVED
        assert payload["current_stage"] == orchestrator.STAGE_IMPLEMENT
        assert payload["agent_snapshot"]["permissions"]["worktree"] is True
    finally:
        db.close()


def test_plan_chat_approval_message_explains_failed_quality_gate(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_chat_approve_failed_gate",
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

        payload = orchestrator.append_user_message(db, run.run_id, content="approve and implement")

        assert payload["status"] == "awaiting_approval"
        assert payload["plan_status"] == "awaiting_approval"
        assert payload["plan"]["analysis"] == "bad"
        assert payload["architect_review"]["status"] == "failed"
        assert payload["messages"][-1]["message_type"] == "status"
        assert "can't approve" in payload["messages"][-1]["content"]
        assert "quality gate" in payload["messages"][-1]["content"]
    finally:
        db.close()


def test_plan_approval_message_parser_avoids_feedback_and_negation():
    assert orchestrator._looks_like_plan_approval_message("Looks good, approve and implement.")
    assert orchestrator._looks_like_plan_approval_message("go ahead")
    assert not orchestrator._looks_like_plan_approval_message("implement a safer settings flow")
    assert not orchestrator._looks_like_plan_approval_message("looks good but add drag-drop first")
    assert not orchestrator._looks_like_plan_approval_message("do not approve yet")


def test_run_cancel_message_parser_avoids_cancel_button_feedback():
    assert orchestrator._looks_like_run_cancel_message("cancel")
    assert orchestrator._looks_like_run_cancel_message("please stop this run")
    assert not orchestrator._looks_like_run_cancel_message("make the cancel button clearer")


def test_plan_chat_cancel_message_cancels_without_replanning(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_chat_cancel_plan",
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
        )
        db.add(run)
        db.commit()

        payload = orchestrator.append_user_message(db, run.run_id, content="cancel this run")

        assert payload["status"] == "cancelled"
        assert payload["merge_status"] == "cancelled"
        assert payload["cancel_requested"] is True
        assert payload["plan"]["analysis"] == "Add prompt attachments."
        assert payload["messages"][-1]["message_type"] == "result"
        assert "cancelled" in payload["messages"][-1]["content"].lower()
    finally:
        db.close()


def test_active_chat_cancel_message_requests_safe_checkpoint_cancel(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="pa_chat_cancel_active",
            repo_id=repo.id,
            prompt="change example",
            status="running",
            current_stage="implement",
            execution_mode="plan_approval",
            plan_status="approved",
        )
        db.add(run)
        db.commit()

        payload = orchestrator.append_user_message(db, run.run_id, content="stop this run")

        assert payload["status"] == "running"
        assert payload["cancel_requested"] is True
        assert payload["steps"][-1]["title"] == "Cancel requested"
        assert payload["messages"][-1]["message_type"] == "status"
        assert "Cancellation requested" in payload["messages"][-1]["content"]
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
            target_branch="main",
            base_branch="main",
            base_sha="def456",
            integration_branch="project-auto-pa_feedback_invalidates_review",
            worktree_path=str(tmp_path / "old-feedback-worktree"),
            merge_status="blocked",
            merge_message="Old feedback merge blocker.",
            plan_json=(
                '{"analysis":"Add prompt attachments.",'
                '"files":[{"path":"chili_mobile/lib/src/brain/brain_dispatch_screen.dart",'
                '"action":"modify","description":"Add prompt attachment controls."}],'
                '"notes":""}'
            ),
            files_json='["chili_mobile/lib/src/brain/brain_dispatch_screen.dart"]',
            agents_json='[{"name":"architect","files":["chili_mobile/lib/src/brain/brain_dispatch_screen.dart"]}]',
            commands_json=json.dumps([{"step_key": "flutter_analyze"}]),
            validation_json=json.dumps([{"step_key": "flutter_analyze", "exit_code": 0}]),
            learning_json=json.dumps({"outcome": "blocked", "validation_passed": False}),
            cancel_requested=True,
            started_at=datetime.utcnow() - timedelta(minutes=15),
            finished_at=datetime.utcnow() - timedelta(minutes=3),
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
        assert payload["agents"] == []
        assert payload["commands"] == []
        assert payload["validation"] == []
        assert payload["learning"] == {}
        assert payload["target_branch"] is None
        assert payload["base_branch"] is None
        assert payload["base_sha"] is None
        assert payload["integration_branch"] is None
        assert payload["worktree_path"] is None
        assert payload["merge_status"] == "pending"
        assert payload["merge_message"] is None
        assert payload["cancel_requested"] is False
        assert payload["started_at"] is None
        assert payload["finished_at"] is None
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
            prompt="find a small enhancement for the desktop-app enhancement plan presentation",
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


def test_open_ended_small_enhancement_requires_operator_choice(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        presenter = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
        cockpit = tmp_path / "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
        orchestrator_file = tmp_path / "app/services/project_autonomy/orchestrator.py"
        presenter.parent.mkdir(parents=True)
        presenter.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
        cockpit.write_text("class BrainDispatchScreen {}\n", encoding="utf-8")
        orchestrator_file.parent.mkdir(parents=True)
        orchestrator_file.write_text("def run():\n    return None\n", encoding="utf-8")
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
            run_id="pa_review_choose",
            repo_id=repo.id,
            prompt=(
                "You are an autonomous coding operator architect. "
                "This is the first prompt as a test -- find a small enhancement for the desktop app."
            ),
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

        payload = orchestrator.run_autonomy_sync(db, run.run_id)

        assert payload["status"] == "awaiting_clarification"
        assert payload["plan_status"] == "awaiting_clarification"
        assert payload["architect_review"]["status"] == "needs_clarification"
        assert "operator_choice_required" in payload["architect_review"]["critique"]["blockers"]
        assert payload["architect_review"]["alternatives"]
        assert not orchestrator._architect_review_passed(payload["architect_review"])
        messages = [m["content"] for m in payload["messages"]]
        assert any("should not pick an arbitrary file" in message for message in messages)
        assert not any("approve it to let me implement" in message for message in messages)
        with pytest.raises(ValueError):
            orchestrator.approve_plan(db, run.run_id)
    finally:
        db.close()


def test_open_ended_small_enhancement_followup_direction_can_plan(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        presenter = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
        cockpit = tmp_path / "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
        presenter.parent.mkdir(parents=True)
        presenter.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
        cockpit.write_text("class BrainDispatchScreen {}\n", encoding="utf-8")
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
            run_id="pa_review_choose_then_plan",
            repo_id=repo.id,
            prompt=(
                "You are an autonomous coding operator architect. "
                "This is the first prompt as a test -- find a small enhancement for the desktop app."
            ),
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

        first_payload = orchestrator.run_autonomy_sync(db, run.run_id)
        assert first_payload["status"] == "awaiting_clarification"

        feedback_payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content="Improve the Autopilot chat composer so Enter sends like the Send button.",
        )
        assert feedback_payload["status"] == "queued"
        assert feedback_payload["plan_status"] == "revising"

        planned_payload = orchestrator.run_autonomy_sync(db, run.run_id)

        assert planned_payload["status"] == "awaiting_approval"
        assert planned_payload["plan_status"] == "awaiting_approval"
        assert planned_payload["architect_review"]["status"] == "passed"
        assert planned_payload["files"] == ["chili_mobile/lib/src/brain/brain_dispatch_screen.dart"]
    finally:
        db.close()


def test_open_ended_clarification_option_number_can_plan(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        presenter = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
        cockpit = tmp_path / "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
        presenter.parent.mkdir(parents=True)
        presenter.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
        cockpit.write_text("class BrainDispatchScreen {}\n", encoding="utf-8")
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
        run = orchestrator.create_run(
            db,
            prompt="find a small enhancement for the desktop app",
            repo_id=repo.id,
            execution_mode="plan_approval",
            start_planning=True,
        )
        monkeypatch.setattr(
            orchestrator,
            "_gather_context",
            lambda *args, **kwargs: {"repos": [], "insights": [], "hotspots": [], "relevant_files": []},
        )

        first_payload = orchestrator.run_autonomy_sync(db, run.run_id)
        assert first_payload["status"] == "awaiting_clarification"
        assert any(
            "1. chili_mobile/lib/src/brain/brain_dispatch_screen.dart" in message["content"]
            for message in first_payload["messages"]
            if message["message_type"] == "clarification"
        )

        orchestrator.append_user_message(db, run.run_id, content="Option 1")
        planned_payload = orchestrator.run_autonomy_sync(db, run.run_id)

        assert planned_payload["status"] == "awaiting_approval"
        assert planned_payload["architect_review"]["status"] == "passed"
        assert planned_payload["files"] == ["chili_mobile/lib/src/brain/brain_dispatch_screen.dart"]
        assert "option 1" in planned_payload["plan"]["analysis"].lower()
        assert "selected option 1" in planned_payload["architect_review"]["selected_files"][0]["rationale"].lower()
    finally:
        db.close()


def test_open_ended_option_parser_uses_whole_choice_phrases():
    prompt = "User message 1: find a small enhancement\n\nUser message 2: choose option 1 please"
    assert orchestrator._selected_open_ended_option_index(prompt.lower()) == 1

    ambiguous_prompt = "User message 1: find a small enhancement\n\nUser message 2: choose option 10"
    assert orchestrator._selected_open_ended_option_index(ambiguous_prompt.lower()) is None


def test_open_ended_followup_can_target_composer_without_repeating_autopilot(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        presenter = tmp_path / "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
        cockpit = tmp_path / "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
        presenter.parent.mkdir(parents=True)
        presenter.write_text("class AutonomyRunPresenter {}\n", encoding="utf-8")
        cockpit.write_text("class BrainDispatchScreen {}\n", encoding="utf-8")
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
            run_id="pa_review_choose_composer",
            repo_id=repo.id,
            prompt="find a small enhancement for the desktop app",
            status="queued",
            current_stage="queued",
            execution_mode="plan_approval",
            plan_status="drafting",
        )
        db.add(run)
        db.add(
            ProjectAutonomyMessage(
                run_id=run.run_id,
                role="user",
                message_type="prompt",
                content=run.prompt,
            )
        )
        db.commit()
        monkeypatch.setattr(
            orchestrator,
            "_gather_context",
            lambda *args, **kwargs: {"repos": [], "insights": [], "hotspots": [], "relevant_files": []},
        )

        first_payload = orchestrator.run_autonomy_sync(db, run.run_id)
        assert first_payload["status"] == "awaiting_clarification"

        orchestrator.append_user_message(
            db,
            run.run_id,
            content="Improve the chat composer so Enter sends like the Send button.",
        )
        planned_payload = orchestrator.run_autonomy_sync(db, run.run_id)

        assert planned_payload["status"] == "awaiting_approval"
        assert planned_payload["architect_review"]["status"] == "passed"
        assert planned_payload["files"] == ["chili_mobile/lib/src/brain/brain_dispatch_screen.dart"]
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


def test_approved_run_blocks_without_current_architect_review(monkeypatch, tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo_file = tmp_path / "app/example.py"
        repo_file.parent.mkdir(parents=True)
        repo_file.write_text("VALUE = 1\n", encoding="utf-8")
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
            run_id="pa_approved_without_review",
            repo_id=repo.id,
            prompt="change example",
            status="queued",
            current_stage="implement",
            execution_mode="plan_approval",
            plan_status="approved",
            plan_json=(
                '{"analysis":"Change example safely.",'
                '"files":[{"path":"app/example.py","action":"modify","description":"Change example safely."}],'
                '"notes":""}'
            ),
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(
            orchestrator,
            "_create_run_worktree",
            lambda *args, **kwargs: pytest.fail("implementation must not start without a current review"),
        )

        payload = orchestrator.run_autonomy_sync(db, run.run_id)

        assert payload["status"] == "blocked"
        assert payload["worktree_path"] is None
        assert "quality gate has not passed" in payload["merge_message"]
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
        owner = User(name="Owner", email="owner@example.com")
        other = User(name="Other", email="other@example.com")
        db.add_all([owner, other])
        db.flush()
        run = ProjectAutonomyRun(
            run_id="pa_events",
            user_id=owner.id,
            prompt="hello",
            status="awaiting_approval",
            current_stage="plan",
            plan_json=json.dumps(
                {
                    "analysis": "Improve the Autopilot chat.",
                    "files": [{"path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"}],
                }
            ),
        )
        db.add(run)
        db.commit()
        orchestrator._record_message(db, run, "assistant", "Plan ready.", message_type="plan")
        orchestrator._record_architect_review(
            db,
            run,
            {
                "attempt_index": 1,
                "status": "passed",
                "score": 91,
                "confidence": "high",
                "selected_files": [
                    {"path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"}
                ],
                "critique": {"blockers": []},
            },
        )
        db.commit()

        events = orchestrator.events_after(db, run.run_id)

        assert events["run"]["run_id"] == run.run_id
        assert events["run"]["architect_review"]["stale"] is False
        assert events["messages"][0]["content"] == "Plan ready."
        assert events["after_message_id"] == events["messages"][0]["id"]

        unauthorized = orchestrator.events_after(db, run.run_id, user_id=other.id)
        assert unauthorized["run"] is None
        assert unauthorized["messages"] == []
        assert unauthorized["steps"] == []
        assert unauthorized["artifacts"] == []
        assert unauthorized["after_message_id"] == 0

        run.plan_json = json.dumps(
            {
                "analysis": "Change a different surface.",
                "files": [{"path": "chili_mobile/lib/src/network/chili_api_client.dart"}],
            }
        )
        db.commit()
        refreshed = orchestrator.events_after(
            db,
            run.run_id,
            after_message_id=events["after_message_id"],
            after_step_id=events["after_step_id"],
            after_artifact_id=events["after_artifact_id"],
        )
        assert refreshed["messages"] == []
        assert refreshed["steps"] == []
        assert refreshed["artifacts"] == []
        assert refreshed["run"]["architect_review"]["stale"] is True
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


def test_autopilot_chat_rejects_unsafe_image_attachment_sources(tmp_path):
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


def test_start_plan_transitions_chat_to_queued_plan(tmp_path):
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


def test_start_plan_clears_old_plan_and_marks_review_stale(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        plan = {
            "analysis": "Improve approval guidance.",
            "files": [
                {
                    "path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                    "action": "modify",
                    "description": "Show clearer approval controls.",
                }
            ],
            "notes": "",
        }
        run = ProjectAutonomyRun(
            run_id="pa_replan_stale_review",
            repo_id=repo.id,
            prompt="improve Autopilot approval guidance",
            status="awaiting_approval",
            current_stage="plan",
            execution_mode="plan_approval",
            plan_status="awaiting_approval",
            target_branch="main",
            base_branch="main",
            base_sha="abc123",
            integration_branch="project-auto-pa_replan_stale_review",
            worktree_path=str(tmp_path / "old-worktree"),
            commands_json=json.dumps([{"step_key": "pytest_targeted"}]),
            validation_json=json.dumps([{"step_key": "pytest_targeted", "exit_code": 0}]),
            learning_json=json.dumps({"outcome": "blocked", "validation_passed": False}),
            merge_status="blocked",
            merge_message="Old merge blocker.",
            plan_json=json.dumps(plan),
            files_json=json.dumps(["chili_mobile/lib/src/brain/brain_dispatch_screen.dart"]),
            agents_json=json.dumps(
                [
                    {
                        "name": "architect",
                        "role": "architect",
                        "status": "lead",
                        "files": ["chili_mobile/lib/src/brain/brain_dispatch_screen.dart"],
                    }
                ]
            ),
            started_at=datetime.utcnow() - timedelta(minutes=10),
            finished_at=datetime.utcnow() - timedelta(minutes=5),
        )
        db.add(run)
        db.commit()
        orchestrator._record_architect_review(
            db,
            run,
            {
                "attempt_index": 1,
                "status": "passed",
                "score": 91,
                "confidence": "high",
                "dimensions": {},
                "alternatives": [],
                "critique": {"blockers": [], "next_action": "approval_ready"},
                "selected_files": [
                    {
                        "path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                        "rationale": "Approval controls are rendered in the cockpit screen.",
                    }
                ],
            },
        )
        db.commit()
        assert orchestrator.run_payload(db, run)["architect_review"]["stale"] is False

        payload = orchestrator.start_plan(db, run.run_id)

        assert payload is not None
        assert payload["status"] == "queued"
        assert payload["plan_status"] == "drafting"
        assert payload["plan"] == {}
        assert payload["files"] == []
        assert payload["agents"] == []
        assert payload["commands"] == []
        assert payload["validation"] == []
        assert payload["learning"] == {}
        assert payload["target_branch"] is None
        assert payload["base_branch"] is None
        assert payload["base_sha"] is None
        assert payload["integration_branch"] is None
        assert payload["worktree_path"] is None
        assert payload["merge_status"] == "pending"
        assert payload["merge_message"] is None
        assert payload["started_at"] is None
        assert payload["finished_at"] is None
        assert payload["architect_review"]["status"] == "passed"
        assert payload["architect_review"]["stale"] is True
        assert not orchestrator._architect_review_passed(payload["architect_review"])
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
        monkeypatch.setattr(
            orchestrator.ollama_client,
            "chat",
            lambda *args, **kwargs: SimpleNamespace(
                ok=True,
                text="I cannot produce a patch.",
                error=None,
                latency_ms=1,
            ),
        )

        with pytest.raises(orchestrator.AutonomyBlocked) as exc:
            orchestrator.generate_diffs_from_plan(
                db,
                run,
                tmp_path,
                [{"path": "foo.txt", "description": "make a small change"}],
            )

        assert "model did not return a unified diff" in str(exc.value)
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


def test_agent_profile_bootstrap_creates_paused_repo_bench_once(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        (tmp_path / "app/services/trading").mkdir(parents=True)
        repo = CodeRepo(path=str(tmp_path), name="algo-trading-repo", active=True)
        db.add(repo)
        db.commit()

        first = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        second = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)

        assert len(first) == len(second)
        keys = {agent["profile_key"] for agent in first}
        assert {
            "architect",
            "product_pm",
            "software_engineer",
            "frontend",
            "backend",
            "qa",
            "data_scientist",
            "risk_reviewer",
            "sre",
            "mlops",
            "algo_trading_architect",
        }.issubset(keys)
        assert all(agent["status"] == "paused" for agent in first)
        assert all(agent["schedule_enabled"] is False for agent in first)
        architect = next(agent for agent in first if agent["profile_key"] == "architect")
        assert architect["tier"] == "macro"
        frontend = next(agent for agent in first if agent["profile_key"] == "frontend")
        assert frontend["permissions"]["plan"] is True
        assert frontend["permissions"]["worktree"] is False
        assert db.query(ProjectAutonomyAgentProfile).count() == len(first)
    finally:
        db.close()


def test_current_workspace_repo_canonicalization_reuses_existing_d_path():
    db = _sqlite_autonomy_session()
    try:
        repo_root = str(Path(__file__).resolve().parents[1])
        d_path_row = CodeRepo(
            path=repo_root,
            host_path=repo_root,
            name="p17-repo",
            user_id=4,
            active=True,
        )
        alias_row = CodeRepo(path="/app", name="chili-home-copilot", active=True)
        db.add_all([d_path_row, alias_row])
        db.commit()

        repos = code_indexer.get_registered_repos(db, include_shared=True)

        preferred = repos[0]
        alias = next(repo for repo in repos if repo["id"] == alias_row.id)
        assert preferred["id"] == d_path_row.id
        assert preferred["name"] == "chili-home-copilot"
        assert preferred["path"] == repo_root
        assert preferred["resolved_path"] == repo_root
        assert preferred["preferred_for_autopilot"] is True
        assert db.get(CodeRepo, d_path_row.id).user_id is None
        assert alias["preferred_for_autopilot"] is False
        assert alias["resolved_path"] == repo_root
        preferred_order = code_indexer.sort_repos_for_runtime_preference(
            [db.get(CodeRepo, alias_row.id), db.get(CodeRepo, d_path_row.id)]
        )
        assert preferred_order[0].id == d_path_row.id
        user_visible = code_indexer.get_registered_repos(
            db,
            user_id=1,
            include_shared=True,
        )
        assert user_visible[0]["id"] == d_path_row.id
    finally:
        db.close()


def test_agent_profile_bootstrap_imports_matching_codex_automations(
    tmp_path, monkeypatch
):
    db = _sqlite_autonomy_session()
    try:
        codex_home = tmp_path / "codex-home"
        automation_dir = codex_home / "automations" / "qa-verification-engineer"
        automation_dir.mkdir(parents=True)
        repo_path = (tmp_path / "workspace").resolve()
        repo_path.mkdir()
        prompt_repo_path = str(repo_path).replace("\\", "/")
        (automation_dir / "automation.toml").write_text(
            "\n".join(
                [
                    'id = "qa-verification-engineer"',
                    'name = "QA Verification Engineer"',
                    'kind = "heartbeat"',
                    'status = "ACTIVE"',
                    'rrule = "RRULE:FREQ=MINUTELY;INTERVAL=5"',
                    f'cwds = ["{prompt_repo_path}"]',
                    'prompt = """',
                    f"Run every 5 minutes. Workspace: {prompt_repo_path}. Inbox: project_ws/QA/IN. Output: project_ws/QA/OUT. State: project_ws/QA/OUT/_state.",
                    "Acquire run.lock and write an OUT report before exit.",
                    "Run `powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/agent-flow-health.ps1 -Root . -Json`.",
                    "Do not merge, deploy, run migrations, or call broker APIs.",
                    "Continuously verify CHILI desktop QA evidence and report blockers.",
                    '"""',
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        repo = CodeRepo(
            path=str(repo_path),
            name="codex-import-target",
            active=True,
        )
        db.add(repo)
        db.commit()

        profiles = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)

        imported = next(
            profile
            for profile in profiles
            if profile["profile_key"] == "codex_qa_verification_engineer"
        )
        schedule = imported["schedule"]
        prompt_setting = imported["prompt_setting"]
        automation = prompt_setting["codex_automation"]

        assert imported["status"] == "paused"
        assert imported["schedule_enabled"] is False
        assert imported["permissions"]["worktree"] is False
        assert imported["permissions"]["merge"] is False
        assert imported["operating_state"]["state"] == orchestrator.AGENT_OPERATING_STATE_PAUSED_SOURCE_ACTIVE
        assert imported["operating_state"]["next_action"] == orchestrator.AGENT_OPERATING_ACTION_ENABLE_ACTIVE
        assert imported["operating_state"]["safety"] == orchestrator.AGENT_OPERATING_SAFETY_PLAN_ONLY
        assert schedule["rrule"] == "FREQ=MINUTELY;INTERVAL=5"
        assert schedule["cadence"] == "five_minutes"
        assert schedule["source_status"] == "ACTIVE"
        assert schedule["budget"]["max_minutes"] == 20
        assert prompt_setting["source"] == "codex_automation"
        assert automation["id"] == "qa-verification-engineer"
        assert automation["kind"] == "heartbeat"
        assert automation["rrule"] == "RRULE:FREQ=MINUTELY;INTERVAL=5"
        assert automation["normalized_rrule"] == "FREQ=MINUTELY;INTERVAL=5"
        assert automation["prompt_sha256"]
        assert automation["operating_contract"]["workspace"] == prompt_repo_path
        assert automation["operating_contract"]["inbox"] == "project_ws/QA/IN"
        assert automation["operating_contract"]["uses_run_lock"] is True
        assert imported["prompt_freshness"]["status"] == "current"
        contract = imported["operating_contract"]
        assert contract["workspace"].replace("\\", "/") == prompt_repo_path
        assert contract["declared_paths"] == [prompt_repo_path]
        assert contract["inbox"] == "project_ws/QA/IN"
        assert contract["output"] == "project_ws/QA/OUT"
        assert contract["state"] == "project_ws/QA/OUT/_state"
        assert contract["uses_run_lock"] is True
        assert contract["requires_out_report"] is True
        assert "broker actions" in contract["safety_boundaries"]
        assert contract["key_commands"] == [
            "powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/agent-flow-health.ps1 -Root . -Json"
        ]
        assert prompt_repo_path in prompt_setting["system_prompt"]
        schedule_row = (
            db.query(ProjectAutonomyAgentSchedule)
            .filter(ProjectAutonomyAgentSchedule.profile_id == imported["id"])
            .one()
        )
        assert schedule_row.status == "paused"
        assert schedule_row.rrule == "FREQ=MINUTELY;INTERVAL=5"
        assert orchestrator._agent_schedule_interval("RRULE:FREQ=MINUTELY;INTERVAL=5").total_seconds() == 300
    finally:
        db.close()


def test_agent_profile_bootstrap_resyncs_changed_codex_prompt(
    tmp_path, monkeypatch
):
    db = _sqlite_autonomy_session()
    try:
        codex_home = tmp_path / "codex-home"
        automation_dir = codex_home / "automations" / "agentops-director"
        automation_dir.mkdir(parents=True)
        repo_path = (tmp_path / "workspace").resolve()
        repo_path.mkdir()
        prompt_repo_path = str(repo_path).replace("\\", "/")
        config_path = automation_dir / "automation.toml"

        def write_automation(prompt_line: str, rrule: str = "FREQ=MINUTELY;INTERVAL=5") -> None:
            config_path.write_text(
                "\n".join(
                    [
                        'id = "agentops-director"',
                        'name = "AgentOps Director"',
                        'kind = "heartbeat"',
                        'status = "ACTIVE"',
                        f'rrule = "{rrule}"',
                        'prompt = """',
                        f"Workspace: {prompt_repo_path}",
                        prompt_line,
                        '"""',
                    ]
                ),
                encoding="utf-8",
            )

        write_automation("Inspect agent flow and report blockers.")
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [codex_home])
        repo = CodeRepo(path=str(repo_path), name="workspace", active=True)
        db.add(repo)
        db.commit()

        first_profiles = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        first = next(
            profile
            for profile in first_profiles
            if profile["profile_key"] == "codex_agentops_director"
        )
        first_hash = first["prompt_setting"]["codex_automation"]["prompt_sha256"]

        write_automation(
            "Inspect agent flow, stale locks, and PR blocker routing.",
            "FREQ=MINUTELY;INTERVAL=10",
        )
        second_profiles = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        second = next(
            profile
            for profile in second_profiles
            if profile["profile_key"] == "codex_agentops_director"
        )

        assert second["prompt_freshness"]["status"] == "current"
        assert second["prompt_setting"]["codex_automation"]["prompt_sha256"] != first_hash
        assert "PR blocker routing" in second["prompt_setting"]["system_prompt"]
        assert second["schedule"]["rrule"] == "FREQ=MINUTELY;INTERVAL=10"
        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)
        freshness = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_CODEX_FRESHNESS
        )
        assert freshness["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        assert readiness["codex_automations"]["stale_profile_keys"] == []
    finally:
        db.close()


def test_codex_profile_sync_reports_refreshed_snapshots(
    tmp_path, monkeypatch
):
    db = _sqlite_autonomy_session()
    try:
        codex_home = tmp_path / "codex-home"
        automation_dir = codex_home / "automations" / "agentops-director"
        automation_dir.mkdir(parents=True)
        repo_path = (tmp_path / "workspace").resolve()
        repo_path.mkdir()
        prompt_repo_path = str(repo_path).replace("\\", "/")
        config_path = automation_dir / "automation.toml"

        def write_automation(prompt_line: str) -> None:
            config_path.write_text(
                "\n".join(
                    [
                        'id = "agentops-director"',
                        'name = "AgentOps Director"',
                        'kind = "heartbeat"',
                        'status = "ACTIVE"',
                        'rrule = "FREQ=MINUTELY;INTERVAL=5"',
                        'prompt = """',
                        f"Workspace: {prompt_repo_path}",
                        prompt_line,
                        '"""',
                    ]
                ),
                encoding="utf-8",
            )

        write_automation("Inspect agent flow and report blockers.")
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [codex_home])
        repo = CodeRepo(path=str(repo_path), name="workspace", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)

        write_automation("Inspect agent flow, stale locks, and Codex prompt drift.")
        result = orchestrator.sync_codex_agent_profiles(db, repo_id=repo.id)
        synced = next(
            profile
            for profile in result["agents"]
            if profile["profile_key"] == "codex_agentops_director"
        )

        assert result["source_count"] == 1
        assert result["current_count"] == 1
        assert result["refreshed_count"] == 1
        assert result["created_count"] == 0
        assert result["stale_count"] == 0
        assert "Codex prompt drift" in synced["prompt_setting"]["system_prompt"]
        assert synced["prompt_freshness"]["status"] == orchestrator.CODEX_AUTOMATION_SYNC_STATUS_CURRENT
    finally:
        db.close()


def test_codex_automation_import_uses_cwds_and_repo_aliases(
    tmp_path, monkeypatch
):
    db = _sqlite_autonomy_session()
    try:
        codex_home = tmp_path / "codex-home"
        perf_dir = codex_home / "automations" / "performance-bottleneck-research"
        option_dir = codex_home / "automations" / "continue-chili-option-path"
        perf_dir.mkdir(parents=True)
        option_dir.mkdir(parents=True)
        repo_path = (tmp_path / "workspace").resolve()
        repo_path.mkdir()
        prompt_repo_path = str(repo_path).replace("\\", "\\\\")
        (perf_dir / "automation.toml").write_text(
            "\n".join(
                [
                    'id = "performance-bottleneck-research"',
                    'name = "Performance Bottleneck Research"',
                    'kind = "cron"',
                    'status = "PAUSED"',
                    'rrule = "FREQ=HOURLY;INTERVAL=6"',
                    'prompt = "Research the repository for system performance bottlenecks."',
                    f'cwds = ["{prompt_repo_path}"]',
                ]
            ),
            encoding="utf-8",
        )
        (option_dir / "automation.toml").write_text(
            "\n".join(
                [
                    'id = "continue-chili-option-path"',
                    'name = "Continue CHILI option path"',
                    'kind = "heartbeat"',
                    'status = "PAUSED"',
                    'rrule = "FREQ=MINUTELY;INTERVAL=30;COUNT=10"',
                    'prompt = "Continue hardening the CHILI option path safely."',
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [codex_home])
        repo = CodeRepo(path=str(repo_path), name="chili-home-copilot", active=True)
        other = CodeRepo(path=str(tmp_path / "other"), name="other-repo", active=True)
        db.add_all([repo, other])
        db.commit()

        profiles = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        profile_keys = {profile["profile_key"] for profile in profiles}
        other_profiles = orchestrator.bootstrap_agent_profiles(db, repo_id=other.id)
        other_keys = {profile["profile_key"] for profile in other_profiles}

        assert "codex_performance_bottleneck_research" in profile_keys
        assert "codex_continue_chili_option_path" in profile_keys
        assert "codex_continue_chili_option_path" not in other_keys
        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)
        assert readiness["codex_automations"]["matching"] == 2
        assert readiness["codex_automations"]["imported"] == 2
    finally:
        db.close()


def test_create_run_binds_default_agent_profile_snapshot(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()

        run = orchestrator.create_run(db, prompt="hi", repo_id=repo.id)
        payload = orchestrator.run_payload(db, run, include_events=True)

        assert payload["agent_profile_id"] is not None
        assert payload["agent_profile"]["profile_key"] == "product_pm"
        assert payload["agent_snapshot"]["profile_key"] == "product_pm"
        assert payload["agent_snapshot"]["permissions"]["merge"] is False
        assert payload["messages"][0]["metadata"]["agent"]["profile_key"] == "product_pm"
    finally:
        db.close()


def test_pm_expert_workflow_dispatches_specialist_child_runs(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        (tmp_path / "app/services/trading").mkdir(parents=True)
        repo = CodeRepo(path=str(tmp_path), name="chili-trading-repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(
            db,
            prompt="Review trading metrics, schema persistence, and risk gates before changing Autopilot.",
            repo_id=repo.id,
            start_planning=True,
        )
        plan = {
            "analysis": "Coordinate a trading-safe project change.",
            "files": [
                {
                    "path": "app/services/trading/auto_trader.py",
                    "action": "modify",
                    "description": "Review trading autopilot project code.",
                },
                {
                    "path": "app/models/project_domain.py",
                    "action": "modify",
                    "description": "Review persistence shape.",
                },
                {
                    "path": "tests/test_project_autonomy_service.py",
                    "action": "modify",
                    "description": "Add coordination coverage.",
                },
            ],
            "notes": "",
        }
        files = orchestrator._plan_files(plan)
        threads, synthesis = orchestrator._sync_expert_workflow_threads(
            db,
            run,
            repo,
            plan=plan,
            files=files,
            review={"status": "passed", "score": 92},
            commit=True,
        )
        payload = orchestrator.run_payload(db, run, include_events=True)

        keys = {thread["profile_key"] for thread in threads}
        assert {"architect", "backend", "qa", "dba_architect", "algo_trading_architect", "data_scientist", "risk_reviewer"}.issubset(keys)
        assert payload["agent_profile"]["profile_key"] == "product_pm"
        assert len(payload["expert_threads"]) == len(threads)
        assert len(payload["delegations"]) == len(threads)
        assert len(payload["child_runs"]) == len(threads)
        assert synthesis["mode"] == "pm_led"
        assert payload["pm_synthesis"]["coordinator"]["profile_key"] == "product_pm"
        assert any("broker execution" in gate for gate in payload["pm_synthesis"]["safety_gates"])
        assert all(thread["child_run_id"].startswith("pa_") for thread in payload["expert_threads"])
        assert all(
            thread["child_run"]["status"] == orchestrator.RUN_STATUS_COMPLETED
            for thread in payload["expert_threads"]
        )
        assert all(
            thread["child_run"]["agent_profile"]["profile_key"] == thread["profile_key"]
            for thread in payload["expert_threads"]
        )
        child_row = (
            db.query(ProjectAutonomyRun)
            .filter(ProjectAutonomyRun.parent_run_id == run.run_id)
            .first()
        )
        child_payload = orchestrator.run_payload(db, child_row, include_events=True)
        assert child_payload["parent_run"]["run_id"] == run.run_id
        assert child_payload["parent_run"]["agent_profile"]["profile_key"] == "product_pm"
        assert db.query(ProjectAutonomyRun).filter(ProjectAutonomyRun.parent_run_id == run.run_id).count() == len(threads)
    finally:
        db.close()


def test_pm_expert_workflow_child_runs_do_not_pollute_parent_run_list(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="Update backend API tests.", repo_id=repo.id, start_planning=True)
        plan = {
            "analysis": "Backend test change.",
            "files": [
                {
                    "path": "app/routers/brain_project.py",
                    "action": "modify",
                    "description": "Update API payload.",
                }
            ],
            "notes": "",
        }
        orchestrator._sync_expert_workflow_threads(
            db,
            run,
            repo,
            plan=plan,
            files=orchestrator._plan_files(plan),
            review={"status": "passed", "score": 90},
            commit=True,
        )

        parent_runs = orchestrator.list_runs(db, repo_id=repo.id)

        assert [item["run_id"] for item in parent_runs] == [run.run_id]
        assert parent_runs[0]["expert_threads"]
    finally:
        db.close()


def test_archive_runs_hides_without_deleting_messages_or_artifacts(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="archive me", repo_id=repo.id)

        result = orchestrator.archive_runs(db, repo_id=repo.id)
        visible = orchestrator.list_runs(db, repo_id=repo.id)
        archived = orchestrator.list_runs(db, repo_id=repo.id, include_archived=True)

        assert result["archived"] == 1
        assert visible == []
        assert archived[0]["run_id"] == run.run_id
        assert archived[0]["archived"] is True
        assert db.query(ProjectAutonomyMessage).filter_by(run_id=run.run_id).count() >= 1
        assert db.query(ProjectAutonomyArtifact).filter_by(run_id=run.run_id).count() >= 1
    finally:
        db.close()


def test_scheduled_agent_cycle_respects_paused_state_and_stays_plan_first(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        agents = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        architect_id = next(agent["id"] for agent in agents if agent["profile_key"] == "architect")

        with pytest.raises(ValueError, match="paused"):
            orchestrator.start_agent_cycle(db, architect_id)

        orchestrator.resume_agent_profile(db, architect_id)
        payload = orchestrator.start_agent_cycle(db, architect_id)

        assert payload is not None
        assert payload["agent_profile_id"] == architect_id
        assert payload["status"] == "queued"
        assert payload["execution_mode"] == orchestrator.EXECUTION_MODE_PLAN_APPROVAL
        assert payload["plan_status"] == "drafting"
        assert payload["agent_snapshot"]["permissions"]["worktree"] is False
    finally:
        db.close()


def test_agent_schedule_due_runner_starts_bounded_plan_cycles(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        agents = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        architect_id = next(agent["id"] for agent in agents if agent["profile_key"] == "architect")
        orchestrator.update_agent_profile(
            db,
            architect_id,
            status="active",
            schedule_enabled=True,
            schedule={
                "cadence": "two_minutes",
                "rrule": "FREQ=MINUTELY;INTERVAL=2",
                "budget": {"max_minutes": 20, "max_child_runs": 0},
            },
        )
        schedule = (
            db.query(ProjectAutonomyAgentSchedule)
            .filter(ProjectAutonomyAgentSchedule.profile_id == architect_id)
            .one()
        )
        assert schedule.status == "active"
        assert schedule.next_run_at is not None
        schedule.next_run_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()

        first = orchestrator.run_due_agent_cycles(db, limit=1)

        assert first["started"] == 1
        assert first["runs"][0]["agent_profile_id"] == architect_id
        assert first["runs"][0]["autonomy_level"] == orchestrator.AUTONOMY_LEVEL_SCHEDULED_AGENT
        assert first["runs"][0]["execution_mode"] == orchestrator.EXECUTION_MODE_PLAN_APPROVAL
        refreshed_schedule = (
            db.query(ProjectAutonomyAgentSchedule)
            .filter(ProjectAutonomyAgentSchedule.profile_id == architect_id)
            .one()
        )
        assert refreshed_schedule.last_run_at is not None
        assert refreshed_schedule.next_run_at is not None
        assert refreshed_schedule.next_run_at > refreshed_schedule.last_run_at

        refreshed_schedule.next_run_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
        second = orchestrator.run_due_agent_cycles(db, limit=1)

        assert second["started"] == 0
        assert second["skipped"][0]["reason"] == "open_scheduled_cycle"
    finally:
        db.close()


def test_always_on_agent_runtime_queues_cycles_and_rests(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        profile = (
            db.query(ProjectAutonomyAgentProfile)
            .filter(ProjectAutonomyAgentProfile.repo_id == repo.id)
            .filter(ProjectAutonomyAgentProfile.profile_key == "architect")
            .one()
        )
        updated = orchestrator.update_agent_profile(
            db,
            int(profile.id),
            status=orchestrator.AGENT_PROFILE_STATUS_ACTIVE,
            schedule_enabled=True,
            schedule={
                orchestrator.AGENT_RUNTIME_MODE_KEY: orchestrator.AGENT_RUNTIME_MODE_ALWAYS_ON,
                "cadence": orchestrator.AGENT_RUNTIME_MODE_ALWAYS_ON,
                "rrule": None,
                orchestrator.AGENT_RUNTIME_WORK_WINDOW_MINUTES_KEY: (
                    orchestrator.AGENT_RUNTIME_DEFAULT_WORK_WINDOW_MINUTES
                ),
                orchestrator.AGENT_RUNTIME_REST_MINUTES_KEY: orchestrator.AGENT_RUNTIME_DEFAULT_REST_MINUTES,
            },
        )
        assert updated["schedule"]["status"] == orchestrator.AGENT_SCHEDULE_STATUS_ACTIVE
        assert updated["schedule"][orchestrator.AGENT_RUNTIME_MODE_KEY] == orchestrator.AGENT_RUNTIME_MODE_ALWAYS_ON
        schedule = (
            db.query(ProjectAutonomyAgentSchedule)
            .filter(ProjectAutonomyAgentSchedule.profile_id == profile.id)
            .one()
        )
        start_at = schedule.next_run_at or datetime.utcnow()

        first = orchestrator.run_due_agent_cycles(db, now=start_at, limit=1)

        assert first["started"] == 1
        assert first["runs"][0]["agent_profile_id"] == profile.id
        assert schedule.next_run_at == start_at
        run = (
            db.query(ProjectAutonomyRun)
            .filter(ProjectAutonomyRun.run_id == first["runs"][0]["run_id"])
            .one()
        )
        run.status = orchestrator.RUN_STATUS_COMPLETED
        run.finished_at = start_at
        profile = db.get(ProjectAutonomyAgentProfile, profile.id)
        schedule_config = json.loads(profile.schedule_json)
        schedule_config[orchestrator.AGENT_RUNTIME_WORK_STARTED_AT_KEY] = (
            start_at - timedelta(minutes=orchestrator.AGENT_RUNTIME_DEFAULT_WORK_WINDOW_MINUTES)
        ).isoformat()
        schedule_config[orchestrator.AGENT_RUNTIME_REST_UNTIL_KEY] = None
        profile.schedule_json = json.dumps(schedule_config)
        schedule.next_run_at = start_at
        db.commit()

        rested = orchestrator.run_due_agent_cycles(db, now=start_at, limit=1)

        assert rested["started"] == 0
        assert rested["skipped"][0]["reason"] == orchestrator.AGENT_SCHEDULE_SKIP_RUNTIME_REST
        rest_until = start_at + timedelta(minutes=orchestrator.AGENT_RUNTIME_DEFAULT_REST_MINUTES)
        assert rested["skipped"][0]["rest_until"] == rest_until.isoformat()
        assert schedule.next_run_at == rest_until

        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)

        runtime_queue = readiness["runtime_queue"]
        assert runtime_queue["always_on_profile_count"] == 1
        assert runtime_queue["queued_count"] == 0
        assert runtime_queue["open_count"] == 0
        assert runtime_queue["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        runtime_check = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_RUNTIME_QUEUE
        )
        assert runtime_check["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
    finally:
        db.close()


def test_agent_os_readiness_compares_codex_automations_and_safety(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        codex_home = tmp_path / "codex-home"
        automation_dir = codex_home / "automations" / "agentops-director"
        automation_dir.mkdir(parents=True)
        repo_path = (tmp_path / "workspace").resolve()
        repo_path.mkdir()
        prompt_repo_path = str(repo_path).replace("\\", "/")
        (automation_dir / "automation.toml").write_text(
            "\n".join(
                [
                    'id = "agentops-director"',
                    'name = "AgentOps Director"',
                    'kind = "heartbeat"',
                    'status = "ACTIVE"',
                    'rrule = "FREQ=MINUTELY;INTERVAL=10"',
                    'prompt = """',
                    "Coordinate local CHILI workspace agents and report safety blockers.",
                    "Inbox: project_ws/AgentOps/IN. Output: project_ws/AgentOps/OUT.",
                    "Read project_ws/AGENT_MAILBOX_PROTOCOL.md, acquire run.lock, and post a draft PR review when needed.",
                    "Run `powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/agent-flow-health.ps1 -Root . -Json`.",
                    "Do not deploy, merge, restart services, or run migrations.",
                    '"""',
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [codex_home])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(repo_path), host_path=str(repo_path), name="workspace", active=True)
        db.add(repo)
        db.commit()

        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)

        assert readiness["status"] == orchestrator.AGENT_OS_READINESS_READY
        assert readiness["repo"]["resolved_path"] == str(repo_path)
        assert readiness["agents"]["total"] >= 1
        assert readiness["teams"]
        pm_team = next(
            team
            for team in readiness["teams"]
            if team["supervisor"]["profile_key"] == "product_pm"
        )
        assert pm_team["child_count"] >= 1
        assert pm_team["pending_question_count"] == 0
        assert pm_team["can_patch"] is False
        assert pm_team["can_merge"] is False
        assert any(
            team["supervisor"]["profile_key"] == "codex_agentops_director"
            for team in readiness["teams"]
        )
        dev_team = next(
            team
            for team in readiness["teams"]
            if team["supervisor"]["profile_key"] == "dev_lead"
        )
        assert {
            child["profile_key"]
            for child in dev_team["children"]
        } >= {"backend", "frontend", "software_engineer", "docs"}
        qa_team = next(
            team
            for team in readiness["teams"]
            if team["supervisor"]["profile_key"] == "qa_manager"
        )
        assert {
            child["profile_key"]
            for child in qa_team["children"]
        } >= {"qa", "security"}
        db_team = next(
            team
            for team in readiness["teams"]
            if team["supervisor"]["profile_key"] == "dba_architect"
        )
        assert {
            child["profile_key"]
            for child in db_team["children"]
        } >= {"db_quality", "data_scientist", "mlops"}
        assert readiness["codex_automations"]["matching"] == 1
        assert readiness["codex_automations"]["imported"] == 1
        assert readiness["codex_automations"]["current_imported"] == 1
        assert readiness["codex_automations"]["historical_imported"] == 0
        assert readiness["codex_automations"]["historical_profile_keys"] == []
        assert readiness["codex_automations"]["missing_profile_keys"] == []
        codex_bench = readiness[orchestrator.AGENT_CODEX_BENCH_KEY]
        assert codex_bench["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert codex_bench["matching_count"] == 1
        assert codex_bench["current_imported_count"] == 1
        assert codex_bench["source_active_count"] == 1
        assert codex_bench["source_active_disabled_count"] == 1
        assert codex_bench["next_action"] == orchestrator.AGENT_CODEX_BENCH_ACTION_ENABLE_ACTIVE
        contract_coverage = readiness["codex_automations"]["contract_coverage"]
        assert contract_coverage["total"] == 1
        assert contract_coverage["workspace_count"] == 1
        assert contract_coverage["missing_workspace_count"] == 0
        assert contract_coverage["inferred_workspace_count"] == 1
        assert contract_coverage["key_command_profile_count"] == 1
        assert contract_coverage["safety_boundary_profile_count"] == 1
        contract_check = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_CODEX_CONTRACTS
        )
        assert contract_check["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        codex_profiles = readiness["codex_automations"]["profiles"]
        assert len(codex_profiles) == 1
        assert codex_profiles[0]["profile_key"] == "codex_agentops_director"
        assert codex_profiles[0]["source_status"] == "ACTIVE"
        assert codex_profiles[0]["chili_status"] == orchestrator.AGENT_PROFILE_STATUS_PAUSED
        assert codex_profiles[0]["chili_schedule_enabled"] is False
        assert codex_profiles[0]["operating_state"]["state"] == orchestrator.AGENT_OPERATING_STATE_PAUSED_SOURCE_ACTIVE
        assert codex_profiles[0]["operating_state"]["next_action"] == orchestrator.AGENT_OPERATING_ACTION_ENABLE_ACTIVE
        assert codex_profiles[0]["prompt_freshness_status"] == orchestrator.CODEX_AUTOMATION_SYNC_STATUS_CURRENT
        assert codex_profiles[0]["can_patch"] is False
        assert codex_profiles[0]["can_merge"] is False
        assert "Coordinate local CHILI workspace agents" in codex_profiles[0]["prompt_preview"]
        contract = codex_profiles[0]["operating_contract"]
        assert contract["workspace"].replace("\\", "/") == prompt_repo_path
        assert contract["workspace_inferred_from_repo"] is True
        assert contract["inbox"] == "project_ws/AgentOps/IN"
        assert contract["output"] == "project_ws/AgentOps/OUT"
        assert contract["uses_mailbox_protocol"] is True
        assert contract["uses_run_lock"] is True
        assert contract["uses_pr_review_flow"] is True
        assert "deploy" in contract["safety_boundaries"]
        assert contract["cadence"] == "ten_minutes"
        assert readiness["local_model"]["coding_ready"] is True
        quality = readiness["quality_scorecard"]
        assert quality["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        assert quality["recent_run_count"] == 0
        assert "architect review" in quality["detail"]
        quality_monitor = readiness[orchestrator.AGENT_QUALITY_MONITOR_KEY]
        assert quality_monitor["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        assert quality_monitor["next_action"] == orchestrator.AGENT_QUALITY_MONITOR_ACTION_KEEP_MONITORING
        assert {
            dimension["key"]
            for dimension in quality_monitor["dimensions"]
        } >= {
            orchestrator.AGENT_QUALITY_MONITOR_DIMENSION_ARCHITECT,
            orchestrator.AGENT_QUALITY_MONITOR_DIMENSION_SCHEDULED,
            orchestrator.AGENT_QUALITY_MONITOR_DIMENSION_MODEL,
            orchestrator.AGENT_QUALITY_MONITOR_DIMENSION_CODEX,
            orchestrator.AGENT_QUALITY_MONITOR_DIMENSION_INBOX,
        }
        capability_audit = readiness[orchestrator.AGENT_OS_CAPABILITY_AUDIT_KEY]
        assert capability_audit["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert capability_audit["next_action"] == orchestrator.AGENT_OS_CAPABILITY_ACTION_ENABLE_ALWAYS_ON
        assert {
            capability["key"]
            for capability in capability_audit["capabilities"]
        } >= {
            orchestrator.AGENT_OS_CAPABILITY_REPO_RUNTIME,
            orchestrator.AGENT_OS_CAPABILITY_AGENT_HIERARCHY,
            orchestrator.AGENT_OS_CAPABILITY_CODEX_MIRROR,
            orchestrator.AGENT_OS_CAPABILITY_SAFE_DEFAULTS,
            orchestrator.AGENT_OS_CAPABILITY_ARCHITECT_QUALITY,
            orchestrator.AGENT_OS_CAPABILITY_ALWAYS_ON,
        }
        safety_capability = next(
            capability
            for capability in capability_audit["capabilities"]
            if capability["key"] == orchestrator.AGENT_OS_CAPABILITY_SAFE_DEFAULTS
        )
        assert safety_capability["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        codex_alignment = readiness["codex_alignment"]
        assert codex_alignment["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        assert codex_alignment["score"] >= orchestrator.AGENT_CODEX_ALIGNMENT_PASSING_SCORE
        assert codex_alignment["reference_count"] == 1
        assert codex_alignment["imported_count"] == 1
        assert codex_alignment["missing_profile_keys"] == []
        assert codex_alignment["extra_imported_count"] == 0
        assert codex_alignment["gaps"] == []
        assert {
            dimension["key"]
            for dimension in codex_alignment["dimensions"]
        } >= {
            orchestrator.AGENT_CODEX_ALIGNMENT_DIMENSION_IMPORT,
            orchestrator.AGENT_CODEX_ALIGNMENT_DIMENSION_CONTRACTS,
            orchestrator.AGENT_CODEX_ALIGNMENT_DIMENSION_RUNTIME,
            orchestrator.AGENT_CODEX_ALIGNMENT_DIMENSION_MODEL,
        }
        quality_bar = readiness[orchestrator.AGENT_CODING_QUALITY_BAR_KEY]
        assert quality_bar["score"] >= orchestrator.AGENT_CODING_QUALITY_BAR_TARGET_SCORE
        assert quality_bar["competitive"] is True
        assert quality_bar["gaps"] == []
        codex_alignment_check = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_CODEX_ALIGNMENT
        )
        assert codex_alignment_check["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        quality_check = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_QUALITY_GOVERNANCE
        )
        assert quality_check["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        safety = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_SAFE_DEFAULTS
        )
        assert safety["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        assert "No agent has worktree or merge permission" in safety["detail"]
    finally:
        db.close()


def test_agent_os_readiness_treats_removed_codex_automations_as_historical_audit(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        codex_home = tmp_path / "codex-home"
        automation_dir = codex_home / "automations" / "agentops-director"
        automation_dir.mkdir(parents=True)
        repo_path = (tmp_path / "workspace").resolve()
        repo_path.mkdir()
        (automation_dir / "automation.toml").write_text(
            "\n".join(
                [
                    'id = "agentops-director"',
                    'name = "AgentOps Director"',
                    'kind = "heartbeat"',
                    'status = "ACTIVE"',
                    'rrule = "FREQ=MINUTELY;INTERVAL=10"',
                    'prompt = """',
                    "Coordinate local CHILI workspace agents.",
                    "Inbox: project_ws/AgentOps/IN. Output: project_ws/AgentOps/OUT.",
                    "Read project_ws/AGENT_MAILBOX_PROTOCOL.md, acquire run.lock, and post a draft PR review when needed.",
                    "Run `powershell -File ./scripts/agent-flow-health.ps1`.",
                    "Do not deploy or merge.",
                    '"""',
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [codex_home])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(repo_path), host_path=str(repo_path), name="workspace", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        historical_prompt = "Old deleted Codex automation. Workspace: " + str(repo_path)
        db.add(
            ProjectAutonomyAgentProfile(
                repo_id=repo.id,
                profile_key="codex_deleted_agent",
                name="Deleted Codex Agent",
                role="dev_lead",
                tier=orchestrator.AGENT_PROFILE_TIER_MACRO,
                status=orchestrator.AGENT_PROFILE_STATUS_PAUSED,
                model_policy="local_first",
                prompt_setting_json=json.dumps(
                    {
                        "source": orchestrator.CODEX_AUTOMATION_SOURCE,
                        "system_prompt": historical_prompt,
                        "codex_automation": {
                            "id": "deleted-agent",
                            "status": "ACTIVE",
                            "operating_contract": {
                                "workspace": str(repo_path),
                                "d_drive_aligned": False,
                                "key_commands": ["powershell -File ./scripts/deleted.ps1"],
                                "safety_boundaries": ["merge"],
                            },
                        },
                    }
                ),
                permissions_json=json.dumps(orchestrator.DEFAULT_AGENT_PERMISSIONS),
                schedule_enabled=False,
                schedule_json=json.dumps(orchestrator.DEFAULT_AGENT_SCHEDULE),
                generated=True,
            )
        )
        db.commit()

        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)

        codex = readiness["codex_automations"]
        assert codex["matching"] == 1
        assert codex["current_imported"] == 1
        assert codex["historical_imported"] == 1
        assert codex["historical_profile_keys"] == ["codex_deleted_agent"]
        assert codex["stale_profile_keys"] == []
        parity = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_CODEX
        )
        freshness = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_CODEX_FRESHNESS
        )
        assert parity["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        assert freshness["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
        assert readiness["codex_alignment"]["gaps"] == []
    finally:
        db.close()


def test_agent_os_readiness_quality_scorecard_flags_risky_recent_runs(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        run = orchestrator.create_run(
            db,
            prompt="Improve the desktop Autopilot chat input.",
            repo_id=repo.id,
        )
        run.status = orchestrator.RUN_STATUS_AWAITING_APPROVAL
        run.plan_status = orchestrator.PLAN_STATUS_AWAITING_APPROVAL
        run.plan_json = json.dumps(
            {
                "analysis": "Improve the chat input ergonomics.",
                "files": [{"path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"}],
            }
        )
        db.add(
            ProjectAutonomyArtifact(
                run_id=run.run_id,
                artifact_type=orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_TYPE,
                name=orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_NAME,
                content_json=json.dumps(
                    {
                        "status": orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_LOW,
                        "score": 40,
                        "issues": ["false_action_claim"],
                    }
                ),
            )
        )
        db.commit()

        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)

        scorecard = readiness["quality_scorecard"]
        assert readiness["status"] == orchestrator.AGENT_OS_READINESS_NEEDS_ATTENTION
        assert scorecard["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert scorecard["recent_run_count"] == 1
        assert scorecard["approval_gate_risk_count"] == 1
        assert scorecard["architect_reviews"]["missing_for_approval"] == 1
        assert scorecard["scheduled_quality"]["low_quality"] == 1
        assert "quality gate" in scorecard["detail"]
        quality_monitor = readiness[orchestrator.AGENT_QUALITY_MONITOR_KEY]
        assert quality_monitor["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert quality_monitor["next_action"] == orchestrator.AGENT_QUALITY_MONITOR_ACTION_REVIEW_PLANS
        assert any(
            dimension["key"] == orchestrator.AGENT_QUALITY_MONITOR_DIMENSION_ARCHITECT
            and dimension["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
            for dimension in quality_monitor["dimensions"]
        )
        assert any(
            dimension["key"] == orchestrator.AGENT_QUALITY_MONITOR_DIMENSION_SCHEDULED
            and dimension["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
            for dimension in quality_monitor["dimensions"]
        )
        quality_check = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_QUALITY_GOVERNANCE
        )
        assert quality_check["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    finally:
        db.close()


def test_agent_os_readiness_visual_qa_flags_ui_runs_without_evidence(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        run = orchestrator.create_run(
            db,
            prompt="Polish the Flutter Autopilot cockpit layout.",
            repo_id=repo.id,
            start_planning=True,
        )
        run.status = orchestrator.RUN_STATUS_COMPLETED
        run.current_stage = orchestrator.STAGE_IMPLEMENT
        run.plan_json = json.dumps(
            {
                "analysis": "UI cockpit polish needs a screenshot pass.",
                "files": [
                    {
                        "path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                        "action": "modify",
                        "description": "Polish the Autopilot cockpit layout.",
                    }
                ],
            }
        )
        command_run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)
        db.commit()

        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)

        scorecard = readiness["quality_scorecard"]
        visual = scorecard["visual_qa"]
        assert scorecard["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert visual["ui_run_count"] == 1
        assert visual["evidenced_ui_run_count"] == 0
        assert visual["missing_ui_evidence_count"] == 1
        assert visual["missing_run_ids"] == [run.run_id]
        assert "screenshot or video evidence" in scorecard["detail"]
        quality_monitor = readiness[orchestrator.AGENT_QUALITY_MONITOR_KEY]
        visual_dimension = next(
            dimension
            for dimension in quality_monitor["dimensions"]
            if dimension["key"] == orchestrator.AGENT_QUALITY_MONITOR_DIMENSION_VISUAL_QA
        )
        assert visual_dimension["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert quality_monitor["next_action"] == orchestrator.AGENT_QUALITY_MONITOR_ACTION_ATTACH_VISUAL_QA
        assert quality_monitor["next_action_run_id"] == run.run_id
        assert "screenshot or video QA evidence" in quality_monitor["next_action_detail"]
        quality_bar = readiness[orchestrator.AGENT_CODING_QUALITY_BAR_KEY]
        quality_bar_dimension = next(
            dimension
            for dimension in quality_bar["dimensions"]
            if dimension["key"] == orchestrator.AGENT_CODING_QUALITY_BAR_DIMENSION_QUALITY
        )
        assert quality_bar_dimension["score"] < orchestrator.AGENT_CODING_QUALITY_BAR_TARGET_SCORE
        assert quality_bar_dimension["next_action_run_id"] == run.run_id
        assert quality_bar["next_action"] == orchestrator.AGENT_QUALITY_MONITOR_ACTION_ATTACH_VISUAL_QA
        assert quality_bar["next_action_run_id"] == run.run_id

        quality_payload = orchestrator.append_user_message(
            db,
            command_run.run_id,
            content="/quality",
        )
        quality_reply = quality_payload["messages"][-1]["content"]
        assert "Quality next action: Attach visual QA" in quality_reply
        assert f"Quality target run: {run.run_id}" in quality_reply
        assert f"Quality bar target run: {run.run_id}" in quality_reply

        orchestrator.record_visual_validation(
            db,
            run.run_id,
            kind="screenshot",
            path=r"D:\captures\autopilot_visual.png",
        )
        readiness_after = orchestrator.agent_os_readiness(db, repo_id=repo.id)

        visual_after = readiness_after["quality_scorecard"]["visual_qa"]
        assert visual_after["missing_ui_evidence_count"] == 0
        assert visual_after["evidenced_ui_run_count"] == 1
        assert visual_after["screenshot_count"] == 1
        quality_monitor_after = readiness_after[orchestrator.AGENT_QUALITY_MONITOR_KEY]
        visual_dimension_after = next(
            dimension
            for dimension in quality_monitor_after["dimensions"]
            if dimension["key"] == orchestrator.AGENT_QUALITY_MONITOR_DIMENSION_VISUAL_QA
        )
        assert visual_dimension_after["status"] == orchestrator.AGENT_OS_READINESS_CHECK_PASSED
    finally:
        db.close()


def test_agent_os_readiness_runtime_queue_flags_backlog(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        for index in range(orchestrator.AGENT_RUNTIME_QUEUE_WARNING_DEPTH + 1):
            run = orchestrator.create_run(
                db,
                prompt=f"Queued task {index}",
                repo_id=repo.id,
                start_planning=True,
            )
            run.status = orchestrator.RUN_STATUS_QUEUED
            run.current_stage = orchestrator.STAGE_QUEUED
        db.commit()

        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)

        runtime_queue = readiness["runtime_queue"]
        assert runtime_queue["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert runtime_queue["queued_count"] == orchestrator.AGENT_RUNTIME_QUEUE_WARNING_DEPTH + 1
        assert runtime_queue["fresh_queued_count"] == orchestrator.AGENT_RUNTIME_QUEUE_WARNING_DEPTH + 1
        assert runtime_queue["stale_queued_count"] == 0
        assert runtime_queue["active_count"] == 0
        assert runtime_queue["fresh_active_count"] == 0
        assert runtime_queue["active_runs"] == []
        assert len(runtime_queue["queued_runs"]) == orchestrator.AGENT_RUNTIME_QUEUE_PREVIEW_LIMIT
        assert runtime_queue["next_action"] == orchestrator.AGENT_RUNTIME_QUEUE_ACTION_DRAIN_QUEUED
        assert runtime_queue["next_action_label"] == "Start queued worker"
        assert runtime_queue["next_action_run_id"] == runtime_queue["queued_runs"][0]["run_id"]
        assert "queued run" in runtime_queue["detail"]
        runtime_check = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_RUNTIME_QUEUE
        )
        assert runtime_check["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    finally:
        db.close()


def test_agent_os_readiness_runtime_queue_flags_single_stale_queued_run(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        queued = orchestrator.create_run(
            db,
            prompt="Queued run with no worker.",
            repo_id=repo.id,
            start_planning=True,
        )
        queued.status = orchestrator.RUN_STATUS_QUEUED
        queued.current_stage = orchestrator.STAGE_QUEUED
        queued.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=orchestrator.AGENT_RUNTIME_QUEUE_STALE_QUEUED_MINUTES + 2
        )
        db.commit()

        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)

        runtime_queue = readiness["runtime_queue"]
        assert runtime_queue["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert runtime_queue["queued_count"] == 1
        assert runtime_queue["stale_queued_count"] == 1
        assert runtime_queue["fresh_queued_count"] == 0
        assert runtime_queue["stale_queued_after_minutes"] == orchestrator.AGENT_RUNTIME_QUEUE_STALE_QUEUED_MINUTES
        assert runtime_queue["next_action"] == orchestrator.AGENT_RUNTIME_QUEUE_ACTION_DRAIN_QUEUED
        assert runtime_queue["next_action_label"] == "Start queued worker"
        assert runtime_queue["next_action_run_id"] == queued.run_id
        assert runtime_queue["next_action_last_seen_age_minutes"] >= orchestrator.AGENT_RUNTIME_QUEUE_STALE_QUEUED_MINUTES
        assert runtime_queue["stale_queued_runs"][0]["run_id"] == queued.run_id
        assert "waited" in runtime_queue["detail"]
        runtime_check = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_RUNTIME_QUEUE
        )
        assert runtime_check["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    finally:
        db.close()


def test_autopilot_commands_prioritize_stale_runtime_targets(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)

        stale = orchestrator.create_run(
            db,
            prompt="Active run with no recent progress.",
            repo_id=repo.id,
            start_planning=True,
        )
        stale.status = orchestrator.RUN_STATUS_RUNNING
        stale.current_stage = orchestrator.STAGE_IMPLEMENT
        stale.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=orchestrator.AGENT_RUNTIME_QUEUE_STALE_ACTIVE_MINUTES + 5
        )
        command_run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)
        db.commit()

        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)
        runtime_queue = readiness["runtime_queue"]
        assert runtime_queue["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert runtime_queue["stale_active_count"] == 1
        assert runtime_queue["fresh_active_count"] == 0
        assert runtime_queue["fresh_active_runs"] == []
        assert runtime_queue["next_action"] == orchestrator.AGENT_RUNTIME_QUEUE_ACTION_INSPECT_STALE
        assert runtime_queue["next_action_label"] == "Inspect stale run"
        assert runtime_queue["next_action_run_id"] == stale.run_id
        assert runtime_queue["next_action_last_seen_at"]
        assert (
            runtime_queue["next_action_last_seen_age_minutes"]
            >= orchestrator.AGENT_RUNTIME_QUEUE_STALE_ACTIVE_MINUTES
        )
        assert runtime_queue["stale_active_runs"][0]["run_id"] == stale.run_id
        assert (
            runtime_queue["stale_active_runs"][0]["last_seen_age_minutes"]
            >= orchestrator.AGENT_RUNTIME_QUEUE_STALE_ACTIVE_MINUTES
        )

        payload = orchestrator.append_user_message(
            db,
            command_run.run_id,
            content="/doctor",
        )
        reply = payload["messages"][-1]["content"]
        assert "Runtime queue next action: Inspect stale run" in reply
        assert f"Runtime queue target run: {stale.run_id}" in reply
        assert f"Runtime stale target: {stale.run_id}" in reply
        assert "last seen" in reply
    finally:
        db.close()


def test_agent_os_readiness_operator_inbox_summarizes_pending_actions(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)

        approval = orchestrator.create_run(
            db,
            prompt="Add a focused inbox panel.",
            repo_id=repo.id,
            start_planning=True,
        )
        approval.status = orchestrator.RUN_STATUS_AWAITING_APPROVAL
        approval.plan_status = orchestrator.PLAN_STATUS_AWAITING_APPROVAL
        approval.current_stage = orchestrator.STAGE_PLAN

        clarification = orchestrator.create_run(
            db,
            prompt="Improve anything you find.",
            repo_id=repo.id,
            start_planning=True,
        )
        clarification.status = orchestrator.RUN_STATUS_AWAITING_CLARIFICATION
        clarification.plan_status = orchestrator.PLAN_STATUS_AWAITING_CLARIFICATION
        clarification.error_message = "Pick the workflow before planning."
        orchestrator.record_operator_question(
            db,
            clarification,
            "Which workflow should the architect inspect first?",
        )

        blocked = orchestrator.create_run(
            db,
            prompt="Implement a risky change.",
            repo_id=repo.id,
            start_planning=True,
        )
        blocked.status = orchestrator.RUN_STATUS_BLOCKED
        blocked.error_message = "Validation failed before merge."
        db.commit()

        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)

        inbox = readiness["operator_inbox"]
        assert inbox["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert inbox["approval_count"] == 1
        assert inbox["clarification_count"] == 1
        assert inbox["pending_question_count"] == 1
        assert inbox["blocked_count"] == 1
        assert inbox["total_action_count"] == 4
        assert "approval" in inbox["detail"]
        assert "clarification" in inbox["detail"]
        assert inbox["next_action"] == orchestrator.AGENT_OPERATOR_INBOX_ACTION_ANSWER_QUESTION
        assert inbox["next_action_label"] == "Answer question"
        assert inbox["next_action_kind"] == orchestrator.AGENT_OPERATOR_INBOX_ITEM_QUESTION
        assert inbox["next_action_run_id"] == clarification.run_id
        assert inbox["next_action_agent"] == "Product PM"
        assert "Which workflow" in inbox["next_action_detail"]
        assert {item["kind"] for item in inbox["items"]} >= {
            orchestrator.AGENT_OPERATOR_INBOX_ITEM_APPROVAL,
            orchestrator.AGENT_OPERATOR_INBOX_ITEM_CLARIFICATION,
            orchestrator.AGENT_OPERATOR_INBOX_ITEM_QUESTION,
            orchestrator.AGENT_OPERATOR_INBOX_ITEM_BLOCKER,
        }
        blocker_item = next(
            item
            for item in inbox["items"]
            if item["kind"] == orchestrator.AGENT_OPERATOR_INBOX_ITEM_BLOCKER
        )
        assert blocker_item["recovery_action"] == orchestrator.AGENT_OPERATOR_INBOX_RECOVERY_RERUN_SAFE
        assert blocker_item["action_label"] == "Rerun"
        assert "Rerun safely" in blocker_item["reason"]
        inbox_check = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_OPERATOR_INBOX
        )
        assert inbox_check["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    finally:
        db.close()


def test_agent_os_readiness_operator_inbox_guides_blocker_recovery(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        blocked = orchestrator.create_run(
            db,
            prompt="Repair the cockpit blocker flow.",
            repo_id=repo.id,
            start_planning=True,
        )
        blocked.status = orchestrator.RUN_STATUS_BLOCKED
        blocked.current_stage = orchestrator.STAGE_IMPLEMENT
        blocked.merge_status = "blocked"
        blocked.merge_message = "Validation failed after repair."
        command_run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)
        db.commit()

        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)
        inbox = readiness["operator_inbox"]

        assert inbox["blocked_count"] == 1
        assert inbox["next_action"] == orchestrator.AGENT_OPERATOR_INBOX_ACTION_RECOVER_BLOCKER
        assert inbox["next_action_label"] == "Recover blocker"
        assert inbox["next_action_kind"] == orchestrator.AGENT_OPERATOR_INBOX_ITEM_BLOCKER
        assert inbox["next_action_run_id"] == blocked.run_id
        assert inbox["next_action_recovery_action"] == orchestrator.AGENT_OPERATOR_INBOX_RECOVERY_RERUN_SAFE
        assert inbox["next_action_button_label"] == "Rerun"
        assert "validation evidence before merge" in inbox["next_action_detail"]
        blocker_item = inbox["items"][0]
        assert blocker_item["run_id"] == blocked.run_id
        assert blocker_item["recovery_detail"] == "Prefill a fresh approval-first draft from this run."
        quality_bar = readiness[orchestrator.AGENT_CODING_QUALITY_BAR_KEY]
        operator_dimension = next(
            dimension
            for dimension in quality_bar["dimensions"]
            if dimension["key"] == orchestrator.AGENT_CODING_QUALITY_BAR_DIMENSION_OPERATOR
        )
        assert operator_dimension["next_action"] == orchestrator.AGENT_OPERATOR_INBOX_ACTION_RECOVER_BLOCKER
        assert operator_dimension["next_action_label"] == "Recover blocker"
        assert operator_dimension["next_action_run_id"] == blocked.run_id
        assert operator_dimension["next_action_kind"] == orchestrator.AGENT_OPERATOR_INBOX_ITEM_BLOCKER
        assert operator_dimension["next_action_recovery_action"] == orchestrator.AGENT_OPERATOR_INBOX_RECOVERY_RERUN_SAFE
        assert operator_dimension["next_action_button_label"] == "Rerun"
        assert quality_bar["next_action"] == orchestrator.AGENT_OPERATOR_INBOX_ACTION_RECOVER_BLOCKER
        assert quality_bar["next_action_run_id"] == blocked.run_id
        assert quality_bar["next_action_recovery_action"] == orchestrator.AGENT_OPERATOR_INBOX_RECOVERY_RERUN_SAFE
        assert quality_bar["next_action_button_label"] == "Rerun"

        doctor_payload = orchestrator.append_user_message(
            db,
            command_run.run_id,
            content="/doctor",
        )
        quality_payload = orchestrator.append_user_message(
            db,
            command_run.run_id,
            content="/quality",
        )

        for payload in (doctor_payload, quality_payload):
            reply = payload["messages"][-1]["content"]
            assert "Operator inbox next action: Recover blocker" in reply
            assert "Quality bar next action: Recover blocker" in reply
            assert "Validation failed after repair" in reply
            assert f"Operator inbox target run: {blocked.run_id}" in reply
            assert f"Quality bar target run: {blocked.run_id}" in reply
    finally:
        db.close()


def test_agent_os_readiness_warns_when_only_general_local_model_exists(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen3:4b",
                "available": True,
                "installed_models": ["qwen3:4b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)

        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)

        assert readiness["status"] == orchestrator.AGENT_OS_READINESS_NEEDS_ATTENTION
        assert readiness["warnings"] == 1
        assert readiness["local_model"]["coding_ready"] is False
        quality_bar = readiness[orchestrator.AGENT_CODING_QUALITY_BAR_KEY]
        assert quality_bar["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert quality_bar["target_score"] == orchestrator.AGENT_CODING_QUALITY_BAR_TARGET_SCORE
        assert quality_bar["competitive"] is False
        assert quality_bar["next_action"] == orchestrator.AGENT_CODING_QUALITY_BAR_ACTION_INSTALL_MODEL
        assert any(
            dimension["key"] == orchestrator.AGENT_CODING_QUALITY_BAR_DIMENSION_LOCAL_MODEL
            and dimension["score"] < orchestrator.AGENT_CODING_QUALITY_BAR_TARGET_SCORE
            for dimension in quality_bar["dimensions"]
        )
        local_model_check = next(
            check
            for check in readiness["checks"]
            if check["key"] == orchestrator.AGENT_OS_READINESS_CHECK_LOCAL_MODEL
        )
        assert local_model_check["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
        assert "install a coder-tuned model" in local_model_check["detail"]
    finally:
        db.close()


def test_coding_quality_bar_preserves_runtime_recovery_target():
    payload = orchestrator._agent_coding_quality_bar_payload(
        local_model={
            "coding_ready": True,
            "available": True,
            "detail": "Coder model is ready.",
        },
        quality_monitor={
            "status": orchestrator.AGENT_OS_READINESS_CHECK_PASSED,
            "score": 100,
            "detail": "Quality monitor is healthy.",
        },
        capability_audit={
            "status": orchestrator.AGENT_OS_READINESS_CHECK_PASSED,
            "score": 100,
            "detail": "Capability audit is healthy.",
        },
        codex_alignment={
            "status": orchestrator.AGENT_OS_READINESS_CHECK_PASSED,
            "score": 100,
            "detail": "Codex parity is healthy.",
        },
        runtime_queue={
            "status": orchestrator.AGENT_OS_READINESS_CHECK_WARNING,
            "detail": "A queued run is waiting for a worker.",
            "next_action": orchestrator.AGENT_RUNTIME_QUEUE_ACTION_DRAIN_QUEUED,
            "next_action_label": "Start queued worker",
            "next_action_detail": "Use Start in the recovery queue.",
            "next_action_run_id": "pa_queued",
        },
        operator_inbox={
            "total_action_count": 0,
            "detail": "No operator action is waiting.",
        },
    )

    runtime_dimension = next(
        dimension
        for dimension in payload["dimensions"]
        if dimension["key"] == orchestrator.AGENT_CODING_QUALITY_BAR_DIMENSION_RUNTIME
    )
    assert payload["competitive"] is False
    assert payload["next_action"] == orchestrator.AGENT_RUNTIME_QUEUE_ACTION_DRAIN_QUEUED
    assert payload["next_action_label"] == "Start queued worker"
    assert payload["next_action_run_id"] == "pa_queued"
    assert runtime_dimension["next_action"] == orchestrator.AGENT_RUNTIME_QUEUE_ACTION_DRAIN_QUEUED
    assert runtime_dimension["next_action_run_id"] == "pa_queued"
    lines = orchestrator._autopilot_coding_quality_bar_lines(
        {orchestrator.AGENT_CODING_QUALITY_BAR_KEY: payload}
    )
    assert any("Quality bar next action: Start queued worker" in line for line in lines)
    assert "Quality bar target run: pa_queued" in lines


def test_autopilot_slash_commands_answer_in_chat_and_start_plan(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)

        helped = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/help",
        )

        assert helped["status"] == orchestrator.RUN_STATUS_CHATTING
        assert helped["messages"][-1]["message_type"] == orchestrator.AUTOPILOT_COMMAND_MESSAGE_TYPE
        assert "/agents" in helped["messages"][-1]["content"]

        agents = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/agents",
        )

        assert "Repo agents:" in agents["messages"][-1]["content"]
        assert "Architect" in agents["messages"][-1]["content"]
        assert agents["status"] == orchestrator.RUN_STATUS_CHATTING

        planning = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/plan",
        )

        assert planning["status"] == orchestrator.RUN_STATUS_QUEUED
        assert planning["plan_status"] == orchestrator.PLAN_STATUS_DRAFTING
        assert planning["messages"][-1]["content"].startswith("Got it.")
    finally:
        db.close()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("what's the status?", "Run status is"),
        ("what's happening with this run?", "Run status is"),
        ("is it running right now?", "Run status is"),
        ("what's blocking this?", "Run status is"),
        ("why are we waiting?", "Run status is"),
        ("what's the schedule?", "schedule is"),
        ("when is the next run scheduled?", "next not scheduled"),
        ("which agents are configured?", "Repo agents:"),
        ("which model are you using?", "model policy"),
        ("what quality guardrails are active?", "Local model quality guardrails"),
        ("any pending questions?", "No pending operator questions"),
        ("what repo is this run using?", "repo"),
        ("what branch is this on?", "Branch:"),
        ("what was my prompt?", "Original request: hello"),
        ("what is the run id?", "Run "),
        ("what is the merge status?", "merge status"),
        ("what is the plan status?", "Plan status:"),
        ("what's next?", "Next action:"),
        ("what are the next steps?", "Next action:"),
        ("can I approve this?", "Next action:"),
        ("is this ready to merge?", "Next action:"),
        ("how do I attach a screenshot?", "attachment control"),
        ("where do I start the plan?", orchestrator.PLAN_START_CHAT_ACTION_LABEL),
        ("how do I approve and merge this?", "approval-ready plan"),
        ("how can I stop this run?", "cancel or stop action"),
    ],
)
def test_autopilot_chat_read_only_questions_use_mechanics_without_model(
    tmp_path,
    monkeypatch,
    content,
    expected,
):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)

        def fail_model_selection():
            raise AssertionError("read-only Autopilot cockpit questions should not call a model")

        monkeypatch.setattr(orchestrator, "select_local_model", fail_model_selection)

        payload = orchestrator.append_user_message(db, run.run_id, content=content)

        assert payload["status"] == orchestrator.RUN_STATUS_CHATTING
        assert expected in payload["messages"][-1]["content"]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("what files did you change?", "app/example.py"),
        ("what changed?", "app/example.py"),
        ("show changes", "app/example.py"),
        ("what tests ran?", "pytest tests/test_example.py -q"),
        ("what checks passed?", "pytest tests/test_example.py -q"),
        ("show me the evidence", "validation: validation_results"),
        ("show receipts", "validation: validation_results"),
    ],
)
def test_autopilot_chat_audit_questions_use_recorded_artifacts_without_model(
    tmp_path,
    monkeypatch,
    content,
    expected,
):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)
        run.files_json = json.dumps(["app/example.py", "tests/test_example.py"])
        run.validation_json = json.dumps([{"command": "pytest tests/test_example.py -q"}])
        db.add(
            ProjectAutonomyArtifact(
                run_id=run.run_id,
                artifact_type="validation",
                name="validation_results",
                content_json=json.dumps({"status": "passed"}),
                byte_length=20,
            )
        )
        db.commit()

        def fail_model_selection():
            raise AssertionError("Autopilot audit questions should not call a model")

        monkeypatch.setattr(orchestrator, "select_local_model", fail_model_selection)

        payload = orchestrator.append_user_message(db, run.run_id, content=content)

        assert payload["status"] == orchestrator.RUN_STATUS_CHATTING
        assert expected in payload["messages"][-1]["content"]
    finally:
        db.close()


def test_autopilot_reference_command_blocks_tainted_local_source(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        repo = CodeRepo(path=str(repo_path), name="repo", active=True)
        reference = tmp_path / "reference"
        reference.mkdir()
        (reference / "package.json").write_text(
            json.dumps(
                {
                    "name": "@anthropic-ai/claude-code",
                    "version": "0.0.0-leaked",
                    "description": "Leaked source; not an official release.",
                    "license": "UNLICENSED",
                    "dependencies": {
                        "@modelcontextprotocol/sdk": "^1.0.0",
                        "proper-lockfile": "^4.0.0",
                    },
                }
            ),
            encoding="utf-8",
        )
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)

        payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content=f'/reference "{reference}"',
        )

        reply = payload["messages"][-1]["content"]
        assert "tainted source blocked" in reply
        assert "leaked" in reply
        assert "unlicensed" in reply.lower()
        assert "MCP connector" in reply
        assert "I will not read, copy, summarize, or train" in reply
    finally:
        db.close()


def test_autopilot_model_command_reports_local_coder_readiness(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen3:4b",
                "available": True,
                "installed_models": ["qwen3:4b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)

        payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/model",
        )

        reply = payload["messages"][-1]["content"]
        assert "Local model: qwen3:4b" in reply
        assert "install a coder-tuned model" in reply
        assert "ollama pull qwen2.5-coder:7b" in reply
    finally:
        db.close()


def test_autopilot_doctor_command_summarizes_agent_os_readiness(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen3:4b",
                "available": True,
                "installed_models": ["qwen3:4b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)

        payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/doctor",
        )

        reply = payload["messages"][-1]["content"]
        assert payload["messages"][-1]["message_type"] == orchestrator.AUTOPILOT_COMMAND_MESSAGE_TYPE
        assert "Agent OS doctor" in reply
        assert "needs attention" in reply
        assert "qwen3:4b" in reply
        assert "ollama pull qwen2.5-coder:7b" in reply
        assert "Agents:" in reply
        assert "Codex parity" in reply
        assert "Codex bench" in reply
        assert "Safety:" in reply
        assert "Local quality monitor" in reply
        assert "Agent OS capability audit" in reply
        assert "Capability gaps:" in reply
        assert "Local model bridge" in reply
        assert "Codex/Claude quality bar" in reply
        assert "Quality bar next action: Install coder model" in reply
        assert "Quality next action: Install coder model" in reply
    finally:
        db.close()


def test_autopilot_quality_command_explains_local_model_guardrails(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen3:4b",
                "available": True,
                "installed_models": ["qwen3:4b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)

        payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/quality",
        )

        reply = payload["messages"][-1]["content"]
        assert payload["messages"][-1]["message_type"] == orchestrator.AUTOPILOT_COMMAND_MESSAGE_TYPE
        assert "Autopilot quality report" in reply
        assert "qwen3:4b" in reply
        assert f"{orchestrator.ARCHITECT_REVIEW_PASSING_SCORE}/100" in reply
        assert f"{orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_PASSING_SCORE}/100" in reply
        assert "observe, research, and plan" in reply
        assert "ollama pull qwen2.5-coder:7b" in reply
        assert "Local quality monitor" in reply
        assert "Codex bench" in reply
        assert "Agent OS capability audit" in reply
        assert "Capability gaps:" in reply
        assert "Local model bridge" in reply
        assert "Codex/Claude quality bar" in reply
        assert "Quality bar next action: Install coder model" in reply
        assert "Quality next action: Install coder model" in reply
    finally:
        db.close()


def test_autopilot_task_board_surfaces_active_approval_item(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="Improve the Autopilot task board.", repo_id=repo.id)
        run.status = orchestrator.RUN_STATUS_AWAITING_APPROVAL
        run.current_stage = orchestrator.STAGE_PLAN
        run.plan_status = orchestrator.PLAN_STATUS_AWAITING_APPROVAL
        run.plan_json = json.dumps(
            {
                "analysis": "Add a visible task board to the run cockpit.",
                "files": [{"path": "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"}],
            }
        )
        db.commit()

        payload = orchestrator.run_payload(db, run, include_events=True)

        task_board = payload["task_board"]
        assert task_board["schema"] == "chili.autopilot.task_board.v1"
        assert task_board["active_item"]["key"] == "approve_plan"
        assert task_board["active_item"]["status"] == orchestrator.AUTOPILOT_TASK_STATUS_IN_PROGRESS
        assert task_board["active_item"]["next_action"] == orchestrator.AUTOPILOT_TASK_ACTION_APPROVE_PLAN
        assert task_board["active_item"]["next_action_label"] == "Approve"
        assert task_board["active_item"]["next_action_run_id"] == run.run_id
        assert any(
            item["key"] == "plan_quality_gate"
            and item["status"] == orchestrator.AUTOPILOT_TASK_STATUS_COMPLETED
            for item in task_board["items"]
        )

        task_payload = orchestrator.append_user_message(db, run.run_id, content="/tasks")
        task_reply = task_payload["messages"][-1]["content"]
        assert "Task board:" in task_reply
        assert "Active item: Approve plan" in task_reply
        assert "Next task action: Approve." in task_reply
        assert "Review the plan" in task_reply

        chat_payload = orchestrator.append_user_message(db, run.run_id, content="what's left?")
        assert "Task board:" in chat_payload["messages"][-1]["content"]
        assert chat_payload["status"] == orchestrator.RUN_STATUS_AWAITING_APPROVAL
        assert chat_payload["plan_status"] == orchestrator.PLAN_STATUS_AWAITING_APPROVAL
    finally:
        db.close()


def test_autopilot_task_board_routes_recovery_actions(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()

        chatting = orchestrator.create_run(db, prompt="Brainstorm task board actions.", repo_id=repo.id)
        chatting_payload = orchestrator.run_payload(db, chatting, include_events=True)
        chatting_task = chatting_payload["task_board"]["active_item"]
        assert chatting_task["key"] == "plan_quality_gate"
        assert chatting_task["next_action"] == orchestrator.AUTOPILOT_TASK_ACTION_START_PLAN
        assert chatting_task["next_action_label"] == orchestrator.PLAN_START_CHAT_ACTION_LABEL

        queued = orchestrator.create_run(
            db,
            prompt="Queued approved run.",
            repo_id=repo.id,
            start_planning=True,
        )
        queued.status = orchestrator.RUN_STATUS_QUEUED
        queued.current_stage = orchestrator.STAGE_QUEUED
        queued.plan_status = orchestrator.PLAN_STATUS_APPROVED
        queued_payload = orchestrator.run_payload(db, queued, include_events=True)
        queued_task = queued_payload["task_board"]["active_item"]
        assert queued_task["key"] == "implement"
        assert queued_task["next_action"] == orchestrator.AUTOPILOT_TASK_ACTION_START_WORKER
        assert queued_task["next_action_run_id"] == queued.run_id

        blocked = orchestrator.create_run(
            db,
            prompt="Blocked implementation.",
            repo_id=repo.id,
            start_planning=True,
        )
        blocked.status = orchestrator.RUN_STATUS_BLOCKED
        blocked.current_stage = orchestrator.STAGE_IMPLEMENT
        blocked.plan_status = orchestrator.PLAN_STATUS_APPROVED
        blocked.error_message = "Validation failed after repair."
        blocked_payload = orchestrator.run_payload(db, blocked, include_events=True)
        blocked_task = blocked_payload["task_board"]["active_item"]
        assert blocked_task["next_action"] == orchestrator.AUTOPILOT_TASK_ACTION_RECOVER_BLOCKER
        assert blocked_task["next_action_kind"] == orchestrator.AGENT_OPERATOR_INBOX_ITEM_BLOCKER
        assert blocked_task["next_action_recovery_action"] == orchestrator.AGENT_OPERATOR_INBOX_RECOVERY_RERUN_SAFE
    finally:
        db.close()


def test_autopilot_commands_surface_runtime_queue_run_targets(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)

        queued = orchestrator.create_run(
            db,
            prompt="Queued recovery target.",
            repo_id=repo.id,
            start_planning=True,
        )
        queued.status = orchestrator.RUN_STATUS_QUEUED
        queued.current_stage = orchestrator.STAGE_QUEUED
        active = orchestrator.create_run(
            db,
            prompt="Active recovery target.",
            repo_id=repo.id,
            start_planning=True,
        )
        active.status = orchestrator.RUN_STATUS_RUNNING
        active.current_stage = orchestrator.STAGE_IMPLEMENT
        waiting = orchestrator.create_run(
            db,
            prompt="Waiting recovery target.",
            repo_id=repo.id,
            start_planning=True,
        )
        waiting.status = orchestrator.RUN_STATUS_AWAITING_APPROVAL
        waiting.plan_status = orchestrator.PLAN_STATUS_AWAITING_APPROVAL
        waiting.current_stage = orchestrator.STAGE_PLAN
        command_run = orchestrator.create_run(db, prompt="hello", repo_id=repo.id)
        db.commit()

        doctor_payload = orchestrator.append_user_message(
            db,
            command_run.run_id,
            content="/doctor",
        )
        quality_payload = orchestrator.append_user_message(
            db,
            command_run.run_id,
            content="/quality",
        )

        doctor_reply = doctor_payload["messages"][-1]["content"]
        quality_reply = quality_payload["messages"][-1]["content"]
        for reply in (doctor_reply, quality_reply):
            assert "Runtime queue recovery:" in reply
            assert "Runtime queue next action: Inspect active run" in reply
            assert f"Runtime queue target run: {active.run_id}" in reply
            assert "Runtime queue targets:" in reply
            assert f"Runtime queued target: {queued.run_id}" in reply
            assert f"Runtime active target: {active.run_id}" in reply
        readiness = orchestrator.agent_os_readiness(db, repo_id=repo.id)
        runtime_queue = readiness["runtime_queue"]
        assert runtime_queue["fresh_active_count"] == 1
        assert runtime_queue["fresh_active_runs"][0]["run_id"] == active.run_id
    finally:
        db.close()


def test_autopilot_commands_surface_operator_inbox_next_action(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        run = orchestrator.create_run(
            db,
            prompt="Improve the Autopilot cockpit.",
            repo_id=repo.id,
            start_planning=True,
        )
        run.status = orchestrator.RUN_STATUS_AWAITING_CLARIFICATION
        run.plan_status = orchestrator.PLAN_STATUS_AWAITING_CLARIFICATION
        run.error_message = "Pick the cockpit workflow before planning."
        orchestrator.record_operator_question(
            db,
            run,
            "Which cockpit workflow should the architect inspect first?",
        )
        db.commit()

        doctor_payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/doctor",
        )
        quality_payload = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/quality",
        )

        doctor_reply = doctor_payload["messages"][-1]["content"]
        quality_reply = quality_payload["messages"][-1]["content"]
        for reply in (doctor_reply, quality_reply):
            assert "Operator inbox next action: Answer question - Product PM" in reply
            assert "Which cockpit workflow should the architect inspect first?" in reply
            assert f"Operator inbox target run: {run.run_id}" in reply
    finally:
        db.close()


def test_autopilot_cold_slash_command_run_returns_command_report(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [])
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "model": "qwen2.5-coder:7b",
                "available": True,
                "installed_models": ["qwen2.5-coder:7b"],
                "skipped_models": {},
                "recommendation": None,
            },
        )
        repo = CodeRepo(path=str(tmp_path), host_path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()

        run = orchestrator.create_run(
            db,
            prompt="/quality",
            repo_id=repo.id,
            start_planning=False,
        )
        payload = orchestrator.run_payload(db, run, include_events=True)

        assert payload["status"] == orchestrator.RUN_STATUS_CHATTING
        assert payload["messages"][-1]["message_type"] == orchestrator.AUTOPILOT_COMMAND_MESSAGE_TYPE
        assert "Autopilot quality report" in payload["messages"][-1]["content"]
        assert "Codex/Claude quality bar" in payload["messages"][-1]["content"]
    finally:
        db.close()


def test_autopilot_slash_schedule_and_clear_are_safe(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        agents = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        architect_id = next(agent["id"] for agent in agents if agent["profile_key"] == "architect")
        run = orchestrator.create_run(
            db,
            prompt="scheduled agent chat",
            repo_id=repo.id,
            agent_profile_id=architect_id,
        )

        scheduled = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/schedule on",
        )

        assert "Schedule enabled" in scheduled["messages"][-1]["content"]
        profile = db.get(ProjectAutonomyAgentProfile, architect_id)
        assert profile.status == orchestrator.AGENT_PROFILE_STATUS_ACTIVE
        assert profile.schedule_enabled is True
        schedule = (
            db.query(ProjectAutonomyAgentSchedule)
            .filter(ProjectAutonomyAgentSchedule.profile_id == architect_id)
            .one()
        )
        assert schedule.status == orchestrator.AGENT_SCHEDULE_STATUS_ACTIVE
        assert schedule.next_run_at is not None

        paused = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/schedule pause",
        )

        assert "Schedule paused" in paused["messages"][-1]["content"]
        profile = db.get(ProjectAutonomyAgentProfile, architect_id)
        assert profile.status == orchestrator.AGENT_PROFILE_STATUS_PAUSED
        assert profile.schedule_enabled is False

        cleared = orchestrator.append_user_message(
            db,
            run.run_id,
            content="/clear",
        )

        assert cleared["archived"] is True
        assert cleared["archive_reason"] == orchestrator.AUTOPILOT_COMMAND_CLEAR_ARCHIVE_REASON
        assert "Audit data" in cleared["messages"][-1]["content"]
    finally:
        db.close()


def test_autopilot_schedule_codex_mirror_command_controls_plan_only_schedules(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        codex_home = tmp_path / "codex-home"
        active_dir = codex_home / "automations" / "agentops-director"
        paused_dir = codex_home / "automations" / "performance-bottleneck-research"
        active_dir.mkdir(parents=True)
        paused_dir.mkdir(parents=True)
        repo_path = (tmp_path / "workspace").resolve()
        repo_path.mkdir()
        prompt_repo_path = str(repo_path).replace("\\", "/")
        (active_dir / "automation.toml").write_text(
            "\n".join(
                [
                    'id = "agentops-director"',
                    'name = "AgentOps Director"',
                    'kind = "heartbeat"',
                    'status = "ACTIVE"',
                    'rrule = "FREQ=MINUTELY;INTERVAL=5"',
                    'prompt = """',
                    f"Workspace: {prompt_repo_path}",
                    "Monitor local agent flow.",
                    '"""',
                ]
            ),
            encoding="utf-8",
        )
        (paused_dir / "automation.toml").write_text(
            "\n".join(
                [
                    'id = "performance-bottleneck-research"',
                    'name = "Performance Bottleneck Research"',
                    'kind = "cron"',
                    'status = "PAUSED"',
                    'rrule = "FREQ=HOURLY;INTERVAL=6"',
                    'prompt = "Research this repository for low-risk performance work."',
                    f'cwds = ["{str(repo_path).replace("\\", "\\\\")}"]',
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [codex_home])
        repo = CodeRepo(path=str(repo_path), host_path=str(repo_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        run = orchestrator.create_run(db, prompt="agent ops chat", repo_id=repo.id)

        inspected = orchestrator.append_user_message(db, run.run_id, content="/schedule codex")
        assert "0 of 1 source-active" in inspected["messages"][-1]["content"]

        enabled = orchestrator.append_user_message(db, run.run_id, content="/schedule codex-active")
        assert "Enabled 1 source-active Codex schedules" in enabled["messages"][-1]["content"]
        assert "1 of 1 source-active" in enabled["messages"][-1]["content"]
        profiles = orchestrator.list_agent_profiles(db, repo_id=repo.id)
        active = next(profile for profile in profiles if profile["profile_key"] == "codex_agentops_director")
        paused = next(profile for profile in profiles if profile["profile_key"] == "codex_performance_bottleneck_research")
        assert active["status"] == orchestrator.AGENT_PROFILE_STATUS_ACTIVE
        assert active["schedule_enabled"] is True
        assert active["permissions"]["worktree"] is False
        assert active["permissions"]["merge"] is False
        assert paused["status"] == orchestrator.AGENT_PROFILE_STATUS_PAUSED
        assert paused["schedule_enabled"] is False

        always_on = orchestrator.append_user_message(db, run.run_id, content="/schedule codex-always-on")
        assert "Enabled 1 source-active Codex agents as always-on queues" in always_on["messages"][-1]["content"]
        assert "1 always-on, 0 scheduled" in always_on["messages"][-1]["content"]
        profiles = orchestrator.list_agent_profiles(db, repo_id=repo.id)
        active = next(profile for profile in profiles if profile["profile_key"] == "codex_agentops_director")
        assert active["schedule"]["runtime_mode"] == orchestrator.AGENT_RUNTIME_MODE_ALWAYS_ON
        assert active["schedule"]["rrule"] is None
        assert active["schedule"]["source_rrule"] == "FREQ=MINUTELY;INTERVAL=5"

        paused_payload = orchestrator.append_user_message(db, run.run_id, content="/schedule codex-pause")
        assert "Paused 2 Codex automation schedules" in paused_payload["messages"][-1]["content"]
        after_pause = orchestrator.list_agent_profiles(db, repo_id=repo.id)
        assert all(
            profile["schedule_enabled"] is False
            for profile in after_pause
            if (profile.get("prompt_setting") or {}).get("source") == orchestrator.CODEX_AUTOMATION_SOURCE
        )
    finally:
        db.close()


def test_agent_profile_prompt_and_model_setting_snapshot_into_runs(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        agents = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        architect_id = next(agent["id"] for agent in agents if agent["profile_key"] == "architect")

        updated = orchestrator.update_agent_profile(
            db,
            architect_id,
            model_policy="local_only",
            prompt_setting={
                "source": "desktop_custom",
                "system_prompt": "Operate as the repo-specific test architect.",
            },
        )
        run = orchestrator.create_run(
            db,
            prompt="Brainstorm a safe next step.",
            repo_id=repo.id,
            agent_profile_id=architect_id,
        )
        payload = orchestrator.run_payload(db, run, include_events=True)

        assert updated["model_policy"] == "local_only"
        assert updated["prompt_setting"]["source"] == "desktop_custom"
        assert updated["prompt_setting"]["system_prompt"] == "Operate as the repo-specific test architect."
        assert payload["model_policy"] == "local_only"
        assert payload["agent_snapshot"]["model_policy"] == "local_only"
        assert payload["agent_snapshot"]["prompt_setting"]["source"] == "desktop_custom"
        assert "repo-specific test architect" in payload["agent_snapshot"]["prompt_setting"]["system_prompt"]
    finally:
        db.close()


def test_standalone_agent_scheduler_runs_when_backend_scheduler_role_is_none(tmp_path, monkeypatch):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        agents = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        architect_id = next(agent["id"] for agent in agents if agent["profile_key"] == "architect")
        orchestrator.update_agent_profile(
            db,
            architect_id,
            status="active",
            schedule_enabled=True,
            schedule={
                "cadence": "two_minutes",
                "rrule": "FREQ=MINUTELY;INTERVAL=2",
                "budget": {"max_minutes": 20, "max_child_runs": 0},
            },
        )
        schedule = (
            db.query(ProjectAutonomyAgentSchedule)
            .filter(ProjectAutonomyAgentSchedule.profile_id == architect_id)
            .one()
        )
        schedule.next_run_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
        started_workers: list[str] = []
        monkeypatch.setattr(agent_scheduler, "SessionLocal", lambda: db)
        monkeypatch.setattr(
            agent_scheduler,
            "start_worker",
            lambda run_id: (
                started_workers.append(run_id)
                or {
                    agent_scheduler.SCHEDULER_WORKER_START_STARTED: True,
                    "run_id": run_id,
                }
            ),
        )

        result = agent_scheduler.run_once()

        assert agent_scheduler.should_start_standalone_scheduler("none") is True
        assert agent_scheduler.should_start_standalone_scheduler("all") is False
        assert result["started"] == 1
        assert result["worker_started"] == 1
        assert result["worker_deferred"] == []
        assert started_workers == [result["runs"][0]["run_id"]]
        assert result["runs"][0]["agent_profile_id"] == architect_id
        assert result["runs"][0]["execution_mode"] == orchestrator.EXECUTION_MODE_PLAN_APPROVAL
        info = agent_scheduler.scheduler_info()
        assert info["last_poll_at"]
        assert info["last_result"]["started"] == 1
        assert info["last_result"]["worker_started"] == 1
        assert info["last_result"]["worker_deferred_count"] == 0
        assert info["last_result"]["run_count"] == 1
        assert info["last_result"]["checked"] >= 1
        assert info["last_result"]["source"] == agent_scheduler.SCHEDULER_RESULT_SOURCE_AUTO
        assert info["last_error"] is None
    finally:
        db.close()


def test_agent_scheduler_worker_capacity_defers_excess(monkeypatch):
    monkeypatch.setattr(agent_scheduler, "_max_workers", lambda: 2)
    with agent_scheduler._lock:
        agent_scheduler._active_worker_run_ids.clear()
        agent_scheduler._active_worker_run_ids.update({"pa_one", "pa_two"})
    try:
        duplicate = agent_scheduler.start_worker("pa_one")
        deferred = agent_scheduler.start_worker("pa_three")

        assert duplicate["started"] is False
        assert duplicate["reason"] == "already_running"
        assert deferred["started"] is False
        assert deferred["reason"] == "worker_capacity"
        assert deferred["active_workers"] == 2
        assert deferred["max_workers"] == 2
    finally:
        with agent_scheduler._lock:
            agent_scheduler._active_worker_run_ids.clear()


def test_plan_only_scheduled_agent_cycle_reports_without_architect_plan(tmp_path, monkeypatch):
    db = _sqlite_autonomy_session()
    try:
        (tmp_path / "README.md").write_text("# Repo\n", encoding="utf-8")
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        agents = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        architect_id = next(agent["id"] for agent in agents if agent["profile_key"] == "architect")
        orchestrator.resume_agent_profile(db, architect_id)
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {
                "available": False,
                "model": None,
                "recommendation": "No local report model available in test.",
            },
        )

        queued = orchestrator.start_agent_cycle(db, architect_id)
        result = orchestrator.run_autonomy_sync(db, queued["run_id"])

        assert result["status"] == orchestrator.RUN_STATUS_COMPLETED
        assert result["plan_status"] == orchestrator.PLAN_STATUS_IMPLEMENTED
        assert result["merge_status"] == "not_applicable"
        assert result["files"] == []
        assert result["architect_review"] == {}
        assert db.query(ProjectAutonomyArchitectReview).filter_by(run_id=queued["run_id"]).count() == 0
        messages = (
            db.query(ProjectAutonomyMessage)
            .filter_by(run_id=queued["run_id"], message_type="agent_cycle_report")
            .all()
        )
        assert len(messages) == 1
        assert "No files were changed" in messages[0].content
        assert "Verdict:" in messages[0].content
        assert "Evidence reviewed:" in messages[0].content
        assert "Checks:" in messages[0].content
        assert "Safety boundary:" in messages[0].content
        assert "Quality guard: passed" in messages[0].content
        quality = (
            db.query(ProjectAutonomyArtifact)
            .filter_by(
                run_id=queued["run_id"],
                name=orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_NAME,
            )
            .one()
        )
        quality_payload = json.loads(quality.content_json)
        assert quality.artifact_type == "quality_gate"
        assert quality_payload["status"] == orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_PASSED
        assert quality_payload["score"] >= orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_PASSING_SCORE
        artifact = (
            db.query(ProjectAutonomyArtifact)
            .filter_by(run_id=queued["run_id"], name="scheduled_agent_report")
            .one()
        )
        assert artifact.artifact_type == "agent_cycle_report"
        report_payload = json.loads(artifact.content_json)
        assert report_payload["report_schema"] == orchestrator.SCHEDULED_AGENT_REPORT_SCHEMA
        assert report_payload["run_id"] == queued["run_id"]
        assert report_payload["status"] == "READ_ONLY_CLEAR"
        assert report_payload["verdict"]
        assert report_payload["evidence_reviewed"]
        assert report_payload["checks_run"]
        assert report_payload["risk_or_blockers"]
        assert report_payload["safety_boundary"]
        assert report_payload["request_binding"]["mode"] == "plan_only"
    finally:
        db.close()


def test_plan_only_scheduled_agent_quality_guard_repairs_false_action_claims(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        agents = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        architect_id = next(agent["id"] for agent in agents if agent["profile_key"] == "architect")
        orchestrator.resume_agent_profile(db, architect_id)
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {"available": True, "model": "local-report-model"},
        )

        def fake_chat(messages, model, **kwargs):
            return SimpleNamespace(
                ok=True,
                text=json.dumps(
                    {
                        "summary": "I implemented the fix and committed it.",
                        "findings": ["Changed files and ran tests."],
                        "recommended_next_steps": ["Merge it now."],
                        "operator_question": "",
                    }
                ),
                model=model,
                latency_ms=12,
                error=None,
            )

        monkeypatch.setattr(orchestrator.ollama_client, "chat", fake_chat)

        queued = orchestrator.start_agent_cycle(db, architect_id)
        result = orchestrator.run_autonomy_sync(db, queued["run_id"])

        assert result["status"] == orchestrator.RUN_STATUS_COMPLETED
        message = (
            db.query(ProjectAutonomyMessage)
            .filter_by(run_id=queued["run_id"], message_type="agent_cycle_report")
            .one()
        )
        assert "implemented the fix" not in message.content
        assert "committed it" not in message.content
        assert "Quality guard: repaired" in message.content
        quality = (
            db.query(ProjectAutonomyArtifact)
            .filter_by(
                run_id=queued["run_id"],
                name=orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_NAME,
            )
            .one()
        )
        payload = json.loads(quality.content_json)
        assert payload["status"] == orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_REPAIRED
        assert payload["initial_score"] < orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_PASSING_SCORE
        assert "false_action_claim" in payload["initial_issues"]
        report = (
            db.query(ProjectAutonomyArtifact)
            .filter_by(run_id=queued["run_id"], name=orchestrator.SCHEDULED_AGENT_REPORT_ARTIFACT_NAME)
            .one()
        )
        report_payload = json.loads(report.content_json)
        assert report_payload["quality"]["status"] == orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_REPAIRED
    finally:
        db.close()


def test_plan_only_scheduled_agent_quality_guard_repairs_missing_receipts(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        agents = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        architect_id = next(agent["id"] for agent in agents if agent["profile_key"] == "architect")
        orchestrator.resume_agent_profile(db, architect_id)
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {"available": True, "model": "local-report-model"},
        )

        def fake_chat(messages, model, **kwargs):
            return SimpleNamespace(
                ok=True,
                text=json.dumps(
                    {
                        "summary": "I observed the scheduled repo context without taking actions.",
                        "findings": [
                            "No files were changed.",
                            "Plan-only observation remained read-only.",
                        ],
                        "recommended_next_steps": ["Keep the agent in observe mode."],
                        "operator_question": "",
                    }
                ),
                model=model,
                latency_ms=12,
                error=None,
            )

        monkeypatch.setattr(orchestrator.ollama_client, "chat", fake_chat)

        queued = orchestrator.start_agent_cycle(db, architect_id)
        result = orchestrator.run_autonomy_sync(db, queued["run_id"])

        assert result["status"] == orchestrator.RUN_STATUS_COMPLETED
        quality = (
            db.query(ProjectAutonomyArtifact)
            .filter_by(
                run_id=queued["run_id"],
                name=orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_NAME,
            )
            .one()
        )
        payload = json.loads(quality.content_json)
        assert payload["status"] == orchestrator.SCHEDULED_AGENT_REPORT_QUALITY_REPAIRED
        assert "missing_status_or_verdict" in payload["initial_issues"]
        assert "missing_evidence_reviewed" in payload["initial_issues"]
        assert "missing_checks_run" in payload["initial_issues"]
        assert "missing_safety_boundary" in payload["initial_issues"]
        report = (
            db.query(ProjectAutonomyArtifact)
            .filter_by(run_id=queued["run_id"], name=orchestrator.SCHEDULED_AGENT_REPORT_ARTIFACT_NAME)
            .one()
        )
        report_payload = json.loads(report.content_json)
        assert report_payload["status"] == "READ_ONLY_REPAIRED"
        assert report_payload["evidence_reviewed"]
        assert report_payload["checks_run"]
        assert report_payload["safety_boundary"]
    finally:
        db.close()


def test_plan_only_scheduled_agent_cycle_preserves_prompt_and_waits_on_question(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        agents = orchestrator.bootstrap_agent_profiles(db, repo_id=repo.id)
        architect_id = next(agent["id"] for agent in agents if agent["profile_key"] == "architect")
        profile = db.get(ProjectAutonomyAgentProfile, architect_id)
        profile.prompt_setting_json = json.dumps(
            {
                "source": "codex_automation",
                "system_prompt": "Always review the Codex automation prompt before reporting.",
            }
        )
        profile.status = orchestrator.AGENT_PROFILE_STATUS_ACTIVE
        db.commit()
        captured: dict[str, list[dict]] = {}
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: {"available": True, "model": "local-report-model"},
        )

        def fake_chat(messages, model, **kwargs):
            captured["messages"] = messages
            return SimpleNamespace(
                ok=True,
                text=json.dumps(
                    {
                        "summary": "I need one operator decision before the next cycle.",
                        "findings": ["No files were changed."],
                        "recommended_next_steps": ["Choose the first focus area."],
                        "operator_question": "Which subsystem should this scheduled agent inspect first?",
                    }
                ),
                model=model,
                latency_ms=12,
                error=None,
            )

        monkeypatch.setattr(orchestrator.ollama_client, "chat", fake_chat)

        queued = orchestrator.start_agent_cycle(db, architect_id)
        result = orchestrator.run_autonomy_sync(db, queued["run_id"])

        assert "Always review the Codex automation prompt" in captured["messages"][1]["content"]
        assert result["status"] == orchestrator.RUN_STATUS_AWAITING_CLARIFICATION
        assert result["plan_status"] == orchestrator.PLAN_STATUS_AWAITING_CLARIFICATION
        assert result["operator_questions"][0]["status"] == "pending"
        assert "Which subsystem" in result["operator_questions"][0]["question"]
        assert result["merge_status"] == "not_applicable"
    finally:
        db.close()


def test_due_scheduler_skips_current_workspace_alias_profiles(tmp_path, monkeypatch):
    db = _sqlite_autonomy_session()
    try:
        root = tmp_path.resolve()

        def fake_resolve(repo: CodeRepo):
            if repo.path in {str(root), "/app", "/workspace"}:
                return root
            return Path(repo.path).resolve()

        monkeypatch.setattr(code_indexer, "_current_workspace_root", lambda: root)
        monkeypatch.setattr(code_indexer, "resolve_repo_runtime_path", fake_resolve)
        preferred = CodeRepo(
            path=str(root),
            host_path=str(root),
            container_path="/workspace",
            name=code_indexer.CURRENT_WORKSPACE_REPO_NAME,
            active=True,
        )
        alias = CodeRepo(
            path="/app",
            container_path="/workspace",
            name=code_indexer.CURRENT_WORKSPACE_REPO_NAME,
            active=True,
        )
        db.add_all([preferred, alias])
        db.commit()
        profile = ProjectAutonomyAgentProfile(
            repo_id=alias.id,
            profile_key="architect",
            name="Architect",
            role="architect",
            tier="macro",
            status="active",
            model_policy="local_first",
            prompt_setting_json="{}",
            permissions_json=json.dumps(orchestrator.DEFAULT_AGENT_PERMISSIONS),
            schedule_enabled=True,
            schedule_json=json.dumps(
                {
                    "cadence": "two_minutes",
                    "rrule": "FREQ=MINUTELY;INTERVAL=2",
                    "budget": {"max_minutes": 20, "max_child_runs": 0},
                }
            ),
        )
        db.add(profile)
        db.flush()
        db.add(
            ProjectAutonomyAgentSchedule(
                profile_id=profile.id,
                status="active",
                rrule="FREQ=MINUTELY;INTERVAL=2",
                budget_json=json.dumps({"max_minutes": 20, "max_child_runs": 0}),
                next_run_at=datetime.utcnow() - timedelta(minutes=1),
            )
        )
        db.commit()

        result = orchestrator.run_due_agent_cycles(db, limit=1)

        assert result["started"] == 0
        assert result["skipped"][0]["reason"] == orchestrator.AGENT_SCHEDULE_SKIP_REPO_ALIAS
        assert result["skipped"][0]["repo_id"] == alias.id
    finally:
        db.close()


def test_agent_profiles_for_current_workspace_alias_canonicalize_to_preferred_repo(
    tmp_path,
    monkeypatch,
):
    db = _sqlite_autonomy_session()
    try:
        root = tmp_path.resolve()

        def fake_resolve(repo: CodeRepo):
            if repo.path in {str(root), "/app", "/workspace"}:
                return root
            return Path(repo.path).resolve()

        monkeypatch.setattr(code_indexer, "_current_workspace_root", lambda: root)
        monkeypatch.setattr(code_indexer, "resolve_repo_runtime_path", fake_resolve)
        preferred = CodeRepo(
            path=str(root),
            host_path=str(root),
            container_path="/workspace",
            name=code_indexer.CURRENT_WORKSPACE_REPO_NAME,
            active=True,
        )
        alias = CodeRepo(
            path="/app",
            container_path="/workspace",
            name=code_indexer.CURRENT_WORKSPACE_REPO_NAME,
            active=True,
        )
        db.add_all([preferred, alias])
        db.flush()
        alias_profile = ProjectAutonomyAgentProfile(
            repo_id=alias.id,
            profile_key="architect",
            name="Alias Architect",
            role="architect",
            tier="macro",
            status="active",
            model_policy="local_first",
            prompt_setting_json="{}",
            permissions_json=json.dumps(orchestrator.DEFAULT_AGENT_PERMISSIONS),
            schedule_enabled=True,
            schedule_json=json.dumps(
                {
                    "cadence": "two_minutes",
                    "rrule": "FREQ=MINUTELY;INTERVAL=2",
                    "budget": {"max_minutes": 20, "max_child_runs": 0},
                }
            ),
        )
        db.add(alias_profile)
        db.flush()
        db.add(
            ProjectAutonomyAgentSchedule(
                profile_id=alias_profile.id,
                status="active",
                rrule="FREQ=MINUTELY;INTERVAL=2",
                budget_json=json.dumps({"max_minutes": 20, "max_child_runs": 0}),
                next_run_at=datetime.utcnow() - timedelta(minutes=1),
            )
        )
        db.commit()

        agents = orchestrator.list_agent_profiles(db, repo_id=alias.id)

        assert agents
        assert {agent["repo_id"] for agent in agents} == {preferred.id}
        refreshed_alias = db.get(ProjectAutonomyAgentProfile, alias_profile.id)
        assert refreshed_alias.status == "paused"
        assert refreshed_alias.schedule_enabled is False
        alias_schedule = (
            db.query(ProjectAutonomyAgentSchedule)
            .filter(ProjectAutonomyAgentSchedule.profile_id == alias_profile.id)
            .one()
        )
        assert alias_schedule.status == "paused"
        assert alias_schedule.next_run_at is None
    finally:
        db.close()


def test_operator_questions_surface_on_run_payload(tmp_path):
    db = _sqlite_autonomy_session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="repo", active=True)
        db.add(repo)
        db.commit()
        run = orchestrator.create_run(db, prompt="needs input", repo_id=repo.id)
        run.status = orchestrator.RUN_STATUS_AWAITING_CLARIFICATION
        run.plan_status = orchestrator.PLAN_STATUS_AWAITING_CLARIFICATION
        db.commit()

        orchestrator.record_operator_question(
            db,
            run,
            "Which UI workflow should this agent optimize first?",
            context={"source": "test"},
            commit=True,
        )
        payload = orchestrator.run_payload(db, run, include_events=True)

        assert payload["operator_questions"][0]["status"] == "pending"
        assert "workflow" in payload["operator_questions"][0]["question"]
        profile = next(
            agent
            for agent in orchestrator.list_agent_profiles(db, repo_id=repo.id)
            if agent["id"] == run.agent_profile_id
        )
        assert profile["pending_question_count"] == 1

        answered = orchestrator.append_user_message(
            db,
            run.run_id,
            content="Focus the Autopilot composer workflow first.",
        )

        assert answered["operator_questions"][0]["status"] == "answered"
        assert answered["operator_questions"][0]["answer"] == "Focus the Autopilot composer workflow first."
        assert answered["status"] == orchestrator.RUN_STATUS_QUEUED
        updated_profile = next(
            agent
            for agent in orchestrator.list_agent_profiles(db, repo_id=repo.id)
            if agent["id"] == run.agent_profile_id
        )
        assert updated_profile["pending_question_count"] == 0
    finally:
        db.close()
