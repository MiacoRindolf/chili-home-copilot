"""Lifecycle supervisor for the dedicated captured Alpaca PAPER service.

This module contains orchestration only.  It neither constructs credentials nor
opens provider, database, or broker connections on import.  The dedicated
service supplies already-verified components and the supervisor starts them in
the only accepted order:

1. bind/start captured IQFeed provider lanes;
2. register the exact captured-PAPER dispatch runtime;
3. start durable transport/fill recovery workers;
4. start the event-driven live-session loop last.

Stopping reverses the risk-producing boundary first.  A no-order smoke starts
only steps 1-2, so it structurally cannot invoke POST or a live-session tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import re
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from .captured_paper_dispatcher import (
    CapturedPaperRuntime,
    CapturedPaperRuntimeHandle,
    CapturedPaperRuntimeUnavailableError,
    register_captured_paper_runtime,
)


class CapturedPaperServiceSupervisorError(RuntimeError):
    """The paper-only runtime cannot be started or quiesced safely."""


class CapturedPaperServiceState(str, Enum):
    PREPARED = "prepared"
    NO_ORDER_SMOKE = "no_order_smoke"
    ACTIVE = "active"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class _CaptureHost(Protocol):
    def start_provider_loops(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def health(self) -> Mapping[str, Any]: ...
    def close(self) -> Mapping[str, Any]: ...


class _ManagedWorker(Protocol):
    def start(self) -> None: ...
    def close(self, *, join_timeout_seconds: float) -> None: ...
    def health(self) -> Any: ...


class _ServiceFence(Protocol):
    def acquire(self) -> Mapping[str, Any]: ...
    def assert_held(self) -> None: ...
    def release(self) -> Mapping[str, Any]: ...
    def health(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CapturedPaperManagedWorker:
    name: str
    worker: _ManagedWorker

    def __post_init__(self) -> None:
        normalized = str(self.name or "").strip().lower()
        if not normalized or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in normalized):
            raise ValueError("captured PAPER managed-worker name is invalid")
        for method in ("start", "close", "health"):
            if not callable(getattr(self.worker, method, None)):
                raise ValueError(
                    f"captured PAPER managed worker lacks {method}"
                )
        object.__setattr__(self, "name", normalized)


@dataclass(frozen=True, slots=True)
class CapturedPaperActiveStartAuthority:
    """One-shot final authority consumed after provider/runtime startup.

    The service owns the callback.  It re-loads the sealed activation and
    consumes its process-bound launcher/cutover attestation at the last point
    before any recovery worker or live-session loop can run.
    """

    expected_account_id: str
    runtime_generation: str
    consume: Callable[[], Mapping[str, Any]]
    assert_current: Callable[[], None]

    def __post_init__(self) -> None:
        if not self.expected_account_id or not self.runtime_generation:
            raise ValueError("captured PAPER active-start identity is absent")
        if not callable(self.consume):
            raise ValueError("captured PAPER active-start consumer is invalid")
        if not callable(self.assert_current):
            raise ValueError("captured PAPER active-start current-fence is invalid")


def _health_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    converter = getattr(value, "to_mapping", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return dict(converted)
    raise CapturedPaperServiceSupervisorError(
        "captured_paper_managed_worker_health_invalid"
    )


class CapturedPaperServiceSupervisor:
    """Own one process generation of the fake-money PAPER runtime."""

    HEALTH_SCHEMA_VERSION = "chili.captured-paper-service-supervisor-health.v1"
    _SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

    def __init__(
        self,
        *,
        host: _CaptureHost,
        runtime: CapturedPaperRuntime,
        service_fence: _ServiceFence,
        fenced_prestart_revalidate: Callable[[], Mapping[str, Any]],
        managed_workers: Sequence[CapturedPaperManagedWorker],
        live_loop_start: Callable[[], bool],
        live_loop_stop: Callable[[], bool],
        live_loop_health: Callable[[], bool],
        runtime_registrar: Callable[
            [CapturedPaperRuntime], CapturedPaperRuntimeHandle
        ] = register_captured_paper_runtime,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        for method in ("start_provider_loops", "health", "close"):
            if not callable(getattr(host, method, None)):
                raise CapturedPaperServiceSupervisorError(
                    f"captured_paper_host_lacks_{method}"
                )
        if type(runtime) is not CapturedPaperRuntime:
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_runtime_type_invalid"
            )
        for method in ("acquire", "assert_held", "release", "health"):
            if not callable(getattr(service_fence, method, None)):
                raise CapturedPaperServiceSupervisorError(
                    f"captured_paper_service_fence_lacks_{method}"
                )
        workers = tuple(managed_workers)
        if any(type(item) is not CapturedPaperManagedWorker for item in workers):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_managed_worker_invalid"
            )
        names = tuple(item.name for item in workers)
        if len(names) != len(set(names)):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_managed_worker_duplicated"
            )
        for callback, label in (
            (live_loop_start, "live_loop_start"),
            (live_loop_stop, "live_loop_stop"),
            (live_loop_health, "live_loop_health"),
            (runtime_registrar, "runtime_registrar"),
            (monotonic_clock, "monotonic_clock"),
            (wait, "wait"),
            (fenced_prestart_revalidate, "fenced_prestart_revalidate"),
        ):
            if not callable(callback):
                raise CapturedPaperServiceSupervisorError(
                    f"captured_paper_{label}_invalid"
                )
        self._host = host
        self._runtime = runtime
        self._service_fence = service_fence
        self._fenced_prestart_revalidate = fenced_prestart_revalidate
        self._workers = workers
        self._live_loop_start = live_loop_start
        self._live_loop_stop = live_loop_stop
        self._live_loop_health = live_loop_health
        self._registrar = runtime_registrar
        self._monotonic = monotonic_clock
        self._wait = wait
        self._state = CapturedPaperServiceState.PREPARED
        self._runtime_handle: Any | None = None
        self._started_workers: list[CapturedPaperManagedWorker] = []
        self._provider_receipt: Mapping[str, Any] | None = None
        self._service_fence_receipt: Mapping[str, Any] | None = None
        self._fenced_prestart_receipt: Mapping[str, Any] | None = None
        self._active_start_authority_receipt: Mapping[str, Any] | None = None
        self._live_loop_started = False

    @property
    def state(self) -> CapturedPaperServiceState:
        return self._state

    @staticmethod
    def _provider_options(
        options: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        provided = dict(options or {})
        allowed = {
            "readiness_timeout_seconds",
            "join_timeout_seconds",
            "reconnect_wait_seconds",
            "trade_forced_symbols",
            "depth_forced_symbols",
        }
        if set(provided) - allowed:
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_provider_options_invalid"
            )
        return provided

    def _start_capture_and_runtime(
        self,
        *,
        provider_options: Mapping[str, Any] | None,
    ) -> None:
        fence_receipt = self._service_fence.acquire()
        # Any return from acquire means the fence implementation may hold a
        # session lock.  Record that fact before semantic validation so the
        # rollback path releases it last even if the receipt is malformed.
        self._service_fence_receipt = (
            dict(fence_receipt)
            if isinstance(fence_receipt, Mapping)
            else {"receipt_unavailable": True}
        )
        if not (
            isinstance(fence_receipt, Mapping)
            and fence_receipt.get("account_scope") == "alpaca:paper"
            and fence_receipt.get("held") is True
            and fence_receipt.get("live_cash_authorized") is False
            and fence_receipt.get("real_money_authorized") is False
        ):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_service_fence_acquire_unconfirmed"
            )
        self._service_fence.assert_held()
        fenced_prestart = self._fenced_prestart_revalidate()
        fenced_body = dict(fenced_prestart) if isinstance(
            fenced_prestart, Mapping
        ) else {}
        supplied_fenced_sha256 = str(
            fenced_body.pop("receipt_sha256", "") or ""
        )
        expected_fenced_body_keys = {
            "schema_version",
            "verdict",
            "account_scope",
            "expected_account_id",
            "runtime_generation",
            "baseline_restart_gate_receipt_sha256",
            "restart_gate_receipt_sha256",
            "admission_inventory_sha256",
            "initial_recovery_count",
            "initial_recovery_inventory_sha256",
            "durable_admission_drift",
            "broker_inventory_flat",
            "paper_execution_only",
            "live_cash_authorized",
            "real_money_authorized",
        }
        if not (
            isinstance(fenced_prestart, Mapping)
            and set(fenced_body) == expected_fenced_body_keys
            and fenced_prestart.get("schema_version")
            == "chili.captured-paper-fenced-prestart.v1"
            and fenced_prestart.get("verdict")
            == "CAPTURED_ALPACA_PAPER_FENCED_PRESTART_REVALIDATED"
            and fenced_prestart.get("account_scope") == "alpaca:paper"
            and fenced_prestart.get("expected_account_id")
            == self._runtime.expected_account_id
            and fenced_prestart.get("runtime_generation")
            == self._runtime.runtime_generation
            and fenced_prestart.get("durable_admission_drift") is False
            and fenced_prestart.get("broker_inventory_flat") is True
            and fenced_prestart.get("paper_execution_only") is True
            and fenced_prestart.get("live_cash_authorized") is False
            and fenced_prestart.get("real_money_authorized") is False
            and self._SHA256_RE.fullmatch(
                str(fenced_prestart.get("admission_inventory_sha256") or "")
            )
            and isinstance(
                fenced_prestart.get("initial_recovery_count"), int
            )
            and not isinstance(
                fenced_prestart.get("initial_recovery_count"), bool
            )
            and fenced_prestart.get("initial_recovery_count") >= 0
            and self._SHA256_RE.fullmatch(
                str(
                    fenced_prestart.get(
                        "initial_recovery_inventory_sha256"
                    )
                    or ""
                )
            )
            and self._SHA256_RE.fullmatch(
                str(
                    fenced_prestart.get(
                        "baseline_restart_gate_receipt_sha256"
                    )
                    or ""
                )
            )
            and self._SHA256_RE.fullmatch(
                str(fenced_prestart.get("restart_gate_receipt_sha256") or "")
            )
            and self._SHA256_RE.fullmatch(
                supplied_fenced_sha256
            )
            and hashlib.sha256(
                json.dumps(
                    fenced_body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            == supplied_fenced_sha256
        ):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_fenced_prestart_revalidation_rejected"
            )
        self._fenced_prestart_receipt = dict(fenced_prestart)
        self._service_fence.assert_held()
        receipt = self._host.start_provider_loops(
            **self._provider_options(provider_options)
        )
        if not isinstance(receipt, Mapping):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_provider_start_receipt_invalid"
            )
        health = self._host.health()
        provider = health.get("provider_loop_supervisor")
        if not (
            isinstance(provider, Mapping)
            and provider.get("state") == "running"
            and provider.get("all_ready") is True
            and provider.get("provider_sockets_started") is True
            and not provider.get("failures")
        ):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_provider_not_ready"
            )
        self._provider_receipt = dict(receipt)
        self._service_fence.assert_held()
        self._runtime_handle = self._registrar(self._runtime)
        if not callable(getattr(self._runtime_handle, "close", None)):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_runtime_registration_unconfirmed"
            )

    def start_no_order_smoke(
        self,
        *,
        provider_options: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Start capture/runtime only; transport and live ticks stay absent."""

        if self._state is not CapturedPaperServiceState.PREPARED:
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_supervisor_start_is_one_shot"
            )
        try:
            self._start_capture_and_runtime(provider_options=provider_options)
            self._service_fence.assert_held()
            self._state = CapturedPaperServiceState.NO_ORDER_SMOKE
            return self.health()
        except BaseException:
            self._state = CapturedPaperServiceState.FAILED
            self._best_effort_rollback()
            raise

    def start_active(
        self,
        *,
        start_authority: CapturedPaperActiveStartAuthority,
        provider_options: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Start fully authorized fake-money PAPER; live ticks are last."""

        if self._state is not CapturedPaperServiceState.PREPARED:
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_supervisor_start_is_one_shot"
            )
        if (
            type(start_authority) is not CapturedPaperActiveStartAuthority
            or start_authority.expected_account_id
            != self._runtime.expected_account_id
            or start_authority.runtime_generation
            != self._runtime.runtime_generation
        ):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_active_start_authority_mismatch"
            )
        try:
            self._start_capture_and_runtime(provider_options=provider_options)
            self._service_fence.assert_held()
            # This intentionally runs *after* provider startup and runtime
            # registration.  Those bounded operations may consume meaningful
            # wall time, so authority checked only during composition is not
            # fresh enough to start broker/order workers.
            final_authority = start_authority.consume()
            if not isinstance(final_authority, Mapping) or not (
                final_authority.get("verdict")
                == "CAPTURED_ALPACA_PAPER_ACTIVE_START_AUTHORIZED"
                and final_authority.get("account_scope") == "alpaca:paper"
                and final_authority.get("expected_account_id")
                == self._runtime.expected_account_id
                and final_authority.get("runtime_generation")
                == self._runtime.runtime_generation
                and final_authority.get("paper_order_submission_authorized")
                is True
                and final_authority.get("launcher_attestation_consumed") is True
                and isinstance(
                    final_authority.get("host_activation_permit_sha256"), str
                )
                and len(final_authority["host_activation_permit_sha256"]) == 64
                and all(
                    character in "0123456789abcdef"
                    for character in final_authority[
                        "host_activation_permit_sha256"
                    ]
                )
                and final_authority.get("host_activation_permit_consumed") is True
                and final_authority.get("live_cash_authorized") is False
                and final_authority.get("real_money_authorized") is False
            ):
                raise CapturedPaperServiceSupervisorError(
                    "captured_paper_active_start_authority_rejected"
                )
            self._active_start_authority_receipt = dict(final_authority)
            for managed in self._workers:
                start_authority.assert_current()
                self._service_fence.assert_held()
                managed.worker.start()
                self._started_workers.append(managed)
                worker_health = _health_mapping(managed.worker.health())
                if (
                    worker_health.get("ever_started") is not True
                    or worker_health.get("fatal") is True
                ):
                    raise CapturedPaperServiceSupervisorError(
                        f"captured_paper_{managed.name}_start_unconfirmed"
                    )
                start_authority.assert_current()
            start_authority.assert_current()
            self._service_fence.assert_held()
            if self._live_loop_start() is not True:
                raise CapturedPaperServiceSupervisorError(
                    "captured_paper_live_loop_start_unconfirmed"
                )
            self._live_loop_started = True
            if self._live_loop_health() is not True:
                raise CapturedPaperServiceSupervisorError(
                    "captured_paper_live_loop_health_unconfirmed"
                )
            start_authority.assert_current()
            self._service_fence.assert_held()
            self._state = CapturedPaperServiceState.ACTIVE
            return self.assert_healthy()
        except BaseException:
            self._state = CapturedPaperServiceState.FAILED
            self._best_effort_rollback()
            raise

    def assert_healthy(self) -> Mapping[str, Any]:
        if self._state is not CapturedPaperServiceState.ACTIVE:
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_service_not_active"
            )
        self._service_fence.assert_held()
        host_health = self._host.health()
        provider = host_health.get("provider_loop_supervisor")
        if not (
            isinstance(provider, Mapping)
            and provider.get("state") == "running"
            and provider.get("all_ready") is True
            and not provider.get("failures")
        ):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_provider_health_lost"
            )
        for managed in self._started_workers:
            health = _health_mapping(managed.worker.health())
            if (
                health.get("running") is not True
                or health.get("fatal") is True
            ):
                raise CapturedPaperServiceSupervisorError(
                    f"captured_paper_{managed.name}_health_lost"
                )
        if self._live_loop_health() is not True:
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_live_loop_health_lost"
            )
        return self.health()

    def _close_runtime_with_retry(self, deadline: float) -> None:
        handle = self._runtime_handle
        if handle is None:
            return
        while True:
            try:
                handle.close()
                self._runtime_handle = None
                return
            except CapturedPaperRuntimeUnavailableError as exc:
                if (
                    "dispatch_in_flight" not in str(exc)
                    or self._monotonic() >= deadline
                ):
                    raise
                self._wait(min(0.05, max(0.0, deadline - self._monotonic())))

    def _close_host_with_retry(self, deadline: float) -> None:
        while True:
            try:
                self._host.close()
                return
            except Exception as exc:
                if (
                    "active capture runs" not in str(exc).lower()
                    or self._monotonic() >= deadline
                ):
                    raise
                self._wait(min(0.05, max(0.0, deadline - self._monotonic())))

    def close(
        self,
        *,
        join_timeout_seconds: float,
        quiesce_timeout_seconds: float,
    ) -> Mapping[str, Any]:
        for value, label in (
            (join_timeout_seconds, "join_timeout"),
            (quiesce_timeout_seconds, "quiesce_timeout"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
                or float(value) > 300.0
            ):
                raise CapturedPaperServiceSupervisorError(
                    f"captured_paper_{label}_invalid"
                )
        if self._state is CapturedPaperServiceState.STOPPED:
            return self.health()
        self._state = CapturedPaperServiceState.STOPPING
        failures: list[BaseException] = []
        if self._live_loop_started:
            try:
                self._live_loop_stop()
            except BaseException as exc:
                failures.append(exc)
            self._live_loop_started = False
        for managed in reversed(self._started_workers):
            try:
                managed.worker.close(
                    join_timeout_seconds=float(join_timeout_seconds)
                )
            except BaseException as exc:
                failures.append(exc)
        self._started_workers.clear()
        deadline = self._monotonic() + float(quiesce_timeout_seconds)
        try:
            self._close_runtime_with_retry(deadline)
        except BaseException as exc:
            failures.append(exc)
        try:
            self._close_host_with_retry(deadline)
        except BaseException as exc:
            failures.append(exc)
        # The process-wide exclusion fence is released last.  If any earlier
        # shutdown stage is unconfirmed, retain it so a generic Alpaca arm path
        # cannot race a partially quiesced captured runtime.  Process exit (or
        # a later successful close retry) releases the PostgreSQL session lock.
        if not failures and self._service_fence_receipt is not None:
            try:
                released = self._service_fence.release()
                if not (
                    isinstance(released, Mapping)
                    and released.get("account_scope") == "alpaca:paper"
                    and released.get("held") is False
                ):
                    raise CapturedPaperServiceSupervisorError(
                        "captured_paper_service_fence_release_unconfirmed"
                    )
                self._service_fence_receipt = None
            except BaseException as exc:
                failures.append(exc)
        if failures:
            self._state = CapturedPaperServiceState.FAILED
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_service_shutdown_incomplete:"
                + ",".join(type(exc).__name__ for exc in failures)
            ) from failures[0]
        self._state = CapturedPaperServiceState.STOPPED
        return self.health()

    def _best_effort_rollback(self) -> None:
        try:
            self.close(
                join_timeout_seconds=20.0,
                quiesce_timeout_seconds=20.0,
            )
        except BaseException:
            self._state = CapturedPaperServiceState.FAILED

    def health(self) -> Mapping[str, Any]:
        worker_health = {
            managed.name: _health_mapping(managed.worker.health())
            for managed in self._workers
        }
        return MappingProxyType(
            {
                "schema_version": self.HEALTH_SCHEMA_VERSION,
                "state": self._state.value,
                "account_scope": self._runtime.account_scope,
                "expected_account_id": self._runtime.expected_account_id,
                "runtime_generation": self._runtime.runtime_generation,
                "runtime_registered": self._runtime_handle is not None,
                "service_fence": dict(self._service_fence.health()),
                "service_fence_acquired": (
                    self._service_fence_receipt is not None
                ),
                "fenced_prestart_revalidated": (
                    self._fenced_prestart_receipt is not None
                ),
                "fenced_prestart_receipt_sha256": (
                    self._fenced_prestart_receipt.get("receipt_sha256")
                    if self._fenced_prestart_receipt is not None
                    else None
                ),
                "provider_started": self._provider_receipt is not None,
                "active_start_authority_consumed": (
                    self._active_start_authority_receipt is not None
                ),
                "live_loop_started": self._live_loop_started,
                "managed_workers": worker_health,
                "host": dict(self._host.health()),
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
        )


__all__ = (
    "CapturedPaperActiveStartAuthority",
    "CapturedPaperManagedWorker",
    "CapturedPaperServiceState",
    "CapturedPaperServiceSupervisor",
    "CapturedPaperServiceSupervisorError",
)
