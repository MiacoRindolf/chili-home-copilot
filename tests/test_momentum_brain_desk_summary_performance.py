from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import MomentumStrategyVariant
from app.services.trading.momentum_neural.brain_desk_summary import _momentum_variants_by_id


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
        assert model is MomentumStrategyVariant
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def test_momentum_variants_by_id_batches_lookup() -> None:
    first = SimpleNamespace(id=3, family="breakout")
    duplicate = SimpleNamespace(id=3, family="duplicate")
    other = SimpleNamespace(id=5, family="pullback")
    db = _FakeSession([first, duplicate, other])

    result = _momentum_variants_by_id(db, {0, 3, 5})

    assert result == {3: duplicate, 5: other}
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_momentum_variants_by_id_skips_empty_lookup() -> None:
    db = _FakeSession([])

    assert _momentum_variants_by_id(db, {0}) == {}
    assert db.query_calls == 0
