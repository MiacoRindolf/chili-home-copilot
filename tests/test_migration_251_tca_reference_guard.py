from app import migrations


class _Result:
    rowcount = 0


class _FakeConn:
    def __init__(self):
        self.executed_sql = []
        self.commit_count = 0
        self.rollback_count = 0

    def execute(self, statement, *_args, **_kwargs):
        self.executed_sql.append(str(statement))
        return _Result()

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


def test_migration_251_skips_trading_alerts_entry_price_when_absent(monkeypatch):
    def columns(_conn, table):
        if table == "trading_alerts":
            return {"id"}
        if table == "trading_trades":
            return {
                "entry_price",
                "avg_fill_price",
                "related_alert_id",
                "tca_entry_slippage_bps",
                "tca_reference_entry_price",
            }
        return set()

    monkeypatch.setattr(migrations, "_tables", lambda _conn: {"trading_alerts", "trading_trades"})
    monkeypatch.setattr(migrations, "_columns", columns)
    conn = _FakeConn()

    migrations._migration_251_tca_reference_entry_backfill(conn)

    assert not any("FROM trading_alerts a" in sql for sql in conn.executed_sql)
    assert any("SET tca_entry_slippage_bps" in sql for sql in conn.executed_sql)
    assert conn.rollback_count == 0
