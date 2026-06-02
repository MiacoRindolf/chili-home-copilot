from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.trading import universe_snapshot


class _StatusQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _StatusDb:
    def __init__(self, row):
        self._row = row
        self.queried = None

    def query(self, *_args, **_kwargs):
        self.queried = _args
        return _StatusQuery(self._row)


def test_lookup_status_reads_status_column_only() -> None:
    db = _StatusDb(("halted",))

    status = universe_snapshot.lookup_status(db, ticker="abc", as_of_date=date(2026, 6, 1))

    assert [getattr(col, "key", None) for col in db.queried] == ["status"]
    assert status == "halted"


def test_status_from_row_supports_empty_object_tuple_and_mapping_rows() -> None:
    assert universe_snapshot._status_from_row(None) is None
    assert universe_snapshot._status_from_row(SimpleNamespace(status="active")) == "active"
    assert universe_snapshot._status_from_row(("delisted",)) == "delisted"
    assert universe_snapshot._status_from_row({"status": "unknown"}) == "unknown"
