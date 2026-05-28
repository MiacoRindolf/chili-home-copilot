from __future__ import annotations

from datetime import datetime

from app.models.trading import BrainWorkEvent
from app.services.trading.brain_work.ledger import _last_done_timestamps_by_type


class _FakeQuery:
    def __init__(self, rows: list[tuple[str | None, datetime | None]]) -> None:
        self.rows = rows
        self.filter_calls = 0
        self.group_by_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def group_by(self, *args: object) -> "_FakeQuery":
        self.group_by_calls += 1
        return self

    def all(self) -> list[tuple[str | None, datetime | None]]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[tuple[str | None, datetime | None]]) -> None:
        self.rows = rows
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None

    def query(self, *args: object) -> _FakeQuery:
        assert args[0] is BrainWorkEvent.event_type
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def test_last_done_timestamps_by_type_batches_lookup() -> None:
    done_at = datetime(2026, 5, 28, 13, 0)
    db = _FakeSession(
        [
            ("backtest_requested", done_at),
            ("paper_trade_closed", None),
            (None, done_at),
        ]
    )

    result = _last_done_timestamps_by_type(
        db,  # type: ignore[arg-type]
        ["backtest_requested", "paper_trade_closed"],
    )

    assert result == {"backtest_requested": done_at}
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1
    assert db.last_query.group_by_calls == 1


def test_last_done_timestamps_by_type_skips_empty_lookup() -> None:
    db = _FakeSession([])

    assert _last_done_timestamps_by_type(db, []) == {}  # type: ignore[arg-type]
    assert db.query_calls == 0
