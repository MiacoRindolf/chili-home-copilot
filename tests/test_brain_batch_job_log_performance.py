from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.services.trading import brain_batch_job_log


class _PayloadQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _Db:
    def __init__(self, row):
        self._row = row
        self.queried = None

    def query(self, *_args, **_kwargs):
        self.queried = _args
        return _PayloadQuery(self._row)


def test_fetch_latest_ok_payload_reads_payload_end_meta_columns_only() -> None:
    ended_at = datetime(2026, 6, 1, 12, 0)
    db = _Db(({"rows": [1]}, ended_at, {"n": 1}))

    payload, ended, meta = brain_batch_job_log.fetch_latest_ok_payload(db, "scanner")

    assert [getattr(col, "key", None) for col in db.queried] == [
        "payload_json",
        "ended_at",
        "meta_json",
    ]
    assert payload == {"rows": [1]}
    assert ended == ended_at
    assert meta == {"n": 1}


def test_latest_payload_field_supports_empty_object_tuple_and_mapping_rows() -> None:
    ended_at = datetime(2026, 6, 1, 12, 0)
    obj = SimpleNamespace(payload_json={"obj": True}, ended_at=ended_at, meta_json={"m": 1})
    mapping = {"payload_json": {"map": True}, "ended_at": ended_at, "meta_json": {"m": 2}}

    assert brain_batch_job_log._latest_payload_field(None, "payload_json", 0) is None
    assert brain_batch_job_log._latest_payload_field(obj, "payload_json", 0) == {"obj": True}
    assert brain_batch_job_log._latest_payload_field(({"tuple": True}, ended_at, {}), "ended_at", 1) == ended_at
    assert brain_batch_job_log._latest_payload_field(mapping, "meta_json", 2) == {"m": 2}
