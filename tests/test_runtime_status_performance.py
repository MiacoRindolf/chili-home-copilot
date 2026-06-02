from __future__ import annotations

from datetime import UTC, datetime

from app.models.core import BrokerSession
from app.models.trading import BrainBatchJob
from app.services.trading import runtime_status
from app.services.trading.batch_job_constants import JOB_MOMENTUM_SCANNER


class _FakeQuery:
    def __init__(self, *, first_result=None, count_result=0):
        self._first_result = first_result
        self._count_result = count_result

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self._first_result

    def count(self):
        return self._count_result


class _FakeDb:
    def __init__(self, *, latest_job_row=None, stale_running=0, broker_session_row=None):
        self.latest_job_row = latest_job_row
        self.stale_running = stale_running
        self.broker_session_row = broker_session_row
        self.query_calls = []

    def query(self, *args):
        self.query_calls.append(args)
        if len(args) == 5:
            return _FakeQuery(first_result=self.latest_job_row)
        if len(args) == 1 and args[0] is BrainBatchJob:
            return _FakeQuery(count_result=self.stale_running)
        if len(args) == 1 and getattr(args[0], "key", None) == "updated_at":
            return _FakeQuery(first_result=self.broker_session_row)
        raise AssertionError(f"unexpected query shape: {args!r}")


def test_scanner_status_reads_latest_ok_as_columns() -> None:
    ended_at = datetime.now(UTC)
    db = _FakeDb(
        latest_job_row=(
            "job-1",
            JOB_MOMENTUM_SCANNER,
            {"picked": 12},
            ended_at,
            datetime(2026, 6, 1, 11, 59, tzinfo=UTC),
        ),
        stale_running=0,
    )

    status = runtime_status.scanner_status(db)

    assert len(db.query_calls[0]) == 5
    assert db.query_calls[0][0] is BrainBatchJob.id
    assert len(db.query_calls[1]) == 1
    assert db.query_calls[1][0] is BrainBatchJob
    assert status["state"] == "ok"
    assert status["latest_job_id"] == "job-1"
    assert status["payload"] == {"picked": 12}


def test_broker_status_reads_session_timestamp_as_column(monkeypatch) -> None:
    surface_at = datetime.now(UTC)
    session_at = datetime(2026, 6, 1, 12, 1, tzinfo=UTC)

    monkeypatch.setattr(
        runtime_status,
        "read_runtime_surface_state",
        lambda db, *, surface: {"surface": surface, "state": "ok", "as_of": surface_at},
    )
    db = _FakeDb(broker_session_row=(session_at,))

    status = runtime_status.broker_status(db)

    assert len(db.query_calls) == 1
    assert len(db.query_calls[0]) == 1
    assert getattr(db.query_calls[0][0], "key", None) == "updated_at"
    assert status["state"] == "ok"
    assert status["session_as_of"] == "2026-06-01T12:01:00Z"
