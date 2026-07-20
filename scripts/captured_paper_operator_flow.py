"""Build one hash-bound Alpaca PAPER preactivation from real read authorities.

The operator flow is deliberately narrower than activation.  It may read the
exact Alpaca PAPER account, read the production schema/kill switch, run one
bounded capture-only IQFeed smoke, and run fixed disposable-test-DB shards.
It publishes a verified *preactivation* envelope and the exact next
``NoOrderSmoke`` command, but it never invokes that command, mutates Windows
tasks/processes/services, starts the PAPER host, or authorizes live cash.
Task/process/restore documents are externally collected raw snapshots: this
flow validates and binds them but does not call that a current-host inventory.
The final real, in-path ``ValidateOnly`` host observation remains mandatory.

Importing this module performs no application, broker, provider, database, or
host I/O.  The live composition helper installs the dedicated PAPER
environment before importing application modules.  Tests can pass an exact
typed composition made entirely from fakes.
"""

from __future__ import annotations

import argparse
import base64
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from types import MappingProxyType, ModuleType
from typing import Any, Callable, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit
import uuid
import xml.etree.ElementTree as ET

from scripts import build_captured_paper_preactivation as builder
from scripts import captured_paper_activation_contract as contract
from scripts import captured_paper_host_cutover as host_cutover
from scripts import captured_paper_readiness_evidence as readiness
from scripts import run_captured_paper_preactivation_probes as probes
from scripts.captured_paper_runtime_env import (
    CapturedPaperRuntimeEnvironmentReceipt,
    install_captured_paper_runtime_environment,
    validate_installed_captured_paper_settings,
)


UTC = timezone.utc
OPERATOR_PLAN_SCHEMA_VERSION = "chili.captured-paper-operator-plan.v1"
OPERATOR_RESULT_SCHEMA_VERSION = "chili.captured-paper-operator-result.v1"
OPERATOR_NEXT_COMMAND_SCHEMA_VERSION = (
    "chili.captured-paper-operator-next-command.v1"
)
OPERATOR_ERROR_SCHEMA_VERSION = "chili.captured-paper-operator-error.v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,16}$")
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_MAX_PLAN_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

# This shard is code-owned.  Neither the plan nor a caller can replace it with
# an easier test selection and still mint database-schema readiness.
MIGRATION_REHEARSAL_NODE_IDS = (
    "tests/test_captured_paper_outbox.py::"
    "test_migration_337_is_registered_idempotent_and_installs_guards",
    "tests/test_alpaca_fill_settlement_runtime_wiring.py::"
    "test_migration_336_preserves_v1_and_requires_strict_v2",
    "tests/test_alpaca_fill_activity_capture.py::"
    "test_migration_354_survives_legacy_repair_reapply",
    "tests/test_captured_paper_selection_producer.py::"
    "test_migration_350_is_registered_idempotent_and_installs_guards",
    "tests/test_captured_paper_selection_producer.py::"
    "test_batch_upsert_and_frontier_cas_commit_together",
    "tests/test_captured_paper_selection_producer.py::"
    "test_migration_353_route_state_schema_and_cas_guards",
    "tests/test_captured_paper_variant_binding.py::"
    "test_migration_352_receipt_and_append_only_transition_round_trip",
)

_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "activation_generation",
        "expected_account_id",
        "candidate_root",
        "operator_output_root",
        "preactivation_output_root",
        "activation_artifact_root",
        "capture_store_root",
        "runtime_env_path",
        "runtime_env_sha256",
        "iqfeed_bootstrap_manifest_path",
        "iqfeed_bootstrap_manifest_sha256",
        "python_executable",
        "python_dependency_root",
        "no_order_receipt_output",
        "powershell_executable",
        "host_principal_user_id",
        "task_snapshot_path",
        "task_snapshot_sha256",
        "process_snapshot_path",
        "process_snapshot_sha256",
        "restore_plan_path",
        "restore_plan_sha256",
        "capture_certification_symbol",
        "allowed_read_roots",
    }
)

