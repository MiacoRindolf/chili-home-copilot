from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models import ChatMessage, HousemateProfile, UserMemory
from app import personality


class FakeQuery:
    def __init__(self, rows=None, count_value=0):
        self._rows = list(rows or [])
        self._count_value = count_value

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def count(self):
        return self._count_value


class FakeDb:
    def __init__(self, messages=None, memories=None, profile=None):
        self.messages = list(messages or [])
        self.memories = list(memories or [])
        self.profile = profile
        self.added = []
        self.commits = 0

    def query(self, model):
        if model is UserMemory:
            return FakeQuery(self.memories)
        if model is ChatMessage:
            return FakeQuery(self.messages, count_value=sum(1 for msg in self.messages if msg.role == "user"))
        if model is HousemateProfile:
            return FakeQuery([self.profile] if self.profile else [])
        return FakeQuery()

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


def test_extract_profile_uses_gateway_without_direct_openai(monkeypatch):
    db = FakeDb(messages=[SimpleNamespace(role="user", content="I love cooking and prefer concise replies")])
    direct_chat = MagicMock(side_effect=AssertionError("personality_apply must not bypass gateway"))
    monkeypatch.setattr(personality.openai_client, "chat", direct_chat)

    from app.services.context_brain import llm_gateway

    gateway = MagicMock(
        return_value={
            "reply": json.dumps({
                "interests": ["cooking"],
                "dietary": "",
                "tone": "brief",
                "notes": "Prefers concise replies",
            }),
            "tokens_used": 10,
            "model": "gpt-5.4-mini",
        }
    )
    monkeypatch.setattr(llm_gateway, "gateway_chat", gateway)

    result = personality.extract_profile(42, db, trace_id="test_personality")

    assert result == {
        "interests": ["cooking"],
        "dietary": "",
        "tone": "brief",
        "notes": "Prefers concise replies",
    }
    assert len(db.added) == 1
    assert isinstance(db.added[0], HousemateProfile)
    assert db.commits == 1
    assert gateway.call_args.kwargs["purpose"] == "personality_apply"
    direct_chat.assert_not_called()


def test_gateway_exception_does_not_fall_back_to_direct_openai(monkeypatch):
    db = FakeDb(messages=[SimpleNamespace(role="user", content="I have a few personal preferences")])
    direct_chat = MagicMock(side_effect=AssertionError("personality_apply must not bypass gateway"))
    monkeypatch.setattr(personality.openai_client, "chat", direct_chat)

    from app.services.context_brain import llm_gateway

    monkeypatch.setattr(
        llm_gateway,
        "gateway_chat",
        MagicMock(side_effect=RuntimeError("gateway unavailable")),
    )

    assert personality.extract_profile(42, db, trace_id="test_personality") is None
    assert db.added == []
    direct_chat.assert_not_called()


def test_personality_source_has_no_direct_chat_fallback():
    source = Path(personality.__file__).read_text(encoding="utf-8")

    assert "openai_client.chat(" not in source
