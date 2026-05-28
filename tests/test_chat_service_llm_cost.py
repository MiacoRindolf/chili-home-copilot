from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services import chat_service


def _recent(message: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(role="user", content=message)]


def _identity() -> dict:
    return {"user_name": "Tester", "is_guest": True, "user_id": None}


def test_ctx_none_gateway_error_does_not_fall_back_to_direct_openai(monkeypatch):
    direct_chat = MagicMock(side_effect=AssertionError("direct OpenAI chat bypassed gateway"))

    monkeypatch.setattr(chat_service.openai_client, "is_configured", lambda: True)
    monkeypatch.setattr(chat_service.openai_client, "SYSTEM_PROMPT", "system")
    monkeypatch.setattr(chat_service.openai_client, "chat", direct_chat)
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.gateway_chat",
        MagicMock(side_effect=RuntimeError("gateway unavailable")),
    )

    result = chat_service.resolve_response(
        db=object(),
        message="tell me an abstract joke about phase space",
        recent=_recent("tell me an abstract joke about phase space"),
        identity=_identity(),
        ctx=None,
        on_planner_page=False,
        trace_id="chat-cost-test",
    )

    assert result["action_type"] == "llm_offline"
    assert result["model_used"] == "offline"
    direct_chat.assert_not_called()


def test_unknown_chat_gateway_error_does_not_fall_back_to_direct_openai(monkeypatch):
    direct_chat = MagicMock(side_effect=AssertionError("direct OpenAI chat bypassed gateway"))

    monkeypatch.setattr(chat_service.openai_client, "is_configured", lambda: True)
    monkeypatch.setattr(chat_service.openai_client, "SYSTEM_PROMPT", "system")
    monkeypatch.setattr(chat_service.openai_client, "chat", direct_chat)
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.gateway_chat",
        MagicMock(side_effect=RuntimeError("gateway unavailable")),
    )
    monkeypatch.setattr(
        chat_service,
        "execute_tool_with_client_action",
        lambda *args, **kwargs: ("", False, "unknown", None),
    )

    result = chat_service.resolve_response(
        db=object(),
        message="compose a note in a style only an LLM would handle",
        recent=_recent("compose a note in a style only an LLM would handle"),
        identity=_identity(),
        ctx={
            "planned": {"type": "unknown", "data": {}, "reply": ""},
            "personality_context": None,
            "rag_context": None,
            "rag_hits": [],
            "model_used": "llama3",
        },
        on_planner_page=False,
        trace_id="chat-cost-test",
    )

    assert result["action_type"] == "unknown"
    assert result["executed"] is False
    assert "I'm not sure what to do" in result["reply"]
    direct_chat.assert_not_called()


def test_search_synthesis_gateway_error_keeps_raw_results_without_direct_openai(monkeypatch):
    direct_chat = MagicMock(side_effect=AssertionError("direct OpenAI chat bypassed gateway"))

    monkeypatch.setattr(chat_service.openai_client, "is_configured", lambda: True)
    monkeypatch.setattr(chat_service.openai_client, "SYSTEM_PROMPT", "system")
    monkeypatch.setattr(chat_service.openai_client, "chat", direct_chat)
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.gateway_chat",
        MagicMock(side_effect=RuntimeError("gateway unavailable")),
    )
    monkeypatch.setattr(
        chat_service,
        "execute_tool_with_client_action",
        lambda *args, **kwargs: ("Raw search result: https://example.test", True, "web_search", None),
    )

    result = chat_service.resolve_response(
        db=object(),
        message="search the web for chili status",
        recent=_recent("search the web for chili status"),
        identity=_identity(),
        ctx={
            "planned": {"type": "web_search", "data": {"query": "chili status"}, "reply": ""},
            "personality_context": None,
            "rag_context": None,
            "rag_hits": [],
            "model_used": "llama3",
        },
        on_planner_page=False,
        trace_id="chat-cost-test",
    )

    assert result["action_type"] == "web_search"
    assert result["reply"] == "Raw search result: https://example.test"
    direct_chat.assert_not_called()


def test_chat_service_source_has_no_direct_chat_fallback():
    source = inspect.getsource(chat_service.resolve_response)

    assert "openai_client.chat(" not in source
    assert "direct_openai_bypass_disabled" in source
