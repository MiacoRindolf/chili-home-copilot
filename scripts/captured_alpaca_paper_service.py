"""Dedicated, hash-bound captured Alpaca PAPER service entry point.

Only standard-library and offline contract modules are imported before the
runtime environment is projected.  In particular, ``app.config`` cannot see
the desktop process' live-cash credentials: the allowlisted PAPER environment
is installed first and then revalidated against the activation envelope.

The no-order smoke consumes a typed preactivation envelope that structurally
denies broker POSTs.  Active fake-money PAPER consumes only the separately
finalized activation envelope that hash-binds the successful no-order receipt.
A launcher therefore cannot turn validation or smoke authority into broker
authority merely by changing one CLI flag.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import traceback
from types import ModuleType
from typing import Any, Callable, ContextManager, Mapping, Sequence
import uuid

from scripts import captured_paper_activation_contract as activation_contract
from scripts import captured_paper_readiness_evidence as readiness_evidence
from scripts import captured_paper_runtime_env as runtime_env


UTC = timezone.utc
SERVICE_REPORT_SCHEMA_VERSION = "chili.captured-paper-service-report.v1"
_MODES = ("validate-only", "no-order-smoke", "activate-paper")
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVICE_HEALTH_POLL_SECONDS = 1.0
_SERVICE_SHUTDOWN_SECONDS = 30.0
_MAX_STARTUP_RECONCILIATION_ROWS = 10_000
_RESTART_GATE_SCHEMA_VERSION = "chili.captured-paper-restart-gate.v1"
_NO_ORDER_SMOKE_SCHEMA_VERSION = "chili.captured-paper-readiness.no_order_smoke.v4"
# 2026-07-17: the post-smoke receipt window must cover the whole
# finalize -> cutover ValidateOnly/Apply -> tape gate -> ActivatePaper tail
# (~2-4 minutes observed live).  The prior 30s window made that tail
# impossible by construction: the final manifest inherits
# min(sealed.expires_at, no_order_expires_at), so cutover always saw
# MANIFEST_STALE (first observed on the first-ever green finalize,
# generation 35c79d11).  10 minutes matches the contract's
# _RECEIPT_MAX_AGE_SECONDS mid-flow class and stays under the 15-minute
# manifest-age cap.
_POST_SMOKE_RECEIPT_WINDOW_SECONDS = 10 * 60
_SERVICE_SINGLETON_NAME = "Global\\CHILI-Captured-Alpaca-PAPER-SERVICE-OWNER"
_LAUNCH_ATTESTATION_SCHEMA_VERSION = (
    "chili.captured-paper-launcher-cutover-attestation.v1"
)
_HOST_PREPARED_SCHEMA_VERSION = (
    "chili.captured-paper-host-startup-prepared.v1"
)
_HOST_ACTIVATION_PERMIT_SCHEMA_VERSION = (
    "chili.captured-paper-host-startup-permit.v1"
)
_HOST_STARTED_SCHEMA_VERSION = "chili.captured-paper-host-startup-started.v2"
_ACTIVE_START_AUTHORITY_SCHEMA_VERSION = (
    "chili.captured-paper-active-start-authority.v2"
)
_HOST_CUTOVER_JOURNAL_EVENT_SCHEMA_VERSION = (
    "chili.captured-paper-host-cutover-journal-event.v1"
)
_HOST_CUTOVER_APPLY_CONFIRMATION = "CUTOVER_FAKE_MONEY_ALPACA_PAPER"
_HOST_ACTIVATION_MAX_AGE_SECONDS = 30.0
_HOST_ACTIVATION_WAIT_SECONDS = 30.0
_ACTIVE_START_EVIDENCE_MAX_BYTES = 512 * 1024
_LEGACY_PAPER_BROKER_QUIET_HORIZON_SECONDS = 30.0
_LEGACY_PAPER_BROKER_QUIET_HORIZON_POLICY = (
    "alpaca-paper-assumption-bound-quiet-horizon.v1"
)
_PAPER_BROKER_FIXED_POINT_SEPARATION_SECONDS = 5.0
_HOST_CUTOVER_JOURNAL_MAX_BYTES = 4 * 1024 * 1024
_MAX_ISOLATED_DEPENDENCY_FILES = 8192
_MAX_ISOLATED_DEPENDENCY_BYTES = 512 * 1024 * 1024
_ISOLATED_DEPENDENCY_EXCLUSION_POLICY = "exclude-__pycache__-pyc-pyo.v1"
_ISOLATED_STAGE0_SCHEMA_VERSION = "chili.captured-paper-isolated-stage0.v2"
_ISOLATED_STAGE0_ATTESTATION_KEYS = frozenset(
    {
        "schema_version",
        "stage0_path",
        "target_path",
        "target_sha256",
        "target_role",
        "candidate_root",
        "dependency_root",
        "dependency_exclusion_policy",
        "dependency_file_count",
        "dependency_mutation_guard_mode",
        "dependency_tree_sha256",
        "dependency_tree_total_bytes",
        "dependency_root_identity_sha256",
        "local_module_count",
        "local_roster_sha256",
        "python_executable_path",
        "python_executable_sha256",
        "manifest_path",
        "manifest_sha256",
        "code_build_sha256",
    }
)
_HOST_DISPATCH_LOCK_IDENTITY_KEYS = frozenset(
    {
        "dispatch_lock_path",
        "dispatch_lock_st_dev",
        "dispatch_lock_st_ino",
        "dispatch_lock_size_bytes",
        "dispatch_lock_byte_sha256",
    }
)


class CapturedAlpacaPaperServiceError(RuntimeError):
    """Sanitized startup rejection before a PAPER transport is constructed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


@dataclass(slots=True)
class _CapturedPaperServiceComposition:
    """Already-constructed PAPER-only runtime resources.

    Construction is permitted only after the runtime environment and every
    source hash have been checked.  The shared capture store remains external
    to the supervisor and is therefore closed explicitly after the host has
    released all symbol leases.
    """

    supervisor: Any
    shared_capture_store: Any
    adapter: Any
    connection_generation_receipt: Mapping[str, Any]
    phase_one_reconciliation_receipt: Mapping[str, Any]
    restart_inventory_receipt: Mapping[str, Any]
    database_engine: Any
    initial_broker_snapshot: Mapping[str, Any] | None = None

    def close_shared_capture_store(self) -> None:
        self.shared_capture_store.close()


@dataclass(frozen=True, slots=True)
class _CapturedPaperPolicyAuthority:
    policy_receipt: Any
    policy_spec: Any
    operational_policy: Any
    feature_flags: Mapping[str, Any]
    feature_flags_sha256: str


@dataclass(frozen=True, slots=True)
class _PreparedCapturedPaperCapture:
    preflight: Any
    host: Any
    shared_store: Any
    adapter: Any
    broker_snapshot: Mapping[str, Any]
    policy_authority: _CapturedPaperPolicyAuthority


class _CapturedPaperServiceSingleton:
    """Second, service-owned cross-process singleton.

    The PowerShell launcher owns its own stable mutex.  This distinct mutex is
    acquired by Python itself, so invoking the service module through another
    host cannot create a second worker owner merely by supplying the genuine
    launcher path and hash on the command line.
    """

    def __init__(self, name: str = _SERVICE_SINGLETON_NAME) -> None:
        self._name = str(name)
        self._handle: Any | None = None
        self._fallback_file: Any | None = None

    def acquire(self) -> None:
        if self._handle is not None or self._fallback_file is not None:
            raise CapturedAlpacaPaperServiceError(
                "SERVICE_SINGLETON_REUSED",
                "captured PAPER service singleton is one-shot",
            )
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            create_mutex.restype = wintypes.HANDLE
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            ctypes.set_last_error(0)
            handle = create_mutex(None, True, self._name)
            if not handle:
                raise CapturedAlpacaPaperServiceError(
                    "SERVICE_SINGLETON_UNAVAILABLE",
                    "captured PAPER service mutex could not be created",
                )
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                close_handle(handle)
                raise CapturedAlpacaPaperServiceError(
                    "SERVICE_SINGLETON_HELD",
                    "another captured PAPER Python service already exists",
                )
            self._handle = (kernel32, handle)
            return

        # Test/development portability.  The production Windows path above is
        # the only supported activation host.
        import fcntl

        path = Path(tempfile.gettempdir()) / "chili-captured-paper-service.lock"
        handle = path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise CapturedAlpacaPaperServiceError(
                "SERVICE_SINGLETON_HELD",
                "another captured PAPER Python service already exists",
            ) from exc
        self._fallback_file = handle

    def close(self) -> None:
        if self._handle is not None:
            kernel32, handle = self._handle
            self._handle = None
            with suppress(Exception):
                kernel32.ReleaseMutex(handle)
            with suppress(Exception):
                kernel32.CloseHandle(handle)
        if self._fallback_file is not None:
            handle = self._fallback_file
            self._fallback_file = None
            with suppress(Exception):
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            with suppress(Exception):
                handle.close()


class _CapturedPaperLauncherCutoverAttestation:
    """Process-bound, expiring, one-shot launcher/cutover proof."""

    __slots__ = (
        "_body",
        "_expires_at",
        "_process_probe",
        "_cutover_probe",
        "_lock",
        "_consumed",
        "attestation_sha256",
    )

    def __init__(
        self,
        *,
        body: Mapping[str, Any],
        expires_at: datetime,
        process_probe: Callable[[], Mapping[str, Any]],
        cutover_probe: Callable[[], Mapping[str, Any]],
    ) -> None:
        self._body = dict(body)
        self._expires_at = _aware_utc(expires_at, "launcher attestation expiry")
        self._process_probe = process_probe
        self._cutover_probe = cutover_probe
        self._lock = threading.Lock()
        self._consumed = False
        self.attestation_sha256 = activation_contract.sha256_json(self._body)

    def consume(
        self,
        *,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> Mapping[str, Any]:
        with self._lock:
            if self._consumed:
                raise CapturedAlpacaPaperServiceError(
                    "LAUNCH_ATTESTATION_ALREADY_CONSUMED",
                    "launcher/cutover authority is one-shot",
                )
            now = _aware_utc(wall_clock(), "launcher attestation consume clock")
            if now >= self._expires_at:
                raise CapturedAlpacaPaperServiceError(
                    "LAUNCH_ATTESTATION_EXPIRED",
                    "launcher/cutover authority expired before worker start",
                )
            current = _normalized_process_evidence(self._process_probe())
            expected = self._body["process_binding"]
            if any(current.get(key) != value for key, value in expected.items()):
                raise CapturedAlpacaPaperServiceError(
                    "LAUNCH_PROCESS_BINDING_DRIFT",
                    "service or launcher process identity changed before worker start",
                )
            current_cutover = _normalized_cutover_evidence(self._cutover_probe())
            if current_cutover != self._body["cutover_binding"]:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_CUTOVER_BINDING_DRIFT",
                    "candidate/legacy task or bridge-process state changed before workers",
                )
            self._consumed = True
            return {
                "schema_version": _LAUNCH_ATTESTATION_SCHEMA_VERSION,
                "attestation_sha256": self.attestation_sha256,
                "consumed_at": _iso(now),
                "launcher_attestation_consumed": True,
            }


def _plain_json_value(value: Any) -> Any:
    """I-unwrap ang Mapping subclasses (MappingProxyType!) at tuples sa purong
    JSON containers. 2026-07-17: ang json.dumps ay HINDI tumatanggap ng
    mappingproxy, taliwas sa inasahan ng canonical-round-trip comment sa
    _build_policy_authority (naranasan live: REPORT_NOT_CANONICAL sa unang
    tunay na NoOrderSmoke boot). Values lang ang ina-unwrap — walang binabago
    sa nilalaman."""

    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _plain_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapturedAlpacaPaperServiceError(
            "REPORT_NOT_CANONICAL", "service report is not canonical JSON"
        ) from exc


def _strict_json_value(raw: bytes, *, field: str) -> Any:
    """Decode JSON while rejecting duplicate object keys and non-finite values."""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON value: {value}")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CapturedAlpacaPaperServiceError(
            "HOST_ACTIVATION_ARTIFACT_INVALID",
            f"{field} is not strict JSON",
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_process_path(value: Any, field: str) -> str:
    try:
        path = Path(str(value or "")).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapturedAlpacaPaperServiceError(
            "PROCESS_PATH_INVALID", f"{field} is not an existing local path"
        ) from exc
    return os.path.normcase(str(path)).replace("/", "\\")


def _canonical_uncreated_local_path(value: Any, field: str) -> str:
    path = Path(str(value or ""))
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise CapturedAlpacaPaperServiceError(
            "PROCESS_PATH_INVALID", f"{field} is not an absolute local path"
        )
    try:
        parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapturedAlpacaPaperServiceError(
            "PROCESS_PATH_INVALID", f"{field} parent is unavailable"
        ) from exc
    return os.path.normcase(str(parent / path.name)).replace("/", "\\")


def _default_process_probe() -> Mapping[str, Any]:
    """Read this process and its still-running PowerShell launcher parent."""

    try:
        import psutil

        process = psutil.Process(os.getpid())
        parent = process.parent()
        if parent is None:
            raise RuntimeError("launcher parent is absent")
        return {
            "pid": process.pid,
            "process_create_time": process.create_time(),
            "parent_pid": parent.pid,
            "parent_create_time": parent.create_time(),
            "python_executable_path": process.exe(),
            # sys.argv is the Python script contract. psutil.cmdline() also
            # contains interpreter switches (for example ``-B``), which are
            # separately sealed by the parent launcher projection.
            "service_argv": list(sys.argv),
            "working_directory": process.cwd(),
            "parent_executable_path": parent.exe(),
            "parent_cmdline": parent.cmdline(),
        }
    except Exception as exc:
        raise CapturedAlpacaPaperServiceError(
            "LAUNCH_PROCESS_NOT_INSPECTABLE",
            "service/launcher process identity could not be inspected",
        ) from exc


def _normalized_process_evidence(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "LAUNCH_PROCESS_NOT_INSPECTABLE", "process evidence is not a mapping"
        )
    service_argv = value.get("service_argv")
    parent_cmdline = value.get("parent_cmdline")
    if (
        not isinstance(service_argv, (list, tuple))
        or not service_argv
        or any(not isinstance(item, str) for item in service_argv)
        or not isinstance(parent_cmdline, (list, tuple))
        or not parent_cmdline
        or any(not isinstance(item, str) for item in parent_cmdline)
    ):
        raise CapturedAlpacaPaperServiceError(
            "LAUNCH_PROCESS_NOT_INSPECTABLE", "process command line is unavailable"
        )
    try:
        pid = int(value.get("pid"))
        parent_pid = int(value.get("parent_pid"))
        created = float(value.get("process_create_time"))
        parent_created = float(value.get("parent_create_time"))
    except (TypeError, ValueError) as exc:
        raise CapturedAlpacaPaperServiceError(
            "LAUNCH_PROCESS_NOT_INSPECTABLE", "process identity is invalid"
        ) from exc
    if (
        pid != os.getpid()
        or parent_pid != os.getppid()
        or parent_pid == pid
        or not math.isfinite(created)
        or not math.isfinite(parent_created)
        or parent_created > created
    ):
        raise CapturedAlpacaPaperServiceError(
            "LAUNCH_PROCESS_BINDING_INVALID", "process ancestry is not credible"
        )
    python_path = _canonical_process_path(
        value.get("python_executable_path"), "python executable"
    )
    parent_path = _canonical_process_path(
        value.get("parent_executable_path"), "launcher executable"
    )
    working_directory = _canonical_process_path(
        value.get("working_directory"), "service working directory"
    )
    return {
        "pid": pid,
        "process_create_time": created,
        "parent_pid": parent_pid,
        "parent_create_time": parent_created,
        "python_executable_path": python_path,
        "python_executable_sha256": _sha256_file(Path(python_path)),
        "service_argv_sha256": activation_contract.sha256_json(list(service_argv)),
        "working_directory": working_directory,
        "parent_executable_path": parent_path,
        "parent_cmdline_sha256": activation_contract.sha256_json(
            list(parent_cmdline)
        ),
    }


def _normalized_cutover_evidence(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "HOST_CUTOVER_NOT_INSPECTABLE", "host cutover evidence is unavailable"
        )
    expected_tasks = {
        "CHILI-IQFeed-Depth-Bridge-Daily",
        "CHILI-IQFeed-Depth-Bridge-Logon",
        "CHILI-IQFeed-Trade-Bridge-Daily",
        "CHILI-IQFeed-Trade-Bridge-Logon",
    }
    legacy = value.get("legacy_task_enabled")
    processes = value.get("legacy_bridge_processes")
    recreator_processes = value.get("legacy_recreator_processes")
    lane = value.get("legacy_execution_lane")
    if (
        not isinstance(legacy, Mapping)
        or set(legacy) != expected_tasks
        or any(type(legacy[name]) is not bool for name in expected_tasks)
        or not isinstance(processes, (list, tuple))
        or any(not isinstance(item, str) for item in processes)
        or not isinstance(recreator_processes, (list, tuple))
        or any(not isinstance(item, str) for item in recreator_processes)
        or not isinstance(lane, Mapping)
    ):
        raise CapturedAlpacaPaperServiceError(
            "HOST_CUTOVER_NOT_INSPECTABLE", "host cutover inventory is malformed"
        )
    raw_recreator_tasks = lane.get("recreator_tasks")
    expected_recreator_tasks = {
        "CHILI-Docker-Socket-Guard",
        "CHILI-captured-paper-premarket-activation",
        "CHILI-liveness-watchdog",
        "CHILI-Premarket-Readiness",
        "CHILI-Premarket-Readiness-Recheck",
    }
    if not isinstance(raw_recreator_tasks, list):
        raise CapturedAlpacaPaperServiceError(
            "HOST_CUTOVER_NOT_INSPECTABLE",
            "external execution-lane authorities are unavailable",
        )
    recreator_tasks = []
    for item in raw_recreator_tasks:
        if not isinstance(item, Mapping) or set(item) != {
            "name",
            "definition_sha256",
            "action_sha256",
            "source_chain_sha256",
            "enabled",
        }:
            raise CapturedAlpacaPaperServiceError(
                "HOST_CUTOVER_NOT_INSPECTABLE",
                "external execution-lane authority is malformed",
            )
        if type(item.get("enabled")) is not bool:
            raise CapturedAlpacaPaperServiceError(
                "HOST_CUTOVER_NOT_INSPECTABLE",
                "external execution-lane authority state is malformed",
            )
        recreator_tasks.append(
            {
                "name": str(item.get("name") or ""),
                "definition_sha256": _require_sha256(
                    item.get("definition_sha256"),
                    "external execution-lane task definition",
                ),
                "action_sha256": _require_sha256(
                    item.get("action_sha256"),
                    "external execution-lane task action",
                ),
                "source_chain_sha256": _require_sha256(
                    item.get("source_chain_sha256"),
                    "external execution-lane task source chain",
                ),
                "enabled": bool(item["enabled"]),
            }
        )
    recreator_tasks.sort(key=lambda item: item["name"])
    if (
        {item["name"] for item in recreator_tasks}
        != expected_recreator_tasks
        or len(recreator_tasks) != len(expected_recreator_tasks)
    ):
        raise CapturedAlpacaPaperServiceError(
            "HOST_CUTOVER_NOT_INSPECTABLE",
            "external execution-lane authority roster is incomplete",
        )
    lane_document = {
        "schema_version": str(lane.get("schema_version") or ""),
        "container_name": str(lane.get("container_name") or ""),
        "container_id": _require_sha256(
            lane.get("container_id"), "legacy Docker container ID"
        ),
        "image_id": str(lane.get("image_id") or "").lower(),
        "config_sha256": _require_sha256(
            lane.get("config_sha256"), "legacy Docker config"
        ),
        "execution_scope": str(lane.get("execution_scope") or ""),
        "scope_sha256": _require_sha256(
            lane.get("scope_sha256"), "legacy Docker execution scope"
        ),
        "recreator_tasks": recreator_tasks,
        "state": str(lane.get("state") or ""),
    }
    if set(lane) != set(lane_document) or not (
        lane_document["schema_version"]
        == "chili.legacy-execution-lane-observation.v2"
        and lane_document["container_name"]
        == "chili-clean-recovery-momentum-exec"
        and lane_document["image_id"].startswith("sha256:")
        and _require_sha256(
            lane_document["image_id"].removeprefix("sha256:"),
            "legacy Docker image ID",
        )
        and lane_document["state"] == "stopped"
        and lane_document["execution_scope"]
        == "legacy:mixed-paper-config-live-masters-disabled"
        and not any(item["enabled"] for item in recreator_tasks)
        and _require_sha256(
            value.get("legacy_execution_lane_sha256"),
            "legacy Docker observation",
        )
        == activation_contract.sha256_json(lane_document)
    ):
        raise CapturedAlpacaPaperServiceError(
            "HOST_CUTOVER_NOT_INSPECTABLE",
            "legacy Docker execution lane identity is incomplete or runnable",
        )
    normalized = {
        "candidate_task_name": str(value.get("candidate_task_name") or ""),
        "candidate_task_enabled": value.get("candidate_task_enabled"),
        "candidate_task_xml_sha256": _require_sha256(
            value.get("candidate_task_xml_sha256"), "candidate task XML"
        ),
        "candidate_task_action_sha256": _require_sha256(
            value.get("candidate_task_action_sha256"), "candidate task action"
        ),
        "legacy_task_enabled": {
            name: bool(legacy[name]) for name in sorted(expected_tasks)
        },
        "legacy_bridge_processes": sorted(processes),
        "legacy_recreator_processes": sorted(recreator_processes),
        "legacy_execution_lane": lane_document,
        "legacy_execution_lane_sha256": activation_contract.sha256_json(
            lane_document
        ),
    }
    if not (
        normalized["candidate_task_name"] == "CHILI-Captured-Alpaca-PAPER"
        and normalized["candidate_task_enabled"] is True
        and all(value is False for value in normalized["legacy_task_enabled"].values())
        and normalized["legacy_bridge_processes"] == []
        and normalized["legacy_recreator_processes"] == []
    ):
        raise CapturedAlpacaPaperServiceError(
            "HOST_CUTOVER_INCOMPLETE",
            "candidate task is not sole owner or legacy capture remains runnable",
        )
    return normalized


def _default_cutover_probe(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    projection: Mapping[str, Any],
    expected_parent_tail: Sequence[str],
    parent_executable_path: str,
) -> Mapping[str, Any]:
    """Re-inventory the applied read-only Task Scheduler/process state."""

    try:
        host_cutover = importlib.import_module("scripts.captured_paper_host_cutover")
        _verify_loaded_module_role(
            verified,
            role="captured_paper_host_cutover",
            module=host_cutover,
        )
        backend = host_cutover.WindowsHostCutoverBackend(bindings=())
        candidate = backend.get_task(host_cutover.CANDIDATE_TASK_NAME)
        if candidate is None:
            raise RuntimeError("candidate task is absent")
        if candidate.enabled is not True:
            raise RuntimeError("candidate task is not enabled")
        # 2026-07-17: do NOT run the authored-template structural validator on
        # the /Query readback — Task Scheduler re-serializes registered XML
        # (adds <URI>, canonicalizes the principal), so "sections are not
        # exact" is guaranteed on a real host (this rejected every boot
        # attempt of generation a816ac1a via task RestartOnFailure).  The
        # action-vs-own-argv comparison below IS the attestation: the sealed
        # template was structurally validated by the cutover before /Create,
        # and this process's argv descends from the sealed launcher chain.
        command, arguments = host_cutover._task_exec_from_xml(candidate.xml)
        if (
            _canonical_process_path(command, "candidate task executable")
            != _canonical_process_path(
                parent_executable_path, "launcher parent executable"
            )
            # Case-insensitive: the registered XML carries the sealed
            # template's normcased path tokens while this process's argv is
            # filesystem-resolved (2026-07-17 bug class).
            or os.path.normcase(arguments)
            != os.path.normcase(
                host_cutover._quote_windows_arguments(tuple(expected_parent_tail))
            )
        ):
            raise RuntimeError("candidate task action differs from this process")
        legacy = {}
        for name in host_cutover.REQUIRED_LEGACY_TASKS:
            task = backend.get_task(name)
            if task is None:
                raise RuntimeError(f"legacy task disappeared: {name}")
            legacy[name] = task.enabled

        lane = backend.inspect_legacy_execution_lane()
        lane_document = dict(host_cutover._legacy_execution_lane_document(lane))
        recreator_processes = list(
            backend.await_execution_lane_recreator_processes(
                timeout_seconds=0.0
            )
        )

        import psutil

        bridge_processes: list[str] = []
        bridge_names = {"iqfeed_trade_bridge.py", "iqfeed_depth_bridge.py"}
        # 2026-07-17: prefilter by process name — cmdline() on every PID
        # raises AccessDenied on protected system processes for any caller
        # (same impossible-by-construction class fixed in the cutover's
        # find_legacy_processes).  Only a python.exe process can be a legacy
        # bridge; an uninspectable python process stays fail-closed.
        for process in psutil.process_iter(("pid", "name"), ad_value=None):
            name = process.info.get("name")
            if name is None:
                raise RuntimeError(
                    f"a process name could not be inspected (PID {process.info['pid']})"
                )
            if str(name).casefold() != "python.exe":
                continue
            try:
                cmdline = process.cmdline()
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess) as exc:
                raise RuntimeError("a python process is not inspectable") from exc
            for token in cmdline:
                if Path(str(token)).name.lower() in bridge_names:
                    bridge_processes.append(
                        f"{int(process.info['pid'])}:{Path(str(token)).name.lower()}"
                    )
                    break
        action_body = {
            "command": _canonical_process_path(command, "candidate task executable"),
            "arguments": arguments,
        }
        return {
            "candidate_task_name": host_cutover.CANDIDATE_TASK_NAME,
            "candidate_task_enabled": candidate.enabled,
            "candidate_task_xml_sha256": hashlib.sha256(candidate.xml).hexdigest(),
            "candidate_task_action_sha256": activation_contract.sha256_json(
                action_body
            ),
            "legacy_task_enabled": legacy,
            "legacy_bridge_processes": bridge_processes,
            "legacy_recreator_processes": recreator_processes,
            "legacy_execution_lane": lane_document,
            "legacy_execution_lane_sha256": activation_contract.sha256_json(
                lane_document
            ),
        }
    except CapturedAlpacaPaperServiceError:
        raise
    except Exception as exc:
        raise CapturedAlpacaPaperServiceError(
            "HOST_CUTOVER_NOT_INSPECTABLE",
            "applied candidate/legacy Task Scheduler state could not be verified",
        ) from exc


