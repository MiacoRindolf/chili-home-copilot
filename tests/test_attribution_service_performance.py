from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.models.trading import ScanPattern
from app.services.trading.attribution_service import (
    _scan_patterns_by_id,
    live_vs_research_by_pattern,
)


class _FakeMappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def all(self) -> list[dict[str, object]]:
        return self.rows


class _FakeExecuteResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self.rows)


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
        rows: list[SimpleNamespace],
        execute_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.rows = rows
        self.execute_rows = execute_rows or []
        self.query_calls = 0
        self.execute_calls = 0
        self.last_query: _FakeQuery | None = None
        self.sql = ""
        self.params: dict[str, object] = {}

    def query(self, model: object) -> _FakeQuery:
        assert model is ScanPattern
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query

    def execute(self, stmt: object, params: dict[str, object]) -> _FakeExecuteResult:
        self.execute_calls += 1
        self.sql = str(stmt)
        self.params = params
        return _FakeExecuteResult(self.execute_rows)


def test_scan_patterns_by_id_batches_lookup() -> None:
    first = SimpleNamespace(id=2, name="breakout")
    duplicate = SimpleNamespace(id=2, name="duplicate")
    other = SimpleNamespace(id=5, name="pullback")
    db = _FakeSession([first, duplicate, other])

    result = _scan_patterns_by_id(db, {0, 2, 5})

    assert result == {2: duplicate, 5: other}
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_scan_patterns_by_id_skips_empty_lookup() -> None:
    db = _FakeSession([])

    assert _scan_patterns_by_id(db, {0}) == {}
    assert db.query_calls == 0


def test_live_vs_research_by_pattern_aggregates_trades_in_sql() -> None:
    pattern = SimpleNamespace(
        id=5,
        name="pullback",
        promotion_status="active",
        win_rate=0.61,
        oos_win_rate=0.58,
        oos_avg_return_pct=1.23456,
    )
    db = _FakeSession(
        [pattern],
        execute_rows=[
            {
                "scan_pattern_id": 5,
                "live_closed_trades": 3,
                "live_wins": 2,
                "live_total_pnl": 12.345,
                "live_avg_pnl": 4.115,
                "live_avg_entry_slippage_bps": 1.236,
                "live_avg_exit_slippage_bps": None,
            }
        ],
    )

    out = live_vs_research_by_pattern(
        db,  # type: ignore[arg-type]
        user_id=7,
        days=30,
        limit=10,
    )

    assert db.execute_calls == 1
    assert "FROM trading_trades" in db.sql
    assert "GROUP BY scan_pattern_id" in db.sql
    assert "ORDER BY live_closed_trades DESC" in db.sql
    assert "LIMIT :limit" in db.sql
    assert db.params["user_id"] == 7
    assert isinstance(db.params["since"], datetime)
    assert db.params["limit"] == 10
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1
    assert out["patterns"] == [
        {
            "scan_pattern_id": 5,
            "pattern_name": "pullback",
            "promotion_status": "active",
            "research_win_rate_pct": 61.0,
            "research_oos_win_rate_pct": 58.0,
            "research_oos_avg_return_pct": 1.235,
            "live_closed_trades": 3,
            "live_win_rate_pct": 66.7,
            "live_total_pnl": 12.35,
            "live_avg_pnl": 4.12,
            "live_avg_entry_slippage_bps": 1.24,
            "live_avg_exit_slippage_bps": None,
        }
    ]


def test_live_vs_research_by_pattern_skips_pattern_query_when_no_stats() -> None:
    db = _FakeSession([], execute_rows=[])

    out = live_vs_research_by_pattern(
        db,  # type: ignore[arg-type]
        user_id=7,
    )

    assert db.execute_calls == 1
    assert db.query_calls == 0
    assert out["patterns"] == []
