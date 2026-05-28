from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import ScanPattern
from app.services.trading.attribution_service import _scan_patterns_by_id


class _FakeQuery:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def all(self) -> list[SimpleNamespace]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None

    def query(self, model: object) -> _FakeQuery:
        assert model is ScanPattern
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def test_scan_patterns_by_id_batches_lookup() -> None:
    first = SimpleNamespace(id=2, name="breakout")
    duplicate = SimpleNamespace(id=2, name="duplicate")
    other = SimpleNamespace(id=5, name="pullback")
    db = _FakeSession([first, duplicate, other])

    result = _scan_patterns_by_id(db, {0, 2, 5})

    assert result == {2: duplicate, 5: other}
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_scan_patterns_by_id_skips_empty_lookup() -> None:
    db = _FakeSession([])

    assert _scan_patterns_by_id(db, {0}) == {}
    assert db.query_calls == 0