def _launcher_projection(
    verified: activation_contract.VerifiedCapturedPaperActivation,
    *,
    mode: str,
) -> Mapping[str, Any]:
    cutover = verified.manifest.get("cutover")
    if not isinstance(cutover, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "CUTOVER_BINDING_UNAVAILABLE", "activation cutover binding is absent"
        )
    path = _strict_local_file(
        str(cutover.get("launcher_arguments_path") or ""),
        "launcher argument contract",
    )
    expected_sha = _require_sha256(
        cutover.get("launcher_arguments_sha256"),
        "launcher argument contract",
    )
    if _sha256_file(path) != expected_sha:
        raise CapturedAlpacaPaperServiceError(
            "CUTOVER_BINDING_DRIFT", "launcher argument contract hash changed"
        )
    try:
        document = json.loads(path.read_bytes())
        entry = document["invocations"][mode]
        projection = entry["projection"]
        projection_sha = entry["projection_sha256"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapturedAlpacaPaperServiceError(
            "CUTOVER_BINDING_INVALID", "launcher argument projection is invalid"
        ) from exc
    if (
        not isinstance(projection, Mapping)
        or activation_contract.sha256_json(projection)
        != _require_sha256(projection_sha, "launcher projection")
    ):
        raise CapturedAlpacaPaperServiceError(
            "CUTOVER_BINDING_INVALID", "launcher argument projection hash mismatch"
        )
    return dict(projection)


def _issue_launcher_cutover_attestation(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    args: argparse.Namespace,
    process_probe: Callable[[], Mapping[str, Any]] = _default_process_probe,
    cutover_probe: Callable[[], Mapping[str, Any]] | None = None,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> _CapturedPaperLauncherCutoverAttestation:
    """Prove the exact foreground launcher ancestry and seal it one-shot."""

    if args.mode != "activate-paper":
        raise CapturedAlpacaPaperServiceError(
            "LAUNCH_ATTESTATION_MODE_INVALID",
            "broker authority requires the ActivatePaper launcher projection",
        )
    projection = _launcher_projection(verified, mode="ActivatePaper")
    raw_evidence = process_probe()
    normalized = _normalized_process_evidence(raw_evidence)
    service_argv = list(raw_evidence["service_argv"])
    parent_cmdline = list(raw_evidence["parent_cmdline"])

    expected_service_argv = [
        str(Path(__file__).resolve()),
        "--mode",
        "activate-paper",
        "--manifest",
        str(args.manifest),
        "--manifest-sha256",
        str(args.manifest_sha256).lower(),
        "--candidate-root",
        str(args.candidate_root),
        "--launcher-path",
        str(args.launcher_path),
        "--launcher-sha256",
        str(args.launcher_sha256).lower(),
    ]
    for root in args.allow_read_root:
        expected_service_argv.extend(("--allow-read-root", str(root)))
    expected_service_argv.extend(
        ("--host-ready-receipt", str(args.host_ready_receipt))
    )
    if service_argv != expected_service_argv:
        raise CapturedAlpacaPaperServiceError(
            "SERVICE_ARGV_NOT_LAUNCHER_BOUND",
            "service argv differs from the sealed foreground launcher call",
        )
    projected_arguments = projection.get("service_arguments")
    if not isinstance(projected_arguments, list):
        raise CapturedAlpacaPaperServiceError(
            "CUTOVER_BINDING_INVALID",
            "launcher service-argument projection is absent",
        )
    positions = [
        index
        for index, value in enumerate(projected_arguments)
        if value == "--host-ready-receipt"
    ]
    if (
        len(positions) != 1
        or positions[0] + 1 >= len(projected_arguments)
        or _canonical_uncreated_local_path(
            projected_arguments[positions[0] + 1],
            "projected host-ready receipt",
        )
        != _canonical_uncreated_local_path(
            args.host_ready_receipt, "service host-ready receipt"
        )
    ):
        raise CapturedAlpacaPaperServiceError(
            "HOST_READY_PATH_NOT_LAUNCHER_BOUND",
            "host handshake path differs from the sealed launcher projection",
        )

    projected_roots = projection.get("allowed_read_roots")
    if not isinstance(projected_roots, list) or not projected_roots:
        raise CapturedAlpacaPaperServiceError(
            "CUTOVER_BINDING_INVALID", "launcher read-root projection is absent"
        )
    roots_b64 = base64.b64encode(
        _canonical_json_bytes(projected_roots)
    ).decode("ascii")
    expected_parent_tail = [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(projection.get("launcher_path") or ""),
        "-Mode",
        "ActivatePaper",
        "-PythonExecutable",
        str(projection.get("python_executable_path") or ""),
        "-CandidateRoot",
        str(projection.get("candidate_root") or ""),
        "-ServiceScriptPath",
        str(projection.get("service_path") or ""),
        "-Stage0ScriptPath",
        str(projection.get("stage0_path") or ""),
        "-ManifestPath",
        str(args.manifest),
        "-ManifestSha256",
        str(args.manifest_sha256).lower(),
        "-AllowedReadRootsBase64",
        roots_b64,
    ]
    if parent_cmdline[1:] != expected_parent_tail:
        raise CapturedAlpacaPaperServiceError(
            "PARENT_NOT_SEALED_LAUNCHER",
            "parent command line is not the sealed cutover invocation",
        )
    if Path(normalized["parent_executable_path"]).name.lower() not in {
        "powershell.exe",
        "pwsh.exe",
        "powershell",
        "pwsh",
    }:
        raise CapturedAlpacaPaperServiceError(
            "PARENT_NOT_POWERSHELL_LAUNCHER",
            "service parent is not the foreground PowerShell launcher",
        )

    resolved_cutover_probe = cutover_probe or (
        lambda: _default_cutover_probe(
            verified=verified,
            projection=projection,
            expected_parent_tail=expected_parent_tail,
            parent_executable_path=normalized["parent_executable_path"],
        )
    )
    cutover_binding = _normalized_cutover_evidence(resolved_cutover_probe())

    projected_python = _canonical_process_path(
        projection.get("python_executable_path"), "projected Python executable"
    )
    projected_service = _canonical_process_path(
        projection.get("service_path"), "projected service"
    )
    projected_root = _canonical_process_path(
        projection.get("working_directory"), "projected working directory"
    )
    if not (
        normalized["python_executable_path"] == projected_python
        and normalized["python_executable_sha256"]
        == _require_sha256(
            projection.get("python_executable_sha256"), "projected Python"
        )
        and _canonical_process_path(service_argv[0], "service argv path")
        == projected_service
        and _sha256_file(Path(projected_service))
        == _require_sha256(projection.get("service_sha256"), "projected service")
        and normalized["working_directory"] == projected_root
        and projection.get("singleton_name")
        == "Global\\CHILI-Captured-Alpaca-PAPER-SINGLETON"
    ):
        raise CapturedAlpacaPaperServiceError(
            "LAUNCHER_INTERPRETER_BINDING_MISMATCH",
            "interpreter/service/cwd differs from the sealed launcher projection",
        )

    now = _aware_utc(wall_clock(), "launcher attestation issue clock")
    if now >= verified.expires_at:
        raise CapturedAlpacaPaperServiceError(
            "LAUNCH_ATTESTATION_EXPIRED", "activation expired before composition"
        )
    body = {
        "schema_version": _LAUNCH_ATTESTATION_SCHEMA_VERSION,
        "activation_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "cutover_sha256": activation_contract.sha256_json(
            verified.manifest["cutover"]
        ),
        "launcher_projection_sha256": activation_contract.sha256_json(
            projection
        ),
        "launcher_sha256": verified.launcher_sha256,
        "launcher_singleton_name": projection["singleton_name"],
        "service_singleton_name": _SERVICE_SINGLETON_NAME,
        "issued_at": _iso(now),
        "expires_at": _iso(verified.expires_at),
        "process_binding": dict(normalized),
        "cutover_binding": dict(cutover_binding),
        "paper_execution_only": True,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    return _CapturedPaperLauncherCutoverAttestation(
        body=body,
        expires_at=verified.expires_at,
        process_probe=process_probe,
        cutover_probe=resolved_cutover_probe,
    )


def _require_sha256(value: Any, field: str) -> str:
    digest = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(digest) is None:
        raise CapturedAlpacaPaperServiceError(
            "INVALID_SHA256", f"{field} is not a lowercase SHA-256"
        )
    return digest


def _aware_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedAlpacaPaperServiceError(
            "INVALID_CLOCK", f"{field} is not timezone-aware"
        )
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware_utc(value, "service clock").isoformat().replace("+00:00", "Z")


def _strict_local_file(value: str | Path, field: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise CapturedAlpacaPaperServiceError(
            "NONLOCAL_PATH", f"{field} must be an absolute local file"
        )
    path = path.resolve(strict=True)
    cursor = path
    while True:
        info = os.lstat(cursor)
        attrs = int(getattr(info, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or attrs & _REPARSE_ATTRIBUTE:
            raise CapturedAlpacaPaperServiceError(
                "REPARSE_PATH", f"{field} traverses a reparse point"
            )
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    if not path.is_file():
        raise CapturedAlpacaPaperServiceError(
            "INVALID_FILE", f"{field} is not a regular file"
        )
    return path


def _verify_loaded_sources(
    verified: activation_contract.VerifiedCapturedPaperActivation,
) -> None:
    cutover = verified.manifest.get("cutover")
    if not isinstance(cutover, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "LOADED_SOURCE_PATH_MISMATCH",
            "activation manifest lacks the sealed cutover entrypoints",
        )
    staged_service = _strict_local_file(
        str(cutover.get("service_path") or ""),
        "staged activation service",
    )
    staged_service_sha = _require_sha256(
        cutover.get("service_sha256"), "staged activation service"
    )
    expected = {
        "activation_contract": (
            Path(activation_contract.__file__).resolve(),
            verified.source_paths.get("activation_contract"),
            verified.source_hashes.get("activation_contract"),
        ),
        # The source roster binds the reviewed bytes; active execution must use
        # the separately verified immutable content-addressed copy.
        "activation_service": (
            Path(__file__).resolve(),
            staged_service,
            staged_service_sha,
        ),
        "runtime_environment": (
            Path(runtime_env.__file__).resolve(),
            verified.source_paths.get("runtime_environment"),
            verified.source_hashes.get("runtime_environment"),
        ),
        "readiness_evidence": (
            Path(readiness_evidence.__file__).resolve(),
            verified.source_paths.get("readiness_evidence"),
            verified.source_hashes.get("readiness_evidence"),
        ),
    }
    for role, (loaded_path, pinned, digest) in expected.items():
        if pinned is None or pinned.resolve() != loaded_path:
            raise CapturedAlpacaPaperServiceError(
                "LOADED_SOURCE_PATH_MISMATCH",
                f"loaded {role} path differs from the activation roster",
            )
        if _sha256_file(loaded_path) != digest:
            raise CapturedAlpacaPaperServiceError(
                "LOADED_SOURCE_HASH_MISMATCH",
                f"loaded {role} bytes differ from the activation roster",
            )
    import_root = Path(str(cutover.get("python_import_root") or "")).resolve(
        strict=True
    )
    dependency_root = Path(
        str(cutover.get("python_dependency_root") or "")
    ).resolve(strict=True)
    stage0_path = _strict_local_file(
        str(cutover.get("stage0_path") or ""), "isolated stage0"
    )
    stage0_sha = _require_sha256(
        cutover.get("stage0_sha256"), "isolated stage0"
    )
    executable_path = _strict_local_file(
        str(cutover.get("python_executable_path") or ""),
        "isolated Python executable",
    )
    executable_sha = _require_sha256(
        cutover.get("python_executable_sha256"),
        "isolated Python executable",
    )
    attestation = getattr(sys, "_captured_paper_isolated_stage0", None)
    if not isinstance(attestation, Mapping) or set(attestation) != (
        _ISOLATED_STAGE0_ATTESTATION_KEYS
    ):
        raise CapturedAlpacaPaperServiceError(
            "ISOLATED_STAGE0_ATTESTATION_REQUIRED",
            "active service was not admitted by the exact sealed stage0",
        )
    code_build = verified.manifest.get("code_build")
    code_build_sha = (
        code_build.get("code_build_sha256")
        if isinstance(code_build, Mapping)
        else None
    )
    expected_attestation = {
        "schema_version": _ISOLATED_STAGE0_SCHEMA_VERSION,
        "stage0_path": str(stage0_path),
        "target_path": str(staged_service),
        "target_sha256": staged_service_sha,
        "target_role": "activation_service",
        "candidate_root": str(verified.candidate_root),
        "dependency_root": str(dependency_root),
        "dependency_root_identity_sha256": _require_sha256(
            cutover.get("python_dependency_root_identity_sha256"),
            "isolated dependency root identity",
        ),
        "local_roster_sha256": verified.code_build_sha256,
        "python_executable_path": str(executable_path),
        "python_executable_sha256": executable_sha,
        "manifest_path": str(verified.manifest_path),
        "manifest_sha256": verified.manifest_sha256,
        "code_build_sha256": verified.code_build_sha256,
    }
    expected_mutation_guard_mode = (
        "windows-deny-write-delete-held-handles.v1"
        if os.name == "nt"
        else "import-time-hash-held-read-handles.v1"
    )
    attested_scalars_valid = (
        type(attestation.get("dependency_file_count")) is int
        and 0 < int(attestation["dependency_file_count"])
        <= _MAX_ISOLATED_DEPENDENCY_FILES
        and type(attestation.get("dependency_tree_total_bytes")) is int
        and 0 < int(attestation["dependency_tree_total_bytes"])
        <= _MAX_ISOLATED_DEPENDENCY_BYTES
        and type(attestation.get("local_module_count")) is int
        and int(attestation["local_module_count"]) > 0
        and attestation.get("dependency_exclusion_policy")
        == _ISOLATED_DEPENDENCY_EXCLUSION_POLICY
        and attestation.get("dependency_mutation_guard_mode")
        == expected_mutation_guard_mode
        and _SHA256_RE.fullmatch(
            str(attestation.get("dependency_tree_sha256") or "")
        )
        is not None
    )
    artifact_root = Path(
        str(cutover.get("activation_artifact_root") or "")
    ).resolve(strict=True)
    dependency_layout_valid = (
        dependency_root.name.casefold() == "site-packages"
        and dependency_root.parent.name.casefold()
        == str(attestation.get("dependency_tree_sha256") or "").casefold()
        and dependency_root.parent.parent.name.casefold() == "dependencies"
        and dependency_root.parent.parent.parent
        == artifact_root / verified.activation_generation
    )
    if not (
        import_root == verified.candidate_root
        and code_build_sha == verified.code_build_sha256
        and all(attestation.get(key) == value for key, value in expected_attestation.items())
        and attested_scalars_valid
        and dependency_layout_valid
        and _sha256_file(stage0_path) == stage0_sha
        and _sha256_file(executable_path) == executable_sha
    ):
        raise CapturedAlpacaPaperServiceError(
            "ISOLATED_STAGE0_ATTESTATION_INVALID",
            "stage0, dependency capsule, target, or code-roster binding drifted",
        )

    forbidden_python_env = (
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE",
    )
    sys_path_roots = []
    for item in sys.path:
        if not item:
            continue
        try:
            sys_path_roots.append(Path(item).resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    if not (
        os.environ.get("PYTHONPATH") is None
        and os.environ.get("PYTHONNOUSERSITE") == "1"
        and all(not os.environ.get(name) for name in forbidden_python_env)
        and int(sys.flags.isolated) == 1
        and int(sys.flags.no_site) == 1
        and int(sys.flags.dont_write_bytecode) == 1
        and import_root not in sys_path_roots
        and all(
            not _inside_local_roots(path, (import_root,))
            for path in sys_path_roots
        )
        and sys_path_roots.count(dependency_root) == 1
    ):
        raise CapturedAlpacaPaperServiceError(
            "PYTHON_IMPORT_ROOT_MISMATCH",
            "Python imports escaped the sealed stage0 roster/dependency capsule",
        )


def _restore_runtime_import_path_authority(
    verified: activation_contract.VerifiedCapturedPaperActivation,
) -> None:
    """Restore the sealed stage0 path after pinned application imports.

    Some legacy package initializers append the application root to
    ``sys.path`` while the captured service composition is imported.  The
    stage0 meta-path finders still deny unsealed imports, but the final
    activation fence correctly rejects the residual mutable path.  Before
    removing it, prove that every already-loaded module beneath the candidate
    root is one of the manifest-pinned source files with unchanged bytes.
    Then remove only candidate-root entries and duplicate copies of the exact
    sealed dependency root; the ordinary strict verifier runs immediately
    afterwards.
    """

    cutover = verified.manifest.get("cutover")
    if not isinstance(cutover, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "LOADED_SOURCE_PATH_MISMATCH",
            "activation manifest lacks the sealed cutover entrypoints",
        )
    candidate_root = verified.candidate_root.resolve(strict=True)
    dependency_root = Path(
        str(cutover.get("python_dependency_root") or "")
    ).resolve(strict=True)

    pinned_by_path: dict[Path, str] = {}
    for role, raw_path in verified.source_paths.items():
        path = Path(raw_path).resolve(strict=True)
        digest = _require_sha256(
            verified.source_hashes.get(role),
            f"runtime source {role}",
        )
        pinned_by_path[path] = digest

    for module in tuple(sys.modules.values()):
        raw_path = getattr(module, "__file__", None)
        if not raw_path:
            continue
        try:
            loaded_path = Path(str(raw_path)).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not _inside_local_roots(loaded_path, (candidate_root,)):
            continue
        digest = pinned_by_path.get(loaded_path)
        if digest is None or _sha256_file(loaded_path) != digest:
            raise CapturedAlpacaPaperServiceError(
                "LOADED_RUNTIME_SOURCE_UNPINNED",
                "a loaded candidate module is absent from the sealed source roster",
            )

    restored: list[str] = []
    dependency_seen = False
    for raw_path in sys.path:
        if not raw_path:
            restored.append(raw_path)
            continue
        try:
            resolved = Path(raw_path).resolve(strict=True)
        except (OSError, RuntimeError):
            restored.append(raw_path)
            continue
        if _inside_local_roots(resolved, (candidate_root,)):
            continue
        if resolved == dependency_root:
            if dependency_seen:
                continue
            dependency_seen = True
        restored.append(raw_path)
    if not dependency_seen:
        raise CapturedAlpacaPaperServiceError(
            "PYTHON_IMPORT_ROOT_MISMATCH",
            "sealed dependency root disappeared during runtime composition",
        )
    sys.path[:] = restored


def _assert_content_addressed_activation_entrypoints(
    verified: activation_contract.VerifiedCapturedPaperActivation,
) -> None:
    """Require active execution from immutable SHA-addressed staged bytes."""

    service = Path(__file__).resolve(strict=True)
    cutover = verified.manifest.get("cutover")
    if not isinstance(cutover, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "ACTIVATION_ENTRYPOINT_NOT_CONTENT_ADDRESSED",
            "activation manifest lacks staged entrypoint bindings",
        )
    service_sha = _require_sha256(
        cutover.get("service_sha256"),
        "staged activation service",
    )
    staged_service = _strict_local_file(
        str(cutover.get("service_path") or ""), "staged activation service"
    )
    launcher = verified.launcher_path.resolve(strict=True)
    launcher_sha = _require_sha256(
        verified.launcher_sha256, "staged activation launcher"
    )
    if not (
        service.name == f"{service_sha}.py"
        and service.parent.name == service_sha
        and _sha256_file(service) == service_sha
        and staged_service == service
        and launcher.name == f"{launcher_sha}.ps1"
        and launcher.parent.name == launcher_sha
        and _sha256_file(launcher) == launcher_sha
    ):
        raise CapturedAlpacaPaperServiceError(
            "ACTIVATION_ENTRYPOINT_NOT_CONTENT_ADDRESSED",
            "PAPER activation entrypoints are not immutable staged SHA bytes",
        )


def _verify_loaded_module_role(
    verified: activation_contract.VerifiedCapturedPaperActivation,
    *,
    role: str,
    module: ModuleType,
) -> Path:
    """Bind one already-imported module to its sealed source-roster bytes."""

    pinned_path = verified.source_paths.get(role)
    pinned_sha256 = verified.source_hashes.get(role)
    if pinned_path is None or pinned_sha256 is None:
        raise CapturedAlpacaPaperServiceError(
            "RUNTIME_SOURCE_ROLE_MISSING",
            f"activation roster omitted runtime source role {role}",
        )
    raw_path = str(getattr(module, "__file__", "") or "")
    if not raw_path:
        raise CapturedAlpacaPaperServiceError(
            "RUNTIME_SOURCE_PATH_UNAVAILABLE",
            f"loaded runtime role {role} has no source path",
        )
    actual_path = Path(raw_path).resolve(strict=True)
    if (
        actual_path != pinned_path.resolve()
        or _sha256_file(actual_path) != pinned_sha256
    ):
        raise CapturedAlpacaPaperServiceError(
            "RUNTIME_SOURCE_BINDING_MISMATCH",
            f"loaded runtime role {role} differs from the activation roster",
        )
    return actual_path


_RUNTIME_MODULE_ROSTER: Mapping[str, str] = {
    "adaptive_risk_account_lock": (
        "app.services.trading.momentum_neural.adaptive_risk_account_lock"
    ),
    "adaptive_risk_policy": (
        "app.services.trading.momentum_neural.adaptive_risk_policy"
    ),
    "adaptive_risk_request_builder": (
        "app.services.trading.momentum_neural.adaptive_risk_request_builder"
    ),
    "adaptive_risk_reservation": (
        "app.services.trading.momentum_neural.adaptive_risk_reservation"
    ),
    "adaptive_risk_runtime_contract": (
        "app.services.trading.momentum_neural.adaptive_risk_runtime_contract"
    ),
    "alpaca_fill_activity": (
        "app.services.trading.momentum_neural.alpaca_fill_activity"
    ),
    "alpaca_fill_read_capability": (
        "app.services.trading.momentum_neural.alpaca_fill_read_capability"
    ),
    "alpaca_paper_adapter": "app.services.trading.venue.alpaca_spot",
    "app_db": "app.db",
    "app_migrations": "app.migrations",
    "auto_arm": "app.services.trading.momentum_neural.auto_arm",
    "captured_adaptive_risk_source": (
        "app.services.trading.momentum_neural.captured_adaptive_risk_source"
    ),
    "captured_alpaca_paper_adapter": (
        "app.services.trading.momentum_neural.captured_alpaca_paper_adapter"
    ),
    "captured_paper_admission": (
        "app.services.trading.momentum_neural.captured_paper_admission"
    ),
    "captured_paper_dispatcher": (
        "app.services.trading.momentum_neural.captured_paper_dispatcher"
    ),
    "captured_paper_entry_intent": (
        "app.services.trading.momentum_neural.captured_paper_entry_intent"
    ),
    "captured_paper_fill_capture": (
        "app.services.trading.momentum_neural.captured_paper_fill_capture"
    ),
    "captured_paper_fill_watch": (
        "app.services.trading.momentum_neural.captured_paper_fill_watch"
    ),
    "captured_paper_financial_breaker": (
        "app.services.trading.momentum_neural.captured_paper_financial_breaker"
    ),
    "captured_paper_initial_admission": (
        "app.services.trading.momentum_neural.captured_paper_initial_admission"
    ),
    "captured_paper_initial_candidate_reader": (
        "app.services.trading.momentum_neural.captured_paper_initial_candidate_reader"
    ),
    "captured_paper_initial_controller": (
        "app.services.trading.momentum_neural.captured_paper_initial_controller"
    ),
    "captured_paper_initial_provider": (
        "app.services.trading.momentum_neural.captured_paper_initial_provider"
    ),
    "captured_paper_initial_recovery": (
        "app.services.trading.momentum_neural.captured_paper_initial_recovery"
    ),
    "captured_paper_iqfeed_trigger": (
        "app.services.trading.momentum_neural.captured_paper_iqfeed_trigger"
    ),
    "captured_paper_outbox": (
        "app.services.trading.momentum_neural.captured_paper_outbox"
    ),
    "captured_paper_phase_one_handoff": (
        "app.services.trading.momentum_neural.captured_paper_phase_one_handoff"
    ),
    "captured_paper_pending_owner": (
        "app.services.trading.momentum_neural.captured_paper_pending_owner"
    ),
    "captured_paper_positive_acceptance": (
        "app.services.trading.momentum_neural.captured_paper_positive_acceptance"
    ),
    "captured_paper_preowner_promotion": (
        "app.services.trading.momentum_neural.captured_paper_preowner_promotion"
    ),
    "captured_paper_post_commit_worker": (
        "app.services.trading.momentum_neural.captured_paper_post_commit_worker"
    ),
    "captured_paper_production_material": (
        "app.services.trading.momentum_neural.captured_paper_production_material"
    ),
    "captured_paper_production_provider": (
        "app.services.trading.momentum_neural.captured_paper_production_provider"
    ),
    "captured_paper_restart_inventory": (
        "app.services.trading.momentum_neural.captured_paper_restart_inventory"
    ),
    "captured_paper_selection": (
        "app.services.trading.momentum_neural.captured_paper_selection"
    ),
    "captured_paper_selection_frontier_model": (
        "app.models.captured_paper_selection_frontier"
    ),
    "captured_paper_selection_producer": (
        "app.services.trading.momentum_neural.captured_paper_selection_producer"
    ),
    "captured_paper_selection_queue": (
        "app.services.trading.momentum_neural.captured_paper_selection_queue"
    ),
    "captured_paper_selection_runtime": (
        "app.services.trading.momentum_neural.captured_paper_selection_runtime"
    ),
    "captured_paper_selection_source": (
        "app.services.trading.momentum_neural.captured_paper_selection_source"
    ),
    "captured_paper_service_supervisor": (
        "app.services.trading.momentum_neural.captured_paper_service_supervisor"
    ),
    "captured_paper_service_fence": (
        "app.services.trading.momentum_neural.captured_paper_service_fence"
    ),
    "captured_paper_transport": (
        "app.services.trading.momentum_neural.captured_paper_transport_coordinator"
    ),
    "captured_paper_transport_worker": (
        "app.services.trading.momentum_neural.captured_paper_transport_worker"
    ),
    "captured_paper_variant_binding": (
        "app.services.trading.momentum_neural.captured_paper_variant_binding"
    ),
    "captured_viability_adapter": (
        "app.services.trading.momentum_neural.captured_viability_adapter"
    ),
    "entry_gates": "app.services.trading.momentum_neural.entry_gates",
    "execution_family_registry": "app.services.trading.execution_family_registry",
    "first_dip_tape_decision": (
        "app.services.trading.momentum_neural.first_dip_tape_decision"
    ),
    "first_dip_tape_policy": (
        "app.services.trading.momentum_neural.first_dip_tape_policy"
    ),
    "iqfeed_capture_bootstrap": "scripts.iqfeed_capture_bootstrap",
    "iqfeed_capture_bootstrap_preflight": (
        "scripts.iqfeed_capture_bootstrap_preflight"
    ),
    "iqfeed_capture_host": "scripts.iqfeed_capture_host",
    "iqfeed_depth_bridge": "scripts.iqfeed_depth_bridge",
    "iqfeed_l1_capture": (
        "app.services.trading.momentum_neural.iqfeed_l1_capture"
    ),
    "iqfeed_l2_capture": (
        "app.services.trading.momentum_neural.iqfeed_l2_capture"
    ),
    "iqfeed_trade_bridge": "scripts.iqfeed_trade_bridge",
    "live_replay_capture": (
        "app.services.trading.momentum_neural.live_replay_capture"
    ),
    "live_runner": "app.services.trading.momentum_neural.live_runner",
    "live_runner_loop": "app.services.trading.momentum_neural.live_runner_loop",
    "momentum_viability": (
        "app.services.trading.momentum_neural.viability"
    ),
    "replay_capture_contract": (
        "app.services.trading.momentum_neural.replay_capture_contract"
    ),
    "replay_capture_runtime": (
        "app.services.trading.momentum_neural.replay_capture_runtime"
    ),
    "trading_models": "app.models.trading",
    "yf_session": "app.services.yf_session",
}


def _load_pinned_runtime_modules(
    verified: activation_contract.VerifiedCapturedPaperActivation,
) -> Mapping[str, ModuleType]:
    """Import runtime code only after PAPER settings exist, then pin every byte."""

    loaded: dict[str, ModuleType] = {}
    for role, module_name in _RUNTIME_MODULE_ROSTER.items():
        module = importlib.import_module(module_name)
        _verify_loaded_module_role(verified, role=role, module=module)
        loaded[role] = module
    return loaded


def _verify_launcher(
    verified: activation_contract.VerifiedCapturedPaperActivation,
    *,
    launcher_path: str | Path,
    launcher_sha256: str,
) -> None:
    path = _strict_local_file(launcher_path, "launcher_path")
    supplied = str(launcher_sha256 or "").strip().lower()
    if (
        path != verified.launcher_path
        or supplied != verified.launcher_sha256
        or _sha256_file(path) != verified.launcher_sha256
    ):
        raise CapturedAlpacaPaperServiceError(
            "LAUNCHER_BINDING_MISMATCH",
            "executing launcher differs from the activation envelope",
        )


def _reload_final_activation_authority(
    verified: activation_contract.VerifiedCapturedPaperActivation,
    *,
    allowed_read_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> activation_contract.VerifiedCapturedPaperActivation:
    """Rehash and semantically revalidate every final authority artifact now."""

    try:
        refreshed = activation_contract.load_captured_paper_activation(
            verified.manifest_path,
            expected_manifest_sha256=verified.manifest_sha256,
            candidate_root=verified.candidate_root,
            allowed_read_roots=allowed_read_roots,
            wall_clock=wall_clock,
        )
    except Exception as exc:
        raise CapturedAlpacaPaperServiceError(
            "FINAL_ACTIVATION_REVALIDATION_FAILED",
            "activation/receipt/kill authority is no longer current",
        ) from exc
    exact_scalars = (
        "manifest_sha256",
        "activation_generation",
        "expected_account_id",
        "code_build_sha256",
        "effective_config_sha256",
        "capture_receipt_sha256",
        "launcher_sha256",
        "iqfeed_bootstrap_manifest_sha256",
        "paper_order_submission_authorized",
    )
    if (
        type(refreshed) is not activation_contract.VerifiedCapturedPaperActivation
        or any(
            getattr(refreshed, field) != getattr(verified, field)
            for field in exact_scalars
        )
        or refreshed.manifest_path != verified.manifest_path
        or refreshed.launcher_path != verified.launcher_path
        or refreshed.candidate_root != verified.candidate_root
        or refreshed.capture_store_root != verified.capture_store_root
        or dict(refreshed.source_hashes) != dict(verified.source_hashes)
        or dict(refreshed.receipt_hashes) != dict(verified.receipt_hashes)
    ):
        raise CapturedAlpacaPaperServiceError(
            "FINAL_ACTIVATION_IDENTITY_DRIFT",
            "reloaded activation differs from the composed PAPER generation",
        )
    _restore_runtime_import_path_authority(refreshed)
    _verify_loaded_sources(refreshed)
    return refreshed


def _install_and_validate_settings(
    verified: activation_contract.VerifiedCapturedPaperActivation,
) -> tuple[runtime_env.CapturedPaperRuntimeEnvironmentReceipt, Mapping[str, Any]]:
    runtime = verified.manifest["runtime_environment"]
    receipt = runtime_env.install_captured_paper_runtime_environment(
        runtime["source_env_path"],
        expected_env_sha256=runtime["source_env_sha256"],
        expected_account_id=verified.expected_account_id,
        first_dip_policy_mode="candidate",
    )
    if (
        receipt.configuration_sha256 != runtime["runtime_environment_sha256"]
        or receipt.secret_fingerprints.get("DATABASE_URL")
        != runtime["database_target_fingerprint"]
    ):
        raise CapturedAlpacaPaperServiceError(
            "RUNTIME_ENVIRONMENT_BINDING_MISMATCH",
            "installed PAPER environment differs from the activation envelope",
        )

    # This is intentionally the first application import in this process.  Its
    # source bytes are checked immediately: settings are order/P&L-affecting
    # authority and may not enter through an unpinned import cache.
    app_config = importlib.import_module("app.config")
    _verify_loaded_module_role(
        verified,
        role="app_config",
        module=app_config,
    )
    adaptive_policy_module = importlib.import_module(
        "app.services.trading.momentum_neural.adaptive_risk_policy"
    )
    _verify_loaded_module_role(
        verified,
        role="adaptive_risk_policy",
        module=adaptive_policy_module,
    )
    settings = app_config.settings

    projection = runtime_env.validate_installed_captured_paper_settings(
        settings, receipt
    )
    if (
        projection.get("settings_projection_sha256")
        != verified.settings_projection_sha256
    ):
        raise CapturedAlpacaPaperServiceError(
            "SETTINGS_PROJECTION_MISMATCH",
            "parsed PAPER settings differ from the activation envelope",
        )
    return receipt, projection


def _parse_utc_text(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturedAlpacaPaperServiceError(
            "INVALID_CLOCK", f"{field} is not a valid timestamp"
        ) from exc
    return _aware_utc(parsed, field)


def _paper_broker_snapshot(
    adapter: Any,
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    purpose: str,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, Any]:
    """Read and bind the exact PAPER UUID, account posture and open inventory."""

    _aware_utc(wall_clock(), "broker preflight clock")
    if getattr(adapter, "broker_environment", None) != "paper":
        raise CapturedAlpacaPaperServiceError(
            "BROKER_NOT_PAPER", "adapter is not bound to Alpaca PAPER"
        )
    if adapter.bind_account_id(verified.expected_account_id) is not True:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_ACCOUNT_BINDING_FAILED", "adapter rejected the expected PAPER UUID"
        )
    account = adapter.get_account_snapshot()
    if not isinstance(account, Mapping) or account.get("ok") is not True:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_ACCOUNT_UNREADABLE", "Alpaca PAPER account read failed"
        )
    account_retrieved_at = _parse_utc_text(
        account.get("retrieved_at_utc"), "broker account retrieved_at"
    )
    account_now = _aware_utc(wall_clock(), "broker account verification clock")
    if not 0.0 <= (account_now - account_retrieved_at).total_seconds() <= 10.0:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_ACCOUNT_STALE", "Alpaca PAPER account read is stale or future-dated"
        )
    blocked_fields = (
        "account_blocked",
        "trading_blocked",
        "transfers_blocked",
        "trade_suspended_by_user",
    )
    if (
        account.get("paper") is not True
        or str(account.get("account_id") or "").strip()
        != verified.expected_account_id
        or str(account.get("status") or "").strip().upper() != "ACTIVE"
        or any(account.get(name) is not False for name in blocked_fields)
    ):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_ACCOUNT_UNSAFE", "Alpaca PAPER account posture is not entry-safe"
        )
    for field in ("equity", "last_equity", "buying_power"):
        value = account.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise CapturedAlpacaPaperServiceError(
                "BROKER_ACCOUNT_ECONOMICS_UNAVAILABLE",
                f"Alpaca PAPER {field} is unavailable",
            )
    cash = account.get("cash")
    if (
        isinstance(cash, bool)
        or not isinstance(cash, (int, float))
        or not math.isfinite(float(cash))
    ):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_ACCOUNT_ECONOMICS_UNAVAILABLE",
            "Alpaca PAPER cash is unavailable",
        )

    connection = adapter.get_paper_connection_generation_receipt()
    if not isinstance(connection, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_CONNECTION_UNAVAILABLE", "PAPER connection receipt is unavailable"
        )
    connection_body = str(connection.get("receipt_canonical_json") or "")
    connection_sha256 = _require_sha256(
        connection.get("receipt_sha256"), "broker connection receipt"
    )
    if (
        connection.get("schema_version")
        != "chili.alpaca-paper-connection-generation.v1"
        or connection.get("broker_environment") != "paper"
        or connection.get("asset_class") != "us_equity"
        or connection.get("provider_account_id") != verified.expected_account_id
        or not str(connection.get("adapter_connection_generation") or "").startswith(
            "alpaca-paper-rest:"
        )
        or hashlib.sha256(connection_body.encode("utf-8")).hexdigest()
        != connection_sha256
    ):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_CONNECTION_BINDING_MISMATCH",
            "PAPER connection receipt is malformed or account-mismatched",
        )
    connection_available_at = _parse_utc_text(
        connection.get("available_at"), "broker connection available_at"
    )
    connection_now = _aware_utc(
        wall_clock(), "broker connection verification clock"
    )
    if not 0.0 <= (connection_now - connection_available_at).total_seconds() <= 10.0:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_CONNECTION_STALE", "PAPER connection receipt is stale"
        )

    submission_audit = adapter.get_order_submission_audit_snapshot()
    if not isinstance(submission_audit, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_SUBMISSION_AUDIT_UNAVAILABLE",
            "PAPER adapter order-submission audit is unavailable",
        )
    submission_body = str(
        submission_audit.get("snapshot_canonical_json") or ""
    )
    submission_sha256 = _require_sha256(
        submission_audit.get("snapshot_sha256"),
        "broker order-submission audit",
    )
    submission_count = submission_audit.get("submission_call_count")
    if not (
        submission_audit.get("schema_version")
        == "chili.alpaca-paper-order-submission-audit.v1"
        and submission_audit.get("broker_environment") == "paper"
        and submission_audit.get("asset_class") == "us_equity"
        and submission_audit.get("provider_account_id")
        == verified.expected_account_id
        and submission_audit.get("adapter_connection_generation")
        == connection.get("adapter_connection_generation")
        and submission_audit.get("adapter_build_sha256")
        == connection.get("adapter_build_sha256")
        and isinstance(submission_audit.get("audit_generation"), str)
        and bool(submission_audit.get("audit_generation"))
        and isinstance(submission_count, int)
        and not isinstance(submission_count, bool)
        and submission_count >= 0
        and _require_sha256(
            submission_audit.get("submission_chain_sha256"),
            "broker order-submission chain",
        )
        and hashlib.sha256(submission_body.encode("utf-8")).hexdigest()
        == submission_sha256
    ):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_SUBMISSION_AUDIT_MISMATCH",
            "PAPER adapter order-submission audit is malformed or generation-mismatched",
        )

    positions, _position_meta = adapter.list_positions()
    if positions is None or not isinstance(positions, list):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_POSITIONS_UNREADABLE", "Alpaca PAPER positions are unreadable"
        )
    if positions:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_NOT_FLAT", "Alpaca PAPER account has existing exposure"
        )

    read_binding = {
        "schema_version": "chili.captured-paper-service-broker-read.v1",
        "purpose": str(purpose or "").strip(),
        "activation_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "expected_account_id": verified.expected_account_id,
        "connection_receipt_sha256": connection_sha256,
    }
    census = adapter.get_paper_open_order_census(read_binding=read_binding)
    if not (
        isinstance(census, Mapping)
        and census.get("readable") is True
        and census.get("pagination_complete") is True
        and census.get("broker_environment") == "paper"
        and census.get("asset_class") == "us_equity"
        and census.get("provider_account_id") == verified.expected_account_id
        and census.get("adapter_connection_generation")
        == connection.get("adapter_connection_generation")
        and isinstance(census.get("orders"), list)
    ):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_OPEN_ORDERS_UNREADABLE",
            "Alpaca PAPER open-order census is incomplete",
        )
    if census["orders"]:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_OPEN_ORDERS_PRESENT", "Alpaca PAPER has existing open orders"
        )
    return {
        "account_id": verified.expected_account_id,
        "account_retrieved_at": _iso(account_retrieved_at),
        "account_status": "ACTIVE",
        "account_equity": float(account["equity"]),
        "account_last_equity": float(account["last_equity"]),
        "account_buying_power": float(account["buying_power"]),
        "account_cash": float(cash),
        "broker_day_change": float(account["equity"])
        - float(account["last_equity"]),
        "account_blocked": False,
        "trading_blocked": False,
        "transfers_blocked": False,
        "trade_suspended_by_user": False,
        "position_count": 0,
        "position_inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
        "open_order_count": 0,
        "open_order_inventory_sha256": _require_sha256(
            census.get("inventory_sha256"), "open-order inventory"
        ),
        "open_order_census_sha256": _require_sha256(
            census.get("query_receipt_sha256"), "open-order census"
        ),
        "connection_generation": connection["adapter_connection_generation"],
        "connection_receipt_sha256": connection_sha256,
        "connection_receipt": dict(connection),
        "order_submission_audit_generation": submission_audit[
            "audit_generation"
        ],
        "order_submission_call_count": int(submission_count),
        "order_submission_chain_sha256": submission_audit[
            "submission_chain_sha256"
        ],
        "order_submission_audit_sha256": submission_sha256,
        "order_submission_audit": dict(submission_audit),
        "snapshot_observed_at": _iso(
            _aware_utc(wall_clock(), "broker snapshot completion clock")
        ),
    }


