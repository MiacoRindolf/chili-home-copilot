from __future__ import annotations

import inspect
from datetime import date
from unittest.mock import MagicMock

from app.services import chat_service
from app.modules.planner import hooks


def test_mechanical_task_suggestions_cover_common_project_archetypes():
    software = hooks._mechanical_task_suggestions("Build a mobile app MVP")
    job = hooks._mechanical_task_suggestions("Job search for data scientist roles")
    niche = hooks._mechanical_task_suggestions("Research obscure personal idea")

    assert len(software) >= 6
    assert any("smallest usable version" in task["title"].lower() for task in software)
    assert len(job) >= 6
    assert any("resume" in task["title"].lower() for task in job)
    assert niche == []


def test_generate_tasks_for_common_project_skips_llm(monkeypatch):
    created = []

    def fake_create_task(db, project_id, user_id, title, **kwargs):
        created.append(
            {
                "db": db,
                "project_id": project_id,
                "user_id": user_id,
                "title": title,
                **kwargs,
            }
        )
        return {"id": len(created), "title": title}

    is_configured = MagicMock(side_effect=AssertionError("mechanical planner should not check LLM config"))
    chat = MagicMock(side_effect=AssertionError("mechanical planner should not call LLM"))

    monkeypatch.setattr(hooks.planner_service, "create_task", fake_create_task)
    monkeypatch.setattr(hooks.openai_client, "is_configured", is_configured)
    monkeypatch.setattr(hooks.openai_client, "chat", chat)

    added = hooks.generate_tasks_for_project(
        db=object(),
        project_id=42,
        project_name="Build a portfolio website",
        user_id=7,
        trace_id="test-planner",
    )

    assert added == len(created)
    assert added >= 6
    assert created[0]["start_date"] == date.today().isoformat()
    assert all("Complexity:" in task["description"] for task in created)
    is_configured.assert_not_called()
    chat.assert_not_called()


def test_generate_tasks_for_niche_project_keeps_llm_fallback(monkeypatch):
    monkeypatch.setattr(hooks.openai_client, "is_configured", lambda: False)

    added = hooks.generate_tasks_for_project(
        db=object(),
        project_id=42,
        project_name="Research obscure personal idea",
        user_id=7,
        trace_id="test-planner",
    )

    assert added == 0


def test_chat_service_does_not_gate_mechanical_planner_on_openai_config():
    source = inspect.getsource(chat_service.resolve_response)

    assert "openai_client.is_configured() and planner_hooks" not in source
    assert "fallback_project_name\n        and openai_client.is_configured()" not in source
