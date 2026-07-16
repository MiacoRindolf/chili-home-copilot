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
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import stat
import sys
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

import psutil


UTC = timezone.utc
BOOTSTRAP_MANIFEST_SCHEMA_VERSION = (
    "chili.iqfeed-capture-bootstrap-preflight.v2"
)
STARTUP_EVIDENCE_SCHEMA_VERSION = (
    "chili.iqfeed-capture-startup-evidence.v2"
)
CODE_BUILD_SCHEMA_VERSION = "chili.capture-code-build.v1"
RUN_CONFIGURATION_SCHEMA_VERSION = (
    "chili.live-replay-capture-run-configuration.v1"
)
BENCHMARK_SCHEMA_VERSION = "chili.replay-capture-benchmark.v4"
IQFEED_L1_CLOCK_CONTRACT_SCHEMA_VERSION = "chili.iqfeed-l1-clock-contract.v2"
IQFEED_L2_CLOCK_CONTRACT_SCHEMA_VERSION = "chili.iqfeed-l2-clock-contract.v1"
IQFEED_HANDOFF_BUDGET_SCHEMA_VERSION = "chili.iqfeed-capture-handoff-budget.v2"
CAPTURE_MODE = "diagnostic_only"

_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_STARTUP_BYTES = 8 * 1024 * 1024
_MAX_BENCHMARK_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_BYTES = 32 * 1024 * 1024
_REPARSE_ATTRIBUTE = 0x400

_REQUIRED_SOURCE_ROLES = frozenset(
    {
        "benchmark_replay_capture_runtime",
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
                "chili.iqfeed-capture-bootstrap-preflight-report.v2"
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
) -> tuple[Any, tuple[str, ...]]:
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
        {"benchmark_script_sha256", "contract_sha256", "runtime_sha256"},
        "resource_benchmark.capture_runtime_source",
    )
    expected_source = {
        "benchmark_script_sha256": source_hashes[
            "benchmark_replay_capture_runtime"
        ],
        "contract_sha256": source_hashes["replay_capture_contract"],
        "runtime_sha256": source_hashes["replay_capture_runtime"],
    }
    if dict(source) != expected_source:
        raise BootstrapPreflightError(
            "BENCHMARK_SOURCE_MISMATCH",
            "resource benchmark was produced by different capture sources",
        )

    environment = _expect_mapping(report.get("environment"), "resource_benchmark.environment")
    measurement = _expect_mapping(
        report.get("resource_measurement"), "resource_benchmark.resource_measurement"
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
    return binding, tuple(reasons_raw)


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
    binding, authority_reasons = _validate_resource_report(
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
                "chili.iqfeed-capture-bootstrap-preflight-report.v2"
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
