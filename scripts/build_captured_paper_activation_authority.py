"""Build the two canonical authorities for fake-money captured PAPER.

This builder is deliberately standard-library-only and import-inert.  It does
not read a database, contact a provider or broker, inspect Task Scheduler, or
start/stop a process.  Its only writes are create-new, content-addressed JSON
files beneath an already-created, empty local artifact directory.

The builder first proves the exact clean Git worktree and immutable local
inputs, then publishes the inner operator-chain request followed by the outer
activation-runner request.  It finally passes those exact bytes through the
real loaders and rechecks Git and every pinned byte before emitting a
non-secret receipt containing argv arrays (never a shell command string).
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import getpass
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
from types import ModuleType
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit
import uuid


CHAIN_SCHEMA_VERSION = "chili.captured-paper-operator-chain-request.v1"
REQUEST_SCHEMA_VERSION = "chili.captured-paper-activation-runner-request.v3"
RECEIPT_SCHEMA_VERSION = "chili.captured-paper-activation-authority-receipt.v1"
ACCOUNT_SCOPE = "alpaca:paper"
PAPER_TASK_NAME = "CHILI-Captured-Alpaca-PAPER"
ACTIVATE_CONFIRMATION = "CUTOVER_FAKE_MONEY_ALPACA_PAPER"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TEST_DATABASE_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}_test$")
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_MAX_JSON_BYTES = 1024 * 1024
_MAX_RUNTIME_ENV_BYTES = 16 * 1024 * 1024
_MAX_BENCHMARK_BYTES = 64 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
_MAX_SOURCE_BYTES = 64 * 1024 * 1024

_CHAIN_KEYS = frozenset(
    {
        "schema_version",
        "account_scope",
        "live_cash_authorized",
        "resource_benchmark",
        "legacy_root",
        "python_dependency_root",
        "python_dependency_root_identity_sha256",
        "bootstrap_stage0_script",
        "bootstrap_stage0_script_sha256",
        "host_principal_user_id",
        "bridge_configuration",
    }
)
_REQUEST_KEYS = frozenset(
    {
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
)
_TIMEOUT_KEYS = frozenset(
    {
        "chain",
        "no_order_smoke",
        "finalize",
        "validate_only",
        "apply",
        "rollback",
        "task_query",
    }
)
_DEFAULT_TIMEOUTS: Mapping[str, int] = {
    "chain": 1800,
    "no_order_smoke": 600,
    "finalize": 600,
    "validate_only": 600,
    "apply": 900,
    "rollback": 600,
    "task_query": 60,
}
_CANDIDATE_ENTRYPOINTS: Mapping[str, Path] = {
    "bootstrap_stage0_script": Path("scripts/captured_paper_isolated_stage0.py"),
    "chain_script": Path("scripts/run_captured_paper_operator_chain.py"),
    "finalizer_script": Path("scripts/finalize_captured_paper_activation.py"),
    "cutover_script": Path("scripts/captured_paper_host_cutover.py"),
}
_ACTIVATION_RUNNER = Path("scripts/captured_paper_activation_runner.py")
_CRITICAL_TRACKED = frozenset(
    {
        *(path.as_posix() for path in _CANDIDATE_ENTRYPOINTS.values()),
        "scripts/start-captured-alpaca-paper.ps1",
        "scripts/captured_alpaca_paper_service.py",
        "scripts/captured_paper_activation_runner.py",
        "scripts/captured_paper_runtime_env.py",
        "scripts/build_captured_paper_activation_authority.py",
    }
)
_DANGEROUS_IGNORED_SUFFIXES = frozenset(
    {".bat", ".cmd", ".com", ".dll", ".exe", ".pyd", ".ps1", ".py", ".pth", ".scr", ".sh", ".so"}
)
_DANGEROUS_IGNORED_NAMES = frozenset(
    {"pyvenv.cfg", "sitecustomize.py", "usercustomize.py"}
)
_MAX_PROJECTION_DEPTH = 16
_MAX_PROJECTION_ITEMS = 10_000
_MAX_PROJECTION_STRING_BYTES = 64 * 1024
_FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "authorization",
        "auth",
        "accesskey",
        "accesstoken",
        "apikey",
        "apisecret",
        "cookie",
        "credential",
        "credentials",
        "headers",
        "password",
        "passwd",
        "privatekey",
        "databaseurl",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_FORBIDDEN_KEY_PAIRS = frozenset(
    {
        ("access", "key"),
        ("api", "key"),
        ("api", "secret"),
        ("database", "url"),
        ("http", "headers"),
        ("private", "key"),
        ("request", "headers"),
    }
)


class CapturedPaperActivationAuthorityError(RuntimeError):
    """Stable, sanitized builder rejection."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True, slots=True)
class BuiltCapturedPaperActivationAuthority:
    chain_request_path: Path
    chain_request_sha256: str
    activation_request_path: Path
    activation_request_sha256: str
    receipt_path: Path
    receipt_sha256: str
    validate_only_argv: tuple[str, ...]
    activate_paper_argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PinnedFile:
    path: Path
    sha256: str
    identity: tuple[int, int, int, int, int, int]
    max_bytes: int


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
        raise CapturedPaperActivationAuthorityError(
            "JSON_NOT_CANONICAL", "authority input is not canonical JSON"
        ) from exc


