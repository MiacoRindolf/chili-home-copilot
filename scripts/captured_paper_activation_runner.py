"""Fail-closed one-shot operator for captured Alpaca PAPER activation.

This is the only supported outer orchestration boundary.  It consumes one
canonical, hash-bound request, owns one host-wide lock, runs every stage from
the exact pinned files, and compensates with the exact cutover rollback after
*any* failure once Apply begins.  It never authorizes live cash.

The inner activation/cutover tools remain responsible for their detailed
receipts.  This module is deliberately small enough to test with a fake
executor and does not contact a broker or mutate host state at import time.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, MutableMapping, Sequence
import uuid


REQUEST_SCHEMA_VERSION = "chili.captured-paper-activation-runner-request.v3"
RESULT_SCHEMA_VERSION = "chili.captured-paper-activation-runner-result.v1"
ACCOUNT_SCOPE = "alpaca:paper"
ACTIVATE_CONFIRMATION = "CUTOVER_FAKE_MONEY_ALPACA_PAPER"
PAPER_TASK_NAME = "CHILI-Captured-Alpaca-PAPER"

_CHAIN_OK = "CAPTURED_ALPACA_PAPER_BUILD_READY_WITH_EXTERNAL_HOST_BASELINE"
_FINAL_OK = "CAPTURED_ALPACA_PAPER_FINAL_MANIFEST_PUBLISHED"
_VALIDATE_OK = "VALIDATED_NO_HOST_MUTATION"
_RECOVERY_NONE = "NO_RECOVERY_REQUIRED"
_APPLY_OK = frozenset({"APPLIED_ALPACA_PAPER_ONLY", "ALREADY_APPLIED_EXACT"})
_ROLLBACK_OK = frozenset({"ROLLED_BACK_EXACT", "ALREADY_ROLLED_BACK_EXACT"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_STAGE_OUTPUT_BYTES = 16 * 1024 * 1024
_HOST_WIDE_ACTIVATION_LOCK_PATH = (
    Path(tempfile.gettempdir())
    / "chili-captured-alpaca-paper-activation-runner.v1.lock"
)
_HOST_WIDE_ACTIVATION_MUTEX_NAME = (
    r"Global\CHILI-Captured-Alpaca-PAPER-Activation-Runner-v1"
)
_DANGEROUS_IGNORED_SUFFIXES = frozenset(
    {
        ".bat",
        ".cmd",
        ".com",
        ".dll",
        ".exe",
        ".pyd",
        ".ps1",
        ".py",
        ".pth",
        ".scr",
        ".sh",
        ".so",
    }
)
_DANGEROUS_IGNORED_NAMES = frozenset(
    {"pyvenv.cfg", "sitecustomize.py", "usercustomize.py"}
)
_PYTHON_CONTROL_ENVIRONMENT = frozenset(
    {
        "PYTHONCASEOK",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    }
)

_CANDIDATE_ENTRYPOINTS: Mapping[str, Path] = {
    "bootstrap_stage0_script": Path("scripts/captured_paper_isolated_stage0.py"),
    "chain_script": Path("scripts/run_captured_paper_operator_chain.py"),
    "finalizer_script": Path("scripts/finalize_captured_paper_activation.py"),
    "cutover_script": Path("scripts/captured_paper_host_cutover.py"),
}
_LAUNCHER_SOURCE_PATHS: Mapping[str, Path] = {
    "activation_launcher": Path("scripts/start-captured-alpaca-paper.ps1"),
    "activation_stage0": Path("scripts/captured_paper_isolated_stage0.py"),
    "activation_service": Path("scripts/captured_alpaca_paper_service.py"),
}
_NEXT_COMMAND_KEYS = {
    "schema_version",
    "activation_generation",
    "account_scope",
    "expected_account_id",
    "next_step",
    "program",
    "arguments",
    "preactivation_manifest_path",
    "preactivation_manifest_sha256",
    "no_order_receipt_output",
    "host_snapshot_authority",
    "current_host_inventory_observed",
    "final_real_validate_only_required",
    "invoked",
    "activate_paper_command_emitted",
    "host_cutover_invoked",
    "paper_service_started",
    "paper_order_submission_authorized",
    "live_cash_authorized",
}


class CapturedPaperActivationRunnerError(RuntimeError):
    """One stable fail-closed error returned by the outer operator."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: {message}")


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
        raise CapturedPaperActivationRunnerError(
            "JSON_NOT_CANONICAL", "activation runner value is not canonical JSON"
        ) from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_json(raw: bytes, *, field: str) -> Mapping[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise CapturedPaperActivationRunnerError(
                    "JSON_DUPLICATE_KEY", f"{field} has duplicate key: {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                CapturedPaperActivationRunnerError(
                    "JSON_NONFINITE", f"{field} contains {constant}"
                )
            ),
        )
    except CapturedPaperActivationRunnerError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturedPaperActivationRunnerError(
            "JSON_INVALID", f"{field} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise CapturedPaperActivationRunnerError(
            "JSON_INVALID", f"{field} must be a JSON object"
        )
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CapturedPaperActivationRunnerError(
            "REQUEST_SHAPE_INVALID",
            f"{field} keys differ; missing={missing}; extra={extra}",
        )


def _reject_reparse_chain(path: Path) -> None:
    """Reject a Windows reparse hop without requiring Windows in unit tests."""

    # Do not call ``resolve`` before walking the caller-supplied spelling: that
    # would erase the very symlink/junction hop this boundary must reject.
    lexical = Path(os.path.abspath(os.fspath(path)))
    chains = (lexical, lexical.resolve(strict=False))
    seen: set[str] = set()
    for chain in chains:
        current = Path(chain.anchor)
        for part in chain.parts[1:]:
            current = current / part
            key = os.path.normcase(str(current))
            if key in seen:
                continue
            seen.add(key)
            if not os.path.lexists(current):
                continue
            stat_result = current.lstat()
            attributes = int(getattr(stat_result, "st_file_attributes", 0))
            if current.is_symlink() or attributes & 0x400:
                raise CapturedPaperActivationRunnerError(
                    "REPARSE_PATH", f"activation path traverses a reparse point: {path}"
                )


def _canonical_existing_path(path: Path, *, field: str) -> Path:
    """Return an existing path only when its spelling is already canonical."""

    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapturedPaperActivationRunnerError(
            "PATH_UNAVAILABLE", f"{field} cannot be resolved exactly"
        ) from exc
    if os.path.normcase(str(path)) != os.path.normcase(str(resolved)):
        raise CapturedPaperActivationRunnerError(
            "PATH_NOT_CANONICAL", f"{field} must use its exact canonical path"
        )
    return resolved


def _strict_file(path_text: str, sha256: str, *, field: str) -> Path:
    path = Path(str(path_text))
    if not path.is_absolute():
        raise CapturedPaperActivationRunnerError(
            "PATH_INVALID", f"{field} must be absolute"
        )
    _reject_reparse_chain(path)
    if not path.is_file():
        raise CapturedPaperActivationRunnerError(
            "FILE_UNAVAILABLE", f"{field} is not a regular file"
        )
    if _SHA256_RE.fullmatch(str(sha256)) is None:
        raise CapturedPaperActivationRunnerError(
            "HASH_INVALID", f"{field} SHA-256 is invalid"
        )
    if _sha256_file(path) != sha256:
        raise CapturedPaperActivationRunnerError(
            "FILE_HASH_MISMATCH", f"{field} differs from its pinned SHA-256"
        )
    return _canonical_existing_path(path, field=field)


def _strict_directory(path_text: str, *, field: str) -> Path:
    path = Path(str(path_text))
    if not path.is_absolute():
        raise CapturedPaperActivationRunnerError(
            "PATH_INVALID", f"{field} must be absolute"
        )
    _reject_network_root(path, field=field)
    _reject_reparse_chain(path)
    if not path.is_dir():
        raise CapturedPaperActivationRunnerError(
            "DIRECTORY_UNAVAILABLE", f"{field} is not a directory"
        )
    return _canonical_existing_path(path, field=field)


def _native_system32_executable(basename: str) -> Path:
    if os.name != "nt":
        raise CapturedPaperActivationRunnerError(
            "WINDOWS_EXECUTABLE_AUTHORITY_UNAVAILABLE",
            "native PAPER activation executables require Windows",
        )
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.IsWow64Process.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        )
        kernel32.IsWow64Process.restype = wintypes.BOOL
        is_wow64 = wintypes.BOOL(0)
        if not kernel32.IsWow64Process(
            kernel32.GetCurrentProcess(), ctypes.byref(is_wow64)
        ) or is_wow64.value:
            raise OSError("native System32 identity is ambiguous")
        buffer = ctypes.create_unicode_buffer(32768)
        length = int(kernel32.GetSystemDirectoryW(buffer, len(buffer)))
        if length <= 0 or length >= len(buffer):
            raise OSError("GetSystemDirectoryW failed")
        root = Path(buffer.value)
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperActivationRunnerError(
            "WINDOWS_EXECUTABLE_AUTHORITY_UNAVAILABLE",
            "native PAPER activation executables cannot be resolved",
        ) from exc
    if basename.casefold() == "powershell.exe":
        return root / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return root / basename


