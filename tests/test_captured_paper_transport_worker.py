from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

import pytest

from app.services.trading.momentum_neural.captured_paper_transport_coordinator import (
    CapturedPaperTransportOutcome,
)
from app.services.trading.momentum_neural.captured_paper_transport_worker import (
    CapturedPaperTransportWorker,
    CapturedPaperTransportWorkerError,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 4, 0, tzinfo=UTC)
DIGEST = "a" * 64


class _Coordinator:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def resume_restart_once(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not self.outcomes:
            return None
        value = self.outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _worker(coordinator, **kwargs):
    return CapturedPaperTransportWorker(
        coordinator=coordinator,
        worker_id="paper-host:transport:1",
        lease_seconds=30,
        recovery_limit=25,
        idle_poll_seconds=0.01,
        observation_clock=lambda: NOW,
        **kwargs,
    )


def test_one_cycle_delegates_only_to_restart_safe_coordinator_entrypoint():
    outcome = CapturedPaperTransportOutcome(
        status="accepted",
        completion_sha256=DIGEST,
        client_order_id="cid-1",
    )
    coordinator = _Coordinator([outcome])
    worker = _worker(coordinator)

    assert worker.run_one_cycle() is outcome
    assert coordinator.calls == [
        {
            "lease_owner_id": "paper-host:transport:1",
            "lease_seconds": 30,
            "recovery_limit": 25,
        }
    ]
    health = worker.health()
    assert health.cycles_completed == 1
    assert health.work_outcomes == 1
    assert health.idle_cycles == 0
    assert health.last_outcome_status == "accepted"
    assert health.last_completion_sha256 == DIGEST
    assert health.last_cycle_completed_at == NOW


def test_idle_cycle_is_explicit_and_does_not_invent_broker_truth():
    coordinator = _Coordinator([None])
    worker = _worker(coordinator)

    assert worker.run_one_cycle() is None
    health = worker.health()
    assert health.idle_cycles == 1
    assert health.work_outcomes == 0
    assert health.last_outcome_status is None
    assert health.last_completion_sha256 is None


def test_background_worker_is_wakeable_and_joins_cleanly():
    coordinator = _Coordinator([None, None, None])
    worker = _worker(coordinator)
    worker.start()
    deadline = time.monotonic() + 1.0
    while not coordinator.calls and time.monotonic() < deadline:
        time.sleep(0.005)
    worker.wake()
    deadline = time.monotonic() + 1.0
    while len(coordinator.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    worker.close(join_timeout_seconds=1.0)

    assert len(coordinator.calls) >= 2
    health = worker.health()
    assert health.ever_started is True
    assert health.running is False
    assert health.stop_requested is True
    assert health.fatal is False


def test_unexpected_cycle_failure_is_terminal_and_never_blindly_retried():
    coordinator = _Coordinator([RuntimeError("unknown post boundary")])
    worker = _worker(coordinator)
    worker.start()
    deadline = time.monotonic() + 1.0
    while worker.health().running and time.monotonic() < deadline:
        time.sleep(0.005)
    worker.close(join_timeout_seconds=1.0)

    assert len(coordinator.calls) == 1
    health = worker.health()
    assert health.fatal is True
    assert health.fatal_error_type == "RuntimeError"
    assert health.running is False


def test_concurrent_cycle_is_rejected_instead_of_double_leasing():
    entered = threading.Event()
    release = threading.Event()

    class _BlockingCoordinator:
        def resume_restart_once(self, **_kwargs):
            entered.set()
            assert release.wait(1.0)
            return None

    worker = _worker(_BlockingCoordinator())
    thread = threading.Thread(target=worker.run_one_cycle)
    thread.start()
    assert entered.wait(1.0)
    with pytest.raises(
        CapturedPaperTransportWorkerError,
        match="cycle_already_running",
    ):
        worker.run_one_cycle()
    release.set()
    thread.join(1.0)
    assert not thread.is_alive()


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"worker_id": "bad worker"}, "worker_id_invalid"),
        ({"lease_seconds": 0}, "lease_seconds_invalid"),
        ({"recovery_limit": 0}, "recovery_limit_invalid"),
        ({"idle_poll_seconds": 0}, "idle_poll_invalid"),
    ],
)
def test_worker_configuration_is_bounded(overrides, match):
    values = {
        "coordinator": _Coordinator([]),
        "worker_id": "paper-host:transport:1",
        "lease_seconds": 30,
        "recovery_limit": 25,
        "idle_poll_seconds": 0.01,
        "observation_clock": lambda: NOW,
    }
    values.update(overrides)
    with pytest.raises(CapturedPaperTransportWorkerError, match=match):
        CapturedPaperTransportWorker(**values)


def test_naive_health_clock_fails_closed():
    worker = CapturedPaperTransportWorker(
        coordinator=_Coordinator([None]),
        worker_id="paper-host:transport:1",
        lease_seconds=30,
        recovery_limit=25,
        idle_poll_seconds=0.01,
        observation_clock=lambda: datetime(2026, 7, 16, 4, 0),
    )
    with pytest.raises(
        CapturedPaperTransportWorkerError,
        match="observation_clock_naive",
    ):
        worker.run_one_cycle()
