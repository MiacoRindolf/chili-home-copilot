"""Rollback-safe Task Scheduler cutover for captured Alpaca PAPER.

The default command mode is ``ValidateOnly``.  Importing this module and
validation itself never mutates Task Scheduler, processes, providers, a
broker, or a database.  ``Apply`` and ``Rollback`` are explicit host
operations.  The apply path accepts only a fully verified, fake-money Alpaca
PAPER activation envelope and its typed rollback-snapshot v3 evidence.

The rollback receipt seals a *tokenized* candidate task XML before the final
activation manifest exists.  Apply replaces only the two already-standardized
manifest tokens after the final envelope has been verified, persists the
resolved XML in the journal's content-addressed object store, and registers
exactly that object.  This avoids a circular manifest commitment while still
binding the actual Task Scheduler action.

All host effects are behind :class:`HostCutoverBackend`.  Tests use a fake
backend; the Windows implementation uses argument-vector subprocess calls and
``psutil`` identity rechecks.  It never constructs a shell command from
untrusted input and never terminates a process selected by basename alone.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import copy
import csv
from dataclasses import dataclass, field, replace as dataclass_replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, NoReturn, Protocol, Sequence
import uuid
import xml.etree.ElementTree as ET

from scripts import captured_paper_activation_contract as activation_contract


UTC = timezone.utc
MODE_VALIDATE_ONLY = "ValidateOnly"
MODE_APPLY = "Apply"
MODE_ROLLBACK = "Rollback"
MODE_RECOVER_ONLY = "RecoverOnly"
APPLY_CONFIRMATION = "CUTOVER_FAKE_MONEY_ALPACA_PAPER"
_HOST_WIDE_CUTOVER_LOCK_PATH = (
    Path(tempfile.gettempdir())
    / "chili-captured-alpaca-paper-host-cutover.v1.lock"
)
_HOST_WIDE_CUTOVER_MUTEX_NAME = r"Global\CHILI-Captured-Alpaca-PAPER-Host-Cutover-v1"
# PAPER-only operational assumption boundary. alpaca-py's legacy
# ``TradingClient`` has no explicit HTTP timeout, so process termination proves
# that no local sender remains but cannot mathematically bound when an
# already-sent request becomes visible at Alpaca. Hold a sealed quiet horizon
# before issuing the short-lived permit, then require a broker fixed point.
# This evidence is never live-cash certification.
LEGACY_PAPER_BROKER_QUIET_HORIZON_SECONDS = 30.0
LEGACY_PAPER_BROKER_QUIET_HORIZON_POLICY = (
    "alpaca-paper-assumption-bound-quiet-horizon.v1"
)
# One historical pre-Docker-identity transaction reached only immutable local
# staging before TASK_XML_INVALID.  Its exact append-only bytes were verified
# on this host.  Generic trust-on-first-use adoption is forbidden: any other
# legacy journal requires an explicit, separately reviewed recovery path.
_PRE_IDENTITY_JOURNAL_RECOVERY_ALLOWLIST = frozenset(
    {"1f4592075ec786b1f274ebbc9156061327fad6f52b8f6bcf2ddd2e7eb2fb3be3"}
)

REQUIRED_LEGACY_TASKS = (
    "CHILI-IQFeed-Depth-Bridge-Daily",
    "CHILI-IQFeed-Depth-Bridge-Logon",
    "CHILI-IQFeed-Trade-Bridge-Daily",
    "CHILI-IQFeed-Trade-Bridge-Logon",
)
REQUIRED_LEGACY_PROCESS_ROLES = frozenset(
    {"iqfeed_trade_bridge", "iqfeed_depth_bridge"}
)
EXECUTION_LANE_RECREATOR_TASKS = (
    "CHILI-Docker-Socket-Guard",
    "CHILI-captured-paper-premarket-activation",
    "CHILI-liveness-watchdog",
    "CHILI-Premarket-Readiness",
    "CHILI-Premarket-Readiness-Recheck",
)
CANDIDATE_TASK_NAME = "CHILI-Captured-Alpaca-PAPER"
LEGACY_EXECUTION_LANE_NAME = "chili-clean-recovery-momentum-exec"
LEGACY_EXECUTION_LANE_PRIOR_STATES = frozenset({"running", "stopped"})
LEGACY_EXECUTION_LANE_SCHEMA = "chili.legacy-execution-lane-observation.v2"
_DOCKER_DESKTOP_EXECUTABLE = Path(
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
)
_EXECUTION_LANE_RECREATOR_SOURCES = MappingProxyType(
    {
        "CHILI-Docker-Socket-Guard": (
            (
                r"D:\dev\chili-home-copilot\scripts\run-hidden.vbs",
                "51d54330a857496ab88d31d6b716f2df0e764b6fc60947ac79f4b8a598e323b9",
            ),
            (
                r"D:\dev\chili-home-copilot\scripts\docker-socket-guard.ps1",
                "976c48a4a1cd4a11b3060d372c2ea6f3495bd6363d79147c7db5de3de07d631e",
            ),
            (
                r"D:\dev\chili-home-copilot\scripts\start-chili-stack.ps1",
                "cd5e382aa75d3c8849c50582a3bccefc3c7c4e1dfe5fdd3023dd59ac96fcd92c",
            ),
        ),
        "CHILI-captured-paper-premarket-activation": (
            (
                r"D:\CHILI-Docker\captured-paper\premarket-activation\run.ps1",
                "5061baaf9ddc3a63193fef04401d64b91cfb1c07e9afa9ee9e342f5e87669765",
            ),
            (
                r"D:\CHILI-Docker\captured-paper\premarket-activation\orchestrator.py",
                "c59008466a421646693a7b89914ea2b5862ec1b56ada53db0258ab81432dbf46",
            ),
        ),
        "CHILI-liveness-watchdog": (
            (
                r"D:\CHILI-Docker\captured-paper\premarket-activation\chili_liveness_watchdog.ps1",
                "e23b9acb9503daf58d83dfc1208f07eac5d9125bd670239e5e18f2292679ed14",
            ),
        ),
        "CHILI-Premarket-Readiness": (
            (
                r"D:\CHILI-Docker\premarket-readiness.ps1",
                "bd1c3a8db84e89ed0c38dab53c7aa4443f32118a62c293d286c52bf12d1defc3",
            ),
        ),
        "CHILI-Premarket-Readiness-Recheck": (
            (
                r"D:\CHILI-Docker\premarket-readiness.ps1",
                "bd1c3a8db84e89ed0c38dab53c7aa4443f32118a62c293d286c52bf12d1defc3",
            ),
        ),
    }
)
_EXECUTION_LANE_RECREATOR_DIRECT_SOURCES = MappingProxyType(
    {
        "CHILI-Docker-Socket-Guard": (
            r"D:\dev\chili-home-copilot\scripts\run-hidden.vbs",
            r"D:\dev\chili-home-copilot\scripts\docker-socket-guard.ps1",
        ),
        "CHILI-captured-paper-premarket-activation": (
            r"D:\CHILI-Docker\captured-paper\premarket-activation\run.ps1",
        ),
        "CHILI-liveness-watchdog": (
            r"D:\CHILI-Docker\captured-paper\premarket-activation\chili_liveness_watchdog.ps1",
        ),
        "CHILI-Premarket-Readiness": (
            r"D:\CHILI-Docker\premarket-readiness.ps1",
        ),
        "CHILI-Premarket-Readiness-Recheck": (
            r"D:\CHILI-Docker\premarket-readiness.ps1",
        ),
    }
)
_EXECUTION_LANE_COMPOSE_FILE = Path(
    r"D:\dev\chili-home-copilot\docker-compose.yml"
)
_EXECUTION_LANE_COMPOSE_FILE_SHA256 = (
    "fcb9f9d1c6515603bd4b0c3a7bbf0871c06cc088aa2d15e4c15776be124071a4"
)
_EXECUTION_LANE_COMPOSE_DEPENDENT_TASKS = frozenset(
    {
        "CHILI-Docker-Socket-Guard",
        "CHILI-Premarket-Readiness",
        "CHILI-Premarket-Readiness-Recheck",
    }
)
_EXECUTION_LANE_REQUIRED_SCOPE_FLAGS = MappingProxyType(
    {
        "CHILI_ALPACA_ENABLED": True,
        "CHILI_ALPACA_PAPER": True,
        "CHILI_MOMENTUM_EQUITY_EXECUTION_VIA_ALPACA_PAPER": True,
        "CHILI_MOMENTUM_PAPER_RUNNER_ENABLED": True,
        "CHILI_MOMENTUM_PAPER_RUNNER_SCHEDULER_ENABLED": True,
        "CHILI_AUTOTRADER_ENABLED": False,
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED": False,
        "CHILI_MOMENTUM_EXEC_AUTO_ARM_LIVE_ENABLED": False,
        "CHILI_MOMENTUM_EXEC_LIVE_RUNNER_ENABLED": False,
        "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": False,
        "CHILI_COINBASE_AUTOTRADER_LIVE": False,
        "COINBASE_AUTOTRADER_LIVE": False,
    }
)
SINGLETON_POLICY = "one_unified_candidate_host"
LEGACY_DIRECT_LAUNCH_KIND = "direct_python_v1"
LEGACY_WRAPPER_LAUNCH_KIND = "wscript_vbs_powershell_starter_v1"
RUN_HIDDEN_SEMANTIC_PROFILE = "chili_run_hidden_forward_wait_exit_v1"
DEPTH_STARTER_SEMANTIC_PROFILE = "chili_iqfeed_depth_bridge_starter_v1"
TRADE_STARTER_SEMANTIC_PROFILE = "chili_iqfeed_trade_bridge_starter_v1"
MANIFEST_PATH_TOKEN = "@verified:content-addressed-manifest-path"
MANIFEST_SHA256_TOKEN = "@verified:manifest-file-sha256"

TASK_SNAPSHOT_SCHEMA = "chili.captured-paper-host-task-snapshot.v1"
PROCESS_SNAPSHOT_SCHEMA = "chili.captured-paper-host-process-snapshot.v1"
RESTORE_PLAN_SCHEMA = "chili.captured-paper-host-restore-plan.v4"
CANDIDATE_ACTION_SCHEMA = "chili.captured-paper-host-cutover-action.v1"
JOURNAL_EVENT_SCHEMA = "chili.captured-paper-host-cutover-journal-event.v1"
JOURNAL_OBJECT_SCHEMA = "chili.captured-paper-host-cutover-journal-object.v1"
ROLLBACK_CAPSULE_SCHEMA = "chili.captured-paper-host-rollback-capsule.v1"
STARTUP_PREPARED_SCHEMA = "chili.captured-paper-host-startup-prepared.v1"
STARTUP_PERMIT_SCHEMA = "chili.captured-paper-host-startup-permit.v1"
STARTUP_STARTED_SCHEMA = "chili.captured-paper-host-startup-started.v2"
ACTIVE_START_AUTHORITY_SCHEMA = "chili.captured-paper-active-start-authority.v2"
STARTUP_REVOKED_SCHEMA = "chili.captured-paper-host-startup-revoked.v1"
PREACTIVATION_ROLLBACK_BASELINE_SCHEMA = (
    "chili.captured-paper-host-preactivation-rollback-baseline.v1"
)
PREACTIVATION_ROLLBACK_BASELINE_MODE = "PREACTIVATION_ROLLBACK_BASELINE"
STARTUP_HANDSHAKE_MAX_AGE_SECONDS = 30.0
STARTUP_DISPATCH_LOCK_WAIT_SECONDS = 30.0
STARTUP_DISPATCH_LOCK_BYTE = b"0"
STARTUP_DISPATCH_LOCK_BYTE_SHA256 = hashlib.sha256(
    STARTUP_DISPATCH_LOCK_BYTE
).hexdigest()

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
ACTIVE_START_EVIDENCE_MAX_BYTES = 512 * 1024
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_LEGACY_TASK_ROLE = MappingProxyType(
    {
        name: ("iqfeed_depth_bridge" if "-Depth-" in name else "iqfeed_trade_bridge")
        for name in REQUIRED_LEGACY_TASKS
    }
)
_LEGACY_ROLE_SCRIPT = MappingProxyType(
    {
        "iqfeed_depth_bridge": "iqfeed_depth_bridge.py",
        "iqfeed_trade_bridge": "iqfeed_trade_bridge.py",
    }
)
_LEGACY_ROLE_STARTER_PROFILE = MappingProxyType(
    {
        "iqfeed_depth_bridge": DEPTH_STARTER_SEMANTIC_PROFILE,
        "iqfeed_trade_bridge": TRADE_STARTER_SEMANTIC_PROFILE,
    }
)
_LEGACY_LAUNCH_CONTRACT_FIELDS = frozenset(
    {
        "task_name", "role", "launch_kind", "task_xml_sha256",
        "task_action_sha256", "task_command", "task_arguments",
        "working_directory", "task_host_path", "task_host_sha256",
        "wrapper_path", "wrapper_sha256", "wrapper_semantic_profile",
        "powershell_path", "powershell_sha256", "starter_path",
        "starter_sha256", "starter_semantic_profile",
        "expected_executable_path", "expected_executable_sha256",
        "expected_bridge_script_path", "expected_bridge_script_sha256",
        "expected_cmdline", "expected_cmdline_sha256",
        "scheduler_principal_sha256", "scheduler_settings_sha256",
        "trigger_profile", "role_semantic_sha256", "contract_sha256",
    }
)


class CapturedPaperHostCutoverError(RuntimeError):
    """Stable fail-closed host-cutover error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapturedPaperHostCutoverError(
            "NON_CANONICAL_JSON", "host-cutover material is not canonical JSON"
        ) from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(_canonical_json_bytes(value))


def _sha(value: Any, field: str) -> str:
    raw = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(raw) is None:
        raise CapturedPaperHostCutoverError(
            "INVALID_SHA256", f"{field} is not a canonical SHA-256"
        )
    return raw


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperHostCutoverError(
            "INVALID_CLOCK", "host-cutover clock must be timezone-aware"
        )
    return value.astimezone(UTC).isoformat()


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CapturedPaperHostCutoverError(
            "INVALID_TIMESTAMP", f"{field} is missing"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturedPaperHostCutoverError(
            "INVALID_TIMESTAMP", f"{field} is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        raise CapturedPaperHostCutoverError(
            "INVALID_TIMESTAMP", f"{field} is not timezone-aware"
        )
    return parsed.astimezone(UTC)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapturedPaperHostCutoverError(
            "INVALID_SCHEMA", f"{field} must be an object"
        )
    return value


def _exact_keys(value: Mapping[str, Any], expected: Iterable[str], field: str) -> None:
    wanted = set(expected)
    actual = set(value)
    if actual != wanted:
        raise CapturedPaperHostCutoverError(
            "INVALID_SCHEMA",
            f"{field} keys differ; missing={sorted(wanted-actual)} "
            f"extra={sorted(actual-wanted)}",
        )


def _strict_json(raw: bytes, field: str) -> Mapping[str, Any]:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise CapturedPaperHostCutoverError(
                    "DUPLICATE_JSON_KEY", f"{field} repeats JSON key {key}"
                )
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise CapturedPaperHostCutoverError(
            "NONFINITE_JSON", f"{field} contains non-finite JSON {value}"
        )

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturedPaperHostCutoverError(
            "INVALID_JSON", f"{field} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise CapturedPaperHostCutoverError(
            "INVALID_JSON", f"{field} root must be an object"
        )
    return value


def _is_local_absolute(path: Path) -> bool:
    raw = str(path)
    return (
        bool(_DRIVE_PATH_RE.match(raw))
        and not raw.startswith(("\\\\", "//"))
        # A colon after the drive designator selects an NTFS alternate data
        # stream.  An ADS is not the regular file identity represented by the
        # visible path and must never be accepted as sealed authority.
        and ":" not in raw[2:]
    )


def _reject_remote_drive(path: Path) -> None:
    """Reject mapped/network drive aliases on Windows.

    Drive-letter syntax alone does not prove local storage: Windows can map a
    UNC share to ``Z:``.  The non-Windows branch exists only for hermetic unit
    tests and never weakens the production Windows check.
    """

    if os.name != "nt":
        return
    try:
        import ctypes

        drive_root = f"{str(path)[:2]}\\"
        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(drive_root))
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperHostCutoverError(
            "DRIVE_IDENTITY_UNINSPECTABLE",
            f"cannot prove local-drive identity for {path}",
        ) from exc
    # DRIVE_REMOTE=4. DRIVE_UNKNOWN/NO_ROOT_DIR are also not acceptable for a
    # supposedly existing, local authority object.
    if drive_type in {0, 1, 4}:
        raise CapturedPaperHostCutoverError(
            "NONLOCAL_PATH", f"mapped/network drive is prohibited: {path}"
        )


def _reject_reparse_chain(path: Path) -> None:
    cursor = path
    while True:
        try:
            status = cursor.lstat()
        except OSError as exc:
            raise CapturedPaperHostCutoverError(
                "PATH_UNREADABLE", f"cannot inspect path {cursor}"
            ) from exc
        if int(getattr(status, "st_file_attributes", 0)) & _REPARSE_ATTRIBUTE:
            raise CapturedPaperHostCutoverError(
                "REPARSE_PATH", f"reparse-point path is prohibited: {path}"
            )
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    normalized = os.path.normcase(str(path))
    for root in roots:
        root_value = os.path.normcase(str(root))
        try:
            if os.path.commonpath((normalized, root_value)) == root_value:
                return True
        except ValueError:
            continue
    return False


def _strict_roots(values: Sequence[str | Path]) -> tuple[Path, ...]:
    if not values:
        raise CapturedPaperHostCutoverError(
            "READ_ROOTS_MISSING", "at least one local read root is required"
        )
    roots: list[Path] = []
    seen: set[str] = set()
    for value in values:
        raw = Path(value)
        if not _is_local_absolute(raw):
            raise CapturedPaperHostCutoverError(
                "NONLOCAL_ROOT", "read roots must be absolute local-drive paths"
            )
        _reject_remote_drive(raw)
        _reject_reparse_chain(raw.absolute())
        resolved = raw.resolve(strict=True)
        _reject_reparse_chain(resolved)
        if not resolved.is_dir():
            raise CapturedPaperHostCutoverError(
                "INVALID_ROOT", f"read root is not a directory: {resolved}"
            )
        key = os.path.normcase(str(resolved))
        if key in seen:
            raise CapturedPaperHostCutoverError(
                "DUPLICATE_ROOT", "canonical read roots must be unique"
            )
        seen.add(key)
        roots.append(resolved)
    return tuple(sorted(roots, key=lambda item: os.path.normcase(str(item))))


def _strict_existing_file(
    value: str | Path, *, roots: Sequence[Path], field: str
) -> Path:
    raw = Path(value)
    if not _is_local_absolute(raw):
        raise CapturedPaperHostCutoverError(
            "NONLOCAL_PATH", f"{field} must be an absolute local-drive path"
        )
    _reject_remote_drive(raw)
    _reject_reparse_chain(raw.absolute())
    resolved = raw.resolve(strict=True)
    _reject_reparse_chain(resolved)
    if not resolved.is_file() or not _inside(resolved, roots):
        raise CapturedPaperHostCutoverError(
            "PATH_OUTSIDE_ROOT", f"{field} escaped the allowed roots"
        )
    return resolved


def _strict_existing_dir(
    value: str | Path, *, roots: Sequence[Path], field: str
) -> Path:
    raw = Path(value)
    if not _is_local_absolute(raw):
        raise CapturedPaperHostCutoverError(
            "NONLOCAL_PATH", f"{field} must be an absolute local-drive path"
        )
    _reject_remote_drive(raw)
    _reject_reparse_chain(raw.absolute())
    resolved = raw.resolve(strict=True)
    _reject_reparse_chain(resolved)
    if not resolved.is_dir() or not _inside(resolved, roots):
        raise CapturedPaperHostCutoverError(
            "PATH_OUTSIDE_ROOT", f"{field} escaped the allowed roots"
        )
    return resolved


def _sealed_capsule_path(
    value: str | Path, *, roots: Sequence[Path], field: str
) -> Path:
    """Validate a capsule-owned path without requiring mutable bytes to exist."""

    raw = Path(value)
    if not _is_local_absolute(raw):
        raise CapturedPaperHostCutoverError(
            "NONLOCAL_PATH", f"{field} must be an absolute local-drive path"
        )
    _reject_remote_drive(raw)
    absolute = Path(os.path.abspath(str(raw)))
    if not _inside(absolute, roots):
        raise CapturedPaperHostCutoverError(
            "PATH_OUTSIDE_ROOT", f"{field} escaped the sealed roots"
        )
    cursor = absolute
    while not cursor.exists():
        parent = cursor.parent
        if parent == cursor:
            raise CapturedPaperHostCutoverError(
                "PATH_UNREADABLE", f"cannot inspect an ancestor for {field}"
            )
        cursor = parent
    _reject_reparse_chain(cursor)
    if absolute.exists():
        _reject_reparse_chain(absolute)
        resolved = absolute.resolve(strict=True)
        _reject_reparse_chain(resolved)
        if not _inside(resolved, roots):
            raise CapturedPaperHostCutoverError(
                "PATH_OUTSIDE_ROOT", f"{field} resolved outside sealed roots"
            )
        return resolved
    return absolute


def _assert_handle_final_path(handle: Any, path: Path, *, field: str) -> None:
    """Reject reparse redirection between path validation and open (TOCTOU).

    Component validation releases its state before the file is reopened by
    pathname, so a writable ancestor swapped to a junction in that window
    redirects the open elsewhere.  The open handle's kernel-resolved final
    path must therefore equal the validated lexical path while the handle is
    still the one being read.
    """

    if os.name != "nt":
        return
    try:
        import ctypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_unicode_buffer(32768)
        length = int(
            kernel32.GetFinalPathNameByHandleW(
                ctypes.c_void_p(msvcrt.get_osfhandle(handle.fileno())),
                buffer,
                len(buffer),
                0,
            )
        )
        if length <= 0 or length >= len(buffer):
            raise OSError("GetFinalPathNameByHandleW failed")
        final = buffer.value
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperHostCutoverError(
            "FILE_UNREADABLE", f"cannot resolve the open handle path for {field}"
        ) from exc
    normalized_final = os.path.normcase(final.removeprefix("\\\\?\\"))
    if normalized_final != os.path.normcase(str(path)):
        raise CapturedPaperHostCutoverError(
            "REPARSE_REDIRECTION",
            f"{field} resolved to a different final path while being read",
        )


def _stable_read(
    value: str | Path,
    *,
    roots: Sequence[Path],
    field: str,
    expected_sha256: str | None = None,
    max_bytes: int = _MAX_ARTIFACT_BYTES,
) -> tuple[Path, bytes, str]:
    path = _strict_existing_file(value, roots=roots, field=field)
    expected = _sha(expected_sha256, field) if expected_sha256 is not None else None
    try:
        before = path.stat()
        if before.st_size < 0 or before.st_size > max_bytes:
            raise CapturedPaperHostCutoverError(
                "ARTIFACT_TOO_LARGE", f"{field} exceeds its bounded size"
            )
        with path.open("rb") as handle:
            _assert_handle_final_path(handle, path, field=field)
            raw = handle.read(max_bytes + 1)
            after = os.stat(handle.fileno())
    except OSError as exc:
        raise CapturedPaperHostCutoverError(
            "FILE_UNREADABLE", f"cannot read {field}"
        ) from exc
    if len(raw) > max_bytes:
        raise CapturedPaperHostCutoverError(
            "ARTIFACT_TOO_LARGE", f"{field} exceeds its bounded size"
        )
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(raw) != after.st_size:
        raise CapturedPaperHostCutoverError(
            "FILE_DRIFT", f"{field} changed while it was read"
        )
    digest = sha256_bytes(raw)
    if expected is not None and digest != expected:
        raise CapturedPaperHostCutoverError(
            "HASH_MISMATCH", f"{field} content hash mismatch"
        )
    return path, raw, digest


def _stable_local_file_unrooted(
    value: str | Path, *, field: str, max_bytes: int = 256 * 1024 * 1024
) -> tuple[Path, str]:
    raw_path = Path(value)
    if not _is_local_absolute(raw_path):
        raise CapturedPaperHostCutoverError(
            "NONLOCAL_PATH", f"{field} must be an absolute local-drive path"
        )
    _reject_remote_drive(raw_path)
    _reject_reparse_chain(raw_path.absolute())
    path = raw_path.resolve(strict=True)
    _reject_reparse_chain(path)
    if not path.is_file():
        raise CapturedPaperHostCutoverError(
            "PATH_UNREADABLE", f"{field} is not a regular file"
        )
    before = path.stat()
    if before.st_size < 0 or before.st_size > max_bytes:
        raise CapturedPaperHostCutoverError(
            "ARTIFACT_TOO_LARGE", f"{field} exceeds its bounded size"
        )
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        _assert_handle_final_path(handle, path, field=field)
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise CapturedPaperHostCutoverError(
                    "ARTIFACT_TOO_LARGE", f"{field} exceeds its bounded size"
                )
            digest.update(chunk)
        after = os.stat(handle.fileno())
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or total != after.st_size
    ):
        raise CapturedPaperHostCutoverError(
            "FILE_DRIFT", f"{field} changed while it was hashed"
        )
    return path, digest.hexdigest()


@dataclass(frozen=True, slots=True)
class TaskObservation:
    name: str
    xml: bytes
    enabled: bool

    @property
    def xml_sha256(self) -> str:
        return sha256_bytes(self.xml)


@dataclass(frozen=True, slots=True)
class ExecutionLaneRecreatorTaskObservation:
    """Secret-free identity/state of one external lane resurrection task."""

    name: str
    definition_sha256: str
    action_sha256: str
    source_chain_sha256: str
    enabled: bool

    def identity_key(self) -> tuple[str, str, str, str]:
        return (
            self.name,
            self.definition_sha256,
            self.action_sha256,
            self.source_chain_sha256,
        )


@dataclass(frozen=True, slots=True)
class LegacyExecutionLaneObservation:
    """Secret-free immutable identity and current state of the legacy lane."""

    container_name: str
    container_id: str
    image_id: str
    config_sha256: str
    execution_scope: str
    scope_sha256: str
    recreator_tasks: tuple[ExecutionLaneRecreatorTaskObservation, ...]
    state: str

    def identity_key(self) -> tuple[Any, ...]:
        return (
            self.container_name,
            self.container_id,
            self.image_id,
            self.config_sha256,
            self.execution_scope,
            self.scope_sha256,
            tuple(item.identity_key() for item in self.recreator_tasks),
        )


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    create_time_ns: int
    executable_path: str
    executable_sha256: str
    cmdline: tuple[str, ...]
    cmdline_sha256: str
    role: str
    bridge_script_path: str | None = None
    bridge_script_sha256: str | None = None

    def semantic_key(self) -> tuple[Any, ...]:
        return (
            self.pid,
            self.create_time_ns,
            os.path.normcase(self.executable_path),
            self.executable_sha256,
            self.cmdline,
            self.cmdline_sha256,
            self.role,
            os.path.normcase(self.bridge_script_path or ""),
            self.bridge_script_sha256 or "",
        )


@dataclass(frozen=True, slots=True)
class LegacyProcessBinding:
    role: str
    executable_path: str
    executable_sha256: str
    bridge_script_path: str
    bridge_script_sha256: str
    restore_task: str
    restore_task_xml_sha256: str
    restore_task_action_sha256: str
    expected_cmdline: tuple[str, ...]
    expected_cmdline_sha256: str


@dataclass(frozen=True, slots=True)
class LegacyTaskLaunchContract:
    """Hash-bound restore launcher which targets one exact legacy process.

    This is deliberately a restore *launch contract*, not proof that the
    scheduled task historically created the process observed in the process
    snapshot.  Runtime rollback still requires the exact post-start PID,
    executable, bridge, and full argv readback.
    """

    task_name: str
    role: str
    launch_kind: str
    task_xml_sha256: str
    task_action_sha256: str
    task_command: str
    task_arguments: str
    working_directory: str
    task_host_path: str
    task_host_sha256: str
    wrapper_path: str | None
    wrapper_sha256: str | None
    wrapper_semantic_profile: str | None
    powershell_path: str | None
    powershell_sha256: str | None
    starter_path: str | None
    starter_sha256: str | None
    starter_semantic_profile: str | None
    expected_executable_path: str
    expected_executable_sha256: str
    expected_bridge_script_path: str
    expected_bridge_script_sha256: str
    expected_cmdline: tuple[str, ...]
    expected_cmdline_sha256: str
    scheduler_principal_sha256: str
    scheduler_settings_sha256: str
    trigger_profile: str
    role_semantic_sha256: str
    contract_sha256: str


@dataclass(frozen=True, slots=True)
class CandidateInvocation:
    task_name: str
    powershell_executable_path: str
    powershell_executable_sha256: str
    launcher_source_path: str
    launcher_source_sha256: str
    launcher_script_path: str
    launcher_script_sha256: str
    stage0_source_path: str
    stage0_source_sha256: str
    stage0_script_path: str
    stage0_script_sha256: str
    service_source_path: str
    service_source_sha256: str
    service_script_path: str
    service_script_sha256: str
    host_ready_receipt_base: str
    launcher_arguments: tuple[str, ...]
    python_executable_path: str
    python_executable_sha256: str
    python_dependency_root: str
    python_dependency_root_identity_sha256: str
    service_arguments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateProcessObservation:
    kind: str
    identity: ProcessIdentity


class HostCutoverBackend(Protocol):
    """All mutable host operations used by the state machine."""

    def get_task(self, name: str) -> TaskObservation | None: ...

    def set_task_enabled(self, name: str, enabled: bool) -> None: ...

    def register_task(self, name: str, xml_path: Path, expected_sha256: str) -> None: ...

    def start_task(self, name: str) -> None: ...

    def stop_task(self, name: str) -> None: ...

    def delete_task(self, name: str) -> None: ...

    def find_candidate_tasks(
        self, invocation: CandidateInvocation
    ) -> tuple[TaskObservation, ...]: ...

    def get_process(self, pid: int, *, role: str) -> ProcessIdentity | None: ...

    def stop_process(self, expected: ProcessIdentity) -> None: ...

    def find_legacy_processes(
        self, bindings: Sequence[LegacyProcessBinding]
    ) -> tuple[ProcessIdentity, ...]: ...

    def await_legacy_processes(
        self,
        bindings: Sequence[LegacyProcessBinding],
        *,
        timeout_seconds: float,
    ) -> tuple[ProcessIdentity, ...]: ...

    def await_candidate_processes(
        self, invocation: CandidateInvocation, *, timeout_seconds: float
    ) -> tuple[CandidateProcessObservation, ...]: ...

    def stop_candidate_process(
        self, expected: CandidateProcessObservation, invocation: CandidateInvocation
    ) -> None: ...

    def read_service_startup_receipt(
        self,
        invocation: CandidateInvocation,
        expected_service: ProcessIdentity,
        *,
        phase: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...

    def inspect_legacy_execution_lane(self) -> LegacyExecutionLaneObservation: ...

    def await_execution_lane_recreator_processes(
        self, *, timeout_seconds: float
    ) -> tuple[str, ...]: ...

    def quiesce_legacy_execution_lane(
        self, *, expected: LegacyExecutionLaneObservation
    ) -> int: ...

    def restore_legacy_execution_lane(
        self, *, expected: LegacyExecutionLaneObservation
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    captured_at: datetime
    tasks: Mapping[str, TaskObservation]
    artifact_path: Path
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    captured_at: datetime
    processes: tuple[ProcessIdentity, ...]
    artifact_path: Path
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class RestorePlan:
    task_enabled_states: Mapping[str, bool]
    restart_tasks: tuple[str, ...]
    bindings: tuple[LegacyProcessBinding, ...]
    candidate_task_name: str
    artifact_path: Path
    artifact_sha256: str
    launch_contracts: Mapping[str, LegacyTaskLaunchContract] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True, slots=True)
class PreparedCutover:
    activation_generation: str
    expected_account_id: str
    manifest_path: Path
    manifest_sha256: str
    candidate_root: Path
    allowed_read_roots: tuple[Path, ...]
    task_snapshot: TaskSnapshot
    process_snapshot: ProcessSnapshot
    restore_plan: RestorePlan
    candidate_action_path: Path
    candidate_action_sha256: str
    candidate_template_path: Path
    candidate_template_sha256: str
    resolved_task_xml: bytes
    resolved_task_xml_sha256: str
    invocation: CandidateInvocation
    rollback_receipt_sha256: str


@dataclass(frozen=True, slots=True)
class PreActivationRollbackContext:
    """Typed, non-order authority for validating rollback inputs early.

    This context deliberately has no activation manifest, broker authority or
    host backend.  It can only bind local bytes needed by a later cutover.
    """

    activation_generation: str
    expected_account_id: str
    candidate_root: Path
    allowed_read_roots: tuple[Path, ...]
    host_cutover_source_sha256: str
    launcher_argument_contract_sha256: str


@dataclass(frozen=True, slots=True)
class PreActivationRollbackBaseline:
    """Read-only validation result; never a final host ValidateOnly report."""

    context: PreActivationRollbackContext
    task_snapshot: TaskSnapshot
    process_snapshot: ProcessSnapshot
    restore_plan: RestorePlan
    candidate_action_path: Path
    candidate_action_sha256: str
    candidate_template_path: Path
    candidate_template_sha256: str
    validated_at: datetime
    baseline_sha256: str


@dataclass(frozen=True, slots=True)
class CutoverReport:
    mode: str
    verdict: str
    activation_generation: str
    manifest_sha256: str
    resolved_task_xml_sha256: str
    journal_path: Path | None
    mutation_count: int


def build_rollback_capsule_document(prepared: PreparedCutover) -> Mapping[str, Any]:
    """Serialize every authority input needed to undo one committed cutover.

    The capsule is published before the first host mutation.  Rollback never
    re-reads the mutable activation manifest, source receipt JSON, or template
    to decide what it owns or what legacy state to restore.
    """

    invocation = prepared.invocation
    return {
        "schema_version": ROLLBACK_CAPSULE_SCHEMA,
        "activation_generation": prepared.activation_generation,
        "expected_account_id": prepared.expected_account_id,
        "manifest_path": str(prepared.manifest_path),
        "manifest_sha256": prepared.manifest_sha256,
        "candidate_root": str(prepared.candidate_root),
        "allowed_read_roots": [str(item) for item in prepared.allowed_read_roots],
        "task_snapshot": build_task_snapshot_document(
            captured_at=prepared.task_snapshot.captured_at,
            tasks=prepared.task_snapshot.tasks,
        ),
        "process_snapshot": build_process_snapshot_document(
            captured_at=prepared.process_snapshot.captured_at,
            processes=prepared.process_snapshot.processes,
        ),
        "restore_plan": build_restore_plan_document(
            tasks=prepared.task_snapshot.tasks,
            bindings=prepared.restore_plan.bindings,
            launch_contracts=(
                prepared.restore_plan.launch_contracts or None
            ),
        ),
        "resolved_task_xml_base64": base64.b64encode(
            prepared.resolved_task_xml
        ).decode("ascii"),
        "resolved_task_xml_sha256": prepared.resolved_task_xml_sha256,
        "candidate_invocation": {
            "task_name": invocation.task_name,
            "powershell_executable_path": invocation.powershell_executable_path,
            "powershell_executable_sha256": invocation.powershell_executable_sha256,
            "launcher_source_path": invocation.launcher_source_path,
            "launcher_source_sha256": invocation.launcher_source_sha256,
            "launcher_script_path": invocation.launcher_script_path,
            "launcher_script_sha256": invocation.launcher_script_sha256,
            "stage0_source_path": invocation.stage0_source_path,
            "stage0_source_sha256": invocation.stage0_source_sha256,
            "stage0_script_path": invocation.stage0_script_path,
            "stage0_script_sha256": invocation.stage0_script_sha256,
            "service_source_path": invocation.service_source_path,
            "service_source_sha256": invocation.service_source_sha256,
            "service_script_path": invocation.service_script_path,
            "service_script_sha256": invocation.service_script_sha256,
            "host_ready_receipt_base": invocation.host_ready_receipt_base,
            "launcher_arguments": list(invocation.launcher_arguments),
            "python_executable_path": invocation.python_executable_path,
            "python_executable_sha256": invocation.python_executable_sha256,
            "python_dependency_root": invocation.python_dependency_root,
            "python_dependency_root_identity_sha256": (
                invocation.python_dependency_root_identity_sha256
            ),
            "service_arguments": list(invocation.service_arguments),
        },
        "rollback_receipt_sha256": prepared.rollback_receipt_sha256,
        "account_scope": "alpaca:paper",
        "live_cash_authorized": False,
    }


def _parse_string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CAPSULE_INVALID", f"{field} is not an exact argument vector"
        )
    return tuple(value)


def build_startup_prepared_receipt(
    *,
    prepared: PreparedCutover,
    service: ProcessIdentity,
    challenge_sha256: str,
    prepared_at: datetime,
    valid_until: datetime,
    dispatch_lock_identity: Mapping[str, Any],
) -> Mapping[str, Any]:
    paths = _startup_handshake_paths(
        prepared.invocation, roots=prepared.allowed_read_roots
    )
    lock_identity = _validate_dispatch_lock_identity(
        dispatch_lock_identity, expected_path=paths["dispatch_lock"]
    )
    body: dict[str, Any] = {
        "schema_version": STARTUP_PREPARED_SCHEMA,
        "state": "PREPARED",
        "activation_generation": prepared.activation_generation,
        "manifest_sha256": prepared.manifest_sha256,
        "account_scope": "alpaca:paper",
        "expected_account_id": prepared.expected_account_id,
        "service_pid": service.pid,
        "service_create_time_ns": service.create_time_ns,
        "service_executable_path": service.executable_path,
        "service_executable_sha256": service.executable_sha256,
        "service_cmdline_sha256": service.cmdline_sha256,
        "challenge_sha256": _sha(challenge_sha256, "startup challenge"),
        "prepared_at": _iso(prepared_at),
        "valid_until": _iso(valid_until),
        "workers_started": False,
        "paper_execution_started": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
        **dict(lock_identity),
    }
    body["receipt_sha256"] = sha256_json(body)
    return MappingProxyType(body)


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


def _validate_active_start_authority(
    value: Mapping[str, Any],
    *,
    prepared: PreparedCutover,
    activation_permit_sha256: str,
    host_quiet_horizon_event_sha256: str | None = None,
) -> str:
    """Independently validate the complete pre-worker PAPER evidence."""

    authority = _mapping(value, "active-start authority")
    _exact_keys(
        authority,
        {*_ACTIVE_START_AUTHORITY_BODY_KEYS, "authority_sha256"},
        "active-start authority",
    )
    claimed = _sha(authority.get("authority_sha256"), "active-start authority")
    body = dict(authority)
    body.pop("authority_sha256")
    broker = _mapping(body.get("broker_fixed_point"), "broker fixed point")
    final_kill = _mapping(
        body.get("final_kill_switch_query"), "final kill-switch query"
    )
    kill_body = dict(final_kill)
    kill_self_sha = _sha(
        kill_body.pop("query_receipt_sha256", None),
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
    snapshots = tuple(
        _mapping(broker.get(field), f"broker fixed-point {field}")
        for field in ("baseline_snapshot", "first_snapshot", "second_snapshot")
    )
    order_fences = tuple(
        _mapping(broker.get(field), f"broker fixed-point {field}")
        for field in ("first_order_census", "second_order_census")
    )
    fill_fences = tuple(
        _mapping(broker.get(field), f"broker fixed-point {field}")
        for field in (
            "first_fill_activity_census",
            "second_fill_activity_census",
        )
    )
    quiet_sha = _sha(
        body.get("host_quiet_horizon_event_sha256"),
        "host quiet-horizon event",
    )
    expected_quiet_sha = (
        quiet_sha
        if host_quiet_horizon_event_sha256 is None
        else _sha(
            host_quiet_horizon_event_sha256,
            "expected host quiet-horizon event",
        )
    )
    try:
        valid = (
            sha256_json(body) == claimed
            and all(_SHA256_RE.fullmatch(str(body.get(field) or "")) for field in digest_fields)
            and body.get("schema_version") == ACTIVE_START_AUTHORITY_SCHEMA
            and body.get("verdict")
            == "CAPTURED_ALPACA_PAPER_ACTIVE_START_AUTHORIZED"
            and body.get("account_scope") == "alpaca:paper"
            and body.get("expected_account_id") == prepared.expected_account_id
            and body.get("runtime_generation") == prepared.activation_generation
            and body.get("activation_manifest_sha256") == prepared.manifest_sha256
            and body.get("host_activation_permit_sha256")
            == activation_permit_sha256
            and quiet_sha == expected_quiet_sha
            and body.get("launcher_attestation_consumed") is True
            and body.get("host_activation_permit_consumed") is True
            and body.get("paper_order_submission_authorized") is True
            and body.get("live_cash_authorized") is False
            and body.get("real_money_authorized") is False
            and sha256_json(dict(broker))
            == body.get("broker_fixed_point_sha256")
            and sha256_json(dict(broker["second_snapshot"]))
            == body.get("post_permit_broker_snapshot_sha256")
            and sha256_json(dict(broker["second_order_census"]))
            == body.get("order_transition_fence_sha256")
            and sha256_json(dict(broker["second_fill_activity_census"]))
            == body.get("fill_activity_fence_sha256")
            and broker.get("schema_version")
            == "chili.captured-paper-broker-fixed-point.v1"
            and broker.get("verdict") == "PAPER_BROKER_QUIET_FIXED_POINT"
            and broker.get("account_scope") == "alpaca:paper"
            and broker.get("expected_account_id") == prepared.expected_account_id
            and broker.get("activation_generation")
            == prepared.activation_generation
            and broker.get("activation_manifest_sha256")
            == prepared.manifest_sha256
            and broker.get("assumption_bound") is True
            and broker.get("live_cash_certification") is False
            and all(
                snapshot.get("position_count") == 0
                and snapshot.get("open_order_count") == 0
                and snapshot.get("order_submission_call_count") == 0
                for snapshot in snapshots
            )
            and all(fence.get("exact_order_count") == 0 for fence in order_fences)
            and all(
                fence.get("exact_activity_count") == 0 for fence in fill_fences
            )
            and sha256_json(kill_body) == kill_self_sha
            and sha256_json(dict(final_kill))
            == body.get("final_kill_switch_query_sha256")
            and final_kill.get("schema_version")
            == "chili.captured-paper-kill-switch-query.v1"
            and final_kill.get("account_scope") == "alpaca:paper"
            and final_kill.get("expected_account_id")
            == prepared.expected_account_id
            and final_kill.get("activation_generation")
            == prepared.activation_generation
            and final_kill.get("active") is False
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise CapturedPaperHostCutoverError(
            "ACTIVE_START_AUTHORITY_INVALID",
            "active-start authority is not exact zero-exposure PAPER evidence",
        )
    return claimed


def build_startup_started_receipt(
    *,
    prepared: PreparedCutover,
    service: ProcessIdentity,
    challenge_sha256: str,
    prepared_receipt_sha256: str,
    activation_permit_sha256: str,
    active_start_authority: Mapping[str, Any],
    active_start_evidence_artifact_sha256: str,
    started_at: datetime,
    valid_until: datetime,
) -> Mapping[str, Any]:
    authority_sha256 = _validate_active_start_authority(
        active_start_authority,
        prepared=prepared,
        activation_permit_sha256=activation_permit_sha256,
    )
    body: dict[str, Any] = {
        "schema_version": STARTUP_STARTED_SCHEMA,
        "state": "STARTED",
        "activation_generation": prepared.activation_generation,
        "manifest_sha256": prepared.manifest_sha256,
        "account_scope": "alpaca:paper",
        "expected_account_id": prepared.expected_account_id,
        "service_pid": service.pid,
        "service_create_time_ns": service.create_time_ns,
        "service_executable_path": service.executable_path,
        "service_executable_sha256": service.executable_sha256,
        "service_cmdline_sha256": service.cmdline_sha256,
        "challenge_sha256": _sha(challenge_sha256, "startup challenge"),
        "prepared_receipt_sha256": _sha(
            prepared_receipt_sha256, "prepared receipt hash"
        ),
        "activation_permit_sha256": _sha(
            activation_permit_sha256, "activation permit hash"
        ),
        "active_start_authority_sha256": authority_sha256,
        "active_start_evidence_artifact_sha256": _sha(
            active_start_evidence_artifact_sha256,
            "active-start evidence artifact",
        ),
        "active_start_authority": dict(active_start_authority),
        "started_at": _iso(started_at),
        "valid_until": _iso(valid_until),
        "workers_started": True,
        "paper_execution_started": True,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    body["receipt_sha256"] = sha256_json(body)
    return MappingProxyType(body)


def _validate_startup_receipt_identity(
    value: Mapping[str, Any],
    *,
    prepared: PreparedCutover,
    service: ProcessIdentity,
    time_field: str,
    now: datetime,
    require_fresh: bool,
) -> tuple[str, datetime, datetime]:
    claimed = _sha(value.get("receipt_sha256"), "startup receipt hash")
    body = dict(value)
    body.pop("receipt_sha256")
    emitted = _parse_utc(value.get(time_field), f"startup {time_field}")
    valid_until = _parse_utc(value.get("valid_until"), "startup valid_until")
    observed = _parse_utc(_iso(now), "startup verification clock")
    if (
        sha256_json(body) != claimed
        or value.get("activation_generation") != prepared.activation_generation
        or value.get("manifest_sha256") != prepared.manifest_sha256
        or value.get("account_scope") != "alpaca:paper"
        or value.get("expected_account_id") != prepared.expected_account_id
        or value.get("service_pid") != service.pid
        or value.get("service_create_time_ns") != service.create_time_ns
        or os.path.normcase(str(value.get("service_executable_path") or ""))
        != os.path.normcase(service.executable_path)
        or value.get("service_executable_sha256") != service.executable_sha256
        or value.get("service_cmdline_sha256") != service.cmdline_sha256
        or value.get("live_cash_authorized") is not False
        or value.get("real_money_authorized") is not False
        or valid_until <= emitted
        or (valid_until - emitted).total_seconds()
        > STARTUP_HANDSHAKE_MAX_AGE_SECONDS
        or (
            require_fresh
            and (
                emitted > observed
                or observed >= valid_until
                or (observed - emitted).total_seconds()
                > STARTUP_HANDSHAKE_MAX_AGE_SECONDS
            )
        )
    ):
        raise CapturedPaperHostCutoverError(
            "STARTUP_RECEIPT_INVALID",
            "candidate service startup receipt escaped its exact identity/freshness",
        )
    return claimed, emitted, valid_until


def _validate_prepared_receipt(
    value: Mapping[str, Any], *, prepared: PreparedCutover,
    service: ProcessIdentity, now: datetime,
) -> tuple[str, str, datetime, Mapping[str, Any]]:
    _exact_keys(
        value,
        {
            "schema_version", "state", "activation_generation", "manifest_sha256",
            "account_scope", "expected_account_id", "service_pid",
            "service_create_time_ns", "service_executable_path",
            "service_executable_sha256", "service_cmdline_sha256",
            "challenge_sha256", "prepared_at", "valid_until", "workers_started",
            "paper_execution_started", "live_cash_authorized",
            "real_money_authorized", "receipt_sha256",
            *_DISPATCH_LOCK_IDENTITY_KEYS,
        },
        "startup PREPARED receipt",
    )
    claimed, _emitted, expires = _validate_startup_receipt_identity(
        value, prepared=prepared, service=service, time_field="prepared_at",
        now=now, require_fresh=True,
    )
    challenge = _sha(value.get("challenge_sha256"), "startup challenge")
    paths = _startup_handshake_paths(
        prepared.invocation, roots=prepared.allowed_read_roots
    )
    lock_identity = _validate_dispatch_lock_identity(
        {key: value.get(key) for key in _DISPATCH_LOCK_IDENTITY_KEYS},
        expected_path=paths["dispatch_lock"],
    )
    if not (
        value.get("schema_version") == STARTUP_PREPARED_SCHEMA
        and value.get("state") == "PREPARED"
        and value.get("workers_started") is False
        and value.get("paper_execution_started") is False
    ):
        raise CapturedPaperHostCutoverError(
            "STARTUP_PREPARED_INVALID", "PREPARED did not prove a no-worker state"
        )
    return claimed, challenge, expires, lock_identity


def _validate_started_receipt(
    value: Mapping[str, Any], *, prepared: PreparedCutover,
    service: ProcessIdentity, now: datetime, challenge_sha256: str,
    prepared_receipt_sha256: str, activation_permit_sha256: str,
    host_quiet_horizon_event_sha256: str,
    require_fresh: bool = True,
) -> str:
    _exact_keys(
        value,
        {
            "schema_version", "state", "activation_generation", "manifest_sha256",
            "account_scope", "expected_account_id", "service_pid",
            "service_create_time_ns", "service_executable_path",
            "service_executable_sha256", "service_cmdline_sha256",
            "challenge_sha256", "prepared_receipt_sha256",
            "activation_permit_sha256", "active_start_authority_sha256",
            "active_start_evidence_artifact_sha256",
            "active_start_authority", "started_at", "valid_until",
            "workers_started", "paper_execution_started", "live_cash_authorized",
            "real_money_authorized", "receipt_sha256",
        },
        "startup STARTED receipt",
    )
    claimed, _emitted, _expires = _validate_startup_receipt_identity(
        value, prepared=prepared, service=service, time_field="started_at",
        now=now, require_fresh=require_fresh,
    )
    authority = _mapping(
        value.get("active_start_authority"), "STARTED active-start authority"
    )
    authority_sha256 = _validate_active_start_authority(
        authority,
        prepared=prepared,
        activation_permit_sha256=activation_permit_sha256,
        host_quiet_horizon_event_sha256=host_quiet_horizon_event_sha256,
    )
    paths = _startup_handshake_paths(
        prepared.invocation, roots=prepared.allowed_read_roots
    )
    _evidence_path, evidence_raw, evidence_artifact_sha256 = _stable_read(
        paths["active_start_evidence"],
        roots=prepared.allowed_read_roots,
        field="active-start evidence artifact",
        max_bytes=ACTIVE_START_EVIDENCE_MAX_BYTES,
    )
    evidence = _strict_json(evidence_raw, "active-start evidence artifact")
    if not (
        value.get("schema_version") == STARTUP_STARTED_SCHEMA
        and value.get("state") == "STARTED"
        and value.get("challenge_sha256") == challenge_sha256
        and value.get("prepared_receipt_sha256") == prepared_receipt_sha256
        and value.get("activation_permit_sha256") == activation_permit_sha256
        and value.get("active_start_authority_sha256") == authority_sha256
        and value.get("active_start_evidence_artifact_sha256")
        == evidence_artifact_sha256
        and evidence == authority
        and evidence_raw == _canonical_json_bytes(evidence)
        and value.get("workers_started") is True
        and value.get("paper_execution_started") is True
    ):
        raise CapturedPaperHostCutoverError(
            "STARTUP_STARTED_INVALID", "STARTED did not consume the exact host permit"
        )
    return claimed


def build_task_snapshot_document(
    *,
    captured_at: datetime,
    tasks: Mapping[str, TaskObservation],
) -> Mapping[str, Any]:
    """Serialize the exact four read-only Task Scheduler XML observations."""

    _exact_keys(tasks, REQUIRED_LEGACY_TASKS, "task snapshot observations")
    rows: dict[str, Any] = {}
    for name in REQUIRED_LEGACY_TASKS:
        task = tasks[name]
        if task.name != name or _task_enabled_from_xml(task.xml) is not task.enabled:
            raise CapturedPaperHostCutoverError(
                "TASK_SNAPSHOT_STATE_MISMATCH", f"task {name} observation is inconsistent"
            )
        rows[name] = {
            "xml_base64": base64.b64encode(task.xml).decode("ascii"),
            "xml_sha256": task.xml_sha256,
            "enabled": task.enabled,
        }
    return {
        "schema_version": TASK_SNAPSHOT_SCHEMA,
        "captured_at": _iso(captured_at),
        "tasks": rows,
    }


def build_process_snapshot_document(
    *, captured_at: datetime, processes: Sequence[ProcessIdentity]
) -> Mapping[str, Any]:
    """Serialize exact PID/start/executable/script/cmdline provenance."""

    ordered = sorted(processes, key=lambda item: (item.role, item.pid))
    if (
        len({item.pid for item in ordered}) != len(ordered)
        or len({item.role for item in ordered}) != len(ordered)
        or {item.role for item in ordered} != set(REQUIRED_LEGACY_PROCESS_ROLES)
    ):
        raise CapturedPaperHostCutoverError(
            "PROCESS_SNAPSHOT_DUPLICATE",
            "process observations must bind exactly one trade and one depth role",
        )
    rows: list[Mapping[str, Any]] = []
    for item in ordered:
        if item.role not in {"iqfeed_trade_bridge", "iqfeed_depth_bridge"}:
            raise CapturedPaperHostCutoverError(
                "PROCESS_ROLE_INVALID", "process snapshot includes a non-legacy role"
            )
        if not item.bridge_script_path or not item.bridge_script_sha256:
            raise CapturedPaperHostCutoverError(
                "PROCESS_PROVENANCE_MISMATCH", "process snapshot lacks bridge provenance"
            )
        if item.cmdline_sha256 != sha256_json(list(item.cmdline)):
            raise CapturedPaperHostCutoverError(
                "PROCESS_IDENTITY_INVALID", "process cmdline digest is inconsistent"
            )
        rows.append(
            {
                "pid": item.pid,
                "create_time_ns": item.create_time_ns,
                "executable_path": item.executable_path,
                "executable_sha256": item.executable_sha256,
                "cmdline": list(item.cmdline),
                "cmdline_sha256": item.cmdline_sha256,
                "role": item.role,
                "bridge_script_path": item.bridge_script_path,
                "bridge_script_sha256": item.bridge_script_sha256,
            }
        )
    return {
        "schema_version": PROCESS_SNAPSHOT_SCHEMA,
        "captured_at": _iso(captured_at),
        "processes": rows,
    }


def build_restore_plan_document(
    *,
    tasks: Mapping[str, TaskObservation],
    bindings: Sequence[LegacyProcessBinding],
    launch_contracts: Mapping[str, LegacyTaskLaunchContract] | None = None,
) -> Mapping[str, Any]:
    """Serialize the deterministic, no-side-effect legacy restoration plan."""

    _exact_keys(tasks, REQUIRED_LEGACY_TASKS, "restore plan task observations")
    ordered = sorted(bindings, key=lambda item: item.role)
    if (
        len({item.role for item in ordered}) != len(ordered)
        or {item.role for item in ordered} != set(REQUIRED_LEGACY_PROCESS_ROLES)
    ):
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_INVALID",
            "restore bindings must bind exactly one trade and one depth role",
        )
    restart_tasks = sorted({item.restore_task for item in ordered})
    if any(name not in REQUIRED_LEGACY_TASKS for name in restart_tasks):
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_INVALID", "restore task is outside the exact legacy roster"
        )
    effective_contracts = (
        _derive_direct_launch_contracts(tasks=tasks, bindings=ordered)
        if launch_contracts is None
        else launch_contracts
    )
    _assert_launch_contract_roster(effective_contracts)
    by_task = {item.restore_task: item for item in ordered}
    if set(by_task) != set(restart_tasks) or any(
        effective_contracts[item.restore_task].role != item.role
        or tasks[item.restore_task].enabled is not True
        for item in ordered
    ):
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_INVALID",
            "each process role must select one enabled matching restore launch contract",
        )
    return {
        "schema_version": RESTORE_PLAN_SCHEMA,
        "task_enabled_states": {
            name: tasks[name].enabled for name in REQUIRED_LEGACY_TASKS
        },
        "restart_tasks": restart_tasks,
        "legacy_task_launch_contracts": {
            name: _launch_contract_payload(
                effective_contracts[name], include_contract_sha256=True
            )
            for name in REQUIRED_LEGACY_TASKS
        },
        "legacy_process_bindings": [
            {
                "role": item.role,
                "executable_path": item.executable_path,
                "executable_sha256": item.executable_sha256,
                "bridge_script_path": item.bridge_script_path,
                "bridge_script_sha256": item.bridge_script_sha256,
                "restore_task": item.restore_task,
                "restore_task_xml_sha256": item.restore_task_xml_sha256,
                "restore_task_action_sha256": item.restore_task_action_sha256,
                "expected_cmdline": list(item.expected_cmdline),
                "expected_cmdline_sha256": item.expected_cmdline_sha256,
            }
            for item in ordered
        ],
        "candidate_task_name": CANDIDATE_TASK_NAME,
    }


def _task_enabled_from_xml(raw: bytes) -> bool:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise CapturedPaperHostCutoverError(
            "TASK_XML_UNSAFE", "task XML cannot contain DTD/entity declarations"
        )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CapturedPaperHostCutoverError(
            "TASK_XML_INVALID", "task XML is malformed"
        ) from exc
    enabled = root.find(f".//{{{_TASK_NS}}}Settings/{{{_TASK_NS}}}Enabled")
    # Task Scheduler's published XSD defines a missing Settings/Enabled value
    # as ``true``.  schtasks omits this default from real exported task XML,
    # so treating absence as invalid makes a byte-faithful rollback snapshot
    # impossible.  An explicitly present value remains strict boolean text.
    if enabled is None:
        return True
    if (enabled.text or "").strip().lower() not in {"true", "false"}:
        raise CapturedPaperHostCutoverError(
            "TASK_XML_INVALID", "task XML Enabled setting is not boolean"
        )
    return (enabled.text or "").strip().lower() == "true"


def _normalize_schtasks_xml_output(raw: bytes) -> bytes:
    """Repair only the observed schtasks pipe encoding/declaration mismatch.

    On Windows, ``schtasks /Query /XML`` can write UTF-8 XML to a pipe while
    retaining ``encoding="UTF-16"`` in the XML declaration.  Those bytes are
    not parseable or safely restorable as declared.  When (and only when) the
    entire payload is strict UTF-8 and declares UTF-16, re-encode it as
    BOM-bearing UTF-16.  Other single-byte output fails closed instead of
    guessing a console code page.  Shared by the read-only collector and the
    cutover backend so both authorities observe identical task bytes.
    """

    if not isinstance(raw, bytes) or not raw:
        raise CapturedPaperHostCutoverError(
            "TASK_XML_INVALID", "task XML output is empty"
        )
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:128]:
        return raw
    declaration = raw[:256]
    if re.search(br"encoding\s*=\s*['\"]UTF-16['\"]", declaration, re.I):
        try:
            text = raw.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise CapturedPaperHostCutoverError(
                "TASK_XML_ENCODING_UNINSPECTABLE",
                "schtasks XML encoding differs from its UTF-16 declaration",
            ) from exc
        return text.encode("utf-16")
    return raw


def _task_exec_from_xml(raw: bytes) -> tuple[str, str]:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise CapturedPaperHostCutoverError(
            "TASK_XML_UNSAFE", "task XML cannot contain DTD/entity declarations"
        )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CapturedPaperHostCutoverError(
            "TASK_XML_INVALID", "task XML is malformed"
        ) from exc
    actions = root.find(f"{{{_TASK_NS}}}Actions")
    if actions is None or len(list(actions)) != 1:
        raise CapturedPaperHostCutoverError(
            "TASK_ACTION_INVALID", "candidate task must contain exactly one action"
        )
    action = list(actions)[0]
    if action.tag != f"{{{_TASK_NS}}}Exec":
        raise CapturedPaperHostCutoverError(
            "TASK_ACTION_INVALID", "candidate task action must be Exec"
        )
    command = action.find(f"{{{_TASK_NS}}}Command")
    arguments = action.find(f"{{{_TASK_NS}}}Arguments")
    if command is None or arguments is None:
        raise CapturedPaperHostCutoverError(
            "TASK_ACTION_INVALID", "candidate Exec action is incomplete"
        )
    command_text = (command.text or "").strip()
    argument_text = arguments.text or ""
    if not command_text or not argument_text:
        raise CapturedPaperHostCutoverError(
            "TASK_ACTION_INVALID", "candidate Exec action is empty"
        )
    return command_text, argument_text


def _task_exec_projection_from_xml(raw: bytes) -> Mapping[str, str]:
    command, arguments = _task_exec_from_xml(raw)
    root = ET.fromstring(raw)
    action = root.find(
        f"{{{_TASK_NS}}}Actions/{{{_TASK_NS}}}Exec"
    )
    assert action is not None
    working = action.find(f"{{{_TASK_NS}}}WorkingDirectory")
    return MappingProxyType(
        {
            "command": command,
            "arguments": arguments,
            "working_directory": (working.text or "").strip() if working is not None else "",
        }
    )


def _task_action_sha256(raw: bytes) -> str:
    return sha256_json(dict(_task_exec_projection_from_xml(raw)))


_LEGACY_TASK_TRIGGER_KIND = MappingProxyType(
    {"-Daily": "CalendarTrigger", "-Logon": "LogonTrigger"}
)


def _task_scheduler_projection_from_xml(
    raw: bytes, *, task_name: str
) -> Mapping[str, str]:
    """Project the scheduler execution semantics behind one legacy task.

    Name/action identity alone is not execution semantics: the Principal
    defines the security context and privilege level, ``Actions@Context``
    selects it, Settings gate execution policy, and Triggers decide when the
    scheduler runs the action on its own.  ``Settings/Enabled`` is lifecycle
    state and is deliberately excluded so enable/disable cycles do not change
    launch identity.
    """

    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise CapturedPaperHostCutoverError(
            "TASK_XML_UNSAFE", "task XML cannot contain DTD/entity declarations"
        )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CapturedPaperHostCutoverError(
            "TASK_XML_INVALID", "task XML is malformed"
        ) from exc
    trigger_kind = next(
        (
            kind
            for suffix, kind in _LEGACY_TASK_TRIGGER_KIND.items()
            if task_name.endswith(suffix)
        ),
        None,
    )
    if trigger_kind is None:
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            f"task {task_name} has no approved trigger classification",
        )

    def _child_text(parent: ET.Element, tag: str) -> str:
        node = parent.find(f"{{{_TASK_NS}}}{tag}")
        return (node.text or "").strip() if node is not None else ""

    principals = root.findall(f"{{{_TASK_NS}}}Principals")
    if (
        len(principals) != 1
        or len(list(principals[0])) != 1
        or list(principals[0])[0].tag != f"{{{_TASK_NS}}}Principal"
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            f"task {task_name} must declare exactly one principal",
        )
    principal = list(principals[0])[0]
    actions = root.find(f"{{{_TASK_NS}}}Actions")
    context = (
        str(actions.attrib.get("Context", "")).strip() if actions is not None else ""
    )
    principal_id = str(principal.attrib.get("id", "")).strip()
    if context != principal_id:
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            f"task {task_name} action context does not select its declared principal",
        )
    principal_projection = {
        "principal_id": principal_id,
        "user_id": _child_text(principal, "UserId"),
        "group_id": _child_text(principal, "GroupId"),
        "logon_type": _child_text(principal, "LogonType"),
        "run_level": _child_text(principal, "RunLevel") or "LeastPrivilege",
        "actions_context": context,
    }
    if not principal_projection["user_id"] and not principal_projection["group_id"]:
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            f"task {task_name} principal identity is empty",
        )
    settings_nodes = root.findall(f"{{{_TASK_NS}}}Settings")
    if len(settings_nodes) > 1:
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            f"task {task_name} declares more than one Settings element",
        )
    settings_c14n = ""
    if settings_nodes:
        settings_copy = copy.deepcopy(settings_nodes[0])
        for enabled in settings_copy.findall(f"{{{_TASK_NS}}}Enabled"):
            settings_copy.remove(enabled)
        settings_c14n = ET.canonicalize(
            ET.tostring(settings_copy, encoding="unicode")
        )
    triggers_nodes = root.findall(f"{{{_TASK_NS}}}Triggers")
    trigger_children = list(triggers_nodes[0]) if len(triggers_nodes) == 1 else []
    if (
        len(triggers_nodes) != 1
        or len(trigger_children) != 1
        or trigger_children[0].tag != f"{{{_TASK_NS}}}{trigger_kind}"
        or (_child_text(trigger_children[0], "Enabled") or "true").casefold()
        != "true"
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            f"task {task_name} must declare exactly one enabled {trigger_kind}"
            " and no other trigger",
        )
    return MappingProxyType(
        {
            "principal_sha256": sha256_json(principal_projection),
            "settings_sha256": sha256_json({"settings_c14n": settings_c14n}),
            "trigger_profile": trigger_kind,
        }
    )


def _quote_windows_arguments(arguments: Sequence[str]) -> str:
    # list2cmdline implements the Windows argv quoting rules without invoking a
    # shell.  Inputs are separately path/hash/schema verified before use.
    return subprocess.list2cmdline([str(value) for value in arguments])


def _windows_command_line_to_argv(value: str) -> tuple[str, ...]:
    """Parse one Task Scheduler argument string using Windows' argv rules."""

    if os.name != "nt":
        raise CapturedPaperHostCutoverError(
            "WINDOWS_REQUIRED", "legacy wrapper argv parsing requires Windows"
        )
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise CapturedPaperHostCutoverError(
            "LEGACY_LAUNCH_CONTRACT_INVALID", "task arguments are malformed"
        )
    try:
        import ctypes
        from ctypes import wintypes

        argc = ctypes.c_int(0)
        parser = ctypes.windll.shell32.CommandLineToArgvW
        parser.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
        parser.restype = ctypes.POINTER(wintypes.LPWSTR)
        result = parser(value, ctypes.byref(argc))
        if not result:
            raise OSError("CommandLineToArgvW failed")
        try:
            return tuple(str(result[index]) for index in range(argc.value))
        finally:
            local_free = ctypes.windll.kernel32.LocalFree
            local_free.argtypes = (ctypes.c_void_p,)
            local_free.restype = ctypes.c_void_p
            local_free(ctypes.cast(result, ctypes.c_void_p))
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperHostCutoverError(
            "LEGACY_LAUNCH_CONTRACT_INVALID", "cannot parse task arguments exactly"
        ) from exc


def _native_system32_directory() -> Path:
    """Resolve the immutable native System32 directory.

    Never derived from ``%SystemRoot%``: environment variables are mutable
    per-process state, so a forged ``SystemRoot`` could point the control
    plane at an attacker-staged ``System32`` tree.  ``GetSystemDirectoryW``
    is answered by the OS itself.  WOW64 processes are rejected because the
    file-system redirector silently maps ``System32`` to ``SysWOW64`` and
    the resolved identity would not be the binary the scheduler executes.
    """

    if os.name != "nt":
        raise CapturedPaperHostCutoverError(
            "WINDOWS_REQUIRED", "System32 launch identity requires Windows"
        )
    try:
        import ctypes
        from ctypes import wintypes

        # A private WinDLL instance: prototype assignments below must not
        # leak into the process-wide ctypes.windll cache.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.IsWow64Process.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        )
        kernel32.IsWow64Process.restype = wintypes.BOOL
        is_wow64 = wintypes.BOOL(0)
        succeeded = kernel32.IsWow64Process(
            kernel32.GetCurrentProcess(), ctypes.byref(is_wow64)
        )
        if not succeeded or is_wow64.value:
            raise CapturedPaperHostCutoverError(
                "LEGACY_SYSTEM_EXECUTABLE_INVALID",
                "WOW64 redirection makes the System32 identity ambiguous",
            )
        buffer = ctypes.create_unicode_buffer(32768)
        length = int(kernel32.GetSystemDirectoryW(buffer, len(buffer)))
        if length <= 0 or length >= len(buffer):
            raise OSError("GetSystemDirectoryW failed")
        return Path(buffer.value)
    except CapturedPaperHostCutoverError:
        raise
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperHostCutoverError(
            "LEGACY_SYSTEM_EXECUTABLE_INVALID",
            "cannot resolve the native Windows System32 directory",
        ) from exc


def _native_system32_executable(basename: str) -> Path:
    """Return the exact native path for one approved system executable."""

    system32 = _native_system32_directory()
    if basename.casefold() == "powershell.exe":
        return system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return system32 / basename


def _strict_system32_profile(value: str, *, basename: str) -> tuple[str, str]:
    """Bind one approved system executable by its exact absolute native path.

    Bare tokens (``wscript.exe``/``powershell.exe``) are rejected: a command
    name without a directory is resolved through current-directory/PATH
    search at launch time, so a stored hash of the native binary would not
    be bound to the binary the scheduler actually executes.
    """

    candidate = _native_system32_executable(basename)
    if not isinstance(value, str) or os.path.normcase(value) != os.path.normcase(
        str(candidate)
    ):
        raise CapturedPaperHostCutoverError(
            "LEGACY_SYSTEM_EXECUTABLE_INVALID",
            f"legacy launch requires the exact absolute native {basename} path",
        )
    path, digest = _stable_local_file_unrooted(
        candidate, field=f"legacy System32 {basename}"
    )
    return str(path), digest


def _normalized_semantic_lines(raw: bytes, *, language: str) -> tuple[str, ...]:
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise CapturedPaperHostCutoverError(
            "LEGACY_SOURCE_SEMANTICS_INVALID",
            f"legacy {language} source is not strict UTF-8",
        ) from exc
    if language not in ("PowerShell", "VBScript"):
        raise CapturedPaperHostCutoverError(
            "LEGACY_SOURCE_SEMANTICS_INVALID",
            f"legacy source language {language} is unsupported",
        )
    lines: list[str] = []
    pending = ""
    for source in text.splitlines():
        stripped = source.strip()
        if not stripped:
            continue
        if language == "PowerShell":
            # A leading single quote starts a string literal in PowerShell
            # (e.g. piped into Invoke-Expression), never a comment, so it
            # must stay in the semantic profile and mismatch the approved
            # source.  `#Requires` is an executable engine directive.
            if stripped.startswith("#"):
                if re.match(r"#requires\b", stripped, re.IGNORECASE):
                    raise CapturedPaperHostCutoverError(
                        "LEGACY_SOURCE_SEMANTICS_INVALID",
                        "legacy PowerShell source declares a #Requires directive",
                    )
                continue
        else:
            # VBScript comments are a leading apostrophe or Rem; `#` is not
            # a VBScript comment and must stay in the semantic profile.
            if stripped.startswith("'") or re.match(
                r"rem(\s|$)", stripped, re.IGNORECASE
            ):
                continue
        normalized = re.sub(r"[ \t]+", " ", stripped)
        if language == "PowerShell" and normalized.endswith("`"):
            pending += normalized[:-1].rstrip() + " "
            continue
        if pending:
            normalized = pending + normalized
            pending = ""
        lines.append(normalized)
    if pending:
        raise CapturedPaperHostCutoverError(
            "LEGACY_SOURCE_SEMANTICS_INVALID",
            f"legacy {language} source has a dangling continuation",
        )
    return tuple(lines)


def _validate_run_hidden_semantics(raw: bytes) -> None:
    expected = (
        "If WScript.Arguments.Count = 0 Then",
        'WScript.Echo "usage: wscript run-hidden.vbs <command> [args...]"',
        "WScript.Quit 2",
        "End If",
        "Dim sh, cmd, i, a",
        'Set sh = CreateObject("WScript.Shell")',
        'cmd = ""',
        "For i = 0 To WScript.Arguments.Count - 1",
        "a = WScript.Arguments(i)",
        'If InStr(a, " ") > 0 Then a = Chr(34) & a & Chr(34)',
        'cmd = cmd & a & " "',
        "Next",
        "WScript.Quit sh.Run(Trim(cmd), 0, True)",
    )
    if _normalized_semantic_lines(raw, language="VBScript") != expected:
        raise CapturedPaperHostCutoverError(
            "LEGACY_WRAPPER_SEMANTICS_INVALID",
            "run-hidden.vbs differs from the approved forward/wait/exit profile",
        )


def _validate_starter_semantics(
    raw: bytes,
    *,
    role: str,
    expected_executable_path: str,
    expected_bridge_script_path: str,
) -> str:
    if any(
        value in expected_executable_path + expected_bridge_script_path
        for value in ("'", '"', "`", "\r", "\n")
    ):
        raise CapturedPaperHostCutoverError(
            "LEGACY_SOURCE_SEMANTICS_INVALID", "starter target path is unsafe"
        )
    basename = _LEGACY_ROLE_SCRIPT.get(role)
    if basename is None or Path(expected_bridge_script_path).name.casefold() != basename.casefold():
        raise CapturedPaperHostCutoverError(
            "LEGACY_SOURCE_SEMANTICS_INVALID", "starter role/bridge target is inconsistent"
        )
    common = (
        "$ErrorActionPreference = 'SilentlyContinue'",
        "if (-not (Get-Process iqconnect -ErrorAction SilentlyContinue)) {",
        "Start-Process -FilePath 'E:\\DTN\\IQFeed\\iqconnect.exe' -WorkingDirectory 'E:\\DTN\\IQFeed'",
        "Start-Sleep -Seconds 20",
        "}",
        '$existing = Get-CimInstance Win32_Process -Filter "Name = \'python.exe\'" |',
        f"Where-Object {{ $_.CommandLine -like '*{basename}*' }}",
        "if ($existing) { exit 0 }",
    )
    if role == "iqfeed_depth_bridge":
        expected = common + (
            "$log = 'D:\\CHILI-Docker\\chili-data\\iqfeed_depth\\bridge.log'",
            "$err = 'D:\\CHILI-Docker\\chili-data\\iqfeed_depth\\bridge.err.log'",
            (
                f"Start-Process -FilePath '{expected_executable_path}' "
                f"-ArgumentList '{expected_bridge_script_path}' "
                "-WindowStyle Hidden -RedirectStandardOutput $log "
                "-RedirectStandardError $err"
            ),
        )
    elif role == "iqfeed_trade_bridge":
        expected = common + (
            "$dir = 'D:\\CHILI-Docker\\chili-data\\iqfeed_trades'",
            "if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }",
            "$log = Join-Path $dir 'bridge.log'",
            "$err = Join-Path $dir 'bridge.err.log'",
            (
                f"Start-Process -FilePath '{expected_executable_path}' "
                f"-ArgumentList '{expected_bridge_script_path}' "
                "-WindowStyle Hidden -RedirectStandardOutput $log "
                "-RedirectStandardError $err"
            ),
        )
    else:
        raise CapturedPaperHostCutoverError(
            "LEGACY_SOURCE_SEMANTICS_INVALID", "starter role is unsupported"
        )
    if _normalized_semantic_lines(raw, language="PowerShell") != expected:
        raise CapturedPaperHostCutoverError(
            "LEGACY_STARTER_SEMANTICS_INVALID",
            f"{role} starter differs from its approved complete source profile",
        )
    return _LEGACY_ROLE_STARTER_PROFILE[role]


def _launch_contract_payload(
    contract: LegacyTaskLaunchContract, *, include_contract_sha256: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "task_name": contract.task_name,
        "role": contract.role,
        "launch_kind": contract.launch_kind,
        "task_xml_sha256": contract.task_xml_sha256,
        "task_action_sha256": contract.task_action_sha256,
        "task_command": contract.task_command,
        "task_arguments": contract.task_arguments,
        "working_directory": contract.working_directory,
        "task_host_path": contract.task_host_path,
        "task_host_sha256": contract.task_host_sha256,
        "wrapper_path": contract.wrapper_path,
        "wrapper_sha256": contract.wrapper_sha256,
        "wrapper_semantic_profile": contract.wrapper_semantic_profile,
        "powershell_path": contract.powershell_path,
        "powershell_sha256": contract.powershell_sha256,
        "starter_path": contract.starter_path,
        "starter_sha256": contract.starter_sha256,
        "starter_semantic_profile": contract.starter_semantic_profile,
        "expected_executable_path": contract.expected_executable_path,
        "expected_executable_sha256": contract.expected_executable_sha256,
        "expected_bridge_script_path": contract.expected_bridge_script_path,
        "expected_bridge_script_sha256": contract.expected_bridge_script_sha256,
        "expected_cmdline": list(contract.expected_cmdline),
        "expected_cmdline_sha256": contract.expected_cmdline_sha256,
        "scheduler_principal_sha256": contract.scheduler_principal_sha256,
        "scheduler_settings_sha256": contract.scheduler_settings_sha256,
        "trigger_profile": contract.trigger_profile,
        "role_semantic_sha256": contract.role_semantic_sha256,
    }
    if include_contract_sha256:
        result["contract_sha256"] = contract.contract_sha256
    return result


def _role_semantic_material(contract: LegacyTaskLaunchContract) -> Mapping[str, Any]:
    return {
        "role": contract.role,
        "launch_kind": contract.launch_kind,
        "task_host_path": os.path.normcase(contract.task_host_path),
        "task_host_sha256": contract.task_host_sha256,
        "wrapper_path": os.path.normcase(contract.wrapper_path or ""),
        "wrapper_sha256": contract.wrapper_sha256,
        "wrapper_semantic_profile": contract.wrapper_semantic_profile,
        "powershell_path": os.path.normcase(contract.powershell_path or ""),
        "powershell_sha256": contract.powershell_sha256,
        "starter_path": os.path.normcase(contract.starter_path or ""),
        "starter_sha256": contract.starter_sha256,
        "starter_semantic_profile": contract.starter_semantic_profile,
        "expected_executable_path": os.path.normcase(contract.expected_executable_path),
        "expected_executable_sha256": contract.expected_executable_sha256,
        "expected_bridge_script_path": os.path.normcase(
            contract.expected_bridge_script_path
        ),
        "expected_bridge_script_sha256": contract.expected_bridge_script_sha256,
        "expected_cmdline": [os.path.normcase(value) for value in contract.expected_cmdline],
        "expected_cmdline_sha256": contract.expected_cmdline_sha256,
        # Trigger profiles are typed per task (Daily vs Logon differ by
        # design); principal/context/settings must match across the pair.
        "scheduler_principal_sha256": contract.scheduler_principal_sha256,
        "scheduler_settings_sha256": contract.scheduler_settings_sha256,
    }


def _seal_launch_contract(contract: LegacyTaskLaunchContract) -> LegacyTaskLaunchContract:
    role_sha = sha256_json(_role_semantic_material(contract))
    provisional = dataclass_replace(
        contract, role_semantic_sha256=role_sha, contract_sha256="0" * 64
    )
    contract_sha = sha256_json(
        _launch_contract_payload(provisional, include_contract_sha256=False)
    )
    return dataclass_replace(provisional, contract_sha256=contract_sha)


def _contract_for_direct_task(
    *, task: TaskObservation, binding: LegacyProcessBinding
) -> LegacyTaskLaunchContract:
    projection = _task_exec_projection_from_xml(task.xml)
    scheduler = _task_scheduler_projection_from_xml(task.xml, task_name=task.name)
    if not (
        os.path.normcase(projection["command"])
        == os.path.normcase(binding.executable_path)
        and projection["arguments"]
        == _quote_windows_arguments(binding.expected_cmdline[1:])
        and os.path.normcase(binding.expected_cmdline[0])
        == os.path.normcase(binding.executable_path)
        and projection["working_directory"] == ""
    ):
        raise CapturedPaperHostCutoverError(
            "LEGACY_LAUNCH_CONTRACT_INVALID",
            f"direct task {task.name} does not launch its exact process binding",
        )
    return _seal_launch_contract(
        LegacyTaskLaunchContract(
            task_name=task.name,
            role=binding.role,
            launch_kind=LEGACY_DIRECT_LAUNCH_KIND,
            task_xml_sha256=task.xml_sha256,
            task_action_sha256=_task_action_sha256(task.xml),
            task_command=projection["command"],
            task_arguments=projection["arguments"],
            working_directory="",
            task_host_path=binding.executable_path,
            task_host_sha256=binding.executable_sha256,
            wrapper_path=None,
            wrapper_sha256=None,
            wrapper_semantic_profile=None,
            powershell_path=None,
            powershell_sha256=None,
            starter_path=None,
            starter_sha256=None,
            starter_semantic_profile=None,
            expected_executable_path=binding.executable_path,
            expected_executable_sha256=binding.executable_sha256,
            expected_bridge_script_path=binding.bridge_script_path,
            expected_bridge_script_sha256=binding.bridge_script_sha256,
            scheduler_principal_sha256=scheduler["principal_sha256"],
            scheduler_settings_sha256=scheduler["settings_sha256"],
            trigger_profile=scheduler["trigger_profile"],
            expected_cmdline=binding.expected_cmdline,
            expected_cmdline_sha256=binding.expected_cmdline_sha256,
            role_semantic_sha256="0" * 64,
            contract_sha256="0" * 64,
        )
    )


def _derive_direct_launch_contracts(
    *, tasks: Mapping[str, TaskObservation], bindings: Sequence[LegacyProcessBinding]
) -> Mapping[str, LegacyTaskLaunchContract]:
    by_role = {item.role: item for item in bindings}
    if set(by_role) != set(REQUIRED_LEGACY_PROCESS_ROLES):
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_INVALID", "direct launch bindings do not cover both roles"
        )
    contracts = {
        name: _contract_for_direct_task(
            task=tasks[name], binding=by_role[_LEGACY_TASK_ROLE[name]]
        )
        for name in REQUIRED_LEGACY_TASKS
    }
    _assert_launch_contract_roster(contracts)
    return MappingProxyType(contracts)


def build_legacy_wrapper_launch_contracts(
    *,
    tasks: Mapping[str, TaskObservation],
    processes: Sequence[ProcessIdentity],
    legacy_root: str | Path,
    argv_parser: Callable[[str], tuple[str, ...]] = _windows_command_line_to_argv,
) -> Mapping[str, LegacyTaskLaunchContract]:
    """Build four strict restore contracts for the approved legacy wrapper chain."""

    _exact_keys(tasks, REQUIRED_LEGACY_TASKS, "legacy wrapper task observations")
    root = _strict_existing_dir(
        legacy_root,
        roots=_strict_roots((legacy_root,)),
        field="legacy wrapper root",
    )
    process_by_role = {item.role: item for item in processes}
    if (
        len(process_by_role) != len(processes)
        or set(process_by_role) != set(REQUIRED_LEGACY_PROCESS_ROLES)
    ):
        raise CapturedPaperHostCutoverError(
            "LEGACY_LAUNCH_CONTRACT_INVALID",
            "wrapper contracts require exactly one trade and one depth process",
        )
    contracts: dict[str, LegacyTaskLaunchContract] = {}
    for name in REQUIRED_LEGACY_TASKS:
        role = _LEGACY_TASK_ROLE[name]
        process = process_by_role[role]
        task = tasks[name]
        projection = _task_exec_projection_from_xml(task.xml)
        scheduler = _task_scheduler_projection_from_xml(task.xml, task_name=name)
        if task.name != name or projection["working_directory"] != "":
            raise CapturedPaperHostCutoverError(
                "LEGACY_LAUNCH_CONTRACT_INVALID",
                f"task {name} identity/working directory is not exact",
            )
        wscript_path, wscript_sha = _strict_system32_profile(
            projection["command"], basename="wscript.exe"
        )
        argv = argv_parser(projection["arguments"])
        if (
            len(argv) != 7
            or tuple(value.casefold() for value in argv[2:6])
            != ("-noprofile", "-executionpolicy", "bypass", "-file")
            or any(not value or any(c in value for c in "\x00\r\n") for value in argv)
        ):
            raise CapturedPaperHostCutoverError(
                "LEGACY_LAUNCH_CONTRACT_INVALID",
                f"task {name} wrapper argv has an extra or unsupported token",
            )
        powershell_path, powershell_sha = _strict_system32_profile(
            argv[1], basename="powershell.exe"
        )
        wrapper_path, wrapper_raw, wrapper_sha = _stable_read(
            argv[0], roots=(root,), field=f"{name} run-hidden wrapper"
        )
        starter_path, starter_raw, starter_sha = _stable_read(
            argv[6], roots=(root,), field=f"{name} PowerShell starter"
        )
        if wrapper_path.suffix.casefold() != ".vbs" or starter_path.suffix.casefold() != ".ps1":
            raise CapturedPaperHostCutoverError(
                "LEGACY_LAUNCH_CONTRACT_INVALID",
                f"task {name} wrapper/starter extensions are not exact",
            )
        _validate_run_hidden_semantics(wrapper_raw)
        executable_path, executable_sha = _stable_local_file_unrooted(
            process.executable_path, field=f"{role} process executable"
        )
        bridge_path, _bridge_raw, bridge_sha = _stable_read(
            str(process.bridge_script_path or ""),
            roots=(root,),
            field=f"{role} bridge script",
        )
        if not (
            process.cmdline == (str(executable_path), str(bridge_path))
            and process.cmdline_sha256 == sha256_json(list(process.cmdline))
            and executable_sha == process.executable_sha256
            and bridge_sha == process.bridge_script_sha256
        ):
            raise CapturedPaperHostCutoverError(
                "LEGACY_LAUNCH_CONTRACT_INVALID",
                f"{role} process identity differs from its exact two-token target",
            )
        starter_profile = _validate_starter_semantics(
            starter_raw,
            role=role,
            expected_executable_path=str(executable_path),
            expected_bridge_script_path=str(bridge_path),
        )
        contracts[name] = _seal_launch_contract(
            LegacyTaskLaunchContract(
                task_name=name,
                role=role,
                launch_kind=LEGACY_WRAPPER_LAUNCH_KIND,
                task_xml_sha256=task.xml_sha256,
                task_action_sha256=_task_action_sha256(task.xml),
                task_command=projection["command"],
                task_arguments=projection["arguments"],
                working_directory="",
                task_host_path=wscript_path,
                task_host_sha256=wscript_sha,
                wrapper_path=str(wrapper_path),
                wrapper_sha256=wrapper_sha,
                wrapper_semantic_profile=RUN_HIDDEN_SEMANTIC_PROFILE,
                powershell_path=powershell_path,
                powershell_sha256=powershell_sha,
                starter_path=str(starter_path),
                starter_sha256=starter_sha,
                starter_semantic_profile=starter_profile,
                expected_executable_path=str(executable_path),
                expected_executable_sha256=executable_sha,
                expected_bridge_script_path=str(bridge_path),
                expected_bridge_script_sha256=bridge_sha,
                expected_cmdline=process.cmdline,
                expected_cmdline_sha256=process.cmdline_sha256,
                scheduler_principal_sha256=scheduler["principal_sha256"],
                scheduler_settings_sha256=scheduler["settings_sha256"],
                trigger_profile=scheduler["trigger_profile"],
                role_semantic_sha256="0" * 64,
                contract_sha256="0" * 64,
            )
        )
    _assert_launch_contract_roster(contracts)
    return MappingProxyType(contracts)


def _assert_launch_contract_roster(
    contracts: Mapping[str, LegacyTaskLaunchContract],
) -> None:
    _exact_keys(contracts, REQUIRED_LEGACY_TASKS, "legacy launch contracts")
    for name, contract in contracts.items():
        if not (
            type(contract) is LegacyTaskLaunchContract
            and contract.task_name == name
            and contract.role == _LEGACY_TASK_ROLE[name]
            and contract.launch_kind
            in {LEGACY_DIRECT_LAUNCH_KIND, LEGACY_WRAPPER_LAUNCH_KIND}
            and contract.trigger_profile
            == next(
                kind
                for suffix, kind in _LEGACY_TASK_TRIGGER_KIND.items()
                if name.endswith(suffix)
            )
            and contract.role_semantic_sha256
            == sha256_json(_role_semantic_material(contract))
            and contract.contract_sha256
            == sha256_json(
                _launch_contract_payload(contract, include_contract_sha256=False)
            )
        ):
            raise CapturedPaperHostCutoverError(
                "LEGACY_LAUNCH_CONTRACT_INVALID",
                f"launch contract {name} is not internally hash-bound",
            )
    for role in sorted(REQUIRED_LEGACY_PROCESS_ROLES):
        values = [item for item in contracts.values() if item.role == role]
        if len(values) != 2 or len({item.role_semantic_sha256 for item in values}) != 1:
            raise CapturedPaperHostCutoverError(
                "LEGACY_LAUNCH_PAIR_MISMATCH",
                f"Daily/Logon restore semantics differ for {role}",
            )


def _materialize_candidate_xml(
    template: bytes, *, manifest_path: Path, manifest_sha256: str
) -> bytes:
    # 2026-07-17: production templates are UTF-16 on disk (schtasks/msxml
    # reject UTF-8-declared XML with "unable to switch the encoding",
    # reproduced live), so token replacement happens on decoded text and the
    # result re-encodes with the same encoding.  UTF-8 remains supported for
    # existing sealed artifacts and fixtures.  Dispatch is by BOM — decoding
    # UTF-8 bytes as UTF-16 does not reliably raise, it yields garbage.
    if template.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = template.decode("utf-16")
        recode = lambda value: value.encode("utf-16")
    else:
        text = template.decode("utf-8")
        recode = lambda value: value.encode("utf-8")
    if text.count(MANIFEST_PATH_TOKEN) != 1 or text.count(MANIFEST_SHA256_TOKEN) != 1:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_TOKEN_MISMATCH",
            "candidate task template must contain each manifest token exactly once",
        )
    path_raw = str(manifest_path)
    if any(value in path_raw for value in ('"', "<", ">", "&")):
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_PATH_UNSAFE",
            "manifest path is not safe for the sealed task action",
        )
    return recode(
        text.replace(MANIFEST_PATH_TOKEN, path_raw).replace(
            MANIFEST_SHA256_TOKEN, _sha(manifest_sha256, "manifest_sha256")
        )
    )


def _normalized_task_account_identity(value: str) -> str:
    """Normalize a Task Scheduler account to its SID when on Windows."""

    raw = str(value or "").strip()
    if not raw or any(character in raw for character in "\x00\r\n"):
        raise CapturedPaperHostCutoverError(
            "TASK_PRINCIPAL_UNINSPECTABLE", "candidate task principal is empty"
        )
    if re.fullmatch(r"S-\d-(?:\d+-)+\d+", raw, flags=re.I):
        return raw.upper()
    if os.name != "nt":
        return raw.casefold()
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        lookup = advapi32.LookupAccountNameW
        lookup.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        )
        lookup.restype = wintypes.BOOL
        sid_size = wintypes.DWORD(0)
        domain_size = wintypes.DWORD(0)
        sid_use = wintypes.DWORD(0)
        lookup(
            None,
            raw,
            None,
            ctypes.byref(sid_size),
            None,
            ctypes.byref(domain_size),
            ctypes.byref(sid_use),
        )
        if ctypes.get_last_error() != 122 or sid_size.value <= 0:
            raise OSError("LookupAccountNameW sizing failed")
        sid = ctypes.create_string_buffer(sid_size.value)
        domain = ctypes.create_unicode_buffer(max(1, domain_size.value))
        if not lookup(
            None,
            raw,
            sid,
            ctypes.byref(sid_size),
            domain,
            ctypes.byref(domain_size),
            ctypes.byref(sid_use),
        ):
            raise OSError("LookupAccountNameW failed")
        convert = advapi32.ConvertSidToStringSidW
        convert.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
        convert.restype = wintypes.BOOL
        sid_text = wintypes.LPWSTR()
        if not convert(sid, ctypes.byref(sid_text)):
            raise OSError("ConvertSidToStringSidW failed")
        try:
            return str(sid_text.value).upper()
        finally:
            local_free = kernel32.LocalFree
            local_free.argtypes = (ctypes.c_void_p,)
            local_free.restype = ctypes.c_void_p
            local_free(ctypes.cast(sid_text, ctypes.c_void_p))
    except (AttributeError, OSError, TypeError, ValueError):
        # Sealed tests and disconnected domain hosts can carry a syntactically
        # valid account which the local LSA cannot currently resolve.  Raw
        # case-folded identity remains strict (different principals still do
        # not compare equal); resolvable production names canonicalize to SID.
        return raw.casefold()


def _candidate_task_scheduler_projection_from_xml(raw: bytes) -> Mapping[str, Any]:
    """Normalize the candidate principal, triggers, settings and Exec policy."""

    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise CapturedPaperHostCutoverError(
            "TASK_XML_UNSAFE", "task XML cannot contain DTD/entity declarations"
        )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CapturedPaperHostCutoverError(
            "TASK_XML_INVALID", "candidate task XML is malformed"
        ) from exc
    if root.tag != f"{{{_TASK_NS}}}Task" or root.attrib.get("version") != "1.4":
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            "candidate task root/version changed during registration",
        )
    root_children = [item.tag.rsplit("}", 1)[-1] for item in list(root)]
    if (
        any(
            not item.tag.startswith(f"{{{_TASK_NS}}}")
            for item in list(root)
        )
        or set(root_children)
        != {"RegistrationInfo", "Triggers", "Principals", "Settings", "Actions"}
        or root_children.count("RegistrationInfo") != 1
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            "candidate task section roster changed during registration",
        )

    def one_root(tag: str) -> ET.Element:
        values = root.findall(f"{{{_TASK_NS}}}{tag}")
        if len(values) != 1:
            raise CapturedPaperHostCutoverError(
                "TASK_SCHEDULER_SEMANTICS_INVALID",
                f"candidate task must contain exactly one {tag}",
            )
        return values[0]

    def child_map(parent: ET.Element, *, field: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for item in list(parent):
            if not item.tag.startswith(f"{{{_TASK_NS}}}"):
                raise CapturedPaperHostCutoverError(
                    "TASK_SCHEDULER_SEMANTICS_INVALID",
                    f"candidate {field} contains a foreign namespace",
                )
            name = item.tag.rsplit("}", 1)[-1]
            if name in values:
                raise CapturedPaperHostCutoverError(
                    "TASK_SCHEDULER_SEMANTICS_INVALID",
                    f"candidate {field} repeats {name}",
                )
            if (item.attrib or list(item)) and not (
                field == "settings" and name == "IdleSettings"
            ):
                raise CapturedPaperHostCutoverError(
                    "TASK_SCHEDULER_SEMANTICS_INVALID",
                    f"candidate {field} scalar {name} contains nested policy",
                )
            values[name] = (item.text or "").strip()
        return values

    triggers = one_root("Triggers")
    if triggers.attrib or list(triggers):
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            "candidate task must be on-demand-only with no automatic triggers",
        )

    principals = one_root("Principals")
    if principals.attrib or len(list(principals)) != 1:
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            "candidate task must contain exactly one principal",
        )
    principal = list(principals)[0]
    if principal.tag != f"{{{_TASK_NS}}}Principal" or set(principal.attrib) != {"id"}:
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID", "candidate principal roster changed"
        )
    principal_values = child_map(principal, field="principal")
    if not (
        {"UserId", "LogonType", "RunLevel"}.issubset(principal_values)
        and set(principal_values)
        <= {"UserId", "LogonType", "RunLevel", "ProcessTokenSidType"}
        and principal_values.get("ProcessTokenSidType", "Default") == "Default"
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID", "candidate principal policy changed"
        )
    principal_id = str(principal.attrib["id"]).strip()
    if not principal_id:
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID", "candidate principal id is empty"
        )

    settings = one_root("Settings")
    if settings.attrib:
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID", "candidate settings gained attributes"
        )
    settings_values = child_map(settings, field="settings")
    # Task Scheduler is allowed to omit schema-default values from an XML
    # readback.  Compare the effective policy, not the serializer's choice to
    # spell those defaults.  Non-default values remain mandatory because an
    # omission would change the authored task's behavior.
    required_scalars = {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "StartWhenAvailable": "true",
        "ExecutionTimeLimit": "PT0S",
    }
    scheduler_default_scalars = {
        "AllowHardTerminate": "true",
        "RunOnlyIfNetworkAvailable": "false",
        "AllowStartOnDemand": "true",
        "Enabled": "true",
        "Hidden": "false",
        "RunOnlyIfIdle": "false",
        "WakeToRun": "false",
        "Priority": "7",
        "DisallowStartOnRemoteAppSession": "false",
        # Current Windows Task Scheduler materializes this as true on the
        # readback of the sealed on-demand/no-trigger task even when the
        # authored XML omits the schema-default field.
        "UseUnifiedSchedulingEngine": "true",
        "Volatile": "false",
    }
    if not (
        (set(required_scalars) | {"IdleSettings"}) <= set(settings_values)
        and set(settings_values)
        <= (
            set(required_scalars)
            | set(scheduler_default_scalars)
            | {"IdleSettings"}
        )
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            "candidate settings roster changed or enables automatic restart",
        )
    for key, expected in required_scalars.items():
        actual = settings_values[key]
        if expected in {"true", "false"}:
            actual = actual.casefold()
        if actual != expected:
            raise CapturedPaperHostCutoverError(
                "TASK_SCHEDULER_SEMANTICS_INVALID",
                f"candidate setting {key} changed during registration",
            )
    for key, expected in scheduler_default_scalars.items():
        actual = settings_values.get(key, expected)
        if key == "Enabled":
            if actual.casefold() not in {"true", "false"}:
                raise CapturedPaperHostCutoverError(
                    "TASK_SCHEDULER_SEMANTICS_INVALID",
                    "candidate lifecycle Enabled setting is not boolean",
                )
            # Enablement is checked from TaskObservation at each lifecycle
            # boundary and is intentionally excluded from definition identity.
            continue
        if actual.casefold() != expected:
            raise CapturedPaperHostCutoverError(
                "TASK_SCHEDULER_SEMANTICS_INVALID",
                f"candidate scheduler-default setting {key} changed",
            )
    idle = next(
        item for item in list(settings)
        if item.tag == f"{{{_TASK_NS}}}IdleSettings"
    )
    idle_values = child_map(idle, field="IdleSettings")
    idle_expected = {"StopOnIdleEnd": "false"}
    idle_defaults = {
        "RestartOnIdle": "false",
        "Duration": "PT10M",
        "WaitTimeout": "PT1H",
    }
    if (
        idle.attrib
        or not set(idle_expected).issubset(idle_values)
        or set(idle_values) - set(idle_expected) - set(idle_defaults)
        or any(
            idle_values[key].casefold() != value
            for key, value in idle_expected.items()
        )
        or any(
            idle_values.get(key, value) != value
            for key, value in idle_defaults.items()
        )
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID", "candidate idle settings changed"
        )

    actions = one_root("Actions")
    if actions.attrib != {"Context": principal_id}:
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_SEMANTICS_INVALID",
            "candidate action context changed principal",
        )
    exec_projection = dict(_task_exec_projection_from_xml(raw))
    return MappingProxyType(
        {
            "principal_id": principal_id,
            "principal_user_id": _normalized_task_account_identity(
                principal_values["UserId"]
            ),
            "logon_type": principal_values["LogonType"].casefold(),
            "run_level": principal_values["RunLevel"].casefold(),
            "process_token_sid_type": principal_values.get(
                "ProcessTokenSidType", "Default"
            ),
            "trigger_profile": "on_demand_only",
            "settings": {
                **{
                    key: value for key, value in required_scalars.items()
                    if key != "Enabled"
                },
                **{
                    key: value for key, value in scheduler_default_scalars.items()
                    if key != "Enabled"
                },
                "IdleSettings": {**idle_expected, **idle_defaults},
            },
            "command": exec_projection["command"],
            "arguments": exec_projection["arguments"],
            "working_directory": exec_projection["working_directory"],
        }
    )


def _candidate_task_semantics_match(
    observed_xml: bytes, resolved_xml: bytes
) -> bool:
    """Prove an installed candidate task is the sealed one, semantically.

    Task Scheduler re-serializes registered XML (UTF-16/CRLF, added <URI>,
    canonicalized principal), so authored bytes never survive a
    /Create -> /Query round trip and byte/sha compares are impossible by
    construction (2026-07-17, first live Apply).  Identity includes the
    Exec action — command, arguments, working directory — compared with
    principal account/logon/run-level, empty trigger roster and exact
    no-restart settings, all compared against the sealed resolved template."""

    try:
        observed_projection = dict(
            _candidate_task_scheduler_projection_from_xml(observed_xml)
        )
        resolved_projection = dict(
            _candidate_task_scheduler_projection_from_xml(resolved_xml)
        )
    except CapturedPaperHostCutoverError:
        return False
    for key in ("command", "arguments", "working_directory"):
        observed_projection[key] = os.path.normcase(str(observed_projection[key]))
        resolved_projection[key] = os.path.normcase(str(resolved_projection[key]))
    return observed_projection == resolved_projection


def _decode_b64(value: Any, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise CapturedPaperHostCutoverError(
            "INVALID_BASE64", f"{field} is missing"
        )
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise CapturedPaperHostCutoverError(
            "INVALID_BASE64", f"{field} is not canonical base64"
        ) from exc
    if base64.b64encode(raw).decode("ascii") != value:
        raise CapturedPaperHostCutoverError(
            "INVALID_BASE64", f"{field} is not canonical base64"
        )
    return raw


def _parse_task_snapshot(
    *, path: Path, raw: bytes, digest: str, receipt_evidence: Mapping[str, Any]
) -> TaskSnapshot:
    document = _strict_json(raw, "task_snapshot")
    if raw != _canonical_json_bytes(document):
        raise CapturedPaperHostCutoverError(
            "ARTIFACT_NOT_CANONICAL", "task snapshot is not canonical JSON"
        )
    _exact_keys(document, {"schema_version", "captured_at", "tasks"}, "task_snapshot")
    if document.get("schema_version") != TASK_SNAPSHOT_SCHEMA:
        raise CapturedPaperHostCutoverError(
            "ARTIFACT_SCHEMA_MISMATCH", "task snapshot schema is unsupported"
        )
    tasks_value = _mapping(document.get("tasks"), "task_snapshot.tasks")
    _exact_keys(tasks_value, REQUIRED_LEGACY_TASKS, "task_snapshot.tasks")
    receipt_hashes = _mapping(
        receipt_evidence.get("scheduled_task_xml_sha256s"),
        "rollback_snapshot.scheduled_task_xml_sha256s",
    )
    _exact_keys(receipt_hashes, REQUIRED_LEGACY_TASKS, "rollback task hashes")
    parsed: dict[str, TaskObservation] = {}
    for name in REQUIRED_LEGACY_TASKS:
        item = _mapping(tasks_value.get(name), f"task_snapshot.tasks.{name}")
        _exact_keys(item, {"xml_base64", "xml_sha256", "enabled"}, f"task {name}")
        xml = _decode_b64(item.get("xml_base64"), f"task {name} XML")
        claimed = _sha(item.get("xml_sha256"), f"task {name} xml_sha256")
        receipt_sha = _sha(receipt_hashes.get(name), f"receipt task {name}")
        if sha256_bytes(xml) != claimed or claimed != receipt_sha:
            raise CapturedPaperHostCutoverError(
                "TASK_SNAPSHOT_HASH_MISMATCH", f"task {name} XML hash mismatch"
            )
        enabled = item.get("enabled")
        if not isinstance(enabled, bool) or _task_enabled_from_xml(xml) is not enabled:
            raise CapturedPaperHostCutoverError(
                "TASK_SNAPSHOT_STATE_MISMATCH", f"task {name} Enabled state mismatch"
            )
        parsed[name] = TaskObservation(name=name, xml=xml, enabled=enabled)
    return TaskSnapshot(
        captured_at=_parse_utc(document.get("captured_at"), "task_snapshot.captured_at"),
        tasks=MappingProxyType(parsed),
        artifact_path=path,
        artifact_sha256=digest,
    )


def _strict_bound_file(
    value: Any,
    *,
    expected_sha256: Any,
    roots: Sequence[Path],
    field: str,
) -> tuple[str, str]:
    path, _raw, digest = _stable_read(
        str(value or ""),
        roots=roots,
        field=field,
        expected_sha256=_sha(expected_sha256, f"{field}.sha256"),
    )
    return str(path), digest


def _parse_process_identity(
    value: Mapping[str, Any],
    *,
    roots: Sequence[Path],
    field: str,
    verify_bound_files: bool = True,
) -> ProcessIdentity:
    _exact_keys(
        value,
        {
            "pid",
            "create_time_ns",
            "executable_path",
            "executable_sha256",
            "cmdline",
            "cmdline_sha256",
            "role",
            "bridge_script_path",
            "bridge_script_sha256",
        },
        field,
    )
    pid = value.get("pid")
    create_time_ns = value.get("create_time_ns")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(create_time_ns, int)
        or isinstance(create_time_ns, bool)
        or create_time_ns <= 0
    ):
        raise CapturedPaperHostCutoverError(
            "PROCESS_IDENTITY_INVALID", f"{field} has invalid PID/start time"
        )
    role = str(value.get("role") or "").strip()
    if role not in {"iqfeed_trade_bridge", "iqfeed_depth_bridge"}:
        raise CapturedPaperHostCutoverError(
            "PROCESS_ROLE_INVALID", f"{field} has an unsupported legacy role"
        )
    if verify_bound_files:
        executable, executable_sha = _strict_bound_file(
            value.get("executable_path"),
            expected_sha256=value.get("executable_sha256"),
            roots=roots,
            field=f"{field}.executable",
        )
        script, script_sha = _strict_bound_file(
            value.get("bridge_script_path"),
            expected_sha256=value.get("bridge_script_sha256"),
            roots=roots,
            field=f"{field}.bridge_script",
        )
    else:
        executable = str(
            _sealed_capsule_path(
                str(value.get("executable_path") or ""),
                roots=roots,
                field=f"{field}.executable",
            )
        )
        executable_sha = _sha(
            value.get("executable_sha256"), f"{field}.executable_sha256"
        )
        script = str(
            _sealed_capsule_path(
                str(value.get("bridge_script_path") or ""),
                roots=roots,
                field=f"{field}.bridge_script",
            )
        )
        script_sha = _sha(
            value.get("bridge_script_sha256"), f"{field}.bridge_script_sha256"
        )
    cmdline_value = value.get("cmdline")
    if (
        not isinstance(cmdline_value, list)
        or not cmdline_value
        or any(not isinstance(item, str) or not item for item in cmdline_value)
    ):
        raise CapturedPaperHostCutoverError(
            "PROCESS_IDENTITY_INVALID", f"{field}.cmdline is invalid"
        )
    cmdline = tuple(cmdline_value)
    cmdline_sha = _sha(value.get("cmdline_sha256"), f"{field}.cmdline_sha256")
    if sha256_json(list(cmdline)) != cmdline_sha:
        raise CapturedPaperHostCutoverError(
            "PROCESS_IDENTITY_INVALID", f"{field}.cmdline hash mismatch"
        )
    if not any(os.path.normcase(item) == os.path.normcase(script) for item in cmdline):
        raise CapturedPaperHostCutoverError(
            "PROCESS_PROVENANCE_MISMATCH",
            f"{field} command line does not contain the exact bridge script",
        )
    return ProcessIdentity(
        pid=pid,
        create_time_ns=create_time_ns,
        executable_path=executable,
        executable_sha256=executable_sha,
        cmdline=cmdline,
        cmdline_sha256=cmdline_sha,
        role=role,
        bridge_script_path=script,
        bridge_script_sha256=script_sha,
    )


def _parse_process_snapshot(
    *,
    path: Path,
    raw: bytes,
    digest: str,
    roots: Sequence[Path],
    verify_bound_files: bool = True,
) -> ProcessSnapshot:
    document = _strict_json(raw, "process_snapshot")
    if raw != _canonical_json_bytes(document):
        raise CapturedPaperHostCutoverError(
            "ARTIFACT_NOT_CANONICAL", "process snapshot is not canonical JSON"
        )
    _exact_keys(
        document, {"schema_version", "captured_at", "processes"}, "process_snapshot"
    )
    if document.get("schema_version") != PROCESS_SNAPSHOT_SCHEMA:
        raise CapturedPaperHostCutoverError(
            "ARTIFACT_SCHEMA_MISMATCH", "process snapshot schema is unsupported"
        )
    values = document.get("processes")
    if not isinstance(values, list):
        raise CapturedPaperHostCutoverError(
            "INVALID_SCHEMA", "process_snapshot.processes must be an array"
        )
    parsed = tuple(
        _parse_process_identity(
            _mapping(item, f"process_snapshot.processes[{index}]"),
            roots=roots,
            field=f"process_snapshot.processes[{index}]",
            verify_bound_files=verify_bound_files,
        )
        for index, item in enumerate(values)
    )
    if len({item.pid for item in parsed}) != len(parsed):
        raise CapturedPaperHostCutoverError(
            "PROCESS_SNAPSHOT_DUPLICATE", "process snapshot repeats a PID"
        )
    if (
        len({item.role for item in parsed}) != len(parsed)
        or {item.role for item in parsed} != set(REQUIRED_LEGACY_PROCESS_ROLES)
    ):
        raise CapturedPaperHostCutoverError(
            "PROCESS_SNAPSHOT_DUPLICATE",
            "process snapshot must bind exactly one trade and one depth role",
        )
    return ProcessSnapshot(
        captured_at=_parse_utc(
            document.get("captured_at"), "process_snapshot.captured_at"
        ),
        processes=tuple(sorted(parsed, key=lambda item: (item.role, item.pid))),
        artifact_path=path,
        artifact_sha256=digest,
    )


def _parse_launch_contract(
    value: Mapping[str, Any],
    *,
    roots: Sequence[Path],
    field_name: str,
    verify_bound_files: bool,
) -> LegacyTaskLaunchContract:
    _exact_keys(value, _LEGACY_LAUNCH_CONTRACT_FIELDS, field_name)
    role = str(value.get("role") or "")
    launch_kind = str(value.get("launch_kind") or "")
    if role not in REQUIRED_LEGACY_PROCESS_ROLES or launch_kind not in {
        LEGACY_DIRECT_LAUNCH_KIND,
        LEGACY_WRAPPER_LAUNCH_KIND,
    }:
        raise CapturedPaperHostCutoverError(
            "LEGACY_LAUNCH_CONTRACT_INVALID", f"{field_name} kind/role is unsupported"
        )
    cmdline_value = value.get("expected_cmdline")
    if (
        not isinstance(cmdline_value, list)
        or len(cmdline_value) != 2
        or any(not isinstance(item, str) or not item for item in cmdline_value)
    ):
        raise CapturedPaperHostCutoverError(
            "LEGACY_LAUNCH_CONTRACT_INVALID", f"{field_name} argv is not exact"
        )
    nullable = (
        "wrapper_path",
        "wrapper_sha256",
        "wrapper_semantic_profile",
        "powershell_path",
        "powershell_sha256",
        "starter_path",
        "starter_sha256",
        "starter_semantic_profile",
    )
    for key in nullable:
        if value.get(key) is not None and (
            not isinstance(value.get(key), str) or not str(value.get(key))
        ):
            raise CapturedPaperHostCutoverError(
                "LEGACY_LAUNCH_CONTRACT_INVALID", f"{field_name}.{key} is invalid"
            )
    contract = LegacyTaskLaunchContract(
        task_name=str(value.get("task_name") or ""),
        role=role,
        launch_kind=launch_kind,
        task_xml_sha256=_sha(value.get("task_xml_sha256"), f"{field_name}.task XML"),
        task_action_sha256=_sha(
            value.get("task_action_sha256"), f"{field_name}.task action"
        ),
        task_command=str(value.get("task_command") or ""),
        task_arguments=str(value.get("task_arguments") or ""),
        working_directory=str(value.get("working_directory") or ""),
        task_host_path=str(value.get("task_host_path") or ""),
        task_host_sha256=_sha(
            value.get("task_host_sha256"), f"{field_name}.task host"
        ),
        wrapper_path=value.get("wrapper_path"),
        wrapper_sha256=(
            _sha(value.get("wrapper_sha256"), f"{field_name}.wrapper")
            if value.get("wrapper_sha256") is not None
            else None
        ),
        wrapper_semantic_profile=value.get("wrapper_semantic_profile"),
        powershell_path=value.get("powershell_path"),
        powershell_sha256=(
            _sha(value.get("powershell_sha256"), f"{field_name}.PowerShell")
            if value.get("powershell_sha256") is not None
            else None
        ),
        starter_path=value.get("starter_path"),
        starter_sha256=(
            _sha(value.get("starter_sha256"), f"{field_name}.starter")
            if value.get("starter_sha256") is not None
            else None
        ),
        starter_semantic_profile=value.get("starter_semantic_profile"),
        expected_executable_path=str(value.get("expected_executable_path") or ""),
        expected_executable_sha256=_sha(
            value.get("expected_executable_sha256"), f"{field_name}.executable"
        ),
        expected_bridge_script_path=str(
            value.get("expected_bridge_script_path") or ""
        ),
        expected_bridge_script_sha256=_sha(
            value.get("expected_bridge_script_sha256"), f"{field_name}.bridge"
        ),
        expected_cmdline=tuple(cmdline_value),
        expected_cmdline_sha256=_sha(
            value.get("expected_cmdline_sha256"), f"{field_name}.argv"
        ),
        scheduler_principal_sha256=_sha(
            value.get("scheduler_principal_sha256"),
            f"{field_name}.scheduler principal",
        ),
        scheduler_settings_sha256=_sha(
            value.get("scheduler_settings_sha256"),
            f"{field_name}.scheduler settings",
        ),
        trigger_profile=str(value.get("trigger_profile") or ""),
        role_semantic_sha256=_sha(
            value.get("role_semantic_sha256"), f"{field_name}.role semantics"
        ),
        contract_sha256=_sha(
            value.get("contract_sha256"), f"{field_name}.contract"
        ),
    )
    if not (
        contract.expected_cmdline_sha256 == sha256_json(list(contract.expected_cmdline))
        and contract.role_semantic_sha256
        == sha256_json(_role_semantic_material(contract))
        and contract.contract_sha256
        == sha256_json(_launch_contract_payload(contract, include_contract_sha256=False))
    ):
        raise CapturedPaperHostCutoverError(
            "LEGACY_LAUNCH_CONTRACT_INVALID", f"{field_name} digest is inconsistent"
        )
    if verify_bound_files:
        _assert_launch_contract_sources_current(contract, roots=roots)
    return contract


def _assert_launch_contract_sources_current(
    contract: LegacyTaskLaunchContract, *, roots: Sequence[Path]
) -> None:
    if contract.launch_kind == LEGACY_DIRECT_LAUNCH_KIND:
        executable, executable_sha = _strict_bound_file(
            contract.expected_executable_path,
            expected_sha256=contract.expected_executable_sha256,
            roots=roots,
            field=f"{contract.task_name} direct executable",
        )
        script, script_sha = _strict_bound_file(
            contract.expected_bridge_script_path,
            expected_sha256=contract.expected_bridge_script_sha256,
            roots=roots,
            field=f"{contract.task_name} direct bridge",
        )
        if not (
            os.path.normcase(executable) == os.path.normcase(contract.task_host_path)
            and executable_sha == contract.task_host_sha256
            and contract.expected_cmdline == (executable, script)
        ):
            raise CapturedPaperHostCutoverError(
                "LEGACY_RESTORE_SOURCE_DRIFT", "direct restore source identity changed"
            )
        return
    if not (
        contract.launch_kind == LEGACY_WRAPPER_LAUNCH_KIND
        and contract.wrapper_path
        and contract.wrapper_sha256
        and contract.wrapper_semantic_profile == RUN_HIDDEN_SEMANTIC_PROFILE
        and contract.powershell_path
        and contract.powershell_sha256
        and contract.starter_path
        and contract.starter_sha256
        and contract.starter_semantic_profile
        == _LEGACY_ROLE_STARTER_PROFILE[contract.role]
        and contract.working_directory == ""
    ):
        raise CapturedPaperHostCutoverError(
            "LEGACY_LAUNCH_CONTRACT_INVALID", "wrapper restore contract is incomplete"
        )
    wscript, wscript_sha = _strict_system32_profile(
        contract.task_command, basename="wscript.exe"
    )
    argv = _windows_command_line_to_argv(contract.task_arguments)
    if (
        len(argv) != 7
        or tuple(value.casefold() for value in argv[2:6])
        != ("-noprofile", "-executionpolicy", "bypass", "-file")
        or os.path.normcase(argv[1]) != os.path.normcase(contract.powershell_path)
        or os.path.normcase(argv[0]) != os.path.normcase(contract.wrapper_path)
        or os.path.normcase(argv[6]) != os.path.normcase(contract.starter_path)
    ):
        raise CapturedPaperHostCutoverError(
            "LEGACY_LAUNCH_CONTRACT_INVALID", "wrapper restore argv changed"
        )
    powershell, powershell_sha = _strict_system32_profile(
        argv[1], basename="powershell.exe"
    )
    wrapper, wrapper_raw, wrapper_sha = _stable_read(
        contract.wrapper_path,
        roots=roots,
        field=f"{contract.task_name} wrapper",
        expected_sha256=contract.wrapper_sha256,
    )
    starter, starter_raw, starter_sha = _stable_read(
        contract.starter_path,
        roots=roots,
        field=f"{contract.task_name} starter",
        expected_sha256=contract.starter_sha256,
    )
    executable, executable_sha = _strict_bound_file(
        contract.expected_executable_path,
        expected_sha256=contract.expected_executable_sha256,
        roots=roots,
        field=f"{contract.task_name} executable",
    )
    bridge, bridge_raw, bridge_sha = _stable_read(
        contract.expected_bridge_script_path,
        roots=roots,
        field=f"{contract.task_name} bridge",
        expected_sha256=contract.expected_bridge_script_sha256,
    )
    del bridge_raw
    _validate_run_hidden_semantics(wrapper_raw)
    profile = _validate_starter_semantics(
        starter_raw,
        role=contract.role,
        expected_executable_path=executable,
        expected_bridge_script_path=str(bridge),
    )
    if not (
        os.path.normcase(wscript) == os.path.normcase(contract.task_host_path)
        and wscript_sha == contract.task_host_sha256
        and os.path.normcase(powershell) == os.path.normcase(contract.powershell_path)
        and powershell_sha == contract.powershell_sha256
        and os.path.normcase(str(wrapper)) == os.path.normcase(contract.wrapper_path)
        and wrapper_sha == contract.wrapper_sha256
        and os.path.normcase(str(starter)) == os.path.normcase(contract.starter_path)
        and starter_sha == contract.starter_sha256
        and executable_sha == contract.expected_executable_sha256
        and bridge_sha == contract.expected_bridge_script_sha256
        and contract.expected_cmdline == (executable, str(bridge))
        and profile == contract.starter_semantic_profile
    ):
        raise CapturedPaperHostCutoverError(
            "LEGACY_RESTORE_SOURCE_DRIFT", "wrapper restore source identity changed"
        )


def _parse_restore_plan(
    *,
    path: Path,
    raw: bytes,
    digest: str,
    roots: Sequence[Path],
    verify_bound_files: bool = True,
) -> RestorePlan:
    document = _strict_json(raw, "restore_plan")
    if raw != _canonical_json_bytes(document):
        raise CapturedPaperHostCutoverError(
            "ARTIFACT_NOT_CANONICAL", "restore plan is not canonical JSON"
        )
    _exact_keys(
        document,
        {
            "schema_version",
            "task_enabled_states",
            "restart_tasks",
            "legacy_task_launch_contracts",
            "legacy_process_bindings",
            "candidate_task_name",
        },
        "restore_plan",
    )
    if document.get("schema_version") != RESTORE_PLAN_SCHEMA:
        raise CapturedPaperHostCutoverError(
            "ARTIFACT_SCHEMA_MISMATCH", "restore plan schema is unsupported"
        )
    task_states = _mapping(document.get("task_enabled_states"), "task_enabled_states")
    _exact_keys(task_states, REQUIRED_LEGACY_TASKS, "restore task states")
    normalized_states: dict[str, bool] = {}
    for name in REQUIRED_LEGACY_TASKS:
        enabled = task_states.get(name)
        if not isinstance(enabled, bool):
            raise CapturedPaperHostCutoverError(
                "RESTORE_PLAN_INVALID", f"restore state for {name} is not Boolean"
            )
        normalized_states[name] = enabled
    restart_tasks = document.get("restart_tasks")
    if (
        not isinstance(restart_tasks, list)
        or len(restart_tasks) != len(set(restart_tasks))
        or any(name not in REQUIRED_LEGACY_TASKS for name in restart_tasks)
    ):
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_INVALID", "restart task roster is not exact"
        )
    contracts_value = _mapping(
        document.get("legacy_task_launch_contracts"),
        "legacy_task_launch_contracts",
    )
    _exact_keys(
        contracts_value, REQUIRED_LEGACY_TASKS, "legacy_task_launch_contracts"
    )
    contracts: dict[str, LegacyTaskLaunchContract] = {}
    for name in REQUIRED_LEGACY_TASKS:
        contract = _parse_launch_contract(
            _mapping(contracts_value.get(name), f"launch contract {name}"),
            roots=roots,
            field_name=f"launch contract {name}",
            verify_bound_files=verify_bound_files,
        )
        if contract.task_name != name or contract.role != _LEGACY_TASK_ROLE[name]:
            raise CapturedPaperHostCutoverError(
                "LEGACY_LAUNCH_CONTRACT_INVALID",
                f"launch contract {name} is bound to another task/role",
            )
        contracts[name] = contract
    _assert_launch_contract_roster(contracts)
    bindings_value = document.get("legacy_process_bindings")
    if not isinstance(bindings_value, list):
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_INVALID", "legacy process bindings must be an array"
        )
    bindings: list[LegacyProcessBinding] = []
    for index, item_value in enumerate(bindings_value):
        item = _mapping(item_value, f"legacy_process_bindings[{index}]")
        _exact_keys(
            item,
            {
                "role",
                "executable_path",
                "executable_sha256",
                "bridge_script_path",
                "bridge_script_sha256",
                "restore_task",
                "restore_task_xml_sha256",
                "restore_task_action_sha256",
                "expected_cmdline",
                "expected_cmdline_sha256",
            },
            f"legacy_process_bindings[{index}]",
        )
        role = str(item.get("role") or "").strip()
        if role not in {"iqfeed_trade_bridge", "iqfeed_depth_bridge"}:
            raise CapturedPaperHostCutoverError(
                "RESTORE_PLAN_INVALID", "restore binding role is unsupported"
            )
        restore_task = str(item.get("restore_task") or "")
        if restore_task not in restart_tasks:
            raise CapturedPaperHostCutoverError(
                "RESTORE_PLAN_INVALID", "restore binding task is not in restart roster"
            )
        if verify_bound_files:
            executable, executable_sha = _strict_bound_file(
                item.get("executable_path"),
                expected_sha256=item.get("executable_sha256"),
                roots=roots,
                field=f"restore binding {role}.executable",
            )
            script, script_sha = _strict_bound_file(
                item.get("bridge_script_path"),
                expected_sha256=item.get("bridge_script_sha256"),
                roots=roots,
                field=f"restore binding {role}.bridge_script",
            )
        else:
            executable = str(
                _sealed_capsule_path(
                    str(item.get("executable_path") or ""),
                    roots=roots,
                    field=f"restore binding {role}.executable",
                )
            )
            executable_sha = _sha(
                item.get("executable_sha256"),
                f"restore binding {role}.executable_sha256",
            )
            script = str(
                _sealed_capsule_path(
                    str(item.get("bridge_script_path") or ""),
                    roots=roots,
                    field=f"restore binding {role}.bridge_script",
                )
            )
            script_sha = _sha(
                item.get("bridge_script_sha256"),
                f"restore binding {role}.bridge_script_sha256",
            )
        expected_cmdline_value = item.get("expected_cmdline")
        if (
            not isinstance(expected_cmdline_value, list)
            or not expected_cmdline_value
            or any(not isinstance(value, str) or not value for value in expected_cmdline_value)
        ):
            raise CapturedPaperHostCutoverError(
                "RESTORE_PLAN_INVALID",
                f"restore binding {role} expected_cmdline is invalid",
            )
        expected_cmdline = tuple(expected_cmdline_value)
        expected_cmdline_sha = _sha(
            item.get("expected_cmdline_sha256"),
            f"restore binding {role}.expected_cmdline_sha256",
        )
        if sha256_json(list(expected_cmdline)) != expected_cmdline_sha:
            raise CapturedPaperHostCutoverError(
                "RESTORE_PLAN_INVALID",
                f"restore binding {role} command line hash is inconsistent",
            )
        bindings.append(
            LegacyProcessBinding(
                role=role,
                executable_path=executable,
                executable_sha256=executable_sha,
                bridge_script_path=script,
                bridge_script_sha256=script_sha,
                restore_task=restore_task,
                restore_task_xml_sha256=_sha(
                    item.get("restore_task_xml_sha256"),
                    f"restore binding {role}.restore_task_xml_sha256",
                ),
                restore_task_action_sha256=_sha(
                    item.get("restore_task_action_sha256"),
                    f"restore binding {role}.restore_task_action_sha256",
                ),
                expected_cmdline=expected_cmdline,
                expected_cmdline_sha256=expected_cmdline_sha,
            )
        )
    if (
        len({item.role for item in bindings}) != len(bindings)
        or {item.role for item in bindings} != set(REQUIRED_LEGACY_PROCESS_ROLES)
    ):
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_INVALID",
            "restore plan must bind exactly one trade and one depth role",
        )
    if (
        set(restart_tasks) != {item.restore_task for item in bindings}
        or any(
            contracts[item.restore_task].role != item.role
            or normalized_states[item.restore_task] is not True
            for item in bindings
        )
    ):
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_INVALID",
            "restart tasks do not select the exact role launch contracts",
        )
    if document.get("candidate_task_name") != CANDIDATE_TASK_NAME:
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_INVALID", "restore plan candidate task name is not exact"
        )
    return RestorePlan(
        task_enabled_states=MappingProxyType(normalized_states),
        restart_tasks=tuple(restart_tasks),
        bindings=tuple(sorted(bindings, key=lambda item: item.role)),
        candidate_task_name=CANDIDATE_TASK_NAME,
        artifact_path=path,
        artifact_sha256=digest,
        launch_contracts=MappingProxyType(contracts),
    )


def _assert_snapshot_plan_consistency(
    task_snapshot: TaskSnapshot,
    process_snapshot: ProcessSnapshot,
    restore_plan: RestorePlan,
) -> None:
    for name in REQUIRED_LEGACY_TASKS:
        if (
            task_snapshot.tasks[name].enabled
            is not restore_plan.task_enabled_states[name]
        ):
            raise CapturedPaperHostCutoverError(
                "RESTORE_PLAN_MISMATCH", f"restore state for {name} differs from snapshot"
            )
    try:
        contracts = (
            restore_plan.launch_contracts
            or _derive_direct_launch_contracts(
                tasks=task_snapshot.tasks, bindings=restore_plan.bindings
            )
        )
    except CapturedPaperHostCutoverError as exc:
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_MISMATCH", "restore launch contracts are inconsistent"
        ) from exc
    _assert_launch_contract_roster(contracts)
    for name in REQUIRED_LEGACY_TASKS:
        task = task_snapshot.tasks[name]
        contract = contracts[name]
        projection = _task_exec_projection_from_xml(task.xml)
        if not (
            contract.task_name == name
            and contract.role == _LEGACY_TASK_ROLE[name]
            and contract.task_xml_sha256 == task.xml_sha256
            and contract.task_action_sha256 == _task_action_sha256(task.xml)
            and contract.task_command == projection["command"]
            and contract.task_arguments == projection["arguments"]
            and contract.working_directory == projection["working_directory"]
        ):
            raise CapturedPaperHostCutoverError(
                "RESTORE_PLAN_MISMATCH",
                f"launch contract for {name} differs from its task snapshot",
            )
    bindings = {item.role: item for item in restore_plan.bindings}
    if set(bindings) != {item.role for item in process_snapshot.processes}:
        raise CapturedPaperHostCutoverError(
            "RESTORE_PLAN_MISMATCH", "restore bindings do not match active process roles"
        )
    for process in process_snapshot.processes:
        binding = bindings[process.role]
        restore_task = task_snapshot.tasks[binding.restore_task]
        contract = contracts[binding.restore_task]
        if (
            os.path.normcase(process.executable_path)
            != os.path.normcase(binding.executable_path)
            or process.executable_sha256 != binding.executable_sha256
            or os.path.normcase(process.bridge_script_path or "")
            != os.path.normcase(binding.bridge_script_path)
            or process.bridge_script_sha256 != binding.bridge_script_sha256
            or process.cmdline != binding.expected_cmdline
            or process.cmdline_sha256 != binding.expected_cmdline_sha256
            or restore_task.xml_sha256 != binding.restore_task_xml_sha256
            or _task_action_sha256(restore_task.xml)
            != binding.restore_task_action_sha256
            or contract.role != binding.role
            or os.path.normcase(contract.expected_executable_path)
            != os.path.normcase(binding.executable_path)
            or contract.expected_executable_sha256 != binding.executable_sha256
            or os.path.normcase(contract.expected_bridge_script_path)
            != os.path.normcase(binding.bridge_script_path)
            or contract.expected_bridge_script_sha256
            != binding.bridge_script_sha256
            or contract.expected_cmdline != binding.expected_cmdline
            or contract.expected_cmdline_sha256 != binding.expected_cmdline_sha256
            or os.path.normcase(binding.expected_cmdline[0])
            != os.path.normcase(binding.executable_path)
            or not any(
                os.path.normcase(value) == os.path.normcase(binding.bridge_script_path)
                for value in binding.expected_cmdline
            )
        ):
            raise CapturedPaperHostCutoverError(
                "RESTORE_PLAN_MISMATCH",
                f"restore binding for {process.role} differs from process snapshot",
            )


def build_preactivation_rollback_baseline_document(
    baseline: PreActivationRollbackBaseline,
) -> Mapping[str, Any]:
    """Return the canonical bytes bound by the preactivation baseline hash."""

    context = baseline.context
    return {
        "schema_version": PREACTIVATION_ROLLBACK_BASELINE_SCHEMA,
        "validation_mode": PREACTIVATION_ROLLBACK_BASELINE_MODE,
        "verdict": "VALIDATED_READ_ONLY_LOCAL_ARTIFACTS",
        "activation_generation": context.activation_generation,
        "expected_account_id": context.expected_account_id,
        "account_scope": "alpaca:paper",
        "candidate_root": str(context.candidate_root),
        "allowed_read_roots": [str(item) for item in context.allowed_read_roots],
        "host_cutover_source_sha256": context.host_cutover_source_sha256,
        "launcher_argument_contract_sha256": (
            context.launcher_argument_contract_sha256
        ),
        "task_snapshot_sha256": baseline.task_snapshot.artifact_sha256,
        "legacy_process_snapshot_sha256": (
            baseline.process_snapshot.artifact_sha256
        ),
        "restore_plan_sha256": baseline.restore_plan.artifact_sha256,
        "candidate_task_xml_sha256": baseline.candidate_template_sha256,
        "candidate_action_sha256": baseline.candidate_action_sha256,
        "validated_at": _iso(baseline.validated_at),
        "host_mutation_count": 0,
        "final_validate_only_performed": False,
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }


def _canonical_uuid(value: Any, field: str) -> str:
    try:
        parsed = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CapturedPaperHostCutoverError(
            "PREACTIVATION_CONTEXT_INVALID", f"{field} is not a UUID"
        ) from exc
    if parsed != str(value):
        raise CapturedPaperHostCutoverError(
            "PREACTIVATION_CONTEXT_INVALID", f"{field} is not canonical"
        )
    return parsed


def prepare_preactivation_rollback_baseline(
    context: PreActivationRollbackContext,
    *,
    task_snapshot_path: str | Path,
    process_snapshot_path: str | Path,
    restore_plan_path: str | Path,
    candidate_task_template_path: str | Path,
    candidate_action_path: str | Path,
    validated_at: datetime,
) -> PreActivationRollbackBaseline:
    """Validate rollback material without final activation or host mutation.

    This is intentionally weaker than :meth:`CapturedPaperHostCutoverExecutor
    .validate_only`: it validates local, content-addressed inputs and their
    internal consistency but does not observe Task Scheduler or running
    processes.  Final activation reconstructs this exact baseline and the
    real executor performs ValidateOnly again immediately before Apply.
    """

    if type(context) is not PreActivationRollbackContext:
        raise CapturedPaperHostCutoverError(
            "PREACTIVATION_CONTEXT_INVALID", "rollback context is not typed"
        )
    generation = _canonical_uuid(
        context.activation_generation, "activation_generation"
    )
    account_id = _canonical_uuid(context.expected_account_id, "expected_account_id")
    roots = _strict_roots(context.allowed_read_roots)
    candidate_root = _strict_existing_dir(
        context.candidate_root, roots=roots, field="candidate_root"
    )
    executor_root = Path(__file__).resolve(strict=True).parents[1]
    if candidate_root != executor_root:
        raise CapturedPaperHostCutoverError(
            "EXECUTOR_ROOT_MISMATCH",
            "preactivation rollback baseline is not running from candidate root",
        )
    executor_path, _executor_raw, executor_sha = _stable_read(
        __file__, roots=roots, field="captured_paper_host_cutover"
    )
    expected_executor_sha = _sha(
        context.host_cutover_source_sha256,
        "preactivation host_cutover_source_sha256",
    )
    launcher_contract_sha = _sha(
        context.launcher_argument_contract_sha256,
        "preactivation launcher_argument_contract_sha256",
    )
    if executor_path != Path(__file__).resolve() or executor_sha != expected_executor_sha:
        raise CapturedPaperHostCutoverError(
            "EXECUTOR_HASH_MISMATCH",
            "preactivation rollback baseline is not bound to running host code",
        )
    validated = _parse_utc(_iso(validated_at), "validated_at")

    task_path, task_raw, task_digest = _stable_read(
        task_snapshot_path, roots=roots, field="task_snapshot"
    )
    task_document = _strict_json(task_raw, "task_snapshot")
    task_values = _mapping(task_document.get("tasks"), "task_snapshot.tasks")
    _exact_keys(task_values, REQUIRED_LEGACY_TASKS, "task_snapshot.tasks")
    task_receipt_hashes = {
        name: _sha(
            _mapping(task_values.get(name), f"task_snapshot.tasks.{name}").get(
                "xml_sha256"
            ),
            f"task_snapshot.tasks.{name}.xml_sha256",
        )
        for name in REQUIRED_LEGACY_TASKS
    }
    task_snapshot = _parse_task_snapshot(
        path=task_path,
        raw=task_raw,
        digest=task_digest,
        receipt_evidence={"scheduled_task_xml_sha256s": task_receipt_hashes},
    )
    process_path, process_raw, process_digest = _stable_read(
        process_snapshot_path, roots=roots, field="process_snapshot"
    )
    process_snapshot = _parse_process_snapshot(
        path=process_path,
        raw=process_raw,
        digest=process_digest,
        roots=roots,
    )
    restore_path, restore_raw, restore_digest = _stable_read(
        restore_plan_path, roots=roots, field="restore_plan"
    )
    restore_plan = _parse_restore_plan(
        path=restore_path,
        raw=restore_raw,
        digest=restore_digest,
        roots=roots,
    )
    _assert_snapshot_plan_consistency(task_snapshot, process_snapshot, restore_plan)

    template_path, template_raw, template_sha = _stable_read(
        candidate_task_template_path,
        roots=roots,
        field="candidate_task_xml",
    )
    # Token counts on decoded text — production templates are UTF-16 (see
    # _materialize_candidate_xml), where a UTF-8 byte-pattern count is
    # always zero.
    template_text = template_raw.decode(
        "utf-16" if template_raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    )
    if (
        template_text.count(MANIFEST_PATH_TOKEN) != 1
        or template_text.count(MANIFEST_SHA256_TOKEN) != 1
        or _task_enabled_from_xml(template_raw) is not True
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_TOKEN_MISMATCH",
            "preactivation task template is not exact or enabled",
        )
    command, _arguments = _task_exec_from_xml(template_raw)
    _resolve_system_executable(command, "candidate task PowerShell executable")
    _validate_candidate_task_semantics(
        template_raw, candidate_root=str(candidate_root)
    )

    action_path, action_raw, action_sha = _stable_read(
        candidate_action_path, roots=roots, field="candidate_action"
    )
    action = _strict_json(action_raw, "candidate_action")
    if action_raw != _canonical_json_bytes(action):
        raise CapturedPaperHostCutoverError(
            "ARTIFACT_NOT_CANONICAL", "candidate action is not canonical JSON"
        )
    expected_action = build_candidate_action_document(
        host_cutover_source_sha256=executor_sha,
        launcher_argument_contract_sha256=launcher_contract_sha,
        candidate_task_xml_sha256=template_sha,
    )
    if dict(action) != dict(expected_action) or action_sha != sha256_json(expected_action):
        raise CapturedPaperHostCutoverError(
            "CANDIDATE_ACTION_HASH_MISMATCH",
            "preactivation candidate action is not exactly context-bound",
        )
    for captured, field in (
        (task_snapshot.captured_at, "task_snapshot.captured_at"),
        (process_snapshot.captured_at, "process_snapshot.captured_at"),
    ):
        age = (validated - captured).total_seconds()
        if age < 0 or age > 3600:
            raise CapturedPaperHostCutoverError(
                "PREACTIVATION_BASELINE_STALE",
                f"{field} is future-dated or too old",
            )

    normalized_context = PreActivationRollbackContext(
        activation_generation=generation,
        expected_account_id=account_id,
        candidate_root=candidate_root,
        allowed_read_roots=roots,
        host_cutover_source_sha256=executor_sha,
        launcher_argument_contract_sha256=launcher_contract_sha,
    )
    provisional = PreActivationRollbackBaseline(
        context=normalized_context,
        task_snapshot=task_snapshot,
        process_snapshot=process_snapshot,
        restore_plan=restore_plan,
        candidate_action_path=action_path,
        candidate_action_sha256=action_sha,
        candidate_template_path=template_path,
        candidate_template_sha256=template_sha,
        validated_at=validated,
        baseline_sha256="0" * 64,
    )
    digest = sha256_json(build_preactivation_rollback_baseline_document(provisional))
    return PreActivationRollbackBaseline(
        context=provisional.context,
        task_snapshot=provisional.task_snapshot,
        process_snapshot=provisional.process_snapshot,
        restore_plan=provisional.restore_plan,
        candidate_action_path=provisional.candidate_action_path,
        candidate_action_sha256=provisional.candidate_action_sha256,
        candidate_template_path=provisional.candidate_template_path,
        candidate_template_sha256=provisional.candidate_template_sha256,
        validated_at=provisional.validated_at,
        baseline_sha256=digest,
    )


def _parse_rollback_capsule(
    *,
    path: Path,
    raw: bytes,
    digest: str,
    caller_roots: Sequence[Path],
    expected_generation: str,
    expected_manifest_sha256: str,
) -> PreparedCutover:
    document = _strict_json(raw, "rollback capsule")
    if raw != _canonical_json_bytes(document):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CAPSULE_INVALID", "rollback capsule is not canonical JSON"
        )
    _exact_keys(
        document,
        {
            "schema_version", "activation_generation", "expected_account_id",
            "manifest_path", "manifest_sha256", "candidate_root",
            "allowed_read_roots", "task_snapshot", "process_snapshot",
            "restore_plan", "resolved_task_xml_base64",
            "resolved_task_xml_sha256", "candidate_invocation",
            "rollback_receipt_sha256", "account_scope", "live_cash_authorized",
        },
        "rollback capsule",
    )
    generation = str(document.get("activation_generation") or "")
    try:
        generation = str(uuid.UUID(generation))
    except ValueError as exc:
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CAPSULE_INVALID", "rollback capsule generation is invalid"
        ) from exc
    manifest_sha = _sha(document.get("manifest_sha256"), "capsule manifest_sha256")
    if (
        document.get("schema_version") != ROLLBACK_CAPSULE_SCHEMA
        or generation != expected_generation
        or manifest_sha != _sha(expected_manifest_sha256, "expected manifest_sha256")
        or document.get("account_scope") != "alpaca:paper"
        or document.get("live_cash_authorized") is not False
    ):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CAPSULE_INVALID", "rollback capsule escaped its sealed PAPER identity"
        )
    roots_value = document.get("allowed_read_roots")
    if not isinstance(roots_value, list) or any(
        not isinstance(item, str) or not item for item in roots_value
    ):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CAPSULE_INVALID", "rollback capsule read roots are invalid"
        )
    capsule_roots = _strict_roots(roots_value)
    if any(not _inside(root, caller_roots) for root in capsule_roots):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CAPSULE_INVALID", "rollback capsule expands caller read authority"
        )
    candidate_root = _sealed_capsule_path(
        str(document.get("candidate_root") or ""),
        roots=capsule_roots,
        field="capsule candidate_root",
    )
    task_document = _mapping(document.get("task_snapshot"), "capsule task_snapshot")
    process_document = _mapping(
        document.get("process_snapshot"), "capsule process_snapshot"
    )
    restore_document = _mapping(document.get("restore_plan"), "capsule restore_plan")
    task_raw = _canonical_json_bytes(task_document)
    process_raw = _canonical_json_bytes(process_document)
    restore_raw = _canonical_json_bytes(restore_document)
    tasks_value = _mapping(task_document.get("tasks"), "capsule task_snapshot.tasks")
    task_hashes = {
        name: _mapping(tasks_value.get(name), f"capsule task {name}").get("xml_sha256")
        for name in REQUIRED_LEGACY_TASKS
    }
    task_snapshot = _parse_task_snapshot(
        path=path,
        raw=task_raw,
        digest=sha256_bytes(task_raw),
        receipt_evidence={"scheduled_task_xml_sha256s": task_hashes},
    )
    process_snapshot = _parse_process_snapshot(
        path=path,
        raw=process_raw,
        digest=sha256_bytes(process_raw),
        roots=capsule_roots,
        verify_bound_files=False,
    )
    restore_plan = _parse_restore_plan(
        path=path,
        raw=restore_raw,
        digest=sha256_bytes(restore_raw),
        roots=capsule_roots,
        verify_bound_files=False,
    )
    _assert_snapshot_plan_consistency(task_snapshot, process_snapshot, restore_plan)
    resolved = _decode_b64(
        document.get("resolved_task_xml_base64"), "capsule resolved task XML"
    )
    resolved_sha = _sha(
        document.get("resolved_task_xml_sha256"), "capsule resolved task XML hash"
    )
    if sha256_bytes(resolved) != resolved_sha or _task_enabled_from_xml(resolved) is not True:
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CAPSULE_INVALID", "capsule candidate task bytes are inconsistent"
        )
    invocation_value = _mapping(
        document.get("candidate_invocation"), "capsule candidate_invocation"
    )
    _exact_keys(
        invocation_value,
        {
            "task_name", "powershell_executable_path",
            "powershell_executable_sha256", "launcher_script_path",
            "launcher_script_sha256", "launcher_source_path",
            "launcher_source_sha256", "stage0_source_path",
            "stage0_source_sha256", "stage0_script_path",
            "stage0_script_sha256", "service_source_path",
            "service_source_sha256", "service_script_path",
            "service_script_sha256", "host_ready_receipt_base", "launcher_arguments",
            "python_executable_path", "python_executable_sha256",
            "python_dependency_root", "python_dependency_root_identity_sha256",
            "service_arguments",
        },
        "capsule candidate_invocation",
    )
    if invocation_value.get("task_name") != CANDIDATE_TASK_NAME:
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CAPSULE_INVALID", "capsule candidate task name differs"
        )
    invocation = CandidateInvocation(
        task_name=CANDIDATE_TASK_NAME,
        powershell_executable_path=str(invocation_value.get("powershell_executable_path") or ""),
        powershell_executable_sha256=_sha(
            invocation_value.get("powershell_executable_sha256"),
            "capsule PowerShell hash",
        ),
        launcher_source_path=str(invocation_value.get("launcher_source_path") or ""),
        launcher_source_sha256=_sha(
            invocation_value.get("launcher_source_sha256"), "capsule launcher source hash"
        ),
        launcher_script_path=str(invocation_value.get("launcher_script_path") or ""),
        launcher_script_sha256=_sha(
            invocation_value.get("launcher_script_sha256"), "capsule launcher hash"
        ),
        stage0_source_path=str(invocation_value.get("stage0_source_path") or ""),
        stage0_source_sha256=_sha(
            invocation_value.get("stage0_source_sha256"), "capsule stage0 source hash"
        ),
        stage0_script_path=str(invocation_value.get("stage0_script_path") or ""),
        stage0_script_sha256=_sha(
            invocation_value.get("stage0_script_sha256"), "capsule staged stage0 hash"
        ),
        service_source_path=str(invocation_value.get("service_source_path") or ""),
        service_source_sha256=_sha(
            invocation_value.get("service_source_sha256"), "capsule service source hash"
        ),
        service_script_path=str(invocation_value.get("service_script_path") or ""),
        service_script_sha256=_sha(
            invocation_value.get("service_script_sha256"), "capsule staged service hash"
        ),
        host_ready_receipt_base=str(
            invocation_value.get("host_ready_receipt_base") or ""
        ),
        launcher_arguments=_parse_string_tuple(
            invocation_value.get("launcher_arguments"), "capsule launcher arguments"
        ),
        python_executable_path=str(invocation_value.get("python_executable_path") or ""),
        python_executable_sha256=_sha(
            invocation_value.get("python_executable_sha256"), "capsule Python hash"
        ),
        python_dependency_root=str(
            invocation_value.get("python_dependency_root") or ""
        ),
        python_dependency_root_identity_sha256=_sha(
            invocation_value.get("python_dependency_root_identity_sha256"),
            "capsule Python dependency root identity",
        ),
        service_arguments=_parse_string_tuple(
            invocation_value.get("service_arguments"), "capsule service arguments"
        ),
    )
    command, arguments = _task_exec_from_xml(resolved)
    if (
        os.path.normcase(command)
        != os.path.normcase(invocation.powershell_executable_path)
        # Case-insensitive: capsule XML carries normcased template tokens
        # while the reconstructed invocation may be filesystem proper case
        # (2026-07-17: this exact compare made every live rollback fail with
        # ROLLBACK_CAPSULE_INVALID).
        or os.path.normcase(arguments)
        != os.path.normcase(_quote_windows_arguments(invocation.launcher_arguments))
    ):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CAPSULE_INVALID", "capsule task and invocation are not identical"
        )
    return PreparedCutover(
        activation_generation=generation,
        expected_account_id=str(document.get("expected_account_id") or ""),
        manifest_path=Path(str(document.get("manifest_path") or "")),
        manifest_sha256=manifest_sha,
        candidate_root=candidate_root,
        allowed_read_roots=capsule_roots,
        task_snapshot=task_snapshot,
        process_snapshot=process_snapshot,
        restore_plan=restore_plan,
        candidate_action_path=path,
        candidate_action_sha256=digest,
        candidate_template_path=path,
        candidate_template_sha256=digest,
        resolved_task_xml=resolved,
        resolved_task_xml_sha256=resolved_sha,
        invocation=invocation,
        rollback_receipt_sha256=_sha(
            document.get("rollback_receipt_sha256"), "capsule rollback receipt hash"
        ),
    )


def _resolve_system_executable(value: str, field: str) -> tuple[Path, str]:
    return _stable_local_file_unrooted(value, field=field)


def _one_child(parent: ET.Element, tag: str, field: str) -> ET.Element:
    values = [item for item in list(parent) if item.tag == f"{{{_TASK_NS}}}{tag}"]
    if len(values) != 1:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", f"candidate task must have one {field}"
        )
    return values[0]


def _validate_candidate_task_semantics(raw: bytes, *, candidate_root: str) -> None:
    """Require the exact safe Task Scheduler policy, not merely one Exec node."""

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CapturedPaperHostCutoverError(
            "TASK_XML_INVALID", "candidate task XML is malformed"
        ) from exc
    if root.tag != f"{{{_TASK_NS}}}Task" or root.attrib != {"version": "1.4"}:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate task root/version is not exact"
        )
    expected_root_children = [
        "RegistrationInfo", "Triggers", "Principals", "Settings", "Actions"
    ]
    if [item.tag.rsplit("}", 1)[-1] for item in list(root)] != expected_root_children:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate task sections are not exact"
        )
    registration = _one_child(root, "RegistrationInfo", "RegistrationInfo")
    description = _one_child(registration, "Description", "Description")
    if list(registration) != [description] or (description.text or "").strip() != (
        "One hash-bound captured Alpaca PAPER host; no live-cash authority."
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate description is not exact"
        )
    triggers = _one_child(root, "Triggers", "Triggers")
    if list(triggers) or triggers.attrib:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID",
            "candidate task must be on-demand-only with no automatic triggers",
        )
    principals = _one_child(root, "Principals", "Principals")
    principal = _one_child(principals, "Principal", "Principal")
    if list(principals) != [principal] or principal.attrib != {"id": "Author"}:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate principal roster is not exact"
        )
    principal_values = {
        item.tag.rsplit("}", 1)[-1]: (item.text or "").strip()
        for item in list(principal)
    }
    if (
        [item.tag.rsplit("}", 1)[-1] for item in list(principal)]
        != ["UserId", "LogonType", "RunLevel"]
        or not principal_values.get("UserId")
        or principal_values.get("LogonType") != "InteractiveToken"
        or principal_values.get("RunLevel") != "HighestAvailable"
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate principal policy is not exact"
        )
    settings = _one_child(root, "Settings", "Settings")
    if settings.attrib:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate settings attributes are not allowed"
        )
    scalar_settings = {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "AllowHardTerminate": "true",
        "StartWhenAvailable": "true",
        "RunOnlyIfNetworkAvailable": "false",
        "AllowStartOnDemand": "true",
        "Enabled": "true",
        "Hidden": "false",
        "RunOnlyIfIdle": "false",
        "WakeToRun": "false",
        "ExecutionTimeLimit": "PT0S",
        "Priority": "7",
    }
    actual_scalars = {
        item.tag.rsplit("}", 1)[-1]: (item.text or "").strip()
        for item in list(settings)
        if item.tag.rsplit("}", 1)[-1] != "IdleSettings"
    }
    if actual_scalars != scalar_settings:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate task settings are not exact"
        )
    idle = _one_child(settings, "IdleSettings", "IdleSettings")
    if {
        item.tag.rsplit("}", 1)[-1]: (item.text or "").strip()
        for item in list(idle)
    } != {"StopOnIdleEnd": "false", "RestartOnIdle": "false"}:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate nested settings are not exact"
        )
    if len(list(settings)) != len(scalar_settings) + 1:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate settings contain extras"
        )
    actions = _one_child(root, "Actions", "Actions")
    if actions.attrib != {"Context": "Author"}:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate action context is not exact"
        )
    exec_action = _one_child(actions, "Exec", "Exec")
    if (
        list(actions) != [exec_action]
        or exec_action.attrib
        or [item.tag.rsplit("}", 1)[-1] for item in list(exec_action)]
        != ["Command", "Arguments", "WorkingDirectory"]
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate Exec projection is not exact"
        )
    projection = _task_exec_projection_from_xml(raw)
    # The launcher projection stores normcased paths while ValidateOnly/Apply
    # callers pass the resolved (proper-case) candidate root; Windows path
    # identity is case-insensitive, so compare like _strict_system32_profile.
    if os.path.normcase(projection["working_directory"]) != os.path.normcase(
        candidate_root
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_SEMANTICS_INVALID", "candidate working directory is not exact"
        )


def _validate_candidate_template(
    *,
    template: bytes,
    projection: Mapping[str, Any],
    manifest_path: Path,
    manifest_sha256: str,
) -> tuple[bytes, CandidateInvocation]:
    if _task_enabled_from_xml(template) is not True:
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_DISABLED", "candidate task template must be enabled"
        )
    command, arguments = _task_exec_from_xml(template)
    powershell_path, powershell_sha = _resolve_system_executable(
        command, "candidate task PowerShell executable"
    )
    candidate_root = str(projection.get("candidate_root") or "")
    launcher_source_path = str(projection.get("launcher_source_path") or "")
    launcher_path = str(projection.get("launcher_path") or "")
    stage0_source_path = str(projection.get("stage0_source_path") or "")
    stage0_path = str(projection.get("stage0_path") or "")
    service_source_path = str(projection.get("service_source_path") or "")
    service_path = str(projection.get("service_staged_path") or "")
    python_path = str(projection.get("python_executable_path") or "")
    read_roots = projection.get("allowed_read_roots")
    if (
        projection.get("mode") != "ActivatePaper"
        or projection.get("service_mode") != "activate-paper"
        or projection.get("foreground") is not True
        or projection.get("singleton_name")
        != "Global\\CHILI-Captured-Alpaca-PAPER-SINGLETON"
        or not isinstance(read_roots, list)
        or not read_roots
        or any(not isinstance(item, str) or not item for item in read_roots)
    ):
        raise CapturedPaperHostCutoverError(
            "INVOCATION_PROJECTION_INVALID",
            "ActivatePaper launcher projection is not exact",
        )
    projection_roots = _strict_roots(read_roots)
    launcher_source, _launcher_raw, launcher_sha = _stable_read(
        launcher_source_path,
        roots=projection_roots,
        field="candidate launcher source",
        expected_sha256=projection.get("launcher_source_sha256"),
    )
    projected_launcher_sha = _sha(
        projection.get("launcher_sha256"), "launcher projection staged launcher_sha256"
    )
    if launcher_sha != projected_launcher_sha:
        raise CapturedPaperHostCutoverError(
            "LAUNCHER_BINDING_MISMATCH", "candidate launcher bytes differ from projection"
        )
    launcher_target = _sealed_capsule_path(
        launcher_path, roots=projection_roots, field="staged launcher target"
    )
    if launcher_target.name.casefold() != f"{launcher_sha}.ps1".casefold():
        raise CapturedPaperHostCutoverError(
            "LAUNCHER_NOT_CONTENT_ADDRESSED",
            "candidate task must execute a content-addressed launcher copy",
        )
    if launcher_target.exists() and _stable_read(
        launcher_target,
        roots=projection_roots,
        field="pre-staged candidate launcher",
        expected_sha256=launcher_sha,
    )[2] != launcher_sha:
        raise CapturedPaperHostCutoverError(
            "LAUNCHER_BINDING_MISMATCH", "pre-staged launcher bytes differ"
        )
    stage0_source, _stage0_raw, stage0_sha = _stable_read(
        stage0_source_path,
        roots=projection_roots,
        field="candidate stage0 source",
        expected_sha256=projection.get("stage0_source_sha256"),
    )
    projected_stage0_sha = _sha(
        projection.get("stage0_sha256"), "launcher projection staged stage0_sha256"
    )
    if stage0_sha != projected_stage0_sha:
        raise CapturedPaperHostCutoverError(
            "STAGE0_BINDING_MISMATCH", "candidate stage0 bytes differ from projection"
        )
    stage0_target = _sealed_capsule_path(
        stage0_path, roots=projection_roots, field="staged stage0 target"
    )
    if stage0_target.name.casefold() != f"{stage0_sha}.py".casefold():
        raise CapturedPaperHostCutoverError(
            "STAGE0_NOT_CONTENT_ADDRESSED",
            "candidate task must execute a content-addressed stage0 copy",
        )
    if stage0_target.exists() and _stable_read(
        stage0_target,
        roots=projection_roots,
        field="pre-staged candidate stage0",
        expected_sha256=stage0_sha,
    )[2] != stage0_sha:
        raise CapturedPaperHostCutoverError(
            "STAGE0_BINDING_MISMATCH", "pre-staged stage0 bytes differ"
        )
    service_source, _service_raw, service_sha = _stable_read(
        service_source_path,
        roots=projection_roots,
        field="candidate service source",
        expected_sha256=projection.get("service_source_sha256"),
    )
    if service_sha != _sha(
        projection.get("service_sha256"), "launcher projection staged service_sha256"
    ):
        raise CapturedPaperHostCutoverError(
            "SERVICE_BINDING_MISMATCH", "candidate service source hash differs"
        )
    service_target = _sealed_capsule_path(
        service_path, roots=projection_roots, field="staged service target"
    )
    if service_target.name.casefold() != f"{service_sha}.py".casefold():
        raise CapturedPaperHostCutoverError(
            "SERVICE_NOT_CONTENT_ADDRESSED",
            "candidate service target filename must be its content hash",
        )
    if service_target.exists() and _stable_read(
        service_target,
        roots=projection_roots,
        field="pre-staged candidate service",
        expected_sha256=service_sha,
    )[2] != service_sha:
        raise CapturedPaperHostCutoverError(
            "SERVICE_BINDING_MISMATCH", "pre-staged service bytes differ"
        )
    _validate_candidate_task_semantics(template, candidate_root=candidate_root)
    read_root_raw = _canonical_json_bytes(read_roots)
    read_root_b64 = base64.b64encode(read_root_raw).decode("ascii")
    launcher_args = (
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        launcher_path,
        "-Mode",
        "ActivatePaper",
        "-PythonExecutable",
        python_path,
        "-CandidateRoot",
        candidate_root,
        "-ServiceScriptPath",
        str(service_target),
        "-Stage0ScriptPath",
        str(stage0_target),
        "-ManifestPath",
        MANIFEST_PATH_TOKEN,
        "-ManifestSha256",
        MANIFEST_SHA256_TOKEN,
        "-AllowedReadRootsBase64",
        read_root_b64,
    )
    # 2026-07-17: compared case-insensitively — the sealed template stores
    # normcased paths while _sealed_capsule_path resolves the filesystem's
    # proper case, so a byte-exact compare fails on real Windows hosts even
    # when every token is path-identical (first observed live as
    # TASK_TEMPLATE_ACTION_MISMATCH on generation 3020dd01, tokens
    # -ServiceScriptPath/-Stage0ScriptPath only).  The template bytes are
    # already hash-bound via the candidate action, so this remains a
    # semantic re-derivation check, same as the working-directory compare.
    if os.path.normcase(arguments) != os.path.normcase(
        _quote_windows_arguments(launcher_args)
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_TEMPLATE_ACTION_MISMATCH",
            "candidate task template differs from the sealed ActivatePaper invocation",
        )
    resolved = _materialize_candidate_xml(
        template, manifest_path=manifest_path, manifest_sha256=manifest_sha256
    )
    resolved_command, resolved_arguments = _task_exec_from_xml(resolved)
    resolved_launcher_args = tuple(
        str(manifest_path) if item == MANIFEST_PATH_TOKEN else
        manifest_sha256 if item == MANIFEST_SHA256_TOKEN else item
        for item in launcher_args
    )
    if (
        os.path.normcase(resolved_command) != os.path.normcase(str(powershell_path))
        # Case-insensitive for the same reason as the template compare above.
        or os.path.normcase(resolved_arguments)
        != os.path.normcase(_quote_windows_arguments(resolved_launcher_args))
    ):
        raise CapturedPaperHostCutoverError(
            "TASK_RESOLUTION_MISMATCH", "resolved candidate task action is not exact"
        )
    service_args_value = projection.get("service_arguments")
    if (
        not isinstance(service_args_value, list)
        or any(not isinstance(item, str) or not item for item in service_args_value)
    ):
        raise CapturedPaperHostCutoverError(
            "INVOCATION_PROJECTION_INVALID", "service argument projection is invalid"
        )
    service_args = tuple(
        str(manifest_path) if item == MANIFEST_PATH_TOKEN else
        manifest_sha256 if item == MANIFEST_SHA256_TOKEN else item
        for item in service_args_value
    )
    if (
        len(service_args) < 20
        or service_args[:3] != ("-I", "-S", "-B")
        # Path token compared case-insensitively (projection stores normcased
        # paths, the resolved target carries filesystem proper case).
        or os.path.normcase(str(service_args[3]))
        != os.path.normcase(str(stage0_target))
        or "--" not in service_args
        or service_args[service_args.index("--target-role") + 1]
        != "activation_service"
        or os.path.normcase(service_args[service_args.index("--target") + 1])
        != os.path.normcase(str(service_target))
        or service_args[service_args.index("--target-sha256") + 1] != service_sha
    ):
        raise CapturedPaperHostCutoverError(
            "SERVICE_BINDING_MISMATCH",
            "service argv does not execute the exact staged service target",
        )
    ready_positions = [
        index for index, value in enumerate(service_args)
        if value == "--host-ready-receipt"
    ]
    if len(ready_positions) != 1 or ready_positions[0] + 1 >= len(service_args):
        raise CapturedPaperHostCutoverError(
            "STARTUP_HANDSHAKE_PATH_INVALID",
            "service argv lacks one sealed host-ready receipt base",
        )
    ready_base = str(service_args[ready_positions[0] + 1])
    _sealed_capsule_path(
        ready_base, roots=projection_roots, field="host-ready receipt base"
    )
    return resolved, CandidateInvocation(
        task_name=CANDIDATE_TASK_NAME,
        powershell_executable_path=str(powershell_path),
        powershell_executable_sha256=powershell_sha,
        launcher_source_path=str(launcher_source),
        launcher_source_sha256=launcher_sha,
        launcher_script_path=str(launcher_target),
        launcher_script_sha256=launcher_sha,
        stage0_source_path=str(stage0_source),
        stage0_source_sha256=stage0_sha,
        stage0_script_path=str(stage0_target),
        stage0_script_sha256=stage0_sha,
        service_source_path=str(service_source),
        service_source_sha256=service_sha,
        service_script_path=str(service_target),
        service_script_sha256=service_sha,
        host_ready_receipt_base=ready_base,
        launcher_arguments=resolved_launcher_args,
        python_executable_path=python_path,
        python_executable_sha256=_sha(
            projection.get("python_executable_sha256"),
            "launcher projection python_executable_sha256",
        ),
        python_dependency_root=str(projection.get("python_dependency_root") or ""),
        python_dependency_root_identity_sha256=_sha(
            projection.get("python_dependency_root_identity_sha256"),
            "launcher projection Python dependency root identity",
        ),
        service_arguments=service_args,
    )


def prepare_cutover(
    verified: activation_contract.VerifiedCapturedPaperActivation,
    *,
    allowed_read_roots: Sequence[str | Path],
    task_snapshot_path: str | Path,
    process_snapshot_path: str | Path,
    restore_plan_path: str | Path,
    candidate_task_template_path: str | Path,
    candidate_action_path: str | Path,
) -> PreparedCutover:
    """Bind final activation authority to exact rollback/candidate artifacts."""

    if not isinstance(verified, activation_contract.VerifiedCapturedPaperActivation):
        raise CapturedPaperHostCutoverError(
            "ACTIVATION_REQUIRED", "final captured PAPER activation is required"
        )
    if isinstance(verified, activation_contract.VerifiedCapturedPaperPreactivation):
        raise CapturedPaperHostCutoverError(
            "ACTIVATION_REQUIRED", "preactivation cannot authorize a host cutover"
        )
    if verified.paper_order_submission_authorized is not True:
        raise CapturedPaperHostCutoverError(
            "PAPER_AUTHORITY_MISSING", "final envelope does not authorize PAPER"
        )
    boundary = _mapping(
        verified.manifest.get("authority_boundary"), "activation.authority_boundary"
    )
    if not (
        boundary.get("broker") == "alpaca"
        and boundary.get("broker_environment") == "paper"
        and boundary.get("account_scope") == "alpaca:paper"
        and boundary.get("paper_order_submission_authorized") is True
        and boundary.get("live_cash_authorized") is False
        and boundary.get("real_money_authorized") is False
        and boundary.get("short_authorized") is False
        and boundary.get("crypto_authorized") is False
    ):
        raise CapturedPaperHostCutoverError(
            "AUTHORITY_BOUNDARY_INVALID",
            "host cutover accepts fake-money Alpaca PAPER equity-long authority only",
        )

    roots = _strict_roots(allowed_read_roots)
    candidate_root = _strict_existing_dir(
        verified.candidate_root, roots=roots, field="candidate_root"
    )
    if candidate_root != Path(__file__).resolve(strict=True).parents[1]:
        raise CapturedPaperHostCutoverError(
            "EXECUTOR_ROOT_MISMATCH", "host cutover executor is not from candidate root"
        )
    executor_path, _executor_raw, executor_sha = _stable_read(
        __file__, roots=roots, field="captured_paper_host_cutover"
    )
    expected_executor_sha = _sha(
        verified.source_hashes.get("captured_paper_host_cutover"),
        "code_build.captured_paper_host_cutover",
    )
    if executor_sha != expected_executor_sha or executor_path != Path(__file__).resolve():
        raise CapturedPaperHostCutoverError(
            "EXECUTOR_HASH_MISMATCH", "running host cutover source is not code-build bound"
        )

    rollback_path = verified.receipt_paths.get("rollback_snapshot")
    rollback_sha = _sha(
        verified.receipt_hashes.get("rollback_snapshot"),
        "rollback_snapshot receipt",
    )
    _receipt_path, receipt_raw, actual_receipt_sha = _stable_read(
        rollback_path,
        roots=roots,
        field="rollback_snapshot receipt",
        expected_sha256=rollback_sha,
    )
    receipt = _strict_json(receipt_raw, "rollback_snapshot receipt")
    if actual_receipt_sha != rollback_sha:
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_RECEIPT_HASH_MISMATCH", "rollback receipt hash mismatch"
        )
    if not (
        receipt.get("schema_version")
        == "chili.captured-paper-readiness.rollback_snapshot.v3"
        and receipt.get("receipt_kind") == "rollback_snapshot"
        and receipt.get("verdict") == "PASS"
        and receipt.get("activation_generation") == verified.activation_generation
        and receipt.get("account_scope") == "alpaca:paper"
        and receipt.get("expected_account_id") == verified.expected_account_id
        and receipt.get("live_cash_authorized") is False
        and receipt.get("orders_submitted") is False
    ):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_RECEIPT_INVALID", "typed rollback receipt escaped PAPER authority"
        )
    evidence = _mapping(receipt.get("evidence"), "rollback_snapshot.evidence")
    _exact_keys(
        evidence,
        {
            "schema_version",
            "source_receipts",
            "task_snapshot_sha256",
            "scheduled_task_xml_sha256s",
            "legacy_process_snapshot_sha256",
            "restore_plan_sha256",
            "host_cutover_source_sha256",
            "launcher_argument_contract_sha256",
            "candidate_task_xml_sha256",
            "candidate_action_sha256",
            "preactivation_baseline_sha256",
            "validation_mode",
            "singleton_policy",
            "host_mutation_count",
            "final_validate_only_performed",
            "captured_at",
        },
        "rollback_snapshot.evidence",
    )
    if evidence.get("schema_version") != (
        "chili.captured-paper-readiness-evidence.rollback_snapshot.v3"
    ):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_RECEIPT_INVALID", "rollback evidence schema is unsupported"
        )
    source_receipts = _mapping(
        evidence.get("source_receipts"), "rollback_snapshot.source_receipts"
    )
    _exact_keys(
        source_receipts,
        {"task_snapshot", "process_snapshot", "restore_plan", "candidate_action"},
        "rollback_snapshot.source_receipts",
    )
    for name in source_receipts:
        _sha(source_receipts[name], f"source receipt {name}")
    if (
        _sha(evidence.get("host_cutover_source_sha256"), "host cutover source")
        != executor_sha
        or evidence.get("singleton_policy") != SINGLETON_POLICY
        or evidence.get("validation_mode") != PREACTIVATION_ROLLBACK_BASELINE_MODE
        or evidence.get("host_mutation_count") != 0
        or evidence.get("final_validate_only_performed") is not False
    ):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_RECEIPT_INVALID",
            "rollback receipt is not an executor-bound preactivation baseline",
        )

    evidence_captured_at = _parse_utc(evidence.get("captured_at"), "evidence.captured_at")
    baseline = prepare_preactivation_rollback_baseline(
        PreActivationRollbackContext(
            activation_generation=verified.activation_generation,
            expected_account_id=verified.expected_account_id,
            candidate_root=candidate_root,
            allowed_read_roots=roots,
            host_cutover_source_sha256=executor_sha,
            launcher_argument_contract_sha256=_sha(
                evidence.get("launcher_argument_contract_sha256"),
                "rollback launcher argument contract",
            ),
        ),
        task_snapshot_path=task_snapshot_path,
        process_snapshot_path=process_snapshot_path,
        restore_plan_path=restore_plan_path,
        candidate_task_template_path=candidate_task_template_path,
        candidate_action_path=candidate_action_path,
        validated_at=evidence_captured_at,
    )
    if not (
        baseline.task_snapshot.artifact_sha256
        == _sha(evidence.get("task_snapshot_sha256"), "task snapshot")
        and baseline.process_snapshot.artifact_sha256
        == _sha(
            evidence.get("legacy_process_snapshot_sha256"), "process snapshot"
        )
        and baseline.restore_plan.artifact_sha256
        == _sha(evidence.get("restore_plan_sha256"), "restore plan")
        and baseline.candidate_template_sha256
        == _sha(evidence.get("candidate_task_xml_sha256"), "candidate task XML")
        and baseline.candidate_action_sha256
        == _sha(evidence.get("candidate_action_sha256"), "candidate action")
        and baseline.baseline_sha256
        == _sha(
            evidence.get("preactivation_baseline_sha256"),
            "preactivation baseline",
        )
    ):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_BASELINE_HASH_MISMATCH",
            "final cutover inputs differ from the preactivation baseline",
        )
    task_snapshot = baseline.task_snapshot
    process_snapshot = baseline.process_snapshot
    restore_plan = baseline.restore_plan

    cutover = _mapping(verified.manifest.get("cutover"), "activation.cutover")
    launcher_contract_path, launcher_contract_raw, launcher_contract_sha = _stable_read(
        cutover.get("launcher_arguments_path"),
        roots=roots,
        field="launcher_argument_contract",
        expected_sha256=cutover.get("launcher_arguments_sha256"),
    )
    if launcher_contract_sha != _sha(
        evidence.get("launcher_argument_contract_sha256"),
        "rollback launcher argument contract",
    ):
        raise CapturedPaperHostCutoverError(
            "LAUNCHER_CONTRACT_HASH_MISMATCH",
            "rollback receipt names another launcher argument contract",
        )
    launcher_contract = _strict_json(
        launcher_contract_raw, "launcher_argument_contract"
    )
    invocations = _mapping(
        launcher_contract.get("invocations"), "launcher_argument_contract.invocations"
    )
    activate_entry = _mapping(invocations.get("ActivatePaper"), "ActivatePaper")
    projection = _mapping(activate_entry.get("projection"), "ActivatePaper.projection")
    if sha256_json(projection) != _sha(
        activate_entry.get("projection_sha256"), "ActivatePaper.projection_sha256"
    ):
        raise CapturedPaperHostCutoverError(
            "LAUNCHER_CONTRACT_HASH_MISMATCH", "ActivatePaper projection hash mismatch"
        )

    template_path, template_raw, template_sha = _stable_read(
        candidate_task_template_path,
        roots=roots,
        field="candidate_task_xml",
        expected_sha256=evidence.get("candidate_task_xml_sha256"),
    )
    action_path, action_raw, action_sha = _stable_read(
        candidate_action_path,
        roots=roots,
        field="candidate_action",
        expected_sha256=evidence.get("candidate_action_sha256"),
    )
    action = _strict_json(action_raw, "candidate_action")
    if action_raw != _canonical_json_bytes(action):
        raise CapturedPaperHostCutoverError(
            "ARTIFACT_NOT_CANONICAL", "candidate action is not canonical JSON"
        )
    _exact_keys(
        action,
        {
            "schema_version",
            "host_cutover_source_sha256",
            "launcher_argument_contract_sha256",
            "candidate_task_xml_sha256",
            "singleton_policy",
        },
        "candidate_action",
    )
    if not (
        action.get("schema_version") == CANDIDATE_ACTION_SCHEMA
        and action.get("host_cutover_source_sha256") == executor_sha
        and action.get("launcher_argument_contract_sha256") == launcher_contract_sha
        and action.get("candidate_task_xml_sha256") == template_sha
        and action.get("singleton_policy") == SINGLETON_POLICY
        and sha256_json(action) == action_sha
    ):
        raise CapturedPaperHostCutoverError(
            "CANDIDATE_ACTION_HASH_MISMATCH", "candidate action is not exactly bound"
        )
    resolved_xml, invocation = _validate_candidate_template(
        template=template_raw,
        projection=projection,
        manifest_path=verified.manifest_path,
        manifest_sha256=verified.manifest_sha256,
    )
    if os.path.normcase(invocation.python_executable_path) != os.path.normcase(
        str(projection.get("python_executable_path") or "")
    ):
        raise CapturedPaperHostCutoverError(
            "PYTHON_BINDING_MISMATCH", "candidate task uses another Python executable"
        )
    _stable_read(
        invocation.python_executable_path,
        roots=roots,
        field="candidate Python executable",
        expected_sha256=invocation.python_executable_sha256,
        max_bytes=256 * 1024 * 1024,
    )
    receipt_captured_at = _parse_utc(receipt.get("captured_at"), "receipt.captured_at")
    evidence_captured_at = _parse_utc(evidence.get("captured_at"), "evidence.captured_at")
    if any(
        timestamp > receipt_captured_at
        for timestamp in (
            task_snapshot.captured_at,
            process_snapshot.captured_at,
            evidence_captured_at,
        )
    ):
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CHRONOLOGY_INVALID", "rollback artifacts postdate their receipt"
        )
    return PreparedCutover(
        activation_generation=verified.activation_generation,
        expected_account_id=verified.expected_account_id,
        manifest_path=verified.manifest_path,
        manifest_sha256=verified.manifest_sha256,
        candidate_root=candidate_root,
        allowed_read_roots=roots,
        task_snapshot=task_snapshot,
        process_snapshot=process_snapshot,
        restore_plan=restore_plan,
        candidate_action_path=action_path,
        candidate_action_sha256=action_sha,
        candidate_template_path=template_path,
        candidate_template_sha256=template_sha,
        resolved_task_xml=resolved_xml,
        resolved_task_xml_sha256=sha256_bytes(resolved_xml),
        invocation=invocation,
        rollback_receipt_sha256=rollback_sha,
    )


def build_candidate_action_document(
    *,
    host_cutover_source_sha256: str,
    launcher_argument_contract_sha256: str,
    candidate_task_xml_sha256: str,
) -> Mapping[str, Any]:
    """Return the exact canonical object sealed by rollback_snapshot v3."""

    return {
        "schema_version": CANDIDATE_ACTION_SCHEMA,
        "host_cutover_source_sha256": _sha(
            host_cutover_source_sha256, "host_cutover_source_sha256"
        ),
        "launcher_argument_contract_sha256": _sha(
            launcher_argument_contract_sha256,
            "launcher_argument_contract_sha256",
        ),
        "candidate_task_xml_sha256": _sha(
            candidate_task_xml_sha256, "candidate_task_xml_sha256"
        ),
        "singleton_policy": SINGLETON_POLICY,
    }


def candidate_action_sha256(
    *,
    host_cutover_source_sha256: str,
    launcher_argument_contract_sha256: str,
    candidate_task_xml_sha256: str,
) -> str:
    return sha256_json(
        build_candidate_action_document(
            host_cutover_source_sha256=host_cutover_source_sha256,
            launcher_argument_contract_sha256=launcher_argument_contract_sha256,
            candidate_task_xml_sha256=candidate_task_xml_sha256,
        )
    )


def build_candidate_task_xml_template(
    *,
    principal_user_id: str,
    powershell_executable_path: str,
    activate_paper_projection: Mapping[str, Any],
) -> bytes:
    """Build deterministic tokenized XML for the one unified PAPER task.

    The two manifest tokens remain literal until final activation.  This
    helper has no host side effects and is suitable for readiness packaging.
    """

    user = str(principal_user_id or "").strip()
    if not user or any(character in user for character in "<>\r\n"):
        raise CapturedPaperHostCutoverError(
            "TASK_PRINCIPAL_INVALID", "candidate task principal is invalid"
        )
    powershell, _powershell_sha = _resolve_system_executable(
        powershell_executable_path, "candidate task PowerShell executable"
    )
    projection = activate_paper_projection
    read_roots = projection.get("allowed_read_roots")
    if (
        projection.get("mode") != "ActivatePaper"
        or projection.get("service_mode") != "activate-paper"
        or not isinstance(read_roots, list)
        or not read_roots
    ):
        raise CapturedPaperHostCutoverError(
            "INVOCATION_PROJECTION_INVALID", "ActivatePaper projection is invalid"
        )
    read_root_b64 = base64.b64encode(_canonical_json_bytes(read_roots)).decode("ascii")
    arguments = (
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
        str(projection.get("service_staged_path") or ""),
        "-Stage0ScriptPath",
        str(projection.get("stage0_path") or ""),
        "-ManifestPath",
        MANIFEST_PATH_TOKEN,
        "-ManifestSha256",
        MANIFEST_SHA256_TOKEN,
        "-AllowedReadRootsBase64",
        read_root_b64,
    )
    import xml.sax.saxutils as saxutils

    escape = lambda value: saxutils.escape(str(value), {'"': "&quot;"})
    xml = (
        # 2026-07-16 -> 2026-07-17: msxml/schtasks REJECT a UTF-8-declared
        # task XML at /Create ("unable to switch the encoding", reproduced
        # live and side-effect-free on the target host) — the file must be
        # real UTF-16 (BOM + matching declaration), the scheduler's own
        # export format.  ET parsers accept it identically.
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.4" xmlns="{_TASK_NS}">\n'
        "  <RegistrationInfo>\n"
        "    <Description>One hash-bound captured Alpaca PAPER host; no live-cash authority.</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers />\n"
        "  <Principals>\n"
        "    <Principal id=\"Author\">\n"
        f"      <UserId>{escape(user)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(powershell)}</Command>\n"
        f"      <Arguments>{escape(_quote_windows_arguments(arguments))}</Arguments>\n"
        f"      <WorkingDirectory>{escape(projection.get('candidate_root') or '')}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )
    raw = xml.encode("utf-16")
    # Self-review with the same parser used at Apply; manifest values are only
    # placeholders here and are deliberately not resolved.
    _task_enabled_from_xml(raw)
    _task_exec_from_xml(raw)
    _validate_candidate_task_semantics(
        raw, candidate_root=str(projection.get("candidate_root") or "")
    )
    return raw


class _JournalLock:
    def __init__(self, path: Path, *, mutex_name: str | None = None) -> None:
        self._path = path
        self._mutex_name = mutex_name
        self._mutex_handle: Any = None
        self._handle: Any = None

    def __enter__(self) -> "_JournalLock":
        if os.name == "nt" and self._mutex_name is not None:
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                create = kernel32.CreateMutexW
                create.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
                create.restype = wintypes.HANDLE
                wait = kernel32.WaitForSingleObject
                wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
                wait.restype = wintypes.DWORD
                close = kernel32.CloseHandle
                close.argtypes = (wintypes.HANDLE,)
                close.restype = wintypes.BOOL
                release = kernel32.ReleaseMutex
                release.argtypes = (wintypes.HANDLE,)
                release.restype = wintypes.BOOL
                handle = create(None, False, self._mutex_name)
                if not handle:
                    raise OSError("CreateMutexW failed")
                result = int(wait(handle, 0))
                if result not in {0x00000000, 0x00000080}:
                    close(handle)
                    raise OSError("host mutex is already owned or unavailable")
                self._mutex_handle = (handle, kernel32)
                return self
            except (AttributeError, ImportError, OSError, TypeError) as exc:
                raise CapturedPaperHostCutoverError(
                    "CUTOVER_ALREADY_RUNNING",
                    "another host-cutover transaction owns or obscures the host mutex",
                ) from exc
        self._handle = self._path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"0")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        try:
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        except (ImportError, OSError) as exc:
            self._handle.close()
            self._handle = None
            raise CapturedPaperHostCutoverError(
                "CUTOVER_ALREADY_RUNNING", "another host-cutover transaction owns the journal"
            ) from exc
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self._mutex_handle is not None:
            handle, kernel32 = self._mutex_handle
            self._mutex_handle = None
            try:
                kernel32.ReleaseMutex(handle)
            finally:
                kernel32.CloseHandle(handle)
            return
        if self._handle is None:
            return
        try:
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()
            self._handle = None


class CutoverJournal:
    """Append-only hash chain plus content-addressed task XML objects."""

    def __init__(
        self,
        *,
        root: Path,
        prepared: PreparedCutover,
        clock: Callable[[], datetime],
    ) -> None:
        self.root = root
        self.prepared = prepared
        self.clock = clock
        self.transaction_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"chili:captured-paper-cutover:{prepared.activation_generation}:"
                f"{prepared.manifest_sha256}",
            )
        )
        generation_root = root / prepared.activation_generation
        generation_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        _reject_reparse_chain(generation_root)
        self.generation_root = generation_root
        self.objects_root = generation_root / "objects"
        self.objects_root.mkdir(mode=0o700, exist_ok=True)
        _reject_reparse_chain(self.objects_root)
        self.path = generation_root / f"{prepared.manifest_sha256}.jsonl"
        self.lock_path = generation_root / f"{prepared.manifest_sha256}.lock"
        self._observed_journal_raw = b""
        self._valid_journal_prefix = b""
        self._complete_record_missing_newline = False
        self._events = self._read_events()

    @property
    def events(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._events)

    def lock(self) -> _JournalLock:
        return _JournalLock(self.lock_path)

    def _read_events(self) -> list[Mapping[str, Any]]:
        if not self.path.exists():
            self._observed_journal_raw = b""
            self._valid_journal_prefix = b""
            self._complete_record_missing_newline = False
            return []
        _reject_reparse_chain(self.path)
        raw = self.path.read_bytes()
        self._observed_journal_raw = raw
        parse_raw = raw
        self._complete_record_missing_newline = False
        if raw and not raw.endswith(b"\n"):
            boundary = raw.rfind(b"\n") + 1
            tail = raw[boundary:]
            try:
                _strict_json(tail, "journal final record")
            except CapturedPaperHostCutoverError as exc:
                if exc.code != "INVALID_JSON":
                    raise
                # A power loss may leave only a byte prefix of the final
                # append.  The preceding hash chain remains authoritative;
                # append() truncates this exact observed tail under the lock.
                parse_raw = raw[:boundary]
            else:
                parse_raw = raw
                self._complete_record_missing_newline = True
        self._valid_journal_prefix = parse_raw + (
            b"\n" if parse_raw and not parse_raw.endswith(b"\n") else b""
        )
        rows: list[Mapping[str, Any]] = []
        previous = "0" * 64
        for index, line in enumerate(parse_raw.splitlines()):
            event = _strict_json(line, f"journal[{index}]")
            if line != _canonical_json_bytes(event):
                raise CapturedPaperHostCutoverError(
                    "JOURNAL_NOT_CANONICAL", "journal record is not canonical JSON"
                )
            _exact_keys(
                event,
                {
                    "schema_version",
                    "transaction_id",
                    "sequence",
                    "previous_event_sha256",
                    "event_type",
                    "recorded_at",
                    "payload",
                    "event_sha256",
                },
                f"journal[{index}]",
            )
            claimed = _sha(event.get("event_sha256"), f"journal[{index}].event_sha256")
            body = dict(event)
            body.pop("event_sha256")
            if not (
                event.get("schema_version") == JOURNAL_EVENT_SCHEMA
                and event.get("transaction_id") == self.transaction_id
                and event.get("sequence") == index + 1
                and event.get("previous_event_sha256") == previous
                and sha256_json(body) == claimed
            ):
                raise CapturedPaperHostCutoverError(
                    "JOURNAL_CHAIN_INVALID", "host-cutover journal hash chain is invalid"
                )
            previous = claimed
            rows.append(event)
        return rows

    def _repair_valid_prefix_before_append(self) -> None:
        if self._observed_journal_raw == self._valid_journal_prefix:
            return
        try:
            current = self.path.read_bytes() if self.path.exists() else b""
            if current != self._observed_journal_raw:
                raise CapturedPaperHostCutoverError(
                    "JOURNAL_DRIFT", "journal changed after its locked inventory"
                )
            with self.path.open("r+b") as handle:
                handle.seek(0)
                written = handle.write(self._valid_journal_prefix)
                if written != len(self._valid_journal_prefix):
                    raise OSError("short journal-prefix repair write")
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise CapturedPaperHostCutoverError(
                "JOURNAL_REPAIR_FAILED", "could not repair the valid journal prefix"
            ) from exc
        self._observed_journal_raw = self._valid_journal_prefix

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        recorded_at: datetime | None = None,
    ) -> Mapping[str, Any]:
        self._repair_valid_prefix_before_append()
        previous = (
            str(self._events[-1]["event_sha256"])
            if self._events
            else "0" * 64
        )
        body: dict[str, Any] = {
            "schema_version": JOURNAL_EVENT_SCHEMA,
            "transaction_id": self.transaction_id,
            "sequence": len(self._events) + 1,
            "previous_event_sha256": previous,
            "event_type": str(event_type),
            "recorded_at": _iso(self.clock() if recorded_at is None else recorded_at),
            "payload": json.loads(_canonical_json_bytes(payload).decode("utf-8")),
        }
        body["event_sha256"] = sha256_json(body)
        raw = _canonical_json_bytes(body) + b"\n"
        with self.path.open("ab", buffering=0) as handle:
            offset = 0
            while offset < len(raw):
                written = handle.write(raw[offset:])
                if not written:
                    raise CapturedPaperHostCutoverError(
                        "JOURNAL_WRITE_FAILED", "journal append made no progress"
                    )
                offset += written
            os.fsync(handle.fileno())
        self._events.append(MappingProxyType(body))
        self._observed_journal_raw += raw
        self._valid_journal_prefix = self._observed_journal_raw
        return self._events[-1]

    def publish_object(self, raw: bytes, *, kind: str) -> Path:
        digest = sha256_bytes(raw)
        folder = self.objects_root / digest[:2]
        folder.mkdir(mode=0o700, exist_ok=True)
        _reject_reparse_chain(folder)
        suffix = ".xml" if kind.endswith("xml") else ".json"
        path = folder / f"{digest}{suffix}"
        try:
            with path.open("xb") as handle:
                written = handle.write(raw)
                if written != len(raw):
                    raise OSError("short journal-object write")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if path.read_bytes() != raw:
                raise CapturedPaperHostCutoverError(
                    "CONTENT_ADDRESS_COLLISION", "journal object path has different bytes"
                )
        if sha256_bytes(path.read_bytes()) != digest:
            raise CapturedPaperHostCutoverError(
                "JOURNAL_OBJECT_DRIFT", "journal object failed readback"
            )
        return path

    def object_path(self, raw: bytes, *, kind: str) -> Path:
        digest = sha256_bytes(raw)
        suffix = ".xml" if kind.endswith("xml") else ".json"
        path = self.objects_root / digest[:2] / f"{digest}{suffix}"
        if not path.is_file() or sha256_bytes(path.read_bytes()) != digest:
            raise CapturedPaperHostCutoverError(
                "JOURNAL_OBJECT_MISSING",
                "a pre-published rollback object is missing or changed",
            )
        _reject_reparse_chain(path)
        return path

    def read_referenced_object(
        self, *, value: Any, expected_sha256: Any, kind: str
    ) -> tuple[Path, bytes, str]:
        digest = _sha(expected_sha256, f"{kind} object hash")
        suffix = ".xml" if kind.endswith("xml") else ".json"
        expected = self.objects_root / digest[:2] / f"{digest}{suffix}"
        supplied = Path(str(value or ""))
        if supplied != expected:
            raise CapturedPaperHostCutoverError(
                "JOURNAL_OBJECT_REFERENCE_INVALID",
                f"{kind} reference is not its content-addressed object path",
            )
        _reject_reparse_chain(expected)
        if not expected.is_file():
            raise CapturedPaperHostCutoverError(
                "JOURNAL_OBJECT_MISSING", f"{kind} object is missing"
            )
        raw = expected.read_bytes()
        if sha256_bytes(raw) != digest:
            raise CapturedPaperHostCutoverError(
                "JOURNAL_OBJECT_DRIFT", f"{kind} object content hash differs"
            )
        return expected, raw, digest


def _last_event_type(journal: CutoverJournal) -> str | None:
    if not journal.events:
        return None
    return str(journal.events[-1].get("event_type") or "")


def _task_definition_sha_ignoring_enabled(raw: bytes) -> str:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CapturedPaperHostCutoverError(
            "TASK_XML_INVALID", "task XML is malformed"
        ) from exc
    settings = root.find(f".//{{{_TASK_NS}}}Settings")
    if settings is None:
        raise CapturedPaperHostCutoverError(
            "TASK_XML_INVALID", "task XML has no Settings element"
        )
    # Task Scheduler's XSD defines a missing Settings/Enabled as ``true`` and
    # schtasks OMITS the default from exports (see _task_enabled_from_xml),
    # while the post-disable readback carries an explicit false at whatever
    # position the service re-serializes it to.  Removing the element (rather
    # than rewriting its text) canonicalizes the omitted-default, explicit
    # and re-positioned forms identically so this sha compares the task
    # DEFINITION, not the enablement flag.  (2026-07-17: the first live
    # Apply died here — TASK_XML_INVALID on the first legacy task's
    # post-disable drift check, reproduced side-effect-free on a throwaway
    # task.)
    enabled = settings.find(f"{{{_TASK_NS}}}Enabled")
    if enabled is not None:
        settings.remove(enabled)
    return sha256_bytes(ET.tostring(root, encoding="utf-8"))


def _journal_state(events: Sequence[Mapping[str, Any]]) -> str:
    state = "baseline"
    for event in events:
        kind = str(event.get("event_type") or "")
        if kind == "apply_started":
            state = "applying"
        elif kind == "apply_completed":
            state = "applied"
        elif kind == "rollback_started":
            state = "rolling_back"
        elif kind == "rollback_completed":
            state = "baseline"
    return state


def _legacy_execution_lane_document(
    value: LegacyExecutionLaneObservation,
) -> Mapping[str, Any]:
    document = {
        "schema_version": LEGACY_EXECUTION_LANE_SCHEMA,
        "container_name": value.container_name,
        "container_id": value.container_id,
        "image_id": value.image_id,
        "config_sha256": value.config_sha256,
        "execution_scope": value.execution_scope,
        "scope_sha256": value.scope_sha256,
        "recreator_tasks": [
            {
                "name": item.name,
                "definition_sha256": item.definition_sha256,
                "action_sha256": item.action_sha256,
                "source_chain_sha256": item.source_chain_sha256,
                "enabled": item.enabled,
            }
            for item in sorted(value.recreator_tasks, key=lambda row: row.name)
        ],
        "state": value.state,
    }
    return MappingProxyType(document)


def _parse_legacy_execution_lane(
    value: Any, *, field: str
) -> LegacyExecutionLaneObservation:
    lane = _mapping(value, field)
    _exact_keys(
        lane,
        {
            "schema_version",
            "container_name",
            "container_id",
            "image_id",
            "config_sha256",
            "execution_scope",
            "scope_sha256",
            "recreator_tasks",
            "state",
        },
        field,
    )
    container_id = str(lane.get("container_id") or "").lower()
    image_id = str(lane.get("image_id") or "").lower()
    state = str(lane.get("state") or "")
    execution_scope = str(lane.get("execution_scope") or "")
    raw_recreator_tasks = lane.get("recreator_tasks")
    if not isinstance(raw_recreator_tasks, list):
        raise CapturedPaperHostCutoverError(
            "EXECUTION_LANE_IDENTITY_INVALID",
            f"{field} has no exact external recreator-task roster",
        )
    recreator_tasks: list[ExecutionLaneRecreatorTaskObservation] = []
    for index, raw_task in enumerate(raw_recreator_tasks):
        task = _mapping(raw_task, f"{field}.recreator_tasks[{index}]")
        _exact_keys(
            task,
            {
                "name",
                "definition_sha256",
                "action_sha256",
                "source_chain_sha256",
                "enabled",
            },
            f"{field}.recreator_tasks[{index}]",
        )
        name = str(task.get("name") or "")
        if type(task.get("enabled")) is not bool:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_IDENTITY_INVALID",
                f"{field} has a malformed recreator-task state",
            )
        recreator_tasks.append(
            ExecutionLaneRecreatorTaskObservation(
                name=name,
                definition_sha256=_sha(
                    task.get("definition_sha256"),
                    f"{field}.recreator_tasks[{index}].definition_sha256",
                ),
                action_sha256=_sha(
                    task.get("action_sha256"),
                    f"{field}.recreator_tasks[{index}].action_sha256",
                ),
                source_chain_sha256=_sha(
                    task.get("source_chain_sha256"),
                    f"{field}.recreator_tasks[{index}].source_chain_sha256",
                ),
                enabled=bool(task["enabled"]),
            )
        )
    if (
        [item.name for item in recreator_tasks]
        != sorted(EXECUTION_LANE_RECREATOR_TASKS)
        or len({item.name for item in recreator_tasks}) != len(recreator_tasks)
    ):
        raise CapturedPaperHostCutoverError(
            "EXECUTION_LANE_IDENTITY_INVALID",
            f"{field} has an incomplete external recreator-task roster",
        )
    if not (
        lane.get("schema_version") == LEGACY_EXECUTION_LANE_SCHEMA
        and lane.get("container_name") == LEGACY_EXECUTION_LANE_NAME
        and _SHA256_RE.fullmatch(container_id)
        and image_id.startswith("sha256:")
        and _SHA256_RE.fullmatch(image_id.removeprefix("sha256:"))
        and _sha(lane.get("config_sha256"), f"{field}.config_sha256")
        and execution_scope == "legacy:mixed-paper-config-live-masters-disabled"
        and _sha(lane.get("scope_sha256"), f"{field}.scope_sha256")
        and state in LEGACY_EXECUTION_LANE_PRIOR_STATES
    ):
        raise CapturedPaperHostCutoverError(
            "EXECUTION_LANE_IDENTITY_INVALID",
            f"{field} is not the exact allowlisted Docker container",
        )
    return LegacyExecutionLaneObservation(
        container_name=LEGACY_EXECUTION_LANE_NAME,
        container_id=container_id,
        image_id=image_id,
        config_sha256=str(lane["config_sha256"]).lower(),
        execution_scope=execution_scope,
        scope_sha256=str(lane["scope_sha256"]).lower(),
        recreator_tasks=tuple(recreator_tasks),
        state=state,
    )


def _legacy_execution_lane_baseline(
    events: Sequence[Mapping[str, Any]],
) -> LegacyExecutionLaneObservation:
    """Read the durable exact pre-mutation Docker lane identity."""

    started = [event for event in events if event.get("event_type") == "apply_started"]
    if len(started) != 1:
        raise CapturedPaperHostCutoverError(
            "EXECUTION_LANE_ROLLBACK_AUTHORITY_MISSING",
            "rollback requires one journal-bound execution lane baseline",
        )
    payload = _mapping(started[0].get("payload"), "apply_started.payload")
    if payload.get("legacy_execution_lane") is not None:
        return _parse_legacy_execution_lane(
            payload.get("legacy_execution_lane"),
            field="apply_started.payload.legacy_execution_lane",
        )
    adopted = [
        event
        for event in events
        if event.get("event_type") == "legacy_execution_lane_identity_adopted"
    ]
    if len(adopted) != 1:
        raise CapturedPaperHostCutoverError(
            "EXECUTION_LANE_ROLLBACK_AUTHORITY_MISSING",
            "legacy journal has no exact Docker identity adoption",
        )
    adopted_payload = _mapping(
        adopted[0].get("payload"),
        "legacy_execution_lane_identity_adopted.payload",
    )
    _exact_keys(
        adopted_payload,
        {"legacy_execution_lane", "reason", "host_baseline_verified"},
        "legacy execution lane adoption",
    )
    if not (
        adopted_payload.get("reason")
        == "pre_identity_schema_journal_already_at_exact_baseline"
        and adopted_payload.get("host_baseline_verified") is True
    ):
        raise CapturedPaperHostCutoverError(
            "EXECUTION_LANE_ROLLBACK_AUTHORITY_INVALID",
            "legacy Docker identity adoption is not an exact baseline proof",
        )
    return _parse_legacy_execution_lane(
        adopted_payload.get("legacy_execution_lane"),
        field="legacy_execution_lane_identity_adopted.legacy_execution_lane",
    )


def _legacy_execution_lane_quiesced_state(prior_state: str) -> str:
    if prior_state in LEGACY_EXECUTION_LANE_PRIOR_STATES:
        return "stopped"
    raise CapturedPaperHostCutoverError(
        "EXECUTION_LANE_STATE_INVALID", "legacy execution lane state is unsupported"
    )


def _discover_rollback_capsule(
    *,
    journal_root: str | Path,
    manifest_sha256: str,
    caller_roots: Sequence[Path],
) -> PreparedCutover:
    """Recover rollback authority without loading mutable activation inputs."""

    root = _strict_existing_dir(
        journal_root, roots=caller_roots, field="rollback journal_root"
    )
    manifest_sha = _sha(manifest_sha256, "rollback manifest_sha256")
    matches: list[tuple[str, Path]] = []
    try:
        children = list(root.iterdir())
    except OSError as exc:
        raise CapturedPaperHostCutoverError(
            "JOURNAL_UNREADABLE", "cannot inventory rollback journal generations"
        ) from exc
    for child in children:
        _reject_reparse_chain(child)
        if not child.is_dir():
            continue
        try:
            generation = str(uuid.UUID(child.name))
        except ValueError:
            continue
        if generation != child.name:
            continue
        candidate = child / f"{manifest_sha}.jsonl"
        if candidate.is_file():
            _reject_reparse_chain(candidate)
            matches.append((generation, candidate))
    if len(matches) != 1:
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_JOURNAL_AMBIGUOUS",
            "manifest hash must identify exactly one local rollback journal",
        )
    generation, journal_path = matches[0]
    raw = journal_path.read_bytes()
    parse_raw = raw
    if raw and not raw.endswith(b"\n"):
        boundary = raw.rfind(b"\n") + 1
        tail = raw[boundary:]
        try:
            _strict_json(tail, "rollback journal final record")
        except CapturedPaperHostCutoverError as exc:
            if exc.code != "INVALID_JSON":
                raise
            parse_raw = raw[:boundary]
    transaction_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"chili:captured-paper-cutover:{generation}:{manifest_sha}",
        )
    )
    events: list[Mapping[str, Any]] = []
    previous = "0" * 64
    for index, line in enumerate(parse_raw.splitlines()):
        event = _strict_json(line, f"rollback journal[{index}]")
        _exact_keys(
            event,
            {
                "schema_version", "transaction_id", "sequence",
                "previous_event_sha256", "event_type", "recorded_at", "payload",
                "event_sha256",
            },
            f"rollback journal[{index}]",
        )
        claimed = _sha(
            event.get("event_sha256"), f"rollback journal[{index}].event_sha256"
        )
        body = dict(event)
        body.pop("event_sha256")
        if not (
            event.get("schema_version") == JOURNAL_EVENT_SCHEMA
            and event.get("transaction_id") == transaction_id
            and event.get("sequence") == index + 1
            and event.get("previous_event_sha256") == previous
            and sha256_json(body) == claimed
        ):
            raise CapturedPaperHostCutoverError(
                "JOURNAL_CHAIN_INVALID", "rollback journal hash chain is invalid"
            )
        previous = claimed
        events.append(event)
    started = [event for event in events if event.get("event_type") == "apply_started"]
    if len(started) != 1:
        raise CapturedPaperHostCutoverError(
            "ROLLBACK_CAPSULE_MISSING", "rollback journal has no unique capsule owner"
        )
    payload = _mapping(started[0].get("payload"), "rollback apply_started.payload")
    capsule_sha = _sha(
        payload.get("rollback_capsule_sha256"), "rollback capsule hash"
    )
    expected_path = (
        journal_path.parent / "objects" / capsule_sha[:2] / f"{capsule_sha}.json"
    )
    if Path(str(payload.get("rollback_capsule_path") or "")) != expected_path:
        raise CapturedPaperHostCutoverError(
            "JOURNAL_OBJECT_REFERENCE_INVALID",
            "rollback capsule path is not content-addressed under its generation",
        )
    _reject_reparse_chain(expected_path)
    if not expected_path.is_file():
        raise CapturedPaperHostCutoverError(
            "JOURNAL_OBJECT_MISSING", "rollback capsule object is missing"
        )
    capsule_raw = expected_path.read_bytes()
    if sha256_bytes(capsule_raw) != capsule_sha:
        raise CapturedPaperHostCutoverError(
            "JOURNAL_OBJECT_DRIFT", "rollback capsule object changed"
        )
    return _parse_rollback_capsule(
        path=expected_path,
        raw=capsule_raw,
        digest=capsule_sha,
        caller_roots=caller_roots,
        expected_generation=generation,
        expected_manifest_sha256=manifest_sha,
    )


def _discover_single_active_rollback_capsule(
    *,
    journal_root: str | Path,
    caller_roots: Sequence[Path],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[PreparedCutover, str] | None:
    """Find one interrupted/applied local transaction without mutable inputs.

    Baseline journals are intentionally ignored.  Every candidate is still
    admitted through ``_discover_rollback_capsule`` and its hash-chained,
    content-addressed rollback capsule.  More than one active transaction is
    ambiguous and therefore cannot be recovered automatically.
    """

    raw_root = Path(journal_root)
    if not raw_root.is_absolute() or str(raw_root).startswith(("\\\\", "//")):
        raise CapturedPaperHostCutoverError(
            "JOURNAL_PATH_INVALID", "recovery journal_root must be an absolute local path"
        )
    if not os.path.lexists(raw_root):
        parent = raw_root.parent.resolve(strict=True)
        _reject_reparse_chain(parent)
        if not _inside(parent, caller_roots):
            raise CapturedPaperHostCutoverError(
                "PATH_OUTSIDE_ROOTS", "recovery journal_root escaped caller roots"
            )
        return None
    _reject_reparse_chain(raw_root)
    root = _strict_existing_dir(
        raw_root, roots=caller_roots, field="recovery journal_root"
    )
    manifest_hashes: set[str] = set()
    for generation_root in root.iterdir():
        _reject_reparse_chain(generation_root)
        if not generation_root.is_dir():
            continue
        try:
            generation = str(uuid.UUID(generation_root.name))
        except ValueError:
            continue
        if generation != generation_root.name:
            continue
        for journal_path in generation_root.glob("*.jsonl"):
            _reject_reparse_chain(journal_path)
            if journal_path.is_file() and _SHA256_RE.fullmatch(journal_path.stem):
                manifest_hashes.add(journal_path.stem)

    active: list[tuple[PreparedCutover, str]] = []
    for manifest_sha in sorted(manifest_hashes):
        prepared = _discover_rollback_capsule(
            journal_root=root,
            manifest_sha256=manifest_sha,
            caller_roots=caller_roots,
        )
        journal = CutoverJournal(root=root, prepared=prepared, clock=clock)
        state = _journal_state(journal.events)
        if state != "baseline":
            active.append((prepared, state))
    if len(active) > 1:
        raise CapturedPaperHostCutoverError(
            "ACTIVE_ROLLBACK_JOURNAL_AMBIGUOUS",
            "more than one nonbaseline cutover transaction requires manual review",
        )
    return active[0] if active else None


def _startup_handshake_paths(
    invocation: CandidateInvocation, *, roots: Sequence[Path]
) -> Mapping[str, Path]:
    base = _sealed_capsule_path(
        invocation.host_ready_receipt_base,
        roots=roots,
        field="startup PREPARED receipt base",
    )
    values = {
        "prepared": base,
        "permit": Path(f"{base}.permit.json"),
        "started": Path(f"{base}.started.json"),
        "active_start_evidence": Path(
            f"{base}.active-start-evidence.json"
        ),
        "revocation_requested": Path(f"{base}.revocation-requested.json"),
        "revoked": Path(f"{base}.revoked.json"),
        "dispatch_lock": Path(f"{base}.dispatch.lock"),
    }
    for kind, path in values.items():
        values[kind] = _sealed_capsule_path(
            path, roots=roots, field=f"startup {kind} path"
        )
    if len({os.path.normcase(str(path)) for path in values.values()}) != len(values):
        raise CapturedPaperHostCutoverError(
            "STARTUP_HANDSHAKE_PATH_INVALID", "startup paths are not distinct"
        )
    return MappingProxyType(values)


_DISPATCH_LOCK_IDENTITY_KEYS = frozenset(
    {
        "dispatch_lock_path",
        "dispatch_lock_st_dev",
        "dispatch_lock_st_ino",
        "dispatch_lock_size_bytes",
        "dispatch_lock_byte_sha256",
    }
)


def _fsync_parent_directory(path: Path) -> None:
    """Durably publish a new authority filename before another process acts."""

    parent = path.parent.resolve(strict=True)
    if os.name == "nt":
        # CPython's fsync() on the CREATE_NEW file handle calls
        # FlushFileBuffers.  Windows rejects FlushFileBuffers on directory
        # handles (ERROR_ACCESS_DENIED/ERROR_INVALID_HANDLE), so there is no
        # second portable stdlib directory-fsync primitive to apply here.
        return
    else:
        descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _dispatch_lock_identity_from_handle(
    path: Path, handle: Any
) -> Mapping[str, Any]:
    resolved = path.resolve(strict=True)
    _reject_reparse_chain(resolved)
    path_stat = os.stat(resolved, follow_symlinks=False)
    handle_stat = os.fstat(handle.fileno())
    path_identity = (int(path_stat.st_dev), int(path_stat.st_ino))
    handle_identity = (int(handle_stat.st_dev), int(handle_stat.st_ino))
    handle.seek(0)
    raw = handle.read(2)
    if not (
        stat.S_ISREG(path_stat.st_mode)
        and stat.S_ISREG(handle_stat.st_mode)
        and path_identity == handle_identity
        and int(path_stat.st_size) == int(handle_stat.st_size) == 1
        and raw == STARTUP_DISPATCH_LOCK_BYTE
    ):
        raise CapturedPaperHostCutoverError(
            "STARTUP_DISPATCH_LOCK_INVALID",
            "dispatch lock path, handle identity, or fixed byte differs",
        )
    return MappingProxyType(
        {
            "dispatch_lock_path": str(resolved),
            "dispatch_lock_st_dev": path_identity[0],
            "dispatch_lock_st_ino": path_identity[1],
            "dispatch_lock_size_bytes": 1,
            "dispatch_lock_byte_sha256": STARTUP_DISPATCH_LOCK_BYTE_SHA256,
        }
    )


def create_startup_dispatch_lock(
    path: str | Path,
) -> Mapping[str, Any]:
    """O_EXCL-create and fsync the one-byte lock before PREPARED publication."""

    target = Path(path)
    target.parent.resolve(strict=True)
    _reject_reparse_chain(target.parent)
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | int(getattr(os, "O_BINARY", 0))
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "r+b", buffering=0) as handle:
            if handle.write(STARTUP_DISPATCH_LOCK_BYTE) != 1:
                raise OSError("short dispatch-lock write")
            handle.flush()
            os.fsync(handle.fileno())
            identity = _dispatch_lock_identity_from_handle(target, handle)
    except BaseException:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _fsync_parent_directory(target)
    return identity


def _validate_dispatch_lock_identity(
    value: Mapping[str, Any], *, expected_path: Path
) -> Mapping[str, Any]:
    _exact_keys(value, _DISPATCH_LOCK_IDENTITY_KEYS, "dispatch lock identity")
    path = Path(str(value.get("dispatch_lock_path") or ""))
    if (
        path != expected_path
        or type(value.get("dispatch_lock_st_dev")) is not int
        or type(value.get("dispatch_lock_st_ino")) is not int
        or int(value["dispatch_lock_st_ino"]) <= 0
        or value.get("dispatch_lock_size_bytes") != 1
        or value.get("dispatch_lock_byte_sha256")
        != STARTUP_DISPATCH_LOCK_BYTE_SHA256
    ):
        raise CapturedPaperHostCutoverError(
            "STARTUP_DISPATCH_LOCK_INVALID", "dispatch lock identity is malformed"
        )
    try:
        with path.open("r+b", buffering=0) as handle:
            observed = _dispatch_lock_identity_from_handle(path, handle)
    except OSError as exc:
        raise CapturedPaperHostCutoverError(
            "STARTUP_DISPATCH_LOCK_INVALID", "dispatch lock is unavailable"
        ) from exc
    if dict(observed) != dict(value):
        raise CapturedPaperHostCutoverError(
            "STARTUP_DISPATCH_LOCK_INVALID", "dispatch lock identity changed"
        )
    return MappingProxyType(dict(observed))


@contextmanager
def hold_startup_dispatch_lock(
    identity: Mapping[str, Any], *, timeout_seconds: float
) -> Iterable[None]:
    """Hold the exact PREPARED-bound 1-byte lock or fail closed on timeout."""

    _exact_keys(identity, _DISPATCH_LOCK_IDENTITY_KEYS, "dispatch lock identity")
    path = Path(str(identity.get("dispatch_lock_path") or ""))
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise CapturedPaperHostCutoverError(
            "STARTUP_DISPATCH_LOCK_INVALID", "dispatch lock is unavailable"
        ) from exc
    expected = MappingProxyType(dict(identity))
    if not (
        type(identity.get("dispatch_lock_st_dev")) is int
        and type(identity.get("dispatch_lock_st_ino")) is int
        and int(identity["dispatch_lock_st_ino"]) > 0
        and identity.get("dispatch_lock_size_bytes") == 1
        and identity.get("dispatch_lock_byte_sha256")
        == STARTUP_DISPATCH_LOCK_BYTE_SHA256
        and stat.S_ISREG(path_stat.st_mode)
        and (int(path_stat.st_dev), int(path_stat.st_ino))
        == (
            int(identity["dispatch_lock_st_dev"]),
            int(identity["dispatch_lock_st_ino"]),
        )
        and int(path_stat.st_size) == 1
    ):
        raise CapturedPaperHostCutoverError(
            "STARTUP_DISPATCH_LOCK_INVALID", "dispatch lock identity is malformed"
        )
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    try:
        handle = path.open("r+b", buffering=0)
    except OSError as exc:
        raise CapturedPaperHostCutoverError(
            "STARTUP_DISPATCH_LOCK_INVALID", "dispatch lock could not be opened"
        ) from exc
    locked = False
    try:
        handle_stat = os.fstat(handle.fileno())
        if (
            (int(handle_stat.st_dev), int(handle_stat.st_ino))
            != (
                int(expected["dispatch_lock_st_dev"]),
                int(expected["dispatch_lock_st_ino"]),
            )
            or int(handle_stat.st_size) != 1
        ):
            raise CapturedPaperHostCutoverError(
                "STARTUP_DISPATCH_LOCK_INVALID", "dispatch lock changed before acquire"
            )
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError) as exc:
                if time.monotonic() >= deadline:
                    raise CapturedPaperHostCutoverError(
                        "STARTUP_DISPATCH_LOCK_TIMEOUT",
                        "dispatch lock remained owned past the revocation deadline",
                    ) from exc
                time.sleep(0.01)
        if dict(_dispatch_lock_identity_from_handle(path, handle)) != dict(expected):
            raise CapturedPaperHostCutoverError(
                "STARTUP_DISPATCH_LOCK_INVALID", "dispatch lock changed after acquire"
            )
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        else:
            handle.close()


def _publish_revocation_requested(
    *, path: Path, value: Mapping[str, Any]
) -> str:
    """Publish the durable deny-new-dispatch intent before waiting on POST."""

    if path.exists():
        raw = path.read_bytes()
        existing = _strict_json(raw, "startup revocation request")
        claimed = _sha(existing.get("receipt_sha256"), "revocation request hash")
        body = dict(existing)
        body.pop("receipt_sha256", None)
        retry_variant_fields = {"receipt_sha256", "requested_at", "reason"}
        if not (
            raw == _canonical_json_bytes(existing)
            and sha256_json(body) == claimed
            and set(existing) == set(value)
            and all(
                existing.get(field) == value.get(field)
                for field in set(value) - retry_variant_fields
            )
            and existing.get("schema_version")
            == "chili.captured-paper-host-revocation-requested.v1"
            and existing.get("state") == "REVOCATION_REQUESTED"
            and existing.get("live_cash_authorized") is False
            and existing.get("real_money_authorized") is False
        ):
            raise CapturedPaperHostCutoverError(
                "STARTUP_REVOCATION_REPLAY",
                "foreign or malformed revocation request already exists",
            )
        return claimed
    _atomic_publish_canonical_json(path, value)
    _fsync_parent_directory(path)
    return _sha(value.get("receipt_sha256"), "revocation request hash")


def _publish_final_revocation_under_dispatch_lock(
    *,
    path: Path,
    value: Mapping[str, Any],
    lock_identity: Mapping[str, Any],
) -> Mapping[str, Any]:
    with hold_startup_dispatch_lock(
        lock_identity, timeout_seconds=STARTUP_DISPATCH_LOCK_WAIT_SECONDS
    ):
        if path.exists():
            raw = path.read_bytes()
            existing = _strict_json(raw, "startup revocation tombstone")
            existing_body = dict(existing)
            existing_claimed = _sha(
                existing_body.pop("receipt_sha256", None),
                "startup revocation tombstone hash",
            )
            retry_variant_fields = {"receipt_sha256", "revoked_at", "reason"}
            if not (
                raw == _canonical_json_bytes(existing)
                and sha256_json(existing_body) == existing_claimed
                and set(existing) == set(value)
                and all(
                    existing.get(field) == value.get(field)
                    for field in set(value) - retry_variant_fields
                )
            ):
                raise CapturedPaperHostCutoverError(
                    "STARTUP_REVOCATION_REPLAY",
                    "foreign or malformed revocation tombstone already exists",
                )
            return MappingProxyType(dict(existing))
        _atomic_publish_canonical_json(path, value)
        _fsync_parent_directory(path)
        return MappingProxyType(dict(value))


def _atomic_publish_canonical_json(path: Path, value: Mapping[str, Any]) -> str:
    raw = _canonical_json_bytes(value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_reparse_chain(path.parent)
    try:
        with path.open("xb") as handle:
            written = handle.write(raw)
            if written != len(raw):
                raise OSError("short startup-authority write")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise CapturedPaperHostCutoverError(
            "STARTUP_AUTHORITY_REPLAY", f"startup authority path already exists: {path}"
        ) from exc
    if path.read_bytes() != raw:
        raise CapturedPaperHostCutoverError(
            "STARTUP_AUTHORITY_DRIFT", "startup authority failed exact readback"
        )
    return sha256_bytes(raw)


def _stage_immutable_runtime_copy(
    *, source: str, target: str, expected_sha256: str, field: str
) -> None:
    source_path, source_sha = _stable_local_file_unrooted(source, field=f"{field} source")
    expected = _sha(expected_sha256, f"{field} expected hash")
    if source_sha != expected:
        raise CapturedPaperHostCutoverError(
            "RUNTIME_STAGING_SOURCE_DRIFT", f"{field} source changed before staging"
        )
    raw = source_path.read_bytes()
    target_path = Path(target)
    target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_reparse_chain(target_path.parent)
    if target_path.exists():
        _reject_reparse_chain(target_path)
        if not target_path.is_file() or target_path.read_bytes() != raw:
            raise CapturedPaperHostCutoverError(
                "RUNTIME_STAGING_REPLAY",
                f"{field} target preexists with different bytes",
            )
    else:
        try:
            with target_path.open("xb") as handle:
                written = handle.write(raw)
                if written != len(raw):
                    raise OSError("short immutable runtime write")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise CapturedPaperHostCutoverError(
                "RUNTIME_STAGING_REPLAY", f"{field} target raced into existence"
            ) from exc
    try:
        os.chmod(target_path, stat.S_IREAD)
    except OSError as exc:
        raise CapturedPaperHostCutoverError(
            "RUNTIME_STAGING_IMMUTABILITY_FAILED", f"{field} could not be sealed read-only"
        ) from exc
    staged, staged_sha = _stable_local_file_unrooted(target_path, field=f"{field} staged")
    if staged_sha != expected or staged != target_path.resolve(strict=True):
        raise CapturedPaperHostCutoverError(
            "RUNTIME_STAGING_DRIFT", f"{field} staged bytes differ"
        )


_ISSUER_PROVENANCE_KEYS = frozenset(
    {
        "issuer_pid",
        "issuer_create_time_ns",
        "issuer_executable_path",
        "issuer_executable_sha256",
        "issuer_cmdline",
        "issuer_cmdline_sha256",
        "issuer_source_path",
        "issuer_source_sha256",
    }
)


def _validate_apply_issuer_cmdline(
    cmdline: Sequence[str],
    *,
    executable_path: Path,
    source_path: Path,
    prepared: PreparedCutover,
    journal_root: Path,
) -> tuple[str, ...]:
    """Bind permit issuance to this script's exact, explicit Apply command.

    A process that merely imports this module (including ``python -c`` or
    ``python -m``) must not be able to mint a host activation permit.  The
    direct script path and every authority-bearing CLI argument are required
    exactly once.  Keeping this parser deliberately narrower than argparse is
    intentional: alternate spellings and implicit/default authority are not
    accepted at the irreversible authorization boundary.
    """

    values = tuple(str(item) for item in cmdline)
    if not values or any(not item or "\x00" in item for item in values):
        raise CapturedPaperHostCutoverError(
            "ISSUER_CMDLINE_INVALID", "cutover issuer argv is empty or malformed"
        )
    if os.path.normcase(values[0]) != os.path.normcase(str(executable_path)):
        raise CapturedPaperHostCutoverError(
            "ISSUER_CMDLINE_INVALID", "cutover issuer argv[0] is not its executable"
        )
    if len(values) < 19:
        raise CapturedPaperHostCutoverError(
            "ISSUER_CMDLINE_INVALID", "cutover issuer argv lacks isolated stage0"
        )
    expected_prefix = (
        "-I",
        "-S",
        "-B",
        prepared.invocation.stage0_script_path,
        "--manifest",
        str(prepared.manifest_path),
        "--manifest-sha256",
        prepared.manifest_sha256,
        "--candidate-root",
        str(prepared.candidate_root),
        "--target-role",
        "captured_paper_host_cutover",
        "--target",
        str(source_path),
        "--target-sha256",
        prepared.invocation.stage0_source_sha256
        if source_path == Path(prepared.invocation.stage0_source_path)
        else sha256_bytes(source_path.read_bytes()),
        "--",
    )
    if values[1:18] != expected_prefix:
        raise CapturedPaperHostCutoverError(
            "ISSUER_CMDLINE_INVALID",
            "permit issuer did not use the exact isolated stage0 envelope",
        )
    supplied_source = Path(values[14])
    if (
        not supplied_source.is_absolute()
        or os.path.normcase(str(supplied_source))
        != os.path.normcase(str(source_path))
    ):
        raise CapturedPaperHostCutoverError(
            "ISSUER_CMDLINE_INVALID",
            "permit issuer must directly execute the exact host-cutover source",
        )

    option_values: dict[str, list[str]] = {}
    arguments = values[18:]
    if len(arguments) % 2:
        raise CapturedPaperHostCutoverError(
            "ISSUER_CMDLINE_INVALID", "cutover issuer argv is not exact flag/value pairs"
        )
    allowed = {
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
    for index in range(0, len(arguments), 2):
        option, value = arguments[index : index + 2]
        if option not in allowed or not value:
            raise CapturedPaperHostCutoverError(
                "ISSUER_CMDLINE_INVALID", "cutover issuer argv contains an unknown option"
            )
        option_values.setdefault(option, []).append(value)
    required_once = allowed - {"--allow-read-root"}
    if (
        set(option_values) != allowed
        or any(len(option_values.get(option, ())) != 1 for option in required_once)
        or not option_values.get("--allow-read-root")
    ):
        raise CapturedPaperHostCutoverError(
            "ISSUER_CMDLINE_INVALID",
            "cutover issuer argv omitted or repeated an authority-bearing option",
        )

    expected_paths = {
        "--manifest": prepared.manifest_path,
        "--candidate-root": prepared.candidate_root,
        "--journal-root": journal_root,
    }
    for option, expected in expected_paths.items():
        supplied = Path(option_values[option][0])
        if (
            not supplied.is_absolute()
            or os.path.normcase(str(supplied.resolve(strict=True)))
            != os.path.normcase(str(expected.resolve(strict=True)))
        ):
            raise CapturedPaperHostCutoverError(
                "ISSUER_CMDLINE_INVALID", f"cutover issuer {option} differs from authority"
            )
    for option in (
        "--task-snapshot",
        "--process-snapshot",
        "--restore-plan",
        "--candidate-task-template",
        "--candidate-action",
    ):
        supplied = Path(option_values[option][0])
        if (
            not supplied.is_absolute()
            or not supplied.resolve(strict=True).is_file()
            or not _inside(supplied.resolve(strict=True), prepared.allowed_read_roots)
        ):
            raise CapturedPaperHostCutoverError(
                "ISSUER_CMDLINE_INVALID",
                f"cutover issuer {option} is not a sealed local input",
            )
    supplied_roots = tuple(
        sorted(
            os.path.normcase(str(Path(item).resolve(strict=True)))
            for item in option_values["--allow-read-root"]
            if Path(item).is_absolute()
        )
    )
    expected_roots = tuple(
        sorted(
            os.path.normcase(str(item.resolve(strict=True)))
            for item in prepared.allowed_read_roots
        )
    )
    if supplied_roots != expected_roots:
        raise CapturedPaperHostCutoverError(
            "ISSUER_CMDLINE_INVALID", "cutover issuer allowed roots differ from authority"
        )
    if not (
        option_values["--mode"][0] == MODE_APPLY
        and option_values["--manifest-sha256"][0] == prepared.manifest_sha256
        and option_values["--confirm-fake-money-paper"][0] == APPLY_CONFIRMATION
    ):
        raise CapturedPaperHostCutoverError(
            "ISSUER_CMDLINE_INVALID",
            "cutover issuer is not the exact fake-money PAPER Apply command",
        )
    return values


def _validate_issuer_provenance(
    value: Mapping[str, Any],
    *,
    prepared: PreparedCutover,
    journal_root: Path,
) -> Mapping[str, Any]:
    normalized = _validate_recorded_issuer_provenance(value)
    value = normalized
    executable, executable_sha = _stable_local_file_unrooted(
        str(value.get("issuer_executable_path") or ""),
        field="cutover issuer executable",
    )
    source, source_sha = _stable_local_file_unrooted(
        str(value.get("issuer_source_path") or ""), field="cutover issuer source"
    )
    cmdline = _validate_apply_issuer_cmdline(
        tuple(str(item) for item in value["issuer_cmdline"]),
        executable_path=executable,
        source_path=source,
        prepared=prepared,
        journal_root=journal_root,
    )
    if not (
        executable_sha == value.get("issuer_executable_sha256")
        and source_sha == value.get("issuer_source_sha256")
        and source == Path(__file__).resolve(strict=True)
    ):
        raise CapturedPaperHostCutoverError(
            "ISSUER_PROVENANCE_INVALID", "cutover issuer provenance changed"
        )
    return MappingProxyType(dict(normalized))


def _validate_recorded_issuer_provenance(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate frozen issuer fields without requiring old files to persist.

    Emergency rollback must still revoke an issued permit after deployment
    files have moved or changed.  The journal hash chain authenticates the
    frozen fields; current-file and live-argv checks are performed when the
    permit is issued and again by the service before consumption.
    """

    _exact_keys(value, _ISSUER_PROVENANCE_KEYS, "cutover issuer provenance")
    if (
        type(value.get("issuer_pid")) is not int
        or int(value["issuer_pid"]) <= 0
        or type(value.get("issuer_create_time_ns")) is not int
        or int(value["issuer_create_time_ns"]) <= 0
        or not isinstance(value.get("issuer_cmdline"), (list, tuple))
    ):
        raise CapturedPaperHostCutoverError(
            "ISSUER_PROVENANCE_INVALID", "cutover issuer process identity is incomplete"
        )
    executable_path = Path(str(value.get("issuer_executable_path") or ""))
    source_path = Path(str(value.get("issuer_source_path") or ""))
    cmdline = tuple(str(item) for item in value["issuer_cmdline"])
    if not (
        _is_local_absolute(executable_path)
        and _is_local_absolute(source_path)
        and _sha(value.get("issuer_executable_sha256"), "issuer executable hash")
        and _sha(value.get("issuer_source_sha256"), "issuer source hash")
        and sha256_json(list(cmdline))
        == _sha(value.get("issuer_cmdline_sha256"), "issuer argv hash")
    ):
        raise CapturedPaperHostCutoverError(
            "ISSUER_PROVENANCE_INVALID", "cutover issuer provenance changed"
        )
    normalized = dict(value)
    normalized["issuer_cmdline"] = list(cmdline)
    return MappingProxyType(normalized)


def _issuer_provenance(
    *, prepared: PreparedCutover, journal_root: Path
) -> Mapping[str, Any]:
    try:
        import psutil  # type: ignore

        process = psutil.Process(os.getpid())
        create_time_ns = int(round(float(process.create_time()) * 1_000_000_000))
        cmdline = tuple(str(item) for item in process.cmdline())
        process_executable = str(Path(process.exe()).resolve(strict=True))
    except (ImportError, OSError, ValueError) as exc:
        raise CapturedPaperHostCutoverError(
            "ISSUER_PROVENANCE_UNAVAILABLE",
            "cutover issuer process identity could not be inspected",
        ) from exc
    except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied) as exc:
        raise CapturedPaperHostCutoverError(
            "ISSUER_PROVENANCE_UNAVAILABLE",
            "cutover issuer process identity could not be inspected",
        ) from exc
    if create_time_ns <= 0 or not cmdline:
        raise CapturedPaperHostCutoverError(
            "ISSUER_PROVENANCE_INVALID",
            "cutover issuer process identity is incomplete",
        )
    executable, executable_sha = _stable_local_file_unrooted(
        process_executable, field="cutover issuer executable"
    )
    source, source_sha = _stable_local_file_unrooted(
        __file__, field="cutover issuer source"
    )
    if os.path.normcase(str(executable)) != os.path.normcase(
        str(Path(sys.executable).resolve(strict=True))
    ):
        raise CapturedPaperHostCutoverError(
            "ISSUER_PROVENANCE_INVALID",
            "cutover issuer process executable differs from this interpreter",
        )
    return _validate_issuer_provenance(
        {
            "issuer_pid": os.getpid(),
            "issuer_create_time_ns": create_time_ns,
            "issuer_executable_path": str(executable),
            "issuer_executable_sha256": executable_sha,
            "issuer_cmdline": list(cmdline),
            "issuer_cmdline_sha256": sha256_json(list(cmdline)),
            "issuer_source_path": str(source),
            "issuer_source_sha256": source_sha,
        },
        prepared=prepared,
        journal_root=journal_root,
    )


_PERMIT_AUTHORIZATION_PAYLOAD_KEYS = frozenset(
    {
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
        "issued_at",
        "valid_until",
        "permit_path",
        *_DISPATCH_LOCK_IDENTITY_KEYS,
        *_ISSUER_PROVENANCE_KEYS,
        "live_cash_authorized",
        "real_money_authorized",
    }
)

_ACTIVATION_PERMIT_KEYS = frozenset(
    {
        "schema_version",
        "state",
        *_PERMIT_AUTHORIZATION_PAYLOAD_KEYS,
        "journal_path",
        "journal_transaction_id",
        "journal_authorization_sequence",
        "journal_authorization_event_sha256",
        "journal_authorization_event",
        "permit_sha256",
    }
)


def _permit_authorization_payload_from_document(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    return MappingProxyType(
        {key: value.get(key) for key in _PERMIT_AUTHORIZATION_PAYLOAD_KEYS}
    )


def _validate_activation_permit_against_journal(
    value: Mapping[str, Any],
    *,
    journal: CutoverJournal,
    prepared: PreparedCutover,
    permit_path: Path,
    service: ProcessIdentity | None = None,
    verify_current_issuer: bool = True,
) -> str:
    """Re-bind a permit to the exact durable journal event that issued it."""

    _exact_keys(value, _ACTIVATION_PERMIT_KEYS, "activation permit")
    claimed = _sha(value.get("permit_sha256"), "activation permit self hash")
    body = dict(value)
    body.pop("permit_sha256")
    embedded = _mapping(
        value.get("journal_authorization_event"),
        "activation permit journal authorization event",
    )
    sequence = value.get("journal_authorization_sequence")
    if type(sequence) is not int or sequence <= 0 or sequence > len(journal.events):
        raise CapturedPaperHostCutoverError(
            "STARTUP_PERMIT_JOURNAL_MISMATCH",
            "activation permit journal sequence is not present",
        )
    actual = journal.events[sequence - 1]
    event_payload = _mapping(actual.get("payload"), "activation permit event payload")
    expected_payload = _permit_authorization_payload_from_document(value)
    issuer = {key: value.get(key) for key in _ISSUER_PROVENANCE_KEYS}
    normalized_issuer = (
        _validate_issuer_provenance(
            issuer, prepared=prepared, journal_root=journal.root
        )
        if verify_current_issuer
        else _validate_recorded_issuer_provenance(issuer)
    )
    service_cmdline = value.get("service_cmdline")
    if not isinstance(service_cmdline, list) or any(
        not isinstance(item, str) or not item for item in service_cmdline
    ):
        raise CapturedPaperHostCutoverError(
            "STARTUP_PERMIT_INVALID", "activation permit service argv is malformed"
        )
    paths = _startup_handshake_paths(
        prepared.invocation, roots=prepared.allowed_read_roots
    )
    lock_identity = _validate_dispatch_lock_identity(
        {key: value.get(key) for key in _DISPATCH_LOCK_IDENTITY_KEYS},
        expected_path=paths["dispatch_lock"],
    )
    service_matches = True
    if service is not None:
        service_matches = (
            value.get("service_pid") == service.pid
            and value.get("service_create_time_ns") == service.create_time_ns
            and os.path.normcase(str(value.get("service_executable_path") or ""))
            == os.path.normcase(service.executable_path)
            and value.get("service_executable_sha256")
            == service.executable_sha256
            and tuple(service_cmdline) == service.cmdline
            and value.get("service_cmdline_sha256") == service.cmdline_sha256
            and value.get("service_role") == service.role == "candidate_service"
        )
    issued_at = _parse_utc(value.get("issued_at"), "activation permit issued_at")
    valid_until = _parse_utc(
        value.get("valid_until"), "activation permit valid_until"
    )
    if not (
        sha256_json(body) == claimed
        and value.get("schema_version") == STARTUP_PERMIT_SCHEMA
        and value.get("state") == "ACTIVATION_PERMITTED"
        and value.get("activation_generation") == prepared.activation_generation
        and value.get("manifest_path") == str(prepared.manifest_path)
        and value.get("manifest_sha256") == prepared.manifest_sha256
        and value.get("candidate_root") == str(prepared.candidate_root)
        and value.get("journal_root") == str(journal.root)
        and value.get("account_scope") == "alpaca:paper"
        and value.get("expected_account_id") == prepared.expected_account_id
        and value.get("service_script_path")
        == prepared.invocation.service_script_path
        and value.get("service_script_sha256")
        == prepared.invocation.service_script_sha256
        and sha256_json(service_cmdline)
        == value.get("service_cmdline_sha256")
        and service_matches
        and value.get("challenge_sha256")
        == _sha(value.get("challenge_sha256"), "activation permit challenge")
        and value.get("prepared_receipt_sha256")
        == _sha(
            value.get("prepared_receipt_sha256"),
            "activation permit PREPARED receipt",
        )
        and issued_at < valid_until
        and (valid_until - issued_at).total_seconds()
        <= STARTUP_HANDSHAKE_MAX_AGE_SECONDS
        and Path(str(value.get("permit_path") or "")) == permit_path
        and Path(str(value.get("journal_path") or "")) == journal.path
        and value.get("journal_transaction_id") == journal.transaction_id
        and value.get("journal_authorization_event_sha256")
        == actual.get("event_sha256")
        and embedded == actual
        and actual.get("event_type") == "activation_permit_issued"
        and actual.get("sequence") == sequence
        and event_payload == expected_payload
        and all(value.get(key) == lock_identity.get(key) for key in lock_identity)
        and dict(normalized_issuer) == issuer
        and value.get("live_cash_authorized") is False
        and value.get("real_money_authorized") is False
    ):
        raise CapturedPaperHostCutoverError(
            "STARTUP_PERMIT_JOURNAL_MISMATCH",
            "activation permit is not bound to its exact durable issuance event",
        )
    return claimed


class CapturedPaperHostCutoverExecutor:
    """Deterministic state machine over a dependency-injected host backend."""

    def __init__(
        self,
        *,
        prepared: PreparedCutover,
        backend: HostCutoverBackend,
        journal_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        self.prepared = prepared
        self.backend = backend
        self.clock = clock
        self.monotonic_clock = monotonic_clock
        self.wait = wait
        self.journal_root = _strict_existing_dir(
            journal_root,
            roots=prepared.allowed_read_roots,
            field="journal_root",
        )

    def validate_only(self) -> CutoverReport:
        """Read-only comparison against the receipt-sealed baseline."""

        self._assert_baseline()
        return CutoverReport(
            mode=MODE_VALIDATE_ONLY,
            verdict="VALIDATED_NO_HOST_MUTATION",
            activation_generation=self.prepared.activation_generation,
            manifest_sha256=self.prepared.manifest_sha256,
            resolved_task_xml_sha256=self.prepared.resolved_task_xml_sha256,
            journal_path=None,
            mutation_count=0,
        )

    def _await_legacy_paper_broker_quiet_horizon(
        self,
        *,
        journal: CutoverJournal,
        expected_lane: LegacyExecutionLaneObservation,
    ) -> None:
        """Hold the sealed PAPER-only quiet horizon with local authority absent."""

        required = float(LEGACY_PAPER_BROKER_QUIET_HORIZON_SECONDS)
        if not (required > 0.0 and required <= 300.0):
            raise CapturedPaperHostCutoverError(
                "BROKER_QUIET_HORIZON_INVALID",
                "sealed PAPER quiet horizon is outside the host contract",
            )

        def probe(field: str) -> tuple[LegacyExecutionLaneObservation, str]:
            lane = self.backend.inspect_legacy_execution_lane()
            lane_document = dict(_legacy_execution_lane_document(lane))
            if (
                lane.identity_key() != expected_lane.identity_key()
                or lane.state != "stopped"
                or any(item.enabled for item in lane.recreator_tasks)
                or self.backend.find_legacy_processes(
                    self.prepared.restore_plan.bindings
                )
                or self.backend.await_execution_lane_recreator_processes(
                    timeout_seconds=0.0
                )
            ):
                raise CapturedPaperHostCutoverError(
                    "BROKER_QUIET_HORIZON_INTERRUPTED",
                    f"legacy execution authority reappeared during {field}",
                )
            return lane, sha256_json(lane_document)

        _first_lane, first_lane_sha = probe("first zero-authority probe")
        first_zero_at = self.clock()
        _iso(first_zero_at)
        first_zero_at = first_zero_at.astimezone(UTC)
        started = float(self.monotonic_clock())
        self.wait(required)
        elapsed = float(self.monotonic_clock()) - started
        if not elapsed >= required:
            raise CapturedPaperHostCutoverError(
                "BROKER_QUIET_HORIZON_INCOMPLETE",
                "PAPER quiet horizon returned before its sealed duration",
            )
        _last_lane, last_lane_sha = probe("last zero-authority probe")
        last_zero_at = self.clock()
        _iso(last_zero_at)
        last_zero_at = last_zero_at.astimezone(UTC)
        if first_lane_sha != last_lane_sha or last_zero_at < first_zero_at:
            raise CapturedPaperHostCutoverError(
                "BROKER_QUIET_HORIZON_DRIFT",
                "legacy execution identity changed during the PAPER quiet horizon",
            )
        journal.append(
            "legacy_paper_broker_quiet_horizon_completed",
            {
                "policy": LEGACY_PAPER_BROKER_QUIET_HORIZON_POLICY,
                "assumption_bound": True,
                "live_cash_certification": False,
                "required_seconds": required,
                "observed_monotonic_seconds": elapsed,
                "stabilized_probe_count": 2,
                "first_zero_at": _iso(first_zero_at),
                "last_zero_at": _iso(last_zero_at),
                "legacy_execution_lane_sha256": last_lane_sha,
                "legacy_process_count": 0,
                "recreator_process_count": 0,
            },
        )

    def apply(self) -> CutoverReport:
        journal = CutoverJournal(
            root=self.journal_root, prepared=self.prepared, clock=self.clock
        )
        with journal.lock():
            # Re-read after acquiring the cross-process lock.
            journal._events = journal._read_events()
            state = _journal_state(journal.events)
            if state == "applied":
                prior_lane = _legacy_execution_lane_baseline(journal.events)
                try:
                    self._assert_applied(prior_lane)
                except BaseException as exc:
                    try:
                        self._rollback(
                            journal, reason="recover_applied_postcondition_failure"
                        )
                    except BaseException as rollback_exc:
                        raise CapturedPaperHostCutoverError(
                            "COMPENSATING_ROLLBACK_FAILED",
                            "applied service failed and exact rollback also failed: "
                            f"{type(rollback_exc).__name__}",
                        ) from rollback_exc
                    raise CapturedPaperHostCutoverError(
                        "APPLIED_POSTCONDITION_RECOVERED",
                        "applied service was not healthy; legacy ownership was restored",
                    ) from exc
                return self._report(MODE_APPLY, "ALREADY_APPLIED_EXACT", journal, 0)
            if state in {"applying", "rolling_back"}:
                self._rollback(journal, reason="recover_incomplete_transaction")
                raise CapturedPaperHostCutoverError(
                    "INCOMPLETE_TRANSACTION_RECOVERED",
                    "an interrupted cutover was rolled back; rerun Apply explicitly",
                )
            if journal.events:
                self._assert_rolled_back(
                    _legacy_execution_lane_baseline(journal.events)
                )
                raise CapturedPaperHostCutoverError(
                    "FRESH_ROLLBACK_SNAPSHOT_REQUIRED",
                    "a consumed rollback snapshot cannot authorize another Apply",
                )
            prior_lane = self._assert_baseline()
            resolved_path = journal.publish_object(
                self.prepared.resolved_task_xml, kind="candidate_task_xml"
            )
            rollback_capsule_raw = _canonical_json_bytes(
                build_rollback_capsule_document(self.prepared)
            )
            rollback_capsule_path = journal.publish_object(
                rollback_capsule_raw, kind="rollback_capsule"
            )
            rollback_capsule_sha = sha256_bytes(rollback_capsule_raw)
            rollback_task_paths = {
                name: journal.publish_object(item.xml, kind="legacy_task_xml")
                for name, item in self.prepared.task_snapshot.tasks.items()
            }
            journal.append(
                "apply_started",
                {
                    "activation_generation": self.prepared.activation_generation,
                    "manifest_sha256": self.prepared.manifest_sha256,
                    "rollback_receipt_sha256": self.prepared.rollback_receipt_sha256,
                    "task_snapshot_sha256": self.prepared.task_snapshot.artifact_sha256,
                    "process_snapshot_sha256": self.prepared.process_snapshot.artifact_sha256,
                    "restore_plan_sha256": self.prepared.restore_plan.artifact_sha256,
                    "candidate_action_sha256": self.prepared.candidate_action_sha256,
                    "candidate_template_sha256": self.prepared.candidate_template_sha256,
                    "resolved_task_xml_sha256": self.prepared.resolved_task_xml_sha256,
                    "resolved_task_xml_path": str(resolved_path),
                    "rollback_capsule_path": str(rollback_capsule_path),
                    "rollback_capsule_sha256": rollback_capsule_sha,
                    "rollback_task_xml_paths": {
                        name: str(path) for name, path in rollback_task_paths.items()
                    },
                    "legacy_execution_lane": dict(
                        _legacy_execution_lane_document(prior_lane)
                    ),
                    "account_scope": "alpaca:paper",
                    "live_cash_authorized": False,
                },
            )
            mutations = 0
            try:
                mutations += self.backend.quiesce_legacy_execution_lane(
                    expected=prior_lane
                )
                quiesced_lane = self.backend.inspect_legacy_execution_lane()
                if (
                    quiesced_lane.identity_key() != prior_lane.identity_key()
                    or quiesced_lane.state != "stopped"
                    or any(
                        item.enabled for item in quiesced_lane.recreator_tasks
                    )
                ):
                    raise CapturedPaperHostCutoverError(
                        "EXECUTION_LANE_QUIESCE_FAILED",
                        "legacy Docker execution lane is not exactly quiesced",
                    )
                journal.append(
                    "legacy_execution_lane_quiesced",
                    {
                        "legacy_execution_lane": dict(
                            _legacy_execution_lane_document(quiesced_lane)
                        ),
                        "legacy_execution_lane_sha256": sha256_json(
                            dict(_legacy_execution_lane_document(quiesced_lane))
                        ),
                    },
                )
                handshake_paths = _startup_handshake_paths(
                    self.prepared.invocation,
                    roots=self.prepared.allowed_read_roots,
                )
                if any(os.path.lexists(path) for path in handshake_paths.values()):
                    raise CapturedPaperHostCutoverError(
                        "STARTUP_HANDSHAKE_REPLAY",
                        "startup handshake path preexisted task activation",
                    )
                _stage_immutable_runtime_copy(
                    source=self.prepared.invocation.launcher_source_path,
                    target=self.prepared.invocation.launcher_script_path,
                    expected_sha256=self.prepared.invocation.launcher_source_sha256,
                    field="candidate launcher",
                )
                _stage_immutable_runtime_copy(
                    source=self.prepared.invocation.stage0_source_path,
                    target=self.prepared.invocation.stage0_script_path,
                    expected_sha256=self.prepared.invocation.stage0_source_sha256,
                    field="candidate stage0",
                )
                _stage_immutable_runtime_copy(
                    source=self.prepared.invocation.service_source_path,
                    target=self.prepared.invocation.service_script_path,
                    expected_sha256=self.prepared.invocation.service_source_sha256,
                    field="candidate service",
                )
                journal.append(
                    "immutable_runtime_staged",
                    {
                        "launcher_path": self.prepared.invocation.launcher_script_path,
                        "launcher_sha256": self.prepared.invocation.launcher_script_sha256,
                        "stage0_path": self.prepared.invocation.stage0_script_path,
                        "stage0_sha256": self.prepared.invocation.stage0_script_sha256,
                        "service_path": self.prepared.invocation.service_script_path,
                        "service_sha256": self.prepared.invocation.service_script_sha256,
                    },
                )
                for name in REQUIRED_LEGACY_TASKS:
                    before_disable = self.backend.get_task(name)
                    expected_task = self.prepared.task_snapshot.tasks[name]
                    if (
                        before_disable is None
                        or before_disable.xml != expected_task.xml
                        or before_disable.enabled is not expected_task.enabled
                    ):
                        raise CapturedPaperHostCutoverError(
                            "TASK_DEFINITION_DRIFT",
                            f"legacy task {name} changed immediately before disable",
                        )
                    self.backend.set_task_enabled(name, False)
                    mutations += 1
                    observed = self.backend.get_task(name)
                    if observed is None or observed.enabled:
                        raise CapturedPaperHostCutoverError(
                            "TASK_DISABLE_FAILED", f"legacy task {name} is not disabled"
                        )
                    if _task_definition_sha_ignoring_enabled(
                        observed.xml
                    ) != _task_definition_sha_ignoring_enabled(
                        self.prepared.task_snapshot.tasks[name].xml
                    ):
                        raise CapturedPaperHostCutoverError(
                            "TASK_DEFINITION_DRIFT",
                            f"legacy task {name} changed while it was disabled",
                        )
                    journal.append(
                        "legacy_task_disabled",
                        {"task_name": name, "readback_xml_sha256": observed.xml_sha256},
                    )

                for expected in self.prepared.process_snapshot.processes:
                    actual = self.backend.get_process(expected.pid, role=expected.role)
                    if actual is None or actual.semantic_key() != expected.semantic_key():
                        raise CapturedPaperHostCutoverError(
                            "PROCESS_IDENTITY_DRIFT",
                            f"legacy {expected.role} process identity changed before stop",
                        )
                    self.backend.stop_process(expected)
                    mutations += 1
                    if self.backend.get_process(expected.pid, role=expected.role) is not None:
                        raise CapturedPaperHostCutoverError(
                            "PROCESS_STOP_FAILED",
                            f"legacy {expected.role} process survived an exact stop",
                        )
                    journal.append(
                        "legacy_process_stopped",
                        {
                            "role": expected.role,
                            "pid": expected.pid,
                            "create_time_ns": expected.create_time_ns,
                            "cmdline_sha256": expected.cmdline_sha256,
                        },
                    )
                if self.backend.find_legacy_processes(self.prepared.restore_plan.bindings):
                    raise CapturedPaperHostCutoverError(
                        "LEGACY_PROCESS_SURVIVED",
                        "a provenance-matched legacy bridge remains after shutdown",
                    )

                # The quiet horizon starts only after *every* local legacy
                # execution authority is gone.  Quiescing the Docker lane alone
                # is insufficient while the scheduled tasks and bridge
                # processes below are still able to submit PAPER orders.
                self._await_legacy_paper_broker_quiet_horizon(
                    journal=journal,
                    expected_lane=quiesced_lane,
                )
                quiet_horizon_event = journal.events[-1]
                if (
                    quiet_horizon_event.get("event_type")
                    != "legacy_paper_broker_quiet_horizon_completed"
                ):
                    raise CapturedPaperHostCutoverError(
                        "BROKER_QUIET_HORIZON_UNBOUND",
                        "quiet-horizon journal event was not durably linearized",
                    )
                quiet_horizon_event_sha256 = _sha(
                    quiet_horizon_event.get("event_sha256"),
                    "quiet-horizon journal event",
                )

                if self.backend.get_task(CANDIDATE_TASK_NAME) is not None:
                    raise CapturedPaperHostCutoverError(
                        "CANDIDATE_TASK_COLLISION",
                        "candidate task appeared after baseline validation",
                    )
                self.backend.register_task(
                    CANDIDATE_TASK_NAME,
                    resolved_path,
                    self.prepared.resolved_task_xml_sha256,
                )
                mutations += 1
                candidate = self.backend.get_task(CANDIDATE_TASK_NAME)
                # Semantic identity, not sha — the scheduler re-serializes
                # registered XML so the readback can never byte-match the
                # authored template (see _candidate_task_semantics_match).
                if (
                    candidate is None
                    or not _candidate_task_semantics_match(
                        candidate.xml, self.prepared.resolved_task_xml
                    )
                    or candidate.enabled is not True
                ):
                    raise CapturedPaperHostCutoverError(
                        "CANDIDATE_TASK_READBACK_FAILED",
                        "registered candidate task differs from resolved XML",
                    )
                journal.append(
                    "candidate_task_registered",
                    {
                        "task_name": CANDIDATE_TASK_NAME,
                        "resolved_task_xml_sha256": candidate.xml_sha256,
                    },
                )
                self.backend.start_task(CANDIDATE_TASK_NAME)
                mutations += 1
                processes = self.backend.await_candidate_processes(
                    self.prepared.invocation, timeout_seconds=15.0
                )
                self._assert_candidate_process_roster(processes)
                journal.append(
                    "candidate_task_started",
                    {
                        "task_name": CANDIDATE_TASK_NAME,
                        "candidate_processes": [
                            {
                                "kind": item.kind,
                                "pid": item.identity.pid,
                                "create_time_ns": item.identity.create_time_ns,
                                "cmdline_sha256": item.identity.cmdline_sha256,
                            }
                            for item in sorted(processes, key=lambda row: row.kind)
                        ],
                    },
                )
                service = next(
                    item.identity for item in processes if item.kind == "service"
                )
                prepared_receipt = self.backend.read_service_startup_receipt(
                    self.prepared.invocation,
                    service,
                    phase="prepared",
                    # 2026-07-17: 90s killed the first Apply to reach this
                    # point — the sealed ActivatePaper boot (env verification,
                    # capture host + IQFeed bring-up, DB binds) was alive and
                    # mid-protocol (dispatch lock written ~90s in) but not yet
                    # PREPARED.  300s covers the observed boot class and still
                    # fits inside the 10-minute receipt window measured from
                    # the smoke; a dead service aborts the wait early either
                    # way, and rollback is proven live (ROLLED_BACK_EXACT).
                    timeout_seconds=300.0,
                )
                prepared_sha, challenge, prepared_valid_until, dispatch_lock_identity = (
                    _validate_prepared_receipt(
                        prepared_receipt,
                        prepared=self.prepared,
                        service=service,
                        now=self.clock(),
                    )
                )
                # Re-evaluate every host postcondition only after PREPARED has
                # proven workers are still stopped.  No permit exists yet.
                postcondition_processes = self._assert_applied(prior_lane)
                current_service = next(
                    item.identity
                    for item in postcondition_processes
                    if item.kind == "service"
                )
                if current_service.semantic_key() != service.semantic_key():
                    raise CapturedPaperHostCutoverError(
                        "STARTUP_SERVICE_IDENTITY_DRIFT",
                        "service identity changed after PREPARED",
                    )
                _permit, permit_sha = self._issue_activation_permit(
                    journal=journal,
                    service=service,
                    prepared_receipt_sha256=prepared_sha,
                    challenge_sha256=challenge,
                    valid_until=prepared_valid_until,
                    dispatch_lock_identity=dispatch_lock_identity,
                )
                started_receipt = self.backend.read_service_startup_receipt(
                    self.prepared.invocation,
                    service,
                    phase="started",
                    timeout_seconds=15.0,
                )
                started_sha = _validate_started_receipt(
                    started_receipt,
                    prepared=self.prepared,
                    service=service,
                    now=self.clock(),
                    challenge_sha256=challenge,
                    prepared_receipt_sha256=prepared_sha,
                    activation_permit_sha256=permit_sha,
                    host_quiet_horizon_event_sha256=(
                        quiet_horizon_event_sha256
                    ),
                )
                self._assert_applied(prior_lane)
                apply_completed_at = self.clock()
                if apply_completed_at >= prepared_valid_until:
                    raise CapturedPaperHostCutoverError(
                        "HOST_ACTIVATION_PERMIT_EXPIRED",
                        "host could not commit STARTED before permit expiry",
                    )
                journal.append(
                    "apply_completed",
                    {
                        "postcondition": "one_unified_candidate_host",
                        "activation_generation": (
                            self.prepared.activation_generation
                        ),
                        "manifest_sha256": self.prepared.manifest_sha256,
                        "account_scope": "alpaca:paper",
                        "expected_account_id": self.prepared.expected_account_id,
                        "service_pid": service.pid,
                        "service_create_time_ns": service.create_time_ns,
                        "service_executable_sha256": (
                            service.executable_sha256
                        ),
                        "service_executable_path": service.executable_path,
                        "service_cmdline_sha256": service.cmdline_sha256,
                        "legacy_task_count_disabled": len(REQUIRED_LEGACY_TASKS),
                        "legacy_process_count": 0,
                        "prepared_receipt_sha256": prepared_sha,
                        "activation_permit_sha256": permit_sha,
                        "started_receipt_sha256": started_sha,
                        "active_start_authority_sha256": started_receipt[
                            "active_start_authority_sha256"
                        ],
                        "active_start_evidence_artifact_sha256": started_receipt[
                            "active_start_evidence_artifact_sha256"
                        ],
                        "host_quiet_horizon_event_sha256": (
                            quiet_horizon_event_sha256
                        ),
                        "challenge_sha256": challenge,
                        "legacy_execution_lane": dict(
                            _legacy_execution_lane_document(quiesced_lane)
                        ),
                        "legacy_execution_lane_sha256": sha256_json(
                            dict(_legacy_execution_lane_document(quiesced_lane))
                        ),
                        "paper_execution_committed": True,
                        "live_cash_authorized": False,
                        "real_money_authorized": False,
                    },
                    recorded_at=apply_completed_at,
                )
                return self._report(MODE_APPLY, "APPLIED_ALPACA_PAPER_ONLY", journal, mutations)
            except BaseException as exc:
                try:
                    journal.append(
                        "apply_failed",
                        {
                            "error_type": type(exc).__name__,
                            "error_code": getattr(exc, "code", "UNEXPECTED_FAILURE"),
                        },
                    )
                except BaseException:
                    # Journal failure must never suppress compensating host
                    # restoration.  _rollback reports journal durability only
                    # after postconditions have been restored.
                    pass
                try:
                    self._rollback(journal, reason="compensate_apply_failure")
                except BaseException as rollback_exc:
                    raise CapturedPaperHostCutoverError(
                        "COMPENSATING_ROLLBACK_FAILED",
                        f"apply failed and rollback also failed: {type(rollback_exc).__name__}",
                    ) from rollback_exc
                if isinstance(exc, CapturedPaperHostCutoverError):
                    raise
                raise CapturedPaperHostCutoverError(
                    "APPLY_FAILED_ROLLED_BACK",
                    f"host cutover failed and was rolled back: {type(exc).__name__}",
                ) from exc

    def rollback(self) -> CutoverReport:
        journal = CutoverJournal(
            root=self.journal_root, prepared=self.prepared, clock=self.clock
        )
        with journal.lock():
            journal._events = journal._read_events()
            if _journal_state(journal.events) == "baseline":
                if journal.events:
                    self._assert_rolled_back(
                        _legacy_execution_lane_baseline(journal.events)
                    )
                else:
                    self._assert_baseline()
                return self._report(
                    MODE_ROLLBACK, "ALREADY_ROLLED_BACK_EXACT", journal, 0
                )
            mutations = self._rollback(journal, reason="explicit_rollback")
            return self._report(MODE_ROLLBACK, "ROLLED_BACK_EXACT", journal, mutations)

    def adopt_pre_identity_journal_at_exact_baseline(
        self,
    ) -> CutoverReport | None:
        """Close an old pre-Docker journal only when the host is already exact.

        Early cutover schema revisions wrote ``apply_started`` before any
        Docker identity was part of the transaction.  We never invent that
        missing rollback authority.  Recovery may append one explicit
        adoption only after the capsule-bound tasks/processes, candidate
        absence, handshake revocation, and the current immutable Docker
        identity all prove the host is already at a complete legacy baseline.
        No host mutation occurs in this path.
        """

        journal = CutoverJournal(
            root=self.journal_root, prepared=self.prepared, clock=self.clock
        )
        with journal.lock():
            journal._events = journal._read_events()
            if _journal_state(journal.events) == "baseline":
                return None
            observed_sha256 = sha256_bytes(journal._observed_journal_raw)
            event_types = [
                str(event.get("event_type")) for event in journal.events
            ]
            if (
                observed_sha256
                not in _PRE_IDENTITY_JOURNAL_RECOVERY_ALLOWLIST
                or event_types
                != ["apply_started", "immutable_runtime_staged", "apply_failed"]
            ):
                return None
            failure = _mapping(
                journal.events[-1].get("payload"), "apply_failed.payload"
            )
            if (
                set(failure) != {"error_code", "error_type"}
                or failure.get("error_code") != "TASK_XML_INVALID"
                or failure.get("error_type")
                != "CapturedPaperHostCutoverError"
            ):
                return None
            started = [
                event
                for event in journal.events
                if event.get("event_type") == "apply_started"
            ]
            if len(started) != 1:
                return None
            payload = _mapping(started[0].get("payload"), "apply_started.payload")
            if payload.get("legacy_execution_lane") is not None:
                return None
            if any(
                event.get("event_type")
                == "legacy_execution_lane_identity_adopted"
                for event in journal.events
            ):
                return None
            prepared = self._rollback_material(journal)
            lane = _parse_legacy_execution_lane(
                _legacy_execution_lane_document(
                    self.backend.inspect_legacy_execution_lane()
                ),
                field="legacy recovery current Docker lane",
            )
            self._assert_rolled_back_with(
                prepared, prior_execution_lane=lane
            )
            journal.append(
                "legacy_execution_lane_identity_adopted",
                {
                    "legacy_execution_lane": dict(
                        _legacy_execution_lane_document(lane)
                    ),
                    "reason": (
                        "pre_identity_schema_journal_already_at_exact_baseline"
                    ),
                    "host_baseline_verified": True,
                },
            )
            journal.append(
                "rollback_started",
                {
                    "reason": "adopt_pre_identity_journal_exact_baseline",
                    "candidate_task_name": CANDIDATE_TASK_NAME,
                    "live_cash_authorized": False,
                },
            )
            journal.append(
                "rollback_completed",
                {
                    "restored_task_count": len(REQUIRED_LEGACY_TASKS),
                    "restored_process_roles": sorted(
                        item.role for item in prepared.restore_plan.bindings
                    ),
                    "candidate_task_absent": True,
                    "host_mutation_count": 0,
                    "legacy_identity_adoption_only": True,
                },
            )
            return self._report(
                MODE_RECOVER_ONLY, "ROLLED_BACK_EXACT", journal, 0
            )

    def _report(
        self, mode: str, verdict: str, journal: CutoverJournal, mutation_count: int
    ) -> CutoverReport:
        return CutoverReport(
            mode=mode,
            verdict=verdict,
            activation_generation=self.prepared.activation_generation,
            manifest_sha256=self.prepared.manifest_sha256,
            resolved_task_xml_sha256=self.prepared.resolved_task_xml_sha256,
            journal_path=journal.path,
            mutation_count=mutation_count,
        )

    def _assert_baseline(self) -> LegacyExecutionLaneObservation:
        handshake_paths = _startup_handshake_paths(
            self.prepared.invocation, roots=self.prepared.allowed_read_roots
        )
        if any(os.path.lexists(path) for path in handshake_paths.values()):
            raise CapturedPaperHostCutoverError(
                "STARTUP_HANDSHAKE_REPLAY", "startup handshake evidence already exists"
            )
        for name in REQUIRED_LEGACY_TASKS:
            expected = self.prepared.task_snapshot.tasks[name]
            observed = self.backend.get_task(name)
            if (
                observed is None
                or observed.xml != expected.xml
                or observed.xml_sha256 != expected.xml_sha256
                or observed.enabled is not expected.enabled
            ):
                raise CapturedPaperHostCutoverError(
                    "LEGACY_TASK_SNAPSHOT_DRIFT",
                    f"legacy task {name} differs from rollback receipt snapshot",
                )
        launch_contracts = (
            self.prepared.restore_plan.launch_contracts
            or _derive_direct_launch_contracts(
                tasks=self.prepared.task_snapshot.tasks,
                bindings=self.prepared.restore_plan.bindings,
            )
        )
        for contract in launch_contracts.values():
            _assert_launch_contract_sources_current(
                contract, roots=self.prepared.allowed_read_roots
            )
        expected_processes = {
            item.semantic_key(): item for item in self.prepared.process_snapshot.processes
        }
        for expected in self.prepared.process_snapshot.processes:
            actual = self.backend.get_process(expected.pid, role=expected.role)
            if actual is None or actual.semantic_key() != expected.semantic_key():
                raise CapturedPaperHostCutoverError(
                    "LEGACY_PROCESS_SNAPSHOT_DRIFT",
                    f"legacy {expected.role} process differs from rollback snapshot",
                )
        discovered = self.backend.find_legacy_processes(
            self.prepared.restore_plan.bindings
        )
        if {item.semantic_key(): item for item in discovered} != expected_processes:
            raise CapturedPaperHostCutoverError(
                "LEGACY_PROCESS_ROSTER_DRIFT",
                "legacy process roster contains missing or additional bridge owners",
            )
        if self.backend.get_task(CANDIDATE_TASK_NAME) is not None:
            raise CapturedPaperHostCutoverError(
                "CANDIDATE_TASK_COLLISION",
                "a candidate task already exists outside this transaction",
            )
        if self.backend.find_candidate_tasks(self.prepared.invocation):
            raise CapturedPaperHostCutoverError(
                "CANDIDATE_TASK_COLLISION",
                "another scheduled task owns the exact candidate invocation",
            )
        if self.backend.await_candidate_processes(
            self.prepared.invocation, timeout_seconds=0.0
        ):
            raise CapturedPaperHostCutoverError(
                "CANDIDATE_PROCESS_COLLISION",
                "a candidate captured PAPER process already exists",
            )
        lane = self.backend.inspect_legacy_execution_lane()
        # Round-trip through the strict public document parser so a backend
        # cannot smuggle an incomplete or noncanonical identity into the
        # durable rollback authority.
        return _parse_legacy_execution_lane(
            _legacy_execution_lane_document(lane),
            field="legacy execution lane baseline",
        )

    @staticmethod
    def _assert_candidate_process_roster(
        processes: Sequence[CandidateProcessObservation],
    ) -> None:
        CapturedPaperHostCutoverExecutor._assert_candidate_process_subset(processes)
        kinds = [item.kind for item in processes]
        if sorted(kinds) != ["launcher", "service"]:
            raise CapturedPaperHostCutoverError(
                "CANDIDATE_PROCESS_ROSTER_INVALID",
                "candidate must have exactly one launcher and one foreground service",
            )
        if len({item.identity.pid for item in processes}) != 2:
            raise CapturedPaperHostCutoverError(
                "CANDIDATE_PROCESS_ROSTER_INVALID", "candidate process IDs are not unique"
            )

    @staticmethod
    def _assert_candidate_process_subset(
        processes: Sequence[CandidateProcessObservation],
    ) -> None:
        kinds = [item.kind for item in processes]
        if (
            any(kind not in {"launcher", "service"} for kind in kinds)
            or len(kinds) != len(set(kinds))
            or len({item.identity.pid for item in processes}) != len(processes)
        ):
            raise CapturedPaperHostCutoverError(
                "CANDIDATE_PROCESS_ROSTER_INVALID",
                "candidate process inventory is ambiguous",
            )

    def _assert_applied(
        self, prior_execution_lane: LegacyExecutionLaneObservation
    ) -> tuple[CandidateProcessObservation, ...]:
        lane = self.backend.inspect_legacy_execution_lane()
        if (
            lane.identity_key() != prior_execution_lane.identity_key()
            or lane.state != "stopped"
            or any(item.enabled for item in lane.recreator_tasks)
        ):
            raise CapturedPaperHostCutoverError(
                "APPLIED_POSTCONDITION_FAILED",
                "legacy Docker execution lane is no longer exactly quiesced",
            )
        launcher_path, launcher_sha = _stable_local_file_unrooted(
            self.prepared.invocation.launcher_script_path,
            field="applied candidate launcher",
        )
        if (
            launcher_sha != self.prepared.invocation.launcher_script_sha256
            or launcher_path.name.casefold() != f"{launcher_sha}.ps1".casefold()
        ):
            raise CapturedPaperHostCutoverError(
                "APPLIED_POSTCONDITION_FAILED",
                "the executed candidate launcher is not the sealed content-addressed file",
            )
        stage0_path, stage0_sha = _stable_local_file_unrooted(
            self.prepared.invocation.stage0_script_path,
            field="applied candidate stage0",
        )
        if (
            stage0_sha != self.prepared.invocation.stage0_script_sha256
            or stage0_path.name.casefold() != f"{stage0_sha}.py".casefold()
        ):
            raise CapturedPaperHostCutoverError(
                "APPLIED_POSTCONDITION_FAILED",
                "the candidate stage0 is not the sealed staged file",
            )
        service_path, service_sha = _stable_local_file_unrooted(
            self.prepared.invocation.service_script_path,
            field="applied candidate service",
        )
        if (
            service_sha != self.prepared.invocation.service_script_sha256
            or service_path.name.casefold() != f"{service_sha}.py".casefold()
        ):
            raise CapturedPaperHostCutoverError(
                "APPLIED_POSTCONDITION_FAILED",
                "the candidate service is not the sealed staged file",
            )
        for name in REQUIRED_LEGACY_TASKS:
            observed = self.backend.get_task(name)
            if observed is None or observed.enabled:
                raise CapturedPaperHostCutoverError(
                    "APPLIED_POSTCONDITION_FAILED", f"legacy task {name} is not disabled"
                )
            if _task_definition_sha_ignoring_enabled(
                observed.xml
            ) != _task_definition_sha_ignoring_enabled(
                self.prepared.task_snapshot.tasks[name].xml
            ):
                raise CapturedPaperHostCutoverError(
                    "APPLIED_POSTCONDITION_FAILED",
                    f"legacy task {name} definition drifted",
                )
        if self.backend.find_legacy_processes(self.prepared.restore_plan.bindings):
            raise CapturedPaperHostCutoverError(
                "APPLIED_POSTCONDITION_FAILED", "legacy bridge process remains"
            )
        candidate = self.backend.get_task(CANDIDATE_TASK_NAME)
        # Semantic identity, not sha (see _candidate_task_semantics_match).
        if (
            candidate is None
            or not _candidate_task_semantics_match(
                candidate.xml, self.prepared.resolved_task_xml
            )
            or candidate.enabled is not True
        ):
            raise CapturedPaperHostCutoverError(
                "APPLIED_POSTCONDITION_FAILED", "candidate task readback differs"
            )
        candidate_tasks = self.backend.find_candidate_tasks(self.prepared.invocation)
        if (
            len(candidate_tasks) != 1
            or candidate_tasks[0].name != CANDIDATE_TASK_NAME
            or not _candidate_task_semantics_match(
                candidate_tasks[0].xml, self.prepared.resolved_task_xml
            )
        ):
            raise CapturedPaperHostCutoverError(
                "APPLIED_POSTCONDITION_FAILED",
                "the exact candidate invocation is not owned by one unified task",
            )
        processes = self.backend.await_candidate_processes(
            self.prepared.invocation, timeout_seconds=0.0
        )
        self._assert_candidate_process_roster(processes)
        return tuple(processes)

    def _issue_activation_permit(
        self,
        *,
        journal: CutoverJournal,
        service: ProcessIdentity,
        prepared_receipt_sha256: str,
        challenge_sha256: str,
        valid_until: datetime,
        dispatch_lock_identity: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], str]:
        paths = _startup_handshake_paths(
            self.prepared.invocation, roots=self.prepared.allowed_read_roots
        )
        lock_identity = _validate_dispatch_lock_identity(
            dispatch_lock_identity, expected_path=paths["dispatch_lock"]
        )
        issuer = _issuer_provenance(
            prepared=self.prepared, journal_root=self.journal_root
        )
        issued_at = self.clock().astimezone(UTC)
        permit_valid_until = min(
            valid_until,
            issued_at + timedelta(seconds=STARTUP_HANDSHAKE_MAX_AGE_SECONDS),
        )
        if permit_valid_until <= issued_at:
            raise CapturedPaperHostCutoverError(
                "STARTUP_PREPARED_EXPIRED", "PREPARED expired before host authorization"
            )
        authorization_payload: dict[str, Any] = {
            "activation_generation": self.prepared.activation_generation,
            "manifest_path": str(self.prepared.manifest_path),
            "manifest_sha256": self.prepared.manifest_sha256,
            "candidate_root": str(self.prepared.candidate_root),
            "journal_root": str(self.journal_root),
            "account_scope": "alpaca:paper",
            "expected_account_id": self.prepared.expected_account_id,
            "service_pid": service.pid,
            "service_create_time_ns": service.create_time_ns,
            "service_executable_path": service.executable_path,
            "service_executable_sha256": service.executable_sha256,
            "service_cmdline": list(service.cmdline),
            "service_cmdline_sha256": service.cmdline_sha256,
            "service_role": service.role,
            "service_script_path": self.prepared.invocation.service_script_path,
            "service_script_sha256": self.prepared.invocation.service_script_sha256,
            "challenge_sha256": challenge_sha256,
            "prepared_receipt_sha256": prepared_receipt_sha256,
            "issued_at": _iso(issued_at),
            "valid_until": _iso(permit_valid_until),
            "permit_path": str(paths["permit"]),
            **dict(lock_identity),
            **dict(issuer),
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        # This append is the durable authorization point.  The published
        # permit embeds the exact hash-chained event and its payload so the
        # service can re-hash it and compare every claim, rather than merely
        # accepting a syntactically valid 64-hex string.
        authorization = journal.append("activation_permit_issued", authorization_payload)
        body: dict[str, Any] = {
            "schema_version": STARTUP_PERMIT_SCHEMA,
            "state": "ACTIVATION_PERMITTED",
            **authorization_payload,
            "journal_path": str(journal.path),
            "journal_transaction_id": journal.transaction_id,
            "journal_authorization_sequence": authorization["sequence"],
            "journal_authorization_event_sha256": authorization["event_sha256"],
            "journal_authorization_event": dict(authorization),
        }
        body["permit_sha256"] = sha256_json(body)
        _atomic_publish_canonical_json(paths["permit"], body)
        permit_raw = paths["permit"].read_bytes()
        persisted = _strict_json(permit_raw, "persisted activation permit")
        if permit_raw != _canonical_json_bytes(persisted):
            raise CapturedPaperHostCutoverError(
                "STARTUP_PERMIT_INVALID", "persisted activation permit is not canonical"
            )
        permit_sha = _validate_activation_permit_against_journal(
            persisted,
            journal=journal,
            prepared=self.prepared,
            permit_path=paths["permit"],
            service=service,
        )
        journal.append(
            "activation_permit_published",
            {
                "permit_path": str(paths["permit"]),
                "activation_permit_sha256": permit_sha,
                "journal_authorization_sequence": authorization["sequence"],
                "journal_authorization_event_sha256": authorization["event_sha256"],
                "prepared_receipt_sha256": prepared_receipt_sha256,
                "challenge_sha256": challenge_sha256,
            },
        )
        return MappingProxyType(body), permit_sha

    def _assert_rolled_back(
        self, prior_execution_lane: LegacyExecutionLaneObservation
    ) -> None:
        self._assert_rolled_back_with(
            self.prepared,
            prior_execution_lane=prior_execution_lane,
        )

    def _assert_rolled_back_with(
        self,
        prepared: PreparedCutover,
        *,
        prior_execution_lane: LegacyExecutionLaneObservation,
    ) -> None:
        handshake_paths = _startup_handshake_paths(
            prepared.invocation, roots=prepared.allowed_read_roots
        )
        if os.path.lexists(handshake_paths["permit"]):
            raise CapturedPaperHostCutoverError(
                "ROLLBACK_POSTCONDITION_FAILED", "activation permit remains after rollback"
            )
        for name in REQUIRED_LEGACY_TASKS:
            expected = prepared.task_snapshot.tasks[name]
            observed = self.backend.get_task(name)
            if (
                observed is None
                or observed.xml != expected.xml
                or observed.enabled is not expected.enabled
            ):
                raise CapturedPaperHostCutoverError(
                    "ROLLBACK_POSTCONDITION_FAILED",
                    f"legacy task {name} is not exactly restored",
                )
        if self.backend.get_task(CANDIDATE_TASK_NAME) is not None:
            raise CapturedPaperHostCutoverError(
                "ROLLBACK_POSTCONDITION_FAILED", "candidate task remains after rollback"
            )
        if self.backend.find_candidate_tasks(prepared.invocation):
            raise CapturedPaperHostCutoverError(
                "ROLLBACK_POSTCONDITION_FAILED",
                "a scheduled task still owns the candidate invocation",
            )
        restored = self.backend.find_legacy_processes(
            prepared.restore_plan.bindings
        )
        roles = [item.role for item in restored]
        expected_roles = [item.role for item in prepared.restore_plan.bindings]
        bindings = {item.role: item for item in prepared.restore_plan.bindings}
        if (
            sorted(roles) != sorted(expected_roles)
            or len(roles) != len(set(roles))
            or len({item.pid for item in restored}) != len(restored)
            or any(not self._process_matches_restore_binding(item, bindings[item.role]) for item in restored)
        ):
            raise CapturedPaperHostCutoverError(
                "ROLLBACK_POSTCONDITION_FAILED",
                "legacy process roles are not exactly restored",
            )
        if self.backend.await_candidate_processes(
            prepared.invocation, timeout_seconds=0.0
        ):
            raise CapturedPaperHostCutoverError(
                "ROLLBACK_POSTCONDITION_FAILED", "candidate process remains after rollback"
            )
        lane = self.backend.inspect_legacy_execution_lane()
        if lane != prior_execution_lane:
            raise CapturedPaperHostCutoverError(
                "ROLLBACK_POSTCONDITION_FAILED",
                "legacy Docker execution lane prior state was not exactly restored",
            )

    @staticmethod
    def _process_matches_restore_binding(
        process: ProcessIdentity, binding: LegacyProcessBinding
    ) -> bool:
        return (
            process.role == binding.role
            and os.path.normcase(process.executable_path)
            == os.path.normcase(binding.executable_path)
            and process.executable_sha256 == binding.executable_sha256
            and os.path.normcase(process.bridge_script_path or "")
            == os.path.normcase(binding.bridge_script_path)
            and process.bridge_script_sha256 == binding.bridge_script_sha256
            and process.cmdline == binding.expected_cmdline
            and process.cmdline_sha256 == binding.expected_cmdline_sha256
        )

    @staticmethod
    def _assert_restore_binding_sources_current(
        binding: LegacyProcessBinding,
        *,
        contract: LegacyTaskLaunchContract,
        roots: Sequence[Path],
    ) -> None:
        try:
            _assert_launch_contract_sources_current(contract, roots=roots)
            executable, executable_sha = _stable_local_file_unrooted(
                binding.executable_path,
                field=f"rollback {binding.role} executable",
            )
            script, script_sha = _stable_local_file_unrooted(
                binding.bridge_script_path,
                field=f"rollback {binding.role} bridge script",
            )
        except CapturedPaperHostCutoverError as exc:
            raise CapturedPaperHostCutoverError(
                "LEGACY_RESTORE_SOURCE_DRIFT",
                f"refusing to start unavailable {binding.role} restore authority",
            ) from exc
        if (
            executable_sha != binding.executable_sha256
            or script_sha != binding.bridge_script_sha256
            or os.path.normcase(str(executable))
            != os.path.normcase(binding.expected_cmdline[0])
            or not any(
                os.path.normcase(str(script)) == os.path.normcase(value)
                for value in binding.expected_cmdline
            )
            or sha256_json(list(binding.expected_cmdline))
            != binding.expected_cmdline_sha256
        ):
            raise CapturedPaperHostCutoverError(
                "LEGACY_RESTORE_SOURCE_DRIFT",
                f"refusing to start drifted {binding.role} restore authority",
            )

    def _revalidate_restore_authority(self, prepared: PreparedCutover) -> None:
        """Revalidate every sealed launch contract and both process bindings.

        Runs unconditionally before any legacy task is registered or enabled.
        A drifted wrapper/starter must never be installed where a Daily/Logon
        trigger could execute it, and an already-running exact bridge process
        must not skip wrapper-chain revalidation.
        """

        contracts: dict[str, LegacyTaskLaunchContract] = dict(
            prepared.restore_plan.launch_contracts
        )
        derived: Mapping[str, LegacyTaskLaunchContract] | None = None
        for binding in prepared.restore_plan.bindings:
            contract = contracts.get(binding.restore_task)
            if contract is None:
                if derived is None:
                    derived = _derive_direct_launch_contracts(
                        tasks=prepared.task_snapshot.tasks,
                        bindings=prepared.restore_plan.bindings,
                    )
                contract = derived[binding.restore_task]
                contracts[binding.restore_task] = contract
            self._assert_restore_binding_sources_current(
                binding,
                contract=contract,
                roots=prepared.allowed_read_roots,
            )
        for task_name, contract in contracts.items():
            try:
                _assert_launch_contract_sources_current(
                    contract, roots=prepared.allowed_read_roots
                )
            except CapturedPaperHostCutoverError as exc:
                raise CapturedPaperHostCutoverError(
                    "LEGACY_RESTORE_SOURCE_DRIFT",
                    f"refusing to restore drifted launch contract for {task_name}",
                ) from exc

    def _rollback_material(self, journal: CutoverJournal) -> PreparedCutover:
        started = [
            event for event in journal.events if event.get("event_type") == "apply_started"
        ]
        if len(started) != 1:
            raise CapturedPaperHostCutoverError(
                "ROLLBACK_CAPSULE_MISSING",
                "rollback requires exactly one journal-bound apply capsule",
            )
        payload = _mapping(started[0].get("payload"), "apply_started.payload")
        capsule_path, capsule_raw, capsule_sha = journal.read_referenced_object(
            value=payload.get("rollback_capsule_path"),
            expected_sha256=payload.get("rollback_capsule_sha256"),
            kind="rollback_capsule",
        )
        return _parse_rollback_capsule(
            path=capsule_path,
            raw=capsule_raw,
            digest=capsule_sha,
            caller_roots=self.prepared.allowed_read_roots,
            expected_generation=self.prepared.activation_generation,
            expected_manifest_sha256=self.prepared.manifest_sha256,
        )

    def _revoke_activation_permit(
        self, *, journal: CutoverJournal, prepared: PreparedCutover, reason: str
    ) -> Mapping[str, Any] | None:
        issued = [
            event
            for event in journal.events
            if event.get("event_type") == "activation_permit_issued"
        ]
        published = [
            event for event in journal.events
            if event.get("event_type") == "activation_permit_published"
        ]
        paths = _startup_handshake_paths(
            prepared.invocation, roots=prepared.allowed_read_roots
        )
        if not issued:
            if paths["permit"].exists():
                raise CapturedPaperHostCutoverError(
                    "STARTUP_PERMIT_ORPHANED",
                    "activation permit exists without a durable issuance event",
                )
            return None
        if len(issued) != 1 or len(published) > 1:
            raise CapturedPaperHostCutoverError(
                "STARTUP_PERMIT_REPLAY", "journal contains multiple activation permits"
            )
        authorization = issued[0]
        authorization_payload = _mapping(
            authorization.get("payload"), "activation permit issuance payload"
        )
        _exact_keys(
            authorization_payload,
            _PERMIT_AUTHORIZATION_PAYLOAD_KEYS,
            "activation permit issuance payload",
        )
        issuer = {
            key: authorization_payload.get(key) for key in _ISSUER_PROVENANCE_KEYS
        }
        _validate_recorded_issuer_provenance(issuer)
        service_cmdline = authorization_payload.get("service_cmdline")
        issued_at = _parse_utc(
            authorization_payload.get("issued_at"), "permit issuance issued_at"
        )
        valid_until = _parse_utc(
            authorization_payload.get("valid_until"), "permit issuance valid_until"
        )
        if not (
            type(authorization.get("sequence")) is int
            and int(authorization["sequence"]) > 0
            and authorization_payload.get("activation_generation")
            == prepared.activation_generation
            and authorization_payload.get("manifest_path")
            == str(prepared.manifest_path)
            and authorization_payload.get("manifest_sha256")
            == prepared.manifest_sha256
            and authorization_payload.get("candidate_root")
            == str(prepared.candidate_root)
            and authorization_payload.get("journal_root") == str(journal.root)
            and authorization_payload.get("account_scope") == "alpaca:paper"
            and authorization_payload.get("expected_account_id")
            == prepared.expected_account_id
            and authorization_payload.get("service_script_path")
            == prepared.invocation.service_script_path
            and authorization_payload.get("service_script_sha256")
            == prepared.invocation.service_script_sha256
            and type(authorization_payload.get("service_pid")) is int
            and int(authorization_payload["service_pid"]) > 0
            and type(authorization_payload.get("service_create_time_ns")) is int
            and int(authorization_payload["service_create_time_ns"]) > 0
            and _is_local_absolute(
                Path(str(authorization_payload.get("service_executable_path") or ""))
            )
            and _sha(
                authorization_payload.get("service_executable_sha256"),
                "permit issuance service executable hash",
            )
            and isinstance(service_cmdline, list)
            and bool(service_cmdline)
            and all(isinstance(item, str) and item for item in service_cmdline)
            and sha256_json(service_cmdline)
            == _sha(
                authorization_payload.get("service_cmdline_sha256"),
                "permit issuance service argv hash",
            )
            and authorization_payload.get("service_role") == "candidate_service"
            and authorization_payload.get("challenge_sha256")
            == _sha(
                authorization_payload.get("challenge_sha256"),
                "permit issuance challenge",
            )
            and authorization_payload.get("prepared_receipt_sha256")
            == _sha(
                authorization_payload.get("prepared_receipt_sha256"),
                "permit issuance PREPARED receipt",
            )
            and issued_at < valid_until
            and (valid_until - issued_at).total_seconds()
            <= STARTUP_HANDSHAKE_MAX_AGE_SECONDS
            and authorization_payload.get("live_cash_authorized") is False
            and authorization_payload.get("real_money_authorized") is False
        ):
            raise CapturedPaperHostCutoverError(
                "STARTUP_PERMIT_JOURNAL_MISMATCH",
                "durable issuance event differs from this activation",
            )
        if Path(str(authorization_payload.get("permit_path") or "")) != paths["permit"]:
            raise CapturedPaperHostCutoverError(
                "STARTUP_PERMIT_PATH_MISMATCH", "journal permit path differs"
            )

        lock_identity = _validate_dispatch_lock_identity(
            {
                key: authorization_payload.get(key)
                for key in _DISPATCH_LOCK_IDENTITY_KEYS
            },
            expected_path=paths["dispatch_lock"],
        )
        request_body: dict[str, Any] = {
            "schema_version": "chili.captured-paper-host-revocation-requested.v1",
            "state": "REVOCATION_REQUESTED",
            "activation_generation": prepared.activation_generation,
            "manifest_sha256": prepared.manifest_sha256,
            "execution_scope": "legacy:mixed-paper-config-live-masters-disabled",
            "expected_account_id": prepared.expected_account_id,
            "journal_transaction_id": journal.transaction_id,
            "journal_authorization_sequence": authorization["sequence"],
            "journal_authorization_event_sha256": authorization["event_sha256"],
            "permit_path": str(paths["permit"]),
            "requested_at": _iso(self.clock().astimezone(UTC)),
            "reason": reason,
            **dict(lock_identity),
            "workers_started": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        request_body["receipt_sha256"] = sha256_json(request_body)
        revocation_request_sha = _publish_revocation_requested(
            path=paths["revocation_requested"], value=request_body
        )

        # Presence of this immutable, generation-owned tombstone is the
        # service's immediate fail-closed revocation signal.  Publish it
        # before any new journal append and before Task Scheduler/process
        # operations, so a stalled evidence append cannot extend authority.
        revoked_at = self.clock().astimezone(UTC)
        revoked: dict[str, Any] = {
            "schema_version": STARTUP_REVOKED_SCHEMA,
            "state": "REVOKED",
            "activation_generation": prepared.activation_generation,
            "manifest_sha256": prepared.manifest_sha256,
            "account_scope": "alpaca:paper",
            "expected_account_id": prepared.expected_account_id,
            "service_pid": authorization_payload.get("service_pid"),
            "service_create_time_ns": authorization_payload.get(
                "service_create_time_ns"
            ),
            "service_executable_path": authorization_payload.get(
                "service_executable_path"
            ),
            "service_executable_sha256": authorization_payload.get(
                "service_executable_sha256"
            ),
            "service_cmdline_sha256": authorization_payload.get(
                "service_cmdline_sha256"
            ),
            "challenge_sha256": authorization_payload.get("challenge_sha256"),
            "prepared_receipt_sha256": authorization_payload.get(
                "prepared_receipt_sha256"
            ),
            "permit_path": str(paths["permit"]),
            "revocation_requested_path": str(paths["revocation_requested"]),
            "revocation_requested_receipt_sha256": revocation_request_sha,
            "revoked_at": _iso(revoked_at),
            "reason": reason,
            "journal_path": str(journal.path),
            "journal_transaction_id": journal.transaction_id,
            "journal_authorization_sequence": authorization["sequence"],
            "journal_authorization_event_sha256": authorization["event_sha256"],
            "workers_started": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
            **dict(lock_identity),
        }
        revoked["receipt_sha256"] = sha256_json(revoked)
        revoked = dict(
            _publish_final_revocation_under_dispatch_lock(
                path=paths["revoked"], value=revoked, lock_identity=lock_identity
            )
        )

        publication_payload: Mapping[str, Any] | None = None
        if published:
            publication_payload = _mapping(
                published[0].get("payload"), "permit publication payload"
            )
            _exact_keys(
                publication_payload,
                {
                    "permit_path", "activation_permit_sha256",
                    "journal_authorization_sequence",
                    "journal_authorization_event_sha256",
                    "prepared_receipt_sha256", "challenge_sha256",
                },
                "permit publication payload",
            )
            if not (
                Path(str(publication_payload.get("permit_path") or ""))
                == paths["permit"]
                and publication_payload.get("journal_authorization_sequence")
                == authorization["sequence"]
                and publication_payload.get("journal_authorization_event_sha256")
                == authorization["event_sha256"]
            ):
                raise CapturedPaperHostCutoverError(
                    "STARTUP_PERMIT_JOURNAL_MISMATCH",
                    "permit publication differs from its issuance event",
                )

        permit_sha: str | None = None
        if paths["permit"].is_file():
            permit_raw = paths["permit"].read_bytes()
            permit = _strict_json(permit_raw, "activation permit")
            if permit_raw != _canonical_json_bytes(permit):
                raise CapturedPaperHostCutoverError(
                    "STARTUP_PERMIT_INVALID", "activation permit is not canonical"
                )
            permit_sha = _validate_activation_permit_against_journal(
                permit,
                journal=journal,
                prepared=prepared,
                permit_path=paths["permit"],
                verify_current_issuer=False,
            )
            if publication_payload is not None and permit_sha != _sha(
                publication_payload.get("activation_permit_sha256"),
                "published activation permit",
            ):
                raise CapturedPaperHostCutoverError(
                    "STARTUP_PERMIT_JOURNAL_MISMATCH",
                    "published activation permit hash differs from exact bytes",
                )
            try:
                os.chmod(paths["permit"], stat.S_IWRITE | stat.S_IREAD)
                paths["permit"].unlink()
            except OSError as exc:
                raise CapturedPaperHostCutoverError(
                    "STARTUP_PERMIT_REVOCATION_FAILED",
                    "could not remove activation permit after fail-closed tombstone",
                ) from exc
        elif publication_payload is not None:
            # A prior interrupted rollback may already have removed the
            # permit.  The exact tombstone above remains the authority stop.
            permit_sha = _sha(
                publication_payload.get("activation_permit_sha256"),
                "published activation permit",
            )
        return MappingProxyType(
            {
                "activation_permit_sha256": permit_sha,
                "revocation_receipt_sha256": revoked["receipt_sha256"],
                "journal_authorization_sequence": authorization["sequence"],
                "journal_authorization_event_sha256": authorization["event_sha256"],
                "permit_absent": not paths["permit"].exists(),
            }
        )

    def _rollback(self, journal: CutoverJournal, *, reason: str) -> int:
        prepared = self._rollback_material(journal)
        prior_lane = _legacy_execution_lane_baseline(journal.events)
        journal_failed = False

        def record(event_type: str, payload: Mapping[str, Any]) -> None:
            nonlocal journal_failed
            try:
                journal.append(event_type, payload)
            except BaseException:
                journal_failed = True

        # Revoke worker authority first.  _revoke_activation_permit publishes
        # an O_EXCL generation-owned tombstone without appending to the
        # journal, so even a blocked/failing evidence append below cannot
        # extend an already-issued permit.
        revocation = self._revoke_activation_permit(
            journal=journal, prepared=prepared, reason=reason
        )
        record(
            "rollback_started",
            {
                "reason": reason,
                "candidate_task_name": CANDIDATE_TASK_NAME,
                "live_cash_authorized": False,
            },
        )
        if revocation is not None:
            record("activation_permit_revoked", dict(revocation))
        # Fail closed before ANY host mutation when restore sources drifted.
        # Registering a drifted wrapper/starter as an enabled Daily/Logon task
        # would hand the scheduler unapproved code, so nothing below may run
        # until every contract and binding revalidates against sealed hashes.
        self._revalidate_restore_authority(prepared)
        mutations = 0
        foreign_candidate = False

        def late_candidate_evidence() -> tuple[
            TaskObservation | None,
            tuple[TaskObservation, ...],
            tuple[CandidateProcessObservation, ...],
        ]:
            return (
                self.backend.get_task(CANDIDATE_TASK_NAME),
                tuple(self.backend.find_candidate_tasks(prepared.invocation)),
                tuple(
                    self.backend.await_candidate_processes(
                        prepared.invocation, timeout_seconds=0.0
                    )
                ),
            )

        def quarantine_late_candidate(
            *,
            named: TaskObservation | None,
            tasks: Sequence[TaskObservation],
            processes: Sequence[CandidateProcessObservation],
        ) -> NoReturn:
            """Re-quiesce every exact legacy lane after a rollback race.

            A candidate task can be registered by an external generation
            after the first inventory.  Once that happens, restoring the
            legacy Docker executor and bridges would create dual authority.
            Never mutate the foreign task; revoke the exact candidate
            processes and put only the sealed legacy identities back into a
            stopped state before reporting the quarantine.
            """

            nonlocal mutations
            lane = self.backend.inspect_legacy_execution_lane()
            if lane.identity_key() != prior_lane.identity_key():
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_IDENTITY_DRIFT",
                    "cannot safely quarantine a changed legacy Docker identity",
                )
            mutations += self.backend.quiesce_legacy_execution_lane(expected=lane)

            for process in processes:
                current = self.backend.await_candidate_processes(
                    prepared.invocation, timeout_seconds=0.0
                )
                match = next(
                    (
                        item
                        for item in current
                        if item.kind == process.kind
                        and item.identity.semantic_key()
                        == process.identity.semantic_key()
                    ),
                    None,
                )
                if match is not None:
                    self.backend.stop_candidate_process(match, prepared.invocation)
                    mutations += 1

            for process in self.backend.find_legacy_processes(
                prepared.restore_plan.bindings
            ):
                current = self.backend.get_process(process.pid, role=process.role)
                if (
                    current is not None
                    and current.semantic_key() == process.semantic_key()
                ):
                    self.backend.stop_process(process)
                    mutations += 1

            legacy_task_drift = False
            for name in REQUIRED_LEGACY_TASKS:
                expected = prepared.task_snapshot.tasks[name]
                observed = self.backend.get_task(name)
                if observed is None:
                    legacy_task_drift = True
                    continue
                if _task_definition_sha_ignoring_enabled(
                    observed.xml
                ) != _task_definition_sha_ignoring_enabled(expected.xml):
                    legacy_task_drift = True
                    continue
                if observed.enabled:
                    self.backend.set_task_enabled(name, False)
                    mutations += 1

            lane_after = self.backend.inspect_legacy_execution_lane()
            legacy_processes_after = self.backend.find_legacy_processes(
                prepared.restore_plan.bindings
            )
            candidate_processes_after = self.backend.await_candidate_processes(
                prepared.invocation, timeout_seconds=0.0
            )
            legacy_tasks_quiesced = all(
                (observed := self.backend.get_task(name)) is not None
                and not observed.enabled
                for name in REQUIRED_LEGACY_TASKS
            )
            quarantine_complete = (
                lane_after.identity_key() == prior_lane.identity_key()
                and lane_after.state == "stopped"
                and not any(
                    item.enabled for item in lane_after.recreator_tasks
                )
                and not legacy_processes_after
                and not candidate_processes_after
                and legacy_tasks_quiesced
                and not legacy_task_drift
            )
            record(
                "rollback_blocked_foreign_candidate",
                {
                    "candidate_task_name": CANDIDATE_TASK_NAME,
                    "foreign_named_collision": named is not None,
                    "exact_invocation_task_names": sorted(
                        {item.name for item in tasks}
                    ),
                    "candidate_process_count_stopped": len(processes),
                    "legacy_execution_remains_quiesced": quarantine_complete,
                    "live_cash_authorized": False,
                },
            )
            if not quarantine_complete:
                raise CapturedPaperHostCutoverError(
                    "FOREIGN_CANDIDATE_QUARANTINE_FAILED",
                    "candidate authority appeared and exact legacy quiescence could not be proven",
                )
            raise CapturedPaperHostCutoverError(
                "FOREIGN_CANDIDATE_EXECUTION_QUARANTINED",
                "candidate authority appeared during rollback; legacy was re-quiesced",
            )

        # Inventory, then disable/End the on-demand-only task before signaling
        # either process.  This also prevents a concurrent explicit /Run.
        candidate_processes = self.backend.await_candidate_processes(
            prepared.invocation, timeout_seconds=0.0
        )
        self._assert_candidate_process_subset(candidate_processes)
        candidate = self.backend.get_task(CANDIDATE_TASK_NAME)
        if candidate is not None:
            # Semantic identity, not sha (see _candidate_task_semantics_match).
            if not _candidate_task_semantics_match(
                candidate.xml, prepared.resolved_task_xml
            ):
                # Never mutate a foreign colliding task, but also never let it
                # strand disabled legacy capture.  Restore legacy first and
                # then report the unresolved collision fail-closed.
                foreign_candidate = True
            else:
                self.backend.set_task_enabled(CANDIDATE_TASK_NAME, False)
                mutations += 1
                disabled = self.backend.get_task(CANDIDATE_TASK_NAME)
                if disabled is None or disabled.enabled:
                    raise CapturedPaperHostCutoverError(
                        "CANDIDATE_DISABLE_FAILED",
                        "candidate task did not disable before process stop",
                    )
                self.backend.stop_task(CANDIDATE_TASK_NAME)
                mutations += 1
        for process in candidate_processes:
            current = self.backend.await_candidate_processes(
                prepared.invocation, timeout_seconds=0.0
            )
            match = next(
                (
                    item for item in current
                    if item.kind == process.kind
                    and item.identity.semantic_key() == process.identity.semantic_key()
                ),
                None,
            )
            if match is not None:
                self.backend.stop_candidate_process(match, prepared.invocation)
                mutations += 1
        if self.backend.await_candidate_processes(
            prepared.invocation, timeout_seconds=0.0
        ):
            raise CapturedPaperHostCutoverError(
                "CANDIDATE_STOP_FAILED", "candidate process survived exact rollback"
            )
        if candidate is not None and not foreign_candidate:
                candidate_before_delete = self.backend.get_task(CANDIDATE_TASK_NAME)
                if (
                    candidate_before_delete is None
                    or not _candidate_task_semantics_match(
                        candidate_before_delete.xml, prepared.resolved_task_xml
                    )
                ):
                    foreign_candidate = True
                else:
                    self.backend.delete_task(CANDIDATE_TASK_NAME)
                    mutations += 1
                    if self.backend.get_task(CANDIDATE_TASK_NAME) is not None:
                        raise CapturedPaperHostCutoverError(
                            "CANDIDATE_DELETE_FAILED", "candidate task survived rollback"
                        )
                    record(
                        "candidate_removed",
                        {"resolved_task_xml_sha256": prepared.resolved_task_xml_sha256},
                    )

        remaining_candidate_tasks = self.backend.find_candidate_tasks(
            prepared.invocation
        )
        if foreign_candidate or remaining_candidate_tasks:
            record(
                "rollback_blocked_foreign_candidate",
                {
                    "candidate_task_name": CANDIDATE_TASK_NAME,
                    "foreign_named_collision": foreign_candidate,
                    "exact_invocation_task_names": sorted(
                        item.name for item in remaining_candidate_tasks
                    ),
                    "legacy_execution_remains_quiesced": True,
                    "live_cash_authorized": False,
                },
            )
            raise CapturedPaperHostCutoverError(
                "FOREIGN_CANDIDATE_EXECUTION_QUARANTINED",
                "foreign candidate authority remains; legacy execution stays quiesced",
            )

        for name in REQUIRED_LEGACY_TASKS:
            expected = prepared.task_snapshot.tasks[name]
            current = self.backend.get_task(name)
            if current is not None and _task_definition_sha_ignoring_enabled(
                current.xml
            ) != _task_definition_sha_ignoring_enabled(expected.xml):
                raise CapturedPaperHostCutoverError(
                    "FOREIGN_LEGACY_TASK",
                    f"refusing to overwrite changed legacy task {name}",
                )
            xml_path = journal.object_path(expected.xml, kind="legacy_task_xml")
            self.backend.register_task(name, xml_path, expected.xml_sha256)
            mutations += 1
            registered = self.backend.get_task(name)
            if registered is None:
                raise CapturedPaperHostCutoverError(
                    "LEGACY_TASK_RESTORE_FAILED",
                    f"legacy task {name} disappeared after registration",
                )
            if registered.enabled is not expected.enabled:
                self.backend.set_task_enabled(name, expected.enabled)
                mutations += 1
            observed = self.backend.get_task(name)
            if (
                observed is None
                or observed.xml != expected.xml
                or observed.enabled is not expected.enabled
            ):
                raise CapturedPaperHostCutoverError(
                    "LEGACY_TASK_RESTORE_FAILED",
                    f"legacy task {name} did not restore exactly",
                )
            record(
                "legacy_task_restored",
                {
                    "task_name": name,
                    "xml_sha256": expected.xml_sha256,
                    "enabled": expected.enabled,
                },
            )

        discovered = self.backend.find_legacy_processes(
            prepared.restore_plan.bindings
        )
        if len({item.pid for item in discovered}) != len(discovered):
            raise CapturedPaperHostCutoverError(
                "LEGACY_PROCESS_RESTORE_FAILED",
                "one PID cannot satisfy two sealed legacy roles",
            )
        by_role: dict[str, list[ProcessIdentity]] = {}
        for process in discovered:
            by_role.setdefault(process.role, []).append(process)
        for binding in prepared.restore_plan.bindings:
            existing = by_role.get(binding.role, [])
            if len(existing) > 1:
                raise CapturedPaperHostCutoverError(
                    "LEGACY_PROCESS_RESTORE_FAILED",
                    f"multiple {binding.role} processes exist during rollback",
                )
            if existing and not self._process_matches_restore_binding(existing[0], binding):
                raise CapturedPaperHostCutoverError(
                    "LEGACY_PROCESS_RESTORE_FAILED",
                    f"existing {binding.role} process differs from sealed full argv",
                )
            if not existing:
                contract = prepared.restore_plan.launch_contracts.get(
                    binding.restore_task
                )
                if contract is None:
                    # In-memory direct fixtures may predate the serialized v3
                    # plan, but production/capsule parsing always supplies it.
                    contract = _derive_direct_launch_contracts(
                        tasks=prepared.task_snapshot.tasks,
                        bindings=prepared.restore_plan.bindings,
                    )[binding.restore_task]
                self._assert_restore_binding_sources_current(
                    binding,
                    contract=contract,
                    roots=prepared.allowed_read_roots,
                )
                self.backend.start_task(binding.restore_task)
                mutations += 1
        restored = self.backend.await_legacy_processes(
            prepared.restore_plan.bindings, timeout_seconds=15.0
        )
        restored_roles = [item.role for item in restored]
        expected_roles = [item.role for item in prepared.restore_plan.bindings]
        bindings_by_role = {item.role: item for item in prepared.restore_plan.bindings}
        if (
            sorted(restored_roles) != sorted(expected_roles)
            or len(set(restored_roles)) != len(restored_roles)
            or len({item.pid for item in restored}) != len(restored)
            or any(
                not self._process_matches_restore_binding(
                    item, bindings_by_role[item.role]
                )
                for item in restored
            )
        ):
            raise CapturedPaperHostCutoverError(
                "LEGACY_PROCESS_RESTORE_FAILED",
                "legacy bridge roles did not restore exactly",
            )
        if self.backend.find_candidate_tasks(prepared.invocation):
            # A foreign exact-invocation task appeared after the initial
            # candidate inventory.  Stop the exact legacy bridges we just
            # restored and disable their tasks before the Docker lane can be
            # restarted.  Do not mutate the foreign task.
            for process in restored:
                current = self.backend.get_process(process.pid, role=process.role)
                if current is not None and current.semantic_key() == process.semantic_key():
                    self.backend.stop_process(process)
                    mutations += 1
            for name in REQUIRED_LEGACY_TASKS:
                observed = self.backend.get_task(name)
                if observed is not None and observed.enabled:
                    self.backend.set_task_enabled(name, False)
                    mutations += 1
            record(
                "rollback_blocked_foreign_candidate",
                {
                    "candidate_task_name": CANDIDATE_TASK_NAME,
                    "foreign_named_collision": False,
                    "exact_invocation_task_names": sorted(
                        item.name
                        for item in self.backend.find_candidate_tasks(
                            prepared.invocation
                        )
                    ),
                    "legacy_execution_remains_quiesced": True,
                    "live_cash_authorized": False,
                },
            )
            raise CapturedPaperHostCutoverError(
                "FOREIGN_CANDIDATE_EXECUTION_QUARANTINED",
                "candidate authority appeared during rollback; legacy was re-quiesced",
            )
        mutations += self.backend.restore_legacy_execution_lane(
            expected=prior_lane
        )
        restored_lane = self.backend.inspect_legacy_execution_lane()
        if restored_lane != prior_lane:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_RESTORE_FAILED",
                "legacy Docker execution lane prior state was not exactly restored",
            )
        record(
            "legacy_execution_lane_restored",
            {
                "legacy_execution_lane": dict(
                    _legacy_execution_lane_document(restored_lane)
                ),
                "legacy_execution_lane_sha256": sha256_json(
                    dict(_legacy_execution_lane_document(restored_lane))
                ),
            },
        )
        named, tasks, processes = late_candidate_evidence()
        if named is not None or tasks or processes:
            quarantine_late_candidate(
                named=named, tasks=tasks, processes=processes
            )
        try:
            self._assert_rolled_back_with(
                prepared, prior_execution_lane=prior_lane
            )
        except CapturedPaperHostCutoverError:
            # Close the final inventory/assertion race.  Only candidate
            # evidence triggers compensation here; unrelated rollback drift
            # retains its original error and is never disguised.
            named, tasks, processes = late_candidate_evidence()
            if named is not None or tasks or processes:
                quarantine_late_candidate(
                    named=named, tasks=tasks, processes=processes
                )
            raise
        named, tasks, processes = late_candidate_evidence()
        if named is not None or tasks or processes:
            quarantine_late_candidate(
                named=named, tasks=tasks, processes=processes
            )
        if self.backend.get_task(CANDIDATE_TASK_NAME) is not None:
            raise CapturedPaperHostCutoverError(
                "ROLLBACK_POSTCONDITION_FAILED", "candidate task remains after rollback"
            )
        record(
            "rollback_completed",
            {
                "restored_task_count": len(REQUIRED_LEGACY_TASKS),
                "restored_process_roles": sorted(restored_roles),
                "candidate_task_absent": True,
            },
        )
        if journal_failed:
            raise CapturedPaperHostCutoverError(
                "ROLLBACK_JOURNAL_WRITE_FAILED",
                "host state was restored but rollback journal durability failed",
            )
        return mutations


class WindowsHostCutoverBackend:
    """Windows backend with exact task/process identity checks.

    It is constructed only by the CLI after all activation/artifact validation
    succeeds.  Task Scheduler commands are fixed argument vectors with
    ``shell=False``.  Process stops are preceded by a second PID/start-time/
    executable/hash/cmdline comparison and a forced stop, if needed, is
    preceded by a third comparison.
    """

    def __init__(self, *, bindings: Sequence[LegacyProcessBinding]) -> None:
        if os.name != "nt":
            raise CapturedPaperHostCutoverError(
                "WINDOWS_REQUIRED", "Task Scheduler cutover requires Windows"
            )
        # The immutable native resolver, never %SystemRoot%: a forged
        # environment variable must not point task control at a staged
        # schtasks.exe.
        self._schtasks, _ = _resolve_system_executable(
            str(_native_system32_directory() / "schtasks.exe"), "schtasks.exe"
        )
        self._bindings = {item.role: item for item in bindings}
        self._docker: Path | None = None
        try:
            import psutil  # type: ignore
        except ImportError as exc:
            raise CapturedPaperHostCutoverError(
                "PSUTIL_REQUIRED", "psutil is required for exact process provenance"
            ) from exc
        self._psutil = psutil

    def _docker_command(
        self, arguments: Sequence[str]
    ) -> subprocess.CompletedProcess[bytes]:
        if self._docker is None:
            self._docker, _ = _resolve_system_executable(
                str(_DOCKER_DESKTOP_EXECUTABLE), "Docker Desktop CLI"
            )
        completed = subprocess.run(
            [str(self._docker), *[str(item) for item in arguments]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_COMMAND_FAILED",
                f"Docker rejected fixed execution-lane operation {arguments[0]}",
            )
        return completed

    def _inspect_legacy_execution_lane_by(
        self, identifier: str
    ) -> LegacyExecutionLaneObservation:
        completed = self._docker_command(
            (
                "inspect",
                "--type",
                "container",
                "--format",
                "{{json .}}",
                identifier,
            )
        )
        value = _strict_json(
            completed.stdout.strip(), "legacy Docker execution lane inspect"
        )
        state_value = _mapping(value.get("State"), "legacy Docker state")
        config = _mapping(value.get("Config"), "legacy Docker config")
        host_config = _mapping(
            value.get("HostConfig"), "legacy Docker host config"
        )
        restart_policy = _mapping(
            host_config.get("RestartPolicy"), "legacy Docker restart policy"
        )
        container_id = str(value.get("Id") or "").lower()
        image_id = str(value.get("Image") or "").lower()
        name = str(value.get("Name") or "").removeprefix("/")
        created = value.get("Created")
        executable = value.get("Path")
        args = value.get("Args")
        config_image = config.get("Image")
        entrypoint = config.get("Entrypoint")
        command = config.get("Cmd")
        restart_name = restart_policy.get("Name")
        restart_maximum = restart_policy.get("MaximumRetryCount")
        auto_remove = host_config.get("AutoRemove")
        status = state_value.get("Status")
        running = state_value.get("Running")
        paused = state_value.get("Paused")
        restarting = state_value.get("Restarting")
        dead = state_value.get("Dead")
        if not (
            _SHA256_RE.fullmatch(container_id)
            and image_id.startswith("sha256:")
            and _SHA256_RE.fullmatch(image_id.removeprefix("sha256:"))
            and name == LEGACY_EXECUTION_LANE_NAME
            and isinstance(created, str)
            and bool(created)
            and isinstance(executable, str)
            and bool(executable)
            and isinstance(args, list)
            and all(isinstance(item, str) for item in args)
            and isinstance(config_image, str)
            and bool(config_image)
            and (
                entrypoint is None
                or isinstance(entrypoint, str)
                or (
                    isinstance(entrypoint, list)
                    and all(isinstance(item, str) for item in entrypoint)
                )
            )
            and (
                command is None
                or isinstance(command, str)
                or (
                    isinstance(command, list)
                    and all(isinstance(item, str) for item in command)
                )
            )
            and isinstance(restart_name, str)
            and type(restart_maximum) is int
            and type(auto_remove) is bool
            and isinstance(status, str)
            and type(running) is bool
            and type(paused) is bool
            and type(restarting) is bool
            and type(dead) is bool
        ):
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_STATE_UNINSPECTABLE",
                "Docker returned a malformed execution-lane state",
            )

        required_scope_flags = dict(_EXECUTION_LANE_REQUIRED_SCOPE_FLAGS)
        raw_environment = config.get("Env")
        labels = config.get("Labels")
        if (
            not isinstance(raw_environment, list)
            or any(not isinstance(item, str) for item in raw_environment)
            or not isinstance(labels, Mapping)
        ):
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_SCOPE_UNINSPECTABLE",
                "legacy Docker execution scope is not inspectable",
            )
        observed_scope_flags: dict[str, bool] = {}
        for item in raw_environment:
            key, separator, raw_value = item.partition("=")
            if not separator or key not in required_scope_flags:
                continue
            if key in observed_scope_flags:
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_SCOPE_UNSAFE",
                    "legacy Docker execution scope contains duplicate authority flags",
                )
            normalized = raw_value.strip().casefold()
            if normalized in {"1", "true", "yes", "on"}:
                observed_scope_flags[key] = True
            elif normalized in {"0", "false", "no", "off"}:
                observed_scope_flags[key] = False
            else:
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_SCOPE_UNSAFE",
                    "legacy Docker execution scope contains an ambiguous authority flag",
                )
        compose_scope = {
            "project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
            "config_files": labels.get("com.docker.compose.project.config_files"),
        }
        if (
            observed_scope_flags != required_scope_flags
            or compose_scope
            != {
                "project": "chili-home-copilot",
                "service": "momentum-exec-worker",
                "config_files": r"D:\dev\chili-home-copilot\docker-compose.yml",
            }
        ):
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_SCOPE_UNSAFE",
                "legacy Docker lane lacks the pinned mixed-paper configuration and disabled live-entry masters",
            )
        scope_document = {
            "execution_scope": "legacy:mixed-paper-config-live-masters-disabled",
            "compose": compose_scope,
            "authority_flags": {
                key: observed_scope_flags[key]
                for key in sorted(observed_scope_flags)
            },
            "live_cash_authorized": False,
        }
        recreator_probe = getattr(
            self, "_execution_lane_recreator_probe", None
        )
        recreator_tasks = (
            tuple(recreator_probe())
            if callable(recreator_probe)
            else self._inspect_execution_lane_recreator_tasks(
                expected_image_id=image_id
            )
        )
        if auto_remove is not False:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_ROLLBACK_UNSAFE",
                "legacy Docker execution lane is configured for automatic removal",
            )
        if restart_name not in {"no", "on-failure", "unless-stopped"}:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_RESTART_POLICY_UNSAFE",
                "legacy Docker execution lane can restart after an explicit stop",
            )
        if restarting or dead:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_STATE_INVALID",
                "legacy Docker execution lane is restarting or dead",
            )
        if running and not paused and status == "running":
            normalized_state = "running"
        elif not running and not paused and status == "exited":
            normalized_state = "stopped"
        else:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_STATE_INVALID",
                "legacy Docker execution lane is not exactly running or stopped",
            )
        sanitized_config = {
            "Path": executable,
            "Args": list(args),
            "Config": {
                "Image": config_image,
                "Entrypoint": entrypoint,
                "Cmd": command,
            },
            "HostConfig": {
                "RestartPolicy": {
                    "Name": restart_name,
                    "MaximumRetryCount": restart_maximum,
                },
                "AutoRemove": auto_remove,
            },
            "Created": created,
        }
        return LegacyExecutionLaneObservation(
            container_name=name,
            container_id=container_id,
            image_id=image_id,
            config_sha256=sha256_json(sanitized_config),
            execution_scope="legacy:mixed-paper-config-live-masters-disabled",
            scope_sha256=sha256_json(scope_document),
            recreator_tasks=tuple(
                sorted(recreator_tasks, key=lambda item: item.name)
            ),
            state=normalized_state,
        )

    def _inspect_execution_lane_recreator_tasks(
        self, *, expected_image_id: str | None = None
    ) -> tuple[ExecutionLaneRecreatorTaskObservation, ...]:
        observations: list[ExecutionLaneRecreatorTaskObservation] = []
        compose_projection_sha256, rendered_image_id = (
            self._execution_lane_compose_projection()
        )
        if (
            expected_image_id is not None
            and rendered_image_id != expected_image_id
        ):
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_SCOPE_UNSAFE",
                "rendered Compose image differs from the exact running legacy image",
            )
        for name in EXECUTION_LANE_RECREATOR_TASKS:
            task = self.get_task(name)
            if task is None:
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_RECREATOR_MISSING",
                    f"external execution-lane authority is missing: {name}",
                )
            command, arguments = _task_exec_from_xml(task.xml)
            argv = _windows_command_line_to_argv(arguments)
            command_token = command.strip()
            command_name = Path(command_token).name.casefold()
            if command_name in {"powershell", "powershell.exe"}:
                native_command = _native_system32_executable("powershell.exe")
                allowed_bare = {"powershell", "powershell.exe"}
            elif command_name == "wscript.exe":
                native_command = _native_system32_executable("wscript.exe")
                allowed_bare = {"wscript.exe"}
            else:
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_RECREATOR_ACTION_DRIFT",
                    f"external execution-lane command is not allowlisted: {name}",
                )
            command_is_bare = not _DRIVE_PATH_RE.match(command_token)
            if (
                command_is_bare
                and command_token.casefold() not in allowed_bare
            ) or (
                not command_is_bare
                and os.path.normcase(os.path.normpath(command_token))
                != os.path.normcase(str(native_command))
            ):
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_RECREATOR_ACTION_DRIFT",
                    f"external execution-lane executable drifted: {name}",
                )
            resolved_command, command_sha256 = _stable_local_file_unrooted(
                native_command,
                field=f"execution-lane recreator executable {name}",
            )
            argv_paths = {
                os.path.normcase(os.path.normpath(item))
                for item in argv
                if _DRIVE_PATH_RE.match(item)
            }
            direct_sources = _EXECUTION_LANE_RECREATOR_DIRECT_SOURCES[name]
            if any(
                os.path.normcase(os.path.normpath(source)) not in argv_paths
                for source in direct_sources
            ):
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_RECREATOR_ACTION_DRIFT",
                    f"external execution-lane action drifted: {name}",
                )
            source_chain: list[Mapping[str, str]] = []
            source_chain.append(
                {
                    "path": str(resolved_command),
                    "sha256": command_sha256,
                }
            )
            for source, expected_sha256 in _EXECUTION_LANE_RECREATOR_SOURCES[name]:
                resolved, observed_sha256 = _stable_local_file_unrooted(
                    source, field=f"execution-lane recreator source {name}"
                )
                if (
                    os.path.normcase(str(resolved))
                    != os.path.normcase(os.path.normpath(source))
                    or observed_sha256 != expected_sha256
                ):
                    raise CapturedPaperHostCutoverError(
                        "EXECUTION_LANE_RECREATOR_SOURCE_DRIFT",
                        f"external execution-lane source drifted: {name}",
                    )
                source_chain.append(
                    {"path": str(resolved), "sha256": observed_sha256}
                )
            if name in _EXECUTION_LANE_COMPOSE_DEPENDENT_TASKS:
                source_chain.append(
                    {
                        "path": (
                            "docker-compose://chili-home-copilot/"
                            "momentum-exec-worker/sanitized"
                        ),
                        "sha256": compose_projection_sha256,
                    }
                )
            observations.append(
                ExecutionLaneRecreatorTaskObservation(
                    name=name,
                    definition_sha256=_task_definition_sha_ignoring_enabled(
                        task.xml
                    ),
                    action_sha256=sha256_json(
                        {
                            "command": os.path.normcase(command),
                            "argv": list(argv),
                        }
                    ),
                    source_chain_sha256=sha256_json(source_chain),
                    enabled=task.enabled,
                )
            )
        return tuple(sorted(observations, key=lambda item: item.name))

    def _execution_lane_compose_projection(self) -> tuple[str, str]:
        compose_path, compose_sha256 = _stable_local_file_unrooted(
            _EXECUTION_LANE_COMPOSE_FILE,
            field="legacy momentum execution Compose file",
        )
        if compose_sha256 != _EXECUTION_LANE_COMPOSE_FILE_SHA256:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_RECREATOR_SOURCE_DRIFT",
                "legacy momentum execution Compose file drifted",
            )
        completed = self._docker_command(
            (
                "compose",
                "--project-directory",
                str(compose_path.parent),
                "-f",
                str(compose_path),
                "--profile",
                "live-momentum",
                "config",
                "--format",
                "json",
            )
        )
        rendered = _strict_json(
            completed.stdout.strip(), "sanitized legacy Compose projection"
        )
        services = _mapping(rendered.get("services"), "legacy Compose services")
        service = _mapping(
            services.get("momentum-exec-worker"),
            "legacy momentum execution Compose service",
        )
        environment = _mapping(
            service.get("environment"), "legacy Compose service environment"
        )

        def flag(name: str) -> bool:
            raw = environment.get(name)
            if type(raw) is bool:
                return bool(raw)
            if isinstance(raw, (str, int)) and not isinstance(raw, bool):
                normalized = str(raw).strip().casefold()
                if normalized in {"1", "true", "yes", "on"}:
                    return True
                if normalized in {"0", "false", "no", "off"}:
                    return False
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_SCOPE_UNSAFE",
                "rendered legacy Compose scope contains an ambiguous authority flag",
            )

        authority_flags = {
            name: flag(name)
            for name in sorted(_EXECUTION_LANE_REQUIRED_SCOPE_FLAGS)
        }
        if authority_flags != dict(_EXECUTION_LANE_REQUIRED_SCOPE_FLAGS):
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_SCOPE_UNSAFE",
                "rendered legacy Compose scope changed its live-entry master policy",
            )
        profiles = service.get("profiles")
        command = service.get("command")
        entrypoint = service.get("entrypoint")
        volumes = service.get("volumes")
        if not (
            service.get("container_name") == LEGACY_EXECUTION_LANE_NAME
            and isinstance(service.get("image"), str)
            and bool(service.get("image"))
            and command == ["python", "scripts/scheduler_worker.py"]
            and (entrypoint is None or isinstance(entrypoint, (str, list)))
            and profiles == ["live-momentum"]
            and service.get("restart") == "unless-stopped"
            and isinstance(volumes, list)
        ):
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_SCOPE_UNSAFE",
                "rendered legacy Compose service identity is not the pinned momentum worker",
            )
        volume_projection = []
        for index, raw_volume in enumerate(volumes):
            volume = _mapping(
                raw_volume, f"legacy Compose volume[{index}]"
            )
            target = volume.get("target")
            kind = volume.get("type")
            read_only = volume.get("read_only", False)
            if (
                not isinstance(target, str)
                or not target.startswith("/")
                or not isinstance(kind, str)
                or type(read_only) is not bool
            ):
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_SCOPE_UNSAFE",
                    "rendered legacy Compose mount is malformed",
                )
            # Host source paths and secret values are deliberately excluded;
            # their operational authority is captured by immutable targets,
            # source-file/rendered-policy hashes, and credential presence.
            volume_projection.append(
                {"type": kind, "target": target, "read_only": read_only}
            )
        credential_names = {
            "CHILI_ALPACA_API_KEY",
            "CHILI_ALPACA_API_SECRET",
            "CHILI_ALPACA_LIVE_API_KEY",
            "CHILI_ALPACA_LIVE_API_SECRET",
            "COINBASE_API_KEY",
            "COINBASE_API_SECRET",
        }
        image_inspect = self._docker_command(
            (
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                str(service["image"]),
            )
        )
        image_document = _strict_json(
            image_inspect.stdout.strip(), "rendered Compose image identity"
        )
        rendered_image_id = str(image_document.get("Id") or "").lower()
        if not (
            rendered_image_id.startswith("sha256:")
            and _SHA256_RE.fullmatch(
                rendered_image_id.removeprefix("sha256:")
            )
        ):
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_SCOPE_UNSAFE",
                "rendered Compose image has no immutable local image identity",
            )
        projection = {
            "schema_version": "chili.legacy-momentum-compose-sanitized.v1",
            "compose_file_sha256": compose_sha256,
            "image": service["image"],
            "image_id": rendered_image_id,
            "container_name": service["container_name"],
            "profiles": list(profiles),
            "command": list(command),
            "entrypoint": entrypoint,
            "restart": service["restart"],
            "authority_flags": authority_flags,
            "credential_presence": {
                name: name in environment for name in sorted(credential_names)
            },
            "volume_projection": sorted(
                volume_projection,
                key=lambda item: (item["target"], item["type"]),
            ),
            "live_cash_authorized": False,
            "account_uuid_bound": False,
        }
        return sha256_json(projection), rendered_image_id

    def inspect_legacy_execution_lane(self) -> LegacyExecutionLaneObservation:
        return self._inspect_legacy_execution_lane_by(LEGACY_EXECUTION_LANE_NAME)

    def _inspect_exact_execution_lane(
        self, expected: LegacyExecutionLaneObservation
    ) -> LegacyExecutionLaneObservation:
        current = self._inspect_legacy_execution_lane_by(expected.container_id)
        if current.identity_key() != expected.identity_key():
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_IDENTITY_DRIFT",
                "Docker container identity changed after its durable baseline",
            )
        return current

    def quiesce_legacy_execution_lane(
        self, *, expected: LegacyExecutionLaneObservation
    ) -> int:
        current = self._inspect_exact_execution_lane(expected)
        if current != expected:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_STATE_DRIFT",
                "execution lane changed after its durable baseline was recorded",
            )
        mutations = 0
        for authority in expected.recreator_tasks:
            task = self.get_task(authority.name)
            if (
                task is None
                or _task_definition_sha_ignoring_enabled(task.xml)
                != authority.definition_sha256
            ):
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_RECREATOR_DRIFT",
                    f"external execution-lane authority drifted: {authority.name}",
                )
            if task.enabled:
                self.set_task_enabled(authority.name, False)
                mutations += 1
            # End a concurrently running instance even after disabling its
            # future triggers.  The backend treats an already-idle task as a
            # successful no-op.
            self.stop_task(authority.name)
            mutations += 1
        lingering = self.await_execution_lane_recreator_processes(
            timeout_seconds=15.0
        )
        if lingering:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_RECREATOR_STILL_RUNNING",
                "an external execution-lane authority or descendant survived /End",
            )
        if expected.state == "running":
            self._docker_command(("stop", expected.container_id))
            mutations += 1
        # A task may have spawned a detached Docker/PowerShell child just
        # before /End.  Require a short stable, disabled interval after the
        # exact-ID stop rather than trusting one immediate readback.
        time.sleep(0.5)
        lingering = self.await_execution_lane_recreator_processes(
            timeout_seconds=0.0
        )
        if lingering:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_RECREATOR_STILL_RUNNING",
                "an external execution-lane descendant appeared after Docker stop",
            )
        stopped = self._inspect_exact_execution_lane(expected)
        if stopped.state != "stopped" or any(
            item.enabled for item in stopped.recreator_tasks
        ):
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_QUIESCE_FAILED",
                "Docker lane or an external resurrection authority did not stop exactly",
            )
        return mutations

    def restore_legacy_execution_lane(
        self, *, expected: LegacyExecutionLaneObservation
    ) -> int:
        current = self._inspect_exact_execution_lane(expected)
        if any(item.enabled for item in current.recreator_tasks):
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_RECREATOR_DRIFT",
                "an external execution-lane authority re-enabled before rollback",
            )
        mutations = 0
        if expected.state == "running":
            if current.state == "stopped":
                self._docker_command(("start", expected.container_id))
                mutations += 1
            elif current.state != "running":
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_STATE_INVALID",
                    "execution lane cannot be restored to running",
                )
        else:
            if current.state == "running":
                self._docker_command(("stop", expected.container_id))
                mutations += 1
            elif current.state != "stopped":
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_STATE_INVALID",
                    "execution lane cannot be restored to stopped",
                )
        for authority in expected.recreator_tasks:
            task = self.get_task(authority.name)
            if (
                task is None
                or _task_definition_sha_ignoring_enabled(task.xml)
                != authority.definition_sha256
                or task.enabled
            ):
                raise CapturedPaperHostCutoverError(
                    "EXECUTION_LANE_RECREATOR_DRIFT",
                    f"external execution-lane authority cannot restore exactly: {authority.name}",
                )
            if authority.enabled:
                self.set_task_enabled(authority.name, True)
                mutations += 1
        restored = self._inspect_exact_execution_lane(expected)
        if restored != expected:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_RESTORE_FAILED",
                "Docker lane or its external authorities did not restore exactly",
            )
        return mutations

    def _task_command(
        self,
        arguments: Sequence[str],
        *,
        allow_not_found: bool = False,
        allow_not_running: bool = False,
    ) -> subprocess.CompletedProcess[bytes] | None:
        completed = subprocess.run(
            [str(self._schtasks), *[str(item) for item in arguments]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=30,
        )
        if completed.returncode == 0:
            return completed
        message = (completed.stdout + b"\n" + completed.stderr).decode(
            "utf-8", errors="replace"
        )
        lowered = message.casefold()
        if allow_not_found and any(
            marker in lowered
            for marker in (
                "cannot find",
                "does not exist",
                "system cannot find",
                "specified task name",
            )
        ):
            return None
        if allow_not_running and "not running" in lowered:
            return None
        raise CapturedPaperHostCutoverError(
            "TASK_SCHEDULER_COMMAND_FAILED",
            f"Task Scheduler rejected fixed operation {arguments[0]}",
        )

    def await_execution_lane_recreator_processes(
        self, *, timeout_seconds: float
    ) -> tuple[str, ...]:
        if timeout_seconds < 0 or not isinstance(timeout_seconds, (int, float)):
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_RECREATOR_INVENTORY_INVALID",
                "external execution-lane process timeout is invalid",
            )
        try:
            import psutil  # type: ignore
        except ImportError as exc:
            raise CapturedPaperHostCutoverError(
                "EXECUTION_LANE_RECREATOR_INVENTORY_UNAVAILABLE",
                "external execution-lane process inventory is unavailable",
            ) from exc
        source_markers = {
            os.path.normcase(os.path.normpath(path))
            for chain in _EXECUTION_LANE_RECREATOR_SOURCES.values()
            for path, _sha256 in chain
        }
        source_markers.add(
            os.path.normcase(os.path.normpath(str(_EXECUTION_LANE_COMPOSE_FILE)))
        )
        relevant_names = {
            "powershell.exe",
            "pwsh.exe",
            "wscript.exe",
            "cscript.exe",
            # The one-shot premarket wrapper invokes orchestrator.py through
            # the shared Python runtime.  Task Scheduler /End can terminate
            # the wrapper while that child is still alive, so excluding Python
            # here would turn an orphaned activation authority into "idle".
            # The exact allowlisted source-path marker below still prevents an
            # unrelated Python process from being classified as a recreator.
            "python.exe",
            "pythonw.exe",
            "docker.exe",
        }

        def inventory() -> tuple[str, ...]:
            matches: list[str] = []
            for process in psutil.process_iter(("pid", "name"), ad_value=None):
                name = process.info.get("name")
                if name is None:
                    continue
                normalized_name = str(name).casefold()
                if normalized_name not in relevant_names:
                    continue
                try:
                    cmdline = tuple(str(item) for item in process.cmdline())
                except psutil.NoSuchProcess:
                    continue
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    matches.append(
                        f"{int(process.info['pid'])}:{normalized_name}:uninspectable"
                    )
                    continue
                normalized_tokens = {
                    os.path.normcase(os.path.normpath(item))
                    for item in cmdline
                    if _DRIVE_PATH_RE.match(item)
                }
                lowered = " ".join(cmdline).casefold()
                source_match = bool(source_markers & normalized_tokens)
                docker_match = normalized_name == "docker.exe" and (
                    "momentum-exec-worker" in lowered
                    or "desktop restart" in lowered
                    or (
                        " compose " in f" {lowered} "
                        and "chili-home-copilot" in lowered
                    )
                )
                if source_match or docker_match:
                    matches.append(
                        f"{int(process.info['pid'])}:{normalized_name}:matched"
                    )
            return tuple(sorted(matches))

        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            found = inventory()
            if not found or time.monotonic() >= deadline:
                return found
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def _get_task_unrestricted(self, name: str) -> TaskObservation | None:
        if not name or len(name) > 238 or any(character in name for character in "\x00\r\n"):
            raise CapturedPaperHostCutoverError(
                "TASK_NAME_INVALID", "Task Scheduler returned an invalid task name"
            )
        result = self._task_command(
            ("/Query", "/TN", name, "/XML"), allow_not_found=True
        )
        if result is None:
            return None
        # The same normalization the collector applies, so backend and
        # collector observe identical hash-authoritative task bytes.
        raw = _normalize_schtasks_xml_output(bytes(result.stdout))
        enabled = _task_enabled_from_xml(raw)
        return TaskObservation(name=name, xml=raw, enabled=enabled)

    def get_task(self, name: str) -> TaskObservation | None:
        if name not in {
            *REQUIRED_LEGACY_TASKS,
            *EXECUTION_LANE_RECREATOR_TASKS,
            CANDIDATE_TASK_NAME,
        }:
            raise CapturedPaperHostCutoverError(
                "TASK_NAME_INVALID", "Task Scheduler task name is not allowlisted"
            )
        return self._get_task_unrestricted(name)

    def set_task_enabled(self, name: str, enabled: bool) -> None:
        if (
            name not in {
                *REQUIRED_LEGACY_TASKS,
                *EXECUTION_LANE_RECREATOR_TASKS,
            }
            and not (name == CANDIDATE_TASK_NAME and enabled is False)
        ):
            raise CapturedPaperHostCutoverError(
                "TASK_NAME_INVALID",
                "only sealed legacy/recreator tasks or candidate disable are permitted",
            )
        self._task_command(
            ("/Change", "/TN", name, "/ENABLE" if enabled else "/DISABLE")
        )

    def register_task(self, name: str, xml_path: Path, expected_sha256: str) -> None:
        if name not in {*REQUIRED_LEGACY_TASKS, CANDIDATE_TASK_NAME}:
            raise CapturedPaperHostCutoverError(
                "TASK_NAME_INVALID", "task registration name is not allowlisted"
            )
        raw = xml_path.read_bytes()
        if sha256_bytes(raw) != _sha(expected_sha256, "registered task XML"):
            raise CapturedPaperHostCutoverError(
                "TASK_XML_HASH_MISMATCH", "task XML changed before registration"
            )
        _task_enabled_from_xml(raw)
        arguments = ["/Create", "/TN", name, "/XML", str(xml_path)]
        if name in REQUIRED_LEGACY_TASKS:
            arguments.append("/F")
        self._task_command(arguments)

    def start_task(self, name: str) -> None:
        if name not in {*REQUIRED_LEGACY_TASKS, CANDIDATE_TASK_NAME}:
            raise CapturedPaperHostCutoverError(
                "TASK_NAME_INVALID", "task start name is not allowlisted"
            )
        self._task_command(("/Run", "/TN", name))

    def stop_task(self, name: str) -> None:
        if name not in {
            *EXECUTION_LANE_RECREATOR_TASKS,
            CANDIDATE_TASK_NAME,
        }:
            raise CapturedPaperHostCutoverError(
                "TASK_NAME_INVALID",
                "only exact recreator or candidate tasks can be stopped",
            )
        self._task_command(("/End", "/TN", name), allow_not_running=True)

    def delete_task(self, name: str) -> None:
        if name != CANDIDATE_TASK_NAME:
            raise CapturedPaperHostCutoverError(
                "TASK_NAME_INVALID", "only the exact candidate task can be deleted"
            )
        self._task_command(
            ("/Delete", "/TN", name, "/F"), allow_not_found=True
        )

    def find_candidate_tasks(
        self, invocation: CandidateInvocation
    ) -> tuple[TaskObservation, ...]:
        result = self._task_command(("/Query", "/FO", "CSV", "/NH"))
        assert result is not None
        text: str | None = None
        for encoding in ("utf-8-sig", "mbcs"):
            try:
                text = result.stdout.decode(encoding, errors="strict")
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text is None:
            raise CapturedPaperHostCutoverError(
                "TASK_INVENTORY_UNREADABLE", "Task Scheduler inventory encoding is unknown"
            )
        names: list[str] = []
        try:
            for row in csv.reader(text.splitlines()):
                if not row or not row[0].strip():
                    continue
                value = row[0].strip()
                names.append(value[1:] if value.startswith("\\") else value)
        except csv.Error as exc:
            raise CapturedPaperHostCutoverError(
                "TASK_INVENTORY_UNREADABLE", "Task Scheduler inventory is malformed"
            ) from exc
        matches: list[TaskObservation] = []
        expected_arguments = _quote_windows_arguments(invocation.launcher_arguments)
        for name in sorted(set(names), key=str.casefold):
            task = self._get_task_unrestricted(name)
            if task is None:
                continue
            try:
                command, arguments = _task_exec_from_xml(task.xml)
            except CapturedPaperHostCutoverError:
                continue
            if (
                os.path.normcase(command)
                == os.path.normcase(invocation.powershell_executable_path)
                # Case-insensitive: the registered Arguments carry the sealed
                # template's normcased path tokens while the invocation is
                # filesystem-resolved proper case (2026-07-17 bug class).
                and os.path.normcase(arguments) == os.path.normcase(expected_arguments)
            ):
                matches.append(task)
        return tuple(matches)

    def _identity_for_pid(
        self,
        pid: int,
        *,
        role: str,
        binding: LegacyProcessBinding | None = None,
    ) -> ProcessIdentity | None:
        try:
            process = self._psutil.Process(pid)
            create_time_ns = int(round(float(process.create_time()) * 1_000_000_000))
            executable_path = str(Path(process.exe()).resolve(strict=True))
            cmdline = tuple(str(item) for item in process.cmdline())
        except (self._psutil.NoSuchProcess, self._psutil.ZombieProcess):
            return None
        except (self._psutil.AccessDenied, OSError) as exc:
            raise CapturedPaperHostCutoverError(
                "PROCESS_INVENTORY_UNINSPECTABLE",
                f"cannot prove process identity for PID {pid}",
            ) from exc
        if not cmdline:
            return None
        _executable, executable_sha = _stable_local_file_unrooted(
            executable_path, field=f"process {pid} executable"
        )
        if binding is not None:
            if not (
                os.path.normcase(executable_path)
                == os.path.normcase(binding.executable_path)
                and executable_sha == binding.executable_sha256
                and any(
                    os.path.normcase(item)
                    == os.path.normcase(binding.bridge_script_path)
                    for item in cmdline
                )
                and _stable_local_file_unrooted(
                    binding.bridge_script_path,
                    field=f"process {pid} bridge script",
                )[1]
                == binding.bridge_script_sha256
                and cmdline == binding.expected_cmdline
                and sha256_json(list(cmdline)) == binding.expected_cmdline_sha256
            ):
                return None
        return ProcessIdentity(
            pid=pid,
            create_time_ns=create_time_ns,
            executable_path=executable_path,
            executable_sha256=executable_sha,
            cmdline=cmdline,
            cmdline_sha256=sha256_json(list(cmdline)),
            role=role,
            bridge_script_path=(binding.bridge_script_path if binding else None),
            bridge_script_sha256=(binding.bridge_script_sha256 if binding else None),
        )

    def get_process(self, pid: int, *, role: str) -> ProcessIdentity | None:
        return self._identity_for_pid(pid, role=role, binding=self._bindings.get(role))

    def _stop_exact_identity(self, expected: ProcessIdentity) -> None:
        binding = self._bindings.get(expected.role)
        actual = self._identity_for_pid(
            expected.pid, role=expected.role, binding=binding
        )
        if actual is None or actual.semantic_key() != expected.semantic_key():
            raise CapturedPaperHostCutoverError(
                "PROCESS_IDENTITY_DRIFT", "refusing to stop a changed/reused PID"
            )
        process = self._psutil.Process(expected.pid)
        process.terminate()
        try:
            process.wait(timeout=5)
            return
        except self._psutil.TimeoutExpired:
            pass
        actual = self._identity_for_pid(
            expected.pid, role=expected.role, binding=binding
        )
        if actual is None:
            return
        if actual.semantic_key() != expected.semantic_key():
            raise CapturedPaperHostCutoverError(
                "PROCESS_IDENTITY_DRIFT", "PID changed before forced process stop"
            )
        process.kill()
        process.wait(timeout=5)

    def stop_process(self, expected: ProcessIdentity) -> None:
        if expected.role not in self._bindings:
            raise CapturedPaperHostCutoverError(
                "PROCESS_ROLE_INVALID", "legacy stop lacks a sealed provenance binding"
            )
        self._stop_exact_identity(expected)

    def find_legacy_processes(
        self, bindings: Sequence[LegacyProcessBinding]
    ) -> tuple[ProcessIdentity, ...]:
        found: list[ProcessIdentity] = []
        # 2026-07-17: only a process sharing a sealed binding's executable
        # name can be a legacy bridge, so prefilter by name before the deep
        # identity inspection — process.exe() on every PID raises
        # AccessDenied on protected system processes for ANY caller, which
        # made this inventory impossible on a real host (first live
        # ValidateOnly to get past template validation died here).  Same
        # prefilter pattern as _candidate_processes; an uninspectable NAME
        # stays fail-closed, as does AccessDenied on a name-matched process.
        expected_names = {
            Path(binding.executable_path).name.casefold() for binding in bindings
        }
        try:
            for process in self._psutil.process_iter(
                attrs=["pid", "name"], ad_value=None
            ):
                pid = int(process.info["pid"])
                name = process.info.get("name")
                if name is None:
                    raise CapturedPaperHostCutoverError(
                        "PROCESS_INVENTORY_UNINSPECTABLE",
                        f"a process name could not be inspected (PID {pid})",
                    )
                if str(name).casefold() not in expected_names:
                    continue
                for binding in bindings:
                    identity = self._identity_for_pid(
                        pid, role=binding.role, binding=binding
                    )
                    if identity is not None:
                        found.append(identity)
        except (self._psutil.AccessDenied, OSError, KeyError, TypeError, ValueError) as exc:
            raise CapturedPaperHostCutoverError(
                "PROCESS_INVENTORY_UNINSPECTABLE",
                "legacy process inventory could not be completed",
            ) from exc
        unique = {item.semantic_key(): item for item in found}
        return tuple(sorted(unique.values(), key=lambda item: (item.role, item.pid)))

    def await_legacy_processes(
        self,
        bindings: Sequence[LegacyProcessBinding],
        *,
        timeout_seconds: float,
    ) -> tuple[ProcessIdentity, ...]:
        expected_roles = sorted(item.role for item in bindings)
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            found = self.find_legacy_processes(bindings)
            roles = sorted(item.role for item in found)
            if roles == expected_roles and len(roles) == len(set(roles)):
                return found
            if time.monotonic() >= deadline:
                return found
            time.sleep(0.1)

    @staticmethod
    def _cmdline_matches(identity: ProcessIdentity, arguments: Sequence[str]) -> bool:
        # Case-insensitive per element: the task-spawned argv carries the
        # sealed template's normcased path tokens while the invocation is
        # filesystem-resolved proper case (2026-07-17 bug class).  The
        # process chain stays hash-bound upstream; this is identification.
        return len(identity.cmdline) == len(arguments) + 1 and all(
            os.path.normcase(actual) == os.path.normcase(str(expected))
            for actual, expected in zip(identity.cmdline[1:], arguments)
        )

    def _candidate_processes(
        self, invocation: CandidateInvocation
    ) -> tuple[CandidateProcessObservation, ...]:
        found: list[CandidateProcessObservation] = []
        try:
            for process in self._psutil.process_iter(
                attrs=["pid", "name", "exe", "cmdline"], ad_value=None
            ):
                pid = int(process.info["pid"])
                info_cmdline = tuple(str(item) for item in (process.info.get("cmdline") or ()))
                info_exe = str(process.info.get("exe") or "")
                info_name = str(process.info.get("name") or "").casefold()
                launcher_tokens = {invocation.launcher_script_path}
                service_tokens = {
                    invocation.service_script_path,
                    invocation.host_ready_receipt_base,
                }
                if info_cmdline:
                    normcased_cmdline = {
                        os.path.normcase(item) for item in info_cmdline
                    }
                    looks_launcher = any(
                        os.path.normcase(token) in normcased_cmdline
                        for token in launcher_tokens
                    )
                    looks_service = any(
                        os.path.normcase(token) in normcased_cmdline
                        for token in service_tokens
                    )
                else:
                    looks_launcher = (
                        os.path.normcase(info_exe)
                        == os.path.normcase(invocation.powershell_executable_path)
                        or info_name == Path(invocation.powershell_executable_path).name.casefold()
                    )
                    looks_service = (
                        os.path.normcase(info_exe)
                        == os.path.normcase(invocation.python_executable_path)
                        or info_name == Path(invocation.python_executable_path).name.casefold()
                    )
                if not looks_launcher and not looks_service:
                    continue
                identity = self._identity_for_pid(pid, role="candidate_probe")
                if identity is None:
                    continue
                normcased_identity_cmdline = {
                    os.path.normcase(item) for item in identity.cmdline
                }
                full_launcher_relevant = any(
                    os.path.normcase(token) in normcased_identity_cmdline
                    for token in launcher_tokens
                )
                full_service_relevant = any(
                    os.path.normcase(token) in normcased_identity_cmdline
                    for token in service_tokens
                )
                if not full_launcher_relevant and not full_service_relevant:
                    # A coarse executable/name prefilter can select an
                    # unrelated PowerShell/Python process.  Once its complete
                    # argv is inspectable and contains no candidate token, it
                    # is safely excluded.
                    continue
                launcher_match = (
                    full_launcher_relevant
                    and os.path.normcase(identity.executable_path)
                    == os.path.normcase(invocation.powershell_executable_path)
                    and identity.executable_sha256
                    == invocation.powershell_executable_sha256
                    and self._cmdline_matches(identity, invocation.launcher_arguments)
                )
                service_match = (
                    full_service_relevant
                    and os.path.normcase(identity.executable_path)
                    == os.path.normcase(invocation.python_executable_path)
                    and identity.executable_sha256
                    == invocation.python_executable_sha256
                    and self._cmdline_matches(identity, invocation.service_arguments)
                )
                if launcher_match == service_match:
                    raise CapturedPaperHostCutoverError(
                        "CANDIDATE_PROCESS_IDENTITY_MISMATCH",
                        "candidate-token process is ambiguous or differs from sealed argv",
                    )
                role = "candidate_launcher" if launcher_match else "candidate_service"
                exact = ProcessIdentity(
                    pid=identity.pid,
                    create_time_ns=identity.create_time_ns,
                    executable_path=identity.executable_path,
                    executable_sha256=identity.executable_sha256,
                    cmdline=identity.cmdline,
                    cmdline_sha256=identity.cmdline_sha256,
                    role=role,
                )
                found.append(
                    CandidateProcessObservation(
                        "launcher" if launcher_match else "service", exact
                    )
                )
        except (self._psutil.AccessDenied, OSError, KeyError, TypeError, ValueError) as exc:
            raise CapturedPaperHostCutoverError(
                "PROCESS_INVENTORY_UNINSPECTABLE",
                "candidate process inventory could not be completed",
            ) from exc
        return tuple(sorted(found, key=lambda item: (item.kind, item.identity.pid)))

    def await_candidate_processes(
        self, invocation: CandidateInvocation, *, timeout_seconds: float
    ) -> tuple[CandidateProcessObservation, ...]:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            found = self._candidate_processes(invocation)
            if sorted(item.kind for item in found) == ["launcher", "service"]:
                return found
            if time.monotonic() >= deadline:
                return found
            time.sleep(0.1)

    def stop_candidate_process(
        self, expected: CandidateProcessObservation, invocation: CandidateInvocation
    ) -> None:
        current = {
            (item.kind, item.identity.pid): item
            for item in self._candidate_processes(invocation)
        }.get((expected.kind, expected.identity.pid))
        if current is None or current.identity.semantic_key() != expected.identity.semantic_key():
            raise CapturedPaperHostCutoverError(
                "PROCESS_IDENTITY_DRIFT",
                "refusing to stop a candidate process whose identity changed",
            )
        self._stop_exact_identity(expected.identity)

    def read_service_startup_receipt(
        self,
        invocation: CandidateInvocation,
        expected_service: ProcessIdentity,
        *,
        phase: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        del expected_service
        if phase not in {"prepared", "started"}:
            raise CapturedPaperHostCutoverError(
                "STARTUP_RECEIPT_UNAVAILABLE", "unsupported startup receipt phase"
            )
        value = (
            invocation.host_ready_receipt_base
            if phase == "prepared"
            else f"{invocation.host_ready_receipt_base}.started.json"
        )
        raw_path = Path(value)
        if not _is_local_absolute(raw_path):
            raise CapturedPaperHostCutoverError(
                "STARTUP_RECEIPT_UNAVAILABLE",
                "service startup receipt path is not an absolute local path",
            )
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while not raw_path.is_file():
            if time.monotonic() >= deadline:
                raise CapturedPaperHostCutoverError(
                    "STARTUP_RECEIPT_UNAVAILABLE",
                    f"service did not publish {phase} receipt in time",
                )
            time.sleep(0.05)
        parent = raw_path.resolve(strict=True).parent
        path, raw, _digest = _stable_read(
            raw_path,
            roots=(parent,),
            field=f"service startup {phase} receipt",
            max_bytes=64 * 1024,
        )
        del path
        receipt = _strict_json(raw, f"service startup {phase} receipt")
        if raw != _canonical_json_bytes(receipt):
            raise CapturedPaperHostCutoverError(
                "STARTUP_RECEIPT_INVALID",
                "service startup receipt is not canonical JSON",
            )
        return receipt


def _report_document(report: CutoverReport) -> Mapping[str, Any]:
    return {
        "schema_version": "chili.captured-paper-host-cutover-report.v1",
        "mode": report.mode,
        "verdict": report.verdict,
        "activation_generation": report.activation_generation,
        "account_scope": "alpaca:paper",
        "manifest_sha256": report.manifest_sha256,
        "resolved_task_xml_sha256": report.resolved_task_xml_sha256,
        "journal_path": str(report.journal_path) if report.journal_path else None,
        "mutation_count": report.mutation_count,
        "live_cash_authorized": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate/apply/rollback the hash-bound captured Alpaca PAPER host"
    )
    parser.add_argument(
        "--mode",
        choices=(MODE_VALIDATE_ONLY, MODE_APPLY, MODE_ROLLBACK, MODE_RECOVER_ONLY),
        default=MODE_VALIDATE_ONLY,
    )
    parser.add_argument("--manifest")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--candidate-root")
    parser.add_argument("--allow-read-root", action="append", required=True)
    parser.add_argument("--task-snapshot")
    parser.add_argument("--process-snapshot")
    parser.add_argument("--restore-plan")
    parser.add_argument("--candidate-task-template")
    parser.add_argument("--candidate-action")
    parser.add_argument("--journal-root", required=True)
    parser.add_argument("--confirm-fake-money-paper")
    return parser


def _load_activation_for_mode(
    *,
    mode: str,
    manifest_path: str | Path,
    manifest_sha256: str,
    candidate_root: str | Path,
    allowed_read_roots: Sequence[Path],
) -> activation_contract.VerifiedCapturedPaperActivation:
    if mode != MODE_ROLLBACK:
        return activation_contract.load_captured_paper_activation(
            manifest_path,
            expected_manifest_sha256=manifest_sha256,
            candidate_root=candidate_root,
            allowed_read_roots=allowed_read_roots,
        )
    # Expiry revokes the ability to start/continue PAPER; it must not revoke
    # the ability to undo a cutover.  For Rollback only, re-run the complete
    # hash/schema/source/receipt verifier at the manifest's own sealed
    # generation time.  This cannot be reached by Apply/ValidateOnly and is
    # never interpreted as current order authority.
    _path, raw, _digest = _stable_read(
        manifest_path,
        roots=allowed_read_roots,
        field="rollback activation manifest",
        expected_sha256=manifest_sha256,
    )
    document = _strict_json(raw, "rollback activation manifest")
    generated_at = _parse_utc(document.get("generated_at"), "manifest.generated_at")
    return activation_contract.load_captured_paper_activation(
        manifest_path,
        expected_manifest_sha256=manifest_sha256,
        candidate_root=candidate_root,
        allowed_read_roots=allowed_read_roots,
        wall_clock=lambda: generated_at,
    )


def _main_with_host_lock_held(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.mode == MODE_APPLY and (
        arguments.confirm_fake_money_paper != APPLY_CONFIRMATION
    ):
        print(
            json.dumps(
                {
                    "verdict": "REJECTED",
                    "reason": "Apply requires the exact fake-money PAPER confirmation",
                    "live_cash_authorized": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        roots = _strict_roots(arguments.allow_read_root)
        if arguments.mode == MODE_RECOVER_ONLY:
            discovered = _discover_single_active_rollback_capsule(
                journal_root=arguments.journal_root,
                caller_roots=roots,
            )
            if discovered is None:
                print(
                    _canonical_json_bytes(
                        {
                            "schema_version": (
                                "chili.captured-paper-host-cutover-recovery.v1"
                            ),
                            "mode": MODE_RECOVER_ONLY,
                            "verdict": "NO_RECOVERY_REQUIRED",
                            "account_scope": "alpaca:paper",
                            "mutation_count": 0,
                            "live_cash_authorized": False,
                        }
                    ).decode("utf-8")
                )
                return 0
            prepared, state = discovered
        elif any(
            not getattr(arguments, field)
            for field in (
                "manifest",
                "manifest_sha256",
                "candidate_root",
                "task_snapshot",
                "process_snapshot",
                "restore_plan",
                "candidate_task_template",
                "candidate_action",
            )
        ):
            raise CapturedPaperHostCutoverError(
                "ACTIVATION_ARGUMENTS_INCOMPLETE",
                "non-recovery mode requires the complete sealed activation inputs",
            )
        elif arguments.mode == MODE_ROLLBACK:
            # Emergency rollback authority is the immutable capsule published
            # before the first host mutation.  Current manifests, receipts,
            # templates, and worktree source are deliberately not consulted.
            prepared = _discover_rollback_capsule(
                journal_root=arguments.journal_root,
                manifest_sha256=arguments.manifest_sha256,
                caller_roots=roots,
            )
        else:
            verified = _load_activation_for_mode(
                mode=arguments.mode,
                manifest_path=arguments.manifest,
                manifest_sha256=arguments.manifest_sha256,
                candidate_root=arguments.candidate_root,
                allowed_read_roots=roots,
            )
            prepared = prepare_cutover(
                verified,
                allowed_read_roots=roots,
                task_snapshot_path=arguments.task_snapshot,
                process_snapshot_path=arguments.process_snapshot,
                restore_plan_path=arguments.restore_plan,
                candidate_task_template_path=arguments.candidate_task_template,
                candidate_action_path=arguments.candidate_action,
            )
        backend = WindowsHostCutoverBackend(bindings=prepared.restore_plan.bindings)
        executor = CapturedPaperHostCutoverExecutor(
            prepared=prepared,
            backend=backend,
            journal_root=Path(arguments.journal_root),
        )
        if arguments.mode == MODE_RECOVER_ONLY:
            adopted = executor.adopt_pre_identity_journal_at_exact_baseline()
            if adopted is not None:
                report = adopted
            elif state == "applied":
                report = executor.apply()
            else:
                report = executor.rollback()
        elif arguments.mode == MODE_VALIDATE_ONLY:
            report = executor.validate_only()
        elif arguments.mode == MODE_APPLY:
            # This is the real current-host ValidateOnly, deliberately later
            # than the preactivation rollback baseline and immediately before
            # Apply.  apply() repeats the baseline assertion under its journal
            # lock before its first host mutation.
            executor.validate_only()
            report = executor.apply()
        else:
            report = executor.rollback()
    except (
        CapturedPaperHostCutoverError,
        activation_contract.CapturedPaperActivationContractError,
        OSError,
        ValueError,
    ) as exc:
        # 2026-07-17 observability: the bare reason_code hid every primary
        # failure behind whichever error fired last (a COMPENSATING_ROLLBACK
        # code masked the real Apply defect for a full live cycle).  Bounded
        # head+tail of the traceback chain names the originating raise.
        detail = traceback.format_exc()
        if len(detail) > 2400:
            detail = detail[:1400] + "\n...[middle truncated]...\n" + detail[-1000:]
        print(
            json.dumps(
                {
                    "verdict": "REJECTED",
                    "reason_code": getattr(exc, "code", type(exc).__name__),
                    "error_detail": detail,
                    "live_cash_authorized": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(_canonical_json_bytes(_report_document(report)).decode("utf-8"))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Serialize every host cutover generation through one fixed host lock."""

    arguments = _parser().parse_args(argv)
    # Preserve the cheap, mutation-free confirmation rejection before any
    # journal path is touched.
    if arguments.mode == MODE_APPLY and (
        arguments.confirm_fake_money_paper != APPLY_CONFIRMATION
    ):
        return _main_with_host_lock_held(argv)
    try:
        roots = _strict_roots(arguments.allow_read_root)
        raw_root = Path(arguments.journal_root)
        if arguments.mode == MODE_RECOVER_ONLY and not os.path.lexists(raw_root):
            # A normal Apply cannot start without this pre-existing sealed
            # root, so there is no competing transaction to serialize here.
            return _main_with_host_lock_held(argv)
        root = _strict_existing_dir(
            raw_root, roots=roots, field="host cutover journal_root"
        )
        # Root-independent by construction: different valid artifact/journal
        # roots cannot own concurrent host cutovers.  This lock is distinct
        # from the outer activation-runner lock so the runner may invoke this
        # child process without self-deadlocking.
        with _JournalLock(
            _HOST_WIDE_CUTOVER_LOCK_PATH,
            mutex_name=_HOST_WIDE_CUTOVER_MUTEX_NAME,
        ):
            return _main_with_host_lock_held(argv)
    except (
        CapturedPaperHostCutoverError,
        activation_contract.CapturedPaperActivationContractError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "verdict": "REJECTED",
                    "reason_code": getattr(exc, "code", type(exc).__name__),
                    "error_detail": "host-global cutover lock could not be acquired",
                    "live_cash_authorized": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "APPLY_CONFIRMATION",
    "CANDIDATE_ACTION_SCHEMA",
    "CANDIDATE_TASK_NAME",
    "CandidateInvocation",
    "CandidateProcessObservation",
    "CapturedPaperHostCutoverError",
    "CapturedPaperHostCutoverExecutor",
    "CutoverReport",
    "HostCutoverBackend",
    "LegacyProcessBinding",
    "LegacyExecutionLaneObservation",
    "LegacyTaskLaunchContract",
    "LEGACY_EXECUTION_LANE_NAME",
    "LEGACY_EXECUTION_LANE_SCHEMA",
    "LEGACY_DIRECT_LAUNCH_KIND",
    "LEGACY_WRAPPER_LAUNCH_KIND",
    "MANIFEST_PATH_TOKEN",
    "MANIFEST_SHA256_TOKEN",
    "MODE_APPLY",
    "MODE_ROLLBACK",
    "MODE_RECOVER_ONLY",
    "MODE_VALIDATE_ONLY",
    "PREACTIVATION_ROLLBACK_BASELINE_MODE",
    "PREACTIVATION_ROLLBACK_BASELINE_SCHEMA",
    "PROCESS_SNAPSHOT_SCHEMA",
    "ROLLBACK_CAPSULE_SCHEMA",
    "PreparedCutover",
    "PreActivationRollbackBaseline",
    "PreActivationRollbackContext",
    "ProcessIdentity",
    "REQUIRED_LEGACY_PROCESS_ROLES",
    "REQUIRED_LEGACY_TASKS",
    "RESTORE_PLAN_SCHEMA",
    "SINGLETON_POLICY",
    "STARTUP_PREPARED_SCHEMA",
    "STARTUP_PERMIT_SCHEMA",
    "STARTUP_STARTED_SCHEMA",
    "STARTUP_REVOKED_SCHEMA",
    "TASK_SNAPSHOT_SCHEMA",
    "TaskObservation",
    "WindowsHostCutoverBackend",
    "build_candidate_action_document",
    "build_candidate_task_xml_template",
    "build_legacy_wrapper_launch_contracts",
    "build_preactivation_rollback_baseline_document",
    "build_rollback_capsule_document",
    "build_startup_prepared_receipt",
    "build_startup_started_receipt",
    "build_process_snapshot_document",
    "build_restore_plan_document",
    "build_task_snapshot_document",
    "candidate_action_sha256",
    "main",
    "prepare_cutover",
    "prepare_preactivation_rollback_baseline",
    "sha256_bytes",
    "sha256_json",
]
