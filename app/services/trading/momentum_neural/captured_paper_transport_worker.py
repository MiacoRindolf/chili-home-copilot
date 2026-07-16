"""Supervised runtime worker for the durable captured-PAPER transport queue.

The worker owns no broker or database construction.  Its only authority is the
already-bound ``CapturedPaperTransportCoordinator`` supplied by the dedicated
PAPER service.  Each cycle asks that coordinator to resume one durable row;
the coordinator itself gives same-CID reconciliation priority over a fresh
POST.  Unexpected failures are terminal and visible in health rather than
being converted into an unsafe retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import threading
from typing import Callable, Mapping, Protocol

from .captured_paper_transport_coordinator import CapturedPaperTransportOutcome


UTC = timezone.utc
_WORKER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")


class CapturedPaperTransportWorkerError(RuntimeError):
    """The supervised transport worker cannot safely continue."""


class _RestartCoordinator(Protocol):
    def resume_restart_once(
        self,
        *,
        lease_owner_id: str,
        lease_seconds: int,
        recovery_limit: int,
    ) -> CapturedPaperTransportOutcome | None: ...


@dataclass(frozen=True, slots=True)
class CapturedPaperTransportWorkerHealth:
    schema_version: str
    worker_id: str
    ever_started: bool
    running: bool
    stop_requested: bool
    fatal: bool
    fatal_error_type: str | None
    cycles_completed: int
    work_outcomes: int
    idle_cycles: int
    last_outcome_status: str | None
    last_completion_sha256: str | None
    last_cycle_completed_at: datetime | None

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "worker_id": self.worker_id,
            "ever_started": self.ever_started,
            "running": self.running,
            "stop_requested": self.stop_requested,
            "fatal": self.fatal,
            "fatal_error_type": self.fatal_error_type,
            "cycles_completed": self.cycles_completed,
            "work_outcomes": self.work_outcomes,
            "idle_cycles": self.idle_cycles,
            "last_outcome_status": self.last_outcome_status,
            "last_completion_sha256": self.last_completion_sha256,
            "last_cycle_completed_at": (
                None
                if self.last_cycle_completed_at is None
                else self.last_cycle_completed_at.isoformat()
            ),
        }


class CapturedPaperTransportWorker:
    """Drain one durable PAPER transport/reconciliation item per cycle."""

    HEALTH_SCHEMA_VERSION = "chili.captured-paper-transport-worker-health.v1"

    def __init__(
        self,
        *,
        coordinator: _RestartCoordinator,
        worker_id: str,
        lease_seconds: int,
        recovery_limit: int,
        idle_poll_seconds: float,
        observation_clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        if _WORKER_ID_RE.fullmatch(normalized_worker_id) is None:
            raise CapturedPaperTransportWorkerError(
                "captured_paper_transport_worker_id_invalid"
            )
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
            or lease_seconds > 86_400
        ):
            raise CapturedPaperTransportWorkerError(
                "captured_paper_transport_lease_seconds_invalid"
            )
        if (
            isinstance(recovery_limit, bool)
            or not isinstance(recovery_limit, int)
            or recovery_limit <= 0
            or recovery_limit > 10_000
        ):
            raise CapturedPaperTransportWorkerError(
                "captured_paper_transport_recovery_limit_invalid"
            )
        if (
            isinstance(idle_poll_seconds, bool)
            or not isinstance(idle_poll_seconds, (int, float))
            or not 0.01 <= float(idle_poll_seconds) <= 60.0
        ):
            raise CapturedPaperTransportWorkerError(
                "captured_paper_transport_idle_poll_invalid"
            )
        clock = observation_clock or (lambda: datetime.now(UTC))
        if not callable(clock):
            raise CapturedPaperTransportWorkerError(
                "captured_paper_transport_observation_clock_invalid"
            )

        self._coordinator = coordinator
        self._worker_id = normalized_worker_id
        self._lease_seconds = lease_seconds
        self._recovery_limit = recovery_limit
        self._idle_poll_seconds = float(idle_poll_seconds)
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running_ready = threading.Event()
        self._cycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ever_started = False
        self._running = False
        self._fatal_error_type: str | None = None
        self._cycles_completed = 0
        self._work_outcomes = 0
        self._idle_cycles = 0
        self._last_outcome_status: str | None = None
        self._last_completion_sha256: str | None = None
        self._last_cycle_completed_at: datetime | None = None

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise CapturedPaperTransportWorkerError(
                "captured_paper_transport_observation_clock_naive"
            )
        return value.astimezone(UTC)

    def run_one_cycle(self) -> CapturedPaperTransportOutcome | None:
        """Run exactly one coordinator cycle; never retry an exception here."""

        if not self._cycle_lock.acquire(blocking=False):
            raise CapturedPaperTransportWorkerError(
                "captured_paper_transport_cycle_already_running"
            )
        try:
            outcome = self._coordinator.resume_restart_once(
                lease_owner_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                recovery_limit=self._recovery_limit,
            )
            if outcome is not None and type(outcome) is not CapturedPaperTransportOutcome:
                raise CapturedPaperTransportWorkerError(
                    "captured_paper_transport_outcome_type_invalid"
                )
            completed_at = self._now()
            with self._state_lock:
                self._cycles_completed += 1
                self._last_cycle_completed_at = completed_at
                if outcome is None:
                    self._idle_cycles += 1
                    self._last_outcome_status = None
                    self._last_completion_sha256 = None
                else:
                    self._work_outcomes += 1
                    self._last_outcome_status = outcome.status
                    self._last_completion_sha256 = outcome.completion_sha256
            return outcome
        finally:
            self._cycle_lock.release()

    def _run(self) -> None:
        with self._state_lock:
            self._running = True
        self._running_ready.set()
        try:
            while not self._stop.is_set():
                try:
                    outcome = self.run_one_cycle()
                except Exception as exc:
                    # An unknown fault may have happened before or after broker
                    # I/O.  Do not create a blind retry loop; coordinator state
                    # remains durable for a supervised same-CID restart.
                    with self._state_lock:
                        self._fatal_error_type = type(exc).__name__
                    self._stop.set()
                    self._wake.set()
                    break
                if outcome is None or outcome.status == "no_work":
                    self._wake.wait(self._idle_poll_seconds)
                    self._wake.clear()
        finally:
            with self._state_lock:
                self._running = False

    def start(self) -> None:
        with self._state_lock:
            if self._ever_started:
                raise CapturedPaperTransportWorkerError(
                    "captured_paper_transport_worker_start_is_one_shot"
                )
            self._ever_started = True
            thread = threading.Thread(
                target=self._run,
                name="chili-captured-paper-transport",
                daemon=False,
            )
            self._thread = thread
        thread.start()
        if not self._running_ready.wait(5.0):
            self._stop.set()
            self._wake.set()
            raise CapturedPaperTransportWorkerError(
                "captured_paper_transport_worker_start_unconfirmed"
            )

    def wake(self) -> None:
        """Wake an idle worker after a phase-one outbox commit."""

        self._wake.set()

    def close(self, *, join_timeout_seconds: float) -> None:
        if (
            isinstance(join_timeout_seconds, bool)
            or not isinstance(join_timeout_seconds, (int, float))
            or not 0.01 <= float(join_timeout_seconds) <= 300.0
        ):
            raise CapturedPaperTransportWorkerError(
                "captured_paper_transport_join_timeout_invalid"
            )
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(float(join_timeout_seconds))
            if thread.is_alive():
                raise CapturedPaperTransportWorkerError(
                    "captured_paper_transport_worker_did_not_join"
                )

    def health(self) -> CapturedPaperTransportWorkerHealth:
        with self._state_lock:
            thread = self._thread
            running = bool(
                self._running and thread is not None and thread.is_alive()
            )
            return CapturedPaperTransportWorkerHealth(
                schema_version=self.HEALTH_SCHEMA_VERSION,
                worker_id=self._worker_id,
                ever_started=self._ever_started,
                running=running,
                stop_requested=self._stop.is_set(),
                fatal=self._fatal_error_type is not None,
                fatal_error_type=self._fatal_error_type,
                cycles_completed=self._cycles_completed,
                work_outcomes=self._work_outcomes,
                idle_cycles=self._idle_cycles,
                last_outcome_status=self._last_outcome_status,
                last_completion_sha256=self._last_completion_sha256,
                last_cycle_completed_at=self._last_cycle_completed_at,
            )


__all__ = (
    "CapturedPaperTransportWorker",
    "CapturedPaperTransportWorkerError",
    "CapturedPaperTransportWorkerHealth",
)