def _reject_network_root(path: Path, *, field: str) -> None:
    text = str(path)
    if text.startswith(("\\\\", "//")):
        raise CapturedPaperActivationRunnerError(
            "NETWORK_PATH_FORBIDDEN", f"{field} may not use a UNC path"
        )
    if os.name != "nt" or not path.anchor:
        return
    try:
        import ctypes

        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(str(Path(path.anchor))))
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperActivationRunnerError(
            "DRIVE_AUTHORITY_UNAVAILABLE", f"{field} drive type is unavailable"
        ) from exc
    if drive_type == 4:  # DRIVE_REMOTE
        raise CapturedPaperActivationRunnerError(
            "NETWORK_PATH_FORBIDDEN", f"{field} may not use a mapped network drive"
        )


def _authoritative_executable_paths() -> Mapping[str, Path]:
    """Resolve executable authority independently of the signed request."""

    git_text = shutil.which("git.exe" if os.name == "nt" else "git")
    if not git_text:
        raise CapturedPaperActivationRunnerError(
            "EXECUTABLE_AUTHORITY_UNAVAILABLE",
            "the operator Git executable cannot be resolved",
        )
    if os.name != "nt":
        # Activation itself remains Windows-only.  This branch keeps the
        # request parser testable without granting non-Windows authority.
        powershell_text = shutil.which("pwsh") or shutil.which("powershell")
        schtasks_text = shutil.which("schtasks")
        if not powershell_text or not schtasks_text:
            raise CapturedPaperActivationRunnerError(
                "WINDOWS_EXECUTABLE_AUTHORITY_UNAVAILABLE",
                "native PAPER activation executables require Windows",
            )
        powershell = Path(powershell_text)
        schtasks = Path(schtasks_text)
    else:
        powershell = _native_system32_executable("powershell.exe")
        schtasks = _native_system32_executable("schtasks.exe")
    return {
        "git_executable": _canonical_existing_path(
            Path(git_text), field="authoritative_git_executable"
        ),
        "python_executable": _canonical_existing_path(
            Path(sys.executable), field="authoritative_python_executable"
        ),
        "powershell_executable": _canonical_existing_path(
            powershell, field="authoritative_powershell_executable"
        ),
        "schtasks_executable": _canonical_existing_path(
            schtasks, field="authoritative_schtasks_executable"
        ),
    }


def _same_canonical_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _assert_expected_path(path: Path, expected: Path, *, field: str) -> None:
    if not _same_canonical_path(path, expected):
        raise CapturedPaperActivationRunnerError(
            "PATH_AUTHORITY_MISMATCH",
            f"{field} is not the canonical operator-owned path",
        )


def _assert_content_addressed_json_path(
    path: Path,
    *,
    root: Path,
    sha256: str,
    field: str,
) -> None:
    expected = root / sha256[:2] / f"{sha256}.json"
    if not _same_canonical_path(path, expected):
        raise CapturedPaperActivationRunnerError(
            "CONTENT_ADDRESS_PATH_MISMATCH",
            f"{field} is not stored at its canonical content address",
        )


@dataclass(frozen=True, slots=True)
class RunnerTimeouts:
    chain: int
    no_order_smoke: int
    finalize: int
    validate_only: int
    apply: int
    rollback: int
    task_query: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RunnerTimeouts":
        keys = {
            "chain",
            "no_order_smoke",
            "finalize",
            "validate_only",
            "apply",
            "rollback",
            "task_query",
        }
        _exact_keys(value, keys, "timeouts")
        parsed: dict[str, int] = {}
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 3600:
                raise CapturedPaperActivationRunnerError(
                    "TIMEOUT_INVALID", f"timeouts.{key} must be 1..3600 seconds"
                )
            parsed[key] = raw
        return cls(**parsed)


@dataclass(frozen=True, slots=True)
class ActivationRunnerRequest:
    request_path: Path
    request_sha256: str
    candidate_root: Path
    expected_git_commit: str
    git_executable: Path
    git_executable_sha256: str
    python_executable: Path
    python_executable_sha256: str
    powershell_executable: Path
    powershell_executable_sha256: str
    schtasks_executable: Path
    schtasks_executable_sha256: str
    bootstrap_stage0_script: Path
    bootstrap_stage0_script_sha256: str
    chain_script: Path
    chain_script_sha256: str
    chain_request_path: Path
    chain_request_sha256: str
    finalizer_script: Path
    finalizer_script_sha256: str
    cutover_script: Path
    cutover_script_sha256: str
    python_dependency_root: Path
    python_dependency_root_identity_sha256: str
    runtime_env_path: Path
    runtime_env_sha256: str
    artifact_root: Path
    expected_account_id: str
    test_database_name: str
    allowed_read_roots: tuple[str, ...]
    timeouts: RunnerTimeouts


_REQUEST_KEYS = {
    "schema_version",
    "account_scope",
    "live_cash_authorized",
    "paper_task_name",
    "candidate_root",
    "expected_git_commit",
    "git_executable",
    "git_executable_sha256",
    "python_executable",
    "python_executable_sha256",
    "powershell_executable",
    "powershell_executable_sha256",
    "schtasks_executable",
    "schtasks_executable_sha256",
    "bootstrap_stage0_script",
    "bootstrap_stage0_script_sha256",
    "chain_script",
    "chain_script_sha256",
    "chain_request_path",
    "chain_request_sha256",
    "finalizer_script",
    "finalizer_script_sha256",
    "cutover_script",
    "cutover_script_sha256",
    "python_dependency_root",
    "python_dependency_root_identity_sha256",
    "runtime_env_path",
    "runtime_env_sha256",
    "artifact_root",
    "expected_account_id",
    "test_database_name",
    "allowed_read_roots",
    "timeouts",
}


