from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app import memory
from app.models import UserMemory


class EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return []


class FakeDb:
    def __init__(self):
        self.added = []
        self.commits = 0

    def query(self, model):
        return EmptyQuery()

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


def test_mechanical_memory_extracts_preferences_schedule_and_habits():
    facts, complete = memory._extract_mechanical_facts(
        "I prefer concise replies and every morning I review my portfolio"
    )

    assert complete is True
    assert facts == [
        {"category": "preference", "content": "Prefers concise replies"},
        {"category": "schedule", "content": "Every morning: review my portfolio"},
    ]

    facts, complete = memory._extract_mechanical_facts(
        "My wife is Alex and I usually review my portfolio at 8am"
    )

    assert complete is True
    assert facts == [
        {"category": "person", "content": "Wife is Alex"},
        {"category": "habit", "content": "Usually review my portfolio at 8am"},
    ]


def test_mechanical_memory_extracts_events_and_dislikes():
    facts, complete = memory._extract_mechanical_facts(
        "My birthday is June 4 and I hate long meetings"
    )

    assert complete is True
    assert facts == [
        {"category": "event", "content": "Birthday: June 4"},
        {"category": "preference", "content": "Dislikes long meetings"},
    ]


def test_extract_facts_skips_openai_for_new_mechanical_patterns(monkeypatch):
    db = FakeDb()
    is_configured = MagicMock(side_effect=AssertionError("complete mechanical memory should not check LLM config"))
    direct_chat = MagicMock(side_effect=AssertionError("complete mechanical memory should not call OpenAI"))
    monkeypatch.setattr(memory.openai_client, "is_configured", is_configured)
    monkeypatch.setattr(memory.openai_client, "chat", direct_chat)

    stored = memory.extract_facts(
        "I prefer concise replies and every morning I review my portfolio",
        "Noted.",
        user_id=42,
        db=db,
        trace_id="test_memory",
    )

    assert stored == [
        {"category": "preference", "content": "Prefers concise replies"},
        {"category": "schedule", "content": "Every morning: review my portfolio"},
    ]
    assert len(db.added) == 2
    assert all(isinstance(row, UserMemory) for row in db.added)
    assert db.commits == 1
    is_configured.assert_not_called()
    direct_chat.assert_not_called()


def test_gateway_exception_stores_mechanical_partial_without_direct_openai(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(memory.openai_client, "is_configured", lambda: True)
    direct_chat = MagicMock(side_effect=AssertionError("memory_extract must not bypass gateway"))
    monkeypatch.setattr(memory.openai_client, "chat", direct_chat)

    from app.services.context_brain import llm_gateway

    monkeypatch.setattr(
        llm_gateway,
        "gateway_chat",
        MagicMock(side_effect=RuntimeError("gateway unavailable")),
    )

    stored = memory.extract_facts(
        "I prefer concise replies and my brother is Sam",
        "Got it.",
        user_id=42,
        db=db,
        trace_id="test_memory",
    )

    assert stored == [{"category": "preference", "content": "Prefers concise replies"}]
    assert len(db.added) == 1
    assert db.commits == 1
    direct_chat.assert_not_called()


def test_memory_source_has_no_direct_chat_fallback():
    source = Path(memory.__file__).read_text(encoding="utf-8")

    assert "openai_client.chat(" not in source
