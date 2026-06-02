from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import MomentumStrategyVariant
from app.services.trading.momentum_neural.brain_desk_summary import (
    _momentum_variants_by_id,
    _paper_live_weighted_return_summaries,
    _viability_count_stats_from_aggregate,
    _viability_top_lines,
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


def test_viability_count_stats_from_aggregate_handles_tuple_and_object_rows() -> None:
    assert _viability_count_stats_from_aggregate((10, 4, 3, 7)) == {
        "row_count": 10,
        "live_eligible_count": 4,
        "paper_only_count": 3,
        "fresh_last_24h_count": 7,
    }

    row = SimpleNamespace(total=5, live_eligible=None, paper_only=2, fresh_24h=1)
    assert _viability_count_stats_from_aggregate(row) == {
        "row_count": 5,
        "live_eligible_count": 0,
        "paper_only_count": 2,
        "fresh_last_24h_count": 1,
    }

    assert _viability_count_stats_from_aggregate(None) == {
        "row_count": 0,
        "live_eligible_count": 0,
        "paper_only_count": 0,
        "fresh_last_24h_count": 0,
    }


def test_viability_top_lines_formats_column_and_object_rows() -> None:
    rows = [
        ("SOL-USD", 0.876, True),
        SimpleNamespace(symbol="BTC-USD", viability_score=0.124, live_eligible=False),
    ]

    assert _viability_top_lines(rows) == [
        "SOL-USD · 0.88 · live",
        "BTC-USD · 0.12 · paper-only",
    ]


def test_paper_live_weighted_return_summaries_scan_column_rows_once() -> None:
    class OneShotRows(list):
        def __init__(self, values: list[tuple]):
            super().__init__(values)
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("rows scanned more than once")
            return super().__iter__()

    rows = OneShotRows(
        [
            ("paper", 2.0, 10.0),
            ("paper", 1.0, None),
            ("live", 1.5, -20.0),
            ("live", None, 40.0),
            ("shadow", 1.0, 999.0),
        ]
    )

    assert _paper_live_weighted_return_summaries(rows) == {
        "paper": {"n": 2, "mean_return_bps": 6.67},
        "live": {"n": 2, "mean_return_bps": 4.0},
    }
    assert rows.iterations == 1
