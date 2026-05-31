from __future__ import annotations

from app.services.trading.realized_stats_sync import sync_realized_stats


class _RowsResult:
    def fetchall(self):
        return []


class _ScalarResult:
    def scalar(self):
        return 0


class _Session:
    def __init__(self):
        self.sqls: list[str] = []
        self.params: list[dict] = []
        self.committed = False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.sqls.append(sql)
        self.params.append(dict(params or {}))
        if "WITH realized_source AS" in sql:
            return _RowsResult()
        return _ScalarResult()

    def commit(self):
        self.committed = True


def test_realized_stats_sync_live_source_uses_realized_notional_only() -> None:
    sess = _Session()

    out = sync_realized_stats(sess, dry_run=True)

    assert out["updated"] == 0
    assert sess.committed is False
    realized_sql = sess.sqls[0]
    live_source_sql = realized_sql.split("UNION ALL", 1)[0]
    assert "FROM trading_management_envelopes" in live_source_sql
    assert "scan_pattern_id != -1" in live_source_sql
    assert "pnl IS NOT NULL" in live_source_sql
    assert "entry_price > 0" in live_source_sql
    assert "quantity > 0" in live_source_sql
    assert "avg((pnl / (entry_price * quantity * contract_multiplier)) * 100.0)" in realized_sql
    assert "exit_price - entry_price" not in realized_sql
    assert "entry_price - exit_price" not in realized_sql

    no_trades_sql = sess.sqls[-1]
    assert "scan_pattern_id != -1" in no_trades_sql
    assert "pnl IS NOT NULL" in no_trades_sql
    assert "entry_price > 0" in no_trades_sql
    assert "quantity > 0" in no_trades_sql
