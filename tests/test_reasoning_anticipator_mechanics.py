from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.reasoning_brain import anticipator


class EmptyQuery:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class FakeDb:
    def __init__(self, existing=None):
        self.added = []
        self.commits = 0
        self.existing = list(existing or [])

    def query(self, *args, **kwargs):
        return EmptyQuery(self.existing)

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


def _model(active_goals):
    return SimpleNamespace(
        decision_style=None,
        risk_tolerance=None,
        communication_prefs=None,
        active_goals=json.dumps(active_goals),
        knowledge_gaps=None,
    )


def test_mechanical_anticipations_from_active_goals():
    items = anticipator._mechanical_anticipation_items(
        _model(
            [
                {"area": "trading", "goal": "improve portfolio risk controls", "horizon": "medium"},
                {"area": "coding", "goal": "ship the CHILI mobile dashboard", "horizon": "short"},
                {"area": "life", "goal": "make morning routines smoother", "horizon": "medium"},
            ]
        )
    )

    assert [item["domain"] for item in items] == ["trading", "code", "life"]
    assert items[0]["description"] == "Prepare a concise trading risk check for improve portfolio risk controls."
    assert items[0]["context"] == {
        "why": "derived_from_active_goal",
        "goal": "improve portfolio risk controls",
        "horizon": "medium",
    }
    assert items[1]["description"] == "Keep recent project context ready for ship the CHILI mobile dashboard."
    assert items[2]["confidence"] == 0.72


def test_generate_anticipations_skips_openai_when_goals_are_mechanical(monkeypatch):
    db = FakeDb()
    model = _model([{"area": "trading", "goal": "reduce options drawdown", "horizon": "short"}])
    monkeypatch.setattr(anticipator, "_current_user_model", lambda *_args, **_kwargs: model)

    from app import openai_client

    is_configured = MagicMock(side_effect=AssertionError("mechanical anticipations should not check LLM config"))
    direct_chat = MagicMock(side_effect=AssertionError("mechanical anticipations should not call OpenAI"))
    monkeypatch.setattr(openai_client, "is_configured", is_configured)
    monkeypatch.setattr(openai_client, "chat", direct_chat)

    rows = anticipator.generate_anticipations(db, user_id=42, trace_id="test_reasoning_anticipate")

    assert len(rows) == 1
    assert db.added == rows
    assert db.commits == 1
    assert rows[0].domain == "trading"
    assert "reduce options drawdown" in rows[0].description
    is_configured.assert_not_called()
    direct_chat.assert_not_called()


def test_store_anticipations_skips_existing_pending_duplicate():
    existing = [
        SimpleNamespace(description="Prepare a concise trading risk check for reduce options drawdown.")
    ]
    db = FakeDb(existing=existing)
    rows = anticipator._store_anticipations(
        db,
        user_id=42,
        items=[
            {
                "description": "Prepare a concise trading risk check for reduce options drawdown.",
                "domain": "trading",
                "confidence": 0.72,
                "context": {"why": "derived_from_active_goal"},
            },
            {
                "description": "Keep recent project context ready for ship the dashboard.",
                "domain": "code",
                "confidence": 0.72,
                "context": {"why": "derived_from_active_goal"},
            },
        ],
        trace_id="test_reasoning_anticipate",
        source="mechanical",
    )

    assert len(rows) == 1
    assert rows[0].domain == "code"
    assert db.added == rows
    assert db.commits == 1


def test_gateway_exception_does_not_fall_back_to_direct_openai(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(anticipator, "_current_user_model", lambda *_args, **_kwargs: _model([]))

    from app import openai_client
    from app.services.context_brain import llm_gateway

    direct_chat = MagicMock(side_effect=AssertionError("reasoning_anticipate must not bypass gateway"))
    monkeypatch.setattr(openai_client, "is_configured", lambda: True)
    monkeypatch.setattr(openai_client, "chat", direct_chat)
    monkeypatch.setattr(
        llm_gateway,
        "gateway_chat",
        MagicMock(side_effect=RuntimeError("gateway unavailable")),
    )

    assert anticipator.generate_anticipations(db, user_id=42, trace_id="test_reasoning_anticipate") == []
    assert db.added == []
    direct_chat.assert_not_called()


def test_reasoning_anticipator_source_has_no_direct_chat_fallback():
    source = Path(anticipator.__file__).read_text(encoding="utf-8")

    assert "openai_client.chat(" not in source
