"""Smart backtest soft-deadline behavior."""
from __future__ import annotations

import time
from types import SimpleNamespace


class _FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def query(self, *_args, **_kwargs):
        raise RuntimeError("query not needed in this test")

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_smart_backtest_soft_deadline_stops_between_ticker_waves(monkeypatch):
    from app.services import backtest_service
    from app.services.trading import backtest_engine

    tickers = ["AAA", "BBB", "CCC", "DDD"]
    saved: list[dict] = []

    monkeypatch.setattr(backtest_engine, "_bt_workers", lambda: 1)
    monkeypatch.setattr(backtest_engine, "_find_linked_pattern", lambda *_a, **_k: None)
    monkeypatch.setattr(
        backtest_engine,
        "_select_tickers",
        lambda *_a, **_k: list(tickers),
    )
    monkeypatch.setattr(
        backtest_service,
        "get_backtest_params",
        lambda _timeframe: {"interval": "1d", "period": "1y"},
    )
    monkeypatch.setattr(
        backtest_service,
        "infer_pattern_timeframe",
        lambda *_a, **_k: "1d",
    )

    def _fake_run(ticker: str, *_args, **_kwargs) -> dict:
        time.sleep(0.03)
        return {
            "ok": True,
            "ticker": ticker,
            "strategy": "deadline-test",
            "return_pct": 1.0,
            "trade_count": 1,
            "win_rate": 1.0,
        }

    monkeypatch.setattr(backtest_service, "run_pattern_backtest", _fake_run)
    monkeypatch.setattr(
        backtest_service,
        "save_backtest",
        lambda _db, _user_id, result, **_kwargs: saved.append(result),
    )

    insight = SimpleNamespace(
        id=11,
        user_id=7,
        pattern_description="rsi < 30",
        confidence=0.5,
        evidence_count=0,
        win_count=0,
        loss_count=0,
    )

    out = backtest_engine.smart_backtest_insight(
        _FakeDb(),
        insight,
        target_tickers=len(tickers),
        update_confidence=False,
        max_runtime_seconds=0.02,
    )

    assert out["backtests_run"] == 1
    assert out["tickers_selected"] == len(tickers)
    assert out["soft_deadline_hit"] is True
    assert [r["ticker"] for r in saved] == ["AAA"]


def test_smart_backtest_soft_deadline_child_avoids_nested_thread_pool(monkeypatch):
    from app.services import backtest_service
    from app.services.trading import backtest_engine

    tickers = ["AAA", "BBB"]
    saved: list[dict] = []

    monkeypatch.setenv("CHILI_MP_BACKTEST_CHILD", "1")
    monkeypatch.setattr(
        backtest_engine,
        "ThreadPoolExecutor",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("queue child soft budgets should run on the main thread")
        ),
    )
    monkeypatch.setattr(backtest_engine, "_find_linked_pattern", lambda *_a, **_k: None)
    monkeypatch.setattr(
        backtest_engine,
        "_select_tickers",
        lambda *_a, **_k: list(tickers),
    )
    monkeypatch.setattr(
        backtest_service,
        "get_backtest_params",
        lambda _timeframe: {"interval": "1d", "period": "1y"},
    )
    monkeypatch.setattr(
        backtest_service,
        "infer_pattern_timeframe",
        lambda *_a, **_k: "1d",
    )
    monkeypatch.setattr(
        backtest_service,
        "run_pattern_backtest",
        lambda ticker, *_a, **_k: {
            "ok": True,
            "ticker": ticker,
            "strategy": "deadline-test",
            "return_pct": 1.0,
            "trade_count": 1,
            "win_rate": 1.0,
        },
    )
    monkeypatch.setattr(
        backtest_service,
        "save_backtest",
        lambda _db, _user_id, result, **_kwargs: saved.append(result),
    )

    insight = SimpleNamespace(
        id=12,
        user_id=7,
        pattern_description="rsi < 30",
        confidence=0.5,
        evidence_count=0,
        win_count=0,
        loss_count=0,
    )

    out = backtest_engine.smart_backtest_insight(
        _FakeDb(),
        insight,
        target_tickers=len(tickers),
        update_confidence=False,
        max_runtime_seconds=10.0,
    )

    assert out["backtests_run"] == len(tickers)
    assert out["soft_deadline_hit"] is False
    assert [r["ticker"] for r in saved] == tickers
