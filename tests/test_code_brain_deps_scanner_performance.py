from __future__ import annotations

from app.models.code_brain import CodeDepAlert
from app.services.code_brain.deps_scanner import _alerts_by_package


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

    def query(self, model):
        assert model is CodeDepAlert
        self.query_calls += 1
        self.last_query = _FakeQuery(self._rows)
        return self.last_query


def test_alerts_by_package_batches_lookup():
    rows = [
        CodeDepAlert(repo_id=7, package_name="fastapi"),
        CodeDepAlert(repo_id=7, package_name="pytest"),
    ]
    db = _FakeSession(rows)

    result = _alerts_by_package(db, 7, ["fastapi", "pytest", "fastapi"])

    assert sorted(result) == ["fastapi", "pytest"]
    assert result["fastapi"].package_name == "fastapi"
    assert db.query_calls == 1
    assert db.last_query.filter_calls == 1


def test_alerts_by_package_skips_empty_names():
    db = _FakeSession([])

    assert _alerts_by_package(db, 7, []) == {}
    assert db.query_calls == 0
