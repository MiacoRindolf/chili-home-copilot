from __future__ import annotations

from pathlib import Path

from app.services.trading import management_envelopes
from app.services.trading.management_envelopes import MANAGEMENT_ENVELOPES_RELATION


class _Result:
    def mappings(self):
        return self

    def all(self):
        return []


class _Db:
    def __init__(self):
        self.sql: list[str] = []
        self.params: list[dict] = []

    def execute(self, sql, params=None):
        self.sql.append(str(sql))
        self.params.append(dict(params or {}))
        return _Result()


def test_bracket_reconciliation_scope_uses_management_envelopes_relation():
    db = _Db()

    rows = management_envelopes.load_bracket_reconciliation_scope(db, user_id=42)

    assert rows == []
    sql = db.sql[-1]
    assert f"FROM {MANAGEMENT_ENVELOPES_RELATION} AS t" in sql
    assert "trading_bracket_intents" in sql
    assert "FROM trading_trades" not in sql
    assert "JOIN trading_trades" not in sql
    assert db.params[-1] == {"uid": 42}


def test_bracket_watchdog_candidates_use_management_envelopes_relation():
    db = _Db()

    rows = management_envelopes.load_stale_bracket_watchdog_candidates(
        db,
        user_id=None,
        stale_after_sec=300,
    )

    assert rows == []
    sql = db.sql[-1]
    assert f"FROM {MANAGEMENT_ENVELOPES_RELATION} AS t" in sql
    assert "JOIN trading_bracket_intents AS bi" in sql
    assert "FROM trading_trades" not in sql
    assert "JOIN trading_trades" not in sql
    assert db.params[-1] == {"stale_sec": 300}


def test_bracket_reconciler_no_longer_has_raw_trading_trades_reader():
    src = Path("app/services/trading/bracket_reconciliation_service.py").read_text(
        encoding="utf-8"
    )

    assert "FROM trading_trades AS t" not in src
    assert "JOIN trading_trades AS t" not in src
