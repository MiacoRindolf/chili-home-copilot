from __future__ import annotations

from app.services.code_brain.reviewer import _reviewed_commit_hashes


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filter_calls = 0

    def filter(self, *_args, **_kwargs):
        self.filter_calls += 1
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


def test_reviewed_commit_hashes_batches_existing_review_lookup():
    db = _FakeSession([("abc123",), ("def456",)])

    result = _reviewed_commit_hashes(db, ["abc123", "def456", "ghi789"])

    assert result == {"abc123", "def456"}
    assert db.query_calls == 1
    assert db.last_query.filter_calls == 1


def test_reviewed_commit_hashes_skips_empty_input():
    db = _FakeSession([])

    assert _reviewed_commit_hashes(db, []) == set()
    assert db.query_calls == 0
