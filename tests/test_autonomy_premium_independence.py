from __future__ import annotations

import inspect

from app import openai_client
from app.config import settings
from app.services import llm_caller
from app.services.code_brain import agent as code_agent
from app.services.coding_task import execution_loop
from app.services.context_brain import llm_gateway


def test_all_autonomy_purposes_default_to_local_only(monkeypatch):
    monkeypatch.setattr(settings, "chili_code_premium_fallback_enabled", False)
    monkeypatch.setattr(settings, "chili_code_frontier_enabled", False)

    purposes = {
        "code_dispatch_plan",
        "code_dispatch_edit",
        "code_dispatch_create",
        "code_dispatch_diagnose",
        "code_review",
        "code_search",
        "project_architect",
        "project_backend_engineer",
        "project_frontend_engineer",
        "project_qa_engineer",
        "project_security_engineer",
        "project_web_research",
    }

    assert all(
        llm_gateway._local_only_code_routing(purpose, None)
        for purpose in purposes
    )
    assert llm_gateway._local_only_code_routing("chat_user", None) is False


def test_project_role_runs_locally_when_cloud_is_unconfigured(monkeypatch):
    captured = {}
    monkeypatch.setattr(openai_client, "is_configured", lambda: False)
    monkeypatch.setattr(openai_client, "is_local_code_configured", lambda: True)

    def fake_gateway(**kwargs):
        captured.update(kwargs)
        return {
            "reply": "local architecture review",
            "model": "qwen2.5-coder:7b",
            "local_only": True,
            "premium_calls": 0,
        }

    monkeypatch.setattr(llm_gateway, "gateway_chat", fake_gateway)
    llm_caller.reset_cache()

    reply = llm_caller.call_llm(
        [{"role": "user", "content": "review this architecture"}],
        purpose="project_architect",
        trace_id="premium-independent-project-role",
    )

    assert reply == "local architecture review"
    assert captured["local_only"] is True
    assert captured["strict_escalation"] is False


def test_optional_premium_fallback_never_becomes_a_cloud_requirement(monkeypatch):
    captured = {}
    monkeypatch.setattr(settings, "chili_code_premium_fallback_enabled", True)
    monkeypatch.setattr(settings, "chili_code_frontier_enabled", False)
    monkeypatch.setattr(openai_client, "is_configured", lambda: False)
    monkeypatch.setattr(openai_client, "is_local_code_configured", lambda: True)

    def fake_gateway(**kwargs):
        captured.update(kwargs)
        return {"reply": "local fallback path", "model": "qwen2.5-coder:7b"}

    monkeypatch.setattr(llm_gateway, "gateway_chat", fake_gateway)

    reply = llm_caller.call_llm(
        [{"role": "user", "content": "review"}],
        purpose="project_architect",
    )

    assert reply == "local fallback path"
    assert captured["local_only"] is False


def test_legacy_execution_loop_has_no_cloud_escape(monkeypatch):
    captured = {}
    monkeypatch.setattr(openai_client, "is_local_code_configured", lambda: True)

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return {
            "reply": '{"analysis":"local"}',
            "model": "qwen2.5-coder:7b",
            "local_only": True,
            "premium_calls": 0,
        }

    monkeypatch.setattr(openai_client, "chat", fake_chat)

    result = execution_loop._llm_chat(
        [{"role": "user", "content": "diagnose the failing test"}],
        "Return JSON.",
        "legacy-loop-local-only",
    )

    assert result["premium_calls"] == 0
    assert captured["local_only"] is True
    assert captured["strict_escalation"] is False


def test_openai_local_only_sentinel_blocks_every_cloud_provider(monkeypatch):
    monkeypatch.setattr(settings, "ollama_host", "http://127.0.0.1:11434")
    monkeypatch.setattr(settings, "chili_code_local_model", "qwen2.5-coder:7b")
    monkeypatch.setattr(openai_client, "_safe_log_llm_call", lambda **kwargs: None)
    monkeypatch.setattr(
        openai_client,
        "_chat_local",
        lambda *args, **kwargs: {
            "reply": "local result",
            "model": "qwen2.5-coder:7b",
            "provider": "ollama",
        },
    )

    def premium_escape(*args, **kwargs):
        raise AssertionError("autonomous coding attempted a premium provider")

    monkeypatch.setattr(openai_client, "_chat_groq", premium_escape)
    monkeypatch.setattr(openai_client, "_chat_openai", premium_escape)
    monkeypatch.setattr(openai_client, "_chat_gemini", premium_escape)
    monkeypatch.setattr(openai_client, "_chat_frontier", premium_escape)

    result = openai_client.chat(
        [{"role": "user", "content": "implement the fix"}],
        local_only=True,
    )

    assert result["reply"] == "local result"
    assert result["premium_calls"] == 0
    assert result["local_only"] is True


def test_code_agent_source_enforces_local_gateway_contract():
    source = inspect.getsource(code_agent.run_code_agent)

    assert "is_local_code_configured" in source
    assert "local_only=True" in source
    assert "premium keys are not required" in source
