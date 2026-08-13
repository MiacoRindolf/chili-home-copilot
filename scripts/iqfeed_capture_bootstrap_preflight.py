"""Hash-bound, no-provider/no-DB preflight for the IQFeed capture bootstrap.

This script intentionally cannot launch IQConnect, import the operational
bridge, create a SQLAlchemy engine, create the capture store, or enable broker
execution.  It validates the immutable inputs which a future host bootstrap
must possess *before* any of those side effects are permitted:

* an externally pinned bootstrap manifest;
* an externally pinned, already-observed account/config startup artifact;
* the exact source files which would participate in capture;
* a content-addressed resource benchmark and its recomputed finite binding;
* local, non-reparse, allowlisted read/write paths; and
* an explicit all-off execution posture.

A successful result means only ``BOOTSTRAP_PREFLIGHT_VALID``.  It is never an
activation, certification, replay-coverage, paper-readiness, or profitability
receipt.  Current IQFeed Q frames still lack an exact quote event clock, the
provider lifecycle has not yet been attached to a unified hot run, and L2
checkpoint completion/watermark authority is still unavailable.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
from types import MappingProxyType, ModuleType
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

import psutil


UTC = timezone.utc
BOOTSTRAP_MANIFEST_SCHEMA_VERSION = (
    "chili.iqfeed-capture-bootstrap-preflight.v3"
)
STARTUP_EVIDENCE_SCHEMA_VERSION = (
    "chili.iqfeed-capture-startup-evidence.v2"
)
CODE_BUILD_SCHEMA_VERSION = "chili.capture-code-build.v1"
RUN_CONFIGURATION_SCHEMA_VERSION = (
    "chili.live-replay-capture-run-configuration.v1"
)
BENCHMARK_SCHEMA_VERSION = "chili.replay-capture-benchmark.v7"
BENCHMARK_AUTHORITY_MANIFEST_SCHEMA_VERSION = (
    "chili.captured-paper-benchmark-authority-manifest.v2"
)
BENCHMARK_RUNNER_AUTHORITY_SCHEMA_VERSION = (
    "chili.captured-paper-benchmark-runner-authority.v1"
)
BENCHMARK_LAUNCH_RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-benchmark-authority-receipt.v2"
)
BENCHMARK_EXECUTION_RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-benchmark-execution-receipt.v2"
)
BENCHMARK_EXECUTION_CLAIM_SCHEMA_VERSION = (
    "chili.captured-paper-benchmark-execution-claim.v1"
)
BENCHMARK_AUTHORITY_BUILDER_SHA256 = (
    "3d146aebf5b3a881322a8c7450dd79510b21c0474495c291db121715e2bab1e3"
)
BENCHMARK_AUTHORITY_RUNNER_SHA256 = (
    "2b11a6b348b7f16fc4d4bad80922c5ad43f043380a1360fe51c6d9ac31eca349"
)
IQFEED_L1_CLOCK_CONTRACT_SCHEMA_VERSION = "chili.iqfeed-l1-clock-contract.v2"
IQFEED_L2_CLOCK_CONTRACT_SCHEMA_VERSION = "chili.iqfeed-l2-clock-contract.v1"
IQFEED_HANDOFF_BUDGET_SCHEMA_VERSION = "chili.iqfeed-capture-handoff-budget.v2"
CAPTURE_MODE = "diagnostic_only"
PRESSURE_WRITE_LATENCY_PROFILE = (
    "chili.capture-pressure.durable-write-fsync-helper-process.v1"
)

_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_STARTUP_BYTES = 8 * 1024 * 1024
_MAX_BENCHMARK_BYTES = 32 * 1024 * 1024
_MAX_AUTHORITY_BYTES = 8 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
_MAX_OWNERSHIP_MARKER_BYTES = 64 * 1024
_MAX_SOURCE_BYTES = 32 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_ATTRIBUTE = 0x400
try:
    _PROCESS_PYTHON_EXECUTABLE = Path(sys.executable).resolve(strict=True)
except OSError:
    _PROCESS_PYTHON_EXECUTABLE = Path(sys.executable)

_REQUIRED_SOURCE_ROLES = frozenset(
    {
        "benchmark_replay_capture_runtime",
        "captured_paper_pressure_probe",
        "captured_paper_isolated_stage0",
        "app_migrations",
        "iqfeed_capture_bootstrap",
        "iqfeed_capture_bootstrap_preflight",
        "iqfeed_capture_host",
        "iqfeed_capture_host_launcher",
        "iqfeed_l1_capture",
        "iqfeed_l2_capture",
        "iqfeed_depth_bridge",
        "iqfeed_trade_bridge",
        "live_replay_capture",
        "first_dip_tape_policy",
        "replay_errors",
        "replay_capture_contract",
        "replay_capture_runtime",
    }
)
_REQUIRED_OFF_FLAGS = (
    "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED",
    "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED",
    "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED",
)
_ARCHITECTURE_BLOCKERS = (
    "bootstrap_preflight_only_not_an_installed_launcher",
    "iqfeed_l1_exact_quote_event_clock_unavailable",
    "iqfeed_provider_socket_loops_not_launched_by_unified_host",
    "iqfeed_unified_capture_host_not_installed_or_launched",
    "live_fsm_hot_admission_boundary_not_attached",
    "iqfeed_l2_initial_snapshot_completion_watermark_unavailable",
    "paper_live_recertification_pending",
)


class BootstrapPreflightError(RuntimeError):
    """Typed fail-closed preflight rejection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = str(code)
        self.message = str(message)


@dataclass(frozen=True)
class HashBoundJsonArtifact:
    path: Path
    sha256: str
    document: Mapping[str, Any]


@dataclass(frozen=True)
class IqfeedCaptureBootstrapPreflight:
    manifest_path: Path
    manifest_sha256: str
    startup_evidence_path: Path
    startup_evidence_sha256: str
    resource_benchmark_path: Path
    resource_benchmark_sha256: str
    benchmark_authority_manifest_path: Path
    benchmark_authority_manifest_sha256: str
    benchmark_runner_authority_path: Path
    benchmark_runner_authority_sha256: str
    benchmark_launch_receipt_path: Path
    benchmark_launch_receipt_sha256: str
    benchmark_execution_receipt_path: Path
    benchmark_execution_receipt_sha256: str
    benchmark_execution_claim_path: Path
    benchmark_execution_claim_sha256: str
    benchmark_authority_read_roots: tuple[Path, ...]
    resource_binding: Any
    capture_store_root: Path
    run_configuration: Mapping[str, Any]
    handoff_configuration: Mapping[str, Any]
    source_paths: Mapping[str, Path]
    source_hashes: Mapping[str, str]
    startup_evidence_hashes: Mapping[str, str]
    startup_captured_at: datetime
    startup_process_instance_id: str
    startup_generation: int
    broker: str
    broker_environment: str
    bridge_configuration: Mapping[str, Any]
    benchmark_authority_reasons: tuple[str, ...]

    @property
    def report(self) -> dict[str, Any]:
        blockers = tuple(
            dict.fromkeys(
                (
                    *_ARCHITECTURE_BLOCKERS,
                    *(
                        "resource_capacity_authority_diagnostic_only:"
                        + reason
                        for reason in self.benchmark_authority_reasons
                    ),
                )
            )
        )
        payload: dict[str, Any] = {
            "schema_version": (
                "chili.iqfeed-capture-bootstrap-preflight-report.v3"
            ),
            "verdict": "BOOTSTRAP_PREFLIGHT_VALID",
            "preflight_valid": True,
            "capture_mode": CAPTURE_MODE,
            "activation_authorized": False,
            "certification_eligible": False,
            "paper_live_execution_enabled": False,
            "provider_or_database_started": False,
            "network_or_current_database_fallback_allowed": False,
            "manifest": {
                "path": str(self.manifest_path),
                "sha256": self.manifest_sha256,
            },
            "startup_evidence": {
                "path": str(self.startup_evidence_path),
                "sha256": self.startup_evidence_sha256,
                "captured_at": self.startup_captured_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "process_instance_id": self.startup_process_instance_id,
                "generation": self.startup_generation,
                "broker": self.broker,
                "broker_environment": self.broker_environment,
                **dict(self.startup_evidence_hashes),
            },
            "resource_benchmark": {
                "path": str(self.resource_benchmark_path),
                "sha256": self.resource_benchmark_sha256,
                "capacity_authority": CAPTURE_MODE,
                "binding_sha256": self.resource_binding.binding_sha256,
                "resource_hashes": self.resource_binding.hashes,
            },
            "benchmark_authority_manifest": {
                "path": str(self.benchmark_authority_manifest_path),
                "sha256": self.benchmark_authority_manifest_sha256,
            },
            "benchmark_runner_authority": {
                "path": str(self.benchmark_runner_authority_path),
                "sha256": self.benchmark_runner_authority_sha256,
            },
            "benchmark_launch_receipt": {
                "path": str(self.benchmark_launch_receipt_path),
                "sha256": self.benchmark_launch_receipt_sha256,
            },
            "benchmark_execution_receipt": {
                "path": str(self.benchmark_execution_receipt_path),
                "sha256": self.benchmark_execution_receipt_sha256,
            },
            "benchmark_execution_claim": {
                "path": str(self.benchmark_execution_claim_path),
                "sha256": self.benchmark_execution_claim_sha256,
            },
            "benchmark_authority_read_roots": [
                str(path) for path in self.benchmark_authority_read_roots
            ],
            "capture_store_root": str(self.capture_store_root),
            "run_configuration": dict(self.run_configuration),
            "handoff_configuration": dict(self.handoff_configuration),
            "source_hashes": dict(sorted(self.source_hashes.items())),
            "blocking_reasons": list(blockers),
        }
        payload["preflight_report_sha256"] = _sha256_json(payload)
        return payload


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
        raise BootstrapPreflightError(
            "NON_CANONICAL_JSON", "artifact is not canonical JSON"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _require_sha256(value: Any, field: str) -> str:
    resolved = str(value or "").strip().lower()
    if len(resolved) != 64 or any(ch not in "0123456789abcdef" for ch in resolved):
        raise BootstrapPreflightError("INVALID_SHA256", f"{field} is malformed")
    return resolved


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BootstrapPreflightError("INVALID_INTEGER", f"{field} is malformed")
    resolved = int(value)
    if resolved <= 0:
        raise BootstrapPreflightError(
            "INVALID_INTEGER", f"{field} must be a positive integer"
        )
    return resolved


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise BootstrapPreflightError("INVALID_NUMBER", f"{field} is malformed")
    try:
        resolved = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BootstrapPreflightError("INVALID_NUMBER", f"{field} is malformed") from exc
    if not math.isfinite(resolved) or resolved <= 0:
        raise BootstrapPreflightError(
            "INVALID_NUMBER", f"{field} must be finite and positive"
        )
    return resolved


def _expect_mapping(value: Any, field: str, *, nonempty: bool = False) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or (nonempty and not value):
        raise BootstrapPreflightError("INVALID_OBJECT", f"{field} is malformed")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Iterable[str], field: str) -> None:
    expected_set = set(expected)
    actual = set(value)
    if actual != expected_set:
        missing = sorted(expected_set - actual)
        extra = sorted(actual - expected_set)
        raise BootstrapPreflightError(
            "SCHEMA_MISMATCH",
            f"{field} fields differ; missing={missing} extra={extra}",
        )


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise BootstrapPreflightError("INVALID_TIMESTAMP", f"{field} is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BootstrapPreflightError(
            "INVALID_TIMESTAMP", f"{field} is malformed"
        ) from exc
    if parsed.tzinfo is None:
        raise BootstrapPreflightError(
            "INVALID_TIMESTAMP", f"{field} must be timezone-aware"
        )
    return parsed.astimezone(UTC)


