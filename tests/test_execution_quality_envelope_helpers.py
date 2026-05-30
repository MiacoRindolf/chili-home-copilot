from __future__ import annotations

from app.services.trading import execution_quality


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = None

    def execute(self, sql, params=None):
        self.sql = str(sql)
        self.params = params
        return _RowsResult(self.rows)


def test_execution_stats_read_closed_management_envelopes():
    db = _FakeDb(
        [
            {
                "ticker": "ABC",
                "entry_price": 11.0,
                "indicator_snapshot": {"signal_price": 10.0},
                "tags": None,
                "tca_entry_slippage_bps": 5,
                "tca_exit_slippage_bps": 3,
            }
        ]
    )

    stats = execution_quality.compute_execution_stats(db, user_id=7, lookback_days=30)

    assert stats["trades_analyzed"] == 1
    assert stats["measurable"] == 1
    assert stats["avg_slippage_pct"] == 10.0
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert db.params["uid"] == 7


def test_implementation_shortfall_reads_closed_management_envelopes():
    db = _FakeDb(
        [
            {
                "ticker": "ABC",
                "entry_price": 11.0,
                "indicator_snapshot": {"signal_price": 10.0},
                "tags": None,
                "tca_entry_slippage_bps": 5,
                "tca_exit_slippage_bps": 3,
            }
        ]
    )

    stats = execution_quality.compute_implementation_shortfall(
        db,
        user_id=7,
        lookback_days=30,
    )

    assert stats["trades_analyzed"] == 1
    assert stats["measurable"] == 1
    assert stats["mean_delay_bps"] == 1000.0
    assert stats["mean_spread_bps"] == 8.0
    assert stats["mean_total_is_bps"] == 1008.0
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert db.params["uid"] == 7
