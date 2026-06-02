from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.momentum_neural.viability import (
    _memory_adjust_from_stats,
    _memory_stats_from_return_rows,
    _memory_stats_from_row,
    _symbol_family_memory_adjust,
)


class _FakeColumn:
    def __gt__(self, _other):
        return True


class _FakeSubquery:
    c = SimpleNamespace(return_bps=_FakeColumn())


class _FakeQuery:
    def __init__(self, result) -> None:
        self.result = result

    def join(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def subquery(self):
        return _FakeSubquery()

    def one_or_none(self):
        return self.result

    def all(self):
        return self.result


class _FakeSession:
    def __init__(self, rows: list[tuple[float | None]]) -> None:
        self.rows = rows
        self.latest_subquery_count = 0
        self.aggregate_query_count = 0

    def query(self, *_args, **_kwargs):
        if len(_args) == 1:
            self.latest_subquery_count += 1
            return _FakeQuery(self.rows)
        self.aggregate_query_count += 1
        n, wins = _memory_stats_from_return_rows(self.rows)
        return _FakeQuery(SimpleNamespace(n=n, wins=wins))


def test_symbol_family_memory_adjust_counts_return_rows_without_temp_values() -> None:
    db = _FakeSession([(12.0,), (8.0,), (-4.0,), (None,), (16.0,), (6.0,)])

    assert _symbol_family_memory_adjust(db, "BTC-USD", "impulse_breakout") > 0.0
    assert db.latest_subquery_count == 1
    assert db.aggregate_query_count == 1


def test_symbol_family_memory_adjust_keeps_negative_memory_penalty() -> None:
    db = _FakeSession([(-12.0,), (-8.0,), (4.0,), (None,)])

    assert _symbol_family_memory_adjust(db, "ETH-USD", "mean_reversion") < 0.0


def test_memory_stats_from_row_handles_tuple_object_and_empty() -> None:
    assert _memory_stats_from_row((5, 4)) == (5, 4)
    assert _memory_stats_from_row(SimpleNamespace(n=3, wins=1)) == (3, 1)
    assert _memory_stats_from_row(None) == (0, 0)


def test_memory_adjust_from_stats_preserves_thresholds() -> None:
    assert _memory_adjust_from_stats(n=2, wins=2) == 0.0
    assert _memory_adjust_from_stats(n=5, wins=4) > 0.0
    assert _memory_adjust_from_stats(n=4, wins=1) < 0.0
