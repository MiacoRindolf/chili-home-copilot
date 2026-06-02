from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import ScanPattern
from app.services.trading import backtest_queue


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, *, existing_rows, candidate_rows):
        self.existing_rows = existing_rows
        self.candidate_rows = candidate_rows
        self.query_calls = []

    def query(self, *args):
        self.query_calls.append(args)
        keys = tuple(getattr(arg, "key", None) for arg in args)
        if keys == ("id", "parent_id"):
            return _FakeQuery(
                self.existing_rows if len(self.query_calls) == 1 else self.candidate_rows
            )
        raise AssertionError(f"unexpected query shape: {keys!r}")


def test_exploration_pattern_ids_counts_excluded_lineages_from_columns(monkeypatch) -> None:
    monkeypatch.setattr(backtest_queue, "_queue_lineage_cap", lambda batch_size: 1)
    db = _FakeDb(
        existing_rows=[(1, 10)],
        candidate_rows=[
            SimpleNamespace(id=2, parent_id=10),
            SimpleNamespace(id=3, parent_id=20),
        ],
    )

    out = backtest_queue.get_exploration_pattern_ids(db, exclude_ids={1}, limit=1)

    query_keys = [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls]
    assert query_keys == [("id", "parent_id"), ("id", "parent_id")]
    assert not any(len(call) == 1 and call[0] is ScanPattern for call in db.query_calls)
    assert out == [3]


def test_lineage_key_from_id_row_handles_tuple_and_object_rows() -> None:
    assert backtest_queue._lineage_key_from_id_row((7, 70)) == 70
    assert backtest_queue._lineage_key_from_id_row((7, None)) == 7
    assert backtest_queue._lineage_key_from_id_row(SimpleNamespace(id=8, parent_id=80)) == 80
