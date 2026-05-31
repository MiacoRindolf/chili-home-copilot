from __future__ import annotations

from datetime import datetime

from app.services.trading.management_envelopes import (
    aggregate_management_envelope_execution_for_pattern,
    count_probation_envelopes_since,
    fetch_synergy_retry_envelope_candidates,
    load_autotrader_desk_live_envelope_objects,
    load_audit_export_envelope_rows,
    load_closed_envelope_execution_rows,
    load_closed_pattern_envelope_rows,
    load_closed_review_envelope_rows,
    load_imminent_alert_actioned_envelope_ids,
    load_monitor_decision_envelope_rows,
    load_open_active_setup_envelope_objects,
    load_open_stop_position_envelope_objects,
    load_pattern_tagged_envelope_rows,
    load_recent_ticker_envelope_rows,
    load_stop_decision_envelope_rows,
    load_trades_api_envelope_rows,
    summarize_closed_envelope_performance,
    tca_summary_by_ticker_from_management_envelopes,
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


class _FakeDbSequence:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        return self.results.pop(0)


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


def test_recent_ticker_envelope_rows_read_management_envelopes():
    db = _FakeDb(_RowsResult([{"id": 1, "ticker": "AAPL"}]))

    rows = load_recent_ticker_envelope_rows(
        db,
        user_id=7,
        ticker="aapl",
        limit=3,
    )

    assert rows == [{"id": 1, "ticker": "AAPL"}]
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "UPPER(ticker) = :ticker" in db.sql
    assert "ORDER BY entry_date DESC NULLS LAST, id DESC" in db.sql
    assert db.params == {"uid": 7, "ticker": "AAPL", "limit": 3}


def test_tca_summary_by_ticker_reads_management_envelopes():
    db = _FakeDbSequence(
        _RowsResult([{"ticker": "AAPL", "fills": 2, "avg_entry_slippage_bps": 19.995}]),
        _RowsResult([{"fills": 3, "avg_entry_slippage_bps": 10.004}]),
        _RowsResult([{"ticker": "MSFT", "closes": 1, "avg_exit_slippage_bps": -2.345}]),
        _RowsResult([{"closes": 4, "avg_exit_slippage_bps": 6.789}]),
    )

    summary = tca_summary_by_ticker_from_management_envelopes(
        db,
        user_id=7,
        days=14,
        limit=2,
    )

    assert summary["overall_fills"] == 3
    assert summary["overall_avg_entry_slippage_bps"] == 10.0
    assert summary["by_ticker"] == [
        {"ticker": "AAPL", "fills": 2, "avg_entry_slippage_bps": 20.0}
    ]
    assert summary["exit_overall_closes"] == 4
    assert summary["exit_overall_avg_slippage_bps"] == 6.79
    assert summary["exit_by_ticker"] == [
        {"ticker": "MSFT", "closes": 1, "avg_exit_slippage_bps": -2.35}
    ]
    assert len(db.calls) == 4
    for sql, params in db.calls:
        assert "FROM trading_management_envelopes" in sql
        assert "trading_trades" not in sql
        assert "NOW() - (:days * INTERVAL '1 day')" in sql
        assert params == {"uid": 7, "days": 14, "limit": 2}


def test_execution_robustness_aggregate_reads_management_envelopes():
    db = _FakeDb(
        _RowsResult(
            [
                {
                    "filled_at": datetime(2026, 5, 31, 10, 0),
                    "avg_fill_price": None,
                    "broker_status": "filled",
                    "status": "closed",
                    "tca_entry_slippage_bps": 12,
                    "tca_exit_slippage_bps": "-3",
                    "broker_source": "Coinbase",
                },
                {
                    "filled_at": None,
                    "avg_fill_price": 15.0,
                    "broker_status": "partially_filled",
                    "status": "open",
                    "tca_entry_slippage_bps": None,
                    "tca_exit_slippage_bps": "bad",
                    "broker_source": "coinbase",
                },
                {
                    "filled_at": None,
                    "avg_fill_price": None,
                    "broker_status": None,
                    "status": "cancelled",
                    "tca_entry_slippage_bps": 4.5,
                    "tca_exit_slippage_bps": None,
                    "broker_source": None,
                },
            ]
        )
    )

    stats = aggregate_management_envelope_execution_for_pattern(
        db,
        scan_pattern_id=42,
        user_id=7,
        window_days=30,
    )

    assert stats == {
        "n_orders": 3,
        "n_filled": 2,
        "n_partial": 1,
        "n_miss": 1,
        "slippages_abs_bps": [12.0, 3.0, 4.5],
        "dominant_broker_source": "coinbase",
    }
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "scan_pattern_id = :scan_pattern_id" in db.sql
    assert "entry_date >= :since" in db.sql
    assert db.params["scan_pattern_id"] == 42
    assert db.params["user_id"] == 7


def test_pattern_tagged_envelope_rows_read_management_envelopes():
    db = _FakeDb(_RowsResult([{"id": 1, "pattern_tags": "breakout"}]))

    rows = load_pattern_tagged_envelope_rows(
        db,
        user_id=7,
        limit=5,
    )

    assert rows == [{"id": 1, "pattern_tags": "breakout"}]
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "pattern_tags IS NOT NULL" in db.sql
    assert "ORDER BY entry_date DESC NULLS LAST, id DESC" in db.sql
    assert db.params == {"uid": 7, "limit": 5}


def test_audit_export_envelope_rows_read_management_envelopes():
    start = datetime(2026, 5, 1, 0, 0)
    end = datetime(2026, 5, 30, 23, 59)
    db = _FakeDb(_RowsResult([{"id": 1, "ticker": "ABC"}]))

    rows = load_audit_export_envelope_rows(
        db,
        user_id=7,
        start=start,
        end=end,
    )

    assert rows == [{"id": 1, "ticker": "ABC"}]
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "entry_date >= :start" in db.sql
    assert "entry_date <= :end" in db.sql
    assert "ORDER BY entry_date ASC NULLS LAST" in db.sql
    assert db.params == {"uid": 7, "start": start, "end": end}


def test_trades_api_envelope_rows_read_management_envelopes_with_public_fields():
    db = _FakeDb(_RowsResult([{"id": 9, "ticker": "ABC"}]))

    rows = load_trades_api_envelope_rows(db, user_id=7, status="open", limit=800)

    assert rows == [{"id": 9, "ticker": "ABC"}]
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "broker_order_id" in db.sql
    assert "tca_reference_entry_price" in db.sql
    assert "strategy_proposal_id" in db.sql
    assert "position_id" in db.sql
    assert "AND status = :status" in db.sql
    assert "LIMIT :limit" in db.sql
    assert db.params == {"uid": 7, "limit": 500, "status": "open"}


def test_monitor_decision_envelope_rows_read_management_envelopes():
    db = _FakeDb(
        _RowsResult(
            [
                {
                    "total_count": 2,
                    "id": 10,
                    "trade_id": 20,
                    "ticker": "ABC",
                    "direction": "long",
                }
            ]
        )
    )

    total, rows = load_monitor_decision_envelope_rows(
        db,
        user_id=7,
        action=" hold ",
        limit=50,
        offset=5,
    )

    assert total == 2
    assert rows[0]["ticker"] == "ABC"
    assert "JOIN trading_management_envelopes t ON t.id = d.trade_id" in db.sql
    assert "trading_trades" not in db.sql
    assert "d.action = :action" in db.sql
    assert db.params == {"uid": 7, "action": "hold", "limit": 50, "offset": 5}


def test_monitor_decision_empty_late_page_keeps_total_count():
    db = _FakeDbSequence(_RowsResult([]), _ScalarResult(37))

    total, rows = load_monitor_decision_envelope_rows(
        db,
        user_id=7,
        action=None,
        limit=50,
        offset=100,
    )

    assert total == 37
    assert rows == []
    assert len(db.calls) == 2
    assert "COUNT(*) OVER()::int AS total_count" in db.calls[0][0]
    assert "SELECT COUNT(*)::int AS total_count FROM scoped" in db.calls[1][0]
    assert "JOIN trading_management_envelopes t ON t.id = d.trade_id" in db.calls[1][0]
    assert "trading_trades" not in db.calls[1][0]
    assert db.calls[1][1] == {"uid": 7, "action": None, "limit": 50, "offset": 100}


def test_monitor_decision_empty_first_page_does_not_count_again():
    db = _FakeDbSequence(_RowsResult([]))

    total, rows = load_monitor_decision_envelope_rows(
        db,
        user_id=7,
        action=None,
        limit=50,
        offset=0,
    )

    assert total == 0
    assert rows == []
    assert len(db.calls) == 1


def test_imminent_alert_actioned_envelope_ids_read_management_envelopes():
    db = _FakeDb(
        _RowsResult(
            [
                {"related_alert_id": 11},
                {"related_alert_id": "12"},
                {"related_alert_id": None},
            ]
        )
    )

    ids = load_imminent_alert_actioned_envelope_ids(db, user_id=None)

    assert ids == {11, 12}
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status IN ('open', 'closed')" in db.sql
    assert "user_id IS NOT DISTINCT FROM :uid" in db.sql
    assert db.params == {"uid": None}


def test_stop_decision_envelope_rows_use_lateral_envelope_scope():
    db = _FakeDb(_RowsResult([{"id": 1, "trade_id": 20}]))

    rows = load_stop_decision_envelope_rows(
        db,
        user_id=7,
        trade_id=None,
        limit=50,
    )

    assert rows == [{"id": 1, "trade_id": 20}]
    assert "WITH scoped AS MATERIALIZED" in db.sql
    assert "FROM trading_management_envelopes" in db.sql
    assert "CROSS JOIN LATERAL" in db.sql
    assert "FROM trading_stop_decisions" in db.sql
    assert "trading_trades" not in db.sql
    assert "ORDER BY as_of_ts DESC, id DESC" in db.sql
    assert db.params == {"uid": 7, "limit": 50}


def test_open_stop_position_envelope_objects_return_trade_like_runtime_objects():
    db = _FakeDb(
        _RowsResult(
            [
                {
                    "id": 9,
                    "ticker": "ABC",
                    "entry_price": 10.0,
                    "indicator_snapshot": {"asset_type": "stock"},
                }
            ]
        )
    )

    rows = load_open_stop_position_envelope_objects(db, user_id=7)

    assert rows[0].id == 9
    assert rows[0].ticker == "ABC"
    assert rows[0].indicator_snapshot == {"asset_type": "stock"}
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'open'" in db.sql
    assert "ORDER BY entry_date DESC, id DESC" in db.sql
    assert db.params == {"uid": 7}


def test_open_active_setup_envelope_objects_filter_to_displayable_open_rows():
    db = _FakeDb(
        _RowsResult(
            [
                {
                    "id": 12,
                    "ticker": "XYZ",
                    "entry_price": 4.2,
                    "indicator_snapshot": {"asset_type": "stock"},
                }
            ]
        )
    )

    rows = load_open_active_setup_envelope_objects(db, user_id=7)

    assert rows[0].id == 12
    assert rows[0].ticker == "XYZ"
    assert rows[0].indicator_snapshot == {"asset_type": "stock"}
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'open'" in db.sql
    assert "entry_price > 0" in db.sql
    assert "ORDER BY entry_date DESC, id DESC" in db.sql
    assert db.params == {"uid": 7}


def test_autotrader_desk_live_envelope_objects_use_live_scope_filter():
    db = _FakeDb(
        _RowsResult(
            [
                {
                    "id": 22,
                    "ticker": "DESK",
                    "entry_price": 10.0,
                    "auto_trader_version": "v1",
                }
            ]
        )
    )

    rows = load_autotrader_desk_live_envelope_objects(db, user_id=7)

    assert rows[0].id == 22
    assert rows[0].ticker == "DESK"
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'open'" in db.sql
    assert "auto_trader_version = 'v1'" in db.sql
    assert "scan_pattern_id IS NOT NULL" in db.sql
    assert "related_alert_id IS NOT NULL" in db.sql
    assert "stop_loss IS NOT NULL" in db.sql
    assert "take_profit IS NOT NULL" in db.sql
    assert "ORDER BY id DESC" in db.sql
    assert db.params == {"uid": 7}


def test_stop_decision_envelope_rows_with_trade_id_use_bounded_join():
    db = _FakeDb(_RowsResult([{"id": 2, "trade_id": 123}]))

    rows = load_stop_decision_envelope_rows(
        db,
        user_id=None,
        trade_id=123,
        limit=10,
    )

    assert rows == [{"id": 2, "trade_id": 123}]
    assert "JOIN trading_management_envelopes t ON t.id = d.trade_id" in db.sql
    assert "d.trade_id = :trade_id" in db.sql
    assert "t.user_id IS NOT DISTINCT FROM :uid" in db.sql
    assert "trading_trades" not in db.sql
    assert db.params == {"uid": None, "limit": 10, "trade_id": 123}
