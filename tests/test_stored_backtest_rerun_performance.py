from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.services.trading import stored_backtest_rerun


class _FakeQuery:
    def __init__(self, row: object) -> None:
        self.row = row
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def first(self) -> object:
        return self.row


class _FakeSession:
    def __init__(self, rows_by_keys: dict[tuple[str | None, ...], object]) -> None:
        self.rows_by_keys = rows_by_keys
        self.query_calls: list[tuple[object, ...]] = []
        self.get_calls: list[tuple[object, object]] = []
        self.queries: list[_FakeQuery] = []

    def query(self, *args: object) -> _FakeQuery:
        self.query_calls.append(args)
        keys = tuple(getattr(arg, "key", None) for arg in args)
        query = _FakeQuery(self.rows_by_keys[keys])
        self.queries.append(query)
        return query

    def get(self, model: object, row_id: object) -> object:
        self.get_calls.append((model, row_id))
        return None


def test_rerun_stored_backtest_uses_compact_setup_rows(monkeypatch) -> None:
    import app.services.backtest_service as backtest_service

    saved = SimpleNamespace(id=777, ran_at=datetime(2026, 6, 1, 12, 0))
    calls: dict[str, object] = {}

    def fake_window(timeframe: str) -> tuple[str, str]:
        calls["timeframe"] = timeframe
        return "1y", "1d"

    def fake_backtest_pattern(**kwargs: object) -> dict[str, object]:
        calls["backtest"] = kwargs
        return {"ok": True, "return_pct": 12.5}

    def fake_save_backtest(db: object, user_id: object, result: object, **kwargs: object) -> object:
        calls["save"] = {"user_id": user_id, "result": result, **kwargs}
        return saved

    monkeypatch.setattr(backtest_service, "get_brain_backtest_window", fake_window)
    monkeypatch.setattr(backtest_service, "backtest_pattern", fake_backtest_pattern)
    monkeypatch.setattr(backtest_service, "save_backtest", fake_save_backtest)

    db = _FakeSession(
        {
            ("user_id", "ticker", "related_insight_id", "scan_pattern_id", "params", "param_set_id"): (
                10,
                "AAPL",
                99,
                None,
                {"period": "6mo", "interval": "1h"},
                None,
            ),
            ("user_id", "scan_pattern_id"): (20, 42),
            ("name", "rules_json", "exit_config", "timeframe"): (
                "Breakout",
                {"conditions": [{"indicator": "rsi", "op": ">", "value": 50}]},
                {"stop": "atr"},
                "4h",
            ),
        }
    )

    out = stored_backtest_rerun.rerun_stored_backtest_by_id(db, 123)  # type: ignore[arg-type]

    assert out["ok"] is True
    assert out["backtest_id"] == 777
    assert [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls] == [
        ("user_id", "ticker", "related_insight_id", "scan_pattern_id", "params", "param_set_id"),
        ("user_id", "scan_pattern_id"),
        ("name", "rules_json", "exit_config", "timeframe"),
    ]
    assert db.get_calls == []
    assert [query.filter_calls for query in db.queries] == [1, 1, 1]
    assert calls["timeframe"] == "4h"
    backtest_call = dict(calls["backtest"])  # type: ignore[arg-type]
    assert isinstance(backtest_call.pop("commission"), float)
    assert isinstance(backtest_call.pop("spread"), float)
    assert backtest_call == {
        "ticker": "AAPL",
        "pattern_name": "Breakout",
        "rules_json": {"conditions": [{"indicator": "rsi", "op": ">", "value": 50}]},
        "interval": "1h",
        "period": "6mo",
        "exit_config": {"stop": "atr"},
        "cash": 100_000.0,
        "ohlc_start": None,
        "ohlc_end": None,
    }
    assert calls["save"] == {
        "user_id": 20,
        "result": {"ok": True, "return_pct": 12.5},
        "insight_id": 99,
        "scan_pattern_id": 42,
        "backtest_row_id": 123,
    }


def test_stored_rerun_value_helpers_handle_tuple_object_and_empty_rows() -> None:
    assert stored_backtest_rerun._stored_backtest_rerun_values(None) is None
    assert stored_backtest_rerun._related_insight_rerun_values((20, 42)).scan_pattern_id == 42
    assert stored_backtest_rerun._scan_pattern_rerun_values(("Name", {}, None, "1d")).name == "Name"

    existing = SimpleNamespace(ticker="AAPL")
    assert stored_backtest_rerun._stored_backtest_rerun_values(existing) is existing


def test_collect_evidence_listed_backtest_ids_uses_compact_insight_and_pattern_reads(
    monkeypatch,
) -> None:
    import app.routers.trading_sub.ai as brain_ai

    calls: dict[str, object] = {}

    def fake_universe(db: object, desc: str, scan_pattern_id: int | None, *, insight_id: int) -> str:
        calls["universe"] = {
            "desc": desc,
            "scan_pattern_id": scan_pattern_id,
            "insight_id": insight_id,
        }
        return "stocks"

    def fake_panel(db: object, insight_ids: list[int], *, asset_universe: str, scan_pattern_id: int | None) -> dict:
        calls["panel"] = {
            "insight_ids": insight_ids,
            "asset_universe": asset_universe,
            "scan_pattern_id": scan_pattern_id,
        }
        return {"backtests_out": [{"id": "101"}, {"id": None}, {"id": "bad"}, {"id": 202}]}

    monkeypatch.setattr(brain_ai, "_evidence_backtest_asset_universe", fake_universe)
    monkeypatch.setattr(brain_ai, "_compute_deduped_backtest_win_stats", fake_panel)

    db = _FakeSession(
        {
            ("id", "scan_pattern_id", "pattern_description"): (7, 42, "Breakout desc"),
            ("id",): (42,),
        }
    )

    ids, err = stored_backtest_rerun.collect_evidence_listed_backtest_ids(
        db, 7, limit=1,  # type: ignore[arg-type]
    )

    assert ids == [101]
    assert err is None
    assert [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls] == [
        ("id", "scan_pattern_id", "pattern_description"),
        ("id",),
    ]
    assert db.get_calls == []
    assert [query.filter_calls for query in db.queries] == [1, 1]
    assert calls["universe"] == {
        "desc": "Breakout desc",
        "scan_pattern_id": 42,
        "insight_id": 7,
    }
    assert calls["panel"] == {
        "insight_ids": [7],
        "asset_universe": "stocks",
        "scan_pattern_id": 42,
    }


def test_evidence_insight_values_and_scan_pattern_resolution_handle_missing_rows() -> None:
    db = _FakeSession(
        {
            ("id",): None,
        }
    )
    model = SimpleNamespace(id=SimpleNamespace(key="id"))

    assert stored_backtest_rerun._evidence_insight_values((1, 2, "desc")).pattern_description == "desc"
    assert stored_backtest_rerun._evidence_insight_values(None) is None
    assert stored_backtest_rerun._scan_pattern_id_from_insight_row(
        db,
        SimpleNamespace(scan_pattern_id=2),
        model,
    ) is None
