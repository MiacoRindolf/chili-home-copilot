from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import column


class _TradingAutomationSession:
    id = column("id")
    mode = column("mode")
    state = column("state")
    user_id = column("user_id")


class _FakeQuery:
    def __init__(self, rows: list[tuple[str, int]]):
        self.rows = rows
        self.filter_calls = 0
        self.group_by_calls = 0

    def filter(self, *_args):
        self.filter_calls += 1
        return self

    def group_by(self, *_args):
        self.group_by_calls += 1
        return self

    def all(self):
        return self.rows

    def count(self):
        raise AssertionError("live risk evaluation should use grouped counts, not repeated count()")


class _FakeDb:
    def __init__(self, rows: list[tuple[str, int]]):
        self.rows = rows
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None
        self.query_columns = None

    def query(self, *columns):
        self.query_calls += 1
        self.query_columns = columns
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def _load_risk_evaluator(monkeypatch):
    repo = Path(__file__).resolve().parents[1]

    trading_pkg = types.ModuleType("app.services.trading")
    trading_pkg.__path__ = [str(repo / "app" / "services" / "trading")]
    monkeypatch.setitem(sys.modules, "app.services.trading", trading_pkg)

    momentum_pkg = types.ModuleType("app.services.trading.momentum_neural")
    momentum_pkg.__path__ = [str(repo / "app" / "services" / "trading" / "momentum_neural")]
    monkeypatch.setitem(sys.modules, "app.services.trading.momentum_neural", momentum_pkg)

    fake_models = types.ModuleType("app.models.trading")
    fake_models.MomentumAutomationOutcome = type("MomentumAutomationOutcome", (), {})
    fake_models.MomentumStrategyVariant = type("MomentumStrategyVariant", (), {})
    fake_models.MomentumSymbolViability = type("MomentumSymbolViability", (), {})
    fake_models.TradingAutomationSession = _TradingAutomationSession
    monkeypatch.setitem(sys.modules, "app.models.trading", fake_models)

    sys.modules.pop("app.services.trading.momentum_neural.risk_evaluator", None)
    return importlib.import_module("app.services.trading.momentum_neural.risk_evaluator")


def test_concurrent_session_counts_from_grouped_rows_handles_tuple_and_object_rows(monkeypatch):
    risk_evaluator = _load_risk_evaluator(monkeypatch)

    rows = [
        ("live", 2),
        ("paper", 3),
        SimpleNamespace(mode="shadow", count=11),
        SimpleNamespace(mode=None, count=4),
    ]

    assert risk_evaluator._concurrent_session_counts_from_grouped_rows(rows) == {
        "total": 20,
        "paper": 3,
        "live": 2,
    }


def test_concurrent_automation_session_counts_uses_one_grouped_query(monkeypatch):
    risk_evaluator = _load_risk_evaluator(monkeypatch)
    db = _FakeDb([("live", 2), ("paper", 3)])

    counts = risk_evaluator._concurrent_automation_session_counts(
        db,
        user_id=42,
        exclude_session_id=99,
    )

    assert counts == {"total": 5, "paper": 3, "live": 2}
    assert db.query_calls == 1
    assert len(db.query_columns) == 2
    assert db.last_query is not None
    assert db.last_query.filter_calls == 2
    assert db.last_query.group_by_calls == 1
