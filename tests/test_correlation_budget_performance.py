from __future__ import annotations

import pytest

from app.services.trading import correlation_budget


class _FakeQuery:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
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


def _option_tuple() -> tuple[object, ...]:
    return (
        "PHHCB_OPT",
        2.0,
        1.25,
        "option",
        "",
        {"option_meta": {"strike": 100.0}},
    )


def _assert_budget_trade_query_shape(db: _FakeSession) -> None:
    assert db.query_args is not None
    assert tuple(getattr(arg, "key", None) for arg in db.query_args) == (
        "ticker",
        "quantity",
        "entry_price",
        "asset_kind",
        "tags",
        "indicator_snapshot",
    )
    assert db.last_query is not None


def test_compute_correlation_budget_reads_compact_trade_notional_fields() -> None:
    db = _FakeSession(
        [
            _option_tuple(),
            ("ZHHCB_OTHER", 10.0, 100.0, None, "", {}),
        ]
    )

    out = correlation_budget.compute_correlation_budget(
        db,  # type: ignore[arg-type]
        user_id=5,
        ticker="PHHCB_NEW",
        capital=100_000.0,
        asset_class="equity",
    )

    assert out.open_notional == pytest.approx(250.0)
    _assert_budget_trade_query_shape(db)
    assert db.last_query.filter_calls == 3


def test_compute_portfolio_budget_reads_compact_trade_notional_fields() -> None:
    db = _FakeSession(
        [
            _option_tuple(),
            ("PHHCB_OTHER", 3.0, 10.0, None, "", {}),
        ]
    )

    out = correlation_budget.compute_portfolio_budget(
        db,  # type: ignore[arg-type]
        user_id=5,
        ticker="PHHCB_OPT",
        capital=100_000.0,
    )

    assert out.deployed_notional == pytest.approx(280.0)
    assert out.ticker_open_notional == pytest.approx(250.0)
    _assert_budget_trade_query_shape(db)
    assert db.last_query.filter_calls == 2


def test_trade_budget_row_handles_object_tuple_and_empty_rows() -> None:
    row = correlation_budget._trade_budget_row(_option_tuple())
    assert row.ticker == "PHHCB_OPT"
    assert row.quantity == 2.0
    assert correlation_budget._trade_budget_field((), "ticker", 0) is None