_TEST_SECRET_KEYS = frozenset(
    {
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "CHILI_ALPACA_API_KEY",
        "CHILI_ALPACA_API_SECRET",
        "CHILI_ALPACA_LIVE_API_KEY",
        "CHILI_ALPACA_LIVE_API_SECRET",
        "MASSIVE_API_KEY",
        "POLYGON_API_KEY",
        "CHILI_ORTEX_API_KEY",
    }
)
_TEST_PROCESS_CONTROL_KEYS = frozenset(
    {
        "COVERAGE_PROCESS_START",
        "COVERAGE_RCFILE",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONOPTIMIZE",
        "PYTHONPATH",
        "PYTHONSAFEPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    }
)


class CapturedPaperOperatorFlowError(RuntimeError):
    """Sanitized, stable rejection from the no-activation operator flow."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True, slots=True)
class CapturedPaperOperatorConfiguration:
    """Hash-bound operator inputs, including external raw host snapshots."""

    activation_generation: str
    expected_account_id: str
    candidate_root: Path
    operator_output_root: Path
    preactivation_output_root: Path
    activation_artifact_root: Path
    capture_store_root: Path
    runtime_env_path: Path
    runtime_env_sha256: str
    iqfeed_bootstrap_manifest_path: Path
    iqfeed_bootstrap_manifest_sha256: str
    python_executable: Path
    python_dependency_root: Path
    no_order_receipt_output: Path
    powershell_executable: Path
    host_principal_user_id: str
    task_snapshot_path: Path
    task_snapshot_sha256: str
    process_snapshot_path: Path
    process_snapshot_sha256: str
    restore_plan_path: Path
    restore_plan_sha256: str
    capture_certification_symbol: str
    allowed_read_roots: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class CapturedPaperOperatorComposition:
    """Secret-bearing runtime objects; none of these are serialized."""

    configuration: CapturedPaperOperatorConfiguration
    runtime_receipt: CapturedPaperRuntimeEnvironmentReceipt
    settings_projection: Mapping[str, Any]
    paper_adapter: Any
    database_engine: Any
    migrations_module: ModuleType | Any
    capture_smoke_runner: Callable[[], Any]
    test_environment: Mapping[str, str]
    command_runner: Callable[..., Any] = subprocess.run
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic_clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True, slots=True)
class BuiltCapturedPaperOperatorFlow:
    probe_manifest_path: Path
    probe_manifest_sha256: str
    request_path: Path
    request_sha256: str
    preactivation_manifest_path: Path
    preactivation_manifest_sha256: str
    next_command_path: Path
    next_command_sha256: str
    code_build_sha256: str
    activation_generation: str
    expected_account_id: str
    account_scope: str = "alpaca:paper"
    host_snapshot_authority: str = (
        "PREACTIVATION_BASELINE_FROM_EXTERNAL_RAW_SNAPSHOT"
    )
    current_host_inventory_observed: bool = False
    final_real_validate_only_required: bool = True
    paper_order_submission_authorized: bool = False
    paper_service_started: bool = False
    live_cash_authorized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OPERATOR_RESULT_SCHEMA_VERSION,
            "verdict": (
                "CAPTURED_ALPACA_PAPER_BUILD_READY_WITH_EXTERNAL_HOST_BASELINE"
            ),
            "activation_generation": self.activation_generation,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "code_build_sha256": self.code_build_sha256,
            "probe_manifest": {
                "path": str(self.probe_manifest_path),
                "sha256": self.probe_manifest_sha256,
            },
            "build_request": {
                "path": str(self.request_path),
                "sha256": self.request_sha256,
            },
            "preactivation_manifest": {
                "path": str(self.preactivation_manifest_path),
                "sha256": self.preactivation_manifest_sha256,
            },
            "next_command": {
                "path": str(self.next_command_path),
                "sha256": self.next_command_sha256,
            },
            "host_snapshot_authority": self.host_snapshot_authority,
            "current_host_inventory_observed": False,
            "final_real_validate_only_required": True,
            "paper_order_submission_authorized": False,
            "paper_service_started": False,
            "no_order_smoke_invoked": False,
            "host_cutover_invoked": False,
            "live_cash_authorized": False,
        }


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
        raise CapturedPaperOperatorFlowError(
            "NON_CANONICAL_VALUE", "operator material is not canonical JSON"
        ) from exc


def _sha(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise CapturedPaperOperatorFlowError(
            "INVALID_SHA256", f"{field} is not a lowercase SHA-256"
        )
    return normalized


def _canonical_uuid(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(normalized)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperOperatorFlowError(
            "INVALID_UUID", f"{field} is not a canonical UUID"
        ) from exc
    if str(parsed) != normalized:
        raise CapturedPaperOperatorFlowError(
            "INVALID_UUID", f"{field} is not a canonical UUID"
        )
    return normalized


def _aware_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperOperatorFlowError(
            "INVALID_CLOCK", f"{field} is not timezone-aware"
        )
    return value.astimezone(UTC)


def _is_reparse(status: os.stat_result) -> bool:
    return stat.S_ISLNK(status.st_mode) or bool(
        int(getattr(status, "st_file_attributes", 0) or 0) & _REPARSE_ATTRIBUTE
    )


def _reject_reparse_chain(path: Path) -> None:
    cursor = path
    while True:
        try:
            status = os.lstat(cursor)
        except OSError as exc:
            raise CapturedPaperOperatorFlowError(
                "PATH_UNAVAILABLE", "an operator path is unavailable"
            ) from exc
        if _is_reparse(status):
            raise CapturedPaperOperatorFlowError(
                "REPARSE_PATH_REJECTED", "operator paths may not cross a reparse point"
            )
        parent = cursor.parent
        if parent == cursor:
            return
        cursor = parent


def _local_path(value: str | Path, field: str, *, strict: bool) -> Path:
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise CapturedPaperOperatorFlowError(
            "NONLOCAL_PATH", f"{field} must be an absolute local path"
        )
    try:
        resolved = path.resolve(strict=strict)
    except OSError as exc:
        raise CapturedPaperOperatorFlowError(
            "PATH_UNAVAILABLE", f"{field} is unavailable"
        ) from exc
    _reject_reparse_chain(resolved if strict else resolved.parent)
    return resolved


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _validated_configuration(
    value: CapturedPaperOperatorConfiguration,
) -> CapturedPaperOperatorConfiguration:
    if type(value) is not CapturedPaperOperatorConfiguration:
        raise CapturedPaperOperatorFlowError(
            "CONFIGURATION_INVALID", "operator configuration is not exact and typed"
        )
    generation = _canonical_uuid(value.activation_generation, "activation_generation")
    account = _canonical_uuid(value.expected_account_id, "expected_account_id")
    roots: list[Path] = []
    for index, raw in enumerate(value.allowed_read_roots):
        root = _local_path(raw, f"allowed_read_roots[{index}]", strict=True)
        if not root.is_dir() or root in roots:
            raise CapturedPaperOperatorFlowError(
                "READ_ROOT_INVALID", "allowed read roots must be unique directories"
            )
        roots.append(root)
    if not roots:
        raise CapturedPaperOperatorFlowError(
            "READ_ROOT_INVALID", "at least one allowed read root is required"
        )
    directories = {
        "candidate_root": value.candidate_root,
        "operator_output_root": value.operator_output_root,
        "preactivation_output_root": value.preactivation_output_root,
        "activation_artifact_root": value.activation_artifact_root,
        "capture_store_root": value.capture_store_root,
        "python_dependency_root": value.python_dependency_root,
    }
    resolved_directories: dict[str, Path] = {}
    for field, raw in directories.items():
        path = _local_path(raw, field, strict=True)
        if not path.is_dir() or not _inside(path, roots):
            raise CapturedPaperOperatorFlowError(
                "PATH_OUTSIDE_ROOT", f"{field} escaped the allowed roots"
            )
        resolved_directories[field] = path
    files = {
        "runtime_env_path": value.runtime_env_path,
        "iqfeed_bootstrap_manifest_path": value.iqfeed_bootstrap_manifest_path,
        "python_executable": value.python_executable,
        "powershell_executable": value.powershell_executable,
        "task_snapshot_path": value.task_snapshot_path,
        "process_snapshot_path": value.process_snapshot_path,
        "restore_plan_path": value.restore_plan_path,
    }
    resolved_files: dict[str, Path] = {}
    for field, raw in files.items():
        path = _local_path(raw, field, strict=True)
        if not path.is_file() or not _inside(path, roots):
            raise CapturedPaperOperatorFlowError(
                "PATH_OUTSIDE_ROOT", f"{field} escaped the allowed roots"
            )
        resolved_files[field] = path
    snapshot_hashes = {
        "task_snapshot_path": _sha(
            value.task_snapshot_sha256, "task_snapshot_sha256"
        ),
        "process_snapshot_path": _sha(
            value.process_snapshot_sha256, "process_snapshot_sha256"
        ),
        "restore_plan_path": _sha(
            value.restore_plan_sha256, "restore_plan_sha256"
        ),
    }
    for field, digest in snapshot_hashes.items():
        _stable_read(
            resolved_files[field],
            expected_sha256=digest,
            max_bytes=_MAX_ARTIFACT_BYTES,
        )
    no_order = _local_path(
        value.no_order_receipt_output,
        "no_order_receipt_output",
        strict=False,
    )
    if not _inside(no_order.parent, roots) or no_order.exists():
        raise CapturedPaperOperatorFlowError(
            "OUTPUT_NOT_FRESH", "no-order receipt output must be new and inside a read root"
        )
    principal = str(value.host_principal_user_id or "").strip()
    if not principal or any(character in principal for character in "<>\r\n"):
        raise CapturedPaperOperatorFlowError(
            "PRINCIPAL_INVALID", "host principal identity is invalid"
        )
    symbol = str(value.capture_certification_symbol or "").strip().upper()
    if _SYMBOL_RE.fullmatch(symbol) is None:
        raise CapturedPaperOperatorFlowError(
            "CAPTURE_SYMBOL_INVALID", "capture certification symbol is invalid"
        )
    return CapturedPaperOperatorConfiguration(
        activation_generation=generation,
        expected_account_id=account,
        candidate_root=resolved_directories["candidate_root"],
        operator_output_root=resolved_directories["operator_output_root"],
        preactivation_output_root=resolved_directories[
            "preactivation_output_root"
        ],
        activation_artifact_root=resolved_directories["activation_artifact_root"],
        capture_store_root=resolved_directories["capture_store_root"],
        runtime_env_path=resolved_files["runtime_env_path"],
        runtime_env_sha256=_sha(value.runtime_env_sha256, "runtime_env_sha256"),
        iqfeed_bootstrap_manifest_path=resolved_files[
            "iqfeed_bootstrap_manifest_path"
        ],
        iqfeed_bootstrap_manifest_sha256=_sha(
            value.iqfeed_bootstrap_manifest_sha256,
            "iqfeed_bootstrap_manifest_sha256",
        ),
        python_executable=resolved_files["python_executable"],
        python_dependency_root=resolved_directories["python_dependency_root"],
        no_order_receipt_output=no_order,
        powershell_executable=resolved_files["powershell_executable"],
        host_principal_user_id=principal,
        task_snapshot_path=resolved_files["task_snapshot_path"],
        task_snapshot_sha256=snapshot_hashes["task_snapshot_path"],
        process_snapshot_path=resolved_files["process_snapshot_path"],
        process_snapshot_sha256=snapshot_hashes["process_snapshot_path"],
        restore_plan_path=resolved_files["restore_plan_path"],
        restore_plan_sha256=snapshot_hashes["restore_plan_path"],
        capture_certification_symbol=symbol,
        allowed_read_roots=tuple(roots),
    )


def _stable_read(
    path: Path,
    *,
    expected_sha256: str | None,
    max_bytes: int = _MAX_ARTIFACT_BYTES,
) -> tuple[bytes, str]:
    before = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > max_bytes
    ):
        raise CapturedPaperOperatorFlowError(
            "ARTIFACT_INVALID", "operator artifact is not a bounded regular file"
        )
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise CapturedPaperOperatorFlowError(
                    "ARTIFACT_INVALID", "operator artifact exceeded its bounded size"
                )
            digest.update(chunk)
            chunks.append(chunk)
    after = path.stat(follow_symlinks=False)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise CapturedPaperOperatorFlowError(
            "ARTIFACT_CHANGED", "operator artifact changed while being read"
        )
    actual = digest.hexdigest()
    if expected_sha256 is not None and actual != _sha(expected_sha256, "artifact"):
        raise CapturedPaperOperatorFlowError(
            "ARTIFACT_HASH_MISMATCH", "operator artifact hash mismatched"
        )
    return b"".join(chunks), actual


def _publish_new_bytes(
    root: Path,
    *,
    namespace: Sequence[str],
    raw: bytes,
    suffix: str,
) -> tuple[Path, str]:
    if not raw:
        raise CapturedPaperOperatorFlowError(
            "EMPTY_ARTIFACT", "operator artifact is empty"
        )
    digest = hashlib.sha256(raw).hexdigest()
    parent = root.joinpath(*namespace, digest[:2])
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_reparse_chain(parent)
    path = parent / f"{digest}{suffix}"
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".pending", dir=str(parent)
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link publishes the fully fsync'd inode under its digest name
        # with create-new semantics on both Windows and POSIX.  A crash before
        # this point can leave only an untrusted `.pending` file, never a
        # partial object at a valid content-addressed path.
        os.link(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except FileExistsError as exc:
        raise CapturedPaperOperatorFlowError(
            "OUTPUT_ALREADY_EXISTS",
            "operator generation is append-only and may not be replayed",
        ) from exc
    except OSError as exc:
        raise CapturedPaperOperatorFlowError(
            "OUTPUT_PUBLICATION_FAILED",
            "operator artifact could not be atomically published",
        ) from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    round_trip, actual = _stable_read(path, expected_sha256=digest)
    if round_trip != raw or actual != digest:
        raise CapturedPaperOperatorFlowError(
            "OUTPUT_ROUND_TRIP_FAILED", "operator artifact did not round-trip"
        )
    return path, digest


def _publish_new_json(
    root: Path,
    *,
    namespace: Sequence[str],
    document: Mapping[str, Any],
) -> tuple[Path, str]:
    return _publish_new_bytes(
        root,
        namespace=namespace,
        raw=_canonical_json_bytes(document),
        suffix=".json",
    )


def _artifact_ref(path: Path, digest: str) -> dict[str, str]:
    return {"path": str(path), "sha256": digest}


def _assert_launcher_inventory_binding(
    *,
    initial_inventory: Any,
    repeated_inventory: Any,
    launcher_document: Mapping[str, Any],
    candidate_root: Path,
) -> None:
    """Reject source drift across launcher staging before operational reads."""

    if (
        repeated_inventory.code_build_sha256 != initial_inventory.code_build_sha256
        or dict(repeated_inventory.source_hashes)
        != dict(initial_inventory.source_hashes)
    ):
        raise CapturedPaperOperatorFlowError(
            "CODE_INVENTORY_DRIFT",
            "candidate source inventory changed during launcher staging",
        )
    expected = {
        "launcher_source_sha256": initial_inventory.source_hashes[
            "activation_launcher"
        ],
        "stage0_source_sha256": initial_inventory.source_hashes[
            "activation_stage0"
        ],
        "service_source_sha256": initial_inventory.source_hashes[
            "activation_service"
        ],
    }
    try:
        invocations = launcher_document["invocations"]
        if set(invocations) != {"ActivatePaper", "NoOrderSmoke", "ValidateOnly"}:
            raise KeyError("invocation roster")
        for mode, row in invocations.items():
            projection = row["projection"]
            if (
                Path(str(projection["candidate_root"])).resolve(strict=True)
                != candidate_root
                or any(projection.get(field) != digest for field, digest in expected.items())
            ):
                raise CapturedPaperOperatorFlowError(
                    "LAUNCHER_INVENTORY_MISMATCH",
                    f"{mode} launcher projection escaped the source inventory",
                )
    except CapturedPaperOperatorFlowError:
        raise
    except (KeyError, TypeError, OSError) as exc:
        raise CapturedPaperOperatorFlowError(
            "LAUNCHER_INVENTORY_MISMATCH",
            "launcher document does not bind the candidate inventory",
        ) from exc


def _is_isolated_test_database_url(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme.lower()
        not in {"postgres", "postgresql", "postgresql+psycopg"}
        or parsed.query
        or parsed.fragment
        or (parsed.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}
        or port is None
        or port == 5432
        or port <= 0
        or port > 65535
    ):
        return False
    # SQLAlchemy preserves percent escapes in the database component.  Check
    # the exact component that the child engine will use; decoding here would
    # let a literal ``prod%5ftest`` database masquerade as ``prod_test``.
    database_path = parsed.path
    if re.fullmatch(r"/[A-Za-z0-9_.-]+_test", database_path) is None:
        return False
    return database_path.lower() != "/_test"


def _sanitized_test_environment(value: Mapping[str, str]) -> Mapping[str, str]:
    environment = {str(key): str(item) for key, item in value.items()}
    test_database = environment.get("TEST_DATABASE_URL", "")
    if not _is_isolated_test_database_url(test_database):
        raise CapturedPaperOperatorFlowError(
            "TEST_DATABASE_UNSAFE",
            "fixed regression shards require an explicit *_test database",
        )
    for key in tuple(environment):
        upper = key.upper()
        if (
            upper in _TEST_SECRET_KEYS
            or upper in _TEST_PROCESS_CONTROL_KEYS
            or upper.startswith("PYTEST_")
        ):
            environment.pop(key, None)
    # The child can never fall back to the production target even if one test
    # imports Settings differently.  The production engine is already held by
    # the parent and is used only by explicit read-only authorities.
    environment["DATABASE_URL"] = test_database
    environment["TEST_DATABASE_URL"] = test_database
    environment["CHILI_PYTEST"] = "1"
    environment["CHILI_ALPACA_ENABLED"] = "false"
    environment["CHILI_ALPACA_PAPER"] = "true"
    environment["CHILI_AUTOPILOT_PRICE_BUS_ENABLED"] = "false"
    environment["CHILI_MOMENTUM_LIVE_RUNNER_ENABLED"] = "false"
    environment["CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED"] = "false"
    environment["CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED"] = "false"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONNOUSERSITE"] = "1"
    return MappingProxyType(environment)


@dataclass(frozen=True, slots=True)
class FixedMigrationRehearsalRunner:
    candidate_root: Path
    python_executable: Path
    environment: Mapping[str, str]
    command_runner: Callable[..., Any] = subprocess.run

    def __call__(self) -> tuple[int, ...]:
        safe_environment = _sanitized_test_environment(self.environment)
        with tempfile.TemporaryDirectory(
            prefix="captured-paper-migration-rehearsal-",
            dir=str(self.candidate_root),
        ) as temporary:
            side_effect_path = Path(temporary) / "side-effects.json"
            junit_path = Path(temporary) / "junit.xml"
            env = dict(safe_environment)
            env["CHILI_CAPTURED_PAPER_SIDE_EFFECT_REPORT"] = str(side_effect_path)
            command = (
                str(self.python_executable),
                "-B",
                "-m",
                "pytest",
                "-q",
                *MIGRATION_REHEARSAL_NODE_IDS,
                "-p",
                "scripts.captured_paper_pytest_side_effect_guard",
                "--override-ini=addopts=",
                f"--junitxml={junit_path}",
            )
            result = self.command_runner(
                command,
                cwd=str(self.candidate_root),
                env=env,
                capture_output=True,
                check=False,
            )
            if not side_effect_path.is_file() or not junit_path.is_file():
                raise CapturedPaperOperatorFlowError(
                    "MIGRATION_REHEARSAL_CENSUS_MISSING",
                    "fixed migration rehearsal did not publish both exact reports",
                )
            try:
                events = probes._load_side_effect_events(side_effect_path)
            except Exception as exc:
                raise CapturedPaperOperatorFlowError(
                    "MIGRATION_REHEARSAL_CENSUS_INVALID",
                    "fixed migration rehearsal side-effect census is invalid",
                ) from exc
            counts = {str(row["event_type"]): int(row["count"]) for row in events}
            try:
                junit_raw = junit_path.read_bytes()
                if (
                    not junit_raw
                    or len(junit_raw) > _MAX_ARTIFACT_BYTES
                    or b"<!DOCTYPE" in junit_raw.upper()
                    or b"<!ENTITY" in junit_raw.upper()
                ):
                    raise ValueError("unsafe JUnit bytes")
                junit_root = ET.fromstring(junit_raw)
                cases = tuple(
                    str(case.attrib.get("name") or "")
                    for case in junit_root.iter("testcase")
                )
                failures = tuple(junit_root.iter("failure"))
                errors = tuple(junit_root.iter("error"))
                skipped = tuple(junit_root.iter("skipped"))
            except (OSError, ET.ParseError, ValueError) as exc:
                raise CapturedPaperOperatorFlowError(
                    "MIGRATION_REHEARSAL_JUNIT_INVALID",
                    "fixed migration rehearsal JUnit is not authoritative",
                ) from exc
            expected_cases = tuple(
                node.rsplit("::", 1)[1] for node in MIGRATION_REHEARSAL_NODE_IDS
            )
            if (
                int(result.returncode) != 0
                or sorted(cases) != sorted(expected_cases)
                or len(cases) != len(expected_cases)
                or failures
                or errors
                or skipped
            ):
                raise CapturedPaperOperatorFlowError(
                    "MIGRATION_REHEARSAL_ROSTER_MISMATCH",
                    "fixed migration rehearsal did not pass the exact test roster",
                )
            if set(counts) != {
                "fake_transport",
                "real_network",
                "live_cash",
                "broker_post",
            } or any(counts.values()):
                raise CapturedPaperOperatorFlowError(
                    "MIGRATION_REHEARSAL_SIDE_EFFECT",
                    "fixed migration rehearsal attempted a forbidden side effect",
                )
            return tuple(0 for _case in expected_cases)


def _measure_capture_pressure(
    *,
    preflight: Any,
    wall_clock: Callable[[], datetime],
    monotonic_clock: Callable[[], float],
) -> Any:
    """Measure the current host without importing the activation service."""

    import psutil
    from app.services.trading.momentum_neural.replay_capture_runtime import (
        CapturePressureSample,
    )

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
                prefix=".chili-operator-pressure-", suffix=".tmp", dir=str(root)
            )
            started = float(monotonic_clock())
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(b"\0" * 4096)
                handle.flush()
                os.fsync(handle.fileno())
            latencies.append(max(0.0, (float(monotonic_clock()) - started) * 1000.0))
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
    if len(latencies) != 3:
        raise CapturedPaperOperatorFlowError(
            "PRESSURE_SAMPLE_UNAVAILABLE", "capture pressure sample is incomplete"
        )
    return CapturePressureSample(
        observed_at=_aware_utc(wall_clock(), "capture pressure clock"),
        resource_binding_sha256=preflight.resource_binding.binding_sha256,
        cpu_percent=cpu_percent,
        available_memory_bytes=available_memory,
        disk_free_bytes=disk_free,
        write_latency_milliseconds=max(latencies),
    )


def build_live_operator_composition(
    configuration: CapturedPaperOperatorConfiguration,
    *,
    preinstalled_runtime_receipt: CapturedPaperRuntimeEnvironmentReceipt | None = None,
    environ: MutableMapping[str, str] | None = None,
    command_runner: Callable[..., Any] = subprocess.run,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> CapturedPaperOperatorComposition:
    """Install exact PAPER runtime objects; perform no external read yet."""

    config = _validated_configuration(configuration)
    application_was_imported = "app.config" in sys.modules
    if application_was_imported and type(preinstalled_runtime_receipt) is not (
        CapturedPaperRuntimeEnvironmentReceipt
    ):
        raise CapturedPaperOperatorFlowError(
            "APPLICATION_IMPORTED_TOO_EARLY",
            "app.config import lacks an exact preinstalled PAPER receipt",
        )
    if preinstalled_runtime_receipt is not None and type(
        preinstalled_runtime_receipt
    ) is not CapturedPaperRuntimeEnvironmentReceipt:
        raise CapturedPaperOperatorFlowError(
            "RUNTIME_ENVIRONMENT_RECEIPT_INVALID",
            "the preinstalled PAPER environment receipt is malformed",
        )
    if environ is not None and environ is not os.environ:
        raise CapturedPaperOperatorFlowError(
            "ENVIRONMENT_TARGET_INVALID",
            "live composition must install into the current process environment",
        )
    target = os.environ
    try:
        runtime_receipt = install_captured_paper_runtime_environment(
            config.runtime_env_path,
            expected_env_sha256=config.runtime_env_sha256,
            expected_account_id=config.expected_account_id,
            first_dip_policy_mode="candidate",
            environ=target,
        )
        if preinstalled_runtime_receipt is not None:
            initial_authority = {
                "schema_version": preinstalled_runtime_receipt.schema_version,
                "source_path": preinstalled_runtime_receipt.source_path,
                "source_sha256": preinstalled_runtime_receipt.source_sha256,
                "expected_account_id": (
                    preinstalled_runtime_receipt.expected_account_id
                ),
                "first_dip_policy_mode": (
                    preinstalled_runtime_receipt.first_dip_policy_mode
                ),
                "effective_config": dict(
                    preinstalled_runtime_receipt.effective_config
                ),
                "secret_fingerprints": dict(
                    preinstalled_runtime_receipt.secret_fingerprints
                ),
                "configuration_sha256": (
                    preinstalled_runtime_receipt.configuration_sha256
                ),
            }
            refreshed_authority = {
                "schema_version": runtime_receipt.schema_version,
                "source_path": runtime_receipt.source_path,
                "source_sha256": runtime_receipt.source_sha256,
                "expected_account_id": runtime_receipt.expected_account_id,
                "first_dip_policy_mode": runtime_receipt.first_dip_policy_mode,
                "effective_config": dict(runtime_receipt.effective_config),
                "secret_fingerprints": dict(runtime_receipt.secret_fingerprints),
                "configuration_sha256": runtime_receipt.configuration_sha256,
            }
            # removed_forbidden_keys is sanitation history, not authority.  A
            # second idempotent install normally has nothing left to remove.
            if initial_authority != refreshed_authority:
                raise CapturedPaperOperatorFlowError(
                    "RUNTIME_ENVIRONMENT_RECEIPT_MISMATCH",
                    "the preinstalled PAPER environment authority changed",
                )

        from app import config as app_config

        settings = app_config.settings
        if application_was_imported:
            fresh_settings = app_config.load_process_settings()
            cached_dump = settings.model_dump(mode="python")
            fresh_dump = fresh_settings.model_dump(mode="python")
            if type(settings) is not type(fresh_settings) or cached_dump != fresh_dump:
                raise CapturedPaperOperatorFlowError(
                    "CACHED_APPLICATION_SETTINGS_MISMATCH",
                    "cached application settings differ from the sealed PAPER environment",
                )
        settings_projection = validate_installed_captured_paper_settings(
            settings, runtime_receipt, environ=target
        )
        from app.db import engine
        from app import migrations
        from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter

        adapter = AlpacaSpotAdapter()
    except CapturedPaperOperatorFlowError:
        raise
    except Exception as exc:
        raise CapturedPaperOperatorFlowError(
            "LIVE_COMPOSITION_UNAVAILABLE",
            "exact captured PAPER runtime composition failed closed",
        ) from exc

    def capture_smoke_runner() -> Any:
        from scripts.iqfeed_capture_bootstrap_preflight import (
            load_iqfeed_capture_bootstrap_preflight,
        )
        from scripts.iqfeed_capture_only_smoke import (
            CaptureOnlySmokeConfiguration,
            IngressCaptureOnlyHealthAuthority,
            run_capture_only_preactivation_smoke,
        )

        preflight = load_iqfeed_capture_bootstrap_preflight(
            config.iqfeed_bootstrap_manifest_path,
            expected_manifest_sha256=config.iqfeed_bootstrap_manifest_sha256,
            allowed_read_roots=config.allowed_read_roots,
            allowed_write_roots=(config.capture_store_root.parent,),
            wall_clock=wall_clock,
        )
        if preflight.capture_store_root.resolve() != config.capture_store_root:
            raise CapturedPaperOperatorFlowError(
                "CAPTURE_ROOT_MISMATCH", "IQFeed preflight changed capture root"
            )
        pressure = _measure_capture_pressure(
            preflight=preflight,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        health = IngressCaptureOnlyHealthAuthority(
            preflight=preflight,
            certification_symbol=config.capture_certification_symbol,
            wall_clock=wall_clock,
        )
        smoke_config = CaptureOnlySmokeConfiguration(
            preflight=preflight,
            pressure_sample=pressure,
            capture_health_authority=health,
            trade_forced_symbols=(config.capture_certification_symbol,),
            depth_forced_symbols=(config.capture_certification_symbol,),
            pressure_sampler=lambda: _measure_capture_pressure(
                preflight=preflight,
                wall_clock=wall_clock,
                monotonic_clock=monotonic_clock,
            ),
        )
        return run_capture_only_preactivation_smoke(
            smoke_config,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )

    test_environment = _sanitized_test_environment(target)
    return CapturedPaperOperatorComposition(
        configuration=config,
        runtime_receipt=runtime_receipt,
        settings_projection=settings_projection,
        paper_adapter=adapter,
        database_engine=engine,
        migrations_module=migrations,
        capture_smoke_runner=capture_smoke_runner,
        test_environment=test_environment,
        command_runner=command_runner,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )


@dataclass(frozen=True, slots=True)
class _NativeAuthority:
    value: Any

    def observe(self) -> Any:
        return self.value

    def execute(self) -> Any:
        return self.value


class _RecordedPaperAdapter:
    """In-memory one-shot replay of exact reads; it has no POST surface."""

    def __init__(
        self,
        *,
        connection: Mapping[str, Any],
        audit_before: Mapping[str, Any],
        account: Mapping[str, Any],
        positions: Mapping[str, Any],
        orders: Mapping[str, Any],
        audit_after: Mapping[str, Any],
        expected_binding: Mapping[str, Any],
    ) -> None:
        self._connection = copy.deepcopy(connection)
        self._audits = [copy.deepcopy(audit_before), copy.deepcopy(audit_after)]
        self._account = copy.deepcopy(account)
        self._positions = copy.deepcopy(positions)
        self._orders = copy.deepcopy(orders)
        self._expected_binding = copy.deepcopy(expected_binding)
        self._audit_index = 0

    def get_paper_connection_generation_receipt(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._connection)

    def get_order_submission_audit_snapshot(self) -> Mapping[str, Any]:
        if self._audit_index >= len(self._audits):
            raise RuntimeError("recorded PAPER audit was consumed more than once")
        value = self._audits[self._audit_index]
        self._audit_index += 1
        return copy.deepcopy(value)

    def get_account_snapshot(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._account)

    def get_paper_position_census(self, *, read_binding: Mapping[str, Any]) -> Any:
        if dict(read_binding) != self._expected_binding:
            raise RuntimeError("recorded PAPER position read binding changed")
        return copy.deepcopy(self._positions)

    def get_paper_open_order_census(self, *, read_binding: Mapping[str, Any]) -> Any:
        if dict(read_binding) != self._expected_binding:
            raise RuntimeError("recorded PAPER order read binding changed")
        return copy.deepcopy(self._orders)


def _record_exact_broker_reads(
    adapter: Any,
    *,
    context: readiness.ReadinessValidationContext,
) -> _RecordedPaperAdapter:
    required = (
        "get_paper_connection_generation_receipt",
        "get_order_submission_audit_snapshot",
        "get_account_snapshot",
        "get_paper_position_census",
        "get_paper_open_order_census",
    )
    if any(not callable(getattr(adapter, name, None)) for name in required):
        raise CapturedPaperOperatorFlowError(
            "BROKER_ADAPTER_INVALID", "exact PAPER read adapter is unavailable"
        )
    try:
        # The real adapter's connection receipt and audit snapshot both require
        # a frozen PAPER account UUID, and the adapter never self-binds -- the
        # account identity must be read and bound FIRST or every later exact
        # read fails closed with an unbound-generation error.
        account = adapter.get_account_snapshot()
        account_id = str((account or {}).get("account_id") or "")
        if (account or {}).get("ok") is False or account_id != str(
            context.expected_account_id
        ):
            raise RuntimeError("exact PAPER account identity mismatch")
        binder = getattr(adapter, "bind_account_id", None)
        if callable(binder) and not getattr(adapter, "bound_account_id", None):
            if binder(account_id) is not True:
                raise RuntimeError("exact PAPER account bind was refused")
        connection = adapter.get_paper_connection_generation_receipt()
        audit_before = adapter.get_order_submission_audit_snapshot()
        connection_sha = str(connection.get("receipt_sha256") or "")
        before_count = audit_before.get("submission_call_count")
        binding = {
            "schema_version": "chili.captured-paper-preactivation-broker-read.v1",
            "activation_generation": context.activation_generation,
            "account_scope": "alpaca:paper",
            "expected_account_id": context.expected_account_id,
            "connection_receipt_sha256": connection_sha,
            "orders_submitted_before": before_count,
        }
        positions = adapter.get_paper_position_census(read_binding=binding)
        orders = adapter.get_paper_open_order_census(read_binding=binding)
        audit_after = adapter.get_order_submission_audit_snapshot()
    except Exception as exc:
        raise CapturedPaperOperatorFlowError(
            "BROKER_READ_FAILED", "exact Alpaca PAPER read failed closed"
        ) from exc
    return _RecordedPaperAdapter(
        connection=connection,
        audit_before=audit_before,
        account=account,
        positions=positions,
        orders=orders,
        audit_after=audit_after,
        expected_binding=binding,
    )


def _materialize_probe_authorities(
    *,
    composition: CapturedPaperOperatorComposition,
    context: readiness.ReadinessValidationContext,
    candidate_template_path: Path,
    candidate_action_path: Path,
) -> tuple[probes.TrustedProbeAuthorities, datetime]:
    """Run slow authorities in TTL order, then mint receipts without replay I/O."""

    config = composition.configuration
    test_environment = _sanitized_test_environment(composition.test_environment)
    runtime_authority = probes.InstalledRuntimeSettingsAuthority(
        receipt=composition.runtime_receipt,
        settings_projection=composition.settings_projection,
    )
    focused_authority = probes.SubprocessFocusedRegressionAuthority(
        candidate_root=config.candidate_root,
        python_executable=config.python_executable,
        environment=test_environment,
        command_runner=composition.command_runner,
        wall_clock=composition.wall_clock,
    )
    lifecycle_authority = probes.SubprocessLifecycleScenarioAuthority(
        candidate_root=config.candidate_root,
        python_executable=config.python_executable,
        environment=test_environment,
        command_runner=composition.command_runner,
        wall_clock=composition.wall_clock,
    )
    rollback_authority = probes.HostCutoverPreactivationBaselineAuthority(
        context=context,
        candidate_root=config.candidate_root,
        allowed_read_roots=config.allowed_read_roots,
        task_snapshot_path=config.task_snapshot_path,
        process_snapshot_path=config.process_snapshot_path,
        restore_plan_path=config.restore_plan_path,
        candidate_task_xml_path=candidate_template_path,
        candidate_action_path=candidate_action_path,
        wall_clock=composition.wall_clock,
    )
    rehearsal = FixedMigrationRehearsalRunner(
        candidate_root=config.candidate_root,
        python_executable=config.python_executable,
        environment=test_environment,
        command_runner=composition.command_runner,
    )

    # Fixed tests and host snapshots have the longest TTL.  The provider smoke,
    # kill-switch read, and broker census happen last so their short freshness
    # windows do not expire behind a test shard.
    focused_native = focused_authority.execute()
    rollback_native = rollback_authority.observe()
    rehearsal_codes = rehearsal()
    lifecycle_native = lifecycle_authority.execute()
    runtime_native = runtime_authority.observe()
    capture_native = probes.CaptureOnlySmokeReadAuthority(
        smoke_runner=composition.capture_smoke_runner
    ).observe()
    database_native = probes.SqlAlchemyDatabaseReadAuthority(
        engine=composition.database_engine,
        migrations_module=composition.migrations_module,
        rehearsal_runner=lambda: rehearsal_codes,
        wall_clock=composition.wall_clock,
    ).observe()
    kill_native = probes.SqlAlchemyKillSwitchReadAuthority(
        engine=composition.database_engine,
        wall_clock=composition.wall_clock,
    ).observe()
    recorded_adapter = _record_exact_broker_reads(
        composition.paper_adapter,
        context=context,
    )
    receipt_now = _aware_utc(composition.wall_clock(), "probe receipt clock")
    return (
        probes.TrustedProbeAuthorities(
            runtime_settings=_NativeAuthority(runtime_native),
            broker_account=probes.AlpacaPaperBrokerReadAuthority(recorded_adapter),
            database_schema=_NativeAuthority(database_native),
            capture_host_smoke=_NativeAuthority(capture_native),
            focused_regressions=_NativeAuthority(focused_native),
            lifecycle_preflight=_NativeAuthority(lifecycle_native),
            kill_switch=_NativeAuthority(kill_native),
            rollback_snapshot=_NativeAuthority(rollback_native),
        ),
        receipt_now,
    )


def _build_next_no_order_command(
    *,
    config: CapturedPaperOperatorConfiguration,
    launcher_document: Mapping[str, Any],
    preactivation_path: Path,
    preactivation_sha256: str,
) -> Mapping[str, Any]:
    try:
        projection = launcher_document["invocations"]["NoOrderSmoke"]["projection"]
        read_roots = list(projection["allowed_read_roots"])
        encoded_roots = base64.b64encode(_canonical_json_bytes(read_roots)).decode(
            "ascii"
        )
        argv = [
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(projection["launcher_path"]),
            "-Mode",
            "NoOrderSmoke",
            "-PythonExecutable",
            str(projection["python_executable_path"]),
            "-CandidateRoot",
            str(projection["candidate_root"]),
            "-ServiceScriptPath",
            str(projection["service_staged_path"]),
            "-Stage0ScriptPath",
            str(projection["stage0_path"]),
            "-ManifestPath",
            str(preactivation_path),
            "-NoOrderReceiptPath",
            str(projection["no_order_receipt_output_path"]),
            "-ManifestSha256",
            preactivation_sha256,
            "-AllowedReadRootsBase64",
            encoded_roots,
        ]
    except (KeyError, TypeError) as exc:
        raise CapturedPaperOperatorFlowError(
            "NEXT_COMMAND_UNAVAILABLE", "sealed NoOrderSmoke projection is incomplete"
        ) from exc
    return {
        "schema_version": OPERATOR_NEXT_COMMAND_SCHEMA_VERSION,
        "activation_generation": config.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": config.expected_account_id,
        "next_step": "NO_ORDER_SMOKE_ONLY",
        "program": str(config.powershell_executable),
        "arguments": argv,
        "preactivation_manifest_path": str(preactivation_path),
        "preactivation_manifest_sha256": preactivation_sha256,
        "no_order_receipt_output": str(projection["no_order_receipt_output_path"]),
        "host_snapshot_authority": (
            "PREACTIVATION_BASELINE_FROM_EXTERNAL_RAW_SNAPSHOT"
        ),
        "current_host_inventory_observed": False,
        "final_real_validate_only_required": True,
        "invoked": False,
        "activate_paper_command_emitted": False,
        "host_cutover_invoked": False,
        "paper_service_started": False,
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
    }


def run_captured_paper_operator_flow(
    composition: CapturedPaperOperatorComposition,
) -> BuiltCapturedPaperOperatorFlow:
    """Run the read-only evidence path and stop before no-order/activation."""

    if type(composition) is not CapturedPaperOperatorComposition:
        raise CapturedPaperOperatorFlowError(
            "COMPOSITION_INVALID", "operator composition is not exact and typed"
        )
    config = _validated_configuration(composition.configuration)
    if composition.runtime_receipt.expected_account_id != config.expected_account_id:
        raise CapturedPaperOperatorFlowError(
            "ACCOUNT_BINDING_MISMATCH", "runtime and operator account identities differ"
        )
    if composition.runtime_receipt.first_dip_policy_mode != "candidate":
        raise CapturedPaperOperatorFlowError(
            "STRATEGY_POLICY_DARK", "PAPER first-dip policy is not the candidate policy"
        )

    bootstrap_raw, bootstrap_sha = _stable_read(
        config.iqfeed_bootstrap_manifest_path,
        expected_sha256=config.iqfeed_bootstrap_manifest_sha256,
        max_bytes=4 * 1024 * 1024,
    )
    try:
        bootstrap_doc = json.loads(bootstrap_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturedPaperOperatorFlowError(
            "BOOTSTRAP_INVALID", "IQFeed bootstrap manifest is not JSON"
        ) from exc
    if (
        not isinstance(bootstrap_doc, Mapping)
        or bootstrap_doc.get("schema_version")
        != contract.IQFEED_BOOTSTRAP_MANIFEST_SCHEMA_VERSION
    ):
        raise CapturedPaperOperatorFlowError(
            "BOOTSTRAP_INVALID", "IQFeed bootstrap schema is unsupported"
        )

    inventory = builder.inventory_captured_paper_code(
        config.candidate_root,
        allowed_read_roots=config.allowed_read_roots,
    )
    runtime_document = composition.runtime_receipt.to_dict()
    runtime_path, runtime_sha = _publish_new_json(
        config.operator_output_root,
        namespace=(config.activation_generation, "runtime"),
        document=runtime_document,
    )
    projection_document = dict(composition.settings_projection)
    projection_path, projection_sha = _publish_new_json(
        config.operator_output_root,
        namespace=(config.activation_generation, "settings"),
        document=projection_document,
    )
    effective_config_sha = str(
        projection_document.get("settings_projection_sha256") or ""
    )
    _sha(effective_config_sha, "settings_projection_sha256")
    capture_binding = {
        "schema_version": contract.CAPTURE_BINDING_SCHEMA_VERSION,
        "verdict": "PASS",
        "activation_generation": config.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": config.expected_account_id,
        "code_build_sha256": inventory.code_build_sha256,
        "effective_config_sha256": effective_config_sha,
        "live_cash_authorized": False,
        "network_fallback_allowed": False,
        "current_database_fallback_allowed": False,
    }
    capture_path, capture_sha = _publish_new_json(
        config.operator_output_root,
        namespace=(config.activation_generation, "capture-binding"),
        document=capture_binding,
    )

    launcher_document = builder.build_launcher_argument_contract_offline(
        activation_generation=config.activation_generation,
        activation_artifact_root=config.activation_artifact_root,
        candidate_root=config.candidate_root,
        python_executable=config.python_executable,
        python_dependency_root=config.python_dependency_root,
        allowed_read_roots=config.allowed_read_roots,
        no_order_receipt_output=config.no_order_receipt_output,
    )
    repeated_inventory = builder.inventory_captured_paper_code(
        config.candidate_root,
        allowed_read_roots=config.allowed_read_roots,
    )
    _assert_launcher_inventory_binding(
        initial_inventory=inventory,
        repeated_inventory=repeated_inventory,
        launcher_document=launcher_document,
        candidate_root=config.candidate_root,
    )
    launcher_path, launcher_sha = _publish_new_json(
        config.operator_output_root,
        namespace=(config.activation_generation, "launcher-contract"),
        document=launcher_document,
    )

    try:
        activate_projection = launcher_document["invocations"]["ActivatePaper"][
            "projection"
        ]
        candidate_template = host_cutover.build_candidate_task_xml_template(
            principal_user_id=config.host_principal_user_id,
            powershell_executable_path=str(config.powershell_executable),
            activate_paper_projection=activate_projection,
        )
        candidate_template_path, candidate_template_sha = _publish_new_bytes(
            config.operator_output_root,
            namespace=(config.activation_generation, "candidate-task-template"),
            raw=candidate_template,
            suffix=".xml",
        )
        candidate_action = host_cutover.build_candidate_action_document(
            host_cutover_source_sha256=inventory.source_hashes[
                "captured_paper_host_cutover"
            ],
            launcher_argument_contract_sha256=launcher_sha,
            candidate_task_xml_sha256=candidate_template_sha,
        )
    except (KeyError, TypeError, host_cutover.CapturedPaperHostCutoverError) as exc:
        raise CapturedPaperOperatorFlowError(
            "ROLLBACK_INPUT_BUILD_FAILED", "candidate rollback inputs failed closed"
        ) from exc
    candidate_action_path, _candidate_action_sha = _publish_new_json(
        config.operator_output_root,
        namespace=(config.activation_generation, "candidate-action"),
        document=candidate_action,
    )

    database_fingerprint = _sha(
        composition.runtime_receipt.secret_fingerprints.get("DATABASE_URL"),
        "database_target_fingerprint",
    )
    context = readiness.ReadinessValidationContext(
        activation_generation=config.activation_generation,
        expected_account_id=config.expected_account_id,
        code_build_sha256=inventory.code_build_sha256,
        effective_config_sha256=effective_config_sha,
        capture_receipt_sha256=capture_sha,
        runtime_environment_sha256=composition.runtime_receipt.configuration_sha256,
        database_target_fingerprint=database_fingerprint,
        iqfeed_bootstrap_manifest_sha256=bootstrap_sha,
        launcher_argument_contract_sha256=launcher_sha,
        capture_store_root=str(config.capture_store_root),
        source_hashes=inventory.source_hashes,
        allowed_read_roots=tuple(str(path) for path in config.allowed_read_roots),
    )
    materialized, receipt_now = _materialize_probe_authorities(
        composition=composition,
        context=context,
        candidate_template_path=candidate_template_path,
        candidate_action_path=candidate_action_path,
    )
    probe_root = config.operator_output_root / config.activation_generation / "probes"
    probe_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    probe_result = probes.run_trusted_preactivation_probes(
        context=context,
        authorities=materialized,
        output_root=probe_root,
        max_age_seconds_by_kind=probes.OPERATIONAL_MAX_AGE_SECONDS_BY_KIND,
        wall_clock=lambda: receipt_now,
    )
    probe_manifest = probe_result["manifest"]
    receipt_rows = probe_manifest.get("readiness_receipts")
    if not isinstance(receipt_rows, Mapping) or set(receipt_rows) != set(
        readiness.PREACTIVATION_KINDS
    ):
        raise CapturedPaperOperatorFlowError(
            "PROBE_RECEIPT_ROSTER_INVALID", "all-eight readiness roster is incomplete"
        )
    readiness_refs: dict[str, dict[str, str]] = {}
    for kind, row in sorted(receipt_rows.items()):
        if not isinstance(row, Mapping):
            raise CapturedPaperOperatorFlowError(
                "PROBE_RECEIPT_INVALID", "readiness receipt reference is malformed"
            )
        readiness_refs[kind] = _artifact_ref(
            Path(str(row.get("path") or "")),
            _sha(row.get("sha256"), f"readiness_receipts.{kind}"),
        )

    final_inventory = builder.inventory_captured_paper_code(
        config.candidate_root,
        allowed_read_roots=config.allowed_read_roots,
    )
    _assert_launcher_inventory_binding(
        initial_inventory=inventory,
        repeated_inventory=final_inventory,
        launcher_document=launcher_document,
        candidate_root=config.candidate_root,
    )
    for snapshot_path, snapshot_sha in (
        (config.task_snapshot_path, config.task_snapshot_sha256),
        (config.process_snapshot_path, config.process_snapshot_sha256),
        (config.restore_plan_path, config.restore_plan_sha256),
    ):
        _stable_read(
            snapshot_path,
            expected_sha256=snapshot_sha,
            max_bytes=_MAX_ARTIFACT_BYTES,
        )

    request = {
        "schema_version": builder.BUILD_REQUEST_SCHEMA_VERSION,
        "activation_generation": config.activation_generation,
        "expected_account_id": config.expected_account_id,
        "candidate_root": str(config.candidate_root),
        "capture_store_root": str(config.capture_store_root),
        "runtime_environment_receipt": _artifact_ref(runtime_path, runtime_sha),
        "settings_projection": _artifact_ref(projection_path, projection_sha),
        "capture_binding": _artifact_ref(capture_path, capture_sha),
        "iqfeed_bootstrap": _artifact_ref(
            config.iqfeed_bootstrap_manifest_path, bootstrap_sha
        ),
        "launcher_arguments": _artifact_ref(launcher_path, launcher_sha),
        "readiness_receipts": readiness_refs,
        "cutover": {
            "scheduled_tasks": sorted(contract._REQUIRED_TASKS),
            "singleton_policy": "one_unified_candidate_host",
            "rollback_required": True,
        },
    }
    request_path, request_sha = _publish_new_json(
        config.operator_output_root,
        namespace=(config.activation_generation, "build-request"),
        document=request,
    )
    try:
        build_now = _aware_utc(composition.wall_clock(), "preactivation build clock")
        built = builder.build_captured_paper_preactivation_offline(
            request_path=request_path,
            request_sha256=request_sha,
            candidate_root=config.candidate_root,
            output_root=config.preactivation_output_root,
            allowed_read_roots=config.allowed_read_roots,
            wall_clock=lambda: build_now,
        )
    except builder.CapturedPaperPreactivationBuildError as exc:
        raise CapturedPaperOperatorFlowError(exc.code, exc.message) from exc

    next_command = _build_next_no_order_command(
        config=config,
        launcher_document=launcher_document,
        preactivation_path=built.manifest_path,
        preactivation_sha256=built.manifest_sha256,
    )
    next_path, next_sha = _publish_new_json(
        config.operator_output_root,
        namespace=(config.activation_generation, "next-command"),
        document=next_command,
    )
    return BuiltCapturedPaperOperatorFlow(
        probe_manifest_path=Path(probe_result["manifest_path"]),
        probe_manifest_sha256=_sha(
            probe_result["manifest_sha256"], "probe manifest"
        ),
        request_path=request_path,
        request_sha256=request_sha,
        preactivation_manifest_path=built.manifest_path,
        preactivation_manifest_sha256=built.manifest_sha256,
        next_command_path=next_path,
        next_command_sha256=next_sha,
        code_build_sha256=inventory.code_build_sha256,
        activation_generation=config.activation_generation,
        expected_account_id=config.expected_account_id,
    )


def _strict_plan(path: Path, *, expected_sha256: str) -> Mapping[str, Any]:
    raw, _digest = _stable_read(
        path, expected_sha256=expected_sha256, max_bytes=_MAX_PLAN_BYTES
    )

    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise CapturedPaperOperatorFlowError(
                    "PLAN_DUPLICATE_KEY", "operator plan repeats a JSON key"
                )
            result[key] = value
        return result

    def bad_constant(value: str) -> Any:
        raise CapturedPaperOperatorFlowError(
            "PLAN_NONFINITE", f"operator plan contains {value}"
        )

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=bad_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturedPaperOperatorFlowError(
            "PLAN_INVALID", "operator plan is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(document, Mapping) or set(document) != set(_PLAN_KEYS):
        raise CapturedPaperOperatorFlowError(
            "PLAN_SCHEMA_MISMATCH", "operator plan fields are not exact"
        )
    if document.get("schema_version") != OPERATOR_PLAN_SCHEMA_VERSION:
        raise CapturedPaperOperatorFlowError(
            "PLAN_SCHEMA_MISMATCH", "operator plan schema is unsupported"
        )
    return document


def configuration_from_plan(document: Mapping[str, Any]) -> CapturedPaperOperatorConfiguration:
    if not isinstance(document.get("allowed_read_roots"), list):
        raise CapturedPaperOperatorFlowError(
            "PLAN_SCHEMA_MISMATCH", "allowed_read_roots is not an array"
        )
    return _validated_configuration(
        CapturedPaperOperatorConfiguration(
            activation_generation=str(document["activation_generation"]),
            expected_account_id=str(document["expected_account_id"]),
            candidate_root=Path(str(document["candidate_root"])),
            operator_output_root=Path(str(document["operator_output_root"])),
            preactivation_output_root=Path(
                str(document["preactivation_output_root"])
            ),
            activation_artifact_root=Path(str(document["activation_artifact_root"])),
            capture_store_root=Path(str(document["capture_store_root"])),
            runtime_env_path=Path(str(document["runtime_env_path"])),
            runtime_env_sha256=str(document["runtime_env_sha256"]),
            iqfeed_bootstrap_manifest_path=Path(
                str(document["iqfeed_bootstrap_manifest_path"])
            ),
            iqfeed_bootstrap_manifest_sha256=str(
                document["iqfeed_bootstrap_manifest_sha256"]
            ),
            python_executable=Path(str(document["python_executable"])),
            python_dependency_root=Path(str(document["python_dependency_root"])),
            no_order_receipt_output=Path(str(document["no_order_receipt_output"])),
            powershell_executable=Path(str(document["powershell_executable"])),
            host_principal_user_id=str(document["host_principal_user_id"]),
            task_snapshot_path=Path(str(document["task_snapshot_path"])),
            task_snapshot_sha256=str(document["task_snapshot_sha256"]),
            process_snapshot_path=Path(str(document["process_snapshot_path"])),
            process_snapshot_sha256=str(document["process_snapshot_sha256"]),
            restore_plan_path=Path(str(document["restore_plan_path"])),
            restore_plan_sha256=str(document["restore_plan_sha256"]),
            capture_certification_symbol=str(
                document["capture_certification_symbol"]
            ),
            allowed_read_roots=tuple(
                Path(str(value)) for value in document["allowed_read_roots"]
            ),
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        plan_path = _local_path(args.plan, "operator plan", strict=True)
        plan = _strict_plan(plan_path, expected_sha256=args.plan_sha256)
        configuration = configuration_from_plan(plan)
        composition = build_live_operator_composition(configuration)
        result = run_captured_paper_operator_flow(composition)
        document = result.to_dict()
        code = 0
    except Exception as exc:
        document = {
            "schema_version": OPERATOR_ERROR_SCHEMA_VERSION,
            "verdict": "CAPTURED_ALPACA_PAPER_BUILD_REJECTED",
            "error_code": str(
                getattr(exc, "code", "CAPTURED_PAPER_OPERATOR_REJECTED")
            ),
            "paper_order_submission_authorized": False,
            "paper_service_started": False,
            "host_cutover_invoked": False,
            "live_cash_authorized": False,
        }
        code = 2
    sys.stdout.buffer.write(_canonical_json_bytes(document) + b"\n")
    return code


__all__ = [
    "BuiltCapturedPaperOperatorFlow",
    "CapturedPaperOperatorComposition",
    "CapturedPaperOperatorConfiguration",
    "CapturedPaperOperatorFlowError",
    "FixedMigrationRehearsalRunner",
    "MIGRATION_REHEARSAL_NODE_IDS",
    "OPERATOR_PLAN_SCHEMA_VERSION",
    "OPERATOR_RESULT_SCHEMA_VERSION",
    "build_live_operator_composition",
    "configuration_from_plan",
    "main",
    "run_captured_paper_operator_flow",
]


if __name__ == "__main__":
    raise SystemExit(main())