def load_activation_runner_request(
    *, request_path: str | Path, request_sha256: str
) -> ActivationRunnerRequest:
    path = Path(request_path)
    if not path.is_absolute() or _SHA256_RE.fullmatch(str(request_sha256)) is None:
        raise CapturedPaperActivationRunnerError(
            "REQUEST_REFERENCE_INVALID", "request path/hash reference is invalid"
        )
    _reject_reparse_chain(path)
    if not path.is_file() or path.stat().st_size > _MAX_REQUEST_BYTES:
        raise CapturedPaperActivationRunnerError(
            "REQUEST_UNAVAILABLE", "activation runner request is unavailable or oversized"
        )
    raw = path.read_bytes()
    if _sha256_bytes(raw) != request_sha256:
        raise CapturedPaperActivationRunnerError(
            "REQUEST_HASH_MISMATCH", "activation runner request hash differs"
        )
    value = _strict_json(raw, field="activation runner request")
    if _canonical_json_bytes(value) != raw:
        raise CapturedPaperActivationRunnerError(
            "REQUEST_NOT_CANONICAL", "activation runner request bytes are not canonical"
        )
    _exact_keys(value, _REQUEST_KEYS, "activation runner request")
    if (
        value.get("schema_version") != REQUEST_SCHEMA_VERSION
        or value.get("account_scope") != ACCOUNT_SCOPE
        or value.get("live_cash_authorized") is not False
        or value.get("paper_task_name") != PAPER_TASK_NAME
    ):
        raise CapturedPaperActivationRunnerError(
            "PAPER_SCOPE_INVALID", "request is not structurally PAPER-only"
        )
    commit = str(value.get("expected_git_commit") or "")
    if _GIT_COMMIT_RE.fullmatch(commit) is None:
        raise CapturedPaperActivationRunnerError(
            "GIT_COMMIT_INVALID", "expected Git commit must be lowercase full SHA-1"
        )
    try:
        account_id = str(uuid.UUID(str(value.get("expected_account_id") or "")))
    except (ValueError, AttributeError) as exc:
        raise CapturedPaperActivationRunnerError(
            "ACCOUNT_ID_INVALID", "expected PAPER account UUID is invalid"
        ) from exc
    if account_id != value.get("expected_account_id"):
        raise CapturedPaperActivationRunnerError(
            "ACCOUNT_ID_INVALID", "expected PAPER account UUID is not canonical"
        )
    runtime_sha = str(value.get("runtime_env_sha256") or "")
    if _SHA256_RE.fullmatch(runtime_sha) is None:
        raise CapturedPaperActivationRunnerError(
            "HASH_INVALID", "runtime environment SHA-256 is invalid"
        )
    roots_raw = value.get("allowed_read_roots")
    if not isinstance(roots_raw, list) or not roots_raw:
        raise CapturedPaperActivationRunnerError(
            "READ_ROOTS_INVALID", "allowed_read_roots must be a non-empty list"
        )
    roots: list[str] = []
    for item in roots_raw:
        root = _strict_directory(str(item), field="allowed_read_root")
        _reject_network_root(root, field="allowed_read_root")
        canonical = os.path.normcase(str(root))
        if canonical in {os.path.normcase(existing) for existing in roots}:
            raise CapturedPaperActivationRunnerError(
                "READ_ROOTS_INVALID", "allowed_read_roots contains a duplicate"
            )
        roots.append(str(root))
    timeouts_raw = value.get("timeouts")
    if not isinstance(timeouts_raw, dict):
        raise CapturedPaperActivationRunnerError(
            "TIMEOUT_INVALID", "timeouts must be an object"
        )
    test_database_name = str(value.get("test_database_name") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}_test", test_database_name):
        raise CapturedPaperActivationRunnerError(
            "TEST_DATABASE_INVALID", "test database must be a bounded *_test name"
        )
    request_file = _canonical_existing_path(path, field="activation_runner_request")
    candidate_root = _strict_directory(
        str(value.get("candidate_root")), field="candidate_root"
    )
    artifact_root = _strict_directory(
        str(value.get("artifact_root")), field="artifact_root"
    )
    root_paths = tuple(Path(root) for root in roots)
    dependency_identity_sha = str(
        value.get("python_dependency_root_identity_sha256") or ""
    )
    if _SHA256_RE.fullmatch(dependency_identity_sha) is None:
        raise CapturedPaperActivationRunnerError(
            "HASH_INVALID", "Python dependency root identity SHA-256 is invalid"
        )
    python_dependency_root = _strict_directory(
        str(value.get("python_dependency_root")), field="python_dependency_root"
    )
    for field, scoped_path in (
        ("activation_runner_request", request_file),
        ("candidate_root", candidate_root),
        ("artifact_root", artifact_root),
        ("python_dependency_root", python_dependency_root),
    ):
        _reject_network_root(scoped_path, field=field)
        if not any(_inside_directory(scoped_path, root) for root in root_paths):
            raise CapturedPaperActivationRunnerError(
                "PATH_OUTSIDE_READ_ROOTS", f"{field} escaped allowed_read_roots"
            )

    executable_hashes = {
        field: str(value.get(f"{field}_sha256") or "")
        for field in (
            "git_executable",
            "python_executable",
            "powershell_executable",
            "schtasks_executable",
        )
    }
    executable_paths = {
        field: _strict_file(
            str(value.get(field)), executable_hashes[field], field=field
        )
        for field in executable_hashes
    }
    for field, expected in _authoritative_executable_paths().items():
        _assert_expected_path(executable_paths[field], expected, field=field)

    script_hashes = {
        field: str(value.get(f"{field}_sha256") or "")
        for field in _CANDIDATE_ENTRYPOINTS
    }
    script_paths = {
        field: _strict_file(str(value.get(field)), script_hashes[field], field=field)
        for field in _CANDIDATE_ENTRYPOINTS
    }
    for field, relative in _CANDIDATE_ENTRYPOINTS.items():
        expected = _canonical_existing_path(candidate_root / relative, field=field)
        _assert_expected_path(script_paths[field], expected, field=field)

    chain_request_sha = str(value.get("chain_request_sha256") or "")
    chain_request_path = _strict_file(
        str(value.get("chain_request_path")),
        chain_request_sha,
        field="chain_request",
    )
    runtime_env_path = _strict_file(
        str(value.get("runtime_env_path")), runtime_sha, field="runtime_env_path"
    )
    for field, scoped_file in (
        ("chain_request", chain_request_path),
        ("runtime_env_path", runtime_env_path),
        ("python_executable", executable_paths["python_executable"]),
        ("powershell_executable", executable_paths["powershell_executable"]),
    ):
        if not any(_inside_directory(scoped_file, root) for root in root_paths):
            raise CapturedPaperActivationRunnerError(
                "PATH_OUTSIDE_READ_ROOTS", f"{field} escaped allowed_read_roots"
            )

    chain_raw = chain_request_path.read_bytes()
    chain_document = _strict_json(chain_raw, field="operator chain request")
    if _canonical_json_bytes(chain_document) != chain_raw:
        raise CapturedPaperActivationRunnerError(
            "CHAIN_REQUEST_NOT_CANONICAL",
            "operator chain request is not canonical JSON",
        )
    if not (
        chain_document.get("schema_version")
        == "chili.captured-paper-operator-chain-request.v1"
        and chain_document.get("account_scope") == ACCOUNT_SCOPE
        and chain_document.get("live_cash_authorized") is False
        and _same_path(
            chain_document.get("python_dependency_root"), python_dependency_root
        )
        and chain_document.get("python_dependency_root_identity_sha256")
        == dependency_identity_sha
        and _same_path(
            chain_document.get("bootstrap_stage0_script"),
            script_paths["bootstrap_stage0_script"],
        )
        and chain_document.get("bootstrap_stage0_script_sha256")
        == script_hashes["bootstrap_stage0_script"]
    ):
        raise CapturedPaperActivationRunnerError(
            "CHAIN_REQUEST_AUTHORITY_MISMATCH",
            "chain request dependency/PAPER authority differs from the outer request",
        )

    return ActivationRunnerRequest(
        request_path=request_file,
        request_sha256=request_sha256,
        candidate_root=candidate_root,
        expected_git_commit=commit,
        git_executable=executable_paths["git_executable"],
        git_executable_sha256=executable_hashes["git_executable"],
        python_executable=executable_paths["python_executable"],
        python_executable_sha256=executable_hashes["python_executable"],
        powershell_executable=executable_paths["powershell_executable"],
        powershell_executable_sha256=executable_hashes["powershell_executable"],
        schtasks_executable=executable_paths["schtasks_executable"],
        schtasks_executable_sha256=executable_hashes["schtasks_executable"],
        bootstrap_stage0_script=script_paths["bootstrap_stage0_script"],
        bootstrap_stage0_script_sha256=script_hashes["bootstrap_stage0_script"],
        chain_script=script_paths["chain_script"],
        chain_script_sha256=script_hashes["chain_script"],
        chain_request_path=chain_request_path,
        chain_request_sha256=chain_request_sha,
        finalizer_script=script_paths["finalizer_script"],
        finalizer_script_sha256=script_hashes["finalizer_script"],
        cutover_script=script_paths["cutover_script"],
        cutover_script_sha256=script_hashes["cutover_script"],
        python_dependency_root=python_dependency_root,
        python_dependency_root_identity_sha256=dependency_identity_sha,
        runtime_env_path=runtime_env_path,
        runtime_env_sha256=runtime_sha,
        artifact_root=artifact_root,
        expected_account_id=account_id,
        test_database_name=test_database_name,
        allowed_read_roots=tuple(roots),
        timeouts=RunnerTimeouts.from_mapping(timeouts_raw),
    )


_SEALED_DEPENDENCY_AUTHORITY: dict[str, Any] | None = None


def _assert_isolated_interpreter() -> None:
    flags = sys.flags
    if not (
        bool(getattr(flags, "isolated", 0))
        and bool(getattr(flags, "no_site", 0))
        and bool(getattr(flags, "dont_write_bytecode", 0))
        and bool(getattr(flags, "safe_path", 0))
    ):
        raise CapturedPaperActivationRunnerError(
            "PYTHON_ISOLATION_REQUIRED",
            "activation runner must be launched with Python -I -S -B",
        )
    forbidden_loaded = sorted(
        name
        for name in ("site", "sitecustomize", "usercustomize")
        if name in sys.modules
    )
    if forbidden_loaded:
        raise CapturedPaperActivationRunnerError(
            "PYTHON_ISOLATION_BREACH",
            "site customization loaded before activation authority",
        )


