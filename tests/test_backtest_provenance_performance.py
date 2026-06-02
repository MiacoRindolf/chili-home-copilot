from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import BacktestResult, ScanPattern
from app.services.trading import backtest_provenance


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, *, backtest_rows, pattern_rows):
        self.backtest_rows = backtest_rows
        self.pattern_rows = pattern_rows
        self.query_calls = []
        self.get_calls = []
        self.commits = 0

    def query(self, *args):
        self.query_calls.append(args)
        if len(args) == 1 and args[0] is BacktestResult:
            return _FakeQuery(self.backtest_rows)
        keys = tuple(getattr(arg, "key", None) for arg in args)
        if keys == backtest_provenance._BACKTEST_REPAIR_FIELDS:
            return _FakeQuery(self.backtest_rows)
        if keys == ("id", "name"):
            return _FakeQuery(self.pattern_rows)
        raise AssertionError(f"unexpected query shape: {keys!r}")

    def get(self, model, row_id):
        self.get_calls.append((model, row_id))
        return None

    def commit(self):
        self.commits += 1


def _complete_params(scan_pattern_id: int) -> dict:
    return {
        "scan_pattern_id": scan_pattern_id,
        "period": "1y",
        "interval": "1d",
        "ohlc_bars": 100,
        "chart_time_from": "2026-01-01",
        "chart_time_to": "2026-02-01",
    }


def test_repair_backtest_provenance_dry_run_uses_column_rows_and_bulk_patterns() -> None:
    db = _FakeDb(
        backtest_rows=[
            ("wrong name", _complete_params(10), 10),
            ("Breakout", _complete_params(10), 10),
            ("mean reversion", _complete_params(11), 11),
        ],
        pattern_rows=[(10, "Breakout"), (11, "Mean Reversion")],
    )

    result = backtest_provenance.repair_backtest_provenance(db, apply=False)

    query_keys = [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls]
    assert query_keys == [
        backtest_provenance._BACKTEST_REPAIR_FIELDS,
        ("id", "name"),
    ]
    assert db.get_calls == []
    assert db.commits == 0
    assert result["rows_scanned"] == 3
    assert result["strategy_fixed"] == 2
    assert result["provenance_complete"] == 3


def test_repair_backtest_provenance_apply_bulk_patterns_without_get() -> None:
    row = SimpleNamespace(
        strategy_name="wrong name",
        params=_complete_params(10),
        scan_pattern_id=10,
    )
    db = _FakeDb(
        backtest_rows=[row],
        pattern_rows=[SimpleNamespace(id=10, name="Breakout")],
    )

    result = backtest_provenance.repair_backtest_provenance(db, apply=True)

    assert len(db.query_calls) == 2
    assert len(db.query_calls[0]) == 1
    assert db.query_calls[0][0] is BacktestResult
    assert tuple(getattr(arg, "key", None) for arg in db.query_calls[1]) == ("id", "name")
    assert db.get_calls == []
    assert db.commits == 1
    assert row.strategy_name == "Breakout"
    assert row.params["data_provenance"]["scan_pattern_id"] == 10
    assert result["applied"] is True
    assert result["strategy_fixed"] == 1