def _paper_order_transition_fence(
    adapter: Any,
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    broker_snapshot: Mapping[str, Any],
    after: datetime,
    until: datetime,
    purpose: str,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, Any]:
    """Prove no PAPER order was submitted during the cutover generation.

    This complements, but does not replace, the flat/open-order snapshot.  An
    accepted-then-canceled legacy order is terminal and therefore invisible to
    an OPEN-only census.  The all-status census is consumed only after the host
    permit and before any captured-PAPER worker can submit an order.
    """

    after_utc = _aware_utc(after, "order-transition after clock")
    until_utc = _aware_utc(until, "order-transition until clock")
    if not after_utc < until_utc:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_ORDER_TRANSITION_WINDOW_INVALID",
            "PAPER order-transition census window is empty",
        )
    connection_generation = str(
        broker_snapshot.get("connection_generation") or ""
    ).strip()
    connection_receipt_sha256 = _require_sha256(
        broker_snapshot.get("connection_receipt_sha256"),
        "broker connection receipt",
    )
    read_binding = {
        "schema_version": "chili.captured-paper-order-transition-read.v1",
        "purpose": str(purpose or "").strip(),
        "activation_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "expected_account_id": verified.expected_account_id,
        "connection_generation": connection_generation,
        "connection_receipt_sha256": connection_receipt_sha256,
        "open_order_census_sha256": _require_sha256(
            broker_snapshot.get("open_order_census_sha256"),
            "open-order census",
        ),
        "position_inventory_sha256": _require_sha256(
            broker_snapshot.get("position_inventory_sha256"),
            "position inventory",
        ),
        "order_submission_audit_sha256": _require_sha256(
            broker_snapshot.get("order_submission_audit_sha256"),
            "order-submission audit",
        ),
        "after": _iso(after_utc),
        "until": _iso(until_utc),
    }
    census = adapter.get_paper_order_transition_census(
        after=after_utc,
        until=until_utc,
        read_binding=read_binding,
    )
    if not (
        isinstance(census, Mapping)
        and census.get("readable") is True
        and census.get("pagination_complete") is True
        and census.get("broker_environment") == "paper"
        and census.get("asset_class") == "us_equity"
        and census.get("provider_account_id") == verified.expected_account_id
        and census.get("adapter_connection_generation") == connection_generation
        and census.get("read_binding_sha256")
        == activation_contract.sha256_json(read_binding)
        and isinstance(census.get("orders"), list)
    ):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_ORDER_TRANSITIONS_UNREADABLE",
            "Alpaca PAPER all-status order census is incomplete",
        )
    available_raw = census.get("available_at")
    available_at = (
        _aware_utc(available_raw, "order-transition available_at")
        if isinstance(available_raw, datetime)
        else _parse_utc_text(
            available_raw, "order-transition available_at"
        )
    )
    observed_at = _aware_utc(
        wall_clock(), "order-transition verification clock"
    )
    if not 0.0 <= (observed_at - available_at).total_seconds() <= 10.0:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_ORDER_TRANSITIONS_STALE",
            "Alpaca PAPER all-status order census is stale",
        )
    orders = census["orders"]
    if orders:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_RECENT_ORDER_TRANSITION_PRESENT",
            "Alpaca PAPER has an order transition during cutover",
        )
    return {
        "schema_version": "chili.captured-paper-order-transition-fence.v1",
        "verdict": "NO_PAPER_ORDER_TRANSITION_DURING_CUTOVER",
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "activation_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "connection_generation": connection_generation,
        "read_binding_sha256": census["read_binding_sha256"],
        "query_receipt_sha256": _require_sha256(
            census.get("query_receipt_sha256"),
            "order-transition census",
        ),
        "inventory_sha256": _require_sha256(
            census.get("inventory_sha256"),
            "order-transition inventory",
        ),
        "exact_order_count": 0,
        "after": _iso(after_utc),
        "until": _iso(until_utc),
        "available_at": _iso(available_at),
        "observed_at": _iso(observed_at),
    }


def _paper_fill_activity_fence(
    adapter: Any,
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    broker_snapshot: Mapping[str, Any],
    after: datetime,
    until: datetime,
    purpose: str,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, Any]:
    """Require a pagination-complete, empty account-wide PAPER fill census."""

    after_utc = _aware_utc(after, "fill-activity after clock")
    until_utc = _aware_utc(until, "fill-activity until clock")
    if not after_utc < until_utc:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_FILL_ACTIVITY_WINDOW_INVALID",
            "PAPER fill-activity census window is empty",
        )
    connection_generation = str(
        broker_snapshot.get("connection_generation") or ""
    ).strip()
    read_binding = {
        "schema_version": "chili.captured-paper-fill-activity-read.v1",
        "purpose": str(purpose or "").strip(),
        "activation_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "expected_account_id": verified.expected_account_id,
        "connection_generation": connection_generation,
        "after": after_utc.isoformat(),
        "until": until_utc.isoformat(),
    }
    census = adapter.get_paper_account_fill_activity_census(
        after=after_utc,
        until=until_utc,
        read_binding=read_binding,
    )
    if not (
        isinstance(census, Mapping)
        and census.get("readable") is True
        and census.get("pagination_complete") is True
        and census.get("broker_environment") == "paper"
        and census.get("activity_scope") == "all_account_fill_activities"
        and census.get("provider_account_id") == verified.expected_account_id
        and census.get("adapter_connection_generation") == connection_generation
        and census.get("read_binding_sha256")
        == activation_contract.sha256_json(read_binding)
        and census.get("query_after") == after_utc.isoformat()
        and census.get("query_until") == until_utc.isoformat()
        and isinstance(census.get("activities"), list)
    ):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_FILL_ACTIVITY_UNREADABLE",
            "Alpaca PAPER account fill-activity census is incomplete",
        )
    available_raw = census.get("available_at")
    available_at = (
        _aware_utc(available_raw, "fill-activity available_at")
        if isinstance(available_raw, datetime)
        else _parse_utc_text(available_raw, "fill-activity available_at")
    )
    observed_at = _aware_utc(wall_clock(), "fill-activity verification clock")
    if not 0.0 <= (observed_at - available_at).total_seconds() <= 10.0:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_FILL_ACTIVITY_STALE",
            "Alpaca PAPER fill-activity census is stale",
        )
    activities = census["activities"]
    if activities:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_RECENT_FILL_ACTIVITY_PRESENT",
            "Alpaca PAPER has fill activity during the activation window",
        )
    return {
        "schema_version": "chili.captured-paper-fill-activity-fence.v1",
        "verdict": "NO_PAPER_FILL_ACTIVITY_DURING_ACTIVATION",
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "activation_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "connection_generation": connection_generation,
        "read_binding_sha256": census["read_binding_sha256"],
        "query_receipt_sha256": _require_sha256(
            census.get("query_receipt_sha256"), "fill-activity census"
        ),
        "inventory_sha256": _require_sha256(
            census.get("inventory_sha256"), "fill-activity inventory"
        ),
        "exact_activity_count": 0,
        "after": _iso(after_utc),
        "until": _iso(until_utc),
        "available_at": _iso(available_at),
        "observed_at": _iso(observed_at),
    }


def _no_order_smoke_order_window_start(
    verified: activation_contract.VerifiedCapturedPaperActivation,
) -> datetime:
    """Return the sealed no-order observation frontier for final broker reads."""

    path = verified.receipt_paths.get("no_order_smoke")
    expected_sha256 = verified.receipt_hashes.get("no_order_smoke")
    if path is None or expected_sha256 is None:
        raise CapturedAlpacaPaperServiceError(
            "NO_ORDER_SMOKE_EVIDENCE_MISSING",
            "final PAPER activation lacks its sealed no-order receipt",
        )
    value = _stable_canonical_json_mapping(path, field="no-order smoke receipt")
    self_digest = _require_sha256(
        value.get("receipt_sha256"), "no-order smoke self digest"
    )
    self_body = dict(value)
    self_body.pop("receipt_sha256")
    if not (
        hashlib.sha256(_canonical_json_bytes(value)).hexdigest()
        == expected_sha256
        and activation_contract.sha256_json(self_body) == self_digest
        and value.get("receipt_kind") == "no_order_smoke"
        and value.get("verdict") == "PASS"
        and value.get("activation_generation") == verified.activation_generation
        and value.get("account_scope") == "alpaca:paper"
        and value.get("expected_account_id") == verified.expected_account_id
        and value.get("orders_submitted") is False
    ):
        raise CapturedAlpacaPaperServiceError(
            "NO_ORDER_SMOKE_EVIDENCE_INVALID",
            "sealed no-order receipt cannot anchor the final broker window",
        )
    return _parse_utc_text(value.get("captured_at"), "no-order captured_at")


def _assert_broker_snapshot_fixed_point(
    baseline: Mapping[str, Any],
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> None:
    """Require a flat, zero-POST, economically stable PAPER account snapshot."""

    stable_fields = (
        "account_id",
        "account_status",
        "account_equity",
        "account_last_equity",
        "account_buying_power",
        "account_cash",
        "broker_day_change",
        "account_blocked",
        "trading_blocked",
        "transfers_blocked",
        "trade_suspended_by_user",
        "position_count",
        "position_inventory_sha256",
        "open_order_count",
        "open_order_inventory_sha256",
        "connection_generation",
        "order_submission_audit_generation",
        "order_submission_call_count",
        "order_submission_chain_sha256",
    )
    snapshots = (baseline, first, second)
    if not all(isinstance(item, Mapping) for item in snapshots):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_FIXED_POINT_UNREADABLE",
            "PAPER broker fixed-point snapshot is unavailable",
        )
    if not all(
        item.get("position_count") == 0
        and item.get("open_order_count") == 0
        and item.get("order_submission_call_count") == 0
        for item in snapshots
    ):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_FIXED_POINT_NOT_QUIET",
            "PAPER broker exposure, open orders, or local POST audit is nonzero",
        )
    if any(
        item.get(field) != baseline.get(field)
        for item in (first, second)
        for field in stable_fields
    ):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_FIXED_POINT_DRIFT",
            "PAPER account/order state changed across the final read cycles",
        )


def _paper_broker_quiet_fixed_point(
    adapter: Any,
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    composition: _CapturedPaperServiceComposition,
    baseline_snapshot: Mapping[str, Any],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> Mapping[str, Any]:
    """Take two separated, complete PAPER broker reads before any worker.

    This is deliberately labeled assumption-bound.  Alpaca does not publish a
    maximum REST acceptance/visibility latency, so a finite local horizon is a
    fake-money operational fence, never a live-cash proof.
    """

    window_start = _no_order_smoke_order_window_start(verified)
    first_snapshot = _paper_broker_snapshot(
        adapter,
        verified=verified,
        purpose="active_service_broker_fixed_point_first",
        wall_clock=wall_clock,
    )
    _assert_composition_broker_generation(composition, first_snapshot)
    first_until = _aware_utc(wall_clock(), "first broker fixed-point cutoff")
    first_fence = _paper_order_transition_fence(
        adapter,
        verified=verified,
        broker_snapshot=first_snapshot,
        after=window_start,
        until=first_until,
        purpose="active_service_broker_fixed_point_first",
        wall_clock=wall_clock,
    )
    first_fill_fence = _paper_fill_activity_fence(
        adapter,
        verified=verified,
        broker_snapshot=first_snapshot,
        after=window_start,
        until=first_until,
        purpose="active_service_broker_fixed_point_first",
        wall_clock=wall_clock,
    )

    required = float(_PAPER_BROKER_FIXED_POINT_SEPARATION_SECONDS)
    started = float(monotonic_clock())
    wait(required)
    elapsed = float(monotonic_clock()) - started
    if elapsed < required:
        raise CapturedAlpacaPaperServiceError(
            "BROKER_FIXED_POINT_HORIZON_INCOMPLETE",
            "PAPER broker fixed-point read returned before its sealed interval",
        )

    second_snapshot = _paper_broker_snapshot(
        adapter,
        verified=verified,
        purpose="active_service_broker_fixed_point_second",
        wall_clock=wall_clock,
    )
    _assert_composition_broker_generation(composition, second_snapshot)
    second_until = _aware_utc(wall_clock(), "second broker fixed-point cutoff")
    second_fence = _paper_order_transition_fence(
        adapter,
        verified=verified,
        broker_snapshot=second_snapshot,
        after=window_start,
        until=second_until,
        purpose="active_service_broker_fixed_point_second",
        wall_clock=wall_clock,
    )
    second_fill_fence = _paper_fill_activity_fence(
        adapter,
        verified=verified,
        broker_snapshot=second_snapshot,
        after=window_start,
        until=second_until,
        purpose="active_service_broker_fixed_point_second",
        wall_clock=wall_clock,
    )
    _assert_broker_snapshot_fixed_point(
        baseline_snapshot, first_snapshot, second_snapshot
    )
    return {
        "schema_version": "chili.captured-paper-broker-fixed-point.v1",
        "verdict": "PAPER_BROKER_QUIET_FIXED_POINT",
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "activation_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "window_start": _iso(window_start),
        "assumption_bound": True,
        "live_cash_certification": False,
        "separation_seconds": elapsed,
        "baseline_snapshot": dict(baseline_snapshot),
        "first_snapshot": dict(first_snapshot),
        "first_order_census": dict(first_fence),
        "first_fill_activity_census": dict(first_fill_fence),
        "second_snapshot": dict(second_snapshot),
        "second_order_census": dict(second_fence),
        "second_fill_activity_census": dict(second_fill_fence),
    }


def _readiness_context(
    verified: activation_contract.VerifiedCapturedPaperPreactivation,
) -> readiness_evidence.ReadinessValidationContext:
    runtime = verified.manifest.get("runtime_environment")
    if not isinstance(runtime, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "READINESS_CONTEXT_UNAVAILABLE",
            "preactivation runtime binding is unavailable",
        )
    return readiness_evidence.ReadinessValidationContext(
        activation_generation=verified.activation_generation,
        expected_account_id=verified.expected_account_id,
        code_build_sha256=verified.code_build_sha256,
        effective_config_sha256=verified.effective_config_sha256,
        capture_receipt_sha256=verified.capture_receipt_sha256,
        runtime_environment_sha256=_require_sha256(
            runtime.get("runtime_environment_sha256"),
            "runtime environment",
        ),
        database_target_fingerprint=_require_sha256(
            runtime.get("database_target_fingerprint"),
            "database target fingerprint",
        ),
        iqfeed_bootstrap_manifest_sha256=(
            verified.iqfeed_bootstrap_manifest_sha256
        ),
        launcher_argument_contract_sha256=_require_sha256(
            (
                verified.manifest.get("cutover")
                if isinstance(verified.manifest.get("cutover"), Mapping)
                else {}
            ).get("launcher_arguments_sha256"),
            "launcher argument contract",
        ),
        capture_store_root=str(verified.capture_store_root),
        source_hashes=verified.source_hashes,
    )


def _paper_kill_switch_snapshot(
    database_engine: Any,
    *,
    verified: activation_contract.VerifiedCapturedPaperPreactivation,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, Any]:
    """Force one durable, read-only kill-switch query after smoke shutdown."""

    context = _readiness_context(verified)
    try:
        from sqlalchemy import text

        with database_engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, breaker_tripped, breaker_reason, "
                    "created_at AT TIME ZONE 'UTC' AS created_at_utc "
                    "FROM trading_risk_state "
                    "WHERE regime = 'kill_switch' "
                    "ORDER BY created_at DESC, id DESC LIMIT 1"
                )
            ).fetchone()
    except Exception as exc:
        raise CapturedAlpacaPaperServiceError(
            "KILL_SWITCH_UNREADABLE",
            "post-smoke kill-switch state cannot be read",
        ) from exc
    if row is None:
        raise CapturedAlpacaPaperServiceError(
            "KILL_SWITCH_STATE_UNAVAILABLE",
            "post-smoke kill-switch has no durable state row",
        )
    row_id = row[0]
    active = row[1]
    reason = None if row[2] is None else str(row[2]).strip() or None
    created_at = row[3]
    if (
        not isinstance(row_id, int)
        or isinstance(row_id, bool)
        or row_id <= 0
        or not isinstance(active, bool)
        or not isinstance(created_at, datetime)
        or created_at.tzinfo is None
    ):
        raise CapturedAlpacaPaperServiceError(
            "KILL_SWITCH_STATE_MALFORMED",
            "post-smoke kill-switch row is malformed",
        )
    if active:
        raise CapturedAlpacaPaperServiceError(
            "KILL_SWITCH_ACTIVE",
            "post-smoke kill-switch forbids new PAPER entries",
        )
    observed_at = _aware_utc(wall_clock(), "kill-switch query clock")
    payload = {
        "schema_version": "chili.captured-paper-kill-switch-query.v1",
        "activation_generation": verified.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "database_target_fingerprint": context.database_target_fingerprint,
        "state_version": row_id,
        "active": False,
        "reason": reason,
        "state_created_at": _iso(created_at),
        "observed_at": _iso(observed_at),
    }
    return {
        **payload,
        "query_receipt_sha256": activation_contract.sha256_json(payload),
    }


def _issue_post_smoke_refreshed_readiness(
    *,
    verified: activation_contract.VerifiedCapturedPaperPreactivation,
    broker_snapshot: Mapping[str, Any],
    kill_switch_snapshot: Mapping[str, Any],
    issued_at: datetime,
) -> Mapping[str, Mapping[str, Any]]:
    """Mint the only two short-lived receipts allowed to replace pre-smoke facts."""

    context = _readiness_context(verified)
    issued = _aware_utc(issued_at, "post-smoke readiness clock")
    broker_observed = _parse_utc_text(
        broker_snapshot.get("snapshot_observed_at"),
        "post-smoke broker observed_at",
    )
    kill_observed = _parse_utc_text(
        kill_switch_snapshot.get("observed_at"),
        "post-smoke kill-switch observed_at",
    )
    if not (
        0.0 <= (issued - broker_observed).total_seconds() <= 10.0
        and 0.0 <= (issued - kill_observed).total_seconds() <= 10.0
    ):
        raise CapturedAlpacaPaperServiceError(
            "POST_SMOKE_READINESS_STALE",
            "post-smoke account or kill-switch evidence is stale",
        )
    account_payload = {
        "schema_version": "chili.captured-paper-broker-account-read.v1",
        "account_id": broker_snapshot.get("account_id"),
        "status": broker_snapshot.get("account_status"),
        "account_blocked": broker_snapshot.get("account_blocked"),
        "trading_blocked": broker_snapshot.get("trading_blocked"),
        "transfers_blocked": broker_snapshot.get("transfers_blocked"),
        "trade_suspended_by_user": broker_snapshot.get(
            "trade_suspended_by_user"
        ),
        "observed_at": broker_snapshot.get("account_retrieved_at"),
    }
    position_payload = {
        "schema_version": "chili.captured-paper-position-census.v1",
        "count": broker_snapshot.get("position_count"),
        "inventory_sha256": broker_snapshot.get("position_inventory_sha256"),
        "observed_at": broker_snapshot.get("snapshot_observed_at"),
    }
    connection_receipt_sha256 = _require_sha256(
        broker_snapshot.get("connection_receipt_sha256"),
        "post-smoke connection receipt",
    )
    broker_evidence = {
        "schema_version": (
            "chili.captured-paper-readiness-evidence.broker_account.v2"
        ),
        "source_receipts": {
            "paper_connection": connection_receipt_sha256,
            "account_read": activation_contract.sha256_json(account_payload),
            "position_census": activation_contract.sha256_json(position_payload),
            "order_census": _require_sha256(
                broker_snapshot.get("open_order_census_sha256"),
                "post-smoke open-order census",
            ),
        },
        "account_identity_sha256": readiness_evidence.sha256_json(
            {
                "account_id": verified.expected_account_id,
                "broker": "alpaca",
                "environment": "paper",
            }
        ),
        "connection_generation": str(
            broker_snapshot.get("connection_generation") or ""
        ),
        "connection_receipt_sha256": connection_receipt_sha256,
        "account_status": broker_snapshot.get("account_status"),
        "account_blocked": broker_snapshot.get("account_blocked"),
        "trading_blocked": broker_snapshot.get("trading_blocked"),
        "transfers_blocked": broker_snapshot.get("transfers_blocked"),
        "trade_suspended_by_user": broker_snapshot.get(
            "trade_suspended_by_user"
        ),
        "position_count": broker_snapshot.get("position_count"),
        "open_order_count": broker_snapshot.get("open_order_count"),
        "position_inventory_sha256": broker_snapshot.get(
            "position_inventory_sha256"
        ),
        "open_order_inventory_sha256": broker_snapshot.get(
            "open_order_inventory_sha256"
        ),
        "observed_at": _iso(broker_observed),
        "paper_execution_only": True,
    }
    kill_evidence = {
        "schema_version": (
            "chili.captured-paper-readiness-evidence.kill_switch.v2"
        ),
        "source_receipts": {
            "kill_switch_query": _require_sha256(
                kill_switch_snapshot.get("query_receipt_sha256"),
                "post-smoke kill-switch query",
            )
        },
        "database_target_fingerprint": context.database_target_fingerprint,
        "state_readable": True,
        "active": False,
        "state_version": kill_switch_snapshot.get("state_version"),
        "observed_at": _iso(kill_observed),
    }
    # Each receipt's fixed maximum lifetime is measured from its own durable
    # observation, not from the later issuance clock, so an older observation
    # can never silently gain extra validity from a later issuance stamp.
    broker_expires_at = broker_observed + timedelta(
        seconds=_POST_SMOKE_RECEIPT_WINDOW_SECONDS
    )
    kill_expires_at = kill_observed + timedelta(
        seconds=_POST_SMOKE_RECEIPT_WINDOW_SECONDS
    )
    try:
        broker_receipt = readiness_evidence.issue_readiness_receipt_v2(
            kind="broker_account",
            context=context,
            evidence=broker_evidence,
            captured_at=broker_observed,
            expires_at=broker_expires_at,
            now=issued,
            max_age_seconds=_POST_SMOKE_RECEIPT_WINDOW_SECONDS,
        )
        kill_receipt = readiness_evidence.issue_readiness_receipt_v2(
            kind="kill_switch",
            context=context,
            evidence=kill_evidence,
            captured_at=kill_observed,
            expires_at=kill_expires_at,
            now=issued,
            max_age_seconds=_POST_SMOKE_RECEIPT_WINDOW_SECONDS,
        )
    except readiness_evidence.CapturedPaperReadinessEvidenceError as exc:
        raise CapturedAlpacaPaperServiceError(
            "POST_SMOKE_READINESS_INVALID",
            "post-smoke typed readiness cannot be issued",
        ) from exc
    return {
        "broker_account": dict(broker_receipt),
        "kill_switch": dict(kill_receipt),
    }


def _assert_composition_broker_generation(
    composition: _CapturedPaperServiceComposition,
    snapshot: Mapping[str, Any],
) -> None:
    """Fence a fresh read to the adapter/client generation built earlier.

    The receipt SHA itself includes ``available_at`` and therefore changes on
    every fresh read.  Stable identity fields—not the timestamped envelope
    digest—must remain exact, while :func:`_paper_broker_snapshot` separately
    verifies each new receipt's canonical bytes and SHA.
    """

    frozen = composition.connection_generation_receipt
    current = snapshot.get("connection_receipt")
    stable_fields = (
        "schema_version",
        "broker_environment",
        "asset_class",
        "provider_account_id",
        "adapter_connection_generation",
        "adapter_build_sha256",
    )
    if not (
        isinstance(frozen, Mapping)
        and isinstance(current, Mapping)
        and all(frozen.get(name) == current.get(name) for name in stable_fields)
        and snapshot.get("connection_generation")
        == frozen.get("adapter_connection_generation")
    ):
        raise CapturedAlpacaPaperServiceError(
            "BROKER_GENERATION_DRIFT",
            "Alpaca PAPER adapter/account generation changed after composition",
        )


def _verify_phase_one_reconciliation_receipt(
    receipt: Mapping[str, Any],
    *,
    activation_generation: str,
) -> Mapping[str, Any]:
    """Verify the exhaustive pre-outbox restart pass before broker census."""

    expected_keys = {
        "schema_version",
        "activation_generation",
        "initial_pending_count",
        "remaining_pending_count",
        "reconciliation_complete",
        "outbox_committed_count",
        "decision_handoff_unavailable_count",
        "outbox_committed_completion_sha256s",
        "decision_handoff_unavailable_completion_sha256s",
        "phase_two_side_effects_inferred",
        "receipt_sha256",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != expected_keys:
        raise CapturedAlpacaPaperServiceError(
            "PHASE_ONE_RECONCILIATION_UNPROVEN",
            "phase-one restart reconciliation receipt is malformed",
        )
    initial = receipt.get("initial_pending_count")
    committed = receipt.get("outbox_committed_count")
    unavailable = receipt.get("decision_handoff_unavailable_count")
    committed_ids = receipt.get("outbox_committed_completion_sha256s")
    unavailable_ids = receipt.get(
        "decision_handoff_unavailable_completion_sha256s"
    )
    counts = (initial, committed, unavailable)
    if not (
        receipt.get("schema_version")
        == "chili.captured-paper-phase-one-restart-reconciliation.v1"
        and receipt.get("activation_generation") == activation_generation
        and receipt.get("remaining_pending_count") == 0
        and receipt.get("reconciliation_complete") is True
        and receipt.get("phase_two_side_effects_inferred") is False
        and all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in counts
        )
        and initial == committed + unavailable
        and isinstance(committed_ids, list)
        and isinstance(unavailable_ids, list)
        and committed_ids == sorted(set(committed_ids))
        and unavailable_ids == sorted(set(unavailable_ids))
        and len(committed_ids) == committed
        and len(unavailable_ids) == unavailable
        and not set(committed_ids).intersection(unavailable_ids)
        and all(_SHA256_RE.fullmatch(str(value or "")) for value in committed_ids)
        and all(_SHA256_RE.fullmatch(str(value or "")) for value in unavailable_ids)
    ):
        raise CapturedAlpacaPaperServiceError(
            "PHASE_ONE_RECONCILIATION_UNPROVEN",
            "phase-one restart reconciliation did not exhaust pending rows",
        )
    supplied = _require_sha256(
        receipt.get("receipt_sha256"), "phase-one reconciliation receipt"
    )
    body = dict(receipt)
    body.pop("receipt_sha256")
    if hashlib.sha256(_canonical_json_bytes(body)).hexdigest() != supplied:
        raise CapturedAlpacaPaperServiceError(
            "PHASE_ONE_RECONCILIATION_UNPROVEN",
            "phase-one restart reconciliation digest is invalid",
        )
    return dict(receipt)


def _verify_restart_classifier_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_account_id: str,
    expected_runtime_generation: str,
    expected_connection_generation: str,
    expected_adapter_build_sha256: str,
    expected_read_binding_sha256: str,
) -> Mapping[str, Any]:
    """Verify a classifier result without trusting its duplicated fields."""

    if not isinstance(receipt, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "RESTART_INVENTORY_UNPROVEN",
            "captured PAPER restart classifier returned no receipt",
        )
    canonical = str(receipt.get("receipt_canonical_json") or "")
    supplied = _require_sha256(
        receipt.get("receipt_sha256"), "restart classifier receipt"
    )
    try:
        body = json.loads(canonical)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CapturedAlpacaPaperServiceError(
            "RESTART_INVENTORY_UNPROVEN",
            "captured PAPER restart receipt is not canonical JSON",
        ) from exc
    echoed = dict(receipt)
    echoed.pop("receipt_canonical_json", None)
    echoed.pop("receipt_sha256", None)
    expected_body_keys = {
        "schema_version",
        "disposition",
        "account_scope",
        "expected_account_id",
        "runtime_generation",
        "broker_connection_generation",
        "broker_adapter_build_sha256",
        "broker_read_binding_sha256",
        "open_order_census_sha256",
        "open_order_inventory_sha256",
        "position_census_sha256",
        "position_inventory_sha256",
        "durable_inventory_sha256",
        "owned_open_orders",
        "owned_positions",
        "terminal_late_fill_quarantines",
        "recovery_required",
        "new_admissions_quarantined",
        "exposure_decreasing_only",
        "broker_inventory_flat",
        "observed_at",
        "paper_execution_only",
        "live_cash_authorized",
        "real_money_authorized",
    }
    if not (
        isinstance(body, dict)
        and set(body) == expected_body_keys
        and _canonical_json_bytes(body).decode("utf-8") == canonical
        and body == echoed
        and hashlib.sha256(canonical.encode("utf-8")).hexdigest() == supplied
        and body.get("schema_version")
        == "chili.captured-paper-restart-inventory.v1"
        and body.get("account_scope") == "alpaca:paper"
        and body.get("expected_account_id") == expected_account_id
        and body.get("runtime_generation") == expected_runtime_generation
        and body.get("broker_connection_generation")
        == expected_connection_generation
        and body.get("broker_adapter_build_sha256")
        == expected_adapter_build_sha256
        and body.get("broker_read_binding_sha256")
        == expected_read_binding_sha256
        and body.get("paper_execution_only") is True
        and body.get("live_cash_authorized") is False
        and body.get("real_money_authorized") is False
        and body.get("disposition")
        in {"strict_flat_first_cutover", "owned_restart_recovery"}
        and isinstance(body.get("owned_open_orders"), list)
        and isinstance(body.get("owned_positions"), list)
        and isinstance(body.get("terminal_late_fill_quarantines"), list)
        and all(
            isinstance(body.get(name), bool)
            for name in (
                "recovery_required",
                "new_admissions_quarantined",
                "exposure_decreasing_only",
                "broker_inventory_flat",
            )
        )
        and body.get("recovery_required")
        == (body.get("disposition") == "owned_restart_recovery")
        and body.get("new_admissions_quarantined")
        == body.get("recovery_required")
        and body.get("exposure_decreasing_only")
        == body.get("recovery_required")
        and (
            body.get("disposition") != "strict_flat_first_cutover"
            or (
                body.get("broker_inventory_flat") is True
                and body.get("owned_open_orders") == []
                and body.get("owned_positions") == []
                and body.get("terminal_late_fill_quarantines") == []
            )
        )
    ):
        raise CapturedAlpacaPaperServiceError(
            "RESTART_INVENTORY_UNPROVEN",
            "captured PAPER restart receipt escaped its pinned identity",
        )
    return dict(receipt)


