"""The corrected-EV writer + the recompute migration must exclude DIRTY exits
(reconcile / sync-gone / position-gone placeholders) from realized-EV evidence —
the same cleanliness the raw-stats writer already applies. Pattern 1246's false
'-0.14% net-loser' block came from 7 position_sync_gone rows whose real broker
fills were all positive.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models.core import User
from app.models.trading import ScanPattern, Trade


def _user(db, name):
    u = User(name=name)
    db.add(u)
    db.flush()
    return u.id


def _pattern(db, name, **stats):
    p = ScanPattern(name=name, timeframe="1d", rules_json={}, origin="mined", active=True)
    for k, v in stats.items():
        setattr(p, k, v)
    db.add(p)
    db.flush()
    return p


def _trade(uid, pid, *, exit_reason, entry, exit_, pnl, days_ago=2):
    return Trade(
        user_id=uid,
        scan_pattern_id=pid,
        ticker="BTC-USD",
        status="closed",
        direction="long",
        entry_price=entry,
        exit_price=exit_,
        quantity=1.0,
        pnl=pnl,
        entry_date=datetime.utcnow() - timedelta(days=days_ago),
        exit_date=datetime.utcnow() - timedelta(days=days_ago - 1),
        exit_reason=exit_reason,
        asset_kind="crypto",
    )


def test_migration_300_sql_nulls_corrected_on_dirty_exits(monkeypatch):
    # Canonical migration-SQL test (mirrors test_migration_280): the recompute must
    # NULL corrected_*/raw_realized_* for patterns whose closed trades carry a dirty
    # reconcile/sync-gone/position-gone exit, and bump updated_at.
    from app import migrations

    monkeypatch.setattr(migrations, "_tables", lambda _c: {"scan_patterns", "trading_trades"})

    class _R:
        rowcount = 0

    class _FakeConn:
        def __init__(self):
            self.sql = []

        def execute(self, stmt, *a, **k):
            self.sql.append(str(stmt))
            return _R()

        def commit(self):
            pass

    conn = _FakeConn()
    migrations._migration_300_realized_ev_clean_window_recompute(conn)
    sql = "\n".join(conn.sql)
    assert "UPDATE scan_patterns" in sql
    assert "corrected_avg_return_pct = NULL" in sql
    assert "corrected_trade_count = NULL" in sql
    assert "raw_realized_avg_return_pct = NULL" in sql
    assert "updated_at = NOW()" in sql
    # dirty-exit filter (matches clean_live_pattern_ev_exit_filter tokens)
    assert "LIKE '%sync_gone%'" in sql
    assert "LIKE '%reconcile%'" in sql
    assert "LIKE '%position_gone%'" in sql
    assert "sync_duplicate" in sql


def test_writer_excludes_dirty_exits_from_corrected_stats(db):
    uid = _user(db, "writer")
    p = _pattern(db, "writerpat", lifecycle_stage="promoted")
    # one CLEAN losing trade + one DIRTY 'winning' trade
    db.add(_trade(uid, p.id, exit_reason="stop_loss_hit", entry=100, exit_=95, pnl=-5))
    db.add(_trade(uid, p.id, exit_reason="coinbase_position_sync_gone", entry=100, exit_=110, pnl=10))
    db.commit()

    from app.services.trading.learning import update_pattern_stats_from_closed_trades
    update_pattern_stats_from_closed_trades(db, user_id=uid)
    db.expire_all()

    p2 = db.get(ScanPattern, p.id)
    # only the clean (losing) trade is counted; the dirty sync-gone 'win' is excluded
    assert p2.corrected_trade_count == 1
    assert (p2.corrected_win_rate or 0.0) == 0.0
