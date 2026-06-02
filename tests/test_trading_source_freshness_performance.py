from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.services.trading import trading_source_freshness as freshness


class _ScalarQuery:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _JobQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _Db:
    def __init__(self, scalar_values, job_row):
        self._scalar_values = list(scalar_values)
        self._job_row = job_row
        self.query_calls = []

    def query(self, *args, **_kwargs):
        self.query_calls.append(args)
        if len(args) == 1 and getattr(args[0], "key", None) == "ended_at":
            return _JobQuery(self._job_row)
        return _ScalarQuery(self._scalar_values.pop(0))


def test_collect_source_freshness_reads_imminent_job_ended_at_column_only(monkeypatch) -> None:
    ended_at = datetime(2026, 6, 1, 12, 0)
    db = _Db(
        [
            datetime(2026, 6, 1, 10, 0),
            datetime(2026, 6, 1, 11, 0),
            datetime(2026, 6, 1, 11, 30),
        ],
        (ended_at,),
    )

    monkeypatch.setattr(
        "app.services.trading.learning.get_prediction_swr_cache_meta",
        lambda: {"cache_last_updated_unix": 0},
    )

    out = freshness.collect_source_freshness(db)

    assert [getattr(col, "key", None) for col in db.query_calls[-1]] == ["ended_at"]
    assert out["imminent_job_ok_latest_utc"] == "2026-06-01T12:00:00+00:00"


def test_ended_at_from_row_supports_empty_object_tuple_and_mapping_rows() -> None:
    ended_at = datetime(2026, 6, 1, 12, 0)

    assert freshness._ended_at_from_row(None) is None
    assert freshness._ended_at_from_row(SimpleNamespace(ended_at=ended_at)) == ended_at
    assert freshness._ended_at_from_row((ended_at,)) == ended_at
    assert freshness._ended_at_from_row({"ended_at": ended_at}) == ended_at
