from __future__ import annotations

from datetime import datetime

from app.services.trading.management_envelopes import (
    count_probation_envelopes_since,
    fetch_synergy_retry_envelope_candidates,
    load_closed_envelope_execution_rows,
    load_closed_pattern_envelope_rows,
    load_closed_review_envelope_rows,
    summarize_closed_envelope_performance,
)


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeDb:
    def __init__(self, result):
        self.result = result
        self.sql = ""
        self.params = None

    def execute(self, sql, params=None):
        self.sql = str(sql)
        self.params = params
        return self.result


def test_synergy_retry_candidates_read_management_envelopes_not_trade_view():
    db = _FakeDb(
        _RowsResult(
            [{"alert_id": 11, "source_run_id": 22, "retry_pool": 3}]
        )
    )

    rows = fetch_synergy_retry_envelope_candidates(
        db,
        uid=7,
        lookback_minutes=45,
        source_reason="synergy_not_applicable",
        autotrader_version="v1",
        query_limit=5,
    )

    assert rows == [{"alert_id": 11, "source_run_id": 22, "retry_pool": 3}]
    assert "FROM trading_management_envelopes t" in db.sql
    assert "trading_trades" not in db.sql
    assert db.params == {
        "uid": 7,
        "lookback_minutes": 45,
        "source_reason": "synergy_not_applicable",
        "autotrader_version": "v1",
        "query_limit": 5,
    }


def test_count_probation_envelopes_reads_management_envelopes_with_pattern_clause():
    start_utc = datetime(2026, 5, 30, 13, 30)
    db = _FakeDb(_ScalarResult(4))

    count = count_probation_envelopes_since(
        db,
        uid=7,
        autotrader_version="v1",
        start_utc=start_utc,
        entry_execution_key="entry_execution",
        probation_flag_key="probation_recert_allowed",
        probation_true_flag="true",
        probation_false_flag="false",
        pattern_id=99,
    )

    assert count == 4
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "AND scan_pattern_id = :pattern_id" in db.sql
    assert "jsonb_extract_path_text" in db.sql
    assert db.params == {
        "uid": 7,
        "version": "v1",
        "start_utc": start_utc,
        "flag": "true",
        "entry_execution_key": "entry_execution",
        "probation_flag_key": "probation_recert_allowed",
        "false_flag": "false",
        "pattern_id": 99,
    }


def test_closed_envelope_performance_summary_reads_management_envelopes():
    since = datetime(2026, 5, 30, 15, 0)
    db = _FakeDb(_RowsResult([{"trades": 4, "wins": 3, "pnl": 12.345}]))

    summary = summarize_closed_envelope_performance(db, user_id=7, since=since)

    assert summary.to_payload() == {"trades": 4, "pnl": 12.35, "win_rate": 75.0}
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'closed'" in db.sql
    assert "exit_date >= :since" in db.sql
    assert db.params == {"uid": 7, "since": since}


def test_closed_envelope_execution_rows_read_management_envelopes():
    since = datetime(2026, 5, 30, 15, 0)
    db = _FakeDb(
        _RowsResult(
            [
                {
                    "id": 1,
                    "ticker": "ABC",
                    "entry_price": 10.0,
                    "indicator_snapshot": {"signal_price": 9.9},
                    "tags": None,
                    "tca_entry_slippage_bps": 12,
                    "tca_exit_slippage_bps": 8,
                }
            ]
        )
    )

    rows = load_closed_envelope_execution_rows(db, user_id=7, since=since)

    assert rows[0]["ticker"] == "ABC"
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'closed'" in db.sql
    assert "entry_date >= :since" in db.sql
    assert db.params == {"uid": 7, "since": since}


def test_closed_pattern_envelope_rows_read_management_envelopes():
    since = datetime(2026, 5, 30, 15, 0)
    db = _FakeDb(_RowsResult([{"id": 9, "ticker": "ABC"}]))

    rows = load_closed_pattern_envelope_rows(
        db,
        pattern_id=42,
        user_id=7,
        since=since,
    )

    assert rows == [{"id": 9, "ticker": "ABC"}]
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "scan_pattern_id = :pattern_id" in db.sql
    assert "ORDER BY exit_date ASC" in db.sql
    assert db.params == {"pattern_id": 42, "since": since, "uid": 7}


def test_closed_review_envelope_rows_read_management_envelopes():
    since = datetime(2026, 5, 30, 15, 0)
    db = _FakeDb(_RowsResult([{"id": 9, "ticker": "ABC"}]))

    rows = load_closed_review_envelope_rows(db, user_id=7, since=since)

    assert rows == [{"id": 9, "ticker": "ABC"}]
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'closed'" in db.sql
    assert "ORDER BY exit_date ASC" in db.sql
    assert db.params == {"uid": 7, "since": since}