def _reject_constant(value: str) -> Any:
    raise BootstrapPreflightError(
        "NONFINITE_JSON", f"non-finite JSON number is forbidden: {value}"
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapPreflightError(
                "DUPLICATE_JSON_KEY", f"duplicate JSON key is forbidden: {key}"
            )
        result[key] = value
    return result


def _strict_json(raw: bytes, field: str) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BootstrapPreflightError("INVALID_UTF8", f"{field} is not UTF-8") from exc
    if text.startswith("\ufeff"):
        raise BootstrapPreflightError("INVALID_UTF8", f"{field} contains a BOM")
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except BootstrapPreflightError:
        raise
    except json.JSONDecodeError as exc:
        raise BootstrapPreflightError(
            "INVALID_JSON", f"{field} is not valid JSON"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise BootstrapPreflightError("INVALID_JSON", f"{field} root must be an object")
    if raw != _canonical_json_bytes(parsed):
        raise BootstrapPreflightError(
            "NON_CANONICAL_JSON", f"{field} bytes are not canonical"
        )
    return parsed


def _is_reparse(status: os.stat_result) -> bool:
    return bool(getattr(status, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)


def _default_local_drive_check(path: Path) -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))
    except Exception:
        return False
    # Only a fixed local disk is accepted.  Mapped/network/removable roots are
    # intentionally not valid bootstrap evidence or capture-store locations.
    return int(drive_type) == 3


def _lexical_absolute_local_path(
    raw: Any,
    *,
    field: str,
    local_drive_check: Callable[[Path], bool],
) -> Path:
    text = str(raw or "").strip()
    lowered = text.lower()
    if (
        not text
        or "\x00" in text
        or lowered.startswith(("file:", "http:", "https:"))
        or text.startswith(("\\\\", "//", "\\?\\", "\\.\\"))
    ):
        raise BootstrapPreflightError(
            "NONLOCAL_PATH", f"{field} must be a local absolute path"
        )
    path = Path(text)
    if not path.is_absolute():
        raise BootstrapPreflightError(
            "NONLOCAL_PATH", f"{field} must be an absolute path"
        )
    if os.name == "nt":
        tail = text[2:] if len(text) >= 2 and text[1] == ":" else text
        if ":" in tail:
            raise BootstrapPreflightError(
                "NONLOCAL_PATH", f"{field} contains an alternate data stream"
            )
    if not local_drive_check(path):
        raise BootstrapPreflightError(
            "NONLOCAL_PATH", f"{field} is not on a fixed local drive"
        )
    return Path(os.path.abspath(os.path.normpath(str(path))))


def _identity_tuple(status: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_size),
        int(status.st_mtime_ns),
    )


def _check_existing_components(path: Path, *, require_leaf: bool, field: str) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    leaf_seen = False
    for index, part in enumerate(parts):
        current = current / part
        try:
            status = current.lstat()
        except FileNotFoundError:
            if require_leaf or index != len(parts) - 1:
                if require_leaf:
                    raise BootstrapPreflightError(
                        "MISSING_PATH", f"{field} does not exist: {current}"
                    )
                # A write target may have multiple not-yet-created descendants.
                return
            return
        if stat.S_ISLNK(status.st_mode) or _is_reparse(status):
            raise BootstrapPreflightError(
                "REPARSE_PATH", f"{field} contains a symlink/reparse point: {current}"
            )
        leaf_seen = index == len(parts) - 1
    if require_leaf and not leaf_seen:
        raise BootstrapPreflightError("MISSING_PATH", f"{field} does not exist")


def _normalized_roots(
    roots: Sequence[str | Path],
    *,
    field: str,
    local_drive_check: Callable[[Path], bool],
) -> tuple[Path, ...]:
    if not roots:
        raise BootstrapPreflightError(
            "ALLOWLIST_REQUIRED", f"at least one {field} root is required"
        )
    normalized: list[Path] = []
    for index, raw in enumerate(roots):
        root = _lexical_absolute_local_path(
            raw,
            field=f"{field}[{index}]",
            local_drive_check=local_drive_check,
        )
        _check_existing_components(root, require_leaf=True, field=f"{field}[{index}]")
        status = root.lstat()
        if not stat.S_ISDIR(status.st_mode):
            raise BootstrapPreflightError(
                "INVALID_ALLOWLIST_ROOT", f"{field}[{index}] is not a directory"
            )
        normalized.append(root)
    return tuple(dict.fromkeys(normalized))


def _inside_any(path: Path, roots: Sequence[Path], *, allow_equal: bool) -> bool:
    target = os.path.normcase(os.path.abspath(str(path)))
    for root in roots:
        candidate = os.path.normcase(os.path.abspath(str(root)))
        try:
            common = os.path.commonpath((target, candidate))
        except ValueError:
            continue
        if common == candidate and (allow_equal or target != candidate):
            return True
    return False


def _validated_read_path(
    raw: Any,
    *,
    field: str,
    roots: Sequence[Path],
    local_drive_check: Callable[[Path], bool],
) -> Path:
    path = _lexical_absolute_local_path(
        raw, field=field, local_drive_check=local_drive_check
    )
    if not _inside_any(path, roots, allow_equal=False):
        raise BootstrapPreflightError(
            "PATH_OUTSIDE_ALLOWLIST", f"{field} is outside every read root"
        )
    _check_existing_components(path, require_leaf=True, field=field)
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode):
        raise BootstrapPreflightError(
            "NOT_REGULAR_FILE", f"{field} is not a regular file"
        )
    return path


def _validated_write_root(
    raw: Any,
    *,
    field: str,
    roots: Sequence[Path],
    local_drive_check: Callable[[Path], bool],
) -> Path:
    path = _lexical_absolute_local_path(
        raw, field=field, local_drive_check=local_drive_check
    )
    if not _inside_any(path, roots, allow_equal=False):
        raise BootstrapPreflightError(
            "PATH_OUTSIDE_ALLOWLIST",
            f"{field} must be a strict descendant of a write root",
        )
    _check_existing_components(path, require_leaf=False, field=field)
    if path.exists():
        status = path.lstat()
        if not stat.S_ISDIR(status.st_mode):
            raise BootstrapPreflightError(
                "INVALID_STORE_ROOT", f"{field} exists but is not a directory"
            )
    return path


def _read_bytes_stable(path: Path, *, field: str, max_bytes: int) -> bytes:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or _is_reparse(before):
        raise BootstrapPreflightError("REPARSE_PATH", f"{field} is a reparse point")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BootstrapPreflightError("READ_FAILED", f"cannot open {field}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity_tuple(opened) != _identity_tuple(before):
            raise BootstrapPreflightError(
                "FILE_CHANGED", f"{field} changed between lstat and open"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise BootstrapPreflightError(
                    "FILE_TOO_LARGE", f"{field} exceeds its byte limit"
                )
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    after = path.lstat()
    if _identity_tuple(after) != _identity_tuple(before):
        raise BootstrapPreflightError("FILE_CHANGED", f"{field} changed while read")
    return b"".join(chunks)


def _read_hash_bound_json(
    raw_path: Any,
    expected_sha256: Any,
    *,
    field: str,
    roots: Sequence[Path],
    max_bytes: int,
    local_drive_check: Callable[[Path], bool],
    content_addressed_filename: bool = True,
) -> HashBoundJsonArtifact:
    expected = _require_sha256(expected_sha256, f"{field}.sha256")
    path = _validated_read_path(
        raw_path,
        field=f"{field}.path",
        roots=roots,
        local_drive_check=local_drive_check,
    )
    raw = _read_bytes_stable(path, field=field, max_bytes=max_bytes)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise BootstrapPreflightError(
            "HASH_MISMATCH", f"{field} SHA-256 does not match its external pin"
        )
    if content_addressed_filename and (path.suffix.lower() != ".json" or path.stem.lower() != actual):
        raise BootstrapPreflightError(
            "NOT_CONTENT_ADDRESSED",
            f"{field} filename must be <canonical-sha256>.json",
        )
    return HashBoundJsonArtifact(
        path=path,
        sha256=actual,
        document=_strict_json(raw, field),
    )


def _read_hash_bound_source(
    raw_path: Any,
    expected_sha256: Any,
    *,
    field: str,
    roots: Sequence[Path],
    local_drive_check: Callable[[Path], bool],
) -> tuple[Path, str]:
    expected = _require_sha256(expected_sha256, f"{field}.sha256")
    path = _validated_read_path(
        raw_path,
        field=f"{field}.path",
        roots=roots,
        local_drive_check=local_drive_check,
    )
    raw = _read_bytes_stable(path, field=field, max_bytes=_MAX_SOURCE_BYTES)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise BootstrapPreflightError(
            "SOURCE_HASH_MISMATCH", f"{field} SHA-256 does not match"
        )
    return path, actual


_BENCHMARK_AUTHORITY_SOURCE_ROLES: Mapping[str, str] = MappingProxyType(
    {
        "benchmark": "benchmark_replay_capture_runtime",
        "contract": "replay_capture_contract",
        "first_dip_tape_policy": "first_dip_tape_policy",
        "pressure_probe": "captured_paper_pressure_probe",
        "replay_errors": "replay_errors",
        "runtime": "replay_capture_runtime",
        "stage0": "captured_paper_isolated_stage0",
    }
)
_BENCHMARK_LOADER_ROLE_ORDER = (
    "benchmark",
    "contract",
    "runtime",
    "pressure_probe",
    "replay_errors",
    "first_dip_tape_policy",
    "stage0",
)
_BENCHMARK_AUTHORITY_PROGRAM_PATHS: Mapping[str, str] = MappingProxyType(
    {
        "builder": "scripts/build_captured_paper_benchmark_authority.py",
        "runner": "scripts/run_captured_paper_benchmark_authority.py",
    }
)


def _artifact_reference(
    value: Any,
    artifact: HashBoundJsonArtifact,
    field: str,
) -> Mapping[str, Any]:
    reference = _expect_mapping(value, field)
    _exact_keys(reference, {"path", "sha256"}, field)
    if reference != {"path": str(artifact.path), "sha256": artifact.sha256}:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_BINDING_MISMATCH",
            f"{field} differs from its externally pinned artifact",
        )
    return reference


def _held_runner_loader(path: Path, expected_sha256: str) -> str:
    raw = _read_bytes_stable(
        path, field="benchmark_authority_program.runner", max_bytes=_MAX_SOURCE_BYTES
    )
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_PROGRAM_MISMATCH",
            "benchmark runner source changed before loader extraction",
        )
    try:
        tree = ast.parse(raw, filename=str(path))
    except (SyntaxError, ValueError) as exc:
        raise BootstrapPreflightError(
            "BENCHMARK_RUNNER_AUTHORITY_INVALID",
            "benchmark runner source cannot be parsed",
        ) from exc
    candidates: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id == "_HELD_BENCHMARK_AUTHORITY_RUNNER_LOADER"
            for target in node.targets
        ):
            candidates.append(node.value)
    value = candidates[0] if len(candidates) == 1 else None
    if not (
        isinstance(value, ast.Call)
        and not value.args
        and not value.keywords
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "strip"
        and isinstance(value.func.value, ast.Constant)
        and isinstance(value.func.value.value, str)
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_RUNNER_AUTHORITY_INVALID",
            "held benchmark runner loader is not one literal",
        )
    loader = value.func.value.value.strip()
    if not loader or len(loader.encode("utf-8")) > _MAX_SOURCE_BYTES or "\x00" in loader:
        raise BootstrapPreflightError(
            "BENCHMARK_RUNNER_AUTHORITY_INVALID",
            "held benchmark runner loader is malformed",
        )
    return loader


def _held_benchmark_loader(path: Path, expected_sha256: str) -> str:
    raw = _read_bytes_stable(
        path, field="benchmark_source.pressure_probe", max_bytes=_MAX_SOURCE_BYTES
    )
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_SOURCE_MISMATCH",
            "benchmark pressure-probe source changed before loader extraction",
        )
    try:
        tree = ast.parse(raw, filename=str(path))
    except (SyntaxError, ValueError) as exc:
        raise BootstrapPreflightError(
            "BENCHMARK_LAUNCH_RECEIPT_INVALID",
            "benchmark pressure-probe source cannot be parsed",
        ) from exc
    candidates: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id == "_HELD_BENCHMARK_SOURCE_LOADER"
            for target in node.targets
        ):
            candidates.append(node.value)
    value = candidates[0] if len(candidates) == 1 else None
    if not (
        isinstance(value, ast.Call)
        and not value.args
        and not value.keywords
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "strip"
        and isinstance(value.func.value, ast.Constant)
        and isinstance(value.func.value.value, str)
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_LAUNCH_RECEIPT_INVALID",
            "held benchmark source loader is not one literal",
        )
    loader = value.func.value.value.strip()
    if not loader or len(loader.encode("utf-8")) > _MAX_SOURCE_BYTES or "\x00" in loader:
        raise BootstrapPreflightError(
            "BENCHMARK_LAUNCH_RECEIPT_INVALID",
            "held benchmark source loader is malformed",
        )
    return loader


def _require_authority_layout(
    artifact: HashBoundJsonArtifact,
    *,
    kind: str,
    field: str,
) -> None:
    path = artifact.path
    if not (
        path.name == f"{artifact.sha256}.json"
        and path.parent.name == artifact.sha256[:2]
        and path.parent.parent.name == kind
        and path.parent.parent.parent.name == "authority"
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_ADDRESS_INVALID",
            f"{field} does not have its exact authority content address",
        )


def _load_verified_stage0(path: Path, expected_sha256: str) -> ModuleType:
    raw = _read_bytes_stable(
        path, field="verified captured PAPER stage0", max_bytes=_MAX_SOURCE_BYTES
    )
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise BootstrapPreflightError(
            "SOURCE_CHANGED_BEFORE_IMPORT",
            "captured PAPER stage0 changed after source-roster verification",
        )
    name = "_chili_iqfeed_verified_stage0_" + expected_sha256[:16]
    existing = sys.modules.get(name)
    if existing is not None:
        if getattr(existing, "_verified_source_sha256", None) != expected_sha256:
            raise BootstrapPreflightError(
                "VERIFIED_RUNTIME_CACHE_CONFLICT",
                "verified stage0 cache has different source bytes",
            )
        return existing
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = None
    module._verified_source_sha256 = expected_sha256  # type: ignore[attr-defined]
    sys.modules[name] = module
    try:
        exec(compile(raw, str(path), "exec", dont_inherit=True), module.__dict__)
    except BaseException as exc:
        sys.modules.pop(name, None)
        raise BootstrapPreflightError(
            "VERIFIED_STAGE0_IMPORT_FAILED",
            "verified captured PAPER stage0 could not be loaded",
        ) from exc
    return module


