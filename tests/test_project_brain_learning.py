from __future__ import annotations

from types import SimpleNamespace

from app.services.project_brain import learning


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def group_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.query_calls = 0

    def query(self, *_args, **_kwargs):
        self.query_calls += 1
        return _FakeQuery(self._rows)


def test_prioritize_agents_batches_pending_message_counts():
    db = _FakeSession([
        ("backend", 2),
        ("product_owner", 1),
    ])
    agents = [
        ("architect", SimpleNamespace(label="Architect")),
        ("backend", SimpleNamespace(label="Backend")),
        ("product_owner", SimpleNamespace(label="Product Owner")),
    ]

    prioritized = learning._prioritize_agents(agents, db, user_id=42)

    assert [name for name, _agent in prioritized] == [
        "product_owner",
        "backend",
        "architect",
    ]
    assert db.query_calls == 1
