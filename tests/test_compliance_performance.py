from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.trading import compliance


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


def test_check_pdt_status_reads_day_trade_columns_only() -> None:
    now = datetime(2026, 6, 2, 15, 30)
    db = _FakeSession(
        [
            (now, now + timedelta(hours=2), "AAPL"),
            (now, now + timedelta(hours=3), "BTC-USD"),
            SimpleNamespace(entry_date=now, exit_date=now + timedelta(days=1), ticker="MSFT"),
        ]
    )

    out = compliance.check_pdt_status(db, user_id=7, equity=10_000.0)  # type: ignore[arg-type]

    assert out["same_day_trades_5d"] == 1
    assert out["at_risk"] is False
    assert out["can_day_trade"] is True
    assert db.query_args is not None
    assert tuple(getattr(arg, "key", None) for arg in db.query_args) == (
        "entry_date",
        "exit_date",
        "ticker",
    )
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_pdt_trade_field_handles_object_tuple_and_empty_rows() -> None:
    now = datetime(2026, 6, 2)
    assert compliance._pdt_trade_field((now, None, "AAPL"), "entry_date", 0) == now
    row = SimpleNamespace(ticker="MSFT")
    assert compliance._pdt_trade_field(row, "ticker", 2) == "MSFT"
    assert compliance._pdt_trade_field((), "entry_date", 0) is None