def _stable_executable_hash(
    path_raw: Any,
    expected_raw: Any,
    field: str,
    *,
    roots: Sequence[Path],
    local_drive_check: Callable[[Path], bool],
) -> Path:
    expected = _require_sha256(expected_raw, f"{field}.sha256")
    path = _validated_read_path(
        path_raw,
        field=field,
        roots=roots,
        local_drive_check=local_drive_check,
    )
    raw = _read_bytes_stable(path, field=field, max_bytes=_MAX_EXECUTABLE_BYTES)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_EXECUTABLE_INVALID",
            f"{field} bytes differ from their authority pin",
        )
    return path


def _minimal_git_environment(sandbox: Path) -> Mapping[str, str]:
    allowed = {
        "COMSPEC", "LANG", "LC_ALL", "NUMBER_OF_PROCESSORS", "OS",
        "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE", "SYSTEMROOT",
        "TEMP", "TMP", "WINDIR",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    hooks = sandbox / "empty-hooks"
    hooks.mkdir(mode=0o700, exist_ok=False)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": "NUL" if os.name == "nt" else "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "6",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_KEY_1": "core.hooksPath",
            "GIT_CONFIG_VALUE_1": str(hooks),
            "GIT_CONFIG_KEY_2": "credential.helper",
            "GIT_CONFIG_VALUE_2": "",
            "GIT_CONFIG_KEY_3": "core.untrackedCache",
            "GIT_CONFIG_VALUE_3": "false",
            "GIT_CONFIG_KEY_4": "safe.directory",
            "GIT_CONFIG_VALUE_4": "*",
            "GIT_CONFIG_KEY_5": "protocol.allow",
            "GIT_CONFIG_VALUE_5": "never",
            "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(sandbox),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return MappingProxyType(environment)


def _validate_retained_benchmark_output(
    *,
    output_root: Path,
    output_volume_sha256: str,
    resource_benchmark: HashBoundJsonArtifact,
    read_roots: Sequence[Path],
    local_drive_check: Callable[[Path], bool],
) -> None:
    """Bind the terminal report to its uniquely owned retained output."""

    report = resource_benchmark.document
    output = _expect_mapping(report.get("output"), "resource benchmark output")
    expected_output_keys = {
        "directory",
        "report_artifact_layout",
        "retained",
        "safe_cleanup_verified",
    }
    if set(output) != expected_output_keys:
        raise BootstrapPreflightError(
            "BENCHMARK_REPORT_INVALID",
            "benchmark output projection fields differ",
        )
    if not (
        output.get("retained") is True
        and output.get("safe_cleanup_verified") is False
        and output.get("report_artifact_layout")
        == "reports/<canonical-sha256>.json_when_retained"
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_REPORT_INVALID",
            "benchmark report was not retained exactly",
        )
    directory_raw = output.get("directory")
    directory = _lexical_absolute_local_path(
        directory_raw,
        field="benchmark owned directory",
        local_drive_check=local_drive_check,
    )
    if not (
        isinstance(directory_raw, str)
        and directory_raw == str(directory)
        and _inside_any(directory, read_roots, allow_equal=False)
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_REPORT_PATH_INVALID",
            "owned report directory is not one canonical allowed path",
        )
    _check_existing_components(
        directory,
        require_leaf=True,
        field="benchmark owned directory",
    )
    try:
        directory_status = directory.lstat()
    except OSError as exc:
        raise BootstrapPreflightError(
            "BENCHMARK_REPORT_PATH_INVALID",
            "owned report directory is unavailable",
        ) from exc
    if (
        not stat.S_ISDIR(directory_status.st_mode)
        or directory.parent != output_root
        or not directory.name.startswith("chili-replay-capture-benchmark-")
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_REPORT_PATH_INVALID",
            "owned report directory escaped the benchmark output root",
        )
    suffix = directory.name.removeprefix("chili-replay-capture-benchmark-")
    owner_token, separator, random_tail = suffix.partition("-")
    if (
        not separator
        or re.fullmatch(r"[0-9a-f]{32}", owner_token) is None
        or not random_tail
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_REPORT_PATH_INVALID",
            "owned report directory token is invalid",
        )

    marker_path = _validated_read_path(
        directory / ".chili-replay-capture-benchmark-owner.json",
        field="benchmark ownership marker",
        roots=(output_root,),
        local_drive_check=local_drive_check,
    )
    marker_raw = _read_bytes_stable(
        marker_path,
        field="benchmark ownership marker",
        max_bytes=_MAX_OWNERSHIP_MARKER_BYTES,
    )
    if not marker_raw.endswith(b"\n") or marker_raw.count(b"\n") != 1:
        raise BootstrapPreflightError(
            "BENCHMARK_OWNERSHIP_INVALID",
            "benchmark ownership marker is not one canonical JSON line",
        )
    marker = _strict_json(marker_raw[:-1], "benchmark ownership marker")
    if marker != {
        "benchmark_schema_version": report.get("benchmark_schema_version"),
        "directory": str(directory),
        "output_root": str(output_root),
        "owner_token": owner_token,
    }:
        raise BootstrapPreflightError(
            "BENCHMARK_OWNERSHIP_INVALID",
            "benchmark ownership marker binding differs",
        )

    expected_report_path = (
        directory / "reports" / f"{resource_benchmark.sha256}.json"
    )
    if resource_benchmark.path != expected_report_path:
        raise BootstrapPreflightError(
            "BENCHMARK_REPORT_PATH_INVALID",
            "terminal report is outside its exact retained report path",
        )
    try:
        children = tuple(output_root.iterdir())
    except OSError as exc:
        raise BootstrapPreflightError(
            "BENCHMARK_OUTPUT_INVENTORY_INVALID",
            "benchmark output inventory is unavailable",
        ) from exc
    if children != (directory,):
        raise BootstrapPreflightError(
            "BENCHMARK_OUTPUT_INVENTORY_INVALID",
            "benchmark output root contains objects outside its owned directory",
        )

    resolved_binding = _expect_mapping(
        report.get("resolved_resource_binding"),
        "resource benchmark resolved binding",
    )
    measurement = _expect_mapping(
        resolved_binding.get("measurement"),
        "resource benchmark binding measurement",
    )
    if (
        _require_sha256(
            output_volume_sha256,
            "benchmark authority output volume SHA-256",
        )
        != _require_sha256(
            measurement.get("write_latency_probe_volume_identity_sha256"),
            "benchmark measurement volume identity SHA-256",
        )
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_OUTPUT_VOLUME_MISMATCH",
            "benchmark output and measured pressure-probe volumes differ",
        )


def _validate_benchmark_authority_chain(
    *,
    authority_manifest: HashBoundJsonArtifact,
    runner_authority: HashBoundJsonArtifact,
    launch_receipt: HashBoundJsonArtifact,
    execution_receipt: HashBoundJsonArtifact,
    execution_claim: HashBoundJsonArtifact,
    resource_benchmark: HashBoundJsonArtifact,
    source_paths: Mapping[str, Path],
    source_hashes: Mapping[str, str],
    read_roots: Sequence[Path],
    local_drive_check: Callable[[Path], bool],
) -> None:
    """Require one terminal, externally pinned benchmark authority chain."""

    for artifact, kind, field in (
        (authority_manifest, "manifest", "benchmark authority manifest"),
        (runner_authority, "runner-authority", "benchmark runner authority"),
        (launch_receipt, "receipt", "benchmark launch receipt"),
        (execution_receipt, "execution-receipt", "benchmark execution receipt"),
    ):
        _require_authority_layout(artifact, kind=kind, field=field)
    authority_root = authority_manifest.path.parents[3]
    if not (
        runner_authority.path.parents[3] == authority_root
        and launch_receipt.path.parents[3] == authority_root
        and execution_receipt.path.parents[3] == authority_root
        and execution_claim.path.parents[2] == authority_root
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_ROOT_MISMATCH",
            "benchmark authority artifacts do not share one canonical root",
        )
    manifest = authority_manifest.document
    _exact_keys(
        manifest,
        {
            "account_scope",
            "authority_mode",
            "authority_programs",
            "benchmark_arguments",
            "candidate_root",
            "execution_context",
            "expected_benchmark_schema_version",
            "expected_git_commit",
            "git",
            "held_loader",
            "output",
            "posture",
            "python",
            "python_dependency_root",
            "schema_version",
            "source_roster",
            "source_roster_sha256",
        },
        "benchmark_authority_manifest",
    )
    if manifest.get("schema_version") != BENCHMARK_AUTHORITY_MANIFEST_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_SCHEMA_MISMATCH",
            "benchmark authority manifest schema is unsupported",
        )
    if not source_paths or set(source_paths) != set(source_hashes):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_SOURCE_MISMATCH",
            "benchmark authority source closure is incomplete",
        )
    repo = source_paths["benchmark_replay_capture_runtime"].parent.parent
    if manifest.get("candidate_root") != str(repo):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_BINDING_MISMATCH",
            "benchmark authority candidate root differs from the capture sources",
        )
    if not (
        manifest.get("account_scope") == "alpaca:paper"
        and manifest.get("authority_mode")
        == "diagnostic_capture_benchmark_only"
        and manifest.get("expected_benchmark_schema_version")
        == BENCHMARK_SCHEMA_VERSION
        and isinstance(manifest.get("expected_git_commit"), str)
        and re.fullmatch(r"[0-9a-f]{40}", manifest["expected_git_commit"])
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_POSTURE_INVALID",
            "benchmark authority is not diagnostic Alpaca PAPER authority",
        )
    expected_manifest_posture = {
        "benchmark_execution_authorized": True,
        "broker_contact_authorized": False,
        "database_access_authorized": False,
        "host_activation_authorized": False,
        "live_cash_authorized": False,
        "order_submission_authorized": False,
        "provider_contact_authorized": False,
    }
    if manifest.get("posture") != expected_manifest_posture:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_POSTURE_INVALID",
            "benchmark authority posture is not inert outside the benchmark",
        )

    expected_rows = [
        {
            "path": str(source_paths[source_role]),
            "role": authority_role,
            "sha256": source_hashes[source_role],
        }
        for authority_role, source_role in sorted(
            _BENCHMARK_AUTHORITY_SOURCE_ROLES.items()
        )
    ]
    if (
        manifest.get("source_roster") != expected_rows
        or manifest.get("source_roster_sha256") != _sha256_json(expected_rows)
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_SOURCE_MISMATCH",
            "benchmark authority source roster differs from current capture bytes",
        )

    programs = _expect_mapping(
        manifest.get("authority_programs"),
        "benchmark_authority_manifest.authority_programs",
    )
    _exact_keys(programs, {"builder", "runner"}, "benchmark authority programs")
    expected_program_hashes = {
        "builder": BENCHMARK_AUTHORITY_BUILDER_SHA256,
        "runner": BENCHMARK_AUTHORITY_RUNNER_SHA256,
    }
    program_paths: dict[str, Path] = {}
    for role in ("builder", "runner"):
        row = _expect_mapping(programs.get(role), f"benchmark authority {role}")
        _exact_keys(row, {"path", "sha256"}, f"benchmark authority {role}")
        expected_path = repo / _BENCHMARK_AUTHORITY_PROGRAM_PATHS[role]
        path, digest = _read_hash_bound_source(
            row.get("path"),
            row.get("sha256"),
            field=f"benchmark_authority_program.{role}",
            roots=read_roots,
            local_drive_check=local_drive_check,
        )
        if (
            path != expected_path
            or digest != expected_program_hashes[role]
            or dict(row) != {"path": str(expected_path), "sha256": digest}
        ):
            raise BootstrapPreflightError(
                "BENCHMARK_AUTHORITY_PROGRAM_MISMATCH",
                f"benchmark authority {role} differs from frozen producer bytes",
            )
        program_paths[role] = path

    python = _expect_mapping(manifest.get("python"), "benchmark authority python")
    _exact_keys(
        python,
        {
            "executable_path",
            "executable_sha256",
            "implementation",
            "isolation_flags",
            "version",
        },
        "benchmark authority python",
    )
    if not (
        python.get("implementation") == "cpython"
        and python.get("version") == [3, 11]
        and python.get("isolation_flags") == ["-I", "-S", "-B"]
        and Path(str(python.get("executable_path") or "")).is_absolute()
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_PYTHON_INVALID",
            "benchmark authority Python identity is unsupported",
        )
    python_sha256 = _require_sha256(
        python.get("executable_sha256"), "benchmark authority Python SHA-256"
    )
    python_path = _stable_executable_hash(
        python.get("executable_path"), python_sha256, "benchmark authority Python",
        roots=read_roots,
        local_drive_check=local_drive_check,
    )
    if (
        python_path.resolve(strict=True) != _PROCESS_PYTHON_EXECUTABLE
        or sys.implementation.name != "cpython"
        or sys.version_info[:2] != (3, 11)
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_PYTHON_INVALID",
            "benchmark authority must bind the running CPython 3.11 executable",
        )
    dependency = _expect_mapping(
        manifest.get("python_dependency_root"),
        "benchmark authority dependency root",
    )
    _exact_keys(
        dependency,
        {
            "identity",
            "identity_sha256",
            "path",
            "required_distributions",
            "tree_sha256",
        },
        "benchmark authority dependency root",
    )
    dependency_identity = _expect_mapping(
        dependency.get("identity"), "dependency root identity"
    )
    if dependency.get("identity_sha256") != _sha256_json(dependency_identity):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_DEPENDENCY_INVALID",
            "benchmark dependency-root identity digest differs",
        )
    dependency_path = _lexical_absolute_local_path(
        dependency.get("path"),
        field="benchmark authority dependency root",
        local_drive_check=local_drive_check,
    )
    if not _inside_any(dependency_path, read_roots, allow_equal=True):
        raise BootstrapPreflightError(
            "PATH_OUTSIDE_ALLOWLIST",
            "benchmark authority dependency root is outside every read root",
        )
    _check_existing_components(
        dependency_path,
        require_leaf=True,
        field="benchmark authority dependency root",
    )
    stage0 = _load_verified_stage0(
        source_paths["captured_paper_isolated_stage0"],
        source_hashes["captured_paper_isolated_stage0"],
    )
    try:
        dependency_tree = stage0._dependency_tree_inventory(dependency_path)
        observed_dependency_identity = stage0._dependency_root_identity_from_inventory(
            root=dependency_path,
            executable=python_path,
            python_executable_sha256=python_sha256,
            tree=dependency_tree,
        )
    except BaseException as exc:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_DEPENDENCY_INVALID",
            "sealed dependency-root identity could not be recomputed",
        ) from exc
    dependency_tree_sha256 = _require_sha256(
        dependency.get("tree_sha256"), "dependency tree SHA-256"
    )
    if not (
        dict(observed_dependency_identity) == dict(dependency_identity)
        and dependency.get("identity_sha256")
        == _sha256_json(dict(observed_dependency_identity))
        and dependency_tree_sha256 == dependency_tree["tree_sha256"]
        and dependency.get("path") == str(dependency_path)
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_DEPENDENCY_INVALID",
            "sealed dependency-root bytes differ from the authority identity",
        )
    required_distributions = dependency.get("required_distributions")
    if not isinstance(required_distributions, list) or len(required_distributions) != 2:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_DEPENDENCY_INVALID",
            "benchmark dependency distribution roster is malformed",
        )
    observed_distributions: list[dict[str, str]] = []
    for index, name in enumerate(("psutil", "zstandard")):
        row = _expect_mapping(
            required_distributions[index],
            f"benchmark dependency distribution {name}",
        )
        _exact_keys(
            row,
            {"import_root", "metadata_path", "metadata_sha256", "name", "version"},
            f"benchmark dependency distribution {name}",
        )
        relative_raw = row.get("metadata_path")
        import_root_raw = row.get("import_root")
        if not (
            row.get("name") == name
            and import_root_raw == name
            and isinstance(relative_raw, str)
            and relative_raw
            and "\\" not in relative_raw
            and not Path(relative_raw).is_absolute()
            and ".." not in Path(relative_raw).parts
            and Path(relative_raw).name == "METADATA"
            and len(relative_raw.encode("utf-8")) <= 4096
        ):
            raise BootstrapPreflightError(
                "BENCHMARK_AUTHORITY_DEPENDENCY_INVALID",
                f"sealed dependency {name} distribution row is malformed",
            )
        relative = Path(relative_raw).as_posix()
        metadata_path = dependency_path / Path(relative_raw)
        if not _inside_any(metadata_path, (dependency_path,), allow_equal=False):
            raise BootstrapPreflightError(
                "BENCHMARK_AUTHORITY_DEPENDENCY_INVALID",
                f"sealed dependency {name} metadata escaped its root",
            )
        inventory_row = dependency_tree["files"].get(relative)
        metadata_raw = _read_bytes_stable(
            metadata_path,
            field=f"benchmark dependency {name} metadata",
            max_bytes=1024 * 1024,
        )
        try:
            metadata_text = metadata_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BootstrapPreflightError(
                "BENCHMARK_AUTHORITY_DEPENDENCY_INVALID",
                f"sealed dependency {name} metadata is not UTF-8",
            ) from exc
        names = [
            line[6:].strip()
            for line in metadata_text.splitlines()
            if line.startswith("Name: ")
        ]
        versions = [
            line[9:].strip()
            for line in metadata_text.splitlines()
            if line.startswith("Version: ")
        ]
        import_root = dependency_path / str(import_root_raw)
        metadata_sha = _require_sha256(
            row.get("metadata_sha256"),
            f"benchmark dependency {name} metadata SHA-256",
        )
        if not (
            isinstance(inventory_row, Mapping)
            and inventory_row.get("sha256")
            == hashlib.sha256(metadata_raw).hexdigest()
            and metadata_sha == inventory_row.get("sha256")
            and names == [name]
            and len(versions) == 1
            and re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+!-]*", versions[0])
            and row.get("version") == versions[0]
            and import_root.is_dir()
        ):
            raise BootstrapPreflightError(
                "BENCHMARK_AUTHORITY_DEPENDENCY_INVALID",
                f"sealed dependency {name} distribution differs",
            )
        observed_distributions.append(
            {
                "import_root": name,
                "metadata_path": relative,
                "metadata_sha256": inventory_row["sha256"],
                "name": name,
                "version": versions[0],
            }
        )
    if required_distributions != observed_distributions:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_DEPENDENCY_INVALID",
            "sealed dependency distribution roster differs",
        )
    git = _expect_mapping(manifest.get("git"), "benchmark authority git")
    _exact_keys(git, {"executable_path", "executable_sha256"}, "benchmark authority git")
    git_path = _stable_executable_hash(
        git.get("executable_path"),
        git.get("executable_sha256"),
        "benchmark authority Git",
        roots=read_roots,
        local_drive_check=local_drive_check,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="chili-iqfeed-git-") as raw:
            commit_result = subprocess.run(
                [str(git_path), "-C", str(repo), "rev-parse", "--verify", "HEAD"],
                check=False,
                env=dict(_minimal_git_environment(Path(raw))),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                shell=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_GIT_INVALID",
            "benchmark Git commit could not be independently resolved",
        ) from exc
    if not (
        commit_result.returncode == 0
        and commit_result.stderr == b""
        and commit_result.stdout.strip()
        == str(manifest.get("expected_git_commit") or "").encode("ascii")
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_GIT_INVALID",
            "benchmark candidate commit differs from Git HEAD",
        )
    context = _expect_mapping(
        manifest.get("execution_context"), "benchmark authority execution context"
    )
    _exact_keys(
        context,
        {"cwd", "environment", "environment_sha256", "shell", "stderr", "stdin", "stdout", "timeout_seconds"},
        "benchmark authority execution context",
    )
    if not (
        context.get("cwd") == str(repo)
        and context.get("shell") is False
        and context.get("stdin") == "devnull"
        and context.get("stderr") == "bounded_binary_pipe_1mib"
        and context.get("stdout") == "bounded_binary_pipe_64mib"
        and context.get("environment_sha256")
        == _sha256_json(_expect_mapping(context.get("environment"), "benchmark environment"))
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_CONTEXT_INVALID",
            "benchmark authority execution context differs",
        )
    output = _expect_mapping(manifest.get("output"), "benchmark authority output")
    _exact_keys(
        output,
        {"root", "root_identity", "storage_volume_identity", "storage_volume_identity_sha256"},
        "benchmark authority output",
    )
    output_volume = _expect_mapping(
        output.get("storage_volume_identity"), "benchmark output volume identity"
    )
    output_root = _lexical_absolute_local_path(
        output.get("root"),
        field="benchmark output root",
        local_drive_check=local_drive_check,
    )
    if not _inside_any(output_root, read_roots, allow_equal=True):
        raise BootstrapPreflightError(
            "PATH_OUTSIDE_ALLOWLIST",
            "benchmark output root is outside every read root",
        )
    try:
        _check_existing_components(
            output_root, require_leaf=True, field="benchmark output root"
        )
        output_status = os.stat(output_root, follow_symlinks=False)
    except OSError as exc:
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_OUTPUT_INVALID",
            "benchmark output root identity is unavailable",
        ) from exc
    expected_output_identity = _expect_mapping(
        output.get("root_identity"), "benchmark output root identity"
    )
    observed_output_volume = {
        "normalized_anchor": os.path.normcase(
            os.path.normpath(output_root.anchor or os.sep)
        ),
        "schema_version": "chili.capture-storage-volume-identity.v1",
        "st_dev": int(output_status.st_dev),
    }
    if not (
        output_root.is_absolute()
        and stat.S_ISDIR(output_status.st_mode)
        and {
            key: expected_output_identity.get(key)
            for key in ("st_dev", "st_ino", "st_mode")
        }
        == {
            "st_dev": int(output_status.st_dev),
            "st_ino": int(output_status.st_ino),
            "st_mode": int(output_status.st_mode),
        }
        and dict(output_volume) == observed_output_volume
        and output.get("storage_volume_identity_sha256")
        == _sha256_json(observed_output_volume)
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_AUTHORITY_OUTPUT_INVALID",
            "benchmark output volume identity differs",
        )

    runner = runner_authority.document
    _exact_keys(
        runner,
        {
            "account_scope",
            "authority_mode",
            "argv_is_shell_string",
            "execution_context",
            "git",
            "launch_receipt",
            "manifest",
            "posture",
            "python",
            "runner_argv",
            "runner_loader_sha256",
            "schema_version",
        },
        "benchmark_runner_authority",
    )
    if runner.get("schema_version") != BENCHMARK_RUNNER_AUTHORITY_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "BENCHMARK_RUNNER_AUTHORITY_SCHEMA_MISMATCH",
            "benchmark runner authority schema is unsupported",
        )
    _artifact_reference(
        runner.get("manifest"), authority_manifest, "benchmark runner manifest"
    )
    launch_ref = _artifact_reference(
        runner.get("launch_receipt"),
        launch_receipt,
        "benchmark runner launch receipt",
    )
    expected_prelaunch_posture = {
        "benchmark_output_written": False,
        "broker_contacted": False,
        "cutover_performed": False,
        "database_accessed": False,
        "host_activation_performed": False,
        "live_cash_authorized": False,
        "orders_submitted": False,
        "provider_contacted": False,
        "task_scheduler_mutated": False,
    }
    if not (
        runner.get("account_scope") == manifest.get("account_scope")
        and runner.get("authority_mode") == manifest.get("authority_mode")
        and runner.get("argv_is_shell_string") is False
        and runner.get("execution_context") == context
        and runner.get("git") == git
        and runner.get("posture") == expected_prelaunch_posture
        and runner.get("python")
        == {
            "executable_path": python["executable_path"],
            "executable_sha256": python["executable_sha256"],
        }
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_RUNNER_AUTHORITY_INVALID",
            "benchmark runner authority bindings differ from the manifest",
        )

    launch = launch_receipt.document
    _exact_keys(
        launch,
        {
            "account_scope", "authority_programs", "argv_is_shell_string",
            "authority_mode", "benchmark_argv", "benchmark_completed",
            "benchmark_report", "candidate_root", "execution_context",
            "expected_git_commit", "git", "held_loader_sha256", "invoked",
            "manifest", "output", "posture", "python",
            "python_dependency_root", "schema_version", "source_roster",
            "source_roster_sha256",
        },
        "benchmark_launch_receipt",
    )
    if launch.get("schema_version") != BENCHMARK_LAUNCH_RECEIPT_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "BENCHMARK_LAUNCH_RECEIPT_SCHEMA_MISMATCH",
            "benchmark launch receipt schema is unsupported",
        )
    _artifact_reference(
        launch.get("manifest"), authority_manifest, "benchmark launch manifest"
    )
    launch_output = _expect_mapping(
        launch.get("output"), "benchmark launch output"
    )
    _exact_keys(
        launch_output,
        {"root", "root_identity", "storage_volume_identity_sha256"},
        "benchmark launch output",
    )
    launch_python = _expect_mapping(
        launch.get("python"), "benchmark launch Python"
    )
    _exact_keys(
        launch_python,
        {"executable_path", "executable_sha256"},
        "benchmark launch Python",
    )
    launch_dependency = _expect_mapping(
        launch.get("python_dependency_root"),
        "benchmark launch dependency root",
    )
    _exact_keys(
        launch_dependency,
        {"identity_sha256", "path", "tree_sha256"},
        "benchmark launch dependency root",
    )
    if not (
        launch.get("account_scope") == manifest.get("account_scope")
        and launch.get("authority_mode") == manifest.get("authority_mode")
        and launch.get("authority_programs") == programs
        and launch.get("argv_is_shell_string") is False
        and launch.get("benchmark_completed") is False
        and launch.get("benchmark_report") is None
        and launch.get("candidate_root") == manifest.get("candidate_root")
        and launch.get("execution_context") == context
        and launch.get("expected_git_commit") == manifest.get("expected_git_commit")
        and launch.get("git") == git
        and launch.get("invoked") is False
        and launch.get("posture") == expected_prelaunch_posture
        and launch_python
        == {
            "executable_path": python["executable_path"],
            "executable_sha256": python["executable_sha256"],
        }
        and launch_dependency
        == {
            "identity_sha256": dependency["identity_sha256"],
            "path": dependency["path"],
            "tree_sha256": dependency["tree_sha256"],
        }
        and launch_output
        == {
            "root": output["root"],
            "root_identity": output["root_identity"],
            "storage_volume_identity_sha256": output[
                "storage_volume_identity_sha256"
            ],
        }
        and launch.get("source_roster") == expected_rows
        and launch.get("source_roster_sha256")
        == manifest.get("source_roster_sha256")
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_LAUNCH_RECEIPT_INVALID",
            "benchmark launch receipt differs from the authority manifest",
        )
    benchmark_arguments = manifest.get("benchmark_arguments")
    if not isinstance(benchmark_arguments, list) or not benchmark_arguments or any(
        type(value) is not str for value in benchmark_arguments
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_LAUNCH_RECEIPT_INVALID",
            "benchmark authority arguments are malformed",
        )
    benchmark_loader = _held_benchmark_loader(
        source_paths["captured_paper_pressure_probe"],
        source_hashes["captured_paper_pressure_probe"],
    )
    benchmark_loader_sha = hashlib.sha256(
        benchmark_loader.encode("utf-8")
    ).hexdigest()
    if (
        manifest.get("held_loader")
        != {
            "sha256": benchmark_loader_sha,
            "source_role": "pressure_probe",
            "variable": "_HELD_BENCHMARK_SOURCE_LOADER",
        }
        or launch.get("held_loader_sha256") != benchmark_loader_sha
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_LAUNCH_RECEIPT_INVALID",
            "benchmark held-source loader differs from the pressure-probe source",
        )
    authority_rows = {row["role"]: row for row in expected_rows}
    benchmark_bootstrap: list[str] = []
    for role in _BENCHMARK_LOADER_ROLE_ORDER:
        benchmark_bootstrap.extend(
            (authority_rows[role]["path"], authority_rows[role]["sha256"])
        )
    benchmark_bootstrap.extend(
        (
            dependency["path"],
            dependency["identity_sha256"],
            str(authority_manifest.path),
            authority_manifest.sha256,
            python["executable_sha256"],
        )
    )
    expected_benchmark_argv = [
        python["executable_path"], "-I", "-S", "-B", "-c",
        benchmark_loader, *benchmark_bootstrap, "--", *benchmark_arguments,
    ]
    if launch.get("benchmark_argv") != expected_benchmark_argv:
        raise BootstrapPreflightError(
            "BENCHMARK_LAUNCH_RECEIPT_INVALID",
            "benchmark launch argv is not derived from the sealed source closure",
        )
    argv = runner.get("runner_argv")
    if not isinstance(argv, list) or len(argv) != 16 or not all(
        isinstance(value, str) for value in argv
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_RUNNER_AUTHORITY_INVALID",
            "benchmark runner argv is malformed",
        )
    loader = _held_runner_loader(
        program_paths["runner"], expected_program_hashes["runner"]
    )
    expected_argv = [
        python["executable_path"], "-I", "-S", "-B", "-c", loader,
        programs["builder"]["path"], programs["builder"]["sha256"],
        programs["runner"]["path"], programs["runner"]["sha256"],
        python["executable_sha256"], "--", "--receipt", launch_ref["path"],
        "--receipt-sha256", launch_ref["sha256"],
    ]
    if (
        argv != expected_argv
        or runner.get("runner_loader_sha256")
        != hashlib.sha256(loader.encode("utf-8")).hexdigest()
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_RUNNER_AUTHORITY_INVALID",
            "benchmark runner argv or held-loader digest differs",
        )

    execution = execution_receipt.document
    _exact_keys(
        execution,
        {
            "account_scope", "authority_programs", "argv_is_shell_string",
            "authority_mode", "benchmark", "benchmark_completed",
            "completed_at_utc", "duration_seconds", "execution_claim",
            "execution_context", "expected_git_commit", "git", "invoked",
            "launch_receipt", "manifest", "posture", "schema_version",
            "started_at_utc",
        },
        "benchmark_execution_receipt",
    )
    if execution.get("schema_version") != BENCHMARK_EXECUTION_RECEIPT_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "BENCHMARK_EXECUTION_RECEIPT_SCHEMA_MISMATCH",
            "a terminal benchmark execution receipt v2 is required",
        )
    _artifact_reference(
        execution.get("manifest"), authority_manifest, "benchmark execution manifest"
    )
    _artifact_reference(
        execution.get("launch_receipt"),
        launch_receipt,
        "benchmark execution launch receipt",
    )
    _artifact_reference(
        execution.get("execution_claim"),
        execution_claim,
        "benchmark execution claim",
    )
    claim = execution_claim.document
    _exact_keys(
        claim,
        {"launch_receipt", "manifest", "schema_version", "started_at_utc"},
        "benchmark_execution_claim",
    )
    if execution_claim.path.name != f"{launch_receipt.sha256}.json":
        raise BootstrapPreflightError(
            "BENCHMARK_EXECUTION_CLAIM_ADDRESS_INVALID",
            "benchmark execution claim filename does not bind the launch digest",
        )
    if execution_claim.path.parent.name != "execution-claim" or (
        execution_claim.path.parent.parent.name != "authority"
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_EXECUTION_CLAIM_ADDRESS_INVALID",
            "benchmark execution claim is outside its exact authority directory",
        )
    _artifact_reference(
        claim.get("launch_receipt"), launch_receipt, "execution claim launch receipt"
    )
    _artifact_reference(
        claim.get("manifest"), authority_manifest, "execution claim manifest"
    )
    if claim.get("schema_version") != BENCHMARK_EXECUTION_CLAIM_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "BENCHMARK_EXECUTION_CLAIM_SCHEMA_MISMATCH",
            "benchmark execution claim schema is unsupported",
        )
    benchmark = _expect_mapping(execution.get("benchmark"), "benchmark execution result")
    _exact_keys(
        benchmark,
        {"acceptance", "exit_code", "report", "schema_version", "stderr_bytes", "stdout_sha256", "stdout_without_newline_sha256"},
        "benchmark execution result",
    )
    _artifact_reference(
        benchmark.get("report"), resource_benchmark, "benchmark execution report"
    )
    report = resource_benchmark.document
    report_raw = _canonical_json_bytes(report)
    expected_terminal_posture = {
        **expected_prelaunch_posture,
        "benchmark_output_written": True,
    }
    if not (
        execution.get("account_scope") == manifest.get("account_scope")
        and execution.get("authority_mode") == manifest.get("authority_mode")
        and execution.get("authority_programs") == programs
        and execution.get("argv_is_shell_string") is False
        and execution.get("execution_context") == context
        and execution.get("expected_git_commit") == manifest.get("expected_git_commit")
        and execution.get("git") == git
        and execution.get("invoked") is True
        and execution.get("benchmark_completed") is True
        and execution.get("posture") == expected_terminal_posture
        and benchmark.get("acceptance") == {"accepted": True, "reasons": []}
        and benchmark.get("exit_code") == 0
        and benchmark.get("stderr_bytes") == 0
        and benchmark.get("schema_version") == BENCHMARK_SCHEMA_VERSION
        and benchmark.get("stdout_without_newline_sha256") == resource_benchmark.sha256
        and benchmark.get("stdout_sha256")
        == hashlib.sha256(report_raw + b"\n").hexdigest()
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_EXECUTION_NOT_TERMINAL",
            "benchmark execution receipt is not a successful inert terminal run",
        )
    started = _parse_utc(execution.get("started_at_utc"), "benchmark execution start")
    claim_started = _parse_utc(
        claim.get("started_at_utc"), "benchmark execution claim start"
    )
    completed = _parse_utc(execution.get("completed_at_utc"), "benchmark execution completion")
    duration = execution.get("duration_seconds")
    if (
        claim_started != started
        or completed < started
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) < 0.0
        or not math.isclose(
            float(duration), (completed - started).total_seconds(), rel_tol=0.0, abs_tol=1e-9
        )
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_EXECUTION_CLOCK_INVALID",
            "benchmark terminal receipt clocks differ",
        )
    environment = _expect_mapping(report.get("environment"), "resource benchmark environment")
    if not (
        report.get("benchmark_schema_version") == BENCHMARK_SCHEMA_VERSION
        and report.get("acceptance") == {"accepted": True, "reasons": []}
        and environment.get("benchmark_authority_manifest_sha256")
        == authority_manifest.sha256
        and environment.get("dependency_root_identity_sha256")
        == dependency.get("identity_sha256")
        and environment.get("python_executable_sha256")
        == python.get("executable_sha256")
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_REPORT_AUTHORITY_MISMATCH",
            "benchmark report does not bind the terminal authority environment",
        )
    _validate_retained_benchmark_output(
        output_root=output_root,
        output_volume_sha256=str(output.get("storage_volume_identity_sha256") or ""),
        resource_benchmark=resource_benchmark,
        read_roots=read_roots,
        local_drive_check=local_drive_check,
    )


