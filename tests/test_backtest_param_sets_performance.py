from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError

from app.models.trading import BacktestParamSet
from app.services.trading import backtest_param_sets


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._row

    def one_or_none(self):
        return self._row


class _FakeDb:
    def __init__(self, row):
        self.row = row
        self.rows = None
        self.query_calls = []
        self.get_calls = []
        self.added = []
        self.flush_calls = 0
        self.raise_on_flush = False

    def query(self, *args):
        self.query_calls.append(args)
        keys = tuple(getattr(arg, "key", None) for arg in args)
        if keys == ("params_json",):
            return _FakeQuery(self.row)
        if keys == ("id",):
            if self.rows is not None:
                return _FakeQuery(self.rows.pop(0))
            return _FakeQuery(self.row)
        raise AssertionError(f"unexpected query shape: {keys!r}")

    def get(self, model, row_id):
        self.get_calls.append((model, row_id))
        return None

    def begin_nested(self):
        return _FakeSavepoint()

    def add(self, row):
        self.added.append(row)

    def flush(self):
        self.flush_calls += 1
        if self.raise_on_flush:
            raise IntegrityError("insert", {}, "duplicate")


class _FakeSavepoint:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_materialize_backtest_params_reads_param_set_json_column_only() -> None:
    db = _FakeDb(row=({"period": "3mo", "interval": "1d"},))
    bt = SimpleNamespace(params=None, param_set_id=42)

    out = backtest_param_sets.materialize_backtest_params(db, bt)

    assert out == {"period": "3mo", "interval": "1d"}
    assert tuple(getattr(arg, "key", None) for arg in db.query_calls[0]) == ("params_json",)
    assert db.get_calls == []


def test_params_json_from_row_handles_tuple_object_and_empty_rows() -> None:
    assert backtest_param_sets._params_json_from_row(({"period": "1y"},)) == {"period": "1y"}
    assert backtest_param_sets._params_json_from_row(
        SimpleNamespace(params_json={"interval": "1h"})
    ) == {"interval": "1h"}
    assert backtest_param_sets._params_json_from_row(None) is None


def test_get_or_create_backtest_param_set_reads_existing_id_column_only() -> None:
    db = _FakeDb(row=(123,))

    out = backtest_param_sets.get_or_create_backtest_param_set(db, {"period": "1y"})

    assert out == 123
    assert [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls] == [("id",)]
    assert db.added == []
    assert db.flush_calls == 0


def test_get_or_create_backtest_param_set_race_lookup_reads_id_column_only() -> None:
    db = _FakeDb(row=None)
    db.rows = [None, (456,)]
    db.raise_on_flush = True

    out = backtest_param_sets.get_or_create_backtest_param_set(db, {"period": "6mo"})

    assert out == 456
    assert [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls] == [
        ("id",),
        ("id",),
    ]
    assert len(db.added) == 1
    assert db.flush_calls == 1


def test_id_from_row_handles_tuple_object_and_empty_rows() -> None:
    assert backtest_param_sets._id_from_row((789,)) == 789
    assert backtest_param_sets._id_from_row(SimpleNamespace(id="321")) == 321
    assert backtest_param_sets._id_from_row(None) is None
    assert backtest_param_sets._id_from_row(()) is None
