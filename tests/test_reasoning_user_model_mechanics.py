from __future__ import annotations

from unittest.mock import MagicMock

from app.services.reasoning_brain import user_model


def test_mechanical_user_model_from_explicit_memories():
    signals = {
        "memories": "\n".join(
            [
                "Things I remember about this person:",
                "- [goal] Goal: build a Python trading dashboard this year",
                "- [preference] Prefers brief direct replies",
                "- [interest] Likes options trading",
            ]
        ),
        "personality": "Preferred tone: direct",
        "recent_messages": "",
        "trading_events": "",
    }

    data = user_model._mechanical_user_model(signals)

    assert data is not None
    assert data["risk_tolerance"] == "medium"
    assert data["decision_style"] == "unsure"
    assert data["communication_prefs"] == {
        "detail_level": "brief",
        "tone": "direct",
        "examples": ["Prefers brief direct replies"],
    }
    assert data["active_goals"] == [
        {
            "area": "trading",
            "goal": "build a Python trading dashboard this year",
            "horizon": "long",
        }
    ]
    assert data["knowledge_gaps"] == []
    assert data["source_memory_count"] == 3


def test_call_llm_skips_openai_when_mechanical_model_exists(monkeypatch):
    signals = {
        "memories": "\n".join(
            [
                "Things I remember about this person:",
                "- [goal] Goal: improve portfolio risk controls",
                "- [preference] Prefers concise replies",
            ]
        ),
        "personality": "",
        "recent_messages": "",
        "trading_events": "",
    }
    is_configured = MagicMock(side_effect=AssertionError("mechanical model should not check LLM config"))
    chat = MagicMock(side_effect=AssertionError("mechanical model should not call OpenAI"))
    monkeypatch.setattr(user_model.openai_client, "is_configured", is_configured)
    monkeypatch.setattr(user_model.openai_client, "chat", chat)

    data = user_model._call_llm(signals, trace_id="test_reasoning_user_model")

    assert data is not None
    assert data["active_goals"][0]["area"] == "trading"
    is_configured.assert_not_called()
    chat.assert_not_called()


def test_gateway_exception_does_not_fall_back_to_direct_openai(monkeypatch):
    signals = {
        "memories": "",
        "personality": "",
        "recent_messages": "USER: I keep asking broad planning questions.",
        "trading_events": "",
    }
    direct_chat = MagicMock(side_effect=AssertionError("reasoning_user_model must not bypass gateway"))
    monkeypatch.setattr(user_model.openai_client, "is_configured", lambda: True)
    monkeypatch.setattr(user_model.openai_client, "chat", direct_chat)

    from app.services.context_brain import llm_gateway

    monkeypatch.setattr(
        llm_gateway,
        "gateway_chat",
        MagicMock(side_effect=RuntimeError("gateway unavailable")),
    )

    assert user_model._call_llm(signals, trace_id="test_reasoning_user_model") is None
    direct_chat.assert_not_called()


def test_reasoning_user_model_source_has_no_direct_chat_fallback():
    source = user_model.__loader__.get_source(user_model.__name__) or ""

    assert "openai_client.chat(" not in source
