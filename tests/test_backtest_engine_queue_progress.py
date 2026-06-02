"""Backtest engine queue progress behavior."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace


_SLOW_TICKER = "SLOW"
_FAST_TICKER = "FAST"
_TEST_USER_ID = 7
_TEST_INSIGHT_ID = 42
_TEST_INITIAL_CONFIDENCE = 0.5
_SLOW_BACKTEST_SECONDS = 0.05
_POSITIVE_RETURN_PCT = 1.0
_TRADE_COUNT_WITH_EVIDENCE = 1
_TARGET_TICKERS = 2
_WORKER_COUNT = 2


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.added: list[object] = []
        self.param_sets: dict[int, object] = {}

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def add(self, item: object) -> None:
        self.added.append(item)

    def get(self, _model, row_id: int):
        return self.param_sets.get(int(row_id))


def test_smart_backtest_persists_completed_tickers_without_order_blocking(monkeypatch):
    from app.services import backtest_service
    from app.services.trading import backtest_engine
    from app.services.trading import experiment_tracker

    saved_tickers: list[str] = []

    def fake_run_pattern_backtest(ticker: str, *args, **kwargs):
        if ticker == _SLOW_TICKER:
            time.sleep(_SLOW_BACKTEST_SECONDS)
        return {
            "ok": True,
            "ticker": ticker,
            "trade_count": _TRADE_COUNT_WITH_EVIDENCE,
            "return_pct": _POSITIVE_RETURN_PCT,
        }

    def fake_save_backtest(db, user_id, result, **kwargs):
        assert user_id == _TEST_USER_ID
        saved_tickers.append(result["ticker"])
        row_id = 10 if result["ticker"] == _SLOW_TICKER else 20
        param_set_id = 110 if result["ticker"] == _SLOW_TICKER else 120
        db.param_sets[param_set_id] = SimpleNamespace(
            param_hash=f"param-hash-{result['ticker']}"
        )
        return SimpleNamespace(id=row_id, param_set_id=param_set_id)

    monkeypatch.setattr(backtest_engine, "_get_shutdown_event", threading.Event)
    monkeypatch.setattr(
        backtest_engine,
        "_extract_context",
        lambda *args, **kwargs: {"mentioned_tickers": []},
    )
    condition_threshold = [50]
    monkeypatch.setattr(
        backtest_engine,
        "_find_linked_pattern",
        lambda db, insight: (
            [{"indicator": "rsi_14", "op": ">", "value": condition_threshold[0]}],
            "queue-progress-pattern",
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        backtest_engine,
        "_select_tickers",
        lambda *args, **kwargs: [_SLOW_TICKER, _FAST_TICKER],
    )
    monkeypatch.setattr(backtest_engine, "_bt_workers", lambda: _WORKER_COUNT)
    monkeypatch.setattr(
        backtest_service,
        "get_backtest_params",
        lambda timeframe: {"interval": "1d", "period": "1mo"},
    )
    monkeypatch.setattr(backtest_service, "run_pattern_backtest", fake_run_pattern_backtest)
    monkeypatch.setattr(backtest_service, "save_backtest", fake_save_backtest)
    monkeypatch.delenv("CHILI_VERSION", raising=False)
    monkeypatch.setattr(experiment_tracker, "get_git_sha", lambda: "git-sha-abc")

    insight = SimpleNamespace(
        id=_TEST_INSIGHT_ID,
        user_id=_TEST_USER_ID,
        scan_pattern_id=None,
        pattern_description="queue progress pattern",
        confidence=_TEST_INITIAL_CONFIDENCE,
        evidence_count=0,
    )

    result = backtest_engine.smart_backtest_insight(
        _FakeSession(),
        insight,
        target_tickers=_TARGET_TICKERS,
    )

    assert saved_tickers == [_FAST_TICKER, _SLOW_TICKER]
    assert result["wins"] == _TARGET_TICKERS
    assert result["losses"] == 0
    assert result["total"] == _TARGET_TICKERS
    assert result["backtests_run"] == _TARGET_TICKERS
    assert result["tickers_selected"] == _TARGET_TICKERS
    assert result["soft_deadline_hit"] is False
    assert result["backtest_result_ids"] == [10, 20]
    assert result["backtest_param_set_ids"] == [110, 120]
    assert result["backtest_param_hashes"] == [
        "param-hash-FAST",
        "param-hash-SLOW",
    ]
    assert result["code_version"] == "git-sha-abc"
    assert result["settings_hash"]
    assert result["conditions_hash"]
    assert result["exit_config_hash"]
    assert result["selected_tickers_hash"]
    assert result["run_lineage"]
    assert result["complete_ticker_attempts"] is True
    assert result["lineage_status"] == "complete"
    assert result["lineage_missing_fields"] == []
    assert result["promotion_grade_provenance"] is True

    saved_tickers.clear()
    condition_threshold[0] = 55
    result_with_different_condition = backtest_engine.smart_backtest_insight(
        _FakeSession(),
        insight,
        target_tickers=_TARGET_TICKERS,
    )
    assert result_with_different_condition["conditions_hash"] != result["conditions_hash"]
    assert result_with_different_condition["settings_hash"] != result["settings_hash"]
