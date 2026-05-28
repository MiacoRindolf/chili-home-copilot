from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import BrainGraphEdge, BrainNodeState
from app.services.trading.brain_neural_mesh.plasticity import (
    _brain_graph_edges_by_id,
    _brain_node_states_by_id,
)


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
    def __init__(
        self,
        *,
        edges: list[SimpleNamespace] | None = None,
        node_states: list[SimpleNamespace] | None = None,
    ) -> None:
        self.rows_by_model = {
            BrainGraphEdge: edges or [],
            BrainNodeState: node_states or [],
        }
        self.query_calls: dict[object, int] = {BrainGraphEdge: 0, BrainNodeState: 0}
        self.last_query_by_model: dict[object, _FakeQuery] = {}

    def query(self, model: object) -> _FakeQuery:
        self.query_calls[model] = self.query_calls.get(model, 0) + 1
        query = _FakeQuery(self.rows_by_model.get(model, []))
        self.last_query_by_model[model] = query
        return query


def test_brain_graph_edges_by_id_batches_lookup() -> None:
    first = SimpleNamespace(id=2, source_node_id="a", target_node_id="b")
    duplicate = SimpleNamespace(id=2, source_node_id="c", target_node_id="d")
    other = SimpleNamespace(id=5, source_node_id="e", target_node_id="f")
    db = _FakeSession(edges=[first, duplicate, other])

    result = _brain_graph_edges_by_id(db, {0, 2, 5})

    assert result == {2: duplicate, 5: other}
    assert db.query_calls[BrainGraphEdge] == 1
    assert db.last_query_by_model[BrainGraphEdge].filter_calls == 1


def test_brain_node_states_by_id_batches_lookup() -> None:
    first = SimpleNamespace(node_id="a", confidence=0.7)
    duplicate = SimpleNamespace(node_id="a", confidence=0.8)
    other = SimpleNamespace(node_id="b", confidence=0.5)
    db = _FakeSession(node_states=[first, duplicate, other])

    result = _brain_node_states_by_id(db, {"", "a", "b"})

    assert result == {"a": duplicate, "b": other}
    assert db.query_calls[BrainNodeState] == 1
    assert db.last_query_by_model[BrainNodeState].filter_calls == 1


def test_plasticity_batch_helpers_skip_empty_lookups() -> None:
    db = _FakeSession()

    assert _brain_graph_edges_by_id(db, {0}) == {}
    assert _brain_node_states_by_id(db, {""}) == {}
    assert db.query_calls[BrainGraphEdge] == 0
    assert db.query_calls[BrainNodeState] == 0
