from __future__ import annotations

from types import SimpleNamespace

from app.services.trading import pattern_trade_storage as storage

UNSANE_RETURN_OFFSET_PCT = 1.0
VALID_WIN_RETURN_PCT = 5.0
SUMMARY_RETURN_PCT = 12.0
SUMMARY_WIN_RATE_PCT = 50.0
SUMMARY_TRADE_COUNT = 2
ONE_DAY_SECONDS = 86_400
BACKTEST_ROW_ID = 42
SCAN_PATTERN_ID = 1011
RELATED_INSIGHT_ID = 1418
ENTRY_TIME_SECONDS = 1_700_000_000
BASE_ENTRY_PRICE = 1.0
UNSANE_EXIT_PRICE = 3.1
VALID_EXIT_PRICE = 1.05


def test_sanitize_outcome_return_pct_nulls_values_outside_sane_bound():
    outcome, label, reason = storage._sanitize_outcome_return_pct(
        storage.PATTERN_TRADE_OUTCOME_RETURN_SANITY_BOUND_PCT
        + storage.PATTERN_TRADE_WIN_LABEL_BREAK_EVEN_RETURN_PCT
        + UNSANE_RETURN_OFFSET_PCT
    )

    assert outcome is None
    assert label is None
    assert reason == storage.PATTERN_TRADE_OUTCOME_RETURN_SANITY_REASON_OUTSIDE_BOUND


def test_sanitize_outcome_return_pct_keeps_boundary_value():
    outcome, label, reason = storage._sanitize_outcome_return_pct(
        storage.PATTERN_TRADE_OUTCOME_RETURN_SANITY_BOUND_PCT
    )

    assert outcome == storage.PATTERN_TRADE_OUTCOME_RETURN_SANITY_BOUND_PCT
    assert label is True
    assert reason is None


def test_persist_rows_nulls_unsane_outcome_without_dropping_batch(monkeypatch):
    captured: dict[str, object] = {}

    class FakeInsert:
        def __init__(self, table):
            self.table = table
            self.rows = []

        def values(self, rows):
            self.rows = rows
            captured["rows"] = rows
            return self

        def on_conflict_do_nothing(self, **_kwargs):
            return self

    class FakeDb:
        def __init__(self):
            self.committed = False
            self.rolled_back = False

        def execute(self, stmt):
            captured["executed_stmt"] = stmt
            return SimpleNamespace(rowcount=len(stmt.rows))

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr(storage, "pg_insert", FakeInsert)
    db = FakeDb()
    backtest_row = SimpleNamespace(id=BACKTEST_ROW_ID, ticker="ONDS")
    result = {
        "ticker": "ONDS",
        "period": "1d",
        "return_pct": SUMMARY_RETURN_PCT,
        "win_rate": SUMMARY_WIN_RATE_PCT,
        "trade_count": SUMMARY_TRADE_COUNT,
        "trades": [
            {
                "entry_time": ENTRY_TIME_SECONDS,
                "exit_time": ENTRY_TIME_SECONDS + ONE_DAY_SECONDS,
                "entry_price": BASE_ENTRY_PRICE,
                "exit_price": UNSANE_EXIT_PRICE,
                "return_pct": (
                    storage.PATTERN_TRADE_OUTCOME_RETURN_SANITY_BOUND_PCT
                    + UNSANE_RETURN_OFFSET_PCT
                ),
            },
            {
                "entry_time": ENTRY_TIME_SECONDS + ONE_DAY_SECONDS,
                "exit_time": ENTRY_TIME_SECONDS
                + (ONE_DAY_SECONDS * SUMMARY_TRADE_COUNT),
                "entry_price": BASE_ENTRY_PRICE,
                "exit_price": VALID_EXIT_PRICE,
                "return_pct": VALID_WIN_RETURN_PCT,
            },
        ],
    }

    inserted = storage.persist_rows_from_backtest_result(
        db,
        user_id=None,
        scan_pattern_id=SCAN_PATTERN_ID,
        related_insight_id=RELATED_INSIGHT_ID,
        backtest_row=backtest_row,
        result=result,
    )

    rows = captured["rows"]
    assert inserted == SUMMARY_TRADE_COUNT
    assert db.committed is True
    assert db.rolled_back is False
    assert len(rows) == SUMMARY_TRADE_COUNT
    assert rows[0]["outcome_return_pct"] is None
    assert rows[0]["label_win"] is None
    assert rows[0]["features_json"]["trade_return_pct"] is None
    assert rows[0]["features_json"]["trade_return_pct_sanitized_reason"] == (
        storage.PATTERN_TRADE_OUTCOME_RETURN_SANITY_REASON_OUTSIDE_BOUND
    )
    assert rows[1]["outcome_return_pct"] == VALID_WIN_RETURN_PCT
    assert rows[1]["label_win"] is True
