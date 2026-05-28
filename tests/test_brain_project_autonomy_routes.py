from __future__ import annotations

from pathlib import Path

from app.models import ProjectAutonomyArchitectReview, ProjectAutonomyRun, ProjectDomainRun
from app.models.code_brain import CodeRepo


def test_project_autonomy_routes_are_registered(fastapi_app):
    expected = {
        ("POST", "/api/brain/project/autonomy/runs"),
        ("GET", "/api/brain/project/autonomy/runs"),
        ("GET", "/api/brain/project/autonomy/runs/{run_id}"),
        ("GET", "/api/brain/project/autonomy/runs/{run_id}/events"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/messages"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/plan/start"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/plan/approve"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/visual-validation"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/cancel"),
        ("POST", "/api/brain/project/autonomy/runs/{run_id}/merge"),
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

    created = client.post(
        "/api/brain/project/autonomy/runs",
        json={"prompt": "Add a tiny project autopilot test hook", "repo_id": repo.id},
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["ok"] is True
    run_id = payload["run"]["run_id"]
    assert payload["run"]["status"] == "chatting"
    assert payload["run"]["execution_mode"] == "plan_approval"
    assert payload["run"]["plan_status"] == "chatting"
    assert payload["run"]["repo_id"] == repo.id
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
    db.add(
        ProjectAutonomyArchitectReview(
            run_id=run_id,
            attempt_index=1,
            status="passed",
            score=90,
            confidence="high",
            dimensions_json="{}",
            alternatives_json="[]",
            critique_json='{"blockers":[]}',
            selected_files_json='[{"path":"app/example.py","rationale":"route test"}]',
        )
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


def test_project_autonomy_list_requires_paired_client(client):
    response = client.get("/api/brain/project/autonomy/runs")

    assert response.status_code == 403
    assert response.json()["detail"]["ok"] is False
