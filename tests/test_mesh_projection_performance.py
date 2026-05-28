from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import BrainGraphNode
from app.services.trading.brain_neural_mesh.projection import _brain_graph_nodes_by_id


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
        assert model is BrainGraphNode
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def test_brain_graph_nodes_by_id_batches_lookup() -> None:
    first = SimpleNamespace(id="source", label="Source")
    duplicate = SimpleNamespace(id="source", label="Duplicate")
    target = SimpleNamespace(id="target", label="Target")
    db = _FakeSession([first, duplicate, target])

    result = _brain_graph_nodes_by_id(db, {"", "source", "target"})

    assert result == {"source": duplicate, "target": target}
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_brain_graph_nodes_by_id_skips_empty_lookup() -> None:
    db = _FakeSession([])

    assert _brain_graph_nodes_by_id(db, {""}) == {}
    assert db.query_calls == 0