def _host_fingerprint() -> str:
    material = {
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "machine": platform.machine(),
        "node": platform.node(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "total_memory_bytes": int(psutil.virtual_memory().total),
    }
    return hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


def _validate_freshness(
    observed_at: datetime,
    *,
    now: datetime,
    max_age_seconds: float,
    max_future_skew_seconds: float,
    field: str,
) -> None:
    age = (now - observed_at).total_seconds()
    if age < -max_future_skew_seconds:
        raise BootstrapPreflightError(
            "FUTURE_EVIDENCE", f"{field} is beyond the permitted future skew"
        )
    if age > max_age_seconds:
        raise BootstrapPreflightError("STALE_EVIDENCE", f"{field} is stale")


def _load_verified_capture_modules(
    contract_path: Path,
    runtime_path: Path,
    *,
    contract_sha256: str,
    runtime_sha256: str,
) -> tuple[ModuleType, ModuleType]:
    if contract_path.parent != runtime_path.parent:
        raise BootstrapPreflightError(
            "SOURCE_LAYOUT_MISMATCH", "capture contract/runtime are not package peers"
        )
    if contract_path.name != "replay_capture_contract.py" or runtime_path.name != "replay_capture_runtime.py":
        raise BootstrapPreflightError(
            "SOURCE_LAYOUT_MISMATCH", "capture contract/runtime filenames are unexpected"
        )
    package_name = (
        "_chili_iqfeed_bootstrap_verified_"
        + hashlib.sha256(
            f"{contract_sha256}:{runtime_sha256}".encode("ascii")
        ).hexdigest()[:16]
    )
    existing_contract = sys.modules.get(f"{package_name}.replay_capture_contract")
    existing_runtime = sys.modules.get(f"{package_name}.replay_capture_runtime")
    if existing_contract is not None and existing_runtime is not None:
        if (
            getattr(existing_contract, "_verified_source_sha256", None)
            != contract_sha256
            or getattr(existing_runtime, "_verified_source_sha256", None)
            != runtime_sha256
        ):
            raise BootstrapPreflightError(
                "VERIFIED_RUNTIME_CACHE_CONFLICT",
                "verified capture module cache has different source bytes",
            )
        return existing_contract, existing_runtime
    contract_raw = _read_bytes_stable(
        contract_path,
        field="verified replay capture contract",
        max_bytes=_MAX_SOURCE_BYTES,
    )
    runtime_raw = _read_bytes_stable(
        runtime_path,
        field="verified replay capture runtime",
        max_bytes=_MAX_SOURCE_BYTES,
    )
    if hashlib.sha256(contract_raw).hexdigest() != contract_sha256:
        raise BootstrapPreflightError(
            "SOURCE_CHANGED_BEFORE_IMPORT",
            "capture contract changed after source-roster verification",
        )
    if hashlib.sha256(runtime_raw).hexdigest() != runtime_sha256:
        raise BootstrapPreflightError(
            "SOURCE_CHANGED_BEFORE_IMPORT",
            "capture runtime changed after source-roster verification",
        )
    package = ModuleType(package_name)
    package.__path__ = [str(contract_path.parent)]  # type: ignore[attr-defined]
    package.__package__ = package_name
    sys.modules[package_name] = package
    contract_name = f"{package_name}.replay_capture_contract"
    runtime_name = f"{package_name}.replay_capture_runtime"
    contract = ModuleType(contract_name)
    contract.__file__ = str(contract_path)
    contract.__package__ = package_name
    contract._verified_source_sha256 = contract_sha256  # type: ignore[attr-defined]
    runtime = ModuleType(runtime_name)
    runtime.__file__ = str(runtime_path)
    runtime.__package__ = package_name
    runtime._verified_source_sha256 = runtime_sha256  # type: ignore[attr-defined]
    sys.modules[contract_name] = contract
    sys.modules[runtime_name] = runtime
    try:
        exec(compile(contract_raw, str(contract_path), "exec"), contract.__dict__)
        exec(compile(runtime_raw, str(runtime_path), "exec"), runtime.__dict__)
    except BaseException as exc:
        sys.modules.pop(runtime_name, None)
        sys.modules.pop(contract_name, None)
        sys.modules.pop(package_name, None)
        raise BootstrapPreflightError(
            "VERIFIED_RUNTIME_IMPORT_FAILED",
            "verified capture contract/runtime could not be loaded",
        ) from exc
    return contract, runtime


def _json_normalized(contract: ModuleType, value: Any) -> Any:
    return json.loads(contract.canonical_json_bytes(value))


def _validate_resource_report(
    report: Mapping[str, Any],
    *,
    source_paths: Mapping[str, Path],
    source_hashes: Mapping[str, str],
    expected_binding_sha256: str,
    now: datetime,
    benchmark_max_age_seconds: float,
    max_future_skew_seconds: float,
    host_fingerprint_provider: Callable[[], str],
) -> tuple[Any, tuple[str, ...], ModuleType]:
    required_top = {
        "acceptance",
        "artifact_freshness",
        "authority",
        "benchmark_schema_version",
        "capture_identity",
        "capture_runtime_source",
        "enqueue",
        "environment",
        "generated_at",
        "measurement_window",
        "output",
        "parameters",
        "process",
        "resolved_resource_binding",
        "resource_measurement",
        "shared_store_validation",
        "storage",
        "workload_base_utc",
        "writer",
    }
    _exact_keys(report, required_top, "resource_benchmark")
    if report.get("benchmark_schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "BENCHMARK_SCHEMA_MISMATCH", "resource benchmark schema is unsupported"
        )
    generated_at = _parse_utc(report.get("generated_at"), "resource_benchmark.generated_at")
    _validate_freshness(
        generated_at,
        now=now,
        max_age_seconds=benchmark_max_age_seconds,
        max_future_skew_seconds=max_future_skew_seconds,
        field="resource benchmark",
    )

    acceptance = _expect_mapping(report.get("acceptance"), "resource_benchmark.acceptance")
    if acceptance.get("accepted") is not True or acceptance.get("reasons") != []:
        raise BootstrapPreflightError(
            "BENCHMARK_REJECTED", "resource benchmark did not pass its own acceptance"
        )
    freshness = _expect_mapping(
        report.get("artifact_freshness"), "resource_benchmark.artifact_freshness"
    )
    if freshness.get("fresh_at_emit") is not True:
        raise BootstrapPreflightError(
            "BENCHMARK_STALE_AT_EMIT", "resource benchmark was stale when emitted"
        )
    authority = _expect_mapping(report.get("authority"), "resource_benchmark.authority")
    _exact_keys(
        authority,
        {
            "capacity_authority",
            "empirical_calibration_receipt_sha256",
            "hot_symbol_limit_authorized",
            "reasons",
            "watcher_limit_authorized",
            "writer_limit_authorized",
        },
        "resource_benchmark.authority",
    )
    if authority.get("capacity_authority") != CAPTURE_MODE:
        raise BootstrapPreflightError(
            "CAPACITY_AUTHORITY_MISMATCH",
            "this preflight accepts diagnostic capture capacity only",
        )
    reasons_raw = authority.get("reasons")
    if (
        not isinstance(reasons_raw, list)
        or not reasons_raw
        or any(not isinstance(row, str) or not row for row in reasons_raw)
        or authority.get("empirical_calibration_receipt_sha256") is not None
        or authority.get("hot_symbol_limit_authorized") is not False
        or authority.get("watcher_limit_authorized") is not False
        or authority.get("writer_limit_authorized") is not False
    ):
        raise BootstrapPreflightError(
            "CAPACITY_AUTHORITY_INVALID",
            "diagnostic resource authority fields are inconsistent",
        )

    source = _expect_mapping(
        report.get("capture_runtime_source"), "resource_benchmark.capture_runtime_source"
    )
    _exact_keys(
        source,
        {
            "benchmark_script_sha256",
            "contract_sha256",
            "first_dip_tape_policy_sha256",
            "pressure_probe_sha256",
            "replay_errors_sha256",
            "runtime_sha256",
            "stage0_sha256",
        },
        "resource_benchmark.capture_runtime_source",
    )
    expected_source = {
        "benchmark_script_sha256": source_hashes[
            "benchmark_replay_capture_runtime"
        ],
        "contract_sha256": source_hashes["replay_capture_contract"],
        "first_dip_tape_policy_sha256": source_hashes[
            "first_dip_tape_policy"
        ],
        "pressure_probe_sha256": source_hashes[
            "captured_paper_pressure_probe"
        ],
        "replay_errors_sha256": source_hashes["replay_errors"],
        "runtime_sha256": source_hashes["replay_capture_runtime"],
        "stage0_sha256": source_hashes["captured_paper_isolated_stage0"],
    }
    if dict(source) != expected_source:
        raise BootstrapPreflightError(
            "BENCHMARK_SOURCE_MISMATCH",
            "resource benchmark was produced by different capture sources",
        )

    environment = _expect_mapping(report.get("environment"), "resource_benchmark.environment")
    _exact_keys(
        environment,
        {
            "current_host_fingerprint_sha256",
            "benchmark_authority_manifest_sha256",
            "dependency_root_identity_sha256",
            "host_fingerprint_matches",
            "logical_cpu_count",
            "measurement_host_fingerprint_sha256",
            "platform",
            "python",
            "python_executable_sha256",
            "resource_sampler_profile",
        },
        "resource_benchmark.environment",
    )
    _require_sha256(
        environment.get("benchmark_authority_manifest_sha256"),
        "resource_benchmark.environment.benchmark_authority_manifest_sha256",
    )
    _require_sha256(
        environment.get("python_executable_sha256"),
        "resource_benchmark.environment.python_executable_sha256",
    )
    _require_sha256(
        environment.get("dependency_root_identity_sha256"),
        "resource_benchmark.environment.dependency_root_identity_sha256",
    )
    if environment.get("resource_sampler_profile") != (
        "chili.benchmark-stdlib-resource-sampler.v1"
    ) or environment.get("python") != platform.python_version():
        raise BootstrapPreflightError(
            "BENCHMARK_RESOURCE_SAMPLER_INVALID",
            "resource benchmark sampler profile is unsupported",
        )
    measurement = _expect_mapping(
        report.get("resource_measurement"), "resource_benchmark.resource_measurement"
    )
    durable = _expect_mapping(
        measurement.get("durable_publication"),
        "resource_benchmark.resource_measurement.durable_publication",
    )
    live_probe = _expect_mapping(
        durable.get("live_pressure_probe"),
        "resource_benchmark.resource_measurement.durable_publication.live_pressure_probe",
    )
    _exact_keys(
        durable,
        {
            "all_verified",
            "file_fsync",
            "live_pressure_probe",
            "parent_publication",
            "sample_count",
            "verified_count",
        },
        "resource_benchmark.resource_measurement.durable_publication",
    )
    _exact_keys(
        live_probe,
        {
            "all_verified",
            "bytes_per_sample",
            "count",
            "helper_sha256",
            "max_ns",
            "mean_ns",
            "min_ns",
            "p50_ns",
            "p95_ns",
            "p99_ns",
            "probe_root_identity_sha256",
            "probe_volume_identity_sha256",
            "write_latency_profile",
        },
        "resource_benchmark.resource_measurement.durable_publication.live_pressure_probe",
    )
    try:
        sample_count = durable.get("sample_count")
        verified_count = durable.get("verified_count")
        latency_values = tuple(
            live_probe.get(name)
            for name in (
                "min_ns", "p50_ns", "mean_ns", "p95_ns", "p99_ns", "max_ns"
            )
        )
        live_probe_p95_ms = float(live_probe.get("p95_ns")) / 1_000_000.0
        measured_fsync_p95_ms = float(measurement.get("fsync_p95_milliseconds"))
    except (TypeError, ValueError) as exc:
        raise BootstrapPreflightError(
            "BENCHMARK_PRESSURE_PROBE_INVALID",
            "resource benchmark pressure-probe latency is invalid",
        ) from exc
    if not (
        durable.get("all_verified") is True
        and type(sample_count) is int
        and sample_count >= 2
        and type(verified_count) is int
        and verified_count == sample_count
        and live_probe.get("all_verified") is True
        and type(live_probe.get("count")) is int
        and live_probe["count"] == sample_count
        and type(live_probe.get("bytes_per_sample")) is int
        and live_probe["bytes_per_sample"] == 4096
        and all(type(value) in (int, float) and not isinstance(value, bool) for value in latency_values)
        and all(math.isfinite(float(value)) and float(value) > 0.0 for value in latency_values)
        and float(latency_values[0])
        <= float(latency_values[1])
        <= float(latency_values[3])
        <= float(latency_values[4])
        <= float(latency_values[5])
        and float(latency_values[0])
        <= float(latency_values[2])
        <= float(latency_values[5])
        and type(live_probe.get("p95_ns")) is int
        and live_probe.get("helper_sha256") == source["pressure_probe_sha256"]
        and _SHA256_RE.fullmatch(
            str(live_probe.get("probe_root_identity_sha256") or "")
        )
        is not None
        and _SHA256_RE.fullmatch(
            str(live_probe.get("probe_volume_identity_sha256") or "")
        )
        is not None
        and live_probe.get("probe_volume_identity_sha256")
        == measurement.get("write_latency_probe_volume_identity_sha256")
        and live_probe.get("write_latency_profile")
        == PRESSURE_WRITE_LATENCY_PROFILE
        and math.isfinite(live_probe_p95_ms)
        and live_probe_p95_ms > 0.0
        and math.isclose(
            measured_fsync_p95_ms,
            live_probe_p95_ms,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        raise BootstrapPreflightError(
            "BENCHMARK_PRESSURE_PROBE_INVALID",
            "resource benchmark pressure-probe evidence is not exact",
        )
    measured_fingerprint = _require_sha256(
        measurement.get("host_fingerprint_sha256"),
        "resource_benchmark.resource_measurement.host_fingerprint_sha256",
    )
    current_fingerprint = _require_sha256(
        host_fingerprint_provider(), "current_host_fingerprint"
    )
    if (
        environment.get("host_fingerprint_matches") is not True
        or environment.get("measurement_host_fingerprint_sha256")
        != measured_fingerprint
        or environment.get("current_host_fingerprint_sha256") != measured_fingerprint
        or current_fingerprint != measured_fingerprint
    ):
        raise BootstrapPreflightError(
            "HOST_FINGERPRINT_MISMATCH",
            "resource benchmark belongs to a different host",
        )

    contract, runtime = _load_verified_capture_modules(
        source_paths["replay_capture_contract"],
        source_paths["replay_capture_runtime"],
        contract_sha256=source_hashes["replay_capture_contract"],
        runtime_sha256=source_hashes["replay_capture_runtime"],
    )
    raw_binding = _expect_mapping(
        report.get("resolved_resource_binding"),
        "resource_benchmark.resolved_resource_binding",
    )
    _exact_keys(
        raw_binding,
        {
            "binding_sha256",
            "budget",
            "budget_sha256",
            "hashes",
            "max_writer_threads",
            "measurement",
            "measurement_sha256",
            "policy",
            "policy_sha256",
            "schema_version",
        },
        "resource_benchmark.resolved_resource_binding",
    )
    raw_measurement = _expect_mapping(raw_binding.get("measurement"), "resource measurement")
    expected_measurement_projection = {
        **dict(raw_measurement),
        "durable_publication": dict(durable),
        "measurement_sha256": raw_binding.get("measurement_sha256"),
    }
    if dict(measurement) != expected_measurement_projection:
        raise BootstrapPreflightError(
            "BENCHMARK_MEASUREMENT_MISMATCH",
            "resource benchmark measurement projection differs from its binding",
        )
    raw_policy = _expect_mapping(raw_binding.get("policy"), "resource policy")
    measurement_kwargs = dict(raw_measurement)
    measurement_kwargs["measured_at"] = _parse_utc(
        measurement_kwargs.get("measured_at"), "resource measurement measured_at"
    )
    try:
        binding = runtime.CaptureResourceBinding.resolve(
            runtime.CaptureResourceMeasurement(**measurement_kwargs),
            runtime.CaptureBudgetPolicy(**dict(raw_policy)),
        )
    except BaseException as exc:
        raise BootstrapPreflightError(
            "RESOURCE_BINDING_INVALID", "resource binding cannot be recomputed"
        ) from exc
    expected_record = _json_normalized(contract, binding.to_record())
    expected_full = {
        **expected_record,
        "binding_sha256": binding.binding_sha256,
        "hashes": binding.hashes,
        "max_writer_threads": binding.budget.max_writer_threads,
    }
    if dict(raw_binding) != expected_full:
        raise BootstrapPreflightError(
            "RESOURCE_BINDING_MISMATCH",
            "persisted resource binding differs from deterministic recomputation",
        )
    expected_binding = _require_sha256(
        expected_binding_sha256, "resource_benchmark.binding_sha256"
    )
    if binding.binding_sha256 != expected_binding:
        raise BootstrapPreflightError(
            "RESOURCE_BINDING_PIN_MISMATCH",
            "resource binding differs from the bootstrap manifest pin",
        )
    if measurement.get("measurement_sha256") != binding.measurement.measurement_sha256:
        raise BootstrapPreflightError(
            "RESOURCE_MEASUREMENT_MISMATCH",
            "benchmark measurement summary differs from its binding",
        )
    return binding, tuple(reasons_raw), runtime


def _assert_capture_store_volume_binding(
    binding: Any,
    runtime: ModuleType,
    capture_store_root: Path,
) -> None:
    if (
        binding.measurement.write_latency_probe_volume_identity_sha256
        != runtime.capture_storage_volume_identity_sha256(
            capture_store_root
        )
    ):
        raise BootstrapPreflightError(
            "CAPTURE_STORAGE_VOLUME_MISMATCH",
            "resource benchmark and capture store use different storage volumes",
        )


def _validate_run_configuration(
    raw: Any,
    binding: Any,
    *,
    downstream_admission: Mapping[str, int],
) -> Mapping[str, Any]:
    config = _expect_mapping(raw, "manifest.run_configuration")
    expected = {
        "schema_version",
        "heartbeat_timeout_seconds",
        "pretrigger_horizon_seconds",
        "per_symbol_pretrigger_events",
        "writer_batch_events",
        "writer_batch_bytes",
        "writer_poll_seconds",
        "writer_flush_interval_seconds",
        "max_change_keys",
        "max_read_sources",
    }
    _exact_keys(config, expected, "manifest.run_configuration")
    if config.get("schema_version") != RUN_CONFIGURATION_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "RUN_CONFIG_SCHEMA_MISMATCH", "live capture run configuration is unsupported"
        )
    for field in (
        "heartbeat_timeout_seconds",
        "pretrigger_horizon_seconds",
        "writer_poll_seconds",
        "writer_flush_interval_seconds",
    ):
        _positive_number(config.get(field), f"run_configuration.{field}")
    for field in (
        "per_symbol_pretrigger_events",
        "writer_batch_events",
        "writer_batch_bytes",
        "max_change_keys",
        "max_read_sources",
    ):
        _positive_int(config.get(field), f"run_configuration.{field}")
    if config["per_symbol_pretrigger_events"] > binding.budget.max_ring_events:
        raise BootstrapPreflightError(
            "RUN_CONFIG_EXCEEDS_RESOURCE_BINDING",
            "per-symbol pretrigger events exceed the measured ring budget",
        )
    if config["writer_batch_events"] > downstream_admission["max_pending_events"]:
        raise BootstrapPreflightError(
            "RUN_CONFIG_EXCEEDS_RESOURCE_BINDING",
            "writer batch events exceed the queue budget remaining after IQFeed handoffs",
        )
    if config["writer_batch_bytes"] > downstream_admission["max_pending_bytes"]:
        raise BootstrapPreflightError(
            "RUN_CONFIG_EXCEEDS_RESOURCE_BINDING",
            "writer batch bytes exceed the queue bytes remaining after IQFeed handoffs",
        )
    return dict(config)


def _validate_handoff_configuration(raw: Any, binding: Any) -> Mapping[str, Any]:
    config = _expect_mapping(raw, "manifest.handoff_configuration")
    _exact_keys(
        config,
        {"schema_version", "l1", "l2"},
        "handoff_configuration",
    )
    if config.get("schema_version") != IQFEED_HANDOFF_BUDGET_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "HANDOFF_SCHEMA_MISMATCH", "IQFeed handoff budget schema is unsupported"
        )

    def _lane(name: str) -> dict[str, int]:
        lane = _expect_mapping(config.get(name), f"handoff_configuration.{name}")
        _exact_keys(
            lane,
            {"max_pending_events", "max_pending_bytes", "max_gap_keys"},
            f"handoff_configuration.{name}",
        )
        return {
            "max_pending_events": _positive_int(
                lane.get("max_pending_events"), f"handoff.{name}.max_pending_events"
            ),
            "max_pending_bytes": _positive_int(
                lane.get("max_pending_bytes"), f"handoff.{name}.max_pending_bytes"
            ),
            "max_gap_keys": _positive_int(
                lane.get("max_gap_keys"), f"handoff.{name}.max_gap_keys"
            ),
        }

    l1 = _lane("l1")
    l2 = _lane("l2")
    aggregate = {
        field: l1[field] + l2[field]
        for field in ("max_pending_events", "max_pending_bytes", "max_gap_keys")
    }
    measured = {
        "max_pending_events": int(binding.budget.max_queue_events),
        "max_pending_bytes": int(binding.budget.async_queue_bytes),
        "max_gap_keys": int(binding.budget.max_gap_keys),
    }
    if any(aggregate[field] >= measured[field] for field in measured):
        raise BootstrapPreflightError(
            "HANDOFF_EXCEEDS_RESOURCE_BINDING",
            "aggregate IQFeed handoffs must leave positive downstream resource budget",
        )
    downstream = {
        field: measured[field] - aggregate[field]
        for field in measured
    }
    return {
        "schema_version": IQFEED_HANDOFF_BUDGET_SCHEMA_VERSION,
        "l1": l1,
        "l2": l2,
        "aggregate": aggregate,
        "downstream_admission": downstream,
    }


def load_iqfeed_capture_bootstrap_preflight(
    manifest_path: str | Path,
    *,
    expected_manifest_sha256: str,
    allowed_read_roots: Sequence[str | Path],
    allowed_write_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    host_fingerprint_provider: Callable[[], str] = _host_fingerprint,
    local_drive_check: Callable[[Path], bool] = _default_local_drive_check,
) -> IqfeedCaptureBootstrapPreflight:
    """Validate every bootstrap input without starting provider/DB/store I/O."""

    if not callable(wall_clock) or not callable(host_fingerprint_provider):
        raise BootstrapPreflightError("INVALID_PROVIDER", "preflight providers are malformed")
    now = wall_clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise BootstrapPreflightError("INVALID_CLOCK", "preflight wall clock is malformed")
    now = now.astimezone(UTC)
    read_roots = _normalized_roots(
        allowed_read_roots,
        field="allowed_read_roots",
        local_drive_check=local_drive_check,
    )
    write_roots = _normalized_roots(
        allowed_write_roots,
        field="allowed_write_roots",
        local_drive_check=local_drive_check,
    )
    manifest = _read_hash_bound_json(
        manifest_path,
        expected_manifest_sha256,
        field="bootstrap_manifest",
        roots=read_roots,
        max_bytes=_MAX_MANIFEST_BYTES,
        local_drive_check=local_drive_check,
    )
    raw = manifest.document
    _exact_keys(
        raw,
        {
            "schema_version",
            "capture_mode",
            "execution_boundary",
            "freshness_policy",
            "resource_benchmark",
            "benchmark_authority_manifest",
            "benchmark_runner_authority",
            "benchmark_launch_receipt",
            "benchmark_execution_receipt",
            "benchmark_execution_claim",
            "benchmark_authority_read_roots",
            "startup_evidence",
            "capture_store_root",
            "run_configuration",
            "handoff_configuration",
        },
        "bootstrap_manifest",
    )
    if raw.get("schema_version") != BOOTSTRAP_MANIFEST_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "MANIFEST_SCHEMA_MISMATCH", "bootstrap manifest schema is unsupported"
        )
    if raw.get("capture_mode") != CAPTURE_MODE:
        raise BootstrapPreflightError(
            "CAPTURE_MODE_MISMATCH", "bootstrap preflight is diagnostic-only"
        )
    authority_roots_raw = raw.get("benchmark_authority_read_roots")
    if not isinstance(authority_roots_raw, list) or not authority_roots_raw or any(
        type(value) is not str for value in authority_roots_raw
    ):
        raise BootstrapPreflightError(
            "INVALID_AUTHORITY_ROOTS",
            "benchmark authority read-root roster is malformed",
        )
    authority_read_roots = _normalized_roots(
        tuple(authority_roots_raw),
        field="benchmark_authority_read_roots",
        local_drive_check=local_drive_check,
    )
    if (
        len(authority_read_roots) != len(authority_roots_raw)
        or any(
            not _inside_any(root, read_roots, allow_equal=True)
            for root in authority_read_roots
        )
    ):
        raise BootstrapPreflightError(
            "INVALID_AUTHORITY_ROOTS",
            "benchmark authority read roots escape the external allowlist",
        )
    boundary = _expect_mapping(raw.get("execution_boundary"), "execution_boundary")
    _exact_keys(
        boundary,
        {
            "alpaca_paper_order_submission_enabled",
            "live_cash_order_submission_enabled",
            "provider_socket_start_enabled",
            "database_write_start_enabled",
            "network_fallback_allowed",
            "current_database_fallback_allowed",
        },
        "execution_boundary",
    )
    if any(value is not False for value in boundary.values()):
        raise BootstrapPreflightError(
            "EXECUTION_BOUNDARY_OPEN",
            "every preflight execution/provider/database boundary must be false",
        )
    freshness = _expect_mapping(raw.get("freshness_policy"), "freshness_policy")
    _exact_keys(
        freshness,
        {
            "max_future_skew_seconds",
            "resource_benchmark_max_age_seconds",
            "startup_evidence_max_age_seconds",
        },
        "freshness_policy",
    )
    max_future_skew = _positive_number(
        freshness.get("max_future_skew_seconds"), "freshness.max_future_skew_seconds"
    )
    benchmark_max_age = _positive_number(
        freshness.get("resource_benchmark_max_age_seconds"),
        "freshness.resource_benchmark_max_age_seconds",
    )
    startup_max_age = _positive_number(
        freshness.get("startup_evidence_max_age_seconds"),
        "freshness.startup_evidence_max_age_seconds",
    )

    startup_ref = _expect_mapping(raw.get("startup_evidence"), "startup_evidence")
    _exact_keys(startup_ref, {"path", "sha256"}, "startup_evidence")
    startup = _read_hash_bound_json(
        startup_ref.get("path"),
        startup_ref.get("sha256"),
        field="startup_evidence",
        roots=read_roots,
        max_bytes=_MAX_STARTUP_BYTES,
        local_drive_check=local_drive_check,
    )
    startup_doc = startup.document
    _exact_keys(
        startup_doc,
        {
            "schema_version",
            "captured_at",
            "generation",
            "process_instance_id",
            "broker",
            "broker_environment",
            "code_build",
            "effective_config",
            "feature_flags",
            "account_identity",
            "account_risk_snapshot",
            "account_query",
            "account_provider",
            "account_snapshot_clocks",
            "bridge_configuration",
            "bridge_configuration_sha256",
            "iqfeed_l1_clock_contract",
            "iqfeed_l2_clock_contract",
        },
        "startup_evidence",
    )
    if startup_doc.get("schema_version") != STARTUP_EVIDENCE_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "STARTUP_SCHEMA_MISMATCH", "startup evidence schema is unsupported"
        )
    captured_at = _parse_utc(startup_doc.get("captured_at"), "startup_evidence.captured_at")
    _validate_freshness(
        captured_at,
        now=now,
        max_age_seconds=startup_max_age,
        max_future_skew_seconds=max_future_skew,
        field="startup evidence",
    )
    startup_generation = _positive_int(
        startup_doc.get("generation"), "startup_evidence.generation"
    )
    try:
        startup_process_instance_id = str(
            uuid.UUID(str(startup_doc.get("process_instance_id") or ""))
        )
    except ValueError as exc:
        raise BootstrapPreflightError(
            "INVALID_PROCESS_INSTANCE", "startup process instance id is malformed"
        ) from exc
    if startup_doc.get("broker") != "alpaca" or startup_doc.get("broker_environment") != "paper":
        raise BootstrapPreflightError(
            "ACCOUNT_ENVIRONMENT_MISMATCH",
            "capture preflight requires the observed Alpaca paper identity",
        )
    for field in (
        "effective_config",
        "feature_flags",
        "account_identity",
        "account_risk_snapshot",
        "account_query",
        "bridge_configuration",
    ):
        _expect_mapping(startup_doc.get(field), f"startup_evidence.{field}", nonempty=True)
    if str(startup_doc.get("account_provider") or "").strip().lower() != "alpaca":
        raise BootstrapPreflightError(
            "ACCOUNT_PROVIDER_MISMATCH",
            "startup account provider must match the observed Alpaca paper identity",
        )
    flags = startup_doc["feature_flags"]
    for flag in _REQUIRED_OFF_FLAGS:
        if flags.get(flag) is not False:
            raise BootstrapPreflightError(
                "BROKER_EXECUTION_FLAG_NOT_OFF", f"{flag} must be explicitly false"
            )
    clocks = _expect_mapping(
        startup_doc.get("account_snapshot_clocks"), "account_snapshot_clocks"
    )
    _exact_keys(
        clocks,
        {"provider_event_at", "received_at", "available_at"},
        "account_snapshot_clocks",
    )
    received_at = _parse_utc(clocks.get("received_at"), "account_snapshot_clocks.received_at")
    available_at = _parse_utc(clocks.get("available_at"), "account_snapshot_clocks.available_at")
    provider_event_raw = clocks.get("provider_event_at")
    provider_event_at = (
        None
        if provider_event_raw is None
        else _parse_utc(provider_event_raw, "account_snapshot_clocks.provider_event_at")
    )
    if available_at < received_at or (provider_event_at is not None and received_at < provider_event_at):
        raise BootstrapPreflightError(
            "ACCOUNT_CLOCK_ORDER_INVALID", "startup account clocks are causally inconsistent"
        )
    if captured_at < available_at:
        raise BootstrapPreflightError(
            "ACCOUNT_CLOCK_ORDER_INVALID", "startup artifact predates account availability"
        )
    clock_contract = _expect_mapping(
        startup_doc.get("iqfeed_l1_clock_contract"), "iqfeed_l1_clock_contract"
    )
    expected_clock_contract = {
        "schema_version": IQFEED_L1_CLOCK_CONTRACT_SCHEMA_VERSION,
        "exact_print": {
            "message_type": "Q",
            "selected_field_ack_required": True,
            "provider_event_at_available": True,
            "event_clock_basis": "most_recent_trade_date_plus_timems",
            "tick_identity_field": "TickID",
            "certifying_exact_event_clock": True,
        },
        "nbbo_quote": {
            "message_type": "Q",
            "provider_event_at_available": False,
            "market_reference_basis": "most_recent_trade_date_plus_timems",
            "certifying_exact_event_clock": False,
        },
    }
    if dict(clock_contract) != expected_clock_contract:
        raise BootstrapPreflightError(
            "IQFEED_CLOCK_CONTRACT_INVALID",
            "IQFeed exact-print and non-exact quote clocks must remain distinct",
        )
    l2_clock_contract = _expect_mapping(
        startup_doc.get("iqfeed_l2_clock_contract"), "iqfeed_l2_clock_contract"
    )
    expected_l2_clock_contract = {
        "schema_version": IQFEED_L2_CLOCK_CONTRACT_SCHEMA_VERSION,
        "delta": {
            "message_type": "6",
            "provider_event_at_available": True,
            "event_clock_basis": "type6_provider_date_plus_time",
            "certifying_exact_event_clock": True,
        },
        "checkpoint": {
            "provider_event_at_available": False,
            "per_level_exact_clocks_required": True,
            "initial_snapshot_complete": False,
            "certifying_snapshot_completion": False,
        },
    }
    if dict(l2_clock_contract) != expected_l2_clock_contract:
        raise BootstrapPreflightError(
            "IQFEED_L2_CLOCK_CONTRACT_INVALID",
            "IQFeed L2 delta authority cannot imply checkpoint completion",
        )
    bridge_configuration = startup_doc["bridge_configuration"]
    if _sha256_json(bridge_configuration) != _require_sha256(
        startup_doc.get("bridge_configuration_sha256"),
        "startup_evidence.bridge_configuration_sha256",
    ):
        raise BootstrapPreflightError(
            "BRIDGE_CONFIG_HASH_MISMATCH", "bridge configuration hash does not match"
        )

    code_build = _expect_mapping(startup_doc.get("code_build"), "startup_evidence.code_build")
    _exact_keys(code_build, {"schema_version", "artifacts"}, "startup_evidence.code_build")
    if code_build.get("schema_version") != CODE_BUILD_SCHEMA_VERSION:
        raise BootstrapPreflightError(
            "CODE_BUILD_SCHEMA_MISMATCH", "code-build evidence schema is unsupported"
        )
    artifacts = code_build.get("artifacts")
    if not isinstance(artifacts, list):
        raise BootstrapPreflightError("INVALID_SOURCE_ROSTER", "code-build artifacts must be a list")
    source_paths: dict[str, Path] = {}
    source_hashes: dict[str, str] = {}
    for index, row_raw in enumerate(artifacts):
        row = _expect_mapping(row_raw, f"code_build.artifacts[{index}]")
        _exact_keys(row, {"role", "path", "sha256"}, f"code_build.artifacts[{index}]")
        role = str(row.get("role") or "").strip().lower()
        if role not in _REQUIRED_SOURCE_ROLES or role in source_paths:
            raise BootstrapPreflightError(
                "INVALID_SOURCE_ROSTER", f"unexpected or duplicate source role: {role}"
            )
        path, digest = _read_hash_bound_source(
            row.get("path"),
            row.get("sha256"),
            field=f"code_build.{role}",
            roots=read_roots,
            local_drive_check=local_drive_check,
        )
        source_paths[role] = path
        source_hashes[role] = digest
    if set(source_paths) != _REQUIRED_SOURCE_ROLES:
        raise BootstrapPreflightError(
            "INVALID_SOURCE_ROSTER",
            f"source roles differ; missing={sorted(_REQUIRED_SOURCE_ROLES-set(source_paths))}",
        )

    authority_manifest_ref = _expect_mapping(
        raw.get("benchmark_authority_manifest"), "benchmark_authority_manifest"
    )
    runner_authority_ref = _expect_mapping(
        raw.get("benchmark_runner_authority"), "benchmark_runner_authority"
    )
    execution_receipt_ref = _expect_mapping(
        raw.get("benchmark_execution_receipt"), "benchmark_execution_receipt"
    )
    for field, reference in (
        ("benchmark_authority_manifest", authority_manifest_ref),
        ("benchmark_runner_authority", runner_authority_ref),
        (
            "benchmark_launch_receipt",
            _expect_mapping(
                raw.get("benchmark_launch_receipt"),
                "benchmark_launch_receipt",
            ),
        ),
        ("benchmark_execution_receipt", execution_receipt_ref),
        (
            "benchmark_execution_claim",
            _expect_mapping(
                raw.get("benchmark_execution_claim"),
                "benchmark_execution_claim",
            ),
        ),
    ):
        _exact_keys(reference, {"path", "sha256"}, field)
    authority_manifest = _read_hash_bound_json(
        authority_manifest_ref.get("path"),
        authority_manifest_ref.get("sha256"),
        field="benchmark_authority_manifest",
        roots=read_roots,
        max_bytes=_MAX_AUTHORITY_BYTES,
        local_drive_check=local_drive_check,
    )
    runner_authority = _read_hash_bound_json(
        runner_authority_ref.get("path"),
        runner_authority_ref.get("sha256"),
        field="benchmark_runner_authority",
        roots=read_roots,
        max_bytes=_MAX_AUTHORITY_BYTES,
        local_drive_check=local_drive_check,
    )
    launch_receipt_ref = _expect_mapping(
        raw.get("benchmark_launch_receipt"), "benchmark_launch_receipt"
    )
    launch_receipt = _read_hash_bound_json(
        launch_receipt_ref.get("path"),
        launch_receipt_ref.get("sha256"),
        field="benchmark_launch_receipt",
        roots=read_roots,
        max_bytes=_MAX_AUTHORITY_BYTES,
        local_drive_check=local_drive_check,
    )
    execution_receipt = _read_hash_bound_json(
        execution_receipt_ref.get("path"),
        execution_receipt_ref.get("sha256"),
        field="benchmark_execution_receipt",
        roots=read_roots,
        max_bytes=_MAX_AUTHORITY_BYTES,
        local_drive_check=local_drive_check,
    )
    execution_claim_ref = _expect_mapping(
        raw.get("benchmark_execution_claim"), "benchmark_execution_claim"
    )
    execution_claim = _read_hash_bound_json(
        execution_claim_ref.get("path"),
        execution_claim_ref.get("sha256"),
        field="benchmark_execution_claim",
        roots=read_roots,
        max_bytes=_MAX_AUTHORITY_BYTES,
        local_drive_check=local_drive_check,
        content_addressed_filename=False,
    )
    resource_ref = _expect_mapping(raw.get("resource_benchmark"), "resource_benchmark")
    _exact_keys(resource_ref, {"path", "sha256", "binding_sha256"}, "resource_benchmark")
    resource = _read_hash_bound_json(
        resource_ref.get("path"),
        resource_ref.get("sha256"),
        field="resource_benchmark",
        roots=read_roots,
        max_bytes=_MAX_BENCHMARK_BYTES,
        local_drive_check=local_drive_check,
    )
    _validate_benchmark_authority_chain(
        authority_manifest=authority_manifest,
        runner_authority=runner_authority,
        launch_receipt=launch_receipt,
        execution_receipt=execution_receipt,
        execution_claim=execution_claim,
        resource_benchmark=resource,
        source_paths=source_paths,
        source_hashes=source_hashes,
        read_roots=authority_read_roots,
        local_drive_check=local_drive_check,
    )
    binding, authority_reasons, resource_runtime = _validate_resource_report(
        resource.document,
        source_paths=source_paths,
        source_hashes=source_hashes,
        expected_binding_sha256=str(resource_ref.get("binding_sha256") or ""),
        now=now,
        benchmark_max_age_seconds=benchmark_max_age,
        max_future_skew_seconds=max_future_skew,
        host_fingerprint_provider=host_fingerprint_provider,
    )
    handoff_configuration = _validate_handoff_configuration(
        raw.get("handoff_configuration"), binding
    )
    run_configuration = _validate_run_configuration(
        raw.get("run_configuration"),
        binding,
        downstream_admission=handoff_configuration["downstream_admission"],
    )
    capture_store_root = _validated_write_root(
        raw.get("capture_store_root"),
        field="capture_store_root",
        roots=write_roots,
        local_drive_check=local_drive_check,
    )
    _assert_capture_store_volume_binding(
        binding,
        resource_runtime,
        capture_store_root,
    )
    startup_hashes = {
        "code_build_sha256": _sha256_json(startup_doc["code_build"]),
        "effective_config_sha256": _sha256_json(startup_doc["effective_config"]),
        "feature_flags_sha256": _sha256_json(startup_doc["feature_flags"]),
        "account_identity_sha256": _sha256_json(startup_doc["account_identity"]),
        "account_risk_snapshot_sha256": _sha256_json(
            startup_doc["account_risk_snapshot"]
        ),
        "account_query_sha256": _sha256_json(startup_doc["account_query"]),
        "bridge_configuration_sha256": _sha256_json(bridge_configuration),
        "iqfeed_l1_clock_contract_sha256": _sha256_json(clock_contract),
        "iqfeed_l2_clock_contract_sha256": _sha256_json(l2_clock_contract),
    }
    return IqfeedCaptureBootstrapPreflight(
        manifest_path=manifest.path,
        manifest_sha256=manifest.sha256,
        startup_evidence_path=startup.path,
        startup_evidence_sha256=startup.sha256,
        resource_benchmark_path=resource.path,
        resource_benchmark_sha256=resource.sha256,
        benchmark_authority_manifest_path=authority_manifest.path,
        benchmark_authority_manifest_sha256=authority_manifest.sha256,
        benchmark_runner_authority_path=runner_authority.path,
        benchmark_runner_authority_sha256=runner_authority.sha256,
        benchmark_launch_receipt_path=launch_receipt.path,
        benchmark_launch_receipt_sha256=launch_receipt.sha256,
        benchmark_execution_receipt_path=execution_receipt.path,
        benchmark_execution_receipt_sha256=execution_receipt.sha256,
        benchmark_execution_claim_path=execution_claim.path,
        benchmark_execution_claim_sha256=execution_claim.sha256,
        benchmark_authority_read_roots=authority_read_roots,
        resource_binding=binding,
        capture_store_root=capture_store_root,
        run_configuration=run_configuration,
        handoff_configuration=handoff_configuration,
        source_paths=source_paths,
        source_hashes=source_hashes,
        startup_evidence_hashes=startup_hashes,
        startup_captured_at=captured_at,
        startup_process_instance_id=startup_process_instance_id,
        startup_generation=startup_generation,
        broker=str(startup_doc["broker"]),
        broker_environment=str(startup_doc["broker_environment"]),
        bridge_configuration=dict(bridge_configuration),
        benchmark_authority_reasons=authority_reasons,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--allow-read-root", action="append", required=True)
    parser.add_argument("--allow-write-root", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = load_iqfeed_capture_bootstrap_preflight(
            args.manifest,
            expected_manifest_sha256=args.manifest_sha256,
            allowed_read_roots=args.allow_read_root,
            allowed_write_roots=args.allow_write_root,
        )
    except BootstrapPreflightError as exc:
        payload = {
            "schema_version": (
                "chili.iqfeed-capture-bootstrap-preflight-report.v3"
            ),
            "verdict": "BOOTSTRAP_PREFLIGHT_REJECTED",
            "preflight_valid": False,
            "activation_authorized": False,
            "provider_or_database_started": False,
            "error_code": exc.code,
            "error": exc.message,
        }
        print(_canonical_json_bytes(payload).decode("utf-8"))
        return 2
    print(_canonical_json_bytes(result.report).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
