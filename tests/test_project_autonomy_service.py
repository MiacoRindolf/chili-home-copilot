from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
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
        assert "Attached images:" in orchestrator._conversation_prompt(db, run)
        artifacts = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.run_id == run.run_id, ProjectAutonomyArtifact.artifact_type == "prompt_image")
            .all()
        )
        assert len(artifacts) == 2
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
