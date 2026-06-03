from __future__ import annotations

from types import SimpleNamespace

from app.services.trading import expectancy_service


class _FakeQuery:
    def __init__(self, row: object) -> None:
        self.row = row
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def one_or_none(self) -> object:
        return self.row


class _FakeSession:
    def __init__(self, row: object) -> None:
        self.row = row
        self.query_args: tuple[object, ...] | None = None
        self.last_query: _FakeQuery | None = None

    def query(self, *args: object) -> _FakeQuery:
        self.query_args = args
        self.last_query = _FakeQuery(self.row)
        return self.last_query


def test_compute_expectancy_edges_reads_pattern_return_columns_only() -> None:
    db = _FakeSession((12.0, 3.0))

    out = expectancy_service.compute_expectancy_edges(
        db,  # type: ignore[arg-type]
        scan_pattern_id=42,
        viability_score=0.01,
        viability_eligible=True,
        regime_multiplier=1.0,
        uncertainty_haircut=0.0,
        execution_penalty=0.0,
        capacity_soft_penalty=0.0,
        correlation_penalty=0.0,
    )

    assert out["research_prior"] == 0.072
    assert out["expected_edge_gross"] == 0.072
    assert db.query_args is not None
    assert tuple(getattr(arg, "key", None) for arg in db.query_args) == (
        "oos_avg_return_pct",
        "avg_return_pct",
    )
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_pattern_return_field_handles_object_tuple_and_empty_rows() -> None:
    assert expectancy_service._pattern_return_field((0.0, 5.0), "avg_return_pct", 1) == 5.0
    row = SimpleNamespace(oos_avg_return_pct=2.5)
    assert expectancy_service._pattern_return_field(row, "oos_avg_return_pct", 0) == 2.5
    assert expectancy_service._pattern_return_field((), "oos_avg_return_pct", 0) is None


def test_compute_expectancy_edges_falls_back_to_average_return_column() -> None:
    db = _FakeSession((0.0, -9.0))

    out = expectancy_service.compute_expectancy_edges(
        db,  # type: ignore[arg-type]
        scan_pattern_id=42,
        viability_score=0.01,
        viability_eligible=True,
        regime_multiplier=1.0,
        uncertainty_haircut=0.0,
        execution_penalty=0.0,
        capacity_soft_penalty=0.0,
        correlation_penalty=0.0,
    )

    assert out["research_prior"] == 0.054
