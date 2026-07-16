"""Supervised retry worker for retained captured-PAPER admission handoffs.

The worker owns no database, provider, or broker constructor.  It retries only
the exact in-memory material retained by ``IqfeedCapturedPaperRuntimeOwner``
after phase one committed but admission commit/readback did not complete.  The
owner performs durable readback before every commit attempt, so this worker can
neither recompute a decision nor create a blind broker retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import threading
from typing import Any, Callable, Mapping, Protocol


UTC = timezone.utc


class CapturedPaperPostCommitWorkerError(RuntimeError):
    """The supervised admission-retry worker cannot safely continue."""


class _PostCommitRetryOwner(Protocol):
    def retry_pending_post_commits(self, *, limit: int) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CapturedPaperPostCommitWorkerHealth:
    schema_version: str
    ever_started: bool
    running: bool
    stop_requested: bool
    fatal: bool
    fatal_error_type: str | None
    cycles_completed: int
    completions: int
    retry_failures: int
    pending: int
    last_cycle_completed_at: datetime | None

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "ever_started": self.ever_started,
            "running": self.running,
            "stop_requested": self.stop_requested,
            "fatal": self.fatal,
            "fatal_error_type": self.fatal_error_type,
            "cycles_completed": self.cycles_completed,
            "completions": self.completions,
            "retry_failures": self.retry_failures,
            "pending": self.pending,
            "last_cycle_completed_at": (
                None
                if self.last_cycle_completed_at is None
                else self.last_cycle_completed_at.isoformat()
            ),
        }


class CapturedPaperPostCommitWorker:
    """Drain retained post-commit material without decision recomputation."""

    HEALTH_SCHEMA_VERSION = "chili.captured-paper-post-commit-worker-health.v1"

    def __init__(
        self,
        *,
        owner: _PostCommitRetryOwner,
        max_items_per_cycle: int,
        idle_poll_seconds: float,
        observation_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(owner, "retry_pending_post_commits", None)):
            raise CapturedPaperPostCommitWorkerError(
                "captured_paper_post_commit_owner_invalid"
            )
        if (
            isinstance(max_items_per_cycle, bool)
            or not isinstance(max_items_per_cycle, int)
            or max_items_per_cycle <= 0
            or max_items_per_cycle > 10_000
        ):
            raise CapturedPaperPostCommitWorkerError(
                "captured_paper_post_commit_cycle_limit_invalid"
            )
        if (
            isinstance(idle_poll_seconds, bool)
            or not isinstance(idle_poll_seconds, (int, float))
            or not math.isfinite(float(idle_poll_seconds))
            or not 0.01 <= float(idle_poll_seconds) <= 60.0
        ):
            raise CapturedPaperPostCommitWorkerError(
                "captured_paper_post_commit_idle_poll_invalid"
            )
        clock = observation_clock or (lambda: datetime.now(UTC))
        if not callable(clock):
            raise CapturedPaperPostCommitWorkerError(
                "captured_paper_post_commit_clock_invalid"
            )
        self._owner = owner
        self._limit = max_items_per_cycle
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
        self._completions = 0
        self._retry_failures = 0
        self._pending = 0
        self._last_cycle_completed_at: datetime | None = None

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise CapturedPaperPostCommitWorkerError(
                "captured_paper_post_commit_clock_naive"
            )
        return value.astimezone(UTC)

    def run_one_cycle(self) -> Mapping[str, Any]:
        if not self._cycle_lock.acquire(blocking=False):
            raise CapturedPaperPostCommitWorkerError(
                "captured_paper_post_commit_cycle_already_running"
            )
        try:
            result = self._owner.retry_pending_post_commits(limit=self._limit)
            if not isinstance(result, Mapping):
                raise CapturedPaperPostCommitWorkerError(
                    "captured_paper_post_commit_result_invalid"
                )
            exact_keys = {
                "attempted",
                "completed",
                "failed",
                "failure_reasons",
                "remaining",
            }
            integer_fields = ("attempted", "completed", "failed", "remaining")
            if set(result) != exact_keys or any(
                isinstance(result.get(name), bool)
                or not isinstance(result.get(name), int)
                or int(result[name]) < 0
                for name in integer_fields
            ) or int(result["attempted"]) > self._limit:
                raise CapturedPaperPostCommitWorkerError(
                    "captured_paper_post_commit_result_invalid"
                )
            if (
                int(result["completed"]) + int(result["failed"])
                != int(result["attempted"])
                or not isinstance(result.get("failure_reasons"), tuple)
                or len(result["failure_reasons"]) != int(result["failed"])
                or any(
                    not isinstance(reason, str) or not reason.strip()
                    for reason in result["failure_reasons"]
                )
            ):
                raise CapturedPaperPostCommitWorkerError(
                    "captured_paper_post_commit_result_invalid"
                )
            completed_at = self._now()
            with self._state_lock:
                self._cycles_completed += 1
                self._completions += int(result["completed"])
                self._retry_failures += int(result["failed"])
                self._pending = int(result["remaining"])
                self._last_cycle_completed_at = completed_at
            return dict(result)
        finally:
            self._cycle_lock.release()

    def _run(self) -> None:
        with self._state_lock:
            self._running = True
        self._running_ready.set()
        try:
            while not self._stop.is_set():
                try:
                    result = self.run_one_cycle()
                except Exception as exc:
                    with self._state_lock:
                        self._fatal_error_type = type(exc).__name__
                    self._stop.set()
                    self._wake.set()
                    break
                # A failed retained handoff is intentionally non-fatal: its
                # exact material remains available for a later durable
                # readback/commit attempt.  Do not hot-spin when the database
                # or its acknowledgement remains unavailable.
                if int(result["remaining"]) == 0 or int(result["completed"]) == 0:
                    self._wake.wait(self._idle_poll_seconds)
                    self._wake.clear()
        finally:
            with self._state_lock:
                self._running = False

    def start(self) -> None:
        with self._state_lock:
            if self._ever_started:
                raise CapturedPaperPostCommitWorkerError(
                    "captured_paper_post_commit_worker_start_is_one_shot"
                )
            self._ever_started = True
            thread = threading.Thread(
                target=self._run,
                name="chili-captured-paper-post-commit",
                daemon=False,
            )
            self._thread = thread
        thread.start()
        if not self._running_ready.wait(5.0):
            self._stop.set()
            self._wake.set()
            raise CapturedPaperPostCommitWorkerError(
                "captured_paper_post_commit_worker_start_unconfirmed"
            )

    def wake(self) -> None:
        self._wake.set()

    def close(self, *, join_timeout_seconds: float) -> None:
        if (
            isinstance(join_timeout_seconds, bool)
            or not isinstance(join_timeout_seconds, (int, float))
            or not math.isfinite(float(join_timeout_seconds))
            or not 0.01 <= float(join_timeout_seconds) <= 300.0
        ):
            raise CapturedPaperPostCommitWorkerError(
                "captured_paper_post_commit_join_timeout_invalid"
            )
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(float(join_timeout_seconds))
            if thread.is_alive():
                raise CapturedPaperPostCommitWorkerError(
                    "captured_paper_post_commit_worker_did_not_join"
                )

    def health(self) -> CapturedPaperPostCommitWorkerHealth:
        with self._state_lock:
            thread = self._thread
            return CapturedPaperPostCommitWorkerHealth(
                schema_version=self.HEALTH_SCHEMA_VERSION,
                ever_started=self._ever_started,
                running=bool(
                    self._running and thread is not None and thread.is_alive()
                ),
                stop_requested=self._stop.is_set(),
                fatal=self._fatal_error_type is not None,
                fatal_error_type=self._fatal_error_type,
                cycles_completed=self._cycles_completed,
                completions=self._completions,
                retry_failures=self._retry_failures,
                pending=self._pending,
                last_cycle_completed_at=self._last_cycle_completed_at,
            )


__all__ = (
    "CapturedPaperPostCommitWorker",
    "CapturedPaperPostCommitWorkerError",
    "CapturedPaperPostCommitWorkerHealth",
)
