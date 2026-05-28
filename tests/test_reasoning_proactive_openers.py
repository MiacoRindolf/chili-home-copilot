from __future__ import annotations

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
