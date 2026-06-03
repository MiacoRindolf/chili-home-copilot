from __future__ import annotations

from types import SimpleNamespace

from app.services.trading import prescreen_internal_signals as signals


class _FakeQuery:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.filter_calls = 0
        self.order_by_calls = 0
        self.limit_calls: list[int] = []

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def order_by(self, *args: object) -> "_FakeQuery":
        self.order_by_calls += 1
        return self

    def limit(self, value: int) -> "_FakeQuery":
        self.limit_calls.append(int(value))
        return self

    def first(self) -> object:
        return self.rows[0] if self.rows else None

    def all(self) -> list[object]:
        return self.rows


class _FakeSession:
    def __init__(self, rows_by_keys: dict[tuple[str | None, ...], list[object]]) -> None:
        self.rows_by_keys = rows_by_keys
        self.query_calls: list[tuple[object, ...]] = []
        self.queries: list[_FakeQuery] = []

    def query(self, *args: object) -> _FakeQuery:
        self.query_calls.append(args)
        keys = tuple(getattr(arg, "key", None) for arg in args)
        query = _FakeQuery(self.rows_by_keys.get(keys, []))
        self.queries.append(query)
        return query


def test_latest_predictions_reads_snapshot_id_and_line_columns_only() -> None:
    db = _FakeSession(
        {
            ("id",): [(99,)],
            ("ticker", "sort_rank", "score"): [("aapl", 1, 0.75), ("bad ticker!", 2, 0.2)],
        }
    )

    out = signals.tickers_from_latest_predictions(db, limit=5)  # type: ignore[arg-type]

    assert out == {
        "AAPL": [
            {
                "kind": "brain_prediction",
                "snapshot_id": 99,
                "sort_rank": 1,
                "score": 0.75,
            }
        ]
    }
    assert [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls] == [
        ("id",),
        ("ticker", "sort_rank", "score"),
    ]
    assert [query.order_by_calls for query in db.queries] == [1, 1]
    assert db.queries[1].filter_calls == 1
    assert db.queries[1].limit_calls == [5]


def test_warming_patterns_reads_id_and_scope_tickers_only() -> None:
    db = _FakeSession(
        {
            ("id", "scope_tickers"): [
                (7, ["aapl", "msft"]),
                SimpleNamespace(id=8, scope_tickers=["AAPL", "TSLA"]),
            ],
        }
    )

    out = signals.tickers_from_warming_patterns(db, limit=3)  # type: ignore[arg-type]

    assert out == {
        "AAPL": [{"kind": "warming_pattern", "scan_pattern_id": 7}],
        "MSFT": [{"kind": "warming_pattern", "scan_pattern_id": 7}],
        "TSLA": [{"kind": "warming_pattern", "scan_pattern_id": 8}],
    }
    assert [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls] == [
        ("id", "scope_tickers"),
    ]
    query = db.queries[0]
    assert query.filter_calls == 5
    assert query.order_by_calls == 1
    assert query.limit_calls == [signals._WARMING_PATTERN_QUERY_LIMIT]


def test_row_field_handles_object_tuple_and_empty_rows() -> None:
    assert signals._row_field((1, "AAPL"), "scope_tickers", 1) == "AAPL"
    row = SimpleNamespace(id=3)
    assert signals._row_field(row, "id", 0) == 3
    assert signals._row_field((), "id", 0) is None
