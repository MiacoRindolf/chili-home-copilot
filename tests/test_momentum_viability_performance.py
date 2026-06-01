from __future__ import annotations

from app.services.trading.momentum_neural.viability import _symbol_family_memory_adjust


class _FakeQuery:
    def __init__(self, rows: list[tuple[float | None]]) -> None:
        self.rows = rows

    def join(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[tuple[float | None]]) -> None:
        self.rows = rows

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self.rows)


def test_symbol_family_memory_adjust_counts_return_rows_without_temp_values() -> None:
    db = _FakeSession([(12.0,), (8.0,), (-4.0,), (None,), (16.0,), (6.0,)])

    assert _symbol_family_memory_adjust(db, "BTC-USD", "impulse_breakout") > 0.0


def test_symbol_family_memory_adjust_keeps_negative_memory_penalty() -> None:
    db = _FakeSession([(-12.0,), (-8.0,), (4.0,), (None,)])

    assert _symbol_family_memory_adjust(db, "ETH-USD", "mean_reversion") < 0.0
