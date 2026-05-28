from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.reasoning_brain import evolution


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

    def first(self):
        return self._rows[0] if self._rows else None


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


def _model(*, active_goals=None, knowledge_gaps=None, decision_style="unsure", risk_tolerance="unsure"):
    return SimpleNamespace(
        active_goals=json.dumps(active_goals or []),
        knowledge_gaps=json.dumps(knowledge_gaps or []),
        decision_style=decision_style,
        risk_tolerance=risk_tolerance,
    )


def test_mechanical_hypotheses_from_goals_and_gaps():
    items = evolution._mechanical_hypothesis_items(
        _model(
            active_goals=[
                {"area": "trading", "goal": "reduce options drawdown", "horizon": "short"},
                {"area": "coding", "goal": "ship the CHILI dashboard", "horizon": "medium"},
            ],
            knowledge_gaps=[
                {"topic": "CPCV promotion", "description": "how promotion gates avoid overfitting"},
            ],
        )
    )

    assert items[:3] == [
        {
            "claim": "User will engage more when CHILI offers practical next steps for reduce options drawdown.",
            "domain": "trading",
        },
        {
            "claim": "User will engage more when CHILI offers practical next steps for ship the CHILI dashboard.",
            "domain": "code",
        },
        {
            "claim": "User may need clearer explanations about CPCV promotion: how promotion gates avoid overfitting.",
            "domain": "trading",
        },
    ]


def test_generate_hypotheses_skips_openai_for_mechanical_items(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(
        evolution,
        "_current_user_model",
        lambda *_args, **_kwargs: _model(active_goals=[{"area": "trading", "goal": "tighten risk sizing"}]),
    )
    is_configured = MagicMock(side_effect=AssertionError("mechanical hypotheses should not check LLM config"))
    direct_chat = MagicMock(side_effect=AssertionError("mechanical hypotheses should not call OpenAI"))
    monkeypatch.setattr(evolution.openai_client, "is_configured", is_configured)
    monkeypatch.setattr(evolution.openai_client, "chat", direct_chat)

    rows = evolution.generate_hypotheses(db, user_id=42)

    assert len(rows) == 1
    assert db.added == rows
    assert db.commits == 1
    assert rows[0].domain == "trading"
    assert "tighten risk sizing" in rows[0].claim
    is_configured.assert_not_called()
    direct_chat.assert_not_called()


def test_store_hypotheses_skips_existing_active_duplicate():
    existing = [
        SimpleNamespace(
            claim="User will engage more when CHILI offers practical next steps for tighten risk sizing."
        )
    ]
    db = FakeDb(existing=existing)

    rows = evolution._store_hypotheses(
        db,
        user_id=42,
        items=[
            {
                "claim": "User will engage more when CHILI offers practical next steps for tighten risk sizing.",
                "domain": "trading",
            },
            {
                "claim": "User may need clearer explanations about CPCV promotion.",
                "domain": "trading",
            },
        ],
        source="mechanical",
    )

    assert len(rows) == 1
    assert rows[0].claim == "User may need clearer explanations about CPCV promotion."
    assert db.added == rows
    assert db.commits == 1


def test_gateway_exception_does_not_fall_back_to_direct_openai(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(evolution, "_current_user_model", lambda *_args, **_kwargs: _model())
    direct_chat = MagicMock(side_effect=AssertionError("reasoning_evolve must not bypass gateway"))
    monkeypatch.setattr(evolution.openai_client, "is_configured", lambda: True)
    monkeypatch.setattr(evolution.openai_client, "chat", direct_chat)

    from app.services.context_brain import llm_gateway

    monkeypatch.setattr(
        llm_gateway,
        "gateway_chat",
        MagicMock(side_effect=RuntimeError("gateway unavailable")),
    )

    assert evolution.generate_hypotheses(db, user_id=42) == []
    assert db.added == []
    direct_chat.assert_not_called()


def test_reasoning_evolution_source_has_no_direct_chat_fallback():
    source = Path(evolution.__file__).read_text(encoding="utf-8")

    assert "openai_client.chat(" not in source
