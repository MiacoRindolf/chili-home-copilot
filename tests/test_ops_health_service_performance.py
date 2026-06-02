from __future__ import annotations

import sys
import types

from sqlalchemy import true

from app.services.trading import ops_health_service


class _Field:
    def __ge__(self, _other):
        return true()

    def is_(self, _other):
        return true()


class _ExitParityLog:
    id = _Field()
    created_at = _Field()
    agree_bool = _Field()


class _FakeQuery:
    def __init__(self, row):
        self.row = row
        self.filter_calls = 0

    def filter(self, *_args):
        self.filter_calls += 1
        return self

    def one_or_none(self):
        return self.row

    def count(self):
        raise AssertionError("exit engine summary should use one aggregate query, not count()")


class _FakeDb:
    def __init__(self, row):
        self.row = row
        self.query_calls = 0
        self.last_query = None
        self.query_columns = None

    def query(self, *columns):
        self.query_calls += 1
        self.query_columns = columns
        self.last_query = _FakeQuery(self.row)
        return self.last_query


def test_exit_engine_counts_from_aggregate_handles_tuple_object_and_empty_rows():
    assert ops_health_service._exit_engine_counts_from_aggregate((10, 7)) == (10, 7)
    assert ops_health_service._exit_engine_counts_from_aggregate(types.SimpleNamespace(total=5, agree=None)) == (
        5,
        0,
    )
    assert ops_health_service._exit_engine_counts_from_aggregate(None) == (0, 0)


def test_exit_engine_summary_uses_single_aggregate_query(monkeypatch):
    fake_models = types.ModuleType("app.models.trading")
    fake_models.ExitParityLog = _ExitParityLog
    monkeypatch.setitem(sys.modules, "app.models.trading", fake_models)
    monkeypatch.setattr(ops_health_service.settings, "brain_exit_engine_mode", "off", raising=False)

    db = _FakeDb((12, 9))
    summary = ops_health_service._exit_engine_summary(db=db, lookback_hours=24)

    assert db.query_calls == 1
    assert len(db.query_columns) == 2
    assert db.last_query.filter_calls == 1
    assert summary == {
        "mode": "off",
        "lookback_hours": 24,
        "total": 12,
        "agree": 9,
        "disagree": 3,
        "disagreement_rate": 0.25,
        "by_severity": {"red": 0, "yellow": 3, "green": 9},
    }
