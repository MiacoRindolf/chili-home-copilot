from __future__ import annotations

from datetime import date, datetime

import pytest

from app.services.trading.pattern_regime_performance_service import (
    _fetch_closed_trades,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = {}

    def execute(self, stmt, params):
        self.sql = str(stmt)
        self.params = dict(params)
        return _Rows(self.rows)


def test_fetch_closed_trades_uses_contract_aware_realized_fraction():
    db = _FakeDb(
        [
            (
                42,
                "XYZ",
                datetime(2026, 5, 18, 14, 30),
                datetime(2026, 5, 20, 14, 30),
                0.16,
            )
        ]
    )

    trades = _fetch_closed_trades(
        db,
        as_of_date=date(2026, 5, 28),
        window_days=10,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.pattern_id == 42
    assert trade.ticker == "XYZ"
    assert trade.entry_date == date(2026, 5, 18)
    assert trade.exit_date == date(2026, 5, 20)
    assert trade.pnl_pct == pytest.approx(0.16)
    assert trade.hold_days == pytest.approx(2.0)

    sql = " ".join(db.sql.split())
    assert "realized_return_frac" in sql
    assert "pt.partial_taken_qty" in sql
    assert "pt.partial_taken_price" in sql
    assert "pt.quantity + pt.partial_taken_qty" in sql
    assert "pt.signal_json" in sql
    assert "100.0" in sql
    assert "pt.scan_pattern_id != -1" in sql
    assert "pt.pnl IS NOT NULL" in sql
    assert "pt.entry_price > 0" in sql
    assert "pt.quantity > 0" in sql
    assert "pnl_pct" not in sql
    assert db.params == {
        "start": date(2026, 5, 18),
        "end": date(2026, 5, 27),
    }
