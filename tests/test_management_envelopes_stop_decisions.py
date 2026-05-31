from __future__ import annotations

import inspect
from datetime import datetime, timezone

from app.routers import trading
from app.services.trading.management_envelopes import load_stop_decision_envelope_rows


class _MappingResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _CaptureDb:
    def __init__(self, rows=None):
        self.sql = ""
        self.params = None
        self.rows = rows or []

    def execute(self, stmt, params=None):
        self.sql = str(stmt)
        self.params = dict(params or {})
        return _MappingResult(self.rows)


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def test_stop_decision_rows_use_lateral_envelope_read_path():
    db = _CaptureDb(rows=[{"id": 1, "trade_id": 11, "state": "armed"}])

    rows = load_stop_decision_envelope_rows(
        db,
        user_id=7,
        trade_id=None,
        limit=500,
    )

    sql = _compact(db.sql)
    assert rows == [{"id": 1, "trade_id": 11, "state": "armed"}]
    assert db.params == {"uid": 7, "limit": 200}
    assert "trading_management_envelopes" in sql
    assert "trading_trades" not in sql
    assert "WITH scoped AS MATERIALIZED" in sql
    assert "CROSS JOIN LATERAL" in sql
    assert "FROM trading_stop_decisions" in sql
    assert "ORDER BY as_of_ts DESC NULLS LAST, id DESC" in sql


def test_stop_decision_rows_trade_filter_uses_single_trade_path():
    db = _CaptureDb()

    load_stop_decision_envelope_rows(
        db,
        user_id=7,
        trade_id=42,
        limit=25,
    )

    sql = _compact(db.sql)
    assert db.params == {"uid": 7, "limit": 25, "trade_id": 42}
    assert "JOIN trading_management_envelopes t ON t.id = d.trade_id" in sql
    assert "CROSS JOIN LATERAL" not in sql
    assert "t.user_id IS NOT DISTINCT FROM :uid" in sql
    assert "d.trade_id = :trade_id" in sql
    assert "ORDER BY d.as_of_ts DESC NULLS LAST, d.id DESC" in sql
    assert "trading_trades" not in sql


def test_stop_decision_rows_default_limit_is_fifty():
    db = _CaptureDb()

    load_stop_decision_envelope_rows(
        db,
        user_id=7,
        trade_id=None,
        limit=0,
    )

    assert db.params == {"uid": 7, "limit": 50}


def test_stop_decision_rows_invalid_helper_limit_clamps_to_one():
    db = _CaptureDb()

    load_stop_decision_envelope_rows(
        db,
        user_id=7,
        trade_id=42,
        limit=-5,
    )

    assert db.params == {"uid": 7, "limit": 1, "trade_id": 42}


def test_stop_decisions_route_uses_envelope_helper_not_trade_join():
    source = inspect.getsource(trading.api_stop_decisions)

    assert "load_stop_decision_envelope_rows" in source
    assert "StopDecision" not in source
    assert ".join(" not in source
    assert '"decisions": _stop_decision_rows(decisions)' in source


def test_stop_decision_rows_preserve_public_contract():
    as_of = datetime(2026, 5, 30, 12, 1, tzinfo=timezone.utc)

    rows = trading._stop_decision_rows(
        [
            {
                "id": 9,
                "trade_id": 22,
                "as_of_ts": as_of,
                "state": "tightened",
                "old_stop": 101.0,
                "new_stop": 103.0,
                "trigger": "trail",
                "reason": "r-multiple",
                "executed": True,
            }
        ]
    )

    assert rows == [
        {
            "id": 9,
            "trade_id": 22,
            "as_of_ts": "2026-05-30T12:01:00+00:00",
            "state": "tightened",
            "old_stop": 101.0,
            "new_stop": 103.0,
            "trigger": "trail",
            "reason": "r-multiple",
            "executed": True,
        }
    ]
