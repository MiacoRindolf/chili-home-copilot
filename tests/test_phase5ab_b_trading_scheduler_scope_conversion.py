from __future__ import annotations

import re
from pathlib import Path

from app.services.trading.management_envelopes import (
    count_scheduler_crypto_stop_envelopes,
    load_scheduler_broker_position_pattern_tickers,
    load_scheduler_broker_position_user_ids,
    load_scheduler_crypto_stop_user_ids,
    load_scheduler_daytrade_fast_user_ids,
    load_scheduler_pattern_monitor_envelope_objects_for_tickers,
    load_scheduler_pattern_position_user_ids,
    load_scheduler_price_monitor_pattern_tickers,
    load_scheduler_price_monitor_user_ids,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


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
        self.calls: list[tuple[str, dict | None]] = []

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        return _RowsResult(self.rows)

    @property
    def sql(self) -> str:
        return self.calls[-1][0]

    @property
    def params(self) -> dict | None:
        return self.calls[-1][1]


def _assert_management_envelope_sql(db: _FakeDb) -> None:
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'open'" in db.sql


def test_scheduler_price_monitor_scopes_read_management_envelopes() -> None:
    db = _FakeDb([{"user_id": 7}])

    user_ids = load_scheduler_price_monitor_user_ids(db)

    assert user_ids == [7]
    _assert_management_envelope_sql(db)
    assert "AND (TRUE)" in db.sql

    db = _FakeDb([{"ticker": "abc"}, {"ticker": "XYZ-USD"}])
    tickers = load_scheduler_price_monitor_pattern_tickers(db)

    assert tickers == ["ABC", "XYZ-USD"]
    _assert_management_envelope_sql(db)
    assert "related_alert_id IS NOT NULL" in db.sql


def test_scheduler_broker_position_scopes_bind_sources() -> None:
    db = _FakeDb([{"user_id": 9}])

    user_ids = load_scheduler_broker_position_user_ids(
        db,
        broker_sources={"robinhood", "coinbase"},
    )

    assert user_ids == [9]
    _assert_management_envelope_sql(db)
    assert "LOWER(COALESCE(broker_source, '')) IN (:src_0, :src_1)" in db.sql
    assert db.params == {"src_0": "coinbase", "src_1": "robinhood"}

    db = _FakeDb([{"ticker": "ray-usd"}])
    tickers = load_scheduler_broker_position_pattern_tickers(
        db,
        broker_sources={"robinhood"},
    )

    assert tickers == ["RAY-USD"]
    _assert_management_envelope_sql(db)
    assert db.params == {"src_0": "robinhood"}


def test_scheduler_daytrade_crypto_and_pattern_scopes_read_envelopes() -> None:
    db = _FakeDb([{"user_id": 1}, {"user_id": 2}])

    daytrade_users = load_scheduler_daytrade_fast_user_ids(
        db,
        trade_types=("scalp", "daytrade"),
    )

    assert daytrade_users == [1, 2]
    _assert_management_envelope_sql(db)
    assert "trade_type IN (:trade_type_0, :trade_type_1)" in db.sql

    db = _FakeDb([{"user_id": 3}])
    crypto_users = load_scheduler_crypto_stop_user_ids(db)

    assert crypto_users == [3]
    _assert_management_envelope_sql(db)
    assert "ticker LIKE '%-USD'" in db.sql

    db = _FakeDb([{"n": 4}])
    assert count_scheduler_crypto_stop_envelopes(db, user_id=3) == 4
    _assert_management_envelope_sql(db)
    assert db.params == {"uid": 3}

    db = _FakeDb([{"user_id": 5}])
    pattern_users = load_scheduler_pattern_position_user_ids(db)

    assert pattern_users == [5]
    _assert_management_envelope_sql(db)
    assert "related_alert_id IS NOT NULL" in db.sql
    assert "stop_loss IS NOT NULL OR take_profit IS NOT NULL" in db.sql


def test_scheduler_pattern_monitor_handoff_loads_envelope_objects_for_tickers() -> None:
    db = _FakeDb(
        [
            {
                "id": 10,
                "ticker": "abc",
                "status": "open",
                "related_alert_id": 99,
            }
        ]
    )

    rows = load_scheduler_pattern_monitor_envelope_objects_for_tickers(
        db,
        tickers=["abc", "ABC", "", "xyz-usd"],
    )

    assert len(rows) == 1
    assert rows[0].id == 10
    assert rows[0].ticker == "abc"
    _assert_management_envelope_sql(db)
    assert "UPPER(ticker) IN (:ticker_0, :ticker_1)" in db.sql
    assert "related_alert_id IS NOT NULL" in db.sql
    assert "stop_loss IS NOT NULL OR take_profit IS NOT NULL" in db.sql
    assert db.params == {"ticker_0": "ABC", "ticker_1": "XYZ-USD"}


def test_trading_scheduler_selection_jobs_use_envelope_helpers() -> None:
    source = (REPO_ROOT / "app" / "services" / "trading_scheduler.py").read_text()
    converted_functions = {
        "_run_price_monitor_job": "load_scheduler_price_monitor_user_ids",
        "_run_broker_position_price_monitor_job": "load_scheduler_broker_position_user_ids",
        "_run_daytrade_fast_monitor_job": "load_scheduler_daytrade_fast_user_ids",
        "_run_stop_alert_dispatch_job": "load_scheduler_crypto_stop_user_ids",
        "_run_pattern_position_monitor_job": "load_scheduler_pattern_position_user_ids",
    }

    for function_name, helper_name in converted_functions.items():
        pattern = rf"def {function_name}\(\):(?P<body>.*?)(?=\ndef _run_|\ndef trigger_|\Z)"
        match = re.search(pattern, source, flags=re.S)
        assert match is not None
        body = match.group("body")
        assert helper_name in body
        assert "db.query(distinct(Trade.user_id))" not in body

    trigger_match = re.search(
        r"def trigger_pattern_monitor_for_tickers\(.*?(?=\ndef _run_|\Z)",
        source,
        flags=re.S,
    )
    assert trigger_match is not None
    trigger_body = trigger_match.group(0)
    assert "load_scheduler_pattern_monitor_envelope_objects_for_tickers" in trigger_body
    assert "db.query(Trade)" not in trigger_body