def _build_bracketed_restart_inventory_receipt(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    prepared: _PreparedCapturedPaperCapture,
    database_engine: Any,
    phase_one_reconciliation_receipt: Mapping[str, Any],
    restart_inventory_module: ModuleType,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, Any]:
    """Bracket one durable snapshot with two independently captured censuses."""

    connection = prepared.broker_snapshot.get("connection_receipt")
    if not isinstance(connection, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "RESTART_INVENTORY_UNPROVEN",
            "captured PAPER connection receipt disappeared before restart census",
        )
    connection_generation = str(
        connection.get("adapter_connection_generation") or ""
    )
    adapter_build_sha256 = _require_sha256(
        connection.get("adapter_build_sha256"), "restart adapter build"
    )
    phase_one_sha256 = _require_sha256(
        phase_one_reconciliation_receipt.get("receipt_sha256"),
        "phase-one reconciliation receipt",
    )
    read_binding = {
        "schema_version": "chili.captured-paper-restart-read-binding.v1",
        "purpose": "captured_paper_restart_inventory",
        "activation_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "code_build_sha256": verified.code_build_sha256,
        "settings_projection_sha256": verified.settings_projection_sha256,
        "capture_receipt_sha256": verified.capture_receipt_sha256,
        "expected_account_id": verified.expected_account_id,
        "connection_receipt_sha256": prepared.broker_snapshot[
            "connection_receipt_sha256"
        ],
        "adapter_connection_generation": connection_generation,
        "adapter_build_sha256": adapter_build_sha256,
        "phase_one_reconciliation_receipt_sha256": phase_one_sha256,
    }
    read_binding_json = _canonical_json_bytes(read_binding).decode("utf-8")
    read_binding_sha256 = hashlib.sha256(
        read_binding_json.encode("utf-8")
    ).hexdigest()
    adapter = prepared.adapter
    try:
        # The asymmetric order is intentional: it catches changes throughout
        # the repeatable-read durable snapshot instead of taking two adjacent
        # broker reads and then consulting the database afterward.
        opening_orders = adapter.get_paper_open_order_census(
            read_binding=read_binding
        )
        opening_positions = adapter.get_paper_position_census(
            read_binding=read_binding
        )
        lineages = restart_inventory_module.load_captured_paper_restart_lineages(
            database_engine,
            expected_account_id=verified.expected_account_id,
            expected_runtime_generation=verified.activation_generation,
        )
        opening = restart_inventory_module.classify_captured_paper_restart_inventory(
            expected_account_id=verified.expected_account_id,
            expected_runtime_generation=verified.activation_generation,
            expected_connection_generation=connection_generation,
            expected_adapter_build_sha256=adapter_build_sha256,
            expected_read_binding_sha256=read_binding_sha256,
            open_order_census=opening_orders,
            position_census=opening_positions,
            durable_lineages=lineages,
            observed_at=_aware_utc(wall_clock(), "opening restart census clock"),
        )
        closing_positions = adapter.get_paper_position_census(
            read_binding=read_binding
        )
        closing_orders = adapter.get_paper_open_order_census(
            read_binding=read_binding
        )
        closing = restart_inventory_module.classify_captured_paper_restart_inventory(
            expected_account_id=verified.expected_account_id,
            expected_runtime_generation=verified.activation_generation,
            expected_connection_generation=connection_generation,
            expected_adapter_build_sha256=adapter_build_sha256,
            expected_read_binding_sha256=read_binding_sha256,
            open_order_census=closing_orders,
            position_census=closing_positions,
            durable_lineages=lineages,
            observed_at=_aware_utc(wall_clock(), "closing restart census clock"),
        )
    except CapturedAlpacaPaperServiceError:
        raise
    except BaseException as exc:
        raise CapturedAlpacaPaperServiceError(
            "RESTART_INVENTORY_UNAVAILABLE",
            "captured PAPER restart inventory could not be proven",
        ) from exc

    opening = _verify_restart_classifier_receipt(
        opening,
        expected_account_id=verified.expected_account_id,
        expected_runtime_generation=verified.activation_generation,
        expected_connection_generation=connection_generation,
        expected_adapter_build_sha256=adapter_build_sha256,
        expected_read_binding_sha256=read_binding_sha256,
    )
    closing = _verify_restart_classifier_receipt(
        closing,
        expected_account_id=verified.expected_account_id,
        expected_runtime_generation=verified.activation_generation,
        expected_connection_generation=connection_generation,
        expected_adapter_build_sha256=adapter_build_sha256,
        expected_read_binding_sha256=read_binding_sha256,
    )
    dynamic_keys = {
        "open_order_census_sha256",
        "position_census_sha256",
        "observed_at",
        "receipt_canonical_json",
        "receipt_sha256",
    }
    opening_projection = {
        key: value for key, value in opening.items() if key not in dynamic_keys
    }
    closing_projection = {
        key: value for key, value in closing.items() if key not in dynamic_keys
    }
    census_pairs = (
        (opening_orders, closing_orders, "open-order"),
        (opening_positions, closing_positions, "position"),
    )
    for first, second, label in census_pairs:
        if not (
            isinstance(first, Mapping)
            and isinstance(second, Mapping)
            and first is not second
            and _require_sha256(
                first.get("query_receipt_sha256"), f"opening {label} census"
            )
            != _require_sha256(
                second.get("query_receipt_sha256"), f"closing {label} census"
            )
        ):
            raise CapturedAlpacaPaperServiceError(
                "RESTART_CENSUS_NOT_INDEPENDENT",
                f"captured PAPER {label} census was reused across the durable read",
            )
    if opening_projection != closing_projection:
        raise CapturedAlpacaPaperServiceError(
            "RESTART_INVENTORY_DRIFT",
            "captured PAPER broker inventory changed during durable restart read",
        )
    projection_sha256 = hashlib.sha256(
        _canonical_json_bytes(opening_projection)
    ).hexdigest()
    observed_at = _aware_utc(wall_clock(), "restart gate receipt clock")
    body = {
        "schema_version": _RESTART_GATE_SCHEMA_VERSION,
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "runtime_generation": verified.activation_generation,
        "broker_connection_generation": connection_generation,
        "broker_adapter_build_sha256": adapter_build_sha256,
        "broker_read_binding_canonical_json": read_binding_json,
        "broker_read_binding_sha256": read_binding_sha256,
        "phase_one_reconciliation_receipt_sha256": phase_one_sha256,
        "opening_open_order_census_sha256": opening[
            "open_order_census_sha256"
        ],
        "opening_position_census_sha256": opening[
            "position_census_sha256"
        ],
        "closing_position_census_sha256": closing[
            "position_census_sha256"
        ],
        "closing_open_order_census_sha256": closing[
            "open_order_census_sha256"
        ],
        "opening_restart_receipt_sha256": opening["receipt_sha256"],
        "closing_restart_receipt_sha256": closing["receipt_sha256"],
        "stable_inventory_projection_sha256": projection_sha256,
        "durable_inventory_sha256": opening["durable_inventory_sha256"],
        "open_order_inventory_sha256": opening[
            "open_order_inventory_sha256"
        ],
        "position_inventory_sha256": opening["position_inventory_sha256"],
        "disposition": opening["disposition"],
        "recovery_required": opening["recovery_required"],
        "new_admissions_quarantined": opening[
            "new_admissions_quarantined"
        ],
        "exposure_decreasing_only": opening["exposure_decreasing_only"],
        "broker_inventory_flat": opening["broker_inventory_flat"],
        "observed_at": _iso(observed_at),
        "paper_execution_only": True,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    canonical = _canonical_json_bytes(body).decode("utf-8")
    return {
        **body,
        "receipt_canonical_json": canonical,
        "receipt_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _verify_restart_gate_receipt(
    receipt: Mapping[str, Any],
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    phase_one_reconciliation_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Re-verify the strict-flat gate before embedding it in readiness evidence."""

    expected_body_keys = {
        "schema_version",
        "account_scope",
        "expected_account_id",
        "runtime_generation",
        "broker_connection_generation",
        "broker_adapter_build_sha256",
        "broker_read_binding_canonical_json",
        "broker_read_binding_sha256",
        "phase_one_reconciliation_receipt_sha256",
        "opening_open_order_census_sha256",
        "opening_position_census_sha256",
        "closing_position_census_sha256",
        "closing_open_order_census_sha256",
        "opening_restart_receipt_sha256",
        "closing_restart_receipt_sha256",
        "stable_inventory_projection_sha256",
        "durable_inventory_sha256",
        "open_order_inventory_sha256",
        "position_inventory_sha256",
        "disposition",
        "recovery_required",
        "new_admissions_quarantined",
        "exposure_decreasing_only",
        "broker_inventory_flat",
        "observed_at",
        "paper_execution_only",
        "live_cash_authorized",
        "real_money_authorized",
    }
    if not isinstance(receipt, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "RESTART_GATE_UNPROVEN", "captured PAPER restart gate is missing"
        )
    canonical = str(receipt.get("receipt_canonical_json") or "")
    supplied = _require_sha256(receipt.get("receipt_sha256"), "restart gate")
    try:
        body = json.loads(canonical)
        read_binding = json.loads(
            str(receipt.get("broker_read_binding_canonical_json") or "")
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise CapturedAlpacaPaperServiceError(
            "RESTART_GATE_UNPROVEN", "captured PAPER restart gate is malformed"
        ) from exc
    echoed = dict(receipt)
    echoed.pop("receipt_canonical_json", None)
    echoed.pop("receipt_sha256", None)
    read_binding_json = str(body.get("broker_read_binding_canonical_json") or "")
    phase_one_sha256 = _require_sha256(
        phase_one_reconciliation_receipt.get("receipt_sha256"),
        "phase-one reconciliation receipt",
    )
    expected_read_binding = {
        "schema_version": "chili.captured-paper-restart-read-binding.v1",
        "purpose": "captured_paper_restart_inventory",
        "activation_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "code_build_sha256": verified.code_build_sha256,
        "settings_projection_sha256": verified.settings_projection_sha256,
        "capture_receipt_sha256": verified.capture_receipt_sha256,
        "expected_account_id": verified.expected_account_id,
        "connection_receipt_sha256": _require_sha256(
            read_binding.get("connection_receipt_sha256"),
            "restart read binding connection receipt",
        ),
        "adapter_connection_generation": body.get(
            "broker_connection_generation"
        ),
        "adapter_build_sha256": body.get("broker_adapter_build_sha256"),
        "phase_one_reconciliation_receipt_sha256": phase_one_sha256,
    }
    empty_inventory_sha256 = hashlib.sha256(b"[]").hexdigest()
    if not (
        isinstance(body, dict)
        and set(body) == expected_body_keys
        and body == echoed
        and _canonical_json_bytes(body).decode("utf-8") == canonical
        and hashlib.sha256(canonical.encode("utf-8")).hexdigest() == supplied
        and isinstance(read_binding, dict)
        and read_binding == expected_read_binding
        and _canonical_json_bytes(read_binding).decode("utf-8")
        == read_binding_json
        and hashlib.sha256(read_binding_json.encode("utf-8")).hexdigest()
        == body.get("broker_read_binding_sha256")
        and body.get("schema_version") == _RESTART_GATE_SCHEMA_VERSION
        and body.get("account_scope") == "alpaca:paper"
        and body.get("expected_account_id") == verified.expected_account_id
        and body.get("runtime_generation") == verified.activation_generation
        and body.get("phase_one_reconciliation_receipt_sha256")
        == phase_one_sha256
        and body.get("disposition") == "strict_flat_first_cutover"
        and body.get("recovery_required") is False
        and body.get("new_admissions_quarantined") is False
        and body.get("exposure_decreasing_only") is False
        and body.get("broker_inventory_flat") is True
        and body.get("paper_execution_only") is True
        and body.get("live_cash_authorized") is False
        and body.get("real_money_authorized") is False
        and body.get("opening_open_order_census_sha256")
        != body.get("closing_open_order_census_sha256")
        and body.get("opening_position_census_sha256")
        != body.get("closing_position_census_sha256")
        and body.get("opening_restart_receipt_sha256")
        != body.get("closing_restart_receipt_sha256")
        and body.get("durable_inventory_sha256") == empty_inventory_sha256
        and body.get("open_order_inventory_sha256") == empty_inventory_sha256
        and body.get("position_inventory_sha256") == empty_inventory_sha256
    ):
        raise CapturedAlpacaPaperServiceError(
            "RESTART_GATE_UNPROVEN",
            "captured PAPER restart gate is not strict-flat or identity-bound",
        )
    for name in expected_body_keys - {
        "schema_version",
        "account_scope",
        "expected_account_id",
        "runtime_generation",
        "broker_connection_generation",
        "broker_read_binding_canonical_json",
        "disposition",
        "recovery_required",
        "new_admissions_quarantined",
        "exposure_decreasing_only",
        "broker_inventory_flat",
        "observed_at",
        "paper_execution_only",
        "live_cash_authorized",
        "real_money_authorized",
    }:
        _require_sha256(body.get(name), f"restart gate {name}")
    _parse_utc_text(body.get("observed_at"), "restart gate observed_at")
    return dict(receipt)


def _recover_fenced_initial_generations(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    prepared: _PreparedCapturedPaperCapture,
    database_engine: Any,
    initial_recovery_module: ModuleType,
    assert_service_fence_held: Callable[[], None],
) -> tuple[Mapping[str, Any], ...]:
    """Recover only exact PREOWNER/PENDING rows while the service fence is held."""

    if not callable(assert_service_fence_held):
        raise CapturedAlpacaPaperServiceError(
            "FENCED_INITIAL_RECOVERY_UNAVAILABLE",
            "captured PAPER service-fence assertion is unavailable",
        )
    recover = getattr(
        initial_recovery_module,
        "recover_captured_paper_initial_preowner",
        None,
    )
    if not callable(recover):
        raise CapturedAlpacaPaperServiceError(
            "FENCED_INITIAL_RECOVERY_UNAVAILABLE",
            "captured PAPER initial recovery capability is unavailable",
        )
    try:
        from sqlalchemy import text

        assert_service_fence_held()
        with database_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, symbol FROM trading_automation_sessions "
                    "WHERE mode = 'live' AND venue = 'alpaca' "
                    "AND execution_family = 'alpaca_spot' AND ended_at IS NULL "
                    "AND ((state = 'captured_paper_preowner' "
                    "      AND source_node_id = 'captured_paper_initial_admission') "
                    " OR (state = 'queued_live' "
                    "      AND source_node_id = 'captured_paper_preowner_promotion')) "
                    "ORDER BY id"
                )
            ).mappings().all()
        receipts: list[Mapping[str, Any]] = []
        for row in rows:
            assert_service_fence_held()
            symbol = str(row["symbol"] or "").strip().upper()
            receipt = recover(
                database_engine,
                session_id=int(row["id"]),
                expected_account_id=verified.expected_account_id,
                expected_runtime_generation=verified.activation_generation,
                expected_code_build_sha256=verified.code_build_sha256,
                expected_config_sha256=(
                    prepared.host.captured_paper_config_sha256_for(symbol)
                ),
                expected_capture_receipt_sha256=(
                    verified.capture_receipt_sha256
                ),
                assert_service_fence_held=assert_service_fence_held,
            )
            payload = receipt.to_dict()
            if not isinstance(payload, Mapping):
                raise TypeError("initial recovery receipt is not a mapping")
            receipts.append(dict(payload))
        assert_service_fence_held()
        return tuple(receipts)
    except CapturedAlpacaPaperServiceError:
        raise
    except BaseException as exc:
        raise CapturedAlpacaPaperServiceError(
            "FENCED_INITIAL_RECOVERY_UNAVAILABLE",
            "captured PAPER initial generations could not be recovered",
        ) from exc


def _build_fenced_prestart_revalidation_receipt(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    prepared: _PreparedCapturedPaperCapture,
    database_engine: Any,
    phase_one_reconciliation_receipt: Mapping[str, Any],
    baseline_restart_inventory_receipt: Mapping[str, Any],
    restart_inventory_module: ModuleType,
    service_fence_module: ModuleType,
    recover_initial_generations: Callable[[], Sequence[Mapping[str, Any]]],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, Any]:
    """Re-prove flat durable+broker state while the process fence is held.

    Composition performs a thorough restart census before the supervisor owns
    the PostgreSQL process fence.  A generic arm transaction could otherwise
    commit in that gap.  The supervisor invokes this callback immediately after
    acquiring the session lock and before starting a provider, runtime, worker,
    or live tick.  The fresh one-statement durable inventory catches bare
    sessions/claims/reservations that the fully joined restart-lineage loader is
    intentionally unable to classify.
    """

    phase_one = _verify_phase_one_reconciliation_receipt(
        phase_one_reconciliation_receipt,
        activation_generation=verified.activation_generation,
    )
    baseline = _verify_restart_gate_receipt(
        baseline_restart_inventory_receipt,
        verified=verified,
        phase_one_reconciliation_receipt=phase_one,
    )
    if not callable(recover_initial_generations):
        raise CapturedAlpacaPaperServiceError(
            "FENCED_INITIAL_RECOVERY_UNAVAILABLE",
            "captured PAPER initial recovery callback is unavailable",
        )
    try:
        initial_recovery_receipts = tuple(recover_initial_generations())
    except CapturedAlpacaPaperServiceError:
        raise
    except BaseException as exc:
        raise CapturedAlpacaPaperServiceError(
            "FENCED_INITIAL_RECOVERY_UNAVAILABLE",
            "captured PAPER initial recovery callback failed",
        ) from exc
    if any(not isinstance(row, Mapping) for row in initial_recovery_receipts):
        raise CapturedAlpacaPaperServiceError(
            "FENCED_INITIAL_RECOVERY_UNAVAILABLE",
            "captured PAPER initial recovery receipt is malformed",
        )
    recovery_inventory = [dict(row) for row in initial_recovery_receipts]
    recovery_inventory_sha256 = hashlib.sha256(
        _canonical_json_bytes(recovery_inventory)
    ).hexdigest()
    reader = getattr(
        service_fence_module,
        "read_captured_paper_prestart_admission_inventory",
        None,
    )
    if not callable(reader):
        raise CapturedAlpacaPaperServiceError(
            "FENCED_PRESTART_INVENTORY_UNAVAILABLE",
            "captured PAPER durable prestart inventory reader is unavailable",
        )
    try:
        inventory = reader(database_engine)
    except BaseException as exc:
        raise CapturedAlpacaPaperServiceError(
            "FENCED_PRESTART_INVENTORY_UNAVAILABLE",
            "captured PAPER durable prestart inventory could not be read",
        ) from exc
    expected_inventory_keys = {
        "schema_version",
        "account_scope",
        "active_sessions",
        "active_action_claims",
        "active_reservations",
        "reserved_opportunities",
        "active_outbox_rows",
        "active_fill_watches",
        "active_total",
        "empty",
        "live_cash_authorized",
        "real_money_authorized",
        "inventory_canonical_json",
        "inventory_sha256",
    }
    inventory_body = dict(inventory) if isinstance(inventory, Mapping) else {}
    canonical = str(inventory_body.pop("inventory_canonical_json", "") or "")
    inventory_sha256 = str(inventory_body.pop("inventory_sha256", "") or "")
    count_names = {
        "active_sessions",
        "active_action_claims",
        "active_reservations",
        "reserved_opportunities",
        "active_outbox_rows",
        "active_fill_watches",
    }
    if not (
        isinstance(inventory, Mapping)
        and set(inventory) == expected_inventory_keys
        and inventory.get("schema_version")
        == "chili.captured-paper-prestart-admission-inventory.v1"
        and inventory.get("account_scope") == "alpaca:paper"
        and all(
            isinstance(inventory.get(name), int)
            and not isinstance(inventory.get(name), bool)
            and inventory.get(name) >= 0
            for name in count_names
        )
        and inventory.get("active_total")
        == sum(int(inventory[name]) for name in count_names)
        and inventory.get("empty") is True
        and inventory.get("active_total") == 0
        and inventory.get("live_cash_authorized") is False
        and inventory.get("real_money_authorized") is False
        and _canonical_json_bytes(inventory_body).decode("utf-8") == canonical
        and _require_sha256(
            inventory_sha256, "fenced prestart admission inventory"
        )
        == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    ):
        raise CapturedAlpacaPaperServiceError(
            "FENCED_PRESTART_DURABLE_DRIFT",
            "a durable Alpaca arm/order owner appeared after restart census",
        )

    fresh = _build_bracketed_restart_inventory_receipt(
        verified=verified,
        prepared=prepared,
        database_engine=database_engine,
        phase_one_reconciliation_receipt=phase_one,
        restart_inventory_module=restart_inventory_module,
        wall_clock=wall_clock,
    )
    fresh = _verify_restart_gate_receipt(
        fresh,
        verified=verified,
        phase_one_reconciliation_receipt=phase_one,
    )
    identity_fields = (
        "account_scope",
        "expected_account_id",
        "runtime_generation",
        "broker_connection_generation",
        "broker_adapter_build_sha256",
        "phase_one_reconciliation_receipt_sha256",
        "disposition",
        "recovery_required",
        "new_admissions_quarantined",
        "exposure_decreasing_only",
        "broker_inventory_flat",
        "paper_execution_only",
        "live_cash_authorized",
        "real_money_authorized",
    )
    if any(fresh.get(name) != baseline.get(name) for name in identity_fields):
        raise CapturedAlpacaPaperServiceError(
            "FENCED_PRESTART_RESTART_DRIFT",
            "captured PAPER restart identity changed after process fencing",
        )

    body = {
        "schema_version": "chili.captured-paper-fenced-prestart.v1",
        "verdict": "CAPTURED_ALPACA_PAPER_FENCED_PRESTART_REVALIDATED",
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "runtime_generation": verified.activation_generation,
        "baseline_restart_gate_receipt_sha256": _require_sha256(
            baseline.get("receipt_sha256"), "baseline restart gate"
        ),
        "restart_gate_receipt_sha256": _require_sha256(
            fresh.get("receipt_sha256"), "fenced restart gate"
        ),
        "admission_inventory_sha256": inventory_sha256,
        "initial_recovery_count": len(recovery_inventory),
        "initial_recovery_inventory_sha256": recovery_inventory_sha256,
        "durable_admission_drift": False,
        "broker_inventory_flat": True,
        "paper_execution_only": True,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    return {
        **body,
        "receipt_sha256": hashlib.sha256(_canonical_json_bytes(body)).hexdigest(),
    }


def _inside_local_roots(path: Path, roots: Sequence[str | Path]) -> bool:
    for raw_root in roots:
        root = Path(raw_root).resolve(strict=True)
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _strict_new_local_json_path(
    value: str | Path,
    *,
    allowed_roots: Sequence[str | Path],
) -> Path:
    path = Path(value)
    if (
        not path.is_absolute()
        or str(path).startswith(("\\\\", "//"))
        or path.suffix.lower() != ".json"
    ):
        raise CapturedAlpacaPaperServiceError(
            "INVALID_OUTPUT_PATH", "no-order receipt path must be local absolute JSON"
        )
    parent = path.parent.resolve(strict=True)
    cursor = parent
    while True:
        info = os.lstat(cursor)
        attrs = int(getattr(info, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or attrs & _REPARSE_ATTRIBUTE:
            raise CapturedAlpacaPaperServiceError(
                "REPARSE_PATH", "no-order receipt parent traverses a reparse point"
            )
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    resolved = parent / path.name
    if not _inside_local_roots(resolved, allowed_roots):
        raise CapturedAlpacaPaperServiceError(
            "OUTPUT_OUTSIDE_ROOT", "no-order receipt path escaped allowed roots"
        )
    if resolved.exists():
        raise CapturedAlpacaPaperServiceError(
            "OUTPUT_ALREADY_EXISTS", "no-order receipt output is append-only"
        )
    return resolved


def _publish_canonical_json_once(path: Path, value: Mapping[str, Any]) -> str:
    raw = _canonical_json_bytes(value)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows rename is no-replace.  The prior existence check gives the
        # same append-only behavior on the supported activation host.
        os.rename(temporary, path)
        temporary = None
    except FileExistsError as exc:
        raise CapturedAlpacaPaperServiceError(
            "OUTPUT_ALREADY_EXISTS", "no-order receipt output already exists"
        ) from exc
    finally:
        if temporary is not None:
            with suppress(OSError):
                os.unlink(temporary)
    return hashlib.sha256(raw).hexdigest()


def _current_service_process_identity() -> Mapping[str, Any]:
    """Return the same exact service identity used by host cutover."""

    try:
        import psutil

        process = psutil.Process(os.getpid())
        executable_path = str(Path(process.exe()).resolve(strict=True))
        cmdline = tuple(str(item) for item in process.cmdline())
        create_time_ns = int(
            round(float(process.create_time()) * 1_000_000_000)
        )
    except Exception as exc:
        raise CapturedAlpacaPaperServiceError(
            "SERVICE_IDENTITY_NOT_INSPECTABLE",
            "service process identity could not be proven",
        ) from exc
    if create_time_ns <= 0 or not cmdline:
        raise CapturedAlpacaPaperServiceError(
            "SERVICE_IDENTITY_NOT_INSPECTABLE",
            "service process identity is incomplete",
        )
    return {
        "service_pid": os.getpid(),
        "service_create_time_ns": create_time_ns,
        "service_executable_path": executable_path,
        "service_executable_sha256": _sha256_file(Path(executable_path)),
        "service_cmdline_sha256": activation_contract.sha256_json(
            list(cmdline)
        ),
    }


def _stable_canonical_json_mapping(
    path: Path,
    *,
    field: str,
    max_bytes: int = 64 * 1024,
) -> Mapping[str, Any]:
    try:
        before = os.lstat(path)
        attrs = int(getattr(before, "st_file_attributes", 0) or 0)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or attrs & _REPARSE_ATTRIBUTE
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise OSError("artifact is not a bounded regular file")
        raw = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise CapturedAlpacaPaperServiceError(
            "HOST_ACTIVATION_ARTIFACT_UNREADABLE",
            f"{field} could not be read as stable local evidence",
        ) from exc
    stable = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if not stable or len(raw) != before.st_size:
        raise CapturedAlpacaPaperServiceError(
            "HOST_ACTIVATION_ARTIFACT_DRIFT",
            f"{field} changed while it was read",
        )
    value = _strict_json_value(raw, field=field)
    if not isinstance(value, Mapping) or raw != _canonical_json_bytes(value):
        raise CapturedAlpacaPaperServiceError(
            "HOST_ACTIVATION_ARTIFACT_INVALID",
            f"{field} is not one canonical JSON object",
        )
    return dict(value)


def _stable_canonical_json_lines(
    path: Path,
    *,
    field: str,
    max_bytes: int = _HOST_CUTOVER_JOURNAL_MAX_BYTES,
) -> tuple[Mapping[str, Any], ...]:
    """Read one stable, newline-terminated canonical JSONL artifact."""

    try:
        before = os.lstat(path)
        attrs = int(getattr(before, "st_file_attributes", 0) or 0)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or attrs & _REPARSE_ATTRIBUTE
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise OSError("artifact is not a bounded regular file")
        raw = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise CapturedAlpacaPaperServiceError(
            "HOST_ACTIVATION_ARTIFACT_UNREADABLE",
            f"{field} could not be read as stable local evidence",
        ) from exc
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or len(raw) != before.st_size
        or not raw.endswith(b"\n")
    ):
        raise CapturedAlpacaPaperServiceError(
            "HOST_ACTIVATION_ARTIFACT_DRIFT",
            f"{field} changed or was incomplete while it was read",
        )
    rows: list[Mapping[str, Any]] = []
    for index, line in enumerate(raw.splitlines()):
        value = _strict_json_value(line, field=f"{field}[{index}]")
        if not isinstance(value, Mapping) or line != _canonical_json_bytes(value):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ARTIFACT_INVALID",
                f"{field}[{index}] is not one canonical JSON object",
            )
        rows.append(dict(value))
    if not rows:
        raise CapturedAlpacaPaperServiceError(
            "HOST_ACTIVATION_ARTIFACT_INVALID", f"{field} is empty"
        )
    return tuple(rows)


def _host_cutover_issuer_process_identity(pid: int) -> Mapping[str, Any]:
    """Inspect the still-live host-cutover issuer by PID and process birth."""

    try:
        import psutil

        process = psutil.Process(int(pid))
        create_time_ns = int(round(float(process.create_time()) * 1_000_000_000))
        executable_path = str(Path(process.exe()).resolve(strict=True))
        cmdline = tuple(str(item) for item in process.cmdline())
    except Exception as exc:
        raise CapturedAlpacaPaperServiceError(
            "HOST_ACTIVATION_ISSUER_NOT_INSPECTABLE",
            "host-cutover issuer process is absent or cannot be inspected",
        ) from exc
    if create_time_ns <= 0 or not cmdline:
        raise CapturedAlpacaPaperServiceError(
            "HOST_ACTIVATION_ISSUER_NOT_INSPECTABLE",
            "host-cutover issuer process identity is incomplete",
        )
    return {
        "issuer_pid": int(pid),
        "issuer_create_time_ns": create_time_ns,
        "issuer_executable_path": executable_path,
        "issuer_executable_sha256": _sha256_file(Path(executable_path)),
        "issuer_cmdline": list(cmdline),
        "issuer_cmdline_sha256": activation_contract.sha256_json(list(cmdline)),
    }


def _long_option_values(argv: Sequence[str]) -> Mapping[str, tuple[str, ...]]:
    """Parse the cutover CLI's value-taking long options without accepting code."""

    values: dict[str, list[str]] = {}
    index = 0
    while index < len(argv):
        token = str(argv[index])
        if not token.startswith("--") or token == "--":
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_COMMAND_INVALID",
                "host-cutover issuer command has an unexpected positional token",
            )
        if "=" in token:
            option, value = token.split("=", 1)
            index += 1
        else:
            option = token
            if index + 1 >= len(argv):
                raise CapturedAlpacaPaperServiceError(
                    "HOST_ACTIVATION_ISSUER_COMMAND_INVALID",
                    "host-cutover issuer command has a value-less option",
                )
            value = str(argv[index + 1])
            index += 2
        if not option.startswith("--") or not value or value.startswith("--"):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_COMMAND_INVALID",
                "host-cutover issuer command has an invalid option value",
            )
        values.setdefault(option, []).append(value)
    return {key: tuple(items) for key, items in values.items()}


