from __future__ import annotations

from pathlib import Path

from app.models import ProjectAutonomyRun, ProjectDomainRun
from app.models.code_brain import CodeRepo
from app.services.project_autonomy import orchestrator


def test_project_autonomy_routes_are_registered(fastapi_app):
    expected = {
        ("POST", "/api/brain/project/autonomy/runs"),
        ("GET", "/api/brain/project/autonomy/runs"),
        ("POST", "/api/brain/project/autonomy/runs/archive"),
        ("GET", "/api/brain/project/autonomy/runs/{run_id}"),
        ("GET", "/api/brain/project/autonomy/runs/{run_id}/events"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/messages"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/plan/start"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/plan/approve"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/visual-validation"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/cancel"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/merge"),
        ("GET", "/api/brain/project/agent-profiles"),
        ("POST", "/api/brain/project/agent-profiles/bootstrap"),
        ("POST", "/api/brain/project/agent-profiles/codex-sync"),
        ("GET", "/api/brain/project/agent-profiles/scheduler"),
        ("POST", "/api/brain/project/agent-profiles/scheduler/run-now"),
        ("POST", "/api/brain/project/agent-profiles/codex-schedules"),
        ("POST", "/api/brain/project/agent-profiles/codex-adopt"),
        ("PATCH", "/api/brain/project/agent-profiles/{profile_id}"),
        ("POST", "/api/brain/project/agent-profiles/{profile_id}/cycle"),
        ("POST", "/api/brain/project/agent-profiles/{profile_id}/pause"),
        ("POST", "/api/brain/project/agent-profiles/{profile_id}/resume"),
    }
    registered = set()
    for route in fastapi_app.routes:
        path = getattr(route, "path", None)
        for method in getattr(route, "methods", set()) or set():
            if (method, path) in expected:
                registered.add((method, path))

    assert registered == expected