def _strict_json(raw: bytes, *, field: str) -> Mapping[str, Any]:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise CapturedPaperActivationAuthorityError(
                    "JSON_DUPLICATE_KEY", f"{field} contains a duplicate key"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except CapturedPaperActivationAuthorityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CapturedPaperActivationAuthorityError(
            "JSON_INVALID", f"{field} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise CapturedPaperActivationAuthorityError(
            "JSON_INVALID", f"{field} must be an object"
        )
    return value


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _same_path(left: Path | str, right: Path | str) -> bool:
    return _path_key(Path(left).resolve(strict=False)) == _path_key(
        Path(right).resolve(strict=False)
    )


def _has_canonical_spelling(path: Path, resolved: Path) -> bool:
    lexical = Path(os.path.abspath(os.fspath(path)))
    return _path_key(lexical) == _path_key(resolved)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_nonlocal_spelling(path: Path, *, field: str) -> None:
    raw = os.fspath(path)
    normalized = raw.replace("/", "\\")
    if normalized.startswith(("\\\\", "\\?\\", "\\.\\")):
        raise CapturedPaperActivationAuthorityError(
            "NONLOCAL_PATH", f"{field} must be a local filesystem path"
        )
    drive, tail = os.path.splitdrive(raw)
    if not drive or not path.is_absolute():
        raise CapturedPaperActivationAuthorityError(
            "PATH_INVALID", f"{field} must be absolute"
        )
    if ":" in tail:
        raise CapturedPaperActivationAuthorityError(
            "ADS_PATH_FORBIDDEN", f"{field} may not use an alternate data stream"
        )


def _reject_network_drive(path: Path, *, field: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(str(Path(path.anchor))))
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperActivationAuthorityError(
            "DRIVE_AUTHORITY_UNAVAILABLE", f"{field} drive type is unavailable"
        ) from exc
    if drive_type == 4:
        raise CapturedPaperActivationAuthorityError(
            "NETWORK_PATH_FORBIDDEN", f"{field} may not use a mapped drive"
        )
    if drive_type not in {2, 3, 6}:
        raise CapturedPaperActivationAuthorityError(
            "LOCAL_DRIVE_UNPROVEN", f"{field} is not on a proven local drive"
        )


def _reject_reparse_chain(path: Path, *, field: str) -> None:
    lexical = Path(os.path.abspath(os.fspath(path)))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current = current / part
        if not os.path.lexists(current):
            continue
        metadata = os.lstat(current)
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if stat.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400):
            raise CapturedPaperActivationAuthorityError(
                "REPARSE_PATH_FORBIDDEN", f"{field} traverses a reparse point"
            )


def _canonical_directory(value: str | Path, *, field: str) -> Path:
    path = Path(value)
    _reject_nonlocal_spelling(path, field=field)
    if ".." in path.parts:
        raise CapturedPaperActivationAuthorityError(
            "PATH_NOT_CANONICAL", f"{field} may not contain parent traversal"
        )
    _reject_network_drive(path, field=field)
    _reject_reparse_chain(path, field=field)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapturedPaperActivationAuthorityError(
            "PATH_UNAVAILABLE", f"{field} is unavailable"
        ) from exc
    if not _has_canonical_spelling(path, resolved) or not resolved.is_dir():
        raise CapturedPaperActivationAuthorityError(
            "PATH_NOT_CANONICAL", f"{field} must be an exact canonical directory"
        )
    if resolved.parent == resolved:
        raise CapturedPaperActivationAuthorityError(
            "BROAD_ROOT_FORBIDDEN", f"{field} may not be a drive root"
        )
    _reject_reparse_chain(resolved, field=field)
    return resolved


def _canonical_file(value: str | Path, *, field: str) -> Path:
    path = Path(value)
    _reject_nonlocal_spelling(path, field=field)
    if ".." in path.parts:
        raise CapturedPaperActivationAuthorityError(
            "PATH_NOT_CANONICAL", f"{field} may not contain parent traversal"
        )
    _reject_network_drive(path, field=field)
    _reject_reparse_chain(path, field=field)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapturedPaperActivationAuthorityError(
            "PATH_UNAVAILABLE", f"{field} is unavailable"
        ) from exc
    if not _has_canonical_spelling(path, resolved) or not resolved.is_file():
        raise CapturedPaperActivationAuthorityError(
            "PATH_NOT_CANONICAL", f"{field} must be an exact canonical file"
        )
    _reject_reparse_chain(resolved, field=field)
    return resolved


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_mode),
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _pin_file(path: Path, *, field: str, max_bytes: int) -> _PinnedFile:
    _reject_reparse_chain(path, field=field)
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise CapturedPaperActivationAuthorityError(
            "FILE_INVALID", f"{field} is not a bounded regular file"
        )
    first = path.read_bytes()
    middle = os.stat(path, follow_symlinks=False)
    second = path.read_bytes()
    after = os.stat(path, follow_symlinks=False)
    if (
        _file_identity(before) != _file_identity(middle)
        or _file_identity(middle) != _file_identity(after)
        or first != second
    ):
        raise CapturedPaperActivationAuthorityError(
            "FILE_DRIFT", f"{field} changed during stable read"
        )
    return _PinnedFile(
        path=path,
        sha256=_sha256_bytes(first),
        identity=_file_identity(after),
        max_bytes=max_bytes,
    )


def _recheck_pin(pin: _PinnedFile, *, field: str) -> None:
    current = _pin_file(pin.path, field=field, max_bytes=pin.max_bytes)
    if current.identity != pin.identity or current.sha256 != pin.sha256:
        raise CapturedPaperActivationAuthorityError(
            "FILE_DRIFT", f"{field} changed after authority construction"
        )


def _read_pinned_bytes(pin: _PinnedFile, *, field: str) -> bytes:
    before = os.stat(pin.path, follow_symlinks=False)
    raw = pin.path.read_bytes()
    after = os.stat(pin.path, follow_symlinks=False)
    if (
        _file_identity(before) != pin.identity
        or _file_identity(after) != pin.identity
        or _sha256_bytes(raw) != pin.sha256
    ):
        raise CapturedPaperActivationAuthorityError(
            "FILE_DRIFT", f"{field} differs from its stable-read pin"
        )
    return raw