_ACTIVE_START_AUTHORITY_BODY_KEYS = frozenset(
    {
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
)


def _validate_active_start_authority_receipt(
    value: Mapping[str, Any],
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
) -> Mapping[str, Any]:
    """Verify the complete pre-worker PAPER evidence object and self-digest."""

    if not isinstance(value, Mapping):
        raise CapturedAlpacaPaperServiceError(
            "ACTIVE_START_AUTHORITY_INVALID",
            "active-start authority is not an object",
        )
    body = dict(value)
    claimed = _require_sha256(
        body.pop("authority_sha256", None), "active-start authority"
    )
    broker_fixed_point = body.get("broker_fixed_point")
    final_kill_switch = body.get("final_kill_switch_query")
    if not isinstance(broker_fixed_point, Mapping) or not isinstance(
        final_kill_switch, Mapping
    ):
        raise CapturedAlpacaPaperServiceError(
            "ACTIVE_START_AUTHORITY_INVALID",
            "active-start authority lacks durable broker or kill-switch evidence",
        )
    kill_switch_body = dict(final_kill_switch)
    kill_switch_self_sha256 = _require_sha256(
        kill_switch_body.pop("query_receipt_sha256", None),
        "final kill-switch query",
    )
    digest_fields = (
        "activation_manifest_sha256",
        "kill_switch_receipt_sha256",
        "launcher_attestation_sha256",
        "host_activation_permit_sha256",
        "host_quiet_horizon_event_sha256",
        "broker_fixed_point_sha256",
        "post_permit_broker_snapshot_sha256",
        "order_transition_fence_sha256",
        "fill_activity_fence_sha256",
        "final_kill_switch_query_sha256",
    )
    try:
        valid = (
            set(body) == _ACTIVE_START_AUTHORITY_BODY_KEYS
            and activation_contract.sha256_json(body) == claimed
            and all(_SHA256_RE.fullmatch(str(body.get(field) or "")) for field in digest_fields)
            and body.get("schema_version")
            == _ACTIVE_START_AUTHORITY_SCHEMA_VERSION
            and body.get("verdict")
            == "CAPTURED_ALPACA_PAPER_ACTIVE_START_AUTHORIZED"
            and body.get("account_scope") == "alpaca:paper"
            and body.get("expected_account_id") == verified.expected_account_id
            and body.get("runtime_generation") == verified.activation_generation
            and body.get("activation_manifest_sha256") == verified.manifest_sha256
            and body.get("launcher_attestation_consumed") is True
            and body.get("host_activation_permit_consumed") is True
            and body.get("paper_order_submission_authorized") is True
            and body.get("live_cash_authorized") is False
            and body.get("real_money_authorized") is False
            and activation_contract.sha256_json(dict(broker_fixed_point))
            == body.get("broker_fixed_point_sha256")
            and activation_contract.sha256_json(
                dict(broker_fixed_point["second_snapshot"])
            )
            == body.get("post_permit_broker_snapshot_sha256")
            and activation_contract.sha256_json(
                dict(broker_fixed_point["second_order_census"])
            )
            == body.get("order_transition_fence_sha256")
            and activation_contract.sha256_json(
                dict(broker_fixed_point["second_fill_activity_census"])
            )
            == body.get("fill_activity_fence_sha256")
            and broker_fixed_point.get("schema_version")
            == "chili.captured-paper-broker-fixed-point.v1"
            and broker_fixed_point.get("verdict")
            == "PAPER_BROKER_QUIET_FIXED_POINT"
            and broker_fixed_point.get("account_scope") == "alpaca:paper"
            and broker_fixed_point.get("expected_account_id")
            == verified.expected_account_id
            and broker_fixed_point.get("activation_generation")
            == verified.activation_generation
            and broker_fixed_point.get("activation_manifest_sha256")
            == verified.manifest_sha256
            and broker_fixed_point.get("assumption_bound") is True
            and broker_fixed_point.get("live_cash_certification") is False
            and activation_contract.sha256_json(kill_switch_body)
            == kill_switch_self_sha256
            and activation_contract.sha256_json(dict(final_kill_switch))
            == body.get("final_kill_switch_query_sha256")
            and final_kill_switch.get("schema_version")
            == "chili.captured-paper-kill-switch-query.v1"
            and final_kill_switch.get("account_scope") == "alpaca:paper"
            and final_kill_switch.get("expected_account_id")
            == verified.expected_account_id
            and final_kill_switch.get("activation_generation")
            == verified.activation_generation
            and final_kill_switch.get("active") is False
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise CapturedAlpacaPaperServiceError(
            "ACTIVE_START_AUTHORITY_INVALID",
            "active-start broker, kill-switch, identity, or hash evidence is invalid",
        )
    return dict(value)


class _CapturedPaperHostActivationHandshake:
    """Two-phase host authorization around order-capable worker startup."""

    _PERMIT_KEYS = {
        "schema_version",
        "state",
        "activation_generation",
        "manifest_path",
        "manifest_sha256",
        "candidate_root",
        "journal_root",
        "account_scope",
        "expected_account_id",
        "service_pid",
        "service_create_time_ns",
        "service_executable_path",
        "service_executable_sha256",
        "service_cmdline",
        "service_cmdline_sha256",
        "service_role",
        "service_script_path",
        "service_script_sha256",
        "challenge_sha256",
        "prepared_receipt_sha256",
        *_HOST_DISPATCH_LOCK_IDENTITY_KEYS,
        "issued_at",
        "valid_until",
        "permit_path",
        "journal_path",
        "journal_transaction_id",
        "journal_authorization_sequence",
        "journal_authorization_event_sha256",
        "journal_authorization_event",
        "issuer_pid",
        "issuer_create_time_ns",
        "issuer_executable_path",
        "issuer_executable_sha256",
        "issuer_cmdline",
        "issuer_cmdline_sha256",
        "issuer_source_path",
        "issuer_source_sha256",
        "live_cash_authorized",
        "real_money_authorized",
        "permit_sha256",
    }

    def __init__(
        self,
        *,
        ready_path: Path,
        permit_path: Path,
        started_path: Path,
        active_start_evidence_path: Path,
        revocation_requested_path: Path,
        revoked_path: Path,
        dispatch_lock_path: Path,
        dispatch_lock_identity: Mapping[str, Any],
        verified: activation_contract.VerifiedCapturedPaperActivation,
        process_identity: Mapping[str, Any],
        challenge_sha256: str,
        allowed_roots: Sequence[Path],
        issuer_process_probe: Callable[[int], Mapping[str, Any]],
        wall_clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ready_path = ready_path
        self.permit_path = permit_path
        self.started_path = started_path
        self.active_start_evidence_path = active_start_evidence_path
        self.revocation_requested_path = revocation_requested_path
        self.revoked_path = revoked_path
        self.dispatch_lock_path = dispatch_lock_path
        if set(dispatch_lock_identity) != _HOST_DISPATCH_LOCK_IDENTITY_KEYS:
            raise CapturedAlpacaPaperServiceError(
                "HOST_DISPATCH_LOCK_INVALID",
                "host dispatch authority lock identity has an unexpected schema",
            )
        self._dispatch_lock_identity = dict(dispatch_lock_identity)
        self._verified = verified
        self._identity = dict(process_identity)
        self._challenge_sha256 = _require_sha256(
            challenge_sha256, "host startup challenge"
        )
        self._allowed_roots = tuple(allowed_roots)
        self._issuer_process_probe = issuer_process_probe
        self._wall_clock = wall_clock
        self._monotonic = monotonic_clock
        self._wait = wait
        self._lock = threading.Lock()
        self._prepared_sha256: str | None = None
        self._permit_sha256: str | None = None
        self._permit_body: Mapping[str, Any] | None = None
        self._quiet_horizon_event_sha256: str | None = None
        self._active_start_authority_body: Mapping[str, Any] | None = None
        self._active_start_evidence_artifact_sha256: str | None = None
        self._started_sha256: str | None = None
        self._started_body: Mapping[str, Any] | None = None
        self._apply_completed_event_sha256: str | None = None
        self._apply_completed_event_body: Mapping[str, Any] | None = None

    @property
    def quiet_horizon_event_sha256(self) -> str:
        if self._quiet_horizon_event_sha256 is None:
            raise CapturedAlpacaPaperServiceError(
                "HOST_QUIET_HORIZON_UNVERIFIED",
                "host quiet-horizon evidence has not been consumed",
            )
        return self._quiet_horizon_event_sha256

    @property
    def active_start_evidence_artifact_sha256(self) -> str:
        if self._active_start_evidence_artifact_sha256 is None:
            raise CapturedAlpacaPaperServiceError(
                "ACTIVE_START_EVIDENCE_UNPUBLISHED",
                "durable active-start evidence has not been published",
            )
        return self._active_start_evidence_artifact_sha256

    def _assert_active_start_evidence_current_unlocked(self) -> None:
        expected = self._active_start_authority_body
        expected_artifact_sha256 = self._active_start_evidence_artifact_sha256
        if expected is None or expected_artifact_sha256 is None:
            raise CapturedAlpacaPaperServiceError(
                "ACTIVE_START_EVIDENCE_UNPUBLISHED",
                "durable active-start evidence is unavailable",
            )
        current = _stable_canonical_json_mapping(
            self.active_start_evidence_path,
            field="active-start evidence",
            max_bytes=_ACTIVE_START_EVIDENCE_MAX_BYTES,
        )
        if (
            dict(current) != dict(expected)
            or hashlib.sha256(_canonical_json_bytes(current)).hexdigest()
            != expected_artifact_sha256
        ):
            raise CapturedAlpacaPaperServiceError(
                "ACTIVE_START_EVIDENCE_DRIFT",
                "durable active-start evidence changed after publication",
            )
        _validate_active_start_authority_receipt(
            current,
            verified=self._verified,
        )

    def publish_active_start_evidence(
        self, active_start_authority: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Durably publish the complete authority before any worker starts."""

        with self._lock:
            if self._active_start_authority_body is not None:
                raise CapturedAlpacaPaperServiceError(
                    "ACTIVE_START_EVIDENCE_ALREADY_PUBLISHED",
                    "active-start evidence is append-only",
                )
            authority = _validate_active_start_authority_receipt(
                active_start_authority,
                verified=self._verified,
            )
            raw = _canonical_json_bytes(authority)
            if len(raw) > _ACTIVE_START_EVIDENCE_MAX_BYTES:
                raise CapturedAlpacaPaperServiceError(
                    "ACTIVE_START_EVIDENCE_TOO_LARGE",
                    "active-start evidence exceeds its bounded artifact size",
                )
            artifact_sha256 = hashlib.sha256(raw).hexdigest()
            published_sha256 = _publish_canonical_json_once(
                self.active_start_evidence_path,
                authority,
            )
            if published_sha256 != artifact_sha256:
                raise CapturedAlpacaPaperServiceError(
                    "ACTIVE_START_EVIDENCE_PUBLISH_MISMATCH",
                    "active-start evidence bytes differ at publication",
                )
            self._active_start_authority_body = dict(authority)
            self._active_start_evidence_artifact_sha256 = artifact_sha256
            self._assert_active_start_evidence_current_unlocked()
            return {
                "authority_sha256": authority["authority_sha256"],
                "artifact_sha256": artifact_sha256,
            }

    def assert_active_start_evidence_current(self) -> None:
        with self._lock:
            self._assert_active_start_evidence_current_unlocked()

    @classmethod
    def prepare(
        cls,
        *,
        ready_output: str | Path,
        verified: activation_contract.VerifiedCapturedPaperActivation,
        allowed_roots: Sequence[str | Path],
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
        process_probe: Callable[[], Mapping[str, Any]] = (
            _current_service_process_identity
        ),
        issuer_process_probe: Callable[[int], Mapping[str, Any]] = (
            _host_cutover_issuer_process_identity
        ),
        challenge_factory: Callable[[], str] = lambda: secrets.token_hex(32),
    ) -> "_CapturedPaperHostActivationHandshake":
        base = _strict_new_local_json_path(
            ready_output, allowed_roots=allowed_roots
        )
        derived = {
            "permit": base.with_name(base.name + ".permit.json"),
            "started": base.with_name(base.name + ".started.json"),
            "active_start_evidence": base.with_name(
                base.name + ".active-start-evidence.json"
            ),
            "revocation_requested": base.with_name(
                base.name + ".revocation-requested.json"
            ),
            "revoked": base.with_name(base.name + ".revoked.json"),
        }
        for path in derived.values():
            _strict_new_local_json_path(path, allowed_roots=allowed_roots)
        identity = process_probe()
        expected_identity_keys = {
            "service_pid",
            "service_create_time_ns",
            "service_executable_path",
            "service_executable_sha256",
            "service_cmdline_sha256",
        }
        if not isinstance(identity, Mapping) or set(identity) != expected_identity_keys:
            raise CapturedAlpacaPaperServiceError(
                "SERVICE_IDENTITY_INVALID",
                "service process identity has an unexpected schema",
            )
        dispatch_lock_path = base.with_name(base.name + ".dispatch.lock")
        if (
            not dispatch_lock_path.is_absolute()
            or str(dispatch_lock_path).startswith(("\\\\", "//"))
            or not _inside_local_roots(dispatch_lock_path, allowed_roots)
            or os.path.lexists(dispatch_lock_path)
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_DISPATCH_LOCK_INVALID",
                "host dispatch authority lock path is not a new local artifact",
            )
        try:
            host_cutover = importlib.import_module(
                "scripts.captured_paper_host_cutover"
            )
            dispatch_lock_identity = host_cutover.create_startup_dispatch_lock(
                dispatch_lock_path
            )
        except Exception as exc:
            raise CapturedAlpacaPaperServiceError(
                "HOST_DISPATCH_LOCK_INVALID",
                "host dispatch authority lock could not be created and sealed",
            ) from exc
        roots = tuple(Path(item).resolve(strict=True) for item in allowed_roots)
        return cls(
            ready_path=base,
            permit_path=derived["permit"],
            started_path=derived["started"],
            active_start_evidence_path=derived["active_start_evidence"],
            revocation_requested_path=derived["revocation_requested"],
            revoked_path=derived["revoked"],
            dispatch_lock_path=dispatch_lock_path,
            dispatch_lock_identity=dispatch_lock_identity,
            verified=verified,
            process_identity=identity,
            challenge_sha256=challenge_factory(),
            allowed_roots=roots,
            issuer_process_probe=issuer_process_probe,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
            wait=wait,
        )

    def _common_body(self) -> dict[str, Any]:
        return {
            "activation_generation": self._verified.activation_generation,
            "manifest_sha256": self._verified.manifest_sha256,
            "account_scope": "alpaca:paper",
            "expected_account_id": self._verified.expected_account_id,
            **self._identity,
            "challenge_sha256": self._challenge_sha256,
        }

    def assert_not_revoked(self) -> None:
        if os.path.lexists(self.revocation_requested_path) or os.path.lexists(
            self.revoked_path
        ):
            # Presence alone is authoritative for fail-closed behavior.  A
            # malformed/racing request or final revocation must never be
            # interpreted as continued broker authority.
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_REVOKED",
                "host cutover revoked PAPER worker authority",
            )

    @contextmanager
    def hold_dispatch_authority(self):
        """Linearize one PAPER POST against host permit revocation.

        Rollback takes the same blocking byte lock before publishing the
        fail-closed revocation tombstone.  Holding it through the synchronous
        adapter call makes a POST either wholly pre-revocation (and therefore
        MAY_POST) or wholly suppressed after revocation; a pathname check alone
        cannot provide that interprocess ordering.
        """

        if self._permit_sha256 is None or self._permit_body is None:
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_PERMIT_REQUIRED",
                "PAPER dispatch cannot precede a consumed host permit",
            )
        self.assert_dispatch_authority_current()
        try:
            host_cutover = importlib.import_module(
                "scripts.captured_paper_host_cutover"
            )
            authority = host_cutover.hold_startup_dispatch_lock(
                self._dispatch_lock_identity,
                timeout_seconds=host_cutover.STARTUP_DISPATCH_LOCK_WAIT_SECONDS,
            )
            authority.__enter__()
        except Exception as exc:
            raise CapturedAlpacaPaperServiceError(
                "HOST_DISPATCH_AUTHORITY_UNAVAILABLE",
                "host dispatch authority could not be acquired exactly",
            ) from exc
        try:
            self.assert_dispatch_authority_current()
            # Exceptions raised by the broker call belong to the transport
            # lifecycle and must not be relabeled as host-lock failures.
            yield
        finally:
            authority.__exit__(None, None, None)

    def publish_prepared(self) -> Mapping[str, Any]:
        with self._lock:
            if self._prepared_sha256 is not None:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_PREPARED_ALREADY_PUBLISHED",
                    "host startup PREPARED receipt is one-shot",
                )
            self.assert_not_revoked()
            now = _aware_utc(self._wall_clock(), "host prepared clock")
            valid_until = min(
                now + timedelta(seconds=_HOST_ACTIVATION_MAX_AGE_SECONDS),
                self._verified.expires_at,
            )
            if valid_until <= now:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_PREPARED_EXPIRED",
                    "activation expired before host PREPARED publication",
                )
            body = {
                "schema_version": _HOST_PREPARED_SCHEMA_VERSION,
                "state": "PREPARED",
                **self._common_body(),
                **self._dispatch_lock_identity,
                "prepared_at": _iso(now),
                "valid_until": _iso(valid_until),
                "workers_started": False,
                "paper_execution_started": False,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
            body["receipt_sha256"] = activation_contract.sha256_json(body)
            _publish_canonical_json_once(self.ready_path, body)
            self._prepared_sha256 = str(body["receipt_sha256"])
            return dict(body)

    def _verify_live_issuer_and_command(
        self,
        value: Mapping[str, Any],
        *,
        source_path: Path,
        executable_path: Path,
        journal_path: Path,
    ) -> None:
        pid = value.get("issuer_pid")
        if type(pid) is not int or int(pid) <= 0:
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_INVALID",
                "host-cutover issuer PID is invalid",
            )
        live = self._issuer_process_probe(int(pid))
        required_live_keys = {
            "issuer_pid",
            "issuer_create_time_ns",
            "issuer_executable_path",
            "issuer_executable_sha256",
            "issuer_cmdline",
            "issuer_cmdline_sha256",
        }
        if not isinstance(live, Mapping) or set(live) != required_live_keys:
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_INVALID",
                "live host-cutover issuer identity has an unexpected schema",
            )
        declared_cmdline = value.get("issuer_cmdline")
        if not isinstance(declared_cmdline, list) or not all(
            isinstance(item, str) and item for item in declared_cmdline
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_INVALID",
                "host-cutover issuer command is absent or malformed",
            )
        live_cmdline = live.get("issuer_cmdline")
        if not isinstance(live_cmdline, list):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_INVALID",
                "live host-cutover issuer command is not inspectable",
            )
        expected_live = {
            "issuer_pid": int(pid),
            "issuer_create_time_ns": value.get("issuer_create_time_ns"),
            "issuer_executable_path": str(executable_path),
            "issuer_executable_sha256": value.get("issuer_executable_sha256"),
            "issuer_cmdline": declared_cmdline,
            "issuer_cmdline_sha256": value.get("issuer_cmdline_sha256"),
        }
        if dict(live) != expected_live or live_cmdline != declared_cmdline:
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_DRIFT",
                "host-cutover issuer process changed or exited before permit consume",
            )
        if activation_contract.sha256_json(declared_cmdline) != value.get(
            "issuer_cmdline_sha256"
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_INVALID",
                "host-cutover issuer command hash does not match its exact argv",
            )

        if _canonical_process_path(
            declared_cmdline[0], "host-cutover interpreter"
        ) != _canonical_process_path(executable_path, "host-cutover executable"):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_COMMAND_INVALID",
                "host-cutover issuer interpreter differs from the live executable",
            )
        cutover = self._verified.manifest.get("cutover")
        if not isinstance(cutover, Mapping):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_COMMAND_INVALID",
                "host-cutover issuer lacks the sealed stage0 binding",
            )
        stage0_path = _strict_local_file(
            str(cutover.get("stage0_path") or ""),
            "host-cutover isolated stage0",
        )
        stage0_sha = _require_sha256(
            cutover.get("stage0_sha256"), "host-cutover isolated stage0"
        )
        source_sha = _require_sha256(
            value.get("issuer_source_sha256"), "host-cutover issuer source"
        )
        stage0_prefix = (
            "-I",
            "-S",
            "-B",
            str(stage0_path),
            "--manifest",
            str(self._verified.manifest_path),
            "--manifest-sha256",
            self._verified.manifest_sha256,
            "--candidate-root",
            str(self._verified.candidate_root),
            "--target-role",
            "captured_paper_host_cutover",
            "--target",
            str(source_path),
            "--target-sha256",
            source_sha,
            "--",
        )
        if not (
            tuple(declared_cmdline[1 : 1 + len(stage0_prefix)]) == stage0_prefix
            and _sha256_file(stage0_path) == stage0_sha
            and len(declared_cmdline) > 1 + len(stage0_prefix)
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_COMMAND_INVALID",
                "host-cutover issuer did not use the exact sealed isolated stage0",
            )
        options = _long_option_values(
            declared_cmdline[1 + len(stage0_prefix) :]
        )
        required_options = {
            "--mode",
            "--manifest",
            "--manifest-sha256",
            "--candidate-root",
            "--allow-read-root",
            "--task-snapshot",
            "--process-snapshot",
            "--restore-plan",
            "--candidate-task-template",
            "--candidate-action",
            "--journal-root",
            "--confirm-fake-money-paper",
        }
        if set(options) != required_options or any(
            len(items) != 1
            for option, items in options.items()
            if option != "--allow-read-root"
        ) or not options.get("--allow-read-root"):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_COMMAND_INVALID",
                "host-cutover issuer command does not match the exact Apply schema",
            )

        def one(option: str) -> str:
            return options[option][0]

        expected_journal_root = journal_path.parent.parent.resolve(strict=True)
        declared_read_roots = tuple(
            Path(item).resolve(strict=True) for item in options["--allow-read-root"]
        )
        if not (
            one("--mode") == "Apply"
            and one("--manifest-sha256") == self._verified.manifest_sha256
            and one("--confirm-fake-money-paper")
            == _HOST_CUTOVER_APPLY_CONFIRMATION
            and Path(one("--manifest")).resolve(strict=True)
            == self._verified.manifest_path
            and Path(one("--candidate-root")).resolve(strict=True)
            == self._verified.candidate_root
            and Path(one("--journal-root")).resolve(strict=True)
            == expected_journal_root
            and _inside_local_roots(journal_path, declared_read_roots)
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_ISSUER_COMMAND_INVALID",
                "host-cutover issuer command is not the exact sealed PAPER Apply",
            )

    def _verify_journal_authorization(
        self,
        value: Mapping[str, Any],
        *,
        journal_path: Path,
    ) -> None:
        expected_transaction = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "chili:captured-paper-cutover:"
                f"{self._verified.activation_generation}:"
                f"{self._verified.manifest_sha256}",
            )
        )
        if not (
            value.get("journal_transaction_id") == expected_transaction
            and journal_path.name == f"{self._verified.manifest_sha256}.jsonl"
            and journal_path.parent.name == self._verified.activation_generation
            and _inside_local_roots(journal_path, self._allowed_roots)
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_JOURNAL_INVALID",
                "host-cutover journal identity escaped the sealed activation",
            )
        rows = _stable_canonical_json_lines(
            journal_path, field="host permit journal"
        )
        previous = "0" * 64
        for index, event in enumerate(rows):
            expected_keys = {
                "schema_version",
                "transaction_id",
                "sequence",
                "previous_event_sha256",
                "event_type",
                "recorded_at",
                "payload",
                "event_sha256",
            }
            if set(event) != expected_keys:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_ACTIVATION_JOURNAL_INVALID",
                    "host-cutover journal event schema is invalid",
                )
            claimed = _require_sha256(
                event.get("event_sha256"), f"host journal[{index}]"
            )
            body = dict(event)
            body.pop("event_sha256")
            _parse_utc_text(
                event.get("recorded_at"), f"host journal[{index}].recorded_at"
            )
            if not (
                event.get("schema_version")
                == _HOST_CUTOVER_JOURNAL_EVENT_SCHEMA_VERSION
                and event.get("transaction_id") == expected_transaction
                and event.get("sequence") == index + 1
                and event.get("previous_event_sha256") == previous
                and isinstance(event.get("payload"), Mapping)
                and activation_contract.sha256_json(body) == claimed
            ):
                raise CapturedAlpacaPaperServiceError(
                    "HOST_ACTIVATION_JOURNAL_INVALID",
                    "host-cutover journal hash chain is invalid",
                )
            previous = claimed

        sequence = value.get("journal_authorization_sequence")
        if type(sequence) is not int or not (1 <= int(sequence) <= len(rows)):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_JOURNAL_INVALID",
                "host-cutover authorization sequence is outside the journal",
            )
        prior_rows = rows[: int(sequence) - 1]
        quiesced_rows = [
            event
            for event in prior_rows
            if event.get("event_type") == "legacy_execution_lane_quiesced"
        ]
        quiet_rows = [
            event
            for event in prior_rows
            if event.get("event_type")
            == "legacy_paper_broker_quiet_horizon_completed"
        ]
        if len(quiesced_rows) != 1 or len(quiet_rows) != 1:
            raise CapturedAlpacaPaperServiceError(
                "HOST_QUIET_HORIZON_UNVERIFIED",
                "host permit lacks one exact PAPER quiet-horizon event",
            )
        quiesced_payload = quiesced_rows[0].get("payload")
        quiet_payload = quiet_rows[0].get("payload")
        expected_quiet_keys = {
            "policy",
            "assumption_bound",
            "live_cash_certification",
            "required_seconds",
            "observed_monotonic_seconds",
            "stabilized_probe_count",
            "first_zero_at",
            "last_zero_at",
            "legacy_execution_lane_sha256",
            "legacy_process_count",
            "recreator_process_count",
        }
        first_zero_at = (
            _parse_utc_text(
                quiet_payload.get("first_zero_at"),
                "host quiet-horizon first_zero_at",
            )
            if isinstance(quiet_payload, Mapping)
            else None
        )
        last_zero_at = (
            _parse_utc_text(
                quiet_payload.get("last_zero_at"),
                "host quiet-horizon last_zero_at",
            )
            if isinstance(quiet_payload, Mapping)
            else None
        )
        elapsed = (
            quiet_payload.get("observed_monotonic_seconds")
            if isinstance(quiet_payload, Mapping)
            else None
        )
        if not (
            isinstance(quiesced_payload, Mapping)
            and isinstance(quiet_payload, Mapping)
            and set(quiet_payload) == expected_quiet_keys
            and quiet_payload.get("policy")
            == _LEGACY_PAPER_BROKER_QUIET_HORIZON_POLICY
            and quiet_payload.get("assumption_bound") is True
            and quiet_payload.get("live_cash_certification") is False
            and quiet_payload.get("required_seconds")
            == _LEGACY_PAPER_BROKER_QUIET_HORIZON_SECONDS
            and isinstance(elapsed, (int, float))
            and not isinstance(elapsed, bool)
            and math.isfinite(float(elapsed))
            and float(elapsed)
            >= _LEGACY_PAPER_BROKER_QUIET_HORIZON_SECONDS
            and quiet_payload.get("stabilized_probe_count") == 2
            and quiet_payload.get("legacy_process_count") == 0
            and quiet_payload.get("recreator_process_count") == 0
            and first_zero_at is not None
            and last_zero_at is not None
            and first_zero_at <= last_zero_at
            and quiet_payload.get("legacy_execution_lane_sha256")
            == quiesced_payload.get("legacy_execution_lane_sha256")
            and quiet_rows[0].get("sequence")
            > quiesced_rows[0].get("sequence")
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_QUIET_HORIZON_UNVERIFIED",
                "host PAPER quiet-horizon evidence is malformed or incomplete",
            )
        self._quiet_horizon_event_sha256 = _require_sha256(
            quiet_rows[0].get("event_sha256"), "host quiet-horizon event"
        )
        authorization = rows[int(sequence) - 1]
        embedded = value.get("journal_authorization_event")
        expected_payload = {
            "activation_generation": self._verified.activation_generation,
            "manifest_path": str(self._verified.manifest_path),
            "manifest_sha256": self._verified.manifest_sha256,
            "candidate_root": str(self._verified.candidate_root),
            "journal_root": str(journal_path.parent.parent),
            "account_scope": "alpaca:paper",
            "expected_account_id": self._verified.expected_account_id,
            **self._identity,
            "service_cmdline": value.get("service_cmdline"),
            "service_role": "candidate_service",
            "service_script_path": str(Path(__file__).resolve(strict=True)),
            "service_script_sha256": _sha256_file(
                Path(__file__).resolve(strict=True)
            ),
            "challenge_sha256": self._challenge_sha256,
            "prepared_receipt_sha256": self._prepared_sha256,
            "issued_at": value.get("issued_at"),
            "valid_until": value.get("valid_until"),
            "permit_path": str(self.permit_path),
            **self._dispatch_lock_identity,
            "issuer_pid": value.get("issuer_pid"),
            "issuer_create_time_ns": value.get("issuer_create_time_ns"),
            "issuer_executable_path": value.get("issuer_executable_path"),
            "issuer_executable_sha256": value.get("issuer_executable_sha256"),
            "issuer_cmdline": value.get("issuer_cmdline"),
            "issuer_cmdline_sha256": value.get("issuer_cmdline_sha256"),
            "issuer_source_path": value.get("issuer_source_path"),
            "issuer_source_sha256": value.get("issuer_source_sha256"),
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        if not (
            isinstance(embedded, Mapping)
            and dict(embedded) == dict(authorization)
            and authorization.get("event_type") == "activation_permit_issued"
            and authorization.get("event_sha256")
            == value.get("journal_authorization_event_sha256")
            and dict(authorization["payload"]) == expected_payload
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_JOURNAL_INVALID",
                "permit claims do not match the durable journal authorization event",
            )
        authorizations = [
            event for event in rows
            if event.get("event_type") == "activation_permit_issued"
        ]
        publications = [
            event for event in rows
            if event.get("event_type") == "activation_permit_published"
        ]
        publication_valid = True
        if len(publications) == 1:
            publication_payload = publications[0].get("payload")
            publication_valid = isinstance(publication_payload, Mapping) and dict(
                publication_payload
            ) == {
                "permit_path": str(self.permit_path),
                "activation_permit_sha256": value.get("permit_sha256"),
                "journal_authorization_sequence": sequence,
                "journal_authorization_event_sha256": value.get(
                    "journal_authorization_event_sha256"
                ),
                "prepared_receipt_sha256": self._prepared_sha256,
                "challenge_sha256": self._challenge_sha256,
            }
        if len(authorizations) != 1 or any(
            str(event.get("event_type") or "").startswith(
                ("activation_permit_revocation", "rollback_")
            )
            for event in rows[int(sequence) :]
        ) or len(publications) > 1 or not publication_valid:
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_REVOKED",
                "host-cutover journal contains replayed or revoked authority",
            )

    def _validate_permit(self, value: Mapping[str, Any]) -> Mapping[str, Any]:
        if set(value) != self._PERMIT_KEYS:
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_PERMIT_INVALID",
                "host activation permit has an unexpected schema",
            )
        supplied = _require_sha256(
            value.get("permit_sha256"), "host activation permit"
        )
        body = dict(value)
        body.pop("permit_sha256")
        now = _aware_utc(self._wall_clock(), "host permit consume clock")
        issued_at = _parse_utc_text(value.get("issued_at"), "permit issued_at")
        valid_until = _parse_utc_text(
            value.get("valid_until"), "permit valid_until"
        )
        same_identity = all(
            value.get(key) == expected
            for key, expected in {
                **self._common_body(),
                **self._dispatch_lock_identity,
            }.items()
        )
        source_path = _strict_local_file(
            str(value.get("issuer_source_path") or ""),
            "host permit issuer source",
        )
        executable_path = _strict_local_file(
            str(value.get("issuer_executable_path") or ""),
            "host permit issuer executable",
        )
        journal_path = _strict_local_file(
            str(value.get("journal_path") or ""),
            "host permit journal",
        )
        expected_source_path = self._verified.source_paths.get(
            "captured_paper_host_cutover"
        )
        expected_source_sha = self._verified.source_hashes.get(
            "captured_paper_host_cutover"
        )
        if not (
            activation_contract.sha256_json(body) == supplied
            and value.get("schema_version")
            == _HOST_ACTIVATION_PERMIT_SCHEMA_VERSION
            and value.get("state") == "ACTIVATION_PERMITTED"
            and same_identity
            and Path(str(value.get("manifest_path") or "")).resolve(strict=True)
            == self._verified.manifest_path
            and Path(str(value.get("candidate_root") or "")).resolve(strict=True)
            == self._verified.candidate_root
            and Path(str(value.get("journal_root") or "")).resolve(strict=True)
            == journal_path.parent.parent.resolve(strict=True)
            and isinstance(value.get("service_cmdline"), list)
            and bool(value.get("service_cmdline"))
            and all(
                isinstance(item, str) and item
                for item in value.get("service_cmdline", [])
            )
            and activation_contract.sha256_json(value["service_cmdline"])
            == self._identity["service_cmdline_sha256"]
            and value.get("service_role") == "candidate_service"
            and Path(str(value.get("service_script_path") or "")).resolve(
                strict=True
            )
            == Path(__file__).resolve(strict=True)
            and value.get("service_script_sha256")
            == _sha256_file(Path(__file__).resolve(strict=True))
            and value.get("prepared_receipt_sha256")
            == self._prepared_sha256
            and str(journal_path) == value.get("journal_path")
            and bool(str(value.get("journal_transaction_id") or "").strip())
            and type(value.get("journal_authorization_sequence")) is int
            and int(value["journal_authorization_sequence"]) > 0
            and _SHA256_RE.fullmatch(
                str(value.get("journal_authorization_event_sha256") or "")
            )
            and type(value.get("issuer_pid")) is int
            and int(value["issuer_pid"]) > 0
            and type(value.get("issuer_create_time_ns")) is int
            and int(value["issuer_create_time_ns"]) > 0
            and isinstance(value.get("issuer_cmdline"), list)
            and bool(value.get("issuer_cmdline"))
            and _SHA256_RE.fullmatch(
                str(value.get("issuer_cmdline_sha256") or "")
            )
            and source_path == expected_source_path
            and _sha256_file(source_path) == expected_source_sha
            and value.get("issuer_source_sha256") == expected_source_sha
            and _sha256_file(executable_path)
            == value.get("issuer_executable_sha256")
            and value.get("live_cash_authorized") is False
            and value.get("real_money_authorized") is False
            and issued_at <= now < valid_until
            and valid_until <= self._verified.expires_at
            and (now - issued_at).total_seconds()
            <= _HOST_ACTIVATION_MAX_AGE_SECONDS
            and (valid_until - issued_at).total_seconds()
            <= _HOST_ACTIVATION_MAX_AGE_SECONDS
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_PERMIT_INVALID",
                "host activation permit is stale, mismatched, or untrusted",
            )
        self._verify_journal_authorization(value, journal_path=journal_path)
        self._verify_live_issuer_and_command(
            value,
            source_path=source_path,
            executable_path=executable_path,
            journal_path=journal_path,
        )
        # Journal/issuer verification can outlive a short permit window.  A
        # timestamp sampled before those checks is not authority to start a
        # worker afterwards.
        final_now = _aware_utc(
            self._wall_clock(), "host permit final validation clock"
        )
        if not (
            issued_at <= final_now < valid_until
            and valid_until <= self._verified.expires_at
            and (final_now - issued_at).total_seconds()
            <= _HOST_ACTIVATION_MAX_AGE_SECONDS
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_PERMIT_INVALID",
                "host activation permit expired during verification",
            )
        self.assert_not_revoked()
        return dict(value)

    def await_and_consume_permit(self) -> Mapping[str, Any]:
        with self._lock:
            if self._prepared_sha256 is None:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_PREPARED_REQUIRED",
                    "host permit cannot precede PREPARED publication",
                )
            if self._permit_sha256 is not None:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_ACTIVATION_PERMIT_ALREADY_CONSUMED",
                    "host activation permit is one-shot",
                )
            deadline = self._monotonic() + _HOST_ACTIVATION_WAIT_SECONDS
            while True:
                self.assert_not_revoked()
                if not self.permit_path.is_file():
                    if self._monotonic() >= deadline:
                        raise CapturedAlpacaPaperServiceError(
                            "HOST_ACTIVATION_PERMIT_UNAVAILABLE",
                            "host did not issue activation authority before timeout",
                        )
                    self._wait(
                        min(0.05, max(0.0, deadline - self._monotonic()))
                    )
                    continue
                try:
                    permit = self._validate_permit(
                        _stable_canonical_json_mapping(
                            self.permit_path, field="host activation permit"
                        )
                    )
                    break
                except CapturedAlpacaPaperServiceError as exc:
                    # The host publishes the permit between two fsync'd journal
                    # appends.  A concurrent JSONL append can invalidate one
                    # inventory read without invalidating the durable authority;
                    # retry only this proven byte-drift case, never a semantic
                    # mismatch or an uninspectable issuer.
                    if (
                        exc.code != "HOST_ACTIVATION_ARTIFACT_DRIFT"
                        or self._monotonic() >= deadline
                    ):
                        raise
                    self._wait(
                        min(0.01, max(0.0, deadline - self._monotonic()))
                    )
            if self._monotonic() >= deadline:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_ACTIVATION_PERMIT_UNAVAILABLE",
                    "host activation authority expired during verification",
                )
            consume_now = _aware_utc(
                self._wall_clock(), "host permit consume linearization clock"
            )
            issued_at = _parse_utc_text(
                permit.get("issued_at"), "permit issued_at"
            )
            valid_until = _parse_utc_text(
                permit.get("valid_until"), "permit valid_until"
            )
            if not (
                issued_at <= consume_now < valid_until
                and valid_until <= self._verified.expires_at
                and (consume_now - issued_at).total_seconds()
                <= _HOST_ACTIVATION_MAX_AGE_SECONDS
            ):
                raise CapturedAlpacaPaperServiceError(
                    "HOST_ACTIVATION_PERMIT_INVALID",
                    "host activation permit expired before consumption",
                )
            self.assert_not_revoked()
            # The immutable permit remains as evidence.  This process-private
            # transition plus a process-bound STARTED ack is the one-shot
            # consumption record; a restart cannot reuse the PREPARED path.
            self._permit_sha256 = str(permit["permit_sha256"])
            self._permit_body = permit
            self.assert_not_revoked()
            return dict(permit)

    def _assert_consumed_permit_current_unlocked(self) -> None:
        if self._permit_sha256 is None or self._permit_body is None:
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_PERMIT_REQUIRED",
                "PAPER worker authority has not consumed a host permit",
            )
        self.assert_not_revoked()
        current = _stable_canonical_json_mapping(
            self.permit_path, field="host activation permit"
        )
        if (
            dict(current) != dict(self._permit_body)
            or current.get("permit_sha256") != self._permit_sha256
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_PERMIT_DRIFT",
                "consumed host activation permit bytes changed before dispatch",
            )
        validated = self._validate_permit(current)
        if (
            dict(validated) != dict(self._permit_body)
            or validated.get("permit_sha256") != self._permit_sha256
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_ACTIVATION_PERMIT_DRIFT",
                "revalidated host activation permit differs from consumption",
            )
        self.assert_not_revoked()

    def assert_consumed_permit_current(self) -> None:
        """Revalidate the exact one-shot permit immediately before side effects.

        Broker reads performed after permit consumption can outlive the short
        host authorization window.  A revocation-path check alone does not
        prove that the immutable permit, its issuing process, journal chain,
        or expiry are still current.  Re-reading and fully validating the same
        bytes closes that post-read authority gap.
        """

        with self._lock:
            self._assert_consumed_permit_current_unlocked()

    def publish_started(
        self,
        *,
        health: Mapping[str, Any],
        active_start_authority: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        with self._lock:
            if self._permit_sha256 is None or self._permit_body is None:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_ACTIVATION_PERMIT_REQUIRED",
                    "workers cannot acknowledge STARTED without a consumed permit",
                )
            if self._started_sha256 is not None:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_STARTED_ALREADY_PUBLISHED",
                    "host STARTED acknowledgment is one-shot",
                )
            self.assert_not_revoked()
            now = _aware_utc(self._wall_clock(), "host started clock")
            valid_until = min(
                now + timedelta(seconds=_HOST_ACTIVATION_MAX_AGE_SECONDS),
                self._verified.expires_at,
            )
            if valid_until <= now:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_STARTED_EXPIRED",
                    "activation expired before STARTED acknowledgment",
                )
            if not isinstance(health, Mapping) or not (
                health.get("state") == "active"
                and health.get("active_start_authority_consumed") is True
                and health.get("active_start_authority_sha256")
                == active_start_authority.get("authority_sha256")
                and health.get("active_start_evidence_artifact_sha256")
                == hashlib.sha256(
                    _canonical_json_bytes(active_start_authority)
                ).hexdigest()
            ):
                raise CapturedAlpacaPaperServiceError(
                    "HOST_STARTED_HEALTH_INVALID",
                    "STARTED requires health from the exact consumed authority",
                )
            authority = _validate_active_start_authority_receipt(
                active_start_authority,
                verified=self._verified,
            )
            if (
                self._active_start_authority_body is None
                or dict(authority) != dict(self._active_start_authority_body)
            ):
                raise CapturedAlpacaPaperServiceError(
                    "ACTIVE_START_EVIDENCE_MISMATCH",
                    "STARTED does not bind the authority published before workers",
                )
            self._assert_active_start_evidence_current_unlocked()
            body = {
                "schema_version": _HOST_STARTED_SCHEMA_VERSION,
                "state": "STARTED",
                **self._common_body(),
                "prepared_receipt_sha256": self._prepared_sha256,
                "activation_permit_sha256": self._permit_sha256,
                "active_start_authority_sha256": authority["authority_sha256"],
                "active_start_evidence_artifact_sha256": (
                    self.active_start_evidence_artifact_sha256
                ),
                "active_start_authority": authority,
                "started_at": _iso(now),
                "valid_until": _iso(valid_until),
                "workers_started": True,
                "paper_execution_started": True,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
            body["receipt_sha256"] = activation_contract.sha256_json(body)
            _publish_canonical_json_once(self.started_path, body)
            self._started_sha256 = str(body["receipt_sha256"])
            self._started_body = dict(body)
            self.assert_not_revoked()
            return dict(body)

    def _read_apply_completed_authority_unlocked(self) -> Mapping[str, Any]:
        if not (
            self._permit_body is not None
            and self._permit_sha256 is not None
            and self._prepared_sha256 is not None
            and self._started_body is not None
            and self._started_sha256 is not None
            and self._active_start_authority_body is not None
            and self._active_start_evidence_artifact_sha256 is not None
            and self._quiet_horizon_event_sha256 is not None
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_APPLY_COMMIT_PREREQUISITE_MISSING",
                "post-STARTED host authority lacks its exact startup chain",
            )
        self._assert_active_start_evidence_current_unlocked()
        self.assert_not_revoked()
        try:
            journal_path = Path(
                str(self._permit_body.get("journal_path") or "")
            ).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CapturedAlpacaPaperServiceError(
                "HOST_APPLY_COMMIT_UNREADABLE",
                "host apply journal is unavailable",
            ) from exc
        self._verify_journal_authorization(
            self._permit_body,
            journal_path=journal_path,
        )
        rows = _stable_canonical_json_lines(
            journal_path,
            field="host committed apply journal",
        )
        commits = [
            event for event in rows if event.get("event_type") == "apply_completed"
        ]
        if not commits:
            raise CapturedAlpacaPaperServiceError(
                "HOST_APPLY_COMMIT_PENDING",
                "host has not durably committed the validated STARTED state",
            )
        if len(commits) != 1 or commits[0] != rows[-1]:
            raise CapturedAlpacaPaperServiceError(
                "HOST_APPLY_COMMIT_INVALID",
                "host apply commit is duplicated or is not the final journal event",
            )
        event = commits[0]
        payload = event.get("payload")
        expected_payload_keys = {
            "postcondition",
            "activation_generation",
            "manifest_sha256",
            "account_scope",
            "expected_account_id",
            "service_pid",
            "service_create_time_ns",
            "service_executable_path",
            "service_executable_sha256",
            "service_cmdline_sha256",
            "legacy_task_count_disabled",
            "legacy_process_count",
            "prepared_receipt_sha256",
            "activation_permit_sha256",
            "started_receipt_sha256",
            "active_start_authority_sha256",
            "active_start_evidence_artifact_sha256",
            "host_quiet_horizon_event_sha256",
            "challenge_sha256",
            "legacy_execution_lane",
            "legacy_execution_lane_sha256",
            "paper_execution_committed",
            "live_cash_authorized",
            "real_money_authorized",
        }
        lane = (
            payload.get("legacy_execution_lane")
            if isinstance(payload, Mapping)
            else None
        )
        recreators = lane.get("recreator_tasks") if isinstance(lane, Mapping) else None
        recorded_at = _parse_utc_text(
            event.get("recorded_at"), "host apply-completed recorded_at"
        )
        started_at = _parse_utc_text(
            self._started_body.get("started_at"), "host STARTED started_at"
        )
        permit_valid_until = _parse_utc_text(
            self._permit_body.get("valid_until"), "host permit valid_until"
        )
        if not (
            isinstance(payload, Mapping)
            and set(payload) == expected_payload_keys
            and payload.get("postcondition") == "one_unified_candidate_host"
            and payload.get("activation_generation")
            == self._verified.activation_generation
            and payload.get("manifest_sha256") == self._verified.manifest_sha256
            and payload.get("account_scope") == "alpaca:paper"
            and payload.get("expected_account_id")
            == self._verified.expected_account_id
            and payload.get("service_pid") == self._identity.get("service_pid")
            and payload.get("service_create_time_ns")
            == self._identity.get("service_create_time_ns")
            and os.path.normcase(str(payload.get("service_executable_path") or ""))
            == os.path.normcase(
                str(self._identity.get("service_executable_path") or "")
            )
            and payload.get("service_executable_sha256")
            == self._identity.get("service_executable_sha256")
            and payload.get("service_cmdline_sha256")
            == self._identity.get("service_cmdline_sha256")
            and payload.get("legacy_task_count_disabled") == 4
            and payload.get("legacy_process_count") == 0
            and payload.get("prepared_receipt_sha256") == self._prepared_sha256
            and payload.get("activation_permit_sha256") == self._permit_sha256
            and payload.get("started_receipt_sha256") == self._started_sha256
            and payload.get("active_start_authority_sha256")
            == self._active_start_authority_body.get("authority_sha256")
            and payload.get("active_start_evidence_artifact_sha256")
            == self._active_start_evidence_artifact_sha256
            and payload.get("host_quiet_horizon_event_sha256")
            == self._quiet_horizon_event_sha256
            and payload.get("challenge_sha256") == self._challenge_sha256
            and isinstance(lane, Mapping)
            and lane.get("state") == "stopped"
            and isinstance(recreators, list)
            and all(
                isinstance(item, Mapping) and item.get("enabled") is False
                for item in recreators
            )
            and activation_contract.sha256_json(dict(lane))
            == payload.get("legacy_execution_lane_sha256")
            and payload.get("paper_execution_committed") is True
            and payload.get("live_cash_authorized") is False
            and payload.get("real_money_authorized") is False
            and started_at <= recorded_at <= permit_valid_until
            and event.get("sequence")
            > self._permit_body.get("journal_authorization_sequence")
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_APPLY_COMMIT_INVALID",
                "host apply commit does not bind the exact PAPER STARTED state",
            )
        self.assert_not_revoked()
        return dict(event)

    def await_and_consume_apply_completed_authority(self) -> Mapping[str, Any]:
        """Promote the one-shot permit only after the host fsyncs apply_completed."""

        deadline = self._monotonic() + _HOST_ACTIVATION_WAIT_SECONDS
        while True:
            with self._lock:
                try:
                    event = self._read_apply_completed_authority_unlocked()
                except CapturedAlpacaPaperServiceError as exc:
                    if exc.code not in {
                        "HOST_APPLY_COMMIT_PENDING",
                        "HOST_ACTIVATION_ARTIFACT_DRIFT",
                    }:
                        raise
                else:
                    self._apply_completed_event_sha256 = _require_sha256(
                        event.get("event_sha256"), "host apply-completed event"
                    )
                    self._apply_completed_event_body = dict(event)
                    return dict(event)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise CapturedAlpacaPaperServiceError(
                    "HOST_APPLY_COMMIT_TIMEOUT",
                    "host did not durably commit STARTED before authority expired",
                )
            self._wait(min(0.01, remaining))

    def _assert_dispatch_authority_current_unlocked(self) -> None:
        self.assert_not_revoked()
        if self._started_body is None:
            self._assert_consumed_permit_current_unlocked()
            return
        if self._apply_completed_event_sha256 is None:
            try:
                event = self._read_apply_completed_authority_unlocked()
            except CapturedAlpacaPaperServiceError as exc:
                if exc.code != "HOST_APPLY_COMMIT_PENDING":
                    raise
                self._assert_consumed_permit_current_unlocked()
                return
            self._apply_completed_event_sha256 = _require_sha256(
                event.get("event_sha256"), "host apply-completed event"
            )
            self._apply_completed_event_body = dict(event)
        current = self._read_apply_completed_authority_unlocked()
        if (
            self._apply_completed_event_body is None
            or dict(current) != dict(self._apply_completed_event_body)
            or current.get("event_sha256")
            != self._apply_completed_event_sha256
        ):
            raise CapturedAlpacaPaperServiceError(
                "HOST_APPLY_COMMIT_DRIFT",
                "committed PAPER dispatch authority changed after promotion",
            )

    def assert_dispatch_authority_current(self) -> None:
        """Require permit-before-commit or the exact durable host commit."""

        with self._lock:
            self._assert_dispatch_authority_current_unlocked()


def _no_order_smoke_receipt(
    *,
    verified: activation_contract.VerifiedCapturedPaperPreactivation,
    phase_one_reconciliation_receipt: Mapping[str, Any],
    restart_inventory_receipt: Mapping[str, Any],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    started_health: Mapping[str, Any],
    stopped_health: Mapping[str, Any],
    refreshed_readiness: Mapping[str, Mapping[str, Any]],
    captured_at: datetime,
) -> Mapping[str, Any]:
    if not isinstance(verified, activation_contract.VerifiedCapturedPaperPreactivation):
        raise CapturedAlpacaPaperServiceError(
            "PREACTIVATION_REQUIRED", "no-order smoke lacks typed no-order authority"
        )
    if verified.paper_order_submission_authorized is not False:
        raise CapturedAlpacaPaperServiceError(
            "PREACTIVATION_ESCALATED", "no-order envelope unexpectedly authorizes POST"
        )
    workers = started_health.get("managed_workers")
    host = started_health.get("host")
    provider = host.get("provider_loop_supervisor") if isinstance(host, Mapping) else None
    if not (
        started_health.get("state") == "no_order_smoke"
        and started_health.get("runtime_registered") is True
        and started_health.get("live_loop_started") is False
        and isinstance(workers, Mapping)
        and all(
            isinstance(value, Mapping)
            and value.get("ever_started") is False
            and value.get("running") is False
            for value in workers.values()
        )
        and isinstance(provider, Mapping)
        and provider.get("state") == "running"
        and provider.get("all_ready") is True
        and provider.get("provider_sockets_started") is True
        and not provider.get("failures")
        and stopped_health.get("state") == "stopped"
        and stopped_health.get("runtime_registered") is False
        and stopped_health.get("live_loop_started") is False
        and before.get("open_order_count") == 0
        and after.get("open_order_count") == 0
        and before.get("open_order_inventory_sha256")
        == after.get("open_order_inventory_sha256")
        and before.get("connection_generation") == after.get("connection_generation")
        and before.get("order_submission_audit_generation")
        == after.get("order_submission_audit_generation")
        and before.get("order_submission_call_count")
        == after.get("order_submission_call_count")
        and before.get("order_submission_chain_sha256")
        == after.get("order_submission_chain_sha256")
    ):
        raise CapturedAlpacaPaperServiceError(
            "NO_ORDER_SMOKE_UNPROVEN",
            "no-order service topology or broker inventory changed",
        )
    phase_one = _verify_phase_one_reconciliation_receipt(
        phase_one_reconciliation_receipt,
        activation_generation=verified.activation_generation,
    )
    restart_gate = _verify_restart_gate_receipt(
        restart_inventory_receipt,
        verified=verified,
        phase_one_reconciliation_receipt=phase_one,
    )
    captured = _aware_utc(captured_at, "no-order receipt clock")
    restart_observed_at = _parse_utc_text(
        restart_gate.get("observed_at"), "restart gate observed_at"
    )
    if not 0.0 <= (captured - restart_observed_at).total_seconds() <= 60.0:
        raise CapturedAlpacaPaperServiceError(
            "RESTART_GATE_STALE",
            "captured PAPER restart gate is stale or future-dated",
        )
    if not isinstance(refreshed_readiness, Mapping) or set(refreshed_readiness) != {
        "broker_account",
        "kill_switch",
    }:
        raise CapturedAlpacaPaperServiceError(
            "POST_SMOKE_READINESS_MISSING",
            "no-order receipt requires exact post-smoke broker and kill-switch evidence",
        )
    context = _readiness_context(verified)
    refreshed_documents: dict[str, Mapping[str, Any]] = {}
    refreshed_expiries: list[datetime] = []
    for kind in ("broker_account", "kill_switch"):
        value = refreshed_readiness.get(kind)
        if not isinstance(value, Mapping):
            raise CapturedAlpacaPaperServiceError(
                "POST_SMOKE_READINESS_MISSING",
                f"post-smoke {kind} readiness is missing",
            )
        try:
            refreshed_captured, refreshed_expires = (
                readiness_evidence.validate_readiness_receipt_v2(
                    value,
                    kind=kind,
                    context=context,
                    now=captured,
                    max_age_seconds=_POST_SMOKE_RECEIPT_WINDOW_SECONDS,
                )
            )
        except readiness_evidence.CapturedPaperReadinessEvidenceError as exc:
            raise CapturedAlpacaPaperServiceError(
                "POST_SMOKE_READINESS_INVALID",
                f"post-smoke {kind} readiness is invalid",
            ) from exc
        if not (
            restart_observed_at <= refreshed_captured <= captured
            and (captured - refreshed_captured).total_seconds() <= 10.0
        ):
            raise CapturedAlpacaPaperServiceError(
                "POST_SMOKE_READINESS_STALE",
                f"post-smoke {kind} readiness is stale or predates the restart gate",
            )
        refreshed_documents[kind] = dict(value)
        refreshed_expiries.append(refreshed_expires)
    expires = min(
        verified.expires_at,
        captured + timedelta(seconds=_POST_SMOKE_RECEIPT_WINDOW_SECONDS),
        *refreshed_expiries,
    )
    if expires <= captured:
        raise CapturedAlpacaPaperServiceError(
            "NO_ORDER_SMOKE_EXPIRED", "preactivation expired during no-order smoke"
        )
    body: dict[str, Any] = {
        "schema_version": _NO_ORDER_SMOKE_SCHEMA_VERSION,
        "receipt_kind": "no_order_smoke",
        "verdict": "PASS",
        "captured_at": _iso(captured),
        "expires_at": _iso(expires),
        "activation_generation": verified.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "code_build_sha256": verified.code_build_sha256,
        "effective_config_sha256": verified.effective_config_sha256,
        "capture_receipt_sha256": verified.capture_receipt_sha256,
        "preactivation_manifest_sha256": verified.manifest_sha256,
        "phase_one_reconciliation": phase_one,
        "restart_inventory_gate": restart_gate,
        "refreshed_readiness": refreshed_documents,
        "live_cash_authorized": False,
        "orders_submitted": False,
        "order_submission_audit": {
            "audit_generation": before["order_submission_audit_generation"],
            "before_call_count": before["order_submission_call_count"],
            "after_call_count": after["order_submission_call_count"],
            "call_count_delta": 0,
            "before_chain_sha256": before[
                "order_submission_chain_sha256"
            ],
            "after_chain_sha256": after[
                "order_submission_chain_sha256"
            ],
            "before_snapshot_sha256": before[
                "order_submission_audit_sha256"
            ],
            "after_snapshot_sha256": after[
                "order_submission_audit_sha256"
            ],
        },
        "checks": {
            "broker_order_count_unchanged": True,
            "broker_post_calls_zero": True,
            "live_cash_authority_absent": True,
            "paper_account_pinned": True,
            "provider_capture_healthy": True,
            "runtime_registered": True,
            "service_started": True,
            "transport_disabled": True,
        },
    }
    body["receipt_sha256"] = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    return body


def _measure_capture_pressure(
    *,
    preflight: Any,
    replay_runtime_module: ModuleType,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> Any:
    """Take a bounded live-host sample; never lower capture fidelity silently."""

    import psutil

    root = Path(preflight.capture_store_root).resolve(strict=True)
    cpu_percent = float(psutil.cpu_percent(interval=0.1))
    available_memory = int(psutil.virtual_memory().available)
    disk_free = int(shutil.disk_usage(root).free)
    latencies: list[float] = []
    for _index in range(3):
        descriptor = -1
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".chili-pressure-", suffix=".tmp", dir=str(root)
            )
            started = float(monotonic_clock())
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(b"\0" * 4096)
                handle.flush()
                os.fsync(handle.fileno())
            completed = float(monotonic_clock())
            latency = max(0.0, (completed - started) * 1000.0)
            latencies.append(latency)
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            if temporary is not None:
                with suppress(OSError):
                    os.unlink(temporary)
    if len(latencies) != 3:
        raise CapturedAlpacaPaperServiceError(
            "PRESSURE_SAMPLE_UNAVAILABLE", "capture write-latency sample is incomplete"
        )
    sample_type = getattr(replay_runtime_module, "CapturePressureSample", None)
    if sample_type is None:
        raise CapturedAlpacaPaperServiceError(
            "PRESSURE_SAMPLE_TYPE_UNAVAILABLE", "capture pressure type is unavailable"
        )
    return sample_type(
        observed_at=_aware_utc(wall_clock(), "capture pressure clock"),
        resource_binding_sha256=preflight.resource_binding.binding_sha256,
        cpu_percent=cpu_percent,
        available_memory_bytes=available_memory,
        disk_free_bytes=disk_free,
        write_latency_milliseconds=max(latencies),
    )


def _verify_database_schema(
    engine: Any,
    *,
    migrations_module: ModuleType,
) -> Mapping[str, Any]:
    """Read-only exact-code migration fence immediately before service start."""

    migrations = tuple(getattr(migrations_module, "MIGRATIONS", ()))
    if not migrations:
        raise CapturedAlpacaPaperServiceError(
            "MIGRATION_ROSTER_UNAVAILABLE", "application migration roster is empty"
        )
    expected_ids = tuple(str(row[0]) for row in migrations)
    if len(expected_ids) != len(set(expected_ids)):
        raise CapturedAlpacaPaperServiceError(
            "MIGRATION_ROSTER_INVALID", "application migration roster is duplicated"
        )
    migration_354_verifier = getattr(
        migrations_module,
        "_verify_migration_354_physical_contract",
        None,
    )
    if not callable(migration_354_verifier):
        raise CapturedAlpacaPaperServiceError(
            "MIGRATION_354_VERIFIER_UNAVAILABLE",
            "PAPER database lacks the exact migration 354 physical verifier",
        )
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                migrations_module.text("SELECT version_id FROM schema_version")
            ).fetchall()
            table_rows = connection.execute(
                migrations_module.text(
                    "SELECT name, to_regclass(name) IS NOT NULL AS present "
                    "FROM (VALUES "
                    "('captured_paper_post_commit_outbox'),"
                    "('captured_paper_post_commit_outbox_events'),"
                    "('captured_paper_completed_fill_watch'),"
                    "('captured_paper_completed_fill_watch_events'),"
                    "('alpaca_paper_fill_activities'),"
                    "('alpaca_paper_fill_query_observations'),"
                    "('alpaca_paper_post_settlement_fill_contradictions'),"
                    "('captured_paper_selection_frontiers'),"
                    "('captured_paper_selection_frontier_events'),"
                    "('captured_paper_selection_route_states'),"
                    "('captured_paper_variant_application_receipts'),"
                    "('captured_paper_variant_application_events')"
                    ") AS required(name)"
                )
            ).fetchall()
            migration_354_verifier(connection)
    except Exception as exc:
        raise CapturedAlpacaPaperServiceError(
            "DATABASE_SCHEMA_UNREADABLE", "PAPER database schema read failed"
        ) from exc
    applied = {str(row[0]) for row in rows}
    missing = tuple(version for version in expected_ids if version not in applied)
    unexpected = tuple(sorted(applied.difference(expected_ids)))
    absent_tables = tuple(str(row[0]) for row in table_rows if row[1] is not True)
    if missing or unexpected or absent_tables:
        raise CapturedAlpacaPaperServiceError(
            "DATABASE_SCHEMA_NOT_CURRENT",
            "PAPER database differs from the exact code generation schema",
        )
    return {
        "latest_migration": expected_ids[-1],
        "migration_count": len(expected_ids),
        "required_tables_present": True,
        "migration_354_physical_contract_verified": True,
    }


def _build_policy_authority(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    projection: Mapping[str, Any],
    runtime_modules: Mapping[str, ModuleType],
    settings: Any,
) -> _CapturedPaperPolicyAuthority:
    """Build the one hash-bound Replay/PAPER economic + lifecycle policy."""

    adaptive_module = runtime_modules["adaptive_risk_policy"]
    source_module = runtime_modules["captured_adaptive_risk_source"]
    admission_module = runtime_modules["captured_paper_admission"]
    receipt = adaptive_module.build_adaptive_risk_policy_from_settings(settings)
    nested = receipt.to_settings_projection()
    if (
        dict(projection.get("adaptive_risk_policy") or {}) != nested
        or projection.get("settings_projection_sha256")
        != verified.settings_projection_sha256
    ):
        raise CapturedAlpacaPaperServiceError(
            "ADAPTIVE_POLICY_PROJECTION_MISMATCH",
            "shared adaptive risk policy differs from the activation projection",
        )
    # Canonical round-trip removes MappingProxy/other Mapping subclasses while
    # preserving exactly the already-validated JSON value.  Capture identity
    # hashing accepts only canonical JSON containers.
    feature_flags = json.loads(_canonical_json_bytes(projection).decode("utf-8"))
    feature_flags_sha256 = activation_contract.sha256_json(feature_flags)
    policy_spec = source_module.CapturedAdaptiveRiskPolicySpec(
        policy=receipt.policy,
        code_build_sha256=verified.code_build_sha256,
        effective_config_sha256=verified.settings_projection_sha256,
        feature_flags_sha256=feature_flags_sha256,
    )
    operational = admission_module.CapturedPaperOperationalPolicy(
        action_claim_lease_seconds=(
            settings.chili_momentum_captured_paper_action_claim_lease_seconds
        ),
        outbox_max_attempts=(
            settings.chili_momentum_captured_paper_outbox_max_attempts
        ),
        outbox_max_reconciliation_attempts=(
            settings.chili_momentum_captured_paper_outbox_max_reconciliation_attempts
        ),
        reconciliation_retry_delay_seconds=(
            settings.chili_momentum_captured_paper_reconciliation_retry_delay_seconds
        ),
        reconciliation_health_escalation_delay_seconds=(
            settings.chili_momentum_captured_paper_reconciliation_health_escalation_seconds
        ),
        time_in_force=settings.chili_momentum_captured_paper_time_in_force,
        extended_hours=settings.chili_momentum_captured_paper_extended_hours,
        config_provenance_sha256=verified.settings_projection_sha256,
    )
    expected_operational = dict(
        projection.get("captured_paper_operational_policy") or {}
    )
    actual_operational = {
        name: getattr(settings, name)
        for name in expected_operational
    }
    if not expected_operational or actual_operational != expected_operational:
        raise CapturedAlpacaPaperServiceError(
            "OPERATIONAL_POLICY_PROJECTION_MISMATCH",
            "captured PAPER lifecycle policy differs from activation projection",
        )
    return _CapturedPaperPolicyAuthority(
        policy_receipt=receipt,
        policy_spec=policy_spec,
        operational_policy=operational,
        feature_flags=feature_flags,
        feature_flags_sha256=feature_flags_sha256,
    )


def _build_startup_evidence(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    preflight: Any,
    broker_snapshot: Mapping[str, Any],
    policy_authority: _CapturedPaperPolicyAuthority,
    bootstrap_module: ModuleType,
) -> Any:
    code_build = dict(verified.manifest["code_build"])
    claimed_code_sha256 = code_build.pop("code_build_sha256", None)
    if (
        claimed_code_sha256 != verified.code_build_sha256
        or activation_contract.sha256_json(code_build)
        != verified.code_build_sha256
    ):
        raise CapturedAlpacaPaperServiceError(
            "CODE_BUILD_BODY_MISMATCH",
            "captured PAPER code-build body does not match its activation digest",
        )
    account_identity = {
        "broker": "alpaca",
        "environment": "paper",
        "account_id": verified.expected_account_id,
    }
    account_risk_snapshot = {
        "equity": broker_snapshot["account_equity"],
        "last_equity": broker_snapshot["account_last_equity"],
        "buying_power": broker_snapshot["account_buying_power"],
        "cash": broker_snapshot["account_cash"],
        "broker_day_change": broker_snapshot["broker_day_change"],
        "status": broker_snapshot["account_status"],
        "account_blocked": broker_snapshot["account_blocked"],
        "trading_blocked": broker_snapshot["trading_blocked"],
        "transfers_blocked": broker_snapshot["transfers_blocked"],
        "trade_suspended_by_user": broker_snapshot["trade_suspended_by_user"],
        "observed_at": broker_snapshot["account_retrieved_at"],
    }
    account_query = {
        "operation": "get_account+list_positions+list_open_orders",
        "environment": "paper",
        "account_id": verified.expected_account_id,
        "account_retrieved_at": broker_snapshot["account_retrieved_at"],
        "connection_generation": broker_snapshot["connection_generation"],
        "connection_receipt_sha256": broker_snapshot[
            "connection_receipt_sha256"
        ],
        "open_order_census_sha256": broker_snapshot[
            "open_order_census_sha256"
        ],
        "open_order_inventory_sha256": broker_snapshot[
            "open_order_inventory_sha256"
        ],
    }
    return bootstrap_module.CapturedPaperStartupEvidence(
        code_build=code_build,
        feature_flags=policy_authority.feature_flags,
        account_identity=account_identity,
        account_risk_snapshot=account_risk_snapshot,
        account_query=account_query,
        account_provider="alpaca",
        settings_projection_sha256=verified.settings_projection_sha256,
        additional_config={
            "activation_generation": verified.activation_generation,
            "activation_manifest_sha256": verified.manifest_sha256,
            "capture_receipt_sha256": verified.capture_receipt_sha256,
            "paper_connection_receipt_sha256": broker_snapshot[
                "connection_receipt_sha256"
            ],
            "adaptive_policy_settings_projection_sha256": (
                policy_authority.policy_receipt.settings_projection_sha256
            ),
            "adaptive_policy_sha256": (
                policy_authority.policy_receipt.policy.policy_sha256
            ),
        },
        activation_generation=preflight.startup_generation,
        service_instance_id=preflight.startup_process_instance_id,
    )


def _prepare_capture_components(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    projection: Mapping[str, Any],
    runtime_modules: Mapping[str, ModuleType],
    allowed_read_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> _PreparedCapturedPaperCapture:
    """Prepare all read-only/inert components; provider sockets remain closed."""

    preflight_module = runtime_modules["iqfeed_capture_bootstrap_preflight"]
    bootstrap_module = runtime_modules["iqfeed_capture_bootstrap"]
    host_module = runtime_modules["iqfeed_capture_host"]
    replay_runtime_module = runtime_modules["replay_capture_runtime"]
    app_db_module = runtime_modules["app_db"]
    app_config_module = importlib.import_module("app.config")
    settings = app_config_module.settings

    engine = getattr(app_db_module, "engine", None)
    if engine is None:
        raise CapturedAlpacaPaperServiceError(
            "DATABASE_ENGINE_UNAVAILABLE", "PAPER database engine is unavailable"
        )
    _verify_database_schema(
        engine,
        migrations_module=runtime_modules["app_migrations"],
    )
    preflight = preflight_module.load_iqfeed_capture_bootstrap_preflight(
        verified.iqfeed_bootstrap_manifest_path,
        expected_manifest_sha256=(
            verified.iqfeed_bootstrap_manifest_sha256
        ),
        allowed_read_roots=allowed_read_roots,
        # The preflight contract intentionally requires the store to be a
        # strict descendant of an allowed write root.
        allowed_write_roots=(verified.capture_store_root.parent,),
        wall_clock=wall_clock,
    )
    if (
        preflight.manifest_sha256
        != verified.iqfeed_bootstrap_manifest_sha256
        or preflight.capture_store_root.resolve()
        != verified.capture_store_root.resolve()
    ):
        raise CapturedAlpacaPaperServiceError(
            "IQFEED_PREFLIGHT_BINDING_MISMATCH",
            "IQFeed preflight escaped the activation capture binding",
        )
    pressure = _measure_capture_pressure(
        preflight=preflight,
        replay_runtime_module=replay_runtime_module,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )
    host = host_module.prepare_iqfeed_capture_host(
        preflight,
        pressure_sample=pressure,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )
    shared_store: Any | None = None
    try:
        shared_store = replay_runtime_module.SharedCaptureStoreRuntime.create(
            verified.capture_store_root,
            resource_binding=host.composition.binding,
            shared_admission_budget=host.composition.shared_admission_budget,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        adapter = runtime_modules["alpaca_paper_adapter"].AlpacaSpotAdapter()
        broker_snapshot = _paper_broker_snapshot(
            adapter,
            verified=verified,
            purpose="captured_paper_service_composition",
            wall_clock=wall_clock,
        )
        policy_authority = _build_policy_authority(
            verified=verified,
            projection=projection,
            runtime_modules=runtime_modules,
            settings=settings,
        )
        startup_evidence = _build_startup_evidence(
            verified=verified,
            preflight=preflight,
            broker_snapshot=broker_snapshot,
            policy_authority=policy_authority,
            bootstrap_module=bootstrap_module,
        )
        startup_provider = (
            bootstrap_module.CapturedPaperLiveCaptureStartupInputProvider(
                startup_evidence
            )
        )
        host.composition.install_hot_run_factory(
            shared_store_runtime=shared_store,
            startup_input_provider=startup_provider,
            settings_projection_sha256=verified.settings_projection_sha256,
        )
    except BaseException:
        with suppress(BaseException):
            host.close()
        if shared_store is not None:
            with suppress(BaseException):
                shared_store.close()
        raise
    return _PreparedCapturedPaperCapture(
        preflight=preflight,
        host=host,
        shared_store=shared_store,
        adapter=adapter,
        broker_snapshot=broker_snapshot,
        policy_authority=policy_authority,
    )


def _resource_derived_runtime_capacity(prepared: _PreparedCapturedPaperCapture) -> int:
    """Use the measured host budget without silently shrinking strategy scope."""

    try:
        budget = prepared.host.composition.binding.budget
        hot_capacity = budget.derived_hot_symbol_capacity
        max_writer_threads = budget.max_writer_threads
    except (AttributeError, TypeError) as exc:
        raise CapturedAlpacaPaperServiceError(
            "RESOURCE_CAPACITY_UNAVAILABLE",
            "captured PAPER hot-symbol capacity is unavailable",
        ) from exc
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (hot_capacity, max_writer_threads)
    ):
        raise CapturedAlpacaPaperServiceError(
            "RESOURCE_CAPACITY_UNAVAILABLE",
            "captured PAPER measured resource capacity is malformed",
        )
    # One process-lifetime writer is reserved for the durable selection queue.
    # The remaining concurrency comes from measured host headroom; this is
    # resource accounting, not a one-symbol or PAPER-only strategy throttle.
    available_hot_writer_threads = max_writer_threads - 1
    value = min(hot_capacity, available_hot_writer_threads)
    if value <= 0:
        raise CapturedAlpacaPaperServiceError(
            "RESOURCE_CAPACITY_UNSUPPORTED",
            "measured writer capacity cannot host both selection capture and a hot-symbol lane",
        )
    if value > 10_000:
        # The transport recovery worker's exact public contract supports at
        # most 10,000 items per recovery pass.  Refuse an incompatible host
        # budget instead of clamping it into an unreported strategy limit.
        raise CapturedAlpacaPaperServiceError(
            "RESOURCE_CAPACITY_UNSUPPORTED",
            "measured hot-symbol capacity exceeds the exact runtime contract",
        )
    return value


def _assemble_service_composition(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    prepared: _PreparedCapturedPaperCapture,
    phase_one_reconciliation_receipt: Mapping[str, Any],
    restart_inventory_receipt: Mapping[str, Any],
    production_material_factory: Any,
    runtime_modules: Mapping[str, ModuleType],
    settings: Any,
    database_engine: Any,
    assert_external_authority_current: Callable[[], None],
    acquire_external_dispatch_authority: Callable[[], ContextManager[None]],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> _CapturedPaperServiceComposition:
    """Assemble inert PAPER components from one verified adapter generation."""

    capacity = _resource_derived_runtime_capacity(prepared)
    policy = prepared.policy_authority.policy_receipt.policy
    handoff_ttl_seconds = float(policy.context_data_max_age_seconds)
    if (
        not math.isfinite(handoff_ttl_seconds)
        or handoff_ttl_seconds <= 0.0
        or handoff_ttl_seconds > 86_400.0
    ):
        raise CapturedAlpacaPaperServiceError(
            "HANDOFF_TTL_INVALID",
            "adaptive policy context age cannot back the PAPER handoff",
        )

    host_module = runtime_modules["iqfeed_capture_host"]
    post_commit_worker_module = runtime_modules["captured_paper_post_commit_worker"]
    transport_module = runtime_modules["captured_paper_transport"]
    transport_worker_module = runtime_modules["captured_paper_transport_worker"]
    acceptance_module = runtime_modules["captured_paper_positive_acceptance"]
    fill_capture_module = runtime_modules["captured_paper_fill_capture"]
    fill_watch_module = runtime_modules["captured_paper_fill_watch"]
    financial_breaker_module = runtime_modules["captured_paper_financial_breaker"]
    initial_candidate_reader_module = runtime_modules[
        "captured_paper_initial_candidate_reader"
    ]
    selection_runtime_module = runtime_modules["captured_paper_selection_runtime"]
    selection_source_module = runtime_modules["captured_paper_selection_source"]
    selection_queue_module = runtime_modules["captured_paper_selection_queue"]
    selection_producer_module = runtime_modules[
        "captured_paper_selection_producer"
    ]
    variant_binding_module = runtime_modules["captured_paper_variant_binding"]
    viability_module = runtime_modules["momentum_viability"]
    replay_contract_module = runtime_modules["replay_capture_contract"]
    replay_runtime_module = runtime_modules["replay_capture_runtime"]
    app_db_module = runtime_modules["app_db"]
    yf_session_module = runtime_modules["yf_session"]
    initial_controller_module = runtime_modules[
        "captured_paper_initial_controller"
    ]
    supervisor_module = runtime_modules["captured_paper_service_supervisor"]
    service_fence_module = runtime_modules["captured_paper_service_fence"]
    live_loop_module = runtime_modules["live_runner_loop"]
    live_runner_module = runtime_modules["live_runner"]
    # One issuer instance backs both post-admission materialization and the
    # final transport fence.  Constructing a second issuer would split the
    # observation clock/session provenance across two order-affecting paths.
    financial_breaker_issuer = (
        financial_breaker_module.SqlAlchemyCapturedPaperFinancialBreakerIssuer(
            database_engine,
            observation_clock=wall_clock,
        )
    )
    # One exact process-lifetime singleton object is shared by the supervisor
    # and every pre-FSM owner activation.  Constructing a second fence would
    # make a healthy supervisor receipt incapable of proving that the runtime
    # callback still owns the same PostgreSQL session advisory lock.
    service_fence = service_fence_module.CapturedPaperServiceFence(
        database_engine
    )
    deferred_initial_candidate_reader = (
        selection_runtime_module.DeferredCapturedPaperInitialCandidateReader()
    )
    initial_controller = (
        initial_controller_module.CapturedPaperInitialAdmissionController(
            host=prepared.host,
            bind=database_engine,
            candidate_reader=deferred_initial_candidate_reader,
            user_id=settings.chili_autotrader_user_id,
            expected_account_id=verified.expected_account_id,
            runtime_generation=verified.activation_generation,
            code_build_sha256=verified.code_build_sha256,
            capture_receipt_sha256=verified.capture_receipt_sha256,
            expected_bridge_version=(
                settings.chili_iqfeed_l1_authoritative_bridge_build
            ),
            adaptive_policy_settings_receipt=(
                prepared.policy_authority.policy_receipt
            ),
            adaptive_policy_spec=prepared.policy_authority.policy_spec,
            controller_policy=(
                initial_controller_module.CapturedPaperInitialControllerPolicy(
                    max_attempts=(
                        settings.chili_momentum_captured_paper_trigger_max_attempts
                    ),
                    retry_delay_seconds=(
                        settings.chili_momentum_captured_paper_trigger_retry_delay_seconds
                    ),
                    future_tolerance_seconds=(
                        settings.chili_momentum_captured_paper_trigger_future_tolerance_seconds
                    ),
                    exact_print_window_seconds=(
                        settings.chili_momentum_captured_paper_trigger_exact_print_window_seconds
                    ),
                )
            ),
            assert_service_fence_held=service_fence.assert_held,
            wall_clock=wall_clock,
            wait=time.sleep,
        )
    )

    session_factory = getattr(app_db_module, "SessionLocal", None)
    if not callable(session_factory):
        raise CapturedAlpacaPaperServiceError(
            "SELECTION_SESSION_FACTORY_UNAVAILABLE",
            "captured PAPER selection DB session factory is unavailable",
        )
    code_build_payload = dict(verified.manifest["code_build"])
    claimed_code_sha256 = code_build_payload.pop("code_build_sha256", None)
    if not (
        claimed_code_sha256 == verified.code_build_sha256
        and activation_contract.sha256_json(code_build_payload)
        == verified.code_build_sha256
    ):
        raise CapturedAlpacaPaperServiceError(
            "SELECTION_CODE_BUILD_BODY_MISMATCH",
            "selection source code body differs from activation authority",
        )
    adaptive_projection = (
        prepared.policy_authority.policy_receipt.to_settings_projection()
    )
    adaptive_policy_snapshot = dict(
        adaptive_projection.get("policy_snapshot") or {}
    )
    if (
        not adaptive_policy_snapshot
        or activation_contract.sha256_json(adaptive_policy_snapshot)
        != policy.policy_sha256
    ):
        raise CapturedAlpacaPaperServiceError(
            "SELECTION_POLICY_BODY_MISMATCH",
            "selection source policy body differs from adaptive policy authority",
        )
    viability_settings_projection = (
        viability_module.ViabilitySettingsProjection.from_runtime(settings)
    )
    try:
        run_configuration = dict(prepared.preflight.run_configuration)
        resource_budget = prepared.host.composition.binding.budget
        writer_batch_events = int(run_configuration["writer_batch_events"])
        writer_batch_bytes = int(run_configuration["writer_batch_bytes"])
        writer_poll_seconds = float(run_configuration["writer_poll_seconds"])
        writer_flush_seconds = float(
            run_configuration["writer_flush_interval_seconds"]
        )
        reader_batch_events = int(resource_budget.max_queue_events)
        reader_batch_bytes = int(resource_budget.async_queue_bytes)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CapturedAlpacaPaperServiceError(
            "SELECTION_RESOURCE_CONFIGURATION_UNAVAILABLE",
            "selection queue lacks exact measured runtime configuration",
        ) from exc
    if not (
        0 < writer_batch_events <= reader_batch_events
        and 0 < writer_batch_bytes <= reader_batch_bytes
        and math.isfinite(writer_poll_seconds)
        and writer_poll_seconds > 0.0
        and math.isfinite(writer_flush_seconds)
        and writer_flush_seconds > 0.0
    ):
        raise CapturedAlpacaPaperServiceError(
            "SELECTION_RESOURCE_CONFIGURATION_INVALID",
            "selection queue runtime configuration exceeds measured resources",
        )

    def selection_setup_from_application(application: Any) -> Any:
        binding = application.plan.authority
        if not (
            binding.expected_account_id == verified.expected_account_id
            and binding.activation_generation == verified.activation_generation
            and binding.policy_sha256 == policy.policy_sha256
            and binding.settings_projection_sha256
            == verified.settings_projection_sha256
            and binding.code_build_sha256 == verified.code_build_sha256
            and binding.account_scope == "alpaca:paper"
            and binding.execution_family == "alpaca_spot"
        ):
            raise CapturedAlpacaPaperServiceError(
                "SELECTION_APPLICATION_AUTHORITY_DRIFT",
                "durable strategy application differs from activation authority",
            )
        selection_authority = (
            selection_producer_module.CapturedPaperSelectionAuthority(
                expected_account_id=verified.expected_account_id,
                activation_generation=verified.activation_generation,
                policy_sha256=policy.policy_sha256,
                settings_projection_sha256=verified.settings_projection_sha256,
                code_build_sha256=verified.code_build_sha256,
                variant_bindings=tuple(
                    selection_producer_module.CapturedPaperSelectionVariantBinding(
                        variant_id=item.target_variant_id,
                        family=item.family,
                        version=item.version,
                        variant_key=item.target_variant_key,
                        target_after_sha256=item.target_after_sha256,
                    )
                    for item in application.items
                ),
            )
        )
        return selection_runtime_module.CapturedPaperSelectionApplicationSetup(
            application=application,
            authority=selection_authority,
        )

    def readback_selection_application(db: Any) -> Any | None:
        receipt = (
            variant_binding_module.load_captured_paper_variant_application_receipt_by_generation(
                db,
                expected_account_id=verified.expected_account_id,
                activation_generation=verified.activation_generation,
                activation_manifest_sha256=verified.manifest_sha256,
                lock=False,
            )
        )
        if receipt is None:
            return None
        if receipt.status != "applied":
            raise CapturedAlpacaPaperServiceError(
                "SELECTION_APPLICATION_GENERATION_TERMINAL",
                "activation generation already has a terminal strategy application",
            )
        setup = selection_setup_from_application(receipt.application)
        variant_binding_module.assert_committed_captured_paper_variant_application(
            db,
            application=setup.application,
            activation_manifest_sha256=verified.manifest_sha256,
        )
        return setup

    def build_selection_application_setup() -> Any:
        """Apply clones and atomically persist their exact crash receipt."""

        service_fence.assert_held()
        db = session_factory()
        attempted_setup: Any | None = None
        bound_at = _aware_utc(wall_clock(), "selection variant binding clock")
        binding_authority = (
            variant_binding_module.CapturedPaperVariantBindingAuthority(
                expected_account_id=verified.expected_account_id,
                activation_generation=verified.activation_generation,
                policy_sha256=policy.policy_sha256,
                settings_projection_sha256=verified.settings_projection_sha256,
                code_build_sha256=verified.code_build_sha256,
                bound_at=bound_at,
            )
        )
        try:
            # Resolve this exact generation before manufacturing or recovering
            # any target row.  This is the commit-ack/restart readback path.
            existing = readback_selection_application(db)
            if existing is not None:
                service_fence.assert_held()
                return existing
            variant_binding_module.recover_stale_captured_paper_variant_bindings(
                db,
                authority=binding_authority,
                recovered_at=bound_at,
            )
            source_variant_ids = (
                variant_binding_module.resolve_intended_canonical_source_variant_ids(
                    db
                )
            )
            plan = variant_binding_module.plan_captured_paper_variant_bindings(
                db,
                authority=binding_authority,
                source_variant_ids=source_variant_ids,
            )
            application = (
                variant_binding_module.apply_captured_paper_variant_bindings(
                    db,
                    plan=plan,
                )
            )
            attempted_setup = selection_setup_from_application(application)
            variant_binding_module.record_captured_paper_variant_application_receipt(
                db,
                application=application,
                activation_manifest_sha256=verified.manifest_sha256,
            )
            db.commit()
            service_fence.assert_held()
            return attempted_setup
        except BaseException as original:
            with suppress(BaseException):
                db.rollback()
            # ``commit`` has returned (success or exception) and the original
            # session is rolled back/terminated.  Use a fresh primary session;
            # absence is authoritative only when both receipt and generation
            # clone census are empty in that new transaction.
            reconciliation_db = session_factory()
            try:
                service_fence.assert_held()
                recovered = readback_selection_application(reconciliation_db)
                if recovered is not None:
                    service_fence.assert_held()
                    return recovered
                proof = (
                    variant_binding_module.prove_captured_paper_variant_application_not_applied(
                        reconciliation_db,
                        authority=binding_authority,
                        activation_manifest_sha256=verified.manifest_sha256,
                        checked_at=_aware_utc(
                            wall_clock(), "selection application readback clock"
                        ),
                    )
                )
            except BaseException as reconciliation_error:
                if attempted_setup is not None:
                    raise selection_runtime_module.CapturedPaperSelectionApplicationOutcomeAmbiguous(
                        attempted_setup
                    ) from reconciliation_error
                raise original from reconciliation_error
            finally:
                with suppress(BaseException):
                    reconciliation_db.rollback()
                reconciliation_db.close()
            raise selection_runtime_module.CapturedPaperSelectionApplicationNotApplied(
                proof
            ) from original
        finally:
            db.close()

    def build_selection_runtime_components(
        setup: Any,
        accounting: Any,
        startup_cleanup: Any,
    ) -> Any:
        """Lazily acquire the one measured writer only after clone commit."""

        service_fence.assert_held()
        if not (
            accounting.permanent_selection_writer_slots == 1
            and accounting.remaining_capture_writer_slots
            == accounting.max_writer_threads - 1
            and accounting.resource_binding_sha256
            == prepared.shared_store.resource_binding.binding_sha256
        ):
            raise CapturedAlpacaPaperServiceError(
                "SELECTION_WRITER_ACCOUNTING_MISMATCH",
                "selection writer accounting differs from measured host resources",
            )
        source = selection_source_module.SqlAlchemyCapturedViabilitySnapshotSource(
            database_engine,
            variant_application=setup.application,
            selection_authority=setup.authority,
            settings_projection=viability_settings_projection,
            expected_account_id=verified.expected_account_id,
            activation_generation=verified.activation_generation,
            policy_sha256=policy.policy_sha256,
            service_settings_projection_sha256=(
                verified.settings_projection_sha256
            ),
            candidate_code_build_sha256=verified.code_build_sha256,
            adaptive_policy_snapshot=adaptive_policy_snapshot,
            code_build_payload=code_build_payload,
            fundamentals_reader=yf_session_module.get_fundamentals_receipt,
            context_max_age_seconds=handoff_ttl_seconds,
            tenbeat_entry_tilt_weight=(
                settings.chili_tenbeat_entry_tilt_weight
            ),
            wall_clock=wall_clock,
        )
        account_identity_sha256 = activation_contract.sha256_json(
            {
                "schema_version": (
                    "chili.captured-paper-selection-account.v1"
                ),
                "account_scope": "alpaca:paper",
                "expected_account_id": verified.expected_account_id,
                "broker": "alpaca",
                "broker_environment": "paper",
            }
        )
        queue_identity = replay_contract_module.CaptureRunIdentity(
            run_id=verified.activation_generation,
            generation=1,
            code_build_sha256=verified.code_build_sha256,
            config_sha256=verified.settings_projection_sha256,
            feature_flags_sha256=policy.policy_sha256,
            account_identity_sha256=account_identity_sha256,
            broker="alpaca",
            broker_environment="paper",
        )
        lease = prepared.shared_store.acquire(queue_identity)
        startup_cleanup.register("selection_lease", lease.release)
        admission = prepared.shared_store.shared_admission_budget
        ingress = replay_runtime_module.BoundedCaptureIngress.from_resource_binding(
            prepared.shared_store.resource_binding,
            pressure_controller=admission.pressure_controller,
            shared_admission_budget=admission,
            monotonic_clock=monotonic_clock,
        )
        publisher = (
            selection_queue_module.CapturedPaperSelectionQueuePublisher(
                writer_lease=lease,
                ingress=ingress,
                selection_authority=setup.authority,
                wall_clock=wall_clock,
                monotonic_clock=monotonic_clock,
            )
        )
        writer = selection_queue_module.CapturedPaperSelectionQueueWriter(
            publisher=publisher,
            batch_events=writer_batch_events,
            batch_bytes=writer_batch_bytes,
            poll_seconds=writer_poll_seconds,
            flush_interval_seconds=writer_flush_seconds,
        )

        def close_partial_writer() -> None:
            if writer.close(timeout_seconds=handoff_ttl_seconds) is not True:
                raise CapturedAlpacaPaperServiceError(
                    "SELECTION_PARTIAL_WRITER_CLOSE_UNCONFIRMED",
                    "partial selection writer did not quiesce",
                )

        startup_cleanup.register("selection_writer", close_partial_writer)
        input_port = (
            selection_queue_module.CapturedPaperSelectionQueueInputPort(
                root=prepared.shared_store.store.root,
                queue_identity=queue_identity,
                selection_authority=setup.authority,
                durable_gate=publisher.durable_gate,
                max_batch_events=reader_batch_events,
                max_batch_bytes=reader_batch_bytes,
                max_read_seconds=handoff_ttl_seconds,
                wall_clock=wall_clock,
                monotonic_clock=monotonic_clock,
            )
        )
        producer = selection_producer_module.CapturedPaperSelectionProducer(
            session_factory=session_factory,
            authority=setup.authority,
            input_port=input_port,
            wall_clock=wall_clock,
        )
        exact_reader = (
            initial_candidate_reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
                database_engine,
                authority=setup.authority,
            )
        )
        service_fence.assert_held()
        return selection_runtime_module.CapturedPaperSelectionRuntimeComponents(
            source=source,
            publisher=publisher,
            writer=writer,
            input_port=input_port,
            producer=producer,
            initial_reader=exact_reader,
            close_source=lambda: None,
        )

    def rollback_selection_application(application: Any) -> Mapping[str, Any]:
        service_fence.assert_held()
        last_error: BaseException | None = None
        for attempt in range(2):
            db = session_factory()
            try:
                receipt = (
                    variant_binding_module.rollback_captured_paper_variant_bindings(
                        db,
                        application=application,
                        rolled_back_at=_aware_utc(
                            wall_clock(), "selection variant rollback clock"
                        ),
                    )
                )
                db.commit()
                service_fence.assert_held()
                return receipt
            except BaseException as exc:
                last_error = exc
                with suppress(BaseException):
                    db.rollback()
            finally:
                db.close()

            # A fresh primary read is authoritative only when the append-only
            # event and the complete target census agree.  A still-APPLIED,
            # exact-active census is the sole safe same-application retry case.
            readback_db = session_factory()
            try:
                service_fence.assert_held()
                durable = (
                    variant_binding_module.load_captured_paper_variant_application_receipt_by_generation(
                        readback_db,
                        expected_account_id=verified.expected_account_id,
                        activation_generation=verified.activation_generation,
                        activation_manifest_sha256=verified.manifest_sha256,
                        lock=False,
                    )
                )
                if durable is not None and durable.status == "rolled_back":
                    proven = variant_binding_module.assert_rolled_back_captured_paper_variant_application(
                        readback_db, application=application
                    )
                    assert proven.rollback is not None
                    service_fence.assert_held()
                    return proven.rollback
                if durable is not None and durable.status == "applied":
                    variant_binding_module.assert_committed_captured_paper_variant_application(
                        readback_db,
                        application=application,
                        activation_manifest_sha256=verified.manifest_sha256,
                    )
                    if attempt == 0:
                        continue
            except BaseException as readback_error:
                raise last_error from readback_error
            finally:
                with suppress(BaseException):
                    readback_db.rollback()
                readback_db.close()
            assert last_error is not None
            raise last_error
        assert last_error is not None
        raise last_error

    selection_worker = (
        selection_runtime_module.CapturedPaperSelectionLifecycleWorker(
            shared_capture_runtime=prepared.shared_store,
            deferred_reader=deferred_initial_candidate_reader,
            assert_service_fence_held=service_fence.assert_held,
            application_setup_factory=build_selection_application_setup,
            component_factory=build_selection_runtime_components,
            rollback_application=rollback_selection_application,
            poll_interval_seconds=(
                settings.chili_momentum_captured_paper_worker_idle_poll_seconds
            ),
            durable_timeout_seconds=handoff_ttl_seconds,
            producer_timeout_seconds=handoff_ttl_seconds,
            monotonic_clock=monotonic_clock,
        )
    )
    # Bound-method objects are recreated on attribute access.  Retain one exact
    # process-private capability so a supervisor health/restart call cannot be
    # mistaken for a foreign admission owner by the live-loop singleton.
    initial_symbol_admitter = initial_controller.admit

    runtime_owner = host_module.IqfeedCapturedPaperRuntimeOwner(
        host=prepared.host,
        adapter_factory=lambda: prepared.adapter,
        admission_bind=database_engine,
        expected_account_id=verified.expected_account_id,
        code_build_sha256=verified.code_build_sha256,
        config_sha256=verified.settings_projection_sha256,
        capture_receipt_sha256=verified.capture_receipt_sha256,
        runtime_generation=verified.activation_generation,
        first_dip_policy_mode="candidate",
        decision_max_entries=capacity,
        decision_ttl_seconds=handoff_ttl_seconds,
        admission_max_entries=capacity,
        admission_ttl_seconds=handoff_ttl_seconds,
        settings_projection_sha256=verified.settings_projection_sha256,
        config_sha256_resolver=prepared.host.captured_paper_config_sha256_for,
        production_material_factory=production_material_factory,
        financial_breaker_issuer=financial_breaker_issuer,
        financial_breaker_clock=wall_clock,
        assert_service_fence_held=service_fence.assert_held,
        allow_manual_staging=False,
        monotonic_clock=monotonic_clock,
    )
    post_commit_worker = (
        post_commit_worker_module.CapturedPaperPostCommitWorker(
            owner=runtime_owner,
            max_items_per_cycle=capacity,
            idle_poll_seconds=(
                settings.chili_momentum_captured_paper_worker_idle_poll_seconds
            ),
            observation_clock=wall_clock,
        )
    )

    transport_store = transport_module.SqlAlchemyCapturedPaperTransportStore(
        database_engine
    )
    broker_transport = transport_module.ExactAlpacaPaperEntryTransport(
        adapter=prepared.adapter,
        expected_account_id=verified.expected_account_id,
        broker_connection_generation=prepared.broker_snapshot[
            "connection_generation"
        ],
        observation_clock=wall_clock,
        acquire_external_dispatch_authority=(
            acquire_external_dispatch_authority
        ),
    )
    acceptance_recorder = (
        acceptance_module.SqlAlchemyCapturedPaperPositiveAcceptanceRecorder(
            database_engine
        )
    )
    fill_capture = fill_capture_module.SqlAlchemyCapturedPaperFillCapture(
        bind=database_engine,
        adapter=prepared.adapter,
        max_pending_reads=capacity,
    )

    def require_exit_owner_inventory_resolved() -> Mapping[str, Any]:
        receipt = dict(
            fill_capture.recover_exit_owner_inventory_bounded(
                expected_account_id=verified.expected_account_id,
                runtime_generation=verified.activation_generation,
                broker_connection_generation=prepared.broker_snapshot[
                    "connection_generation"
                ],
                execution_family="alpaca_spot",
                limit=capacity,
            )
        )
        supplied_sha256 = receipt.pop("receipt_sha256", None)
        if not (
            supplied_sha256 == activation_contract.sha256_json(receipt)
            and receipt.get("exit_owner_inventory_resolved") is True
            and receipt.get("exit_owner_recovery_bounded") is True
            and receipt.get("exit_owner_recovery_exhausted") is False
            and receipt.get("paper_order_submission_authorized") is False
            and receipt.get("live_cash_authorized") is False
            and receipt.get("real_money_authorized") is False
        ):
            raise CapturedAlpacaPaperServiceError(
                "EXIT_OWNER_INVENTORY_UNRESOLVED",
                "captured PAPER exit-owner inventory did not reach a bounded fixed point",
            )
        return {**receipt, "receipt_sha256": supplied_sha256}

    require_exit_owner_inventory_resolved()
    # Bound methods are recreated on attribute access.  Freeze the exact
    # completion capability once so loop start and health prove ownership by
    # object identity across the same adapter/store generation.
    exit_completion_handler = fill_capture.complete_exit_post_commit
    exit_transport_handler = (
        live_runner_module
        .build_captured_paper_exit_transport_post_commit_handler(
            adapter=prepared.adapter,
            expected_account_id=verified.expected_account_id,
            runtime_generation=verified.activation_generation,
            broker_connection_generation=prepared.broker_snapshot[
                "connection_generation"
            ],
            assert_service_fence_held=service_fence.assert_held,
        )
    )
    exit_owner_worker = fill_capture_module.CapturedPaperExitOwnerWorker(
        fill_capture=fill_capture,
        expected_account_id=verified.expected_account_id,
        runtime_generation=verified.activation_generation,
        broker_connection_generation=prepared.broker_snapshot[
            "connection_generation"
        ],
        execution_family="alpaca_spot",
        max_items_per_cycle=capacity,
        idle_poll_seconds=(
            settings.chili_momentum_captured_paper_worker_idle_poll_seconds
        ),
        observation_clock=wall_clock,
    )
    transport_coordinator = transport_module.CapturedPaperTransportCoordinator(
        store=transport_store,
        broker_transport=broker_transport,
        acceptance_recorder=acceptance_recorder,
        fill_capture=fill_capture,
        financial_breaker_issuer=financial_breaker_issuer,
        assert_external_authority_current=(
            assert_external_authority_current
        ),
    )
    worker_id = str(prepared.preflight.startup_process_instance_id)
    transport_worker = transport_worker_module.CapturedPaperTransportWorker(
        coordinator=transport_coordinator,
        worker_id=worker_id,
        lease_seconds=(
            prepared.policy_authority.operational_policy.action_claim_lease_seconds
        ),
        recovery_limit=capacity,
        idle_poll_seconds=(
            settings.chili_momentum_captured_paper_worker_idle_poll_seconds
        ),
        observation_clock=wall_clock,
    )

    fill_watch_store = fill_watch_module.SqlAlchemyCapturedPaperCompletedFillWatchStore(
        database_engine
    )
    fill_watch_reader = fill_watch_module.ExactAlpacaPaperCompletedFillWatchReader(
        adapter=prepared.adapter,
        expected_account_id=verified.expected_account_id,
        broker_connection_generation=prepared.broker_snapshot[
            "connection_generation"
        ],
        observation_clock=wall_clock,
    )
    fill_watch_coordinator = (
        fill_watch_module.CapturedPaperCompletedFillWatchCoordinator(
            store=fill_watch_store,
            reader=fill_watch_reader,
            fill_capture=fill_capture,
            retry_delay_seconds=(
                prepared.policy_authority.operational_policy.reconciliation_retry_delay_seconds
            ),
        )
    )
    fill_watch_worker = fill_watch_module.CapturedPaperCompletedFillWatchWorker(
        coordinator=fill_watch_coordinator,
        worker_id=worker_id,
        lease_seconds=(
            prepared.policy_authority.operational_policy.action_claim_lease_seconds
        ),
        idle_poll_seconds=(
            settings.chili_momentum_captured_paper_worker_idle_poll_seconds
        ),
        observation_clock=wall_clock,
    )

    managed_workers = (
        supervisor_module.CapturedPaperManagedWorker(
            name="post_commit", worker=post_commit_worker
        ),
        supervisor_module.CapturedPaperManagedWorker(
            name="transport", worker=transport_worker
        ),
        supervisor_module.CapturedPaperManagedWorker(
            name="later_fill", worker=fill_watch_worker
        ),
        supervisor_module.CapturedPaperManagedWorker(
            name="exit_owner", worker=exit_owner_worker
        ),
    )
    selection_pre_authority_workers = (
        supervisor_module.CapturedPaperManagedWorker(
            name="selection", worker=selection_worker
        ),
    )

    def rollback_selection_after_quiesce() -> Mapping[str, Any]:
        runtime_receipt = dict(selection_worker.rollback_after_quiesce())
        runtime_sha256 = runtime_receipt.get("runtime_rollback_sha256")
        runtime_body = {
            key: value
            for key, value in runtime_receipt.items()
            if key != "runtime_rollback_sha256"
        }
        application_outcome = runtime_receipt.get("application_outcome")
        target_variant_ids = runtime_receipt.get("target_variant_ids")
        variants_deactivated = runtime_receipt.get(
            "strategy_variants_deactivated"
        )
        if not (
            runtime_receipt.get("schema_version")
            == "chili.captured-paper-selection-runtime-rollback.v2"
            and runtime_receipt.get("account_scope") == "alpaca:paper"
            and runtime_receipt.get("expected_account_id")
            == verified.expected_account_id
            and runtime_receipt.get("activation_generation")
            == verified.activation_generation
            and isinstance(runtime_sha256, str)
            and activation_contract.sha256_json(runtime_body) == runtime_sha256
            and application_outcome in {"rolled_back", "not_applied"}
            and isinstance(target_variant_ids, list)
            and (
                (
                    application_outcome == "rolled_back"
                    and variants_deactivated is True
                    and bool(target_variant_ids)
                )
                or (
                    application_outcome == "not_applied"
                    and variants_deactivated is False
                    and target_variant_ids == []
                )
            )
            and runtime_receipt.get("paper_order_submission_authorized") is False
            and runtime_receipt.get("live_cash_authorized") is False
            and runtime_receipt.get("real_money_authorized") is False
        ):
            raise CapturedAlpacaPaperServiceError(
                "SELECTION_RUNTIME_ROLLBACK_RECEIPT_INVALID",
                "post-quiesce selection receipt is not bound to this PAPER generation",
            )
        body = {
            "schema_version": "chili.captured-paper-post-quiesce.v3",
            "verdict": (
                "CAPTURED_PAPER_SELECTION_BINDINGS_ROLLED_BACK"
                if application_outcome == "rolled_back"
                else "CAPTURED_PAPER_SELECTION_APPLICATION_NOT_APPLIED"
            ),
            "account_scope": "alpaca:paper",
            "expected_account_id": verified.expected_account_id,
            "runtime_generation": verified.activation_generation,
            "workers_stopped": True,
            "runtime_unregistered": True,
            "provider_stopped": True,
            "application_outcome": application_outcome,
            "strategy_variants_deactivated": runtime_receipt.get(
                "strategy_variants_deactivated"
            ),
            "variant_application_sha256": runtime_receipt.get(
                "variant_application_sha256"
            ),
            "variant_rollback_sha256": runtime_receipt.get(
                "variant_rollback_sha256"
            ),
            "target_variant_ids": runtime_receipt.get("target_variant_ids"),
            "selection_runtime_rollback_sha256": runtime_receipt.get(
                "runtime_rollback_sha256"
            ),
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        return {
            **body,
            "receipt_sha256": activation_contract.sha256_json(body),
        }

    def start_dedicated_live_loop() -> bool:
        return live_loop_module.start_captured_paper_live_runner_loop(
            expected_account_id=verified.expected_account_id,
            runtime_generation=verified.activation_generation,
            broker_connection_generation=prepared.broker_snapshot[
                "connection_generation"
            ],
            execution_family="alpaca_spot",
            captured_paper_symbol_admitter=initial_symbol_admitter,
            captured_paper_exit_completion_handler=(
                exit_completion_handler
            ),
            captured_paper_exit_transport_handler=(
                exit_transport_handler
            ),
        )

    def dedicated_live_loop_healthy() -> bool:
        return live_loop_module.is_captured_paper_live_runner_loop_admission_ready(
            expected_account_id=verified.expected_account_id,
            runtime_generation=verified.activation_generation,
            broker_connection_generation=prepared.broker_snapshot[
                "connection_generation"
            ],
            execution_family="alpaca_spot",
            captured_paper_exit_completion_handler=(
                exit_completion_handler
            ),
            captured_paper_exit_transport_handler=(
                exit_transport_handler
            ),
        )

    def fenced_prestart_revalidate() -> Mapping[str, Any]:
        require_exit_owner_inventory_resolved()
        fenced = dict(_build_fenced_prestart_revalidation_receipt(
            verified=verified,
            prepared=prepared,
            database_engine=database_engine,
            phase_one_reconciliation_receipt=(
                phase_one_reconciliation_receipt
            ),
            baseline_restart_inventory_receipt=restart_inventory_receipt,
            restart_inventory_module=runtime_modules[
                "captured_paper_restart_inventory"
            ],
            service_fence_module=service_fence_module,
            recover_initial_generations=lambda: _recover_fenced_initial_generations(
                verified=verified,
                prepared=prepared,
                database_engine=database_engine,
                initial_recovery_module=runtime_modules[
                    "captured_paper_initial_recovery"
                ],
                assert_service_fence_held=service_fence.assert_held,
            ),
            wall_clock=wall_clock,
        ))
        fenced_sha256 = fenced.get("receipt_sha256")
        fenced_body = dict(fenced)
        fenced_body.pop("receipt_sha256", None)
        if fenced_sha256 != activation_contract.sha256_json(fenced_body):
            raise CapturedAlpacaPaperServiceError(
                "FENCED_PRESTART_RECEIPT_INVALID",
                "captured PAPER fenced prestart receipt hash changed",
            )
        # The recovery call above is the in-fence gate.  Preserve the exact
        # fenced-prestart schema consumed by CapturedPaperServiceSupervisor;
        # adding locally invented fields would make the real supervisor reject
        # startup even though a recording test double accepted them.
        return fenced

    supervisor = supervisor_module.CapturedPaperServiceSupervisor(
        host=prepared.host,
        runtime=runtime_owner.runtime,
        service_fence=service_fence,
        fenced_prestart_revalidate=fenced_prestart_revalidate,
        managed_workers=managed_workers,
        active_pre_authority_workers=selection_pre_authority_workers,
        live_loop_start=start_dedicated_live_loop,
        live_loop_stop=live_loop_module.stop_live_runner_loop,
        live_loop_health=dedicated_live_loop_healthy,
        post_quiesce_before_fence_release=rollback_selection_after_quiesce,
        monotonic_clock=monotonic_clock,
    )
    return _CapturedPaperServiceComposition(
        supervisor=supervisor,
        shared_capture_store=prepared.shared_store,
        adapter=prepared.adapter,
        connection_generation_receipt=dict(
            prepared.broker_snapshot["connection_receipt"]
        ),
        phase_one_reconciliation_receipt=dict(
            phase_one_reconciliation_receipt
        ),
        restart_inventory_receipt=dict(restart_inventory_receipt),
        database_engine=database_engine,
        initial_broker_snapshot=dict(prepared.broker_snapshot),
    )


def _build_service_composition(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    projection: Mapping[str, Any],
    runtime_modules: Mapping[str, ModuleType],
    allowed_read_roots: Sequence[str | Path],
    assert_external_authority_current: Callable[[], None],
    acquire_external_dispatch_authority: Callable[[], ContextManager[None]],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> _CapturedPaperServiceComposition:
    """Build the one real replay-parity PAPER composition, still inert."""

    prepared = _prepare_capture_components(
        verified=verified,
        projection=projection,
        runtime_modules=runtime_modules,
        allowed_read_roots=allowed_read_roots,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )
    if not callable(assert_external_authority_current):
        raise CapturedAlpacaPaperServiceError(
            "HOST_TRANSPORT_AUTHORITY_GUARD_INVALID",
            "captured PAPER transport requires a current host authority guard",
        )
    if not callable(acquire_external_dispatch_authority):
        raise CapturedAlpacaPaperServiceError(
            "HOST_TRANSPORT_DISPATCH_AUTHORITY_INVALID",
            "captured PAPER transport requires a host dispatch lock authority",
        )
    try:
        app_config_module = importlib.import_module("app.config")
        _verify_loaded_module_role(
            verified,
            role="app_config",
            module=app_config_module,
        )
        settings = app_config_module.settings
        provider_module = runtime_modules["captured_paper_production_provider"]
        builder = getattr(
            provider_module,
            "build_live_fsm_captured_paper_service_material_factory",
            None,
        )
        if not callable(builder):
            raise CapturedAlpacaPaperServiceError(
                "PRODUCTION_MATERIAL_FACTORY_UNAVAILABLE",
                "callback-free captured PAPER material factory is unavailable",
            )
        production_factory = builder(
            host=prepared.host,
            settings=settings,
            settings_projection_sha256=verified.settings_projection_sha256,
            raw_adapter_factory=(
                lambda *_args, **_kwargs: prepared.adapter
            ),
            policy_spec=prepared.policy_authority.policy_spec,
            operational_policy=(
                prepared.policy_authority.operational_policy
            ),
            wall_clock=wall_clock,
            quote_max_age_seconds=(
                settings.chili_momentum_adaptive_risk_market_data_max_age_seconds
            ),
            account_max_age_seconds=(
                settings.chili_momentum_adaptive_risk_account_data_max_age_seconds
            ),
        )
        database_engine = getattr(runtime_modules["app_db"], "engine", None)
        if database_engine is None:
            raise CapturedAlpacaPaperServiceError(
                "DATABASE_ENGINE_UNAVAILABLE",
                "PAPER database engine disappeared during composition",
            )
        phase_one_module = runtime_modules["captured_paper_phase_one_handoff"]
        try:
            phase_one_receipt = (
                phase_one_module.reconcile_captured_paper_phase_one_after_restart(
                    database_engine,
                    activation_generation=verified.activation_generation,
                    limit=_MAX_STARTUP_RECONCILIATION_ROWS,
                )
            )
        except BaseException as exc:
            raise CapturedAlpacaPaperServiceError(
                "PHASE_ONE_RECONCILIATION_UNAVAILABLE",
                "phase-one restart reconciliation did not complete",
            ) from exc
        phase_one_receipt = _verify_phase_one_reconciliation_receipt(
            phase_one_receipt,
            activation_generation=verified.activation_generation,
        )
        restart_inventory_receipt = (
            _build_bracketed_restart_inventory_receipt(
                verified=verified,
                prepared=prepared,
                database_engine=database_engine,
                phase_one_reconciliation_receipt=phase_one_receipt,
                restart_inventory_module=runtime_modules[
                    "captured_paper_restart_inventory"
                ],
                wall_clock=wall_clock,
            )
        )
        owner_recovery_fields = {
            key: restart_inventory_receipt.get(key)
            for key in (
                "exit_owner_inventory_resolved",
                "exit_owner_recovery_bounded",
                "exit_owner_recovery_exhausted",
            )
            if key in restart_inventory_receipt
        }
        if owner_recovery_fields and owner_recovery_fields != {
            "exit_owner_inventory_resolved": True,
            "exit_owner_recovery_bounded": True,
            "exit_owner_recovery_exhausted": False,
        }:
            raise CapturedAlpacaPaperServiceError(
                "EXIT_OWNER_INVENTORY_UNRESOLVED",
                "captured PAPER exit-owner inventory remains unresolved",
            )
        if not (
            restart_inventory_receipt.get("disposition")
            == "strict_flat_first_cutover"
            and restart_inventory_receipt.get("recovery_required") is False
            and restart_inventory_receipt.get("new_admissions_quarantined")
            is False
            and restart_inventory_receipt.get("exposure_decreasing_only")
            is False
            and restart_inventory_receipt.get("broker_inventory_flat") is True
        ):
            # The restart classifier preserves owned exposure instead of
            # pretending the account is flat.  This service does not yet have
            # a separately supervised exposure-decreasing-only owner, so it
            # must stop before constructing any entry-capable worker.
            raise CapturedAlpacaPaperServiceError(
                "OWNED_RESTART_RECOVERY_REQUIRED",
                "captured PAPER durable or broker inventory requires quarantined recovery",
            )
        return _assemble_service_composition(
            verified=verified,
            prepared=prepared,
            phase_one_reconciliation_receipt=phase_one_receipt,
            restart_inventory_receipt=restart_inventory_receipt,
            production_material_factory=production_factory,
            runtime_modules=runtime_modules,
            settings=settings,
            database_engine=database_engine,
            assert_external_authority_current=(
                assert_external_authority_current
            ),
            acquire_external_dispatch_authority=(
                acquire_external_dispatch_authority
            ),
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
    except BaseException:
        with suppress(BaseException):
            prepared.host.close()
        with suppress(BaseException):
            prepared.shared_store.close()
        raise


def _close_composition(
    composition: _CapturedPaperServiceComposition,
    *,
    supervisor_started: bool,
) -> Mapping[str, Any]:
    stopped: Mapping[str, Any] = {
        "state": "prepared",
        "runtime_registered": False,
        "live_loop_started": False,
    }
    # A prepared supervisor already owns host queues and capture resources even
    # when the final broker fence rejects before ``start_*``.  Close it in every
    # path; its close operation is intentionally valid from PREPARED and is
    # idempotent after an internal startup rollback.
    try:
        stopped = composition.supervisor.close(
            join_timeout_seconds=_SERVICE_SHUTDOWN_SECONDS,
            quiesce_timeout_seconds=_SERVICE_SHUTDOWN_SECONDS,
        )
    except BaseException as exc:
        # The store remains live when any worker/host may still reference it.
        # Closing underneath an unjoined thread would turn a controlled
        # shutdown failure into capture corruption.
        raise CapturedAlpacaPaperServiceError(
            "SERVICE_SHUTDOWN_INCOMPLETE",
            "captured PAPER supervisor did not quiesce cleanly",
        ) from exc
    if stopped.get("state") != "stopped":
        raise CapturedAlpacaPaperServiceError(
            "SERVICE_SHUTDOWN_INCOMPLETE",
            "captured PAPER supervisor stop state is unconfirmed",
        )
    try:
        composition.close_shared_capture_store()
    except BaseException as exc:
        raise CapturedAlpacaPaperServiceError(
            "SERVICE_SHUTDOWN_INCOMPLETE",
            "captured PAPER capture store did not close cleanly",
        ) from exc
    return stopped


def _deny_active_transport_authority() -> None:
    """Fail closed if order transport is reached outside active PAPER."""

    raise CapturedAlpacaPaperServiceError(
        "HOST_TRANSPORT_AUTHORITY_UNAVAILABLE",
        "active host authority is unavailable for PAPER order transport",
    )


def _deny_active_transport_dispatch() -> ContextManager[None]:
    """Deny an irreversible call when no active host handshake exists."""

    raise CapturedAlpacaPaperServiceError(
        "HOST_TRANSPORT_DISPATCH_AUTHORITY_UNAVAILABLE",
        "active host dispatch authority is unavailable for PAPER transport",
    )


def _execute_no_order_smoke(
    *,
    verified: activation_contract.VerifiedCapturedPaperPreactivation,
    composition: _CapturedPaperServiceComposition,
    receipt_output: str | Path,
    allowed_output_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, Any]:
    output = _strict_new_local_json_path(
        receipt_output,
        allowed_roots=allowed_output_roots,
    )
    supervisor_started = False
    started_health: Mapping[str, Any] | None = None
    stopped_health: Mapping[str, Any] | None = None
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    try:
        before = _paper_broker_snapshot(
            composition.adapter,
            verified=verified,
            purpose="no_order_smoke_before",
            wall_clock=wall_clock,
        )
        _assert_composition_broker_generation(composition, before)
        supervisor_started = True
        started_health = composition.supervisor.start_no_order_smoke()
        # Re-read the supervisor instead of trusting only the start return.
        started_health = composition.supervisor.health()
        after = _paper_broker_snapshot(
            composition.adapter,
            verified=verified,
            purpose="no_order_smoke_after",
            wall_clock=wall_clock,
        )
        _assert_composition_broker_generation(composition, after)
    finally:
        stopped_health = _close_composition(
            composition,
            supervisor_started=supervisor_started,
        )
    if before is None or after is None or started_health is None or stopped_health is None:
        raise CapturedAlpacaPaperServiceError(
            "NO_ORDER_SMOKE_INCOMPLETE", "no-order topology did not fully start and stop"
        )
    refreshed_broker = _paper_broker_snapshot(
        composition.adapter,
        verified=verified,
        purpose="no_order_smoke_post_shutdown_refresh",
        wall_clock=wall_clock,
    )
    _assert_composition_broker_generation(composition, refreshed_broker)
    if not (
        refreshed_broker.get("order_submission_audit_generation")
        == after.get("order_submission_audit_generation")
        and refreshed_broker.get("order_submission_call_count")
        == after.get("order_submission_call_count")
        and refreshed_broker.get("order_submission_chain_sha256")
        == after.get("order_submission_chain_sha256")
    ):
        raise CapturedAlpacaPaperServiceError(
            "NO_ORDER_SMOKE_UNPROVEN",
            "post-shutdown broker refresh observed an order-submission audit change",
        )
    refreshed_kill_switch = _paper_kill_switch_snapshot(
        composition.database_engine,
        verified=verified,
        wall_clock=wall_clock,
    )
    completion_clock = _aware_utc(wall_clock(), "no-order completion clock")
    refreshed_readiness = _issue_post_smoke_refreshed_readiness(
        verified=verified,
        broker_snapshot=refreshed_broker,
        kill_switch_snapshot=refreshed_kill_switch,
        issued_at=completion_clock,
    )
    receipt = _no_order_smoke_receipt(
        verified=verified,
        phase_one_reconciliation_receipt=(
            composition.phase_one_reconciliation_receipt
        ),
        restart_inventory_receipt=composition.restart_inventory_receipt,
        before=before,
        after=after,
        started_health=started_health,
        stopped_health=stopped_health,
        refreshed_readiness=refreshed_readiness,
        captured_at=completion_clock,
    )
    artifact_sha256 = _publish_canonical_json_once(output, receipt)
    return {
        "schema_version": SERVICE_REPORT_SCHEMA_VERSION,
        "verdict": "CAPTURED_ALPACA_PAPER_NO_ORDER_SMOKE_PASSED",
        "generated_at": _iso(_aware_utc(wall_clock(), "service report clock")),
        "activation_generation": verified.activation_generation,
        "preactivation_manifest_sha256": verified.manifest_sha256,
        "no_order_smoke_path": str(output),
        "no_order_smoke_sha256": artifact_sha256,
        "no_order_smoke_receipt_sha256": receipt["receipt_sha256"],
        "broker_contacted_read_only": True,
        "provider_sockets_started_then_stopped": True,
        "database_connected": True,
        "orders_submitted": False,
        "paper_execution_started": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }


def _execute_active_service(
    *,
    verified: activation_contract.VerifiedCapturedPaperActivation,
    composition: _CapturedPaperServiceComposition,
    allowed_read_roots: Sequence[str | Path],
    launcher_attestation: _CapturedPaperLauncherCutoverAttestation,
    host_activation_handshake: _CapturedPaperHostActivationHandshake,
    stop_event: threading.Event | None = None,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> Mapping[str, Any]:
    if verified.paper_order_submission_authorized is not True:
        raise CapturedAlpacaPaperServiceError(
            "FINAL_ACTIVATION_REQUIRED",
            "active PAPER service cannot consume preactivation authority",
        )
    if type(launcher_attestation) is not _CapturedPaperLauncherCutoverAttestation:
        raise CapturedAlpacaPaperServiceError(
            "LAUNCH_ATTESTATION_REQUIRED",
            "active PAPER service lacks process-bound launcher authority",
        )
    if type(host_activation_handshake) is not _CapturedPaperHostActivationHandshake:
        raise CapturedAlpacaPaperServiceError(
            "HOST_ACTIVATION_HANDSHAKE_REQUIRED",
            "active PAPER service lacks two-phase host authority",
        )
    # Composition can include bounded DB/broker reads and restart inventory.
    # Re-read all manifest/source/receipt/kill-switch bytes immediately after it
    # finishes instead of inheriting the earlier validation clock.
    _reload_final_activation_authority(
        verified,
        allowed_read_roots=allowed_read_roots,
        wall_clock=wall_clock,
    )
    shutdown = stop_event or threading.Event()
    installed_handlers: dict[int, Any] = {}

    def request_shutdown(_signum: int, _frame: Any) -> None:
        shutdown.set()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            installed_handlers[int(signum)] = signal.getsignal(signum)
            signal.signal(signum, request_shutdown)

    supervisor_started = False
    start_health: Mapping[str, Any] | None = None
    consumed_active_authority: Mapping[str, Any] | None = None
    try:
        final_broker_snapshot = _paper_broker_snapshot(
            composition.adapter,
            verified=verified,
            purpose="active_service_final_fence",
            wall_clock=wall_clock,
        )
        _assert_composition_broker_generation(
            composition, final_broker_snapshot
        )
        supervisor_module = importlib.import_module(
            "app.services.trading.momentum_neural.captured_paper_service_supervisor"
        )

        def consume_final_start_authority() -> Mapping[str, Any]:
            nonlocal consumed_active_authority
            refreshed = _reload_final_activation_authority(
                verified,
                allowed_read_roots=allowed_read_roots,
                wall_clock=wall_clock,
            )
            consumed = launcher_attestation.consume(wall_clock=wall_clock)
            host_activation_handshake.publish_prepared()
            permit = host_activation_handshake.await_and_consume_permit()
            broker_fixed_point = _paper_broker_quiet_fixed_point(
                composition.adapter,
                verified=refreshed,
                composition=composition,
                baseline_snapshot=final_broker_snapshot,
                wall_clock=wall_clock,
                monotonic_clock=monotonic_clock,
                wait=wait,
            )
            # The broker reads above can consume most or all of the short host
            # permit window.  Re-read every sealed activation byte and query
            # the durable kill switch after those reads, then validate the
            # exact consumed permit last.
            refreshed = _reload_final_activation_authority(
                refreshed,
                allowed_read_roots=allowed_read_roots,
                wall_clock=wall_clock,
            )
            final_kill_switch = _paper_kill_switch_snapshot(
                composition.database_engine,
                verified=refreshed,
                wall_clock=wall_clock,
            )
            host_activation_handshake.assert_consumed_permit_current()
            authority_body = {
                "schema_version": _ACTIVE_START_AUTHORITY_SCHEMA_VERSION,
                "verdict": "CAPTURED_ALPACA_PAPER_ACTIVE_START_AUTHORIZED",
                "account_scope": "alpaca:paper",
                "expected_account_id": refreshed.expected_account_id,
                "runtime_generation": refreshed.activation_generation,
                "activation_manifest_sha256": refreshed.manifest_sha256,
                "kill_switch_receipt_sha256": refreshed.receipt_hashes[
                    "kill_switch"
                ],
                "launcher_attestation_sha256": consumed[
                    "attestation_sha256"
                ],
                "launcher_attestation_consumed": consumed[
                    "launcher_attestation_consumed"
                ],
                "host_activation_permit_sha256": permit["permit_sha256"],
                "host_activation_permit_consumed": True,
                "host_quiet_horizon_event_sha256": (
                    host_activation_handshake.quiet_horizon_event_sha256
                ),
                "broker_fixed_point": dict(broker_fixed_point),
                "broker_fixed_point_sha256": (
                    activation_contract.sha256_json(broker_fixed_point)
                ),
                "post_permit_broker_snapshot_sha256": (
                    activation_contract.sha256_json(
                        broker_fixed_point["second_snapshot"]
                    )
                ),
                "order_transition_fence_sha256": (
                    activation_contract.sha256_json(
                        broker_fixed_point["second_order_census"]
                    )
                ),
                "fill_activity_fence_sha256": (
                    activation_contract.sha256_json(
                        broker_fixed_point["second_fill_activity_census"]
                    )
                ),
                "final_kill_switch_query_sha256": (
                    activation_contract.sha256_json(final_kill_switch)
                ),
                "final_kill_switch_query": dict(final_kill_switch),
                "paper_order_submission_authorized": True,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
            authority_body["authority_sha256"] = (
                activation_contract.sha256_json(authority_body)
            )
            consumed_active_authority = _validate_active_start_authority_receipt(
                authority_body,
                verified=refreshed,
            )
            host_activation_handshake.publish_active_start_evidence(
                consumed_active_authority
            )
            return dict(consumed_active_authority)

        def assert_final_start_current() -> None:
            refreshed = _reload_final_activation_authority(
                verified,
                allowed_read_roots=allowed_read_roots,
                wall_clock=wall_clock,
            )
            _paper_kill_switch_snapshot(
                composition.database_engine,
                verified=refreshed,
                wall_clock=wall_clock,
            )
            host_activation_handshake.assert_consumed_permit_current()
            host_activation_handshake.assert_active_start_evidence_current()

        start_authority = supervisor_module.CapturedPaperActiveStartAuthority(
            expected_account_id=verified.expected_account_id,
            runtime_generation=verified.activation_generation,
            consume=consume_final_start_authority,
            assert_current=assert_final_start_current,
        )
        supervisor_started = True
        start_health = composition.supervisor.start_active(
            start_authority=start_authority
        )
        assert_final_start_current()
        start_health = composition.supervisor.assert_healthy()
        if consumed_active_authority is None:
            raise CapturedAlpacaPaperServiceError(
                "ACTIVE_START_AUTHORITY_MISSING",
                "supervisor did not consume the durable PAPER start evidence",
            )
        host_activation_handshake.publish_started(
            health=start_health,
            active_start_authority=consumed_active_authority,
        )
        apply_commit = (
            host_activation_handshake.await_and_consume_apply_completed_authority()
        )
        started_report = {
            "schema_version": SERVICE_REPORT_SCHEMA_VERSION,
            "verdict": "CAPTURED_ALPACA_PAPER_STARTED",
            "generated_at": _iso(_aware_utc(wall_clock(), "service start clock")),
            "activation_generation": verified.activation_generation,
            "manifest_sha256": verified.manifest_sha256,
            "account_scope": "alpaca:paper",
            "expected_account_id": verified.expected_account_id,
            "provider_sockets_started": True,
            "database_connected": True,
            "broker_contacted": True,
            # Recovery workers start before this report and may legitimately
            # reconcile or submit a previously durable initial outbox row.
            # Until the exact adapter call census is wired, do not claim zero.
            "orders_submitted_at_startup": None,
            "paper_execution_started": True,
            "host_apply_completed_event_sha256": apply_commit["event_sha256"],
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        sys.stdout.buffer.write(_canonical_json_bytes(started_report) + b"\n")
        sys.stdout.buffer.flush()
        while not shutdown.wait(_SERVICE_HEALTH_POLL_SECONDS):
            # The short permit is a one-shot startup authority.  Once STARTED
            # is durably acknowledged, continuing operation is governed by the
            # revocation tombstone/dispatch lock plus per-decision runtime
            # authority, not by extending the expired startup permit.
            host_activation_handshake.assert_dispatch_authority_current()
            composition.supervisor.assert_healthy()
    finally:
        stopped_health = _close_composition(
            composition,
            supervisor_started=supervisor_started,
        )
        for signum, prior in installed_handlers.items():
            signal.signal(signum, prior)
    if start_health is None:
        raise CapturedAlpacaPaperServiceError(
            "ACTIVE_START_UNCONFIRMED", "captured PAPER supervisor did not start"
        )
    return {
        "schema_version": SERVICE_REPORT_SCHEMA_VERSION,
        "verdict": "CAPTURED_ALPACA_PAPER_STOPPED_CLEANLY",
        "generated_at": _iso(_aware_utc(wall_clock(), "service stop clock")),
        "activation_generation": verified.activation_generation,
        "manifest_sha256": verified.manifest_sha256,
        "paper_execution_started": True,
        "paper_execution_stopped": stopped_health.get("state") == "stopped",
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }


def validate_offline_startup(
    *,
    manifest_path: str | Path,
    manifest_sha256: str,
    candidate_root: str | Path,
    allowed_read_roots: Sequence[str | Path],
    launcher_path: str | Path,
    launcher_sha256: str,
    envelope_stage: str = "activation",
) -> tuple[
    activation_contract.VerifiedCapturedPaperActivation,
    runtime_env.CapturedPaperRuntimeEnvironmentReceipt,
    Mapping[str, Any],
]:
    if envelope_stage == "preactivation":
        verified = activation_contract.load_captured_paper_preactivation(
            manifest_path,
            expected_manifest_sha256=manifest_sha256,
            candidate_root=candidate_root,
            allowed_read_roots=allowed_read_roots,
        )
    elif envelope_stage == "activation":
        verified = activation_contract.load_captured_paper_activation(
            manifest_path,
            expected_manifest_sha256=manifest_sha256,
            candidate_root=candidate_root,
            allowed_read_roots=allowed_read_roots,
        )
    else:
        raise CapturedAlpacaPaperServiceError(
            "ENVELOPE_STAGE_INVALID",
            "captured PAPER service envelope stage is unsupported",
        )
    _verify_loaded_sources(verified)
    _verify_launcher(
        verified,
        launcher_path=launcher_path,
        launcher_sha256=launcher_sha256,
    )
    receipt, projection = _install_and_validate_settings(verified)
    return verified, receipt, projection


def _offline_report(
    verified: activation_contract.VerifiedCapturedPaperActivation,
    receipt: runtime_env.CapturedPaperRuntimeEnvironmentReceipt,
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SERVICE_REPORT_SCHEMA_VERSION,
        "verdict": "CAPTURED_ALPACA_PAPER_OFFLINE_STARTUP_VALID",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "activation_generation": verified.activation_generation,
        "envelope_stage": verified.envelope_stage,
        "paper_order_submission_authorized": (
            verified.paper_order_submission_authorized
        ),
        "manifest_sha256": verified.manifest_sha256,
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "code_build_sha256": verified.code_build_sha256,
        "effective_config_sha256": verified.effective_config_sha256,
        "settings_projection_sha256": verified.settings_projection_sha256,
        "capture_receipt_sha256": verified.capture_receipt_sha256,
        "runtime_environment_sha256": receipt.configuration_sha256,
        "settings_projection_sha256": projection["settings_projection_sha256"],
        "paper_credentials_present": True,
        "live_cash_credentials_present": False,
        "provider_sockets_started": False,
        "database_connected": False,
        "broker_contacted": False,
        "orders_submitted": False,
        "paper_execution_started": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=_MODES, required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--allow-read-root", action="append", required=True)
    parser.add_argument("--launcher-path", required=True)
    parser.add_argument("--launcher-sha256", required=True)
    parser.add_argument("--no-order-receipt-output")
    parser.add_argument("--host-ready-receipt")
    return parser


def _validate_mode_arguments(args: argparse.Namespace) -> None:
    receipt_output = str(
        getattr(args, "no_order_receipt_output", None) or ""
    ).strip()
    host_ready = str(getattr(args, "host_ready_receipt", None) or "").strip()
    if args.mode == "no-order-smoke" and not receipt_output:
        raise CapturedAlpacaPaperServiceError(
            "NO_ORDER_RECEIPT_OUTPUT_REQUIRED",
            "no-order smoke requires one append-only receipt output",
        )
    if args.mode != "no-order-smoke" and receipt_output:
        raise CapturedAlpacaPaperServiceError(
            "NO_ORDER_RECEIPT_OUTPUT_FORBIDDEN",
            "receipt output is accepted only for no-order smoke",
        )
    if args.mode == "activate-paper" and not host_ready:
        raise CapturedAlpacaPaperServiceError(
            "HOST_READY_RECEIPT_REQUIRED",
            "active PAPER requires a sealed two-phase host receipt path",
        )
    if args.mode != "activate-paper" and host_ready:
        raise CapturedAlpacaPaperServiceError(
            "HOST_READY_RECEIPT_FORBIDDEN",
            "host activation receipt is accepted only for active PAPER",
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    # 2026-07-17: two consecutive live PREPARED-phase stalls were invisible
    # (only the dispatch lock appeared, then silence until the cutover's
    # timeout killed the run).  Periodic all-thread stack dumps to stderr —
    # persisted by the launcher's ActivatePaper console redirect — name the
    # exact blocking frame without attaching a debugger to the sealed
    # process.
    import faulthandler

    faulthandler.dump_traceback_later(150.0, repeat=True, file=sys.stderr)
    external_runtime_boundary_entered = False
    provider_start_may_have_been_attempted = False
    active_order_boundary_entered = False
    service_singleton: _CapturedPaperServiceSingleton | None = None
    try:
        _validate_mode_arguments(args)
        verified, receipt, projection = validate_offline_startup(
            manifest_path=args.manifest,
            manifest_sha256=args.manifest_sha256,
            candidate_root=args.candidate_root,
            allowed_read_roots=args.allow_read_root,
            launcher_path=args.launcher_path,
            launcher_sha256=args.launcher_sha256,
            envelope_stage=(
                "preactivation" if args.mode == "no-order-smoke" else "activation"
            ),
        )
        if args.mode == "validate-only":
            report = _offline_report(verified, receipt, projection)
        else:
            service_singleton = _CapturedPaperServiceSingleton()
            service_singleton.acquire()
            host_activation_handshake = None
            if args.mode == "activate-paper":
                _assert_content_addressed_activation_entrypoints(verified)
                host_activation_handshake = (
                    _CapturedPaperHostActivationHandshake.prepare(
                        ready_output=args.host_ready_receipt,
                        verified=verified,
                        allowed_roots=args.allow_read_root,
                    )
                )
            launcher_attestation = (
                _issue_launcher_cutover_attestation(
                    verified=verified,
                    args=args,
                )
                if args.mode == "activate-paper"
                else None
            )
            # From this point construction may perform read-only DB/broker
            # preflights.  Preserve uncertainty in the rejection report if an
            # exception interrupts one of those reads.
            external_runtime_boundary_entered = True
            runtime_modules = _load_pinned_runtime_modules(verified)
            composition = _build_service_composition(
                verified=verified,
                projection=projection,
                runtime_modules=runtime_modules,
                allowed_read_roots=args.allow_read_root,
                assert_external_authority_current=(
                    host_activation_handshake.assert_dispatch_authority_current
                    if host_activation_handshake is not None
                    else _deny_active_transport_authority
                ),
                acquire_external_dispatch_authority=(
                    host_activation_handshake.hold_dispatch_authority
                    if host_activation_handshake is not None
                    else _deny_active_transport_dispatch
                ),
            )
            if args.mode == "no-order-smoke":
                provider_start_may_have_been_attempted = True
                report = _execute_no_order_smoke(
                    verified=verified,
                    composition=composition,
                    receipt_output=args.no_order_receipt_output,
                    allowed_output_roots=args.allow_read_root,
                )
            else:
                provider_start_may_have_been_attempted = True
                active_order_boundary_entered = True
                report = _execute_active_service(
                    verified=verified,
                    composition=composition,
                    allowed_read_roots=args.allow_read_root,
                    launcher_attestation=launcher_attestation,
                    host_activation_handshake=host_activation_handshake,
                )
        exit_code = 0
    except Exception as exc:
        code = getattr(exc, "code", "CAPTURED_PAPER_STARTUP_REJECTED")
        report = {
            "schema_version": SERVICE_REPORT_SCHEMA_VERSION,
            "verdict": "CAPTURED_ALPACA_PAPER_STARTUP_REJECTED",
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "error_code": str(code),
            # 2026-07-17 observability fix: ang nilunok na traceback ay
            # nagpahirap sa bawat live diagnosis (codeless rejections).
            # Bounded detail para ma-diagnose ang fail-closed startup nang
            # hindi kailangang i-reproduce sa labas ng sealed path. Head+tail
            # para kasama ang ROOT CAUSE ng chained tracebacks (nasa unahan).
            "error_detail": (
                (lambda _tb: _tb if len(_tb) <= 2400 else _tb[:1400] + "\n...[gitna pinutol]...\n" + _tb[-1000:])(
                    traceback.format_exc()
                )
            ),
            "paper_execution_started": (
                None if active_order_boundary_entered else False
            ),
            "provider_sockets_started": (
                None if provider_start_may_have_been_attempted else False
            ),
            "database_connected": (
                None if external_runtime_boundary_entered else False
            ),
            "broker_contacted": (
                None if external_runtime_boundary_entered else False
            ),
            "orders_submitted": (
                None if active_order_boundary_entered else False
            ),
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        exit_code = 2
    finally:
        if service_singleton is not None:
            service_singleton.close()
    sys.stdout.buffer.write(_canonical_json_bytes(report) + b"\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CapturedAlpacaPaperServiceError",
    "SERVICE_REPORT_SCHEMA_VERSION",
    "main",
    "validate_offline_startup",
]
