from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading import shadow_testing


class _FakeQuery:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.filter_calls = 0
        self.order_by_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def order_by(self, *args: object) -> "_FakeQuery":
        self.order_by_calls += 1
        return self

    def all(self) -> list[object]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.query_args: tuple[object, ...] | None = None
        self.last_query: _FakeQuery | None = None

    def query(self, *args: object) -> _FakeQuery:
        self.query_args = args
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


class _CreateFakeQuery:
    def __init__(self, row: object) -> None:
        self.row = row
        self.filter_calls = 0

    def filter(self, *args: object) -> "_CreateFakeQuery":
        self.filter_calls += 1
        return self

    def first(self) -> object:
        return self.row


class _CreateFakeSession:
    def __init__(self) -> None:
        self.control_row = (11, "Control")
        self.variant = SimpleNamespace(id=22, name="Variant", paper_book_json={"existing": True})
        self.query_calls: list[tuple[object, ...]] = []
        self.queries: list[_CreateFakeQuery] = []
        self.commit_calls = 0

    def query(self, *args: object) -> _CreateFakeQuery:
        self.query_calls.append(args)
        keys = tuple(getattr(arg, "key", None) for arg in args)
        row = self.control_row if keys == ("id", "name") else self.variant
        query = _CreateFakeQuery(row)
        self.queries.append(query)
        return query

    def commit(self) -> None:
        self.commit_calls += 1


def _closed_tuple_return_row() -> tuple[object, ...]:
    now = datetime(2026, 6, 1, 12, 0)
    return (
        now,
        40.0,
        1.25,
        2.0,
        {"asset_type": "options", "option_meta": {"strike": 500.0}},
        1.45,
        "long",
        5000.0,
        now - timedelta(days=2),
    )


def test_get_closed_trades_reads_paper_return_columns_only() -> None:
    db = _FakeSession([_closed_tuple_return_row()])

    rows = shadow_testing._get_closed_trades(db, pattern_id=42)  # type: ignore[arg-type]

    assert rows == [_closed_tuple_return_row()]
    assert db.query_args is not None
    assert tuple(getattr(arg, "key", None) for arg in db.query_args) == (
        "exit_date",
        "pnl",
        "entry_price",
        "quantity",
        "signal_json",
        "exit_price",
        "direction",
        "pnl_pct",
        "entry_date",
    )
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1
    assert db.last_query.order_by_calls == 1


def test_create_shadow_test_reads_control_identity_columns_only() -> None:
    db = _CreateFakeSession()

    out = shadow_testing.create_shadow_test(db, 11, 22, min_trades=5, min_days=9)  # type: ignore[arg-type]

    assert out == {
        "ok": True,
        "control": {"id": 11, "name": "Control"},
        "variant": {"id": 22, "name": "Variant"},
        "min_trades": 5,
        "min_days": 9,
    }
    assert [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls] == [
        ("id", "name"),
        (None,),
    ]
    assert [query.filter_calls for query in db.queries] == [1, 1]
    assert db.commit_calls == 1
    assert db.variant.paper_book_json["existing"] is True
    assert db.variant.paper_book_json["shadow_test"]["control_id"] == 11
    assert db.variant.paper_book_json["shadow_test"]["variant_id"] == 22


def test_extract_trade_returns_handles_compact_paper_tuple_rows() -> None:
    returns, hold_days = shadow_testing._extract_trade_returns([_closed_tuple_return_row()])

    assert returns == pytest.approx([16.0])
    assert hold_days == pytest.approx([2.0])


def test_paper_return_row_handles_object_tuple_and_empty_rows() -> None:
    row = _closed_tuple_return_row()
    out = shadow_testing._paper_return_row(row)

    assert out.exit_date == row[0]
    assert out.pnl == 40.0
    assert out.signal_json == {"asset_type": "options", "option_meta": {"strike": 500.0}}
    existing = SimpleNamespace(exit_date="date")
    assert shadow_testing._paper_return_row(existing) is existing
    empty = shadow_testing._paper_return_row(())
    assert empty.exit_date is None
    assert empty.entry_date is None


def test_shadow_pattern_identity_row_handles_object_tuple_and_empty_rows() -> None:
    assert shadow_testing._shadow_pattern_identity_row((7, "Seven")).name == "Seven"
    existing = SimpleNamespace(id=8, name="Eight")
    assert shadow_testing._shadow_pattern_identity_row(existing) is existing
    assert shadow_testing._shadow_pattern_identity_row(None) is None