def _native_system32_executable(basename: str) -> Path:
    if os.name != "nt":
        candidate = shutil.which("pwsh" if basename == "powershell.exe" else basename)
        if not candidate and basename == "powershell.exe":
            candidate = shutil.which("powershell")
        if not candidate:
            raise CapturedPaperActivationAuthorityError(
                "WINDOWS_EXECUTABLE_AUTHORITY_UNAVAILABLE",
                "native activation executable is unavailable",
            )
        return _canonical_file(candidate, field=basename)
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
            raise OSError("ambiguous native process architecture")
        buffer = ctypes.create_unicode_buffer(32768)
        length = int(kernel32.GetSystemDirectoryW(buffer, len(buffer)))
        if length <= 0 or length >= len(buffer):
            raise OSError("GetSystemDirectoryW failed")
        system32 = Path(buffer.value)
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperActivationAuthorityError(
            "WINDOWS_EXECUTABLE_AUTHORITY_UNAVAILABLE",
            "native activation executable authority is unavailable",
        ) from exc
    path = (
        system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if basename == "powershell.exe"
        else system32 / basename
    )
    return _canonical_file(path, field=basename)


def _authoritative_executables() -> Mapping[str, Path]:
    git_name = "git.exe" if os.name == "nt" else "git"
    git = shutil.which(git_name)
    if not git:
        raise CapturedPaperActivationAuthorityError(
            "GIT_AUTHORITY_UNAVAILABLE", "Git executable cannot be resolved"
        )
    return {
        "git_executable": _canonical_file(git, field="git_executable"),
        "python_executable": _canonical_file(
            sys.executable, field="python_executable"
        ),
        "powershell_executable": _native_system32_executable("powershell.exe"),
        "schtasks_executable": _native_system32_executable("schtasks.exe"),
    }


