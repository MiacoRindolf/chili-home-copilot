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
import os
import re
import tempfile
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from .captured_paper_dispatcher import (
    CapturedPaperRuntime,
    CapturedPaperRuntimeHandle,
    CapturedPaperRuntimeUnavailableError,
    register_captured_paper_runtime,
)


def _emit_supervisor_breadcrumb(step: str) -> None:
    """Fsync one start_active boundary marker to disk. Pure syscalls in a bare
    try/except: survives block-buffered stdout, os._exit, an external
    TerminateProcess, and a hang (the last line on disk names the blocked
    boundary). NO faulthandler/SEH — earlier faulthandler-based instruments
    crashed the run. Shares the service's breadcrumb file via
    CHILI_CAPTURED_PAPER_BREADCRUMB_PATH so supervisor lines interleave with the
    service's own BEGIN-6 breadcrumbs in call order."""
    try:
        path = os.environ.get("CHILI_CAPTURED_PAPER_BREADCRUMB_PATH") or os.path.join(
            tempfile.gettempdir(),
            "captured_alpaca_paper_service.service-breadcrumbs.log",
        )
        line = f"{time.time()} pid={os.getpid()} supervisor {step}\n".encode(
            "utf-8", "replace"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


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


def _sha256_json(value: Mapping[str, Any]) -> str:
    """Hash one canonical JSON object without accepting non-finite values."""

    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class CapturedPaperServiceSupervisor:
    """Own one process generation of the fake-money PAPER runtime."""

    HEALTH_SCHEMA_VERSION = "chili.captured-paper-service-supervisor-health.v2"
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
        active_pre_authority_workers: Sequence[
            CapturedPaperManagedWorker
        ] = (),
        post_quiesce_before_fence_release: (
            Callable[[], Mapping[str, Any]] | None
        ) = None,
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
        pre_authority_workers = tuple(active_pre_authority_workers)
        if any(
            type(item) is not CapturedPaperManagedWorker
            for item in (*pre_authority_workers, *workers)
        ):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_managed_worker_invalid"
            )
        names = tuple(
            item.name for item in (*pre_authority_workers, *workers)
        )
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
        if (
            post_quiesce_before_fence_release is not None
            and not callable(post_quiesce_before_fence_release)
        ):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_post_quiesce_callback_invalid"
            )
        self._host = host
        self._runtime = runtime
        self._service_fence = service_fence
        self._fenced_prestart_revalidate = fenced_prestart_revalidate
        self._pre_authority_workers = pre_authority_workers
        self._workers = workers
        self._live_loop_start = live_loop_start
        self._live_loop_stop = live_loop_stop
        self._live_loop_health = live_loop_health
        self._post_quiesce_before_fence_release = (
            post_quiesce_before_fence_release
        )
        self._registrar = runtime_registrar
        self._monotonic = monotonic_clock
        self._wait = wait
        self._state = CapturedPaperServiceState.PREPARED
        self._runtime_handle: Any | None = None
        self._started_pre_authority_workers: list[
            CapturedPaperManagedWorker
        ] = []
        self._started_workers: list[CapturedPaperManagedWorker] = []
        self._provider_receipt: Mapping[str, Any] | None = None
        self._service_fence_receipt: Mapping[str, Any] | None = None
        self._fenced_prestart_receipt: Mapping[str, Any] | None = None
        self._active_start_authority_receipt: Mapping[str, Any] | None = None
        self._post_quiesce_receipt: Mapping[str, Any] | None = None
        self._live_loop_started = False
        self._active_path_started = False

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
        _emit_supervisor_breadcrumb("BEGIN start_provider_loops")
        receipt = self._host.start_provider_loops(
            **self._provider_options(provider_options)
        )
        _emit_supervisor_breadcrumb("END start_provider_loops")
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
            self._active_path_started = True
            # Broker-incapable selection/capture work primes here, before the
            # short-lived final order authority is consumed.  Clone binding,
            # provider reads, async fsync and frontier catch-up can legitimately
            # exceed that authority window; none of these workers can POST.
            for managed in self._pre_authority_workers:
                self._service_fence.assert_held()
                _emit_supervisor_breadcrumb("BEGIN pre_auth_worker.start " + managed.name)
                managed.worker.start()
                _emit_supervisor_breadcrumb("END pre_auth_worker.start " + managed.name)
                self._started_pre_authority_workers.append(managed)
                worker_health = _health_mapping(managed.worker.health())
                if (
                    worker_health.get("ever_started") is not True
                    or worker_health.get("running") is not True
                    or worker_health.get("fatal") is True
                ):
                    raise CapturedPaperServiceSupervisorError(
                        f"captured_paper_{managed.name}_pre_authority_start_unconfirmed"
                    )
                self._service_fence.assert_held()
            # This intentionally runs *after* provider startup and runtime
            # registration *and* broker-incapable selection priming.  Those
            # bounded operations may consume meaningful wall time, so authority
            # checked only during composition is not fresh enough to start
            # broker/order workers.
            final_authority = start_authority.consume()
            authority_body = (
                dict(final_authority) if isinstance(final_authority, Mapping) else {}
            )
            supplied_authority_sha256 = str(
                authority_body.pop("authority_sha256", "") or ""
            )
            broker_fixed_point = authority_body.get("broker_fixed_point")
            final_kill_switch = authority_body.get("final_kill_switch_query")
            kill_switch_body = (
                dict(final_kill_switch)
                if isinstance(final_kill_switch, Mapping)
                else {}
            )
            supplied_kill_switch_sha256 = str(
                kill_switch_body.pop("query_receipt_sha256", "") or ""
            )
            expected_authority_fields = {
                "schema_version",
                "verdict",
                "account_scope",
                "expected_account_id",
                "runtime_generation",
                "activation_manifest_sha256",
                "kill_switch_receipt_sha256",
                "launcher_attestation_sha256",
                "launcher_attestation_consumed",
                "host_activation_permit_sha256",
                "host_activation_permit_consumed",
                "host_quiet_horizon_event_sha256",
                "broker_fixed_point",
                "broker_fixed_point_sha256",
                "post_permit_broker_snapshot_sha256",
                "order_transition_fence_sha256",
                "fill_activity_fence_sha256",
                "final_kill_switch_query",
                "final_kill_switch_query_sha256",
                "paper_order_submission_authorized",
                "live_cash_authorized",
                "real_money_authorized",
            }
            try:
                authority_hash_valid = (
                    self._SHA256_RE.fullmatch(supplied_authority_sha256)
                    and _sha256_json(authority_body) == supplied_authority_sha256
                )
                broker_hash_valid = (
                    isinstance(broker_fixed_point, Mapping)
                    and _sha256_json(dict(broker_fixed_point))
                    == authority_body.get("broker_fixed_point_sha256")
                    and _sha256_json(dict(broker_fixed_point["second_snapshot"]))
                    == authority_body.get("post_permit_broker_snapshot_sha256")
                    and _sha256_json(dict(broker_fixed_point["second_order_census"]))
                    == authority_body.get("order_transition_fence_sha256")
                    and _sha256_json(
                        dict(broker_fixed_point["second_fill_activity_census"])
                    )
                    == authority_body.get("fill_activity_fence_sha256")
                )
                kill_switch_hash_valid = (
                    isinstance(final_kill_switch, Mapping)
                    and self._SHA256_RE.fullmatch(supplied_kill_switch_sha256)
                    and _sha256_json(kill_switch_body)
                    == supplied_kill_switch_sha256
                    and _sha256_json(dict(final_kill_switch))
                    == authority_body.get("final_kill_switch_query_sha256")
                )
            except (KeyError, TypeError, ValueError):
                authority_hash_valid = False
                broker_hash_valid = False
                kill_switch_hash_valid = False
            if not isinstance(final_authority, Mapping) or not (
                set(authority_body) == expected_authority_fields
                and authority_hash_valid
                and broker_hash_valid
                and kill_switch_hash_valid
                and authority_body.get("schema_version")
                == "chili.captured-paper-active-start-authority.v2"
                and authority_body.get("verdict")
                == "CAPTURED_ALPACA_PAPER_ACTIVE_START_AUTHORIZED"
                and authority_body.get("account_scope") == "alpaca:paper"
                and authority_body.get("expected_account_id")
                == self._runtime.expected_account_id
                and authority_body.get("runtime_generation")
                == self._runtime.runtime_generation
                and authority_body.get("paper_order_submission_authorized")
                is True
                and authority_body.get("launcher_attestation_consumed") is True
                and authority_body.get("host_activation_permit_consumed") is True
                and all(
                    self._SHA256_RE.fullmatch(str(authority_body.get(field) or ""))
                    for field in (
                        "activation_manifest_sha256",
                        "kill_switch_receipt_sha256",
                        "launcher_attestation_sha256",
                        "host_activation_permit_sha256",
                        "post_permit_broker_snapshot_sha256",
                        "order_transition_fence_sha256",
                        "fill_activity_fence_sha256",
                        "host_quiet_horizon_event_sha256",
                        "broker_fixed_point_sha256",
                        "final_kill_switch_query_sha256",
                    )
                )
                and broker_fixed_point.get("schema_version")
                == "chili.captured-paper-broker-fixed-point.v1"
                and broker_fixed_point.get("verdict")
                == "PAPER_BROKER_QUIET_FIXED_POINT"
                and broker_fixed_point.get("account_scope") == "alpaca:paper"
                and broker_fixed_point.get("expected_account_id")
                == self._runtime.expected_account_id
                and broker_fixed_point.get("activation_generation")
                == self._runtime.runtime_generation
                and broker_fixed_point.get("assumption_bound") is True
                and broker_fixed_point.get("live_cash_certification") is False
                and final_kill_switch.get("account_scope") == "alpaca:paper"
                and final_kill_switch.get("expected_account_id")
                == self._runtime.expected_account_id
                and final_kill_switch.get("activation_generation")
                == self._runtime.runtime_generation
                and final_kill_switch.get("active") is False
                and authority_body.get("live_cash_authorized") is False
                and authority_body.get("real_money_authorized") is False
            ):
                raise CapturedPaperServiceSupervisorError(
                    "captured_paper_active_start_authority_rejected"
                )
            self._active_start_authority_receipt = dict(final_authority)
            for managed in self._workers:
                start_authority.assert_current()
                self._service_fence.assert_held()
                _emit_supervisor_breadcrumb("BEGIN worker.start " + managed.name)
                managed.worker.start()
                _emit_supervisor_breadcrumb("END worker.start " + managed.name)
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
            _emit_supervisor_breadcrumb("BEGIN live_loop_start")
            if self._live_loop_start() is not True:
                raise CapturedPaperServiceSupervisorError(
                    "captured_paper_live_loop_start_unconfirmed"
                )
            _emit_supervisor_breadcrumb("END live_loop_start")
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
        for managed in (
            *self._started_pre_authority_workers,
            *self._started_workers,
        ):
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

    def _consume_post_quiesce_receipt(self) -> None:
        callback = self._post_quiesce_before_fence_release
        if (
            callback is None
            or not self._active_path_started
            or self._post_quiesce_receipt is not None
        ):
            return
        raw = callback()
        body = dict(raw) if isinstance(raw, Mapping) else {}
        supplied_sha256 = str(body.pop("receipt_sha256", "") or "")
        expected_keys = {
            "schema_version",
            "verdict",
            "account_scope",
            "expected_account_id",
            "runtime_generation",
            "workers_stopped",
            "runtime_unregistered",
            "provider_stopped",
            "application_outcome",
            "strategy_variants_deactivated",
            "variant_application_sha256",
            "variant_rollback_sha256",
            "target_variant_ids",
            "selection_runtime_rollback_sha256",
            "paper_order_submission_authorized",
            "live_cash_authorized",
            "real_money_authorized",
        }
        target_variant_ids = body.get("target_variant_ids")
        runtime_rollback_body = {
            "schema_version": (
                "chili.captured-paper-selection-runtime-rollback.v2"
            ),
            "account_scope": body.get("account_scope"),
            "expected_account_id": body.get("expected_account_id"),
            "activation_generation": body.get("runtime_generation"),
            "variant_application_sha256": body.get(
                "variant_application_sha256"
            ),
            "variant_rollback_sha256": body.get("variant_rollback_sha256"),
            "target_variant_ids": target_variant_ids,
            "application_outcome": body.get("application_outcome"),
            "strategy_variants_deactivated": body.get(
                "strategy_variants_deactivated"
            ),
            "paper_order_submission_authorized": body.get(
                "paper_order_submission_authorized"
            ),
            "live_cash_authorized": body.get("live_cash_authorized"),
            "real_money_authorized": body.get("real_money_authorized"),
        }
        if not (
            isinstance(raw, Mapping)
            and set(body) == expected_keys
            and body.get("schema_version")
            == "chili.captured-paper-post-quiesce.v3"
            and (
                (
                    body.get("application_outcome") == "rolled_back"
                    and body.get("verdict")
                    == "CAPTURED_PAPER_SELECTION_BINDINGS_ROLLED_BACK"
                    and body.get("strategy_variants_deactivated") is True
                    and isinstance(target_variant_ids, list)
                    and bool(target_variant_ids)
                )
                or (
                    body.get("application_outcome") == "not_applied"
                    and body.get("verdict")
                    == "CAPTURED_PAPER_SELECTION_APPLICATION_NOT_APPLIED"
                    and body.get("strategy_variants_deactivated") is False
                    and target_variant_ids == []
                )
            )
            and body.get("account_scope") == "alpaca:paper"
            and body.get("expected_account_id")
            == self._runtime.expected_account_id
            and body.get("runtime_generation")
            == self._runtime.runtime_generation
            and body.get("workers_stopped") is True
            and body.get("runtime_unregistered") is True
            and body.get("provider_stopped") is True
            and self._SHA256_RE.fullmatch(
                str(body.get("variant_application_sha256") or "")
            )
            and self._SHA256_RE.fullmatch(
                str(body.get("variant_rollback_sha256") or "")
            )
            and isinstance(target_variant_ids, list)
            and all(
                type(value) is int and value > 0
                for value in target_variant_ids
            )
            and target_variant_ids == sorted(set(target_variant_ids))
            and self._SHA256_RE.fullmatch(
                str(body.get("selection_runtime_rollback_sha256") or "")
            )
            and _sha256_json(runtime_rollback_body)
            == body.get("selection_runtime_rollback_sha256")
            and body.get("paper_order_submission_authorized") is False
            and body.get("live_cash_authorized") is False
            and body.get("real_money_authorized") is False
            and self._SHA256_RE.fullmatch(supplied_sha256)
            and hashlib.sha256(
                json.dumps(
                    body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            == supplied_sha256
        ):
            raise CapturedPaperServiceSupervisorError(
                "captured_paper_post_quiesce_receipt_rejected"
            )
        self._post_quiesce_receipt = dict(raw)

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
        for managed in reversed(self._started_pre_authority_workers):
            try:
                managed.worker.close(
                    join_timeout_seconds=float(join_timeout_seconds)
                )
            except BaseException as exc:
                failures.append(exc)
        self._started_pre_authority_workers.clear()
        deadline = self._monotonic() + float(quiesce_timeout_seconds)
        try:
            self._close_runtime_with_retry(deadline)
        except BaseException as exc:
            failures.append(exc)
        try:
            self._close_host_with_retry(deadline)
        except BaseException as exc:
            failures.append(exc)
        # Strategy-clone deactivation is permitted only after the live loop,
        # every managed worker, the dispatch runtime, and the capture host are
        # quiesced.  It still runs while the process-wide PostgreSQL exclusion
        # fence is held; a failed/ambiguous rollback therefore retains that
        # fence and cannot race another PAPER generation.
        if not failures:
            try:
                self._consume_post_quiesce_receipt()
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
            for managed in (*self._pre_authority_workers, *self._workers)
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
                "active_start_authority_sha256": (
                    self._active_start_authority_receipt.get("authority_sha256")
                    if self._active_start_authority_receipt is not None
                    else None
                ),
                "active_start_evidence_artifact_sha256": (
                    _sha256_json(dict(self._active_start_authority_receipt))
                    if self._active_start_authority_receipt is not None
                    else None
                ),
                "post_quiesce_completed": (
                    self._post_quiesce_receipt is not None
                ),
                "post_quiesce_receipt_sha256": (
                    self._post_quiesce_receipt.get("receipt_sha256")
                    if self._post_quiesce_receipt is not None
                    else None
                ),
                "live_loop_started": self._live_loop_started,
                "managed_workers": worker_health,
                "active_pre_authority_worker_names": [
                    managed.name for managed in self._pre_authority_workers
                ],
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
