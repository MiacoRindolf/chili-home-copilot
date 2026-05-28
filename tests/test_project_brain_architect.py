from __future__ import annotations

from app.services.project_brain.agents.architect import _dependency_counts_by_repo


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filter_calls = 0
        self.group_by_calls = 0

    def filter(self, *_args, **_kwargs):
        self.filter_calls += 1
        return self

    def group_by(self, *_args, **_kwargs):
        self.group_by_calls += 1
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None
        self.query_calls = 0

    def query(self, *_args, **_kwargs):
        self.query_calls += 1
        self.last_query = _FakeQuery(self._rows)
        return self.last_query


def test_dependency_counts_by_repo_batches_counts():
    db = _FakeSession([(1, 3), (2, 0), (3, 7)])

    result = _dependency_counts_by_repo(db, [1, 2, 3], circular_only=True)

    assert result == {1: 3, 2: 0, 3: 7}
    assert db.query_calls == 1
    assert db.last_query.filter_calls == 2
    assert db.last_query.group_by_calls == 1


def test_dependency_counts_by_repo_skips_empty_repo_list():
    db = _FakeSession([])

    assert _dependency_counts_by_repo(db, []) == {}
    assert db.query_calls == 0
