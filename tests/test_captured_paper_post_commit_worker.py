from __future__ import annotations

from datetime import datetime, timezone
import threading

import pytest

from app.services.trading.momentum_neural.captured_paper_post_commit_worker import (
    CapturedPaperPostCommitWorker,
    CapturedPaperPostCommitWorkerError,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 7, 0, tzinfo=UTC)


class _Owner:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def retry_pending_post_commits(self, *, limit):
        self.calls.append(limit)
        if self.rows:
            return self.rows.pop(0)
        return {
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "failure_reasons": (),
            "remaining": 0,
        }


def _result(*, attempted=0, completed=0, failed=0, remaining=0):
    return {
        "attempted": attempted,
        "completed": completed,
        "failed": failed,
        "failure_reasons": tuple("retry_failed" for _ in range(failed)),
        "remaining": remaining,
    }


def test_cycle_records_exact_completions_failures_and_remaining() -> None:
    owner = _Owner(
        [_result(attempted=2, completed=1, failed=1, remaining=1)]
    )
    worker = CapturedPaperPostCommitWorker(
        owner=owner,
        max_items_per_cycle=7,
        idle_poll_seconds=0.01,
        observation_clock=lambda: NOW,
    )

    result = worker.run_one_cycle()

    assert result["completed"] == 1
    assert owner.calls == [7]
    health = worker.health().to_mapping()
    assert health["cycles_completed"] == 1
    assert health["completions"] == 1
    assert health["retry_failures"] == 1
    assert health["pending"] == 1
    assert health["fatal"] is False


def test_malformed_owner_result_fails_closed() -> None:
    owner = _Owner([{"attempted": 1}])
    worker = CapturedPaperPostCommitWorker(
        owner=owner,
        max_items_per_cycle=1,
        idle_poll_seconds=0.01,
        observation_clock=lambda: NOW,
    )

    with pytest.raises(CapturedPaperPostCommitWorkerError, match="result_invalid"):
        worker.run_one_cycle()


def test_owner_cannot_escape_resource_derived_cycle_limit() -> None:
    owner = _Owner([_result(attempted=2, completed=2)])
    worker = CapturedPaperPostCommitWorker(
        owner=owner,
        max_items_per_cycle=1,
        idle_poll_seconds=0.01,
        observation_clock=lambda: NOW,
    )

    with pytest.raises(CapturedPaperPostCommitWorkerError, match="result_invalid"):
        worker.run_one_cycle()


def test_supervised_worker_starts_and_stops_without_external_io() -> None:
    called = threading.Event()

    class _SignalingOwner(_Owner):
        def retry_pending_post_commits(self, *, limit):
            called.set()
            return super().retry_pending_post_commits(limit=limit)

    owner = _SignalingOwner([])
    worker = CapturedPaperPostCommitWorker(
        owner=owner,
        max_items_per_cycle=2,
        idle_poll_seconds=0.01,
        observation_clock=lambda: NOW,
    )

    worker.start()
    assert called.wait(1.0)
    assert worker.health().running is True
    worker.close(join_timeout_seconds=1.0)

    health = worker.health()
    assert health.ever_started is True
    assert health.running is False
    assert health.fatal is False
