from __future__ import annotations

from app.models import ReasoningInterest
from app.services.reasoning_brain import interest_graph


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows


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