def test_project_autonomy_create_list_detail_cancel_and_merge(
    paired_client,
    db,
    monkeypatch,
):
    client, user = paired_client
    repo_root = Path(__file__).resolve().parents[1]
    repo = CodeRepo(
        user_id=user.id,
        path=str(repo_root),
        host_path=str(repo_root),
        name="workspace",
        active=True,
    )
    db.add(repo)
    db.commit()

    import app.routers.brain_project as brain_project_router

    started_threads = []
    monkeypatch.setattr(brain_project_router, "_start_autonomy_thread", started_threads.append)

    bootstrapped = client.post(
        "/api/brain/project/agent-profiles/bootstrap",
        json={"repo_id": repo.id},
    )
    assert bootstrapped.status_code == 200
    agents = bootstrapped.json()["agents"]
    architect = next(agent for agent in agents if agent["profile_key"] == "architect")
    assert architect["status"] == "paused"
    assert architect["permissions"]["worktree"] is False

    created = client.post(
        "/api/brain/project/autonomy/runs",
        json={
            "prompt": "Add a tiny project autopilot test hook",
            "repo_id": repo.id,
            "agent_profile_id": architect["id"],
        },
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["ok"] is True
    run_id = payload["run"]["run_id"]
    assert payload["run"]["status"] == "chatting"
    assert payload["run"]["execution_mode"] == "plan_approval"
    assert payload["run"]["plan_status"] == "chatting"
    assert payload["run"]["repo_id"] == repo.id
    assert payload["run"]["agent_profile"]["profile_key"] == "architect"
    assert payload["run"]["agent_snapshot"]["permissions"]["merge"] is False
    assert payload["run"]["messages"][0]["role"] == "user"
    assert payload["run"]["messages"][0]["content"] == "Add a tiny project autopilot test hook"
    assert payload["run"]["messages"][1]["role"] == "assistant"
    assert started_threads == []

    with_image = client.post(
        f"/api/brain/project/autonomy/runs/{run_id}/messages",
        json={
            "content": "Use this image when we add UI evidence support.",
            "attachments": [
                {
                    "kind": "image",
                    "path": str(repo_root / "tests" / "fixtures" / "autopilot.png"),
                    "name": "autopilot.png",
                    "mime_type": "image/png",
                }
            ],
        },
    )
    assert with_image.status_code == 200
    last_user = [
        m for m in with_image.json()["run"]["messages"] if m["role"] == "user"
    ][-1]
    assert last_user["metadata"]["attachments"][0]["name"] == "autopilot.png"

    domain_run = db.query(ProjectDomainRun).filter(ProjectDomainRun.run_kind == "autonomous").first()
    assert domain_run is not None
    assert domain_run.repo_id == repo.id

    listed = client.get("/api/brain/project/autonomy/runs")
    assert listed.status_code == 200
    assert listed.json()["runs"][0]["run_id"] == run_id

    listed_by_agent = client.get(
        f"/api/brain/project/autonomy/runs?agent_profile_id={architect['id']}"
    )
    assert listed_by_agent.status_code == 200
    assert listed_by_agent.json()["runs"][0]["run_id"] == run_id

    detail = client.get(f"/api/brain/project/autonomy/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run"]["steps"][0]["stage"] == "chat"
    assert detail.json()["run"]["messages"][0]["message_type"] == "prompt"

    visual = client.post(
        f"/api/brain/project/autonomy/runs/{run_id}/visual-validation",
        json={"kind": "video"},
    )
    assert visual.status_code == 200
    artifacts = visual.json()["run"]["artifacts"]
    assert any(a["artifact_type"] == "visual_video" for a in artifacts)
    assert any(a["artifact_type"] == "ux_review" for a in artifacts)

    message = client.post(
        f"/api/brain/project/autonomy/runs/{run_id}/messages",
        json={"content": "Please keep the change narrow."},
    )
    assert message.status_code == 200
    assert any(m["content"] == "Please keep the change narrow." for m in message.json()["run"]["messages"])

    started = client.post(f"/api/brain/project/autonomy/runs/{run_id}/plan/start")
    assert started.status_code == 200
    assert started.json()["run"]["status"] == "queued"
    assert started.json()["run"]["plan_status"] == "drafting"
    assert started_threads == [run_id]

    db_run = db.query(ProjectAutonomyRun).filter(ProjectAutonomyRun.run_id == run_id).one()
    db_run.status = "awaiting_approval"
    db_run.current_stage = "plan"
    db_run.plan_status = "awaiting_approval"
    db_run.plan_json = (
        '{"analysis":"Change app example safely.",'
        '"files":[{"path":"app/example.py","action":"modify","description":"Change app example safely."}],'
        '"notes":""}'
    )
    orchestrator._record_architect_review(
        db,
        db_run,
        {
            "attempt_index": 1,
            "status": "passed",
            "score": 90,
            "confidence": "high",
            "dimensions": {},
            "alternatives": [],
            "critique": {"blockers": [], "next_action": "approval_ready"},
            "selected_files": [{"path": "app/example.py", "rationale": "route test"}],
            "blocking_reason": None,
        },
    )
    db.commit()
    approved = client.post(f"/api/brain/project/autonomy/runs/{run_id}/plan/approve")
    assert approved.status_code == 200
    assert approved.json()["run"]["plan_status"] == "approved"

    cancelled = client.post(f"/api/brain/project/autonomy/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["run"]["cancel_requested"] is True

    merged = client.post(f"/api/brain/project/autonomy/runs/{run_id}/merge")
    assert merged.status_code == 200
    assert merged.json()["run"]["merge_status"] == "blocked"

    archived = client.post(
        "/api/brain/project/autonomy/runs/archive",
        json={"repo_id": repo.id},
    )
    assert archived.status_code == 200
    assert archived.json()["archived"] >= 1
    hidden = client.get(f"/api/brain/project/autonomy/runs?repo_id={repo.id}")
    assert hidden.status_code == 200
    assert all(item["run_id"] != run_id for item in hidden.json()["runs"])
    visible_archived = client.get(
        f"/api/brain/project/autonomy/runs?repo_id={repo.id}&include_archived=true"
    )
    assert visible_archived.status_code == 200
    assert any(item["run_id"] == run_id and item["archived"] is True for item in visible_archived.json()["runs"])


def test_project_autonomy_list_requires_paired_client(client):
    response = client.get("/api/brain/project/autonomy/runs")

    assert response.status_code == 403
    assert response.json()["detail"]["ok"] is False


def test_project_autonomy_agent_profile_cycle_requires_resume(
    paired_client,
    db,
    monkeypatch,
    tmp_path,
):
    client, user = paired_client
    repo = CodeRepo(
        user_id=user.id,
        path=str(tmp_path),
        host_path=str(tmp_path),
        name="workspace",
        active=True,
    )
    db.add(repo)
    db.commit()

    import app.routers.brain_project as brain_project_router

    started_threads = []
    monkeypatch.setattr(brain_project_router, "_start_autonomy_thread", started_threads.append)

    agents_response = client.get(f"/api/brain/project/agent-profiles?repo_id={repo.id}")
    assert agents_response.status_code == 200
    architect = next(
        agent
        for agent in agents_response.json()["agents"]
        if agent["profile_key"] == "architect"
    )

    blocked = client.post(f"/api/brain/project/agent-profiles/{architect['id']}/cycle")
    assert blocked.status_code == 409
    assert "paused" in blocked.json()["message"]

    resumed = client.post(f"/api/brain/project/agent-profiles/{architect['id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["agent"]["status"] == "active"

    cycle = client.post(f"/api/brain/project/agent-profiles/{architect['id']}/cycle")
    assert cycle.status_code == 200
    assert cycle.json()["run"]["agent_profile_id"] == architect["id"]
    assert cycle.json()["run"]["status"] == "queued"
    assert started_threads == [cycle.json()["run"]["run_id"]]


def test_agent_profile_update_route_persists_schedule_and_permissions(
    paired_client,
    db,
    tmp_path,
):
    client, user = paired_client
    repo = CodeRepo(
        user_id=user.id,
        path=str(tmp_path),
        host_path=str(tmp_path),
        name="workspace",
        active=True,
    )
    db.add(repo)
    db.commit()

    bootstrapped = client.post(
        "/api/brain/project/agent-profiles/bootstrap",
        json={"repo_id": repo.id},
    )
    assert bootstrapped.status_code == 200
    architect = next(
        agent
        for agent in bootstrapped.json()["agents"]
        if agent["profile_key"] == "architect"
    )

    updated = client.patch(
        f"/api/brain/project/agent-profiles/{architect['id']}",
        json={
            "permissions": {orchestrator.AGENT_PERMISSION_WORKTREE: True},
            "schedule_enabled": True,
            "schedule": {
                "cadence": "ten_minutes",
                "rrule": "FREQ=MINUTELY;INTERVAL=10",
                "budget": {"max_minutes": 20, "max_child_runs": 0},
            },
        },
    )

    assert updated.status_code == 200
    payload = updated.json()["agent"]
    assert payload["permissions"]["worktree"] is True
    assert payload["permissions"]["merge"] is False
    assert payload["schedule_enabled"] is True
    assert payload["schedule"]["rrule"] == "FREQ=MINUTELY;INTERVAL=10"
    assert payload["schedule"]["budget"]["max_minutes"] == 20


def test_agent_profile_scheduler_route_reports_runtime_state(paired_client):
    client, _user = paired_client

    response = client.get("/api/brain/project/agent-profiles/scheduler")

    assert response.status_code == 200
    scheduler = response.json()["scheduler"]
    assert scheduler["enabled"] is True
    assert scheduler["mode"] in {"standalone", "apscheduler"}
    assert scheduler["interval_seconds"] >= 30
    assert "running" in scheduler
    assert scheduler["active_workers"] >= 0
    assert scheduler["max_workers"] >= 1


def test_agent_profile_codex_schedule_route_toggles_imported_codex(
    paired_client,
    db,
    monkeypatch,
    tmp_path,
):
    client, user = paired_client
    import app.routers.brain_project as brain_project_router

    started_threads = []
    monkeypatch.setattr(brain_project_router, "_start_autonomy_thread", started_threads.append)
    repo_path = tmp_path / "workspace"
    repo_path.mkdir()
    codex_home = tmp_path / "codex_home"
    active_dir = codex_home / "automations" / "agentops-director"
    paused_dir = codex_home / "automations" / "performance-research"
    active_dir.mkdir(parents=True)
    paused_dir.mkdir(parents=True)
    prompt_repo_path = str(repo_path.resolve()).replace("\\", "/")
    active_dir.joinpath("automation.toml").write_text(
        "\n".join(
            [
                'id = "agentops-director"',
                'name = "AgentOps Director"',
                'kind = "heartbeat"',
                'status = "ACTIVE"',
                'rrule = "RRULE:FREQ=MINUTELY;INTERVAL=5"',
                'prompt = """',
                f"Workspace: {prompt_repo_path}",
                "Monitor local agent flow.",
                '"""',
            ]
        ),
        encoding="utf-8",
    )
    paused_dir.joinpath("automation.toml").write_text(
        "\n".join(
            [
                'id = "performance-research"',
                'name = "Performance Research"',
                'kind = "cron"',
                'status = "PAUSED"',
                'rrule = "FREQ=HOURLY;INTERVAL=6"',
                'prompt = "Research this repository for low-risk performance work."',
                f'cwds = ["{prompt_repo_path}"]',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [codex_home])
    repo = CodeRepo(
        user_id=user.id,
        path=str(repo_path),
        host_path=str(repo_path),
        name="workspace",
        active=True,
    )
    db.add(repo)
    db.commit()

    bootstrapped = client.post(
        "/api/brain/project/agent-profiles/bootstrap",
        json={"repo_id": repo.id},
    )
    assert bootstrapped.status_code == 200

    active_dir.joinpath("automation.toml").write_text(
        "\n".join(
            [
                'id = "agentops-director"',
                'name = "AgentOps Director"',
                'kind = "heartbeat"',
                'status = "ACTIVE"',
                'rrule = "RRULE:FREQ=MINUTELY;INTERVAL=5"',
                'prompt = """',
                f"Workspace: {prompt_repo_path}",
                "Monitor local agent flow and Codex prompt drift.",
                '"""',
            ]
        ),
        encoding="utf-8",
    )
    synced = client.post(
        "/api/brain/project/agent-profiles/codex-sync",
        json={"repo_id": repo.id},
    )
    assert synced.status_code == 200
    sync_payload = synced.json()
    assert sync_payload["refreshed_count"] == 1
    assert sync_payload["stale_count"] == 0
    synced_active = next(
        agent
        for agent in sync_payload["agents"]
        if agent["profile_key"] == "codex_agentops_director"
    )
    assert "Codex prompt drift" in synced_active["prompt_setting"]["system_prompt"]

    enabled = client.post(
        "/api/brain/project/agent-profiles/codex-schedules",
        json={"repo_id": repo.id, "enable_source_active": True},
    )
    assert enabled.status_code == 200
    enabled_payload = enabled.json()
    assert enabled_payload["changed"] == 1
    assert enabled_payload["schedule_mirror"]["source_active_enabled"] == 1
    assert "Enabled 1 source-active Codex schedules" in enabled_payload["message"]
    agents = enabled_payload["agents"]
    active = next(
        agent for agent in agents if agent["profile_key"] == "codex_agentops_director"
    )
    paused = next(
        agent for agent in agents if agent["profile_key"] == "codex_performance_research"
    )
    assert active["status"] == orchestrator.AGENT_PROFILE_STATUS_ACTIVE
    assert active["schedule_enabled"] is True
    assert active["schedule"]["rrule"] == "FREQ=MINUTELY;INTERVAL=5"
    assert active["prompt_setting"]["codex_automation"]["rrule"] == "RRULE:FREQ=MINUTELY;INTERVAL=5"
    assert active["prompt_setting"]["codex_automation"]["normalized_rrule"] == "FREQ=MINUTELY;INTERVAL=5"
    assert active["permissions"][orchestrator.AGENT_PERMISSION_WORKTREE] is False
    assert active["permissions"][orchestrator.AGENT_PERMISSION_MERGE] is False
    assert paused["status"] == orchestrator.AGENT_PROFILE_STATUS_PAUSED
    assert paused["schedule_enabled"] is False

    always_on = client.post(
        "/api/brain/project/agent-profiles/codex-schedules",
        json={"repo_id": repo.id, "enable_source_active": True, "always_on": True},
    )
    assert always_on.status_code == 200
    always_on_payload = always_on.json()
    assert always_on_payload["always_on"] is True
    assert always_on_payload["schedule_mirror"]["source_active_always_on"] == 1
    assert always_on_payload["schedule_mirror"]["source_active_scheduled"] == 0
    active = next(
        agent
        for agent in always_on_payload["agents"]
        if agent["profile_key"] == "codex_agentops_director"
    )
    assert active["schedule"]["runtime_mode"] == orchestrator.AGENT_RUNTIME_MODE_ALWAYS_ON
    assert active["schedule"]["rrule"] is None
    assert active["schedule"]["source_rrule"] == "FREQ=MINUTELY;INTERVAL=5"

    run_now = client.post(
        "/api/brain/project/agent-profiles/scheduler/run-now",
        json={"repo_id": repo.id, "codex_only": True, "limit": 2},
    )
    assert run_now.status_code == 200
    run_now_payload = run_now.json()
    assert run_now_payload["woken"] == 1
    assert run_now_payload["started"] == 1
    assert run_now_payload["worker_started"] == 1
    assert run_now_payload["worker_deferred"] == []
    assert run_now_payload["runs"][0]["agent_profile_id"] == active["id"]
    assert run_now_payload["runs"][0]["autonomy_level"] == orchestrator.AUTONOMY_LEVEL_SCHEDULED_AGENT
    assert started_threads == [run_now_payload["runs"][0]["run_id"]]
    scheduler_after_wake = client.get("/api/brain/project/agent-profiles/scheduler")
    assert scheduler_after_wake.status_code == 200
    last_result = scheduler_after_wake.json()["scheduler"]["last_result"]
    assert last_result["started"] == 1
    assert last_result["worker_started"] == 1
    assert last_result["worker_deferred_count"] == 0
    assert last_result["run_count"] == 1
    assert last_result["source"] == "manual_wake"

    paused_response = client.post(
        "/api/brain/project/agent-profiles/codex-schedules",
        json={"repo_id": repo.id, "enable_source_active": False},
    )
    assert paused_response.status_code == 200
    paused_payload = paused_response.json()
    assert paused_payload["changed"] == 2
    assert paused_payload["schedule_mirror"]["source_active_enabled"] == 0
    assert all(
        agent["schedule_enabled"] is False
        for agent in paused_payload["agents"]
        if (agent.get("prompt_setting") or {}).get("source")
        == orchestrator.CODEX_AUTOMATION_SOURCE
    )


def test_agent_profile_codex_adopt_route_syncs_enables_and_wakes(
    paired_client,
    db,
    monkeypatch,
    tmp_path,
):
    client, user = paired_client
    import app.routers.brain_project as brain_project_router

    started_threads = []
    monkeypatch.setattr(brain_project_router, "_start_autonomy_thread", started_threads.append)
    repo_path = tmp_path / "workspace"
    repo_path.mkdir()
    codex_home = tmp_path / "codex_home"
    active_dir = codex_home / "automations" / "qa-verification-engineer"
    active_dir.mkdir(parents=True)
    prompt_repo_path = str(repo_path.resolve()).replace("\\", "/")
    active_dir.joinpath("automation.toml").write_text(
        "\n".join(
            [
                'id = "qa-verification-engineer"',
                'name = "QA Verification Engineer"',
                'kind = "heartbeat"',
                'status = "ACTIVE"',
                'rrule = "RRULE:FREQ=MINUTELY;INTERVAL=5"',
                'prompt = """',
                f"Workspace: {prompt_repo_path}",
                "Continuously inspect local QA risks and report blockers.",
                '"""',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_codex_automation_roots", lambda: [codex_home])
    repo = CodeRepo(
        user_id=user.id,
        path=str(repo_path),
        host_path=str(repo_path),
        name="workspace",
        active=True,
    )
    db.add(repo)
    db.commit()

    adopted = client.post(
        "/api/brain/project/agent-profiles/codex-adopt",
        json={"repo_id": repo.id, "wake_now": True, "limit": 2},
    )

    assert adopted.status_code == 200
    payload = adopted.json()
    assert payload["sync"]["current_count"] == 1
    assert payload["schedule"]["schedule_mirror"]["source_active_always_on"] == 1
    assert payload["wake"]["started"] == 1
    assert payload["wake"]["worker_started"] == 1
    assert payload["wake"]["worker_deferred"] == []
    assert payload["wake"]["runs"][0]["autonomy_level"] == orchestrator.AUTONOMY_LEVEL_SCHEDULED_AGENT
    assert started_threads == [payload["wake"]["runs"][0]["run_id"]]
    readiness = payload["readiness"]
    assert readiness["codex_automations"]["schedule_mirror"]["source_active_always_on"] == 1
    scheduler_after_adopt = client.get("/api/brain/project/agent-profiles/scheduler")
    assert scheduler_after_adopt.status_code == 200
    assert scheduler_after_adopt.json()["scheduler"]["last_result"]["source"] == "manual_wake"


def test_scheduled_plan_only_agent_approval_route_is_blocked(
    paired_client,
    db,
    monkeypatch,
    tmp_path,
):
    client, user = paired_client
    repo = CodeRepo(
        user_id=user.id,
        path=str(tmp_path),
        host_path=str(tmp_path),
        name="workspace",
        active=True,
    )
    db.add(repo)
    db.commit()

    import app.routers.brain_project as brain_project_router

    started_threads = []
    monkeypatch.setattr(brain_project_router, "_start_autonomy_thread", started_threads.append)

    bootstrapped = client.post(
        "/api/brain/project/agent-profiles/bootstrap",
        json={"repo_id": repo.id},
    )
    assert bootstrapped.status_code == 200
    architect = next(
        agent
        for agent in bootstrapped.json()["agents"]
        if agent["profile_key"] == "architect"
    )
    assert architect["permissions"]["worktree"] is False

    created = client.post(
        "/api/brain/project/autonomy/runs",
        json={
            "prompt": "Find a small safe improvement.",
            "repo_id": repo.id,
            "agent_profile_id": architect["id"],
            "autonomy_level": orchestrator.AUTONOMY_LEVEL_SCHEDULED_AGENT,
        },
    )
    assert created.status_code == 200
    run_id = created.json()["run"]["run_id"]
    assert started_threads == []

    db_run = db.query(ProjectAutonomyRun).filter(ProjectAutonomyRun.run_id == run_id).one()
    db_run.status = "awaiting_approval"
    db_run.current_stage = "plan"
    db_run.plan_status = "awaiting_approval"
    db_run.plan_json = (
        '{"analysis":"Add a narrow route-level test.",'
        '"files":[{"path":"tests/test_brain_project_autonomy_routes.py",'
        '"action":"modify","description":"Cover scheduled plan-only approval blocking."}],'
        '"notes":"Keep this approval-first."}'
    )
    orchestrator._record_architect_review(
        db,
        db_run,
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
                    "path": "tests/test_brain_project_autonomy_routes.py",
                    "rationale": "route-level regression for the approval API",
                }
            ],
            "blocking_reason": None,
        },
    )
    db.commit()

    approved = client.post(f"/api/brain/project/autonomy/runs/{run_id}/plan/approve")

    assert approved.status_code == 409
    assert approved.json()["ok"] is False
    assert "permission gate" in approved.json()["message"]
    assert started_threads == []


def test_autonomy_sse_state_signature_tracks_review_freshness():
    import app.routers.brain_project as brain_project_router

    fresh = {
        "run_id": "pa_state",
        "status": "running",
        "current_stage": "plan",
        "plan_status": "drafting",
        "merge_status": "pending",
        "updated_at": "2026-05-28T16:00:00",
        "architect_review": {"status": "passed", "score": 92, "stale": False},
        "messages": [{"id": 1, "content": "history does not define freshness"}],
    }
    stale = {
        **fresh,
        "architect_review": {"status": "passed", "score": 92, "stale": True},
    }
    newer_history_only = {
        **fresh,
        "messages": [{"id": 2, "content": "new historical event"}],
    }

    assert (
        brain_project_router._autonomy_sse_state_signature(fresh)
        != brain_project_router._autonomy_sse_state_signature(stale)
    )
    assert (
        brain_project_router._autonomy_sse_state_signature(fresh)
        == brain_project_router._autonomy_sse_state_signature(newer_history_only)
    )


def test_autonomy_sse_stays_open_for_idle_non_terminal_states():
    import app.routers.brain_project as brain_project_router

    for status in ("chatting", "awaiting_approval", "awaiting_clarification"):
        assert (
            brain_project_router._autonomy_sse_should_complete({"status": status})
            is False
        )

    assert (
        brain_project_router._autonomy_sse_should_complete({"status": "merged"})
        is True
    )
    assert brain_project_router._autonomy_sse_should_complete(None) is True
    assert brain_project_router._autonomy_sse_keepalive() == ": keep-alive\n\n"
