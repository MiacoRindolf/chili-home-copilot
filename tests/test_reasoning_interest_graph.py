from __future__ import annotations

from types import SimpleNamespace

from app.models import ChatMessage, ReasoningInterest
from app.services.reasoning_brain import interest_graph


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.added = []
        self.query_calls = 0

    def query(self, model):
        assert model is ReasoningInterest
        self.query_calls += 1
        return _FakeQuery(self._rows)

    def add(self, row):
        self.added.append(row)


def test_bump_interests_batches_existing_topic_lookup():
    existing = ReasoningInterest(
        user_id=7,
        topic="python",
        category="explicit",
        weight=1.0,
        source="chat",
    )
    db = _FakeSession([existing])

    interest_graph._bump_interests(
        db,
        user_id=7,
        updates=[
            ("python", "explicit", 0.5, "chat", None),
            ("AAPL", "inferred_trading", 1.3, "trading", None),
            ("  ", "explicit", 10.0, "chat", None),
        ],
    )

    assert db.query_calls == 1
    assert existing.weight == 1.5
    assert existing.source == "chat"
    assert [row.topic for row in db.added] == ["AAPL"]
    assert db.added[0].weight == 1.3


class _GraphFakeSession:
    def __init__(self):
        self.interests = []
        self.messages = [SimpleNamespace(content="market alpha python")]
        self.queried_models = []
        self.added = []
        self.committed = False

    def query(self, model):
        self.queried_models.append(model)
        if model is ReasoningInterest:
            return _FakeQuery(self.interests)
        if model is ChatMessage:
            return _FakeQuery(self.messages)
        raise AssertionError(f"unexpected model query: {model}")

    def add(self, row):
        self.added.append(row)
        if isinstance(row, ReasoningInterest):
            self.interests.append(row)

    def commit(self):
        self.committed = True


def test_rebuild_interest_graph_uses_management_envelope_ticker_adapter(monkeypatch):
    db = _GraphFakeSession()

    monkeypatch.setattr(
        interest_graph,
        "load_recent_management_envelope_tickers_for_user",
        lambda _db, *, user_id, limit: ["AAPL", "aapl", "TSLA"],
    )

    interest_graph.rebuild_interest_graph(db, user_id=7)

    topics = {row.topic: row for row in db.interests}
    assert topics["AAPL"].weight == 1.6
    assert topics["TSLA"].weight == 1.3
    assert topics["python"].source == "chat"
    assert db.committed is True
    assert set(db.queried_models) == {ReasoningInterest, ChatMessage}
