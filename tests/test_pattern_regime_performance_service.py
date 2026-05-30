from __future__ import annotations

from datetime import date, datetime

import pytest

from app.services.trading.pattern_regime_performance_service import (
    _build_regime_lookup,
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
    assert "pt.pnl / (pt.entry_price * pt.quantity" in sql
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


class _EmptyRows:
    def fetchall(self):
        return []


class _RecordingDb:
    def __init__(self):
        self.sql = []

    def execute(self, stmt, params):
        self.sql.append(" ".join(str(stmt).split()))
        return _EmptyRows()


def test_regime_lookup_reads_macro_snapshots_via_macro_label():
    db = _RecordingDb()

    _build_regime_lookup(
        db,
        trades=[],
        window_start=date(2026, 5, 1),
        window_end=date(2026, 5, 30),
    )

    macro_sql = next(
        sql for sql in db.sql if "FROM trading_macro_regime_snapshots" in sql
    )
    assert "SELECT as_of_date, macro_label" in macro_sql
    assert "macro_label IS NOT NULL" in macro_sql
    assert "regime_label" not in macro_sql


def test_regime_lookup_uses_live_snapshot_label_columns():
    db = _RecordingDb()

    _build_regime_lookup(
        db,
        trades=[],
        window_start=date(2026, 5, 1),
        window_end=date(2026, 5, 30),
    )

    sql = "\n".join(db.sql)
    assert "SELECT as_of_date, breadth_label" in sql
    assert "SELECT as_of_date, cross_asset_label" in sql
    assert "SELECT as_of_date, session_label" in sql
    assert "breadth_composite_label" not in sql
    assert "composite_label" not in sql


def test_regime_lookup_uses_live_ticker_regime_label_column():
    db = _RecordingDb()

    _build_regime_lookup(
        db,
        trades=[
            SimpleClosedTrade(
                ticker="AAPL",
            )
        ],
        window_start=date(2026, 5, 1),
        window_end=date(2026, 5, 30),
    )

    ticker_sql = next(
        sql for sql in db.sql if "FROM trading_ticker_regime_snapshots" in sql
    )
    assert "SELECT ticker, as_of_date, ticker_regime_label" in ticker_sql
    assert "ticker_regime_label IS NOT NULL" in ticker_sql
    assert "SELECT ticker, as_of_date, regime_label" not in ticker_sql


class SimpleClosedTrade:
    def __init__(self, ticker: str):
        self.ticker = ticker