def _minimal_git_environment(sandbox: Path) -> Mapping[str, str]:
    safe = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in safe}
    hooks = sandbox / "empty-hooks"
    hooks.mkdir(mode=0o700, parents=False, exist_ok=False)
    null_device = "NUL" if os.name == "nt" else "/dev/null"
    env.update(
        {
            "GIT_CONFIG_GLOBAL": null_device,
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
    return env


def _run_git(
    git: Path,
    candidate_root: Path,
    arguments: Sequence[str],
    *,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [str(git), *map(str, arguments)],
            cwd=candidate_root,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapturedPaperActivationAuthorityError(
            "GIT_PROBE_FAILED", "sanitized Git probe failed"
        ) from exc
    if len(result.stdout) + len(result.stderr) > 16 * 1024 * 1024:
        raise CapturedPaperActivationAuthorityError(
            "GIT_OUTPUT_OVERSIZED", "sanitized Git probe output is oversized"
        )
    return result


def _ignored_payload_is_dangerous(relative: str) -> bool:
    path = Path(str(relative or "").replace("\\", "/"))
    if (
        path.name.casefold() in _DANGEROUS_IGNORED_NAMES
        or path.suffix.casefold() in _DANGEROUS_IGNORED_SUFFIXES
    ):
        return True
    return path.suffix.casefold() == ".pyc" and "__pycache__" not in {
        part.casefold() for part in path.parts
    }


def _verify_git_worktree(
    *,
    git: Path,
    candidate_root: Path,
    expected_commit: str | None = None,
) -> str:
    with tempfile.TemporaryDirectory(prefix="chili-paper-authority-git-") as raw:
        sandbox = Path(raw)
        env = _minimal_git_environment(sandbox)
        top = _run_git(git, candidate_root, ("rev-parse", "--show-toplevel"), env=env)
        if top.returncode != 0 or not _same_path(top.stdout.strip(), candidate_root):
            raise CapturedPaperActivationAuthorityError(
                "GIT_ROOT_MISMATCH", "candidate_root is not the exact Git root"
            )
        head = _run_git(git, candidate_root, ("rev-parse", "HEAD"), env=env)
        commit = head.stdout.strip()
        if head.returncode != 0 or _GIT_COMMIT_RE.fullmatch(commit) is None:
            raise CapturedPaperActivationAuthorityError(
                "GIT_HEAD_INVALID", "candidate Git HEAD is not a full commit"
            )
        if expected_commit is not None and commit != expected_commit:
            raise CapturedPaperActivationAuthorityError(
                "GIT_HEAD_DRIFT", "candidate Git HEAD changed during construction"
            )
        status_result = _run_git(
            git,
            candidate_root,
            (
                "status",
                "--porcelain=v2",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ),
            env=env,
        )
        if status_result.returncode != 0 or status_result.stdout.strip():
            raise CapturedPaperActivationAuthorityError(
                "WORKTREE_DIRTY", "candidate worktree is not exactly clean"
            )
        tracked_result = _run_git(
            git,
            candidate_root,
            ("ls-files", "--error-unmatch", "-z", "--", *sorted(_CRITICAL_TRACKED)),
            env=env,
        )
        observed = {item for item in tracked_result.stdout.split("\0") if item}
        if tracked_result.returncode != 0 or observed != set(_CRITICAL_TRACKED):
            raise CapturedPaperActivationAuthorityError(
                "CRITICAL_PATH_UNTRACKED",
                "activation authority code is not tracked exactly",
            )
        ignored_result = _run_git(
            git,
            candidate_root,
            ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
            env=env,
        )
        if ignored_result.returncode != 0:
            raise CapturedPaperActivationAuthorityError(
                "IGNORED_INVENTORY_UNAVAILABLE",
                "ignored payload inventory is unavailable",
            )
        if any(
            _ignored_payload_is_dangerous(item)
            for item in ignored_result.stdout.split("\0")
            if item
        ):
            raise CapturedPaperActivationAuthorityError(
                "IGNORED_EXECUTABLE_PAYLOAD",
                "candidate contains ignored executable or importable payloads",
            )
        return commit


def _validate_bridge_configuration(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"iqfeed_l1", "iqfeed_l2"}:
        raise CapturedPaperActivationAuthorityError(
            "BRIDGE_CONFIGURATION_INVALID",
            "bridge configuration must contain exact IQFeed L1/L2 lanes",
        )

    count = [0]

    def key_tokens(value: str) -> tuple[str, ...]:
        expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
        return tuple(part.lower() for part in re.findall(r"[A-Za-z0-9]+", expanded))

    def credential_key(value: str) -> bool:
        tokens = key_tokens(value)
        if not tokens or any(token in _FORBIDDEN_KEY_TOKENS for token in tokens):
            return True
        return any(pair in _FORBIDDEN_KEY_PAIRS for pair in zip(tokens, tokens[1:]))

    def inspect_string(value: str) -> None:
        if "\x00" in value or len(value.encode("utf-8")) > _MAX_PROJECTION_STRING_BYTES:
            raise CapturedPaperActivationAuthorityError(
                "BRIDGE_CONFIGURATION_INVALID",
                "bridge configuration contains an invalid or oversized string",
            )
        lowered = value.strip().lower()
        if lowered.startswith(("bearer ", "basic ", "-----begin private key")):
            raise CapturedPaperActivationAuthorityError(
                "SECRET_INPUT_FORBIDDEN",
                "bridge configuration may not carry credential material",
            )
        if "://" not in value and not value.startswith("//"):
            return
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise CapturedPaperActivationAuthorityError(
                "SECRET_INPUT_FORBIDDEN",
                "bridge configuration may not carry URL user information",
            )
        if any(credential_key(key) for key, _item in parse_qsl(parsed.query, keep_blank_values=True)):
            raise CapturedPaperActivationAuthorityError(
                "SECRET_INPUT_FORBIDDEN",
                "bridge configuration may not carry credential URL parameters",
            )

    def inspect(item: Any, *, depth: int) -> Any:
        count[0] += 1
        if depth > _MAX_PROJECTION_DEPTH or count[0] > _MAX_PROJECTION_ITEMS:
            raise CapturedPaperActivationAuthorityError(
                "BRIDGE_CONFIGURATION_INVALID",
                "bridge configuration exceeds bounded projection limits",
            )
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise CapturedPaperActivationAuthorityError(
                        "BRIDGE_CONFIGURATION_INVALID",
                        "bridge configuration keys must be strings",
                    )
                normalized = key.strip()
                if not normalized or normalized in result or credential_key(normalized):
                    raise CapturedPaperActivationAuthorityError(
                        "SECRET_INPUT_FORBIDDEN",
                        "bridge configuration may not carry secret material",
                    )
                result[normalized] = inspect(child, depth=depth + 1)
            return result
        if isinstance(item, list):
            return [inspect(child, depth=depth + 1) for child in item]
        if isinstance(item, str):
            inspect_string(item)
            return item
        if item is None or isinstance(item, (bool, int, float)):
            if isinstance(item, float) and not (float("-inf") < item < float("inf")):
                raise CapturedPaperActivationAuthorityError(
                    "BRIDGE_CONFIGURATION_INVALID",
                    "bridge configuration contains a non-finite number",
                )
            return item
        raise CapturedPaperActivationAuthorityError(
            "BRIDGE_CONFIGURATION_INVALID",
            "bridge configuration contains an unsupported value",
        )

    projected = inspect(value, depth=0)
    raw = _canonical_json_bytes(projected)
    if len(raw) > _MAX_JSON_BYTES:
        raise CapturedPaperActivationAuthorityError(
            "BRIDGE_CONFIGURATION_OVERSIZED", "bridge configuration is oversized"
        )
    return _strict_json(raw, field="bridge_configuration")


def _validate_timeouts(value: Mapping[str, int] | None) -> Mapping[str, int]:
    parsed = dict(_DEFAULT_TIMEOUTS if value is None else value)
    if set(parsed) != set(_TIMEOUT_KEYS):
        raise CapturedPaperActivationAuthorityError(
            "TIMEOUTS_INVALID", "timeouts must use the exact activation fields"
        )
    for field, number in parsed.items():
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 3600:
            raise CapturedPaperActivationAuthorityError(
                "TIMEOUTS_INVALID", f"timeout {field} must be 1..3600 seconds"
            )
    return parsed


def _reject_security_domain_overlaps(domains: Mapping[str, Path]) -> None:
    rows = tuple(domains.items())
    for index, (left_name, left) in enumerate(rows):
        for right_name, right in rows[index + 1 :]:
            if _inside(left, right) or _inside(right, left):
                raise CapturedPaperActivationAuthorityError(
                    "SECURITY_ROOT_OVERLAP",
                    f"{left_name} and {right_name} must be disjoint",
                )


def _minimal_allowed_roots(paths: Sequence[Path]) -> tuple[Path, ...]:
    unique = sorted({_path_key(path): path for path in paths}.values(), key=_path_key)
    minimal: list[Path] = []
    for candidate in unique:
        if candidate.parent == candidate:
            raise CapturedPaperActivationAuthorityError(
                "BROAD_ROOT_FORBIDDEN", "derived read authority reached a drive root"
            )
        if any(_inside(candidate, other) for other in unique if other != candidate):
            continue
        minimal.append(candidate)
    for index, left in enumerate(minimal):
        for right in minimal[index + 1 :]:
            if _inside(left, right) or _inside(right, left):
                raise CapturedPaperActivationAuthorityError(
                    "READ_ROOT_OVERLAP", "derived read roots overlap"
                )
    return tuple(sorted(minimal, key=_path_key))


def _load_exact_module(pin: _PinnedFile, *, role: str) -> ModuleType:
    path = pin.path
    name = f"_chili_activation_authority_{role}_{hashlib.sha256(str(path).encode()).hexdigest()[:16]}"
    # Compile the already-pinned source bytes directly.  SourceFileLoader may
    # create a candidate-tree ``__pycache__`` between the two clean-Git
    # probes, which would make the builder itself perturb the authority it is
    # trying to prove.
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    try:
        source = _read_pinned_bytes(pin, field=role)
        exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)
    except Exception as exc:
        raise CapturedPaperActivationAuthorityError(
            "LOADER_IMPORT_REJECTED", f"real {role} loader import failed"
        ) from exc
    return module


def _unload_exact_module(module: ModuleType) -> None:
    sys.modules.pop(module.__name__, None)


def _dependency_identity_sha256(
    *, stage0_pin: _PinnedFile, dependency_root: Path, python_pin: _PinnedFile
) -> str:
    module = _load_exact_module(stage0_pin, role="isolated_stage0")
    try:
        value = module.dependency_root_identity_sha256(
            dependency_root=dependency_root,
            python_executable=python_pin.path,
            python_executable_sha256=python_pin.sha256,
        )
    except Exception as exc:
        raise CapturedPaperActivationAuthorityError(
            "DEPENDENCY_IDENTITY_REJECTED",
            "sealed Python dependency root identity could not be proven",
        ) from exc
    finally:
        _unload_exact_module(module)
    digest = str(value or "")
    if _SHA256_RE.fullmatch(digest) is None:
        raise CapturedPaperActivationAuthorityError(
            "DEPENDENCY_IDENTITY_REJECTED", "dependency identity is malformed"
        )
    return digest


def _fsync_directory(path: Path) -> None:
    """Durably flush directory metadata on the supported local filesystem."""

    if os.name != "nt":
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise CapturedPaperActivationAuthorityError(
                "DIRECTORY_DURABILITY_FAILED", "artifact directory could not be flushed"
            ) from exc
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x40000000,  # GENERIC_WRITE, required for FlushFileBuffers(directory)
            0x00000007,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            raise OSError(ctypes.get_last_error(), "CreateFileW(directory) failed")
        try:
            if not kernel32.FlushFileBuffers(handle):
                raise OSError(ctypes.get_last_error(), "FlushFileBuffers failed")
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperActivationAuthorityError(
            "DIRECTORY_DURABILITY_FAILED", "artifact directory could not be flushed"
        ) from exc


def _ensure_private_directory(path: Path, *, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        child = current / part
        try:
            child.mkdir(mode=0o700, exist_ok=False)
            _fsync_directory(current)
        except FileExistsError:
            if not child.is_dir():
                raise CapturedPaperActivationAuthorityError(
                    "ARTIFACT_PATH_CONFLICT", "artifact parent is not a directory"
                )
        _reject_reparse_chain(child, field="artifact_directory")
        try:
            child.chmod(0o700)
        except OSError as exc:
            raise CapturedPaperActivationAuthorityError(
                "ARTIFACT_PERMISSION_FAILED", "artifact directory cannot be private"
            ) from exc
        current = child


def _publish_new_json(root: Path, *, kind: str, raw: bytes) -> tuple[Path, str]:
    digest = _sha256_bytes(raw)
    parent = root / "authority" / kind / digest[:2]
    _ensure_private_directory(parent, root=root)
    _reject_reparse_chain(parent, field=f"{kind}_artifact_parent")
    target = parent / f"{digest}.json"
    descriptor = -1
    staging: Path | None = None
    linked = False
    try:
        descriptor, raw_staging = tempfile.mkstemp(
            prefix=".pending-", suffix=".json", dir=parent
        )
        staging = Path(raw_staging)
        os.chmod(staging, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard-link publish is atomic and fails if the content address already
        # exists; unlike replace/rename it can never overwrite prior authority.
        os.link(staging, target)
        linked = True
        _fsync_directory(parent)
        staging.unlink()
        staging = None
        _fsync_directory(parent)
    except FileExistsError as exc:
        raise CapturedPaperActivationAuthorityError(
            "APPEND_ONLY_CONFLICT", f"{kind} content address already exists"
        ) from exc
    except BaseException as exc:
        if linked and os.path.lexists(target):
            try:
                target.unlink()
                _fsync_directory(parent)
            except (OSError, CapturedPaperActivationAuthorityError):
                pass
        if isinstance(exc, CapturedPaperActivationAuthorityError):
            raise
        raise CapturedPaperActivationAuthorityError(
            "PUBLISH_FAILED", f"{kind} could not be durably published"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if staging is not None and os.path.lexists(staging):
            try:
                staging.unlink()
                _fsync_directory(parent)
            except (OSError, CapturedPaperActivationAuthorityError):
                # A private complete staging file is not authority.  Never
                # mask a prior failure or misrepresent it as the final digest.
                pass
    observed = target.read_bytes()
    if observed != raw or _sha256_bytes(observed) != digest:
        try:
            target.unlink()
            _fsync_directory(parent)
        except (OSError, CapturedPaperActivationAuthorityError):
            pass
        raise CapturedPaperActivationAuthorityError(
            "PUBLISH_VERIFY_FAILED", f"{kind} did not reread exactly"
        )
    return target.resolve(strict=True), digest


def _load_exact_chain_contract(
    pin: _PinnedFile, *, activation_runner: ModuleType
) -> ModuleType:
    """Compile the real loader AST without importing the application graph.

    The operator-chain module is import-inert, but importing it normally still
    resolves its many production dependencies through ambient ``site`` and
    existing bytecode.  This builder needs only its exact schema and loader.
    Extracting those definitions from the pinned source preserves the real
    loader body while making ambient packages and ignored ``.pyc`` unreachable.
    """

    path = pin.path
    try:
        tree = ast.parse(
            _read_pinned_bytes(pin, field="operator_chain"), filename=str(path)
        )
    except (SyntaxError, ValueError) as exc:
        raise CapturedPaperActivationAuthorityError(
            "CHAIN_LOADER_IMPORT_REJECTED", "operator-chain source cannot be parsed"
        ) from exc
    assignments = {
        "CHAIN_REQUEST_SCHEMA_VERSION",
        "ACCOUNT_SCOPE",
        "_SHA256_RE",
        "_MAX_REQUEST_BYTES",
        "_CHAIN_KEYS",
    }
    functions = {
        "_canonical_json_bytes",
        "_strict_json",
        "_sha256_bytes",
        "_load_chain_request",
    }
    selected: list[ast.stmt] = [
        ast.ImportFrom(module="__future__", names=[ast.alias("annotations")], level=0)
    ]
    found_assignments: set[str] = set()
    found_functions: set[str] = set()
    found_error = False
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if names & assignments:
                selected.append(node)
                found_assignments.update(names & assignments)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            selected.append(node)
            found_functions.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name == "CapturedPaperOperatorChainError":
            selected.append(node)
            found_error = True
    if (
        found_assignments != assignments
        or found_functions != functions
        or not found_error
    ):
        raise CapturedPaperActivationAuthorityError(
            "CHAIN_LOADER_SHAPE_DRIFT", "operator-chain loader definitions changed"
        )
    name = f"_chili_chain_contract_{hashlib.sha256(str(path).encode()).hexdigest()[:16]}"
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    module.__dict__.update(
        {
            "Any": Any,
            "Mapping": Mapping,
            "Path": Path,
            "activation_runner": activation_runner,
            "hashlib": hashlib,
            "json": json,
            "re": re,
        }
    )
    sys.modules[name] = module
    try:
        ast.fix_missing_locations(tree)
        subset = ast.Module(body=selected, type_ignores=[])
        ast.fix_missing_locations(subset)
        exec(compile(subset, str(path), "exec", dont_inherit=True), module.__dict__)
    except Exception as exc:
        _unload_exact_module(module)
        raise CapturedPaperActivationAuthorityError(
            "CHAIN_LOADER_IMPORT_REJECTED", "operator-chain loader compile failed"
        ) from exc
    return module


def _revalidate_with_real_loaders(
    *,
    candidate_root: Path,
    activation_path: Path,
    activation_sha256: str,
    chain_path: Path,
    chain_sha256: str,
    runner_pin: _PinnedFile,
    chain_pin: _PinnedFile,
) -> None:
    if not _same_path(runner_pin.path, candidate_root / _ACTIVATION_RUNNER) or not _same_path(
        chain_pin.path, candidate_root / _CANDIDATE_ENTRYPOINTS["chain_script"]
    ):
        raise CapturedPaperActivationAuthorityError(
            "LOADER_PATH_MISMATCH", "real loader pins escaped candidate_root"
        )
    runner = _load_exact_module(runner_pin, role="activation_runner")
    chain: ModuleType | None = None
    try:
        if set(runner._REQUEST_KEYS) != set(_REQUEST_KEYS):
            raise CapturedPaperActivationAuthorityError(
                "OUTER_SCHEMA_DRIFT", "outer request key contract changed"
            )
        loaded = runner.load_activation_runner_request(
            request_path=activation_path,
            request_sha256=activation_sha256,
        )
        chain = _load_exact_chain_contract(chain_pin, activation_runner=runner)
        if set(chain._CHAIN_KEYS) != set(_CHAIN_KEYS):
            raise CapturedPaperActivationAuthorityError(
                "CHAIN_SCHEMA_DRIFT", "chain request key contract changed"
            )
        document = chain._load_chain_request(
            request_path=chain_path,
            request_sha256=chain_sha256,
            activation_request=loaded,
        )
        if set(document) != set(_CHAIN_KEYS):
            raise CapturedPaperActivationAuthorityError(
                "CHAIN_LOADER_REJECTED", "real chain loader returned a partial request"
            )
    except CapturedPaperActivationAuthorityError:
        raise
    except Exception as exc:
        raise CapturedPaperActivationAuthorityError(
            "REAL_LOADER_REJECTED", "real activation loaders rejected authority"
        ) from exc
    finally:
        if chain is not None:
            _unload_exact_module(chain)
        _unload_exact_module(runner)


def build_captured_paper_activation_authority(
    *,
    candidate_root: str | Path,
    artifact_root: str | Path,
    legacy_root: str | Path,
    python_dependency_root: str | Path,
    runtime_env_path: str | Path,
    resource_benchmark_path: str | Path,
    expected_account_id: str,
    test_database_name: str,
    bridge_configuration: Mapping[str, Any],
    timeouts: Mapping[str, int] | None = None,
) -> BuiltCapturedPaperActivationAuthority:
    """Create the exact inner and outer PAPER authority envelopes.

    The caller must provide a new empty ``artifact_root``.  All other paths are
    read-only inputs.  No live/cash switch exists in this API by design.
    """

    candidate = _canonical_directory(candidate_root, field="candidate_root")
    artifact = _canonical_directory(artifact_root, field="artifact_root")
    legacy = _canonical_directory(legacy_root, field="legacy_root")
    dependency = _canonical_directory(
        python_dependency_root, field="python_dependency_root"
    )
    runtime = _canonical_file(runtime_env_path, field="runtime_env_path")
    benchmark = _canonical_file(
        resource_benchmark_path, field="resource_benchmark_path"
    )
    _reject_security_domain_overlaps(
        {
            "candidate_root": candidate,
            "artifact_root": artifact,
            "legacy_root": legacy,
            "python_dependency_root": dependency,
        }
    )
    for field, input_path in (
        ("runtime_env_path", runtime),
        ("resource_benchmark_path", benchmark),
    ):
        if _inside(input_path, artifact):
            raise CapturedPaperActivationAuthorityError(
                "INPUT_OUTPUT_OVERLAP", f"{field} may not be inside artifact_root"
            )
        parent = input_path.parent
        for root_name, protected in (
            ("candidate_root", candidate),
            ("artifact_root", artifact),
            ("legacy_root", legacy),
            ("python_dependency_root", dependency),
        ):
            if parent != protected and _inside(protected, parent):
                raise CapturedPaperActivationAuthorityError(
                    "BROAD_INPUT_ROOT_FORBIDDEN",
                    f"{field} parent would over-authorize {root_name}",
                )
    if any(artifact.iterdir()):
        raise CapturedPaperActivationAuthorityError(
            "ARTIFACT_ROOT_NOT_EMPTY", "artifact_root must be caller-created and empty"
        )

    try:
        canonical_account_id = str(uuid.UUID(str(expected_account_id)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperActivationAuthorityError(
            "ACCOUNT_ID_INVALID", "expected PAPER account id must be a UUID"
        ) from exc
    if canonical_account_id != str(expected_account_id):
        raise CapturedPaperActivationAuthorityError(
            "ACCOUNT_ID_INVALID", "expected PAPER account id must be canonical"
        )
    database_name = str(test_database_name or "")
    if _TEST_DATABASE_RE.fullmatch(database_name) is None:
        raise CapturedPaperActivationAuthorityError(
            "TEST_DATABASE_INVALID", "test database must be a bounded *_test name"
        )
    principal = getpass.getuser()
    if _PRINCIPAL_RE.fullmatch(principal) is None:
        raise CapturedPaperActivationAuthorityError(
            "HOST_PRINCIPAL_INVALID", "host principal cannot be bound safely"
        )
    safe_bridge_configuration = _validate_bridge_configuration(bridge_configuration)
    safe_timeouts = _validate_timeouts(timeouts)

    executables = _authoritative_executables()
    pins: dict[str, _PinnedFile] = {
        "runtime_env": _pin_file(
            runtime, field="runtime_env_path", max_bytes=_MAX_RUNTIME_ENV_BYTES
        ),
        "resource_benchmark": _pin_file(
            benchmark,
            field="resource_benchmark_path",
            max_bytes=_MAX_BENCHMARK_BYTES,
        ),
    }
    for field, path in executables.items():
        pins[field] = _pin_file(path, field=field, max_bytes=_MAX_EXECUTABLE_BYTES)
    entrypoints: dict[str, _PinnedFile] = {}
    for field, relative in _CANDIDATE_ENTRYPOINTS.items():
        path = _canonical_file(candidate / relative, field=field)
        entrypoints[field] = _pin_file(path, field=field, max_bytes=_MAX_SOURCE_BYTES)
    runner_path = _canonical_file(
        candidate / _ACTIVATION_RUNNER, field="activation_runner"
    )
    runner_pin = _pin_file(
        runner_path, field="activation_runner", max_bytes=_MAX_SOURCE_BYTES
    )

    commit = _verify_git_worktree(
        git=executables["git_executable"], candidate_root=candidate
    )
    dependency_identity_sha256 = _dependency_identity_sha256(
        stage0_pin=entrypoints["bootstrap_stage0_script"],
        dependency_root=dependency,
        python_pin=pins["python_executable"],
    )

    roots = _minimal_allowed_roots(
        (
            candidate,
            artifact,
            legacy,
            dependency,
            runtime.parent,
            benchmark.parent,
            *(path.parent for path in executables.values()),
        )
    )
    chain_document: Mapping[str, Any] = {
        "schema_version": CHAIN_SCHEMA_VERSION,
        "account_scope": ACCOUNT_SCOPE,
        "live_cash_authorized": False,
        "resource_benchmark": {
            "path": str(benchmark),
            "sha256": pins["resource_benchmark"].sha256,
        },
        "legacy_root": str(legacy),
        "python_dependency_root": str(dependency),
        "python_dependency_root_identity_sha256": dependency_identity_sha256,
        "bootstrap_stage0_script": str(entrypoints["bootstrap_stage0_script"].path),
        "bootstrap_stage0_script_sha256": entrypoints[
            "bootstrap_stage0_script"
        ].sha256,
        "host_principal_user_id": principal,
        "bridge_configuration": safe_bridge_configuration,
    }
    if set(chain_document) != set(_CHAIN_KEYS):
        raise CapturedPaperActivationAuthorityError(
            "CHAIN_SCHEMA_INTERNAL_ERROR", "inner request was not exact"
        )
    chain_raw = _canonical_json_bytes(chain_document)
    chain_path, chain_sha256 = _publish_new_json(
        artifact, kind="chain-request", raw=chain_raw
    )

    request_document: Mapping[str, Any] = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "account_scope": ACCOUNT_SCOPE,
        "live_cash_authorized": False,
        "paper_task_name": PAPER_TASK_NAME,
        "candidate_root": str(candidate),
        "expected_git_commit": commit,
        "git_executable": str(pins["git_executable"].path),
        "git_executable_sha256": pins["git_executable"].sha256,
        "python_executable": str(pins["python_executable"].path),
        "python_executable_sha256": pins["python_executable"].sha256,
        "powershell_executable": str(pins["powershell_executable"].path),
        "powershell_executable_sha256": pins["powershell_executable"].sha256,
        "schtasks_executable": str(pins["schtasks_executable"].path),
        "schtasks_executable_sha256": pins["schtasks_executable"].sha256,
        "bootstrap_stage0_script": str(entrypoints["bootstrap_stage0_script"].path),
        "bootstrap_stage0_script_sha256": entrypoints[
            "bootstrap_stage0_script"
        ].sha256,
        "chain_script": str(entrypoints["chain_script"].path),
        "chain_script_sha256": entrypoints["chain_script"].sha256,
        "chain_request_path": str(chain_path),
        "chain_request_sha256": chain_sha256,
        "finalizer_script": str(entrypoints["finalizer_script"].path),
        "finalizer_script_sha256": entrypoints["finalizer_script"].sha256,
        "cutover_script": str(entrypoints["cutover_script"].path),
        "cutover_script_sha256": entrypoints["cutover_script"].sha256,
        "python_dependency_root": str(dependency),
        "python_dependency_root_identity_sha256": dependency_identity_sha256,
        "runtime_env_path": str(runtime),
        "runtime_env_sha256": pins["runtime_env"].sha256,
        "artifact_root": str(artifact),
        "expected_account_id": canonical_account_id,
        "test_database_name": database_name,
        "allowed_read_roots": [str(path) for path in roots],
        "timeouts": safe_timeouts,
    }
    if set(request_document) != set(_REQUEST_KEYS):
        raise CapturedPaperActivationAuthorityError(
            "OUTER_SCHEMA_INTERNAL_ERROR", "outer request was not exact"
        )
    request_raw = _canonical_json_bytes(request_document)
    request_path, request_sha256 = _publish_new_json(
        artifact, kind="activation-request", raw=request_raw
    )

    _revalidate_with_real_loaders(
        candidate_root=candidate,
        activation_path=request_path,
        activation_sha256=request_sha256,
        chain_path=chain_path,
        chain_sha256=chain_sha256,
        runner_pin=runner_pin,
        chain_pin=entrypoints["chain_script"],
    )
    for field, pin in {**pins, **entrypoints, "activation_runner": runner_pin}.items():
        _recheck_pin(pin, field=field)
    if (
        _dependency_identity_sha256(
            stage0_pin=entrypoints["bootstrap_stage0_script"],
            dependency_root=dependency,
            python_pin=pins["python_executable"],
        )
        != dependency_identity_sha256
    ):
        raise CapturedPaperActivationAuthorityError(
            "DEPENDENCY_ROOT_DRIFT", "Python dependency root changed"
        )
    _verify_git_worktree(
        git=executables["git_executable"],
        candidate_root=candidate,
        expected_commit=commit,
    )
    if chain_path.read_bytes() != chain_raw or request_path.read_bytes() != request_raw:
        raise CapturedPaperActivationAuthorityError(
            "PUBLISHED_AUTHORITY_DRIFT", "published authority bytes changed"
        )

    common = (
        str(pins["python_executable"].path),
        "-I",
        "-S",
        "-B",
        str(runner_path),
        "--request",
        str(request_path),
        "--request-sha256",
        request_sha256,
    )
    validate_argv = (*common, "--mode", "ValidateOnly")
    activate_argv = (
        *common,
        "--mode",
        "ActivatePaper",
        "--confirm-fake-money-paper",
        ACTIVATE_CONFIRMATION,
    )
    receipt_document = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "account_scope": ACCOUNT_SCOPE,
        "live_cash_authorized": False,
        "paper_task_name": PAPER_TASK_NAME,
        "expected_git_commit": commit,
        "chain_request": {"path": str(chain_path), "sha256": chain_sha256},
        "activation_request": {
            "path": str(request_path),
            "sha256": request_sha256,
        },
        "activation_runner": {
            "path": str(runner_path),
            "sha256": runner_pin.sha256,
        },
        "python_dependency_root_identity_sha256": dependency_identity_sha256,
        "runtime_env_sha256": pins["runtime_env"].sha256,
        "resource_benchmark_sha256": pins["resource_benchmark"].sha256,
        "validate_only_argv": list(validate_argv),
        "activate_paper_argv": list(activate_argv),
        "argv_is_shell_string": False,
        "invoked": False,
        "broker_contacted": False,
        "provider_contacted": False,
        "host_state_mutated": False,
        "paper_service_started": False,
        "paper_order_submission_authorized": False,
    }
    receipt_raw = _canonical_json_bytes(receipt_document)
    receipt_path, receipt_sha256 = _publish_new_json(
        artifact, kind="receipt", raw=receipt_raw
    )
    return BuiltCapturedPaperActivationAuthority(
        chain_request_path=chain_path,
        chain_request_sha256=chain_sha256,
        activation_request_path=request_path,
        activation_request_sha256=request_sha256,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha256,
        validate_only_argv=tuple(validate_argv),
        activate_paper_argv=tuple(activate_argv),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--legacy-root", required=True)
    parser.add_argument("--python-dependency-root", required=True)
    parser.add_argument("--runtime-env", required=True)
    parser.add_argument("--resource-benchmark", required=True)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--test-database-name", required=True)
    parser.add_argument("--bridge-configuration", required=True)
    parser.add_argument("--bridge-configuration-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        bridge_path = _canonical_file(
            arguments.bridge_configuration, field="bridge_configuration_path"
        )
        bridge_pin = _pin_file(
            bridge_path,
            field="bridge_configuration_path",
            max_bytes=_MAX_JSON_BYTES,
        )
        expected_bridge_sha = str(arguments.bridge_configuration_sha256 or "")
        if (
            _SHA256_RE.fullmatch(expected_bridge_sha) is None
            or bridge_pin.sha256 != expected_bridge_sha
        ):
            raise CapturedPaperActivationAuthorityError(
                "BRIDGE_CONFIGURATION_HASH_MISMATCH",
                "bridge configuration differs from its hash",
            )
        bridge = _strict_json(
            _read_pinned_bytes(bridge_pin, field="bridge_configuration_path"),
            field="bridge_configuration",
        )
        built = build_captured_paper_activation_authority(
            candidate_root=arguments.candidate_root,
            artifact_root=arguments.artifact_root,
            legacy_root=arguments.legacy_root,
            python_dependency_root=arguments.python_dependency_root,
            runtime_env_path=arguments.runtime_env,
            resource_benchmark_path=arguments.resource_benchmark,
            expected_account_id=arguments.expected_account_id,
            test_database_name=arguments.test_database_name,
            bridge_configuration=bridge,
        )
        _recheck_pin(bridge_pin, field="bridge_configuration_path")
    except CapturedPaperActivationAuthorityError as exc:
        print(
            json.dumps(
                {
                    "schema_version": RECEIPT_SCHEMA_VERSION,
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
    print(
        json.dumps(
            {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "verdict": "BUILT_NOT_INVOKED",
                "receipt_path": str(built.receipt_path),
                "receipt_sha256": built.receipt_sha256,
                "account_scope": ACCOUNT_SCOPE,
                "live_cash_authorized": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI boundary
    raise SystemExit(main())