def _sanitize_python_control_environment(
    environ: MutableMapping[str, str] | None = None,
) -> None:
    target = os.environ if environ is None else environ
    for key in tuple(target):
        if key.upper() in _PYTHON_CONTROL_ENVIRONMENT:
            target.pop(key, None)
    target["PYTHONNOUSERSITE"] = "1"
    target["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True


def _load_verified_module_from_path(
    *, path: Path, sha256: str, module_name: str
) -> Any:
    verified = _strict_file(str(path), sha256, field=module_name)
    spec = importlib.util.spec_from_file_location(module_name, verified)
    if spec is None or spec.loader is None:
        raise CapturedPaperActivationRunnerError(
            "SEALED_BOOTSTRAP_UNAVAILABLE", f"{module_name} loader is unavailable"
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise CapturedPaperActivationRunnerError(
            "SEALED_BOOTSTRAP_REJECTED", f"{module_name} could not be loaded"
        ) from exc
    return module


def _install_sealed_dependency_root(request: ActivationRunnerRequest) -> None:
    """Verify/install third-party imports without site or .pth processing."""

    global _SEALED_DEPENDENCY_AUTHORITY
    if _SEALED_DEPENDENCY_AUTHORITY is not None:
        if (
            _SEALED_DEPENDENCY_AUTHORITY.get("root")
            != request.python_dependency_root
            or _SEALED_DEPENDENCY_AUTHORITY.get("identity_sha256")
            != request.python_dependency_root_identity_sha256
        ):
            raise CapturedPaperActivationRunnerError(
                "DEPENDENCY_AUTHORITY_CONFLICT",
                "process already owns a different dependency authority",
            )
        return
    root = request.python_dependency_root
    canonical_paths = {
        os.path.normcase(str(Path(value).resolve(strict=False))) for value in sys.path
    }
    if os.path.normcase(str(root)) in canonical_paths:
        raise CapturedPaperActivationRunnerError(
            "DEPENDENCY_IMPORTED_BEFORE_VERIFICATION",
            "dependency root was visible before its identity was verified",
        )
    stage0 = _load_verified_module_from_path(
        path=request.bootstrap_stage0_script,
        sha256=request.bootstrap_stage0_script_sha256,
        module_name="_chili_captured_paper_outer_stage0",
    )
    try:
        tree = stage0._dependency_tree_inventory(
            root, retain_mutation_guards=True
        )
        identity = stage0._dependency_root_identity_from_inventory(
            root=root,
            executable=request.python_executable,
            python_executable_sha256=request.python_executable_sha256,
            tree=tree,
        )
        identity_sha = _sha256_bytes(_canonical_json_bytes(dict(identity)))
    except Exception as exc:
        raise CapturedPaperActivationRunnerError(
            "DEPENDENCY_AUTHORITY_REJECTED",
            "Python dependency root identity could not be proven",
        ) from exc
    if identity_sha != request.python_dependency_root_identity_sha256:
        raise CapturedPaperActivationRunnerError(
            "DEPENDENCY_AUTHORITY_MISMATCH",
            "Python dependency root differs from its sealed identity",
        )
    dependency_finder = stage0._SealedDependencyFinder(
        root=root,
        files=tree["files"],
        guards=tuple(tree["guards"]),
    )
    deny_finder = stage0._DenyDependencyPathFinder()
    for relative in tree["files"]:
        parent = root.joinpath(*Path(relative).parts).parent
        sys.path_importer_cache[str(parent)] = deny_finder
    sys.path_importer_cache[str(root)] = deny_finder
    sys.path.append(str(root))
    sys.meta_path.insert(0, dependency_finder)
    # Candidate imports are admitted only after exact Git cleanliness and the
    # dependency mutation guards above.  site remains disabled, so no .pth or
    # sitecustomize processing occurs when these roots become visible.
    sys.path.insert(0, str(request.candidate_root))
    _SEALED_DEPENDENCY_AUTHORITY = {
        "root": root,
        "identity_sha256": identity_sha,
        "tree": tree,
        "stage0": stage0,
        "finder": dependency_finder,
    }


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class SubprocessExecutor:
    @staticmethod
    def _windows_job() -> tuple[Any, Any]:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMITS(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMITS),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("CreateJobObjectW failed")
        limits = EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel32.CloseHandle(handle)
            raise OSError("SetInformationJobObject failed")
        return handle, kernel32

    def _run_owned_tree(
        self,
        argv: Sequence[str],
        *,
        timeout: int,
        cwd: Path,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        values = [str(item) for item in argv]
        if os.name != "nt":
            process = subprocess.Popen(
                values,
                cwd=str(cwd),
                env=dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                import signal

                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            return subprocess.CompletedProcess(values, process.returncode, stdout, stderr)

        import ctypes
        from ctypes import wintypes

        job, kernel32 = self._windows_job()
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                values,
                cwd=str(cwd),
                env=dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=0x00000004,  # CREATE_SUSPENDED
            )
            kernel32.AssignProcessToJobObject.argtypes = (
                wintypes.HANDLE,
                wintypes.HANDLE,
            )
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
                raise OSError("AssignProcessToJobObject failed")
            ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
            ntdll.NtResumeProcess.restype = ctypes.c_long
            if int(ntdll.NtResumeProcess(int(process._handle))) != 0:
                raise OSError("NtResumeProcess failed")
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kernel32.TerminateJobObject(job, 0xC000013A)
                process.communicate()
                raise
            return subprocess.CompletedProcess(values, process.returncode, stdout, stderr)
        except BaseException:
            if process is not None and process.poll() is None:
                kernel32.TerminateJobObject(job, 0xC000013A)
                try:
                    process.communicate(timeout=10)
                except Exception:
                    pass
            raise
        finally:
            kernel32.CloseHandle(job)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int,
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult:
        try:
            completed = self._run_owned_tree(
                argv, timeout=timeout, cwd=cwd, env=env
            )
        except subprocess.TimeoutExpired as exc:
            raise CapturedPaperActivationRunnerError(
                "STAGE_TIMEOUT", f"stage exceeded {timeout} seconds"
            ) from exc
        except OSError as exc:
            raise CapturedPaperActivationRunnerError(
                "STAGE_LAUNCH_FAILED", "stage process could not be launched"
            ) from exc
        return CommandResult(
            returncode=int(completed.returncode),
            stdout=str(completed.stdout or ""),
            stderr=str(completed.stderr or ""),
        )


class _HostWideActivationLock(AbstractContextManager["_HostWideActivationLock"]):
    def __init__(self, path: Path, *, mutex_name: str | None = None) -> None:
        self.path = path
        self._mutex_name = mutex_name
        self._mutex_handle: Any = None
        self._handle: Any = None

    def __enter__(self) -> "_HostWideActivationLock":
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
                raise CapturedPaperActivationRunnerError(
                    "ACTIVATION_ALREADY_RUNNING",
                    "another activation runner owns or obscures the host-wide mutex",
                ) from exc
        _reject_reparse_chain(self.path)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"0")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError) as exc:
            self._handle.close()
            self._handle = None
            raise CapturedPaperActivationRunnerError(
                "ACTIVATION_ALREADY_RUNNING",
                "another activation runner owns the host-wide lock",
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
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _last_json_line(value: str) -> Mapping[str, Any] | None:
    for line in reversed(str(value or "").splitlines()):
        candidate = line.strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            parsed = _strict_json(candidate.encode("utf-8"), field="stage_result")
        except CapturedPaperActivationRunnerError:
            continue
        return parsed
    return None


def _write_once(path: Path, raw: bytes) -> None:
    if path.exists():
        if path.read_bytes() != raw:
            raise CapturedPaperActivationRunnerError(
                "APPEND_ONLY_CONFLICT", f"append-only artifact conflicts: {path.name}"
            )
        return
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


class _StageRecorder:
    def __init__(self, root: Path) -> None:
        _reject_reparse_chain(root.parent)
        self.root = root
        self.root.mkdir(mode=0o700, parents=False, exist_ok=False)

    def record(self, stage: str, result: CommandResult) -> None:
        for suffix, value in (("stdout", result.stdout), ("stderr", result.stderr)):
            raw = value.encode("utf-8", errors="replace")
            if len(raw) > _MAX_STAGE_OUTPUT_BYTES:
                raw = raw[-_MAX_STAGE_OUTPUT_BYTES:]
            digest = _sha256_bytes(raw)
            _write_once(self.root / f"{stage}.{digest}.{suffix}", raw)


def _minimal_git_environment(*, sandbox: Path) -> dict[str, str]:
    safe_names = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SystemRoot",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    env = {name: value for name, value in os.environ.items() if name in safe_names}
    hooks = sandbox / "empty-hooks"
    hooks.mkdir(mode=0o700, parents=False, exist_ok=False)
    null_device = "NUL" if os.name == "nt" else "/dev/null"
    env.update(
        {
            "GIT_CONFIG_GLOBAL": null_device,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "5",
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
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(sandbox),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return env


def _ignored_payload_is_dangerous(relative: str) -> bool:
    path = Path(str(relative or "").replace("\\", "/"))
    name = path.name.casefold()
    if (
        name in _DANGEROUS_IGNORED_NAMES
        or path.suffix.casefold() in _DANGEROUS_IGNORED_SUFFIXES
    ):
        return True
    if path.suffix.casefold() == ".pyc":
        return "__pycache__" not in {part.casefold() for part in path.parts}
    return False


def _verify_repo(request: ActivationRunnerRequest, executor: Any, env: Mapping[str, str]) -> None:
    top_level = executor.run(
        [str(request.git_executable), "rev-parse", "--show-toplevel"],
        timeout=request.timeouts.task_query,
        cwd=request.candidate_root,
        env=env,
    )
    if top_level.returncode != 0 or not _same_path(
        top_level.stdout.strip(), request.candidate_root
    ):
        raise CapturedPaperActivationRunnerError(
            "GIT_ROOT_MISMATCH",
            "candidate_root is not the exact Git worktree root",
        )
    head = executor.run(
        [str(request.git_executable), "rev-parse", "HEAD"],
        timeout=request.timeouts.task_query,
        cwd=request.candidate_root,
        env=env,
    )
    if head.returncode != 0 or head.stdout.strip() != request.expected_git_commit:
        raise CapturedPaperActivationRunnerError(
            "GIT_COMMIT_MISMATCH", "candidate repository is not the pinned commit"
        )
    status = executor.run(
        [
            str(request.git_executable),
            "status",
            "--porcelain=v2",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        timeout=request.timeouts.task_query,
        cwd=request.candidate_root,
        env=env,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise CapturedPaperActivationRunnerError(
            "WORKTREE_DIRTY",
            "candidate repository has tracked or untracked drift",
        )
    tracked_relatives = sorted(
        {
            *(relative.as_posix() for relative in _CANDIDATE_ENTRYPOINTS.values()),
            *(relative.as_posix() for relative in _LAUNCHER_SOURCE_PATHS.values()),
            "scripts/captured_paper_activation_runner.py",
            "scripts/captured_paper_runtime_env.py",
        }
    )
    tracked = executor.run(
        [
            str(request.git_executable),
            "ls-files",
            "--error-unmatch",
            "-z",
            "--",
            *tracked_relatives,
        ],
        timeout=request.timeouts.task_query,
        cwd=request.candidate_root,
        env=env,
    )
    observed_tracked = {item for item in tracked.stdout.split("\0") if item}
    if tracked.returncode != 0 or observed_tracked != set(tracked_relatives):
        raise CapturedPaperActivationRunnerError(
            "GIT_CRITICAL_PATH_UNTRACKED",
            "one or more activation entrypoints are not tracked exactly",
        )
    ignored_code = executor.run(
        [
            str(request.git_executable),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ],
        timeout=request.timeouts.task_query,
        cwd=request.candidate_root,
        env=env,
    )
    if ignored_code.returncode != 0:
        raise CapturedPaperActivationRunnerError(
            "GIT_IGNORED_INVENTORY_UNAVAILABLE",
            "ignored payload inventory could not be proven",
        )
    dangerous = sorted(
        item
        for item in ignored_code.stdout.split("\0")
        if item and _ignored_payload_is_dangerous(item)
    )
    if dangerous:
        raise CapturedPaperActivationRunnerError(
            "GIT_IGNORED_EXECUTABLE_PAYLOAD",
            "candidate worktree contains ignored executable/importable payloads",
        )


def _paper_task_exists(request: ActivationRunnerRequest, executor: Any, env: Mapping[str, str]) -> bool:
    _assert_expected_path(
        _strict_file(
            str(request.schtasks_executable),
            request.schtasks_executable_sha256,
            field="schtasks_executable",
        ),
        _authoritative_executable_paths()["schtasks_executable"],
        field="schtasks_executable",
    )
    result = executor.run(
        [
            str(request.schtasks_executable),
            "/Query",
            "/TN",
            PAPER_TASK_NAME,
            "/FO",
            "LIST",
        ],
        timeout=request.timeouts.task_query,
        cwd=request.candidate_root,
        env=env,
    )
    if result.returncode == 0:
        return True
    combined = f"{result.stdout}\n{result.stderr}".casefold()
    if result.returncode == 1 and (
        "cannot find the file specified" in combined
        or "cannot find the task" in combined
    ):
        return False
    raise CapturedPaperActivationRunnerError(
        "TASK_QUERY_FAILED", "candidate PAPER task state is not authoritative"
    )


def _run_stage(
    *,
    name: str,
    argv: Sequence[str],
    timeout: int,
    request: ActivationRunnerRequest,
    executor: Any,
    env: Mapping[str, str],
    recorder: _StageRecorder,
    prelaunch_validator: Callable[[], None] | None = None,
) -> CommandResult:
    # Hash/path authority is checked again immediately before every process
    # creation.  A clean Git check alone cannot close a chain-stage mutation or
    # load-to-execute replacement window for external executables.
    _revalidate_request_path_authority(request)
    if prelaunch_validator is not None:
        prelaunch_validator()
    result = executor.run(
        argv,
        timeout=timeout,
        cwd=request.candidate_root,
        env=env,
    )
    recorder.record(name, result)
    return result


def _reference_path_and_document(
    reference: Mapping[str, Any], *, field: str
) -> tuple[Path, Mapping[str, Any]]:
    if set(reference) != {"path", "sha256"}:
        raise CapturedPaperActivationRunnerError(
            "REFERENCE_INVALID", f"{field} reference shape is invalid"
        )
    path = _strict_file(str(reference.get("path")), str(reference.get("sha256")), field=field)
    raw = path.read_bytes()
    document = _strict_json(raw, field=field)
    if _canonical_json_bytes(document) != raw:
        raise CapturedPaperActivationRunnerError(
            "REFERENCE_NOT_CANONICAL", f"{field} bytes are not canonical JSON"
        )
    return path, document


def _reference_document(reference: Mapping[str, Any], *, field: str) -> Mapping[str, Any]:
    return _reference_path_and_document(reference, field=field)[1]


def _inside_directory(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _expected_no_order_powershell_arguments(
    *,
    projection: Mapping[str, Any],
    preactivation_path: Path,
    preactivation_sha256: str,
) -> tuple[str, ...]:
    roots = projection.get("allowed_read_roots")
    if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
        raise CapturedPaperActivationRunnerError(
            "LAUNCHER_PROJECTION_INVALID",
            "NoOrderSmoke projection has invalid read roots",
        )
    encoded_roots = base64.b64encode(_canonical_json_bytes(roots)).decode("ascii")
    required = {
        "launcher_path",
        "python_executable_path",
        "candidate_root",
        "service_staged_path",
        "stage0_path",
        "no_order_receipt_output_path",
    }
    if any(not isinstance(projection.get(key), str) for key in required):
        raise CapturedPaperActivationRunnerError(
            "LAUNCHER_PROJECTION_INVALID",
            "NoOrderSmoke projection is incomplete",
        )
    return (
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
    )


def _revalidate_staged_no_order_paths(projection: Mapping[str, Any]) -> None:
    for field, path_key, sha_key in (
        ("staged_launcher", "launcher_path", "launcher_sha256"),
        ("staged_stage0", "stage0_path", "stage0_sha256"),
        ("staged_service", "service_staged_path", "service_sha256"),
    ):
        _strict_file(
            str(projection.get(path_key) or ""),
            str(projection.get(sha_key) or ""),
            field=field,
        )


def _validate_no_order_launcher_authority(
    *,
    next_command: Mapping[str, Any],
    preactivation_path: Path,
    preactivation_sha256: str,
    preactivation: Mapping[str, Any],
    request: ActivationRunnerRequest,
    generation: str,
) -> tuple[str, tuple[str, ...], Path, Mapping[str, Any]]:
    """Bind the executable PowerShell argv to the sealed launcher projection."""

    _exact_keys(next_command, _NEXT_COMMAND_KEYS, "next_command")
    cutover = preactivation.get("cutover")
    authority = preactivation.get("authority_boundary")
    claimed_self_digest = str(
        preactivation.get("activation_manifest_sha256") or ""
    )
    preactivation_body = dict(preactivation)
    preactivation_body.pop("activation_manifest_sha256", None)
    if not (
        preactivation.get("schema_version")
        == "chili.captured-paper-preactivation.v2"
        and preactivation.get("activation_generation") == generation
        and _SHA256_RE.fullmatch(claimed_self_digest) is not None
        and _sha256_bytes(_canonical_json_bytes(preactivation_body))
        == claimed_self_digest
        and isinstance(authority, dict)
        and authority.get("broker") == "alpaca"
        and authority.get("broker_environment") == "paper"
        and authority.get("account_scope") == ACCOUNT_SCOPE
        and authority.get("expected_account_id") == request.expected_account_id
        and authority.get("paper_order_submission_authorized") is False
        and authority.get("live_cash_authorized") is False
        and authority.get("real_money_authorized") is False
        and isinstance(cutover, dict)
        and _same_path(cutover.get("candidate_root"), request.candidate_root)
    ):
        raise CapturedPaperActivationRunnerError(
            "PREACTIVATION_AUTHORITY_MISMATCH",
            "preactivation is not the exact self-bound PAPER authority",
        )
    launcher_reference = {
        "path": cutover.get("launcher_arguments_path"),
        "sha256": cutover.get("launcher_arguments_sha256"),
    }
    launcher_path, launcher_document = _reference_path_and_document(
        launcher_reference, field="launcher_arguments"
    )
    launcher_sha = str(launcher_reference["sha256"] or "")
    _assert_content_addressed_json_path(
        launcher_path,
        root=(request.artifact_root / "operator" / generation / "launcher-contract"),
        sha256=launcher_sha,
        field="launcher_arguments",
    )
    source_paths = {
        role: _canonical_existing_path(request.candidate_root / relative, field=role)
        for role, relative in _LAUNCHER_SOURCE_PATHS.items()
    }
    source_hashes = {role: _sha256_file(path) for role, path in source_paths.items()}
    try:
        from scripts import captured_paper_activation_contract as contract

        invocations = contract._validate_launcher_argument_contract(
            launcher_document,
            raw=launcher_path.read_bytes(),
            candidate_root=request.candidate_root,
            allowed_read_roots=tuple(Path(root) for root in request.allowed_read_roots),
            source_paths=source_paths,
            source_hashes=source_hashes,
            activation_generation=generation,
        )
    except Exception as exc:
        raise CapturedPaperActivationRunnerError(
            "LAUNCHER_PROJECTION_INVALID",
            "sealed NoOrderSmoke launcher projection rejected",
        ) from exc
    projection = invocations.get("NoOrderSmoke")
    if not isinstance(projection, Mapping):
        raise CapturedPaperActivationRunnerError(
            "LAUNCHER_PROJECTION_INVALID",
            "sealed NoOrderSmoke projection is unavailable",
        )
    expected_arguments = _expected_no_order_powershell_arguments(
        projection=projection,
        preactivation_path=preactivation_path,
        preactivation_sha256=preactivation_sha256,
    )
    program = str(next_command.get("program") or "")
    arguments = next_command.get("arguments")
    receipt = str(projection.get("no_order_receipt_output_path") or "")
    checks = {
        "schema_version": next_command.get("schema_version")
        == "chili.captured-paper-operator-next-command.v1",
        "activation_generation": next_command.get("activation_generation") == generation,
        "account_scope": next_command.get("account_scope") == ACCOUNT_SCOPE,
        "expected_account_id": next_command.get("expected_account_id")
        == request.expected_account_id,
        "next_step": next_command.get("next_step") == "NO_ORDER_SMOKE_ONLY",
        "host_snapshot_authority": next_command.get("host_snapshot_authority")
        == "PREACTIVATION_BASELINE_FROM_EXTERNAL_RAW_SNAPSHOT",
        "current_host_inventory_observed": next_command.get(
            "current_host_inventory_observed"
        )
        is False,
        "final_real_validate_only_required": next_command.get(
            "final_real_validate_only_required"
        )
        is True,
        "invoked": next_command.get("invoked") is False,
        "activate_paper_command_emitted": next_command.get(
            "activate_paper_command_emitted"
        )
        is False,
        "host_cutover_invoked": next_command.get("host_cutover_invoked") is False,
        "paper_order_submission_authorized": next_command.get(
            "paper_order_submission_authorized"
        )
        is False,
        "paper_service_started": next_command.get("paper_service_started") is False,
        "live_cash_authorized": next_command.get("live_cash_authorized") is False,
        "program": program == str(request.powershell_executable),
        "arguments": arguments == list(expected_arguments),
        "preactivation_manifest_path": next_command.get(
            "preactivation_manifest_path"
        )
        == str(preactivation_path),
        "preactivation_manifest_sha256": next_command.get(
            "preactivation_manifest_sha256"
        )
        == preactivation_sha256,
        "no_order_receipt_output": next_command.get("no_order_receipt_output")
        == receipt,
        "projection_candidate_root": _same_path(
            projection.get("candidate_root"), request.candidate_root
        ),
        "projection_python_executable": _same_path(
            projection.get("python_executable_path"), request.python_executable
        ),
        "projection_python_sha256": projection.get("python_executable_sha256")
        == request.python_executable_sha256,
        "projection_dependency_root": _same_path(
            projection.get("python_dependency_root"),
            Path(str(cutover.get("python_dependency_root") or "")),
        ),
        "projection_dependency_identity": projection.get(
            "python_dependency_root_identity_sha256"
        )
        == cutover.get("python_dependency_root_identity_sha256"),
        "projection_allowed_read_roots": tuple(
            sorted(
                os.path.normcase(str(root))
                for root in projection.get("allowed_read_roots", [])
            )
        )
        == tuple(sorted(os.path.normcase(root) for root in request.allowed_read_roots)),
    }
    failed_checks = sorted(name for name, passed in checks.items() if not passed)
    if failed_checks:
        raise CapturedPaperActivationRunnerError(
            "NEXT_COMMAND_AUTHORITY_MISMATCH",
            "no-order launcher argv differs from the sealed projection: "
            + ",".join(failed_checks),
        )
    receipt_path = Path(receipt)
    if not receipt_path.is_absolute():
        raise CapturedPaperActivationRunnerError(
            "NEXT_COMMAND_INVALID", "no-order receipt path is not absolute"
        )
    return program, expected_arguments, receipt_path, projection


def _same_path(left: Any, right: Path) -> bool:
    try:
        candidate = Path(str(left or ""))
        if not candidate.is_absolute():
            return False
        _reject_reparse_chain(candidate)
        return os.path.normcase(str(candidate.resolve(strict=False))) == os.path.normcase(
            str(right.resolve(strict=False))
        )
    except (OSError, CapturedPaperActivationRunnerError):
        return False


def _revalidate_request_path_authority(request: ActivationRunnerRequest) -> None:
    """Close the load-to-execute gap for every file used by the runner."""

    _assert_expected_path(
        _strict_file(
            str(request.request_path),
            request.request_sha256,
            field="activation_runner_request",
        ),
        request.request_path,
        field="activation_runner_request",
    )
    expected_executables = _authoritative_executable_paths()
    for field in (
        "git_executable",
        "python_executable",
        "powershell_executable",
        "schtasks_executable",
    ):
        path = getattr(request, field)
        digest = getattr(request, f"{field}_sha256")
        actual = _strict_file(str(path), digest, field=field)
        _assert_expected_path(actual, expected_executables[field], field=field)
    for field, relative in _CANDIDATE_ENTRYPOINTS.items():
        path = getattr(request, field)
        digest = getattr(request, f"{field}_sha256")
        actual = _strict_file(str(path), digest, field=field)
        expected = _canonical_existing_path(request.candidate_root / relative, field=field)
        _assert_expected_path(actual, expected, field=field)
    for field, path, digest in (
        (
            "chain_request",
            request.chain_request_path,
            request.chain_request_sha256,
        ),
        ("runtime_env_path", request.runtime_env_path, request.runtime_env_sha256),
    ):
        actual = _strict_file(str(path), digest, field=field)
        _assert_expected_path(actual, path, field=field)
    dependency_root = _strict_directory(
        str(request.python_dependency_root), field="python_dependency_root"
    )
    _reject_network_root(dependency_root, field="python_dependency_root")
    _assert_expected_path(
        dependency_root, request.python_dependency_root, field="python_dependency_root"
    )


def _validate_operator_plan(
    plan: Mapping[str, Any],
    *,
    request: ActivationRunnerRequest,
    generation: str,
) -> None:
    if not (
        plan.get("schema_version") == "chili.captured-paper-operator-plan.v1"
        and plan.get("activation_generation") == generation
        and plan.get("expected_account_id") == request.expected_account_id
        and _same_path(plan.get("candidate_root"), request.candidate_root)
        and _same_path(plan.get("operator_output_root"), request.artifact_root / "operator")
        and _same_path(
            plan.get("preactivation_output_root"),
            request.artifact_root / "preactivation",
        )
        and _same_path(
            plan.get("activation_artifact_root"), request.artifact_root / "activation"
        )
        and _same_path(plan.get("runtime_env_path"), request.runtime_env_path)
        and plan.get("runtime_env_sha256") == request.runtime_env_sha256
        and _same_path(plan.get("python_executable"), request.python_executable)
        and _same_path(
            plan.get("powershell_executable"), request.powershell_executable
        )
        and plan.get("allowed_read_roots") == list(request.allowed_read_roots)
    ):
        raise CapturedPaperActivationRunnerError(
            "PLAN_AUTHORITY_MISMATCH", "operator plan differs from the outer authority"
        )
    capture_root = Path(str(plan.get("capture_store_root") or "")).resolve(strict=False)
    receipt_path = Path(str(plan.get("no_order_receipt_output") or "")).resolve(
        strict=False
    )
    if not (
        capture_root.is_absolute()
        and receipt_path.is_absolute()
        and _inside_directory(capture_root, request.artifact_root)
        and _inside_directory(receipt_path, request.artifact_root)
    ):
        raise CapturedPaperActivationRunnerError(
            "PLAN_AUTHORITY_MISMATCH", "operator write path escaped artifact_root"
        )
    for name in ("task_snapshot", "process_snapshot", "restore_plan"):
        snapshot = _strict_file(
            str(plan.get(f"{name}_path") or ""),
            str(plan.get(f"{name}_sha256") or ""),
            field=name,
        )
        if not _inside_directory(snapshot, request.artifact_root):
            raise CapturedPaperActivationRunnerError(
                "PLAN_AUTHORITY_MISMATCH", f"{name} escaped artifact_root"
            )


def _single_glob(root: Path, pattern: str, *, field: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1 or not matches[0].is_file():
        raise CapturedPaperActivationRunnerError(
            "ARTIFACT_UNAVAILABLE", f"{field} expected exactly one artifact"
        )
    _reject_reparse_chain(matches[0])
    return matches[0].resolve(strict=True)


def _install_paper_environment(
    request: ActivationRunnerRequest, *, pycache_root: Path
) -> dict[str, str]:
    # Import only after the outer request has proven a PAPER-only runtime file.
    from scripts.captured_paper_runtime_env import (
        install_captured_paper_runtime_environment,
    )

    install_captured_paper_runtime_environment(
        request.runtime_env_path,
        expected_env_sha256=request.runtime_env_sha256,
        expected_account_id=request.expected_account_id,
        first_dip_policy_mode="candidate",
    )
    from sqlalchemy.engine import make_url

    database_url = str(os.environ.get("DATABASE_URL") or "")
    parsed = make_url(database_url)
    if not parsed.database:
        raise CapturedPaperActivationRunnerError(
            "DATABASE_URL_INVALID", "protected runtime has no database name"
        )
    test_url = parsed.set(database=request.test_database_name)
    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = test_url.render_as_string(hide_password=False)
    for key in tuple(env):
        if key.upper() in _PYTHON_CONTROL_ENVIRONMENT:
            env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(pycache_root)
    env["PYTHONPATH"] = os.pathsep.join(
        (str(request.candidate_root), str(request.python_dependency_root))
    )
    return env


def run_activation(
    request: ActivationRunnerRequest,
    *,
    mode: str,
    confirmation: str | None,
    executor: Any | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Any]:
    if mode not in {"ValidateOnly", "ActivatePaper"}:
        raise CapturedPaperActivationRunnerError(
            "MODE_INVALID", "mode must be ValidateOnly or ActivatePaper"
        )
    if mode == "ActivatePaper" and confirmation != ACTIVATE_CONFIRMATION:
        raise CapturedPaperActivationRunnerError(
            "ACTIVATION_CONFIRMATION_REQUIRED",
            "fake-money PAPER confirmation is absent",
        )
    if mode == "ValidateOnly" and confirmation is not None:
        raise CapturedPaperActivationRunnerError(
            "ACTIVATION_CONFIRMATION_FORBIDDEN",
            "ValidateOnly must not receive activation confirmation",
        )
    _assert_isolated_interpreter()
    _sanitize_python_control_environment()
    executor = executor or SubprocessExecutor()
    clock = clock or (lambda: datetime.now(UTC))
    # This path is deliberately independent of every caller-supplied artifact
    # root.  Two otherwise-valid generations rooted in different directories
    # must still serialize through one host authority boundary.
    lock_path = _HOST_WIDE_ACTIVATION_LOCK_PATH
    _reject_reparse_chain(lock_path.parent)
    with _HostWideActivationLock(
        lock_path,
        mutex_name=_HOST_WIDE_ACTIVATION_MUTEX_NAME,
    ):
        _revalidate_request_path_authority(request)
        run_id = str(uuid.uuid4())
        run_root = request.artifact_root / "operator-runs" / run_id
        run_root.parent.mkdir(mode=0o700, exist_ok=True)
        bootstrap_root = request.artifact_root / "operator-bootstrap" / run_id
        bootstrap_root.parent.mkdir(mode=0o700, exist_ok=True)
        bootstrap_root.mkdir(mode=0o700, exist_ok=False)
        git_sandbox = bootstrap_root / "git"
        git_sandbox.mkdir(mode=0o700, exist_ok=False)
        git_env = _minimal_git_environment(sandbox=git_sandbox)
        _verify_repo(request, executor, git_env)
        pycache_root = bootstrap_root / "pycache"
        pycache_root.mkdir(mode=0o700, exist_ok=False)
        sys.pycache_prefix = str(pycache_root)
        _install_sealed_dependency_root(request)
        env = _install_paper_environment(request, pycache_root=pycache_root)
        recorder = _StageRecorder(run_root)
        journal_root = request.artifact_root / "cutover-journal"
        recovery_read_roots: list[str] = []
        for root in request.allowed_read_roots:
            recovery_read_roots.extend(("--allow-read-root", root))
        recovery = _run_stage(
            name="recover-only",
            argv=[
                str(request.python_executable),
                "-S",
                "-B",
                str(request.cutover_script),
                "--mode",
                "RecoverOnly",
                "--journal-root",
                str(journal_root),
                *recovery_read_roots,
            ],
            timeout=request.timeouts.rollback,
            request=request,
            executor=executor,
            env=env,
            recorder=recorder,
        )
        recovery_doc = _last_json_line(recovery.stdout)
        if recovery.returncode != 0 or recovery_doc is None:
            raise CapturedPaperActivationRunnerError(
                "RECOVERY_REJECTED", "host cutover recovery did not finish cleanly"
            )
        recovery_verdict = str(recovery_doc.get("verdict") or "")
        if recovery_verdict == "ALREADY_APPLIED_EXACT":
            raise CapturedPaperActivationRunnerError(
                "PAPER_ALREADY_ACTIVE",
                "an exact captured PAPER generation is already active",
            )
        if recovery_verdict in _ROLLBACK_OK:
            raise CapturedPaperActivationRunnerError(
                "RECOVERY_COMPLETED_RERUN_REQUIRED",
                "an interrupted cutover was restored; rerun with fresh evidence",
            )
        if recovery_verdict != _RECOVERY_NONE:
            raise CapturedPaperActivationRunnerError(
                "RECOVERY_RESULT_INVALID", "cutover recovery returned an unknown state"
            )
        _reject_reparse_chain(journal_root)
        try:
            journal_root.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise CapturedPaperActivationRunnerError(
                "PATH_UNAVAILABLE",
                "host cutover journal root could not be prepared",
            ) from exc
        journal_root = _strict_directory(
            str(journal_root), field="host_cutover_journal_root"
        )
        if _paper_task_exists(request, executor, env):
            raise CapturedPaperActivationRunnerError(
                "EXISTING_PAPER_TASK_REQUIRES_RECONCILIATION",
                "candidate PAPER task already exists; refusing a fresh generation",
            )
        chain = _run_stage(
            name="chain",
            argv=[
                str(request.python_executable),
                "-S",
                "-B",
                str(request.chain_script),
                "--request",
                str(request.chain_request_path),
                "--request-sha256",
                request.chain_request_sha256,
                "--activation-request",
                str(request.request_path),
                "--activation-request-sha256",
                request.request_sha256,
            ],
            timeout=request.timeouts.chain,
            request=request,
            executor=executor,
            env=env,
            recorder=recorder,
        )
        chain_doc = _last_json_line(chain.stdout)
        if chain.returncode != 0 or chain_doc is None or chain_doc.get("verdict") != _CHAIN_OK:
            raise CapturedPaperActivationRunnerError(
                "CHAIN_REJECTED", "fresh captured-paper operator chain rejected"
            )
        generation = str(chain_doc.get("activation_generation") or "")
        try:
            generation = str(uuid.UUID(generation))
        except (ValueError, AttributeError) as exc:
            raise CapturedPaperActivationRunnerError(
                "CHAIN_RESULT_INVALID", "chain generation UUID is invalid"
            ) from exc
        plan_match = re.search(r"(?m)^PLAN: ([0-9a-f]{64})$", chain.stdout)
        if plan_match is None:
            raise CapturedPaperActivationRunnerError(
                "CHAIN_RESULT_INVALID", "chain did not publish its plan hash"
            )
        plan_path = request.artifact_root / "operator" / f"{plan_match.group(1)}.plan.json"
        plan_raw = _strict_file(
            str(plan_path), plan_match.group(1), field="operator_plan"
        ).read_bytes()
        plan = _strict_json(plan_raw, field="operator_plan")
        if _canonical_json_bytes(plan) != plan_raw:
            raise CapturedPaperActivationRunnerError(
                "PLAN_NOT_CANONICAL", "operator plan bytes are not canonical JSON"
            )
        _validate_operator_plan(plan, request=request, generation=generation)
        next_command_ref = chain_doc.get("next_command")
        if not isinstance(next_command_ref, dict):
            raise CapturedPaperActivationRunnerError(
                "CHAIN_RESULT_INVALID", "chain next-command reference is absent"
            )
        next_command_path, next_command = _reference_path_and_document(
            next_command_ref, field="next_command"
        )
        next_command_sha = str(next_command_ref.get("sha256") or "")
        _assert_content_addressed_json_path(
            next_command_path,
            root=(request.artifact_root / "operator" / generation / "next-command"),
            sha256=next_command_sha,
            field="next_command",
        )
        preactivation_ref = chain_doc.get("preactivation_manifest")
        if not isinstance(preactivation_ref, dict):
            raise CapturedPaperActivationRunnerError(
                "CHAIN_RESULT_INVALID", "chain preactivation reference is absent"
            )
        preactivation_path, preactivation_document = _reference_path_and_document(
            preactivation_ref, field="preactivation_manifest"
        )
        preactivation_sha = str(preactivation_ref.get("sha256") or "")
        _assert_content_addressed_json_path(
            preactivation_path,
            root=request.artifact_root / "preactivation",
            sha256=preactivation_sha,
            field="preactivation_manifest",
        )
        program, arguments, receipt_path, no_order_projection = (
            _validate_no_order_launcher_authority(
            next_command=next_command,
            preactivation_path=preactivation_path,
            preactivation_sha256=preactivation_sha,
            preactivation=preactivation_document,
            request=request,
            generation=generation,
            )
        )
        receipt_resolved = receipt_path.resolve(strict=False)
        _reject_reparse_chain(receipt_resolved.parent)
        if (
            not _inside_directory(receipt_resolved, request.artifact_root)
            or os.path.lexists(receipt_path)
        ):
            raise CapturedPaperActivationRunnerError(
                "NO_ORDER_RECEIPT_REPLAY", "no-order receipt path is unsafe or preexisting"
            )
        no_order = _run_stage(
            name="no-order-smoke",
            argv=[program, *arguments],
            timeout=request.timeouts.no_order_smoke,
            request=request,
            executor=executor,
            env=env,
            recorder=recorder,
            prelaunch_validator=lambda: _revalidate_staged_no_order_paths(
                no_order_projection
            ),
        )
        if no_order.returncode != 0 or not receipt_path.is_file():
            raise CapturedPaperActivationRunnerError(
                "NO_ORDER_SMOKE_REJECTED", "zero-POST smoke did not publish a receipt"
            )
        _reject_reparse_chain(receipt_path)
        receipt_sha = _sha256_file(receipt_path)
        receipt_raw = receipt_path.read_bytes()
        receipt_document = _strict_json(receipt_raw, field="no_order_receipt")
        if _canonical_json_bytes(receipt_document) != receipt_raw:
            raise CapturedPaperActivationRunnerError(
                "NO_ORDER_RECEIPT_INVALID", "no-order receipt is not canonical JSON"
            )
        read_root_args: list[str] = []
        for root in request.allowed_read_roots:
            read_root_args.extend(("--allow-read-root", root))
        finalizer = _run_stage(
            name="finalize",
            argv=[
                str(request.python_executable),
                "-S",
                "-B",
                str(request.finalizer_script),
                "--preactivation",
                str(preactivation_ref["path"]),
                "--preactivation-sha256",
                str(preactivation_ref["sha256"]),
                "--candidate-root",
                str(request.candidate_root),
                "--no-order-receipt",
                str(receipt_path),
                "--no-order-receipt-sha256",
                receipt_sha,
                "--output-root",
                str(request.artifact_root / "activation"),
                *read_root_args,
            ],
            timeout=request.timeouts.finalize,
            request=request,
            executor=executor,
            env=env,
            recorder=recorder,
        )
        final_doc = _last_json_line(finalizer.stdout)
        if finalizer.returncode != 0 or final_doc is None or final_doc.get("verdict") != _FINAL_OK:
            raise CapturedPaperActivationRunnerError(
                "FINALIZE_REJECTED", "final activation manifest was not published"
            )
        manifest_path = str(final_doc.get("manifest_path") or "")
        manifest_sha = str(final_doc.get("manifest_sha256") or "")
        manifest_file = _strict_file(
            manifest_path, manifest_sha, field="activation_manifest"
        )
        _assert_content_addressed_json_path(
            manifest_file,
            root=request.artifact_root / "activation",
            sha256=manifest_sha,
            field="activation_manifest",
        )
        generation_root = request.artifact_root / "operator" / generation
        template = _single_glob(
            generation_root, "candidate-task-template/**/*.xml", field="candidate_task_template"
        )
        action = _single_glob(
            generation_root, "candidate-action/**/*.json", field="candidate_action"
        )
        cutover_common = [
            str(request.python_executable),
            "-S",
            "-B",
            str(request.cutover_script),
            "--manifest",
            manifest_path,
            "--manifest-sha256",
            manifest_sha,
            "--candidate-root",
            str(request.candidate_root),
            *read_root_args,
            "--task-snapshot",
            str(plan["task_snapshot_path"]),
            "--process-snapshot",
            str(plan["process_snapshot_path"]),
            "--restore-plan",
            str(plan["restore_plan_path"]),
            "--candidate-task-template",
            str(template),
            "--candidate-action",
            str(action),
            "--journal-root",
            str(journal_root),
        ]
        validate = _run_stage(
            name="validate-only",
            argv=[*cutover_common, "--mode", "ValidateOnly"],
            timeout=request.timeouts.validate_only,
            request=request,
            executor=executor,
            env=env,
            recorder=recorder,
        )
        validate_doc = _last_json_line(validate.stdout)
        if validate.returncode != 0 or validate_doc is None or validate_doc.get("verdict") != _VALIDATE_OK:
            raise CapturedPaperActivationRunnerError(
                "VALIDATE_ONLY_REJECTED", "real host ValidateOnly rejected"
            )
        base_result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "account_scope": ACCOUNT_SCOPE,
            "activation_generation": generation,
            "manifest_sha256": manifest_sha,
            "request_sha256": request.request_sha256,
            "expected_git_commit": request.expected_git_commit,
            "live_cash_authorized": False,
            "generated_at": clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        if mode == "ValidateOnly":
            result = {**base_result, "verdict": "VALIDATED_NO_HOST_MUTATION", "paper_started": False}
            _write_once(run_root / "result.json", _canonical_json_bytes(result))
            return result

        apply_started = False
        try:
            apply_started = True
            apply_result = _run_stage(
                name="apply",
                argv=[
                    *cutover_common,
                    "--mode",
                    "Apply",
                    "--confirm-fake-money-paper",
                    ACTIVATE_CONFIRMATION,
                ],
                timeout=request.timeouts.apply,
                request=request,
                executor=executor,
                env=env,
                recorder=recorder,
            )
            apply_doc = _last_json_line(apply_result.stdout)
            if (
                apply_result.returncode != 0
                or apply_doc is None
                or apply_doc.get("verdict") not in _APPLY_OK
            ):
                raise CapturedPaperActivationRunnerError(
                    "APPLY_REJECTED", "fake-money PAPER Apply rejected"
                )
            handshake = request.artifact_root / "activation" / generation / "handshake"
            started = sorted(handshake.glob("*.started.json")) if handshake.is_dir() else []
            if len(started) != 1:
                raise CapturedPaperActivationRunnerError(
                    "STARTED_RECEIPT_UNAVAILABLE",
                    "Apply did not leave exactly one STARTED receipt",
                )
            if not _paper_task_exists(request, executor, env):
                raise CapturedPaperActivationRunnerError(
                    "PAPER_TASK_UNAVAILABLE", "candidate PAPER task is absent after Apply"
                )
            result = {
                **base_result,
                "verdict": "ACTIVATED_ALPACA_PAPER_ONLY",
                "paper_started": True,
            }
            # Persistence of the outer success receipt is part of Apply.  A
            # write/fsync failure must compensate the already-started task.
            _write_once(run_root / "result.json", _canonical_json_bytes(result))
            return result
        except BaseException as primary:
            rollback_error: BaseException | None = None
            if apply_started:
                try:
                    rollback = _run_stage(
                        name="rollback",
                        argv=[*cutover_common, "--mode", "Rollback"],
                        timeout=request.timeouts.rollback,
                        request=request,
                        executor=executor,
                        env=env,
                        recorder=recorder,
                    )
                    rollback_doc = _last_json_line(rollback.stdout)
                    if (
                        rollback.returncode != 0
                        or rollback_doc is None
                        or rollback_doc.get("verdict") not in _ROLLBACK_OK
                    ):
                        rollback_error = CapturedPaperActivationRunnerError(
                            "ROLLBACK_REJECTED", "compensating rollback rejected"
                        )
                except BaseException as exc:  # preserve both failures for the operator
                    rollback_error = exc
            if rollback_error is not None:
                raise CapturedPaperActivationRunnerError(
                    "APPLY_AND_ROLLBACK_FAILED",
                    f"primary={type(primary).__name__}; rollback={type(rollback_error).__name__}",
                ) from primary
            raise
        raise AssertionError("compensated activation boundary returned unexpectedly")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--mode", choices=("ValidateOnly", "ActivatePaper"), required=True)
    parser.add_argument("--confirm-fake-money-paper")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = load_activation_runner_request(
            request_path=args.request,
            request_sha256=args.request_sha256,
        )
        result = run_activation(
            request,
            mode=args.mode,
            confirmation=args.confirm_fake_money_paper,
        )
    except CapturedPaperActivationRunnerError as exc:
        print(
            json.dumps(
                {
                    "schema_version": RESULT_SCHEMA_VERSION,
                    "verdict": "REJECTED",
                    "reason_code": exc.code,
                    "account_scope": ACCOUNT_SCOPE,
                    "live_cash_authorized": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        # Never leak credentials, provider payloads, or local command lines
        # from an unexpected dependency exception at the activation boundary.
        print(
            json.dumps(
                {
                    "schema_version": RESULT_SCHEMA_VERSION,
                    "verdict": "REJECTED",
                    "reason_code": "UNEXPECTED_ACTIVATION_FAILURE",
                    "account_scope": ACCOUNT_SCOPE,
                    "live_cash_authorized": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(_canonical_json_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACCOUNT_SCOPE",
    "ACTIVATE_CONFIRMATION",
    "ActivationRunnerRequest",
    "CapturedPaperActivationRunnerError",
    "CommandResult",
    "PAPER_TASK_NAME",
    "REQUEST_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "RunnerTimeouts",
    "SubprocessExecutor",
    "load_activation_runner_request",
    "main",
    "run_activation",
]
