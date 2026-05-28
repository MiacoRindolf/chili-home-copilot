from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.reasoning_brain import proactive_chat


class EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return None


class EmptyDb:
    def query(self, *args, **kwargs):
        return EmptyQuery()


@pytest.fixture(autouse=True)
def clear_pending_openers():
    proactive_chat._pending_openers.clear()
    yield
    proactive_chat._pending_openers.clear()


def _goal(dimension: str, description: str):
    return SimpleNamespace(id=17, dimension=dimension, description=description)


def test_general_personality_opener_is_mechanical_and_non_meta():
    goal = _goal(
        "general_personality",
        "Understand the user's general preferences and priorities.",
    )

    message = proactive_chat._mechanical_opening_message(goal)

    assert message == "What have you been getting into lately, outside the usual day-to-day stuff?"
    lowered = message.lower()
    assert "data" not in lowered
    assert "learning" not in lowered
    assert "collecting info" not in lowered
    assert "ai" not in lowered


def test_generate_opening_message_skips_openai_for_common_goal(monkeypatch):
    goal = _goal(
        "general_personality",
        "Understand the user's general preferences and priorities.",
    )
    is_configured = MagicMock(side_effect=AssertionError("mechanical opener should not check OpenAI config"))
    chat = MagicMock(side_effect=AssertionError("mechanical opener should not call OpenAI"))
    monkeypatch.setattr(proactive_chat.openai_client, "is_configured", is_configured)
    monkeypatch.setattr(proactive_chat.openai_client, "chat", chat)

    data = proactive_chat.generate_opening_message(EmptyDb(), user_id=42, goal=goal)

    assert data == {
        "message": "What have you been getting into lately, outside the usual day-to-day stuff?",
        "goal_id": 17,
        "goal_description": "Understand the user's general preferences and priorities.",
    }
    assert proactive_chat._pending_openers[42] == data
    is_configured.assert_not_called()
    chat.assert_not_called()


@pytest.mark.parametrize(
    ("dimension", "description", "expected_fragment"),
    [
        ("risk_tolerance", "Understand appetite for uncertainty.", "real upside"),
        ("decision_style", "Learn how choices are made.", "tricky choice"),
        ("communication_prefs", "Understand preferred response style.", "quick version"),
        ("active_goals", "Understand current priorities.", "one thing"),
    ],
)
def test_common_goal_types_have_mechanical_openers(dimension, description, expected_fragment):
    message = proactive_chat._mechanical_opening_message(_goal(dimension, description))

    assert message is not None
    assert expected_fragment in message


def test_unknown_goal_still_uses_existing_llm_gate(monkeypatch):
    goal = _goal("novel_gap", "Ask about a highly specific unknown topic.")
    monkeypatch.setattr(proactive_chat.openai_client, "is_configured", lambda: False)
    chat = MagicMock(side_effect=AssertionError("unconfigured OpenAI should not be called"))
    monkeypatch.setattr(proactive_chat.openai_client, "chat", chat)

    assert proactive_chat.generate_opening_message(EmptyDb(), user_id=42, goal=goal) is None
    chat.assert_not_called()


def test_unknown_goal_uses_gateway_when_configured(monkeypatch):
    goal = _goal("novel_gap", "Ask about a highly specific unknown topic.")
    monkeypatch.setattr(proactive_chat.openai_client, "is_configured", lambda: True)
    direct_chat = MagicMock(side_effect=AssertionError("reasoning_proactive must not bypass gateway"))
    monkeypatch.setattr(proactive_chat.openai_client, "chat", direct_chat)

    from app.services.context_brain import llm_gateway

    gateway = MagicMock(return_value={"reply": "What has made that topic feel worth exploring now?"})
    monkeypatch.setattr(llm_gateway, "gateway_chat", gateway)

    data = proactive_chat.generate_opening_message(EmptyDb(), user_id=42, goal=goal)

    assert data == {
        "message": "What has made that topic feel worth exploring now?",
        "goal_id": 17,
        "goal_description": "Ask about a highly specific unknown topic.",
    }
    assert proactive_chat._pending_openers[42] == data
    assert gateway.call_args.kwargs["purpose"] == "reasoning_proactive"
    direct_chat.assert_not_called()


def test_gateway_exception_does_not_fall_back_to_direct_openai(monkeypatch):
    goal = _goal("novel_gap", "Ask about a highly specific unknown topic.")
    monkeypatch.setattr(proactive_chat.openai_client, "is_configured", lambda: True)
    direct_chat = MagicMock(side_effect=AssertionError("reasoning_proactive must not bypass gateway"))
    monkeypatch.setattr(proactive_chat.openai_client, "chat", direct_chat)

    from app.services.context_brain import llm_gateway

    monkeypatch.setattr(
        llm_gateway,
        "gateway_chat",
        MagicMock(side_effect=RuntimeError("gateway unavailable")),
    )

    assert proactive_chat.generate_opening_message(EmptyDb(), user_id=42, goal=goal) is None
    assert 42 not in proactive_chat._pending_openers
    direct_chat.assert_not_called()


def test_reasoning_proactive_source_has_no_direct_chat_fallback():
    source = Path(proactive_chat.__file__).read_text(encoding="utf-8")

    assert "openai_client.chat(" not in source
