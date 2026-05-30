from __future__ import annotations

import pytest

from app.services.trading.recert_queue_service import _aggregate_oos_backtest_evidence


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _Session:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = {}

    def execute(self, stmt, params=None):
        self.sql = str(stmt)
        self.params = dict(params or {})
        return _Result(self.row)


def test_oos_recert_average_return_ignores_missing_return_rows() -> None:
    db = _Session(
        {
            "backtests_run": 3,
            "total": 15,
            "wins_float": 9.0,
            "avg_return": 2.5,
        }
    )

    out = _aggregate_oos_backtest_evidence(db, scan_pattern_id=77)

    assert out["backtests_run"] == 3
    assert out["total"] == 15
    assert out["wins"] == 9
    assert out["win_rate"] == pytest.approx(0.6)
    assert out["avg_return"] == pytest.approx(2.5)
    assert "COALESCE(oos_return_pct, 0.0)" not in db.sql
    assert "FILTER" in db.sql
    assert "oos_return_pct IS NOT NULL" in db.sql
    assert db.params == {"pid": 77}
