from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace


class _FakeQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.row


class _FakeDb:
    def __init__(self, *, backtest, insight, pattern):
        self.backtest = backtest
        self.insight = insight
        self.pattern = pattern

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "BacktestResult":
            return _FakeQuery(self.backtest)
        if name == "TradingInsight":
            return _FakeQuery(self.insight)
        return _FakeQuery(None)

    def get(self, model, _row_id):
        if getattr(model, "__name__", "") == "ScanPattern":
            return self.pattern
        return None


def _fake_rows():
    backtest = SimpleNamespace(
        id=11,
        ticker="AAA",
        user_id=7,
        related_insight_id=22,
        scan_pattern_id=33,
    )
    insight = SimpleNamespace(id=22, user_id=7, scan_pattern_id=33)
    pattern = SimpleNamespace(
        id=33,
        name="Stored Replay Pattern",
        timeframe="1d",
        rules_json="{}",
        exit_config=None,
    )
    return backtest, insight, pattern


def test_stored_backtest_rerun_blocks_incomplete_provenance(monkeypatch):
    from app.services.trading import backtest_param_sets
    from app.services.trading import stored_backtest_rerun

    backtest, insight, pattern = _fake_rows()
    db = _FakeDb(backtest=backtest, insight=insight, pattern=pattern)
    monkeypatch.setattr(
        backtest_param_sets,
        "materialize_backtest_params",
        lambda _db, _bt: {"period": "1y", "interval": "1d"},
    )

    def _should_not_backtest(*_args, **_kwargs):
        raise AssertionError("incomplete stored provenance must not replay")

    monkeypatch.setattr("app.services.backtest_service.backtest_pattern", _should_not_backtest)

    out = stored_backtest_rerun.rerun_stored_backtest_by_id(db, backtest.id)

    assert out["ok"] is False
    assert out["error"] == "stored_backtest_replay_provenance_incomplete"
    assert "chart_time_from" in out["missing_provenance_fields"]
    assert out["backtest_id"] == backtest.id


def test_stored_backtest_rerun_blocks_invalid_replay_math(monkeypatch):
    from app.services.trading import backtest_param_sets
    from app.services.trading import stored_backtest_rerun

    backtest, insight, pattern = _fake_rows()
    db = _FakeDb(backtest=backtest, insight=insight, pattern=pattern)
    monkeypatch.setattr(
        backtest_param_sets,
        "materialize_backtest_params",
        lambda _db, _bt: {
            "period": "6mo",
            "interval": "1d",
            "ohlc_bars": 0,
            "chart_time_from": 1_706_659_200,
            "chart_time_to": 1_704_067_200,
            "cash_used": 50_000.0,
            "spread_used": -0.0002,
            "commission_used": 0.001,
            "provenance_status": "complete",
        },
    )

    def _should_not_backtest(*_args, **_kwargs):
        raise AssertionError("invalid stored provenance must not replay")

    monkeypatch.setattr("app.services.backtest_service.backtest_pattern", _should_not_backtest)

    out = stored_backtest_rerun.rerun_stored_backtest_by_id(db, backtest.id)

    assert out["ok"] is False
    assert out["error"] == "stored_backtest_replay_provenance_invalid"
    assert set(out["invalid_provenance_fields"]) == {
        "chart_time_from",
        "chart_time_to",
        "ohlc_bars",
        "spread_used",
    }


def test_stored_backtest_rerun_requires_complete_provenance_status(monkeypatch):
    from app.services.trading import backtest_param_sets
    from app.services.trading import stored_backtest_rerun

    backtest, insight, pattern = _fake_rows()
    db = _FakeDb(backtest=backtest, insight=insight, pattern=pattern)
    monkeypatch.setattr(
        backtest_param_sets,
        "materialize_backtest_params",
        lambda _db, _bt: {
            "period": "6mo",
            "interval": "1d",
            "ohlc_bars": 120,
            "chart_time_from": 1_704_067_200,
            "chart_time_to": 1_706_659_200,
            "cash_used": 50_000.0,
            "spread_used": 0.0002,
            "commission_used": 0.001,
        },
    )

    def _should_not_backtest(*_args, **_kwargs):
        raise AssertionError("unknown provenance status must not replay")

    monkeypatch.setattr("app.services.backtest_service.backtest_pattern", _should_not_backtest)

    out = stored_backtest_rerun.rerun_stored_backtest_by_id(db, backtest.id)

    assert out["ok"] is False
    assert out["error"] == "stored_backtest_replay_provenance_incomplete"
    assert out["missing_provenance_fields"] == ["provenance_status"]
    assert out["provenance_status"] == "incomplete"


def test_stored_backtest_rerun_uses_stored_window_and_assumptions(monkeypatch):
    from app.services.trading import backtest_param_sets
    from app.services.trading import stored_backtest_rerun

    backtest, insight, pattern = _fake_rows()
    db = _FakeDb(backtest=backtest, insight=insight, pattern=pattern)
    params = {
        "period": "6mo",
        "interval": "1d",
        "ohlc_bars": 120,
        "chart_time_from": 1_704_067_200,
        "chart_time_to": 1_706_659_200,
        "cash_used": 50_000.0,
        "spread_used": 0.0002,
        "commission_used": 0.001,
        "provenance_status": "complete",
    }
    monkeypatch.setattr(
        backtest_param_sets,
        "materialize_backtest_params",
        lambda _db, _bt: dict(params),
    )
    seen = {}

    def _fake_backtest_pattern(**kwargs):
        seen.update(kwargs)
        return {
            "ok": True,
            "ticker": kwargs["ticker"],
            "strategy": "Stored Replay Pattern",
            "return_pct": 1.0,
            "win_rate": 50.0,
            "trade_count": 1,
            "equity_curve": [],
            "max_drawdown": 0.0,
        }

    monkeypatch.setattr("app.services.backtest_service.backtest_pattern", _fake_backtest_pattern)
    monkeypatch.setattr(
        "app.services.backtest_service.save_backtest",
        lambda *_args, **_kwargs: SimpleNamespace(id=backtest.id, ran_at=datetime(2026, 1, 1)),
    )

    out = stored_backtest_rerun.rerun_stored_backtest_by_id(db, backtest.id)

    assert out["ok"] is True
    assert seen["period"] == "6mo"
    assert seen["interval"] == "1d"
    assert seen["ohlc_start"] == "2024-01-01"
    assert seen["ohlc_end"] == "2024-01-31"
    assert seen["cash"] == 50_000.0
    assert seen["spread"] == 0.0002
    assert seen["commission"] == 0.001
