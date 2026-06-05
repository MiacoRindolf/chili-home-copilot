from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models import ReasoningInterest, ReasoningResearch
from app.services.reasoning_brain import web_researcher


class FakeQuery:
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
    def __init__(self, *, interests=None, research=None):
        self.interests = list(interests or [])
        self.research = list(research or [])
        self.added = []
        self.commits = 0

    def query(self, model):
        if model is ReasoningInterest:
            return FakeQuery(self.interests)
        if model is ReasoningResearch:
            return FakeQuery(self.research)
        return FakeQuery()

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


def _interest(topic):
    return SimpleNamespace(topic=topic)


def test_mechanical_research_summary_from_search_results():
    raw = json.dumps(
        [
            {
                "title": "CPCV guide",
                "href": "https://example.com/cpcv",
                "body": "CPCV reduces overfitting by validating across combinatorial train/test splits.",
            },
            {
                "title": "Promotion gates",
                "href": "https://example.com/gates",
                "body": "Promotion gates compare walk-forward evidence before deploying a model live.",
            },
        ]
    )

    data = web_researcher._mechanical_research_summary("CPCV promotion gates", raw)

    assert data is not None
    assert data["topic"] == "CPCV promotion gates"
    assert "CPCV reduces overfitting" in data["summary"]
    assert data["sources"] == [
        {"title": "CPCV guide", "url": "https://example.com/cpcv"},
        {"title": "Promotion gates", "url": "https://example.com/gates"},
    ]
    assert data["relevance_score"] > 0.0


def test_refresh_research_uses_search_results_without_openai(monkeypatch):
    db = FakeDb(interests=[_interest("CPCV promotion gates")])
    raw_results = [
        {
            "title": "CPCV guide",
            "href": "https://example.com/cpcv",
            "body": "CPCV reduces overfitting by validating across combinatorial train/test splits.",
        }
    ]
    monkeypatch.setattr(web_researcher.settings, "reasoning_enabled", True)
    monkeypatch.setattr(web_researcher.settings, "reasoning_max_web_searches", 5)
    monkeypatch.setattr(web_researcher, "_search_topic", lambda *_args, **_kwargs: json.dumps(raw_results))
    is_configured = MagicMock(side_effect=AssertionError("mechanical research should not check LLM config"))
    direct_chat = MagicMock(side_effect=AssertionError("mechanical research should not call OpenAI"))
    monkeypatch.setattr(web_researcher.openai_client, "is_configured", is_configured)
    monkeypatch.setattr(web_researcher.openai_client, "chat", direct_chat)

    web_researcher.refresh_research_for_top_interests(db, user_id=42, trace_id="test_research")

    assert len(db.added) == 1
    assert db.added[0].topic == "CPCV promotion gates"
    assert "CPCV reduces overfitting" in db.added[0].summary
    assert db.commits == 1
    is_configured.assert_not_called()
    direct_chat.assert_not_called()


def test_refresh_research_skips_existing_non_stale_topic(monkeypatch):
    db = FakeDb(
        interests=[_interest("CPCV promotion gates")],
        research=[SimpleNamespace(topic="CPCV promotion gates", stale=False)],
    )
    search = MagicMock(side_effect=AssertionError("fresh research should skip search"))
    monkeypatch.setattr(web_researcher.settings, "reasoning_enabled", True)
    monkeypatch.setattr(web_researcher.settings, "reasoning_max_web_searches", 5)
    monkeypatch.setattr(web_researcher, "_search_topic", search)

    web_researcher.refresh_research_for_top_interests(db, user_id=42, trace_id="test_research")

    assert db.added == []
    assert db.commits == 1
    search.assert_not_called()


def test_gateway_exception_does_not_fall_back_to_direct_openai(monkeypatch):
    db = FakeDb(interests=[_interest("unstructured topic")])
    monkeypatch.setattr(web_researcher.settings, "reasoning_enabled", True)
    monkeypatch.setattr(web_researcher.settings, "reasoning_max_web_searches", 5)
    monkeypatch.setattr(web_researcher, "_search_topic", lambda *_args, **_kwargs: "plain unstructured text")
    monkeypatch.setattr(web_researcher.openai_client, "is_configured", lambda: True)
    direct_chat = MagicMock(side_effect=AssertionError("reasoning_web_research must not bypass gateway"))
    monkeypatch.setattr(web_researcher.openai_client, "chat", direct_chat)

    from app.services.context_brain import llm_gateway

    monkeypatch.setattr(
        llm_gateway,
        "gateway_chat",
        MagicMock(side_effect=RuntimeError("gateway unavailable")),
    )

    web_researcher.refresh_research_for_top_interests(db, user_id=42, trace_id="test_research")

    assert db.added == []
    direct_chat.assert_not_called()


def test_reasoning_web_research_source_has_no_direct_chat_fallback():
    source = Path(web_researcher.__file__).read_text(encoding="utf-8")

    assert "openai_client.chat(" not in source


def test_research_topic_now_stores_mechanical(monkeypatch):
    db = FakeDb()
    raw_results = [
        {
            "title": "VIX explainer",
            "href": "https://example.com/vix",
            "body": "The VIX measures expected 30-day volatility implied by S&P 500 options.",
        }
    ]
    monkeypatch.setattr(web_researcher.settings, "reasoning_enabled", True)
    monkeypatch.setattr(
        web_researcher, "_search_topic", lambda *_a, **_k: json.dumps(raw_results)
    )

    data = web_researcher.research_topic_now(db, user_id=7, topic="what is the VIX")

    assert data is not None
    assert data["topic"] == "what is the VIX"
    assert "VIX measures" in data["summary"]
    assert len(db.added) == 1
    assert db.added[0].user_id == 7
    assert db.commits == 1


def test_research_topic_now_disabled_returns_none(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(web_researcher.settings, "reasoning_enabled", False)
    monkeypatch.setattr(
        web_researcher, "_search_topic", lambda *_a, **_k: "[]"
    )
    assert web_researcher.research_topic_now(db, user_id=7, topic="x") is None
    assert db.added == []


def test_research_topic_now_blank_topic_returns_none(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(web_researcher.settings, "reasoning_enabled", True)
    assert web_researcher.research_topic_now(db, user_id=7, topic="   ") is None
    assert db.added == []
