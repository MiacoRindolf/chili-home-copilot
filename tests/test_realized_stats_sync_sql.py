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
    paper_source_sql = realized_sql.split("UNION ALL", 1)[1]
    assert "FROM trading_trades" in live_source_sql
    assert "scan_pattern_id != -1" in live_source_sql
    assert "pnl IS NOT NULL" in live_source_sql
    assert "entry_price > 0" in live_source_sql
    assert "quantity > 0" in live_source_sql
    assert "realized_return_frac" in realized_sql
    assert "avg(realized_return_frac * 100.0)" in realized_sql
    assert "count(realized_return_frac) AS n" in realized_sql
    assert "CASE WHEN realized_return_frac > 0 THEN 1 ELSE 0 END" in realized_sql
    assert "WHEN realized_return_frac > 0" in realized_sql
    assert "WHEN realized_return_frac < 0" in realized_sql
    assert "HAVING count(realized_return_frac) >= :min_n" in realized_sql
    assert "partial_taken_qty" in realized_sql
    assert "partial_taken_price" in realized_sql
    assert "filled_quantity" in realized_sql
    assert "entry_price * quantity * contract_multiplier" not in realized_sql
    assert "WHEN pnl > 0" not in realized_sql
    assert "WHEN pnl < 0" not in realized_sql
    assert "exit_price - entry_price" not in realized_sql
    assert "entry_price - exit_price" not in realized_sql
    assert "shadow_capacity_janitor" not in live_source_sql
    assert "shadow_capacity_janitor" in paper_source_sql

    no_trades_sql = sess.sqls[-1]
    assert "scan_pattern_id != -1" in no_trades_sql
    assert "pnl IS NOT NULL" in no_trades_sql
    assert "entry_price > 0" in no_trades_sql
    assert "quantity > 0" in no_trades_sql
    assert "FROM scan_patterns sp" in no_trades_sql
    assert "FROM trading_trades t" in no_trades_sql
    assert "FROM trading_paper_trades pt" in no_trades_sql
    assert "t.filled_quantity" in no_trades_sql
    assert "t.partial_taken_qty" in no_trades_sql
    assert "pt.partial_taken_qty" in no_trades_sql
    assert "COALESCE(pt.signal_json" in no_trades_sql
    assert "shadow_capacity_janitor" in no_trades_sql
    assert ") IS NOT NULL" in no_trades_sql
