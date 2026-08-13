"""Build an offline, exact launch authority for the captured-PAPER benchmark.

This module is deliberately standard-library-only and import-inert.  It never
imports candidate repository code, reads a database, contacts a provider or
broker, or launches the benchmark.  Its only writes are create-new canonical
JSON objects beneath a caller-created, empty authority root.

The resulting receipt is the launch capability: it binds one exact argv array
to a clean Git commit, seven externally supplied source hashes, an exact
CPython 3.11 executable, a sealed dependency-root identity, and one empty local
benchmark output root.  The held-source loader is extracted from the already
hash-pinned pressure-probe source with :mod:`ast`; candidate code is never
executed while the authority is constructed.
"""

from __future__ import annotations

if __name__ == "__main__" and (
    globals().get("__chili_held_builder_source_sha256__") is None
    or globals().get("__chili_held_python_executable_sha256__") is None
):
    raise SystemExit("BUILDER_HELD_BOOTSTRAP_REQUIRED")

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence


MANIFEST_SCHEMA_VERSION = (
    "chili.captured-paper-benchmark-authority-manifest.v2"
)
RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-benchmark-authority-receipt.v2"
)
RUNNER_AUTHORITY_SCHEMA_VERSION = (
    "chili.captured-paper-benchmark-runner-authority.v1"
)
ACCOUNT_SCOPE = "alpaca:paper"
AUTHORITY_MODE = "diagnostic_capture_benchmark_only"
EXPECTED_BENCHMARK_SCHEMA_VERSION = "chili.replay-capture-benchmark.v7"
DEPENDENCY_ROOT_IDENTITY_SCHEMA_VERSION = (
    "chili.captured-paper-python-dependency-root-identity.v2"
)
DEPENDENCY_TREE_SCHEMA_VERSION = "chili.python-dependency-tree.v2"
DEPENDENCY_EXCLUSION_POLICY = "exclude-__pycache__-pyc-pyo.v1"
STORAGE_VOLUME_IDENTITY_SCHEMA_VERSION = (
    "chili.capture-storage-volume-identity.v1"
)
HELD_LOADER_VARIABLE = "_HELD_BENCHMARK_SOURCE_LOADER"
HELD_RUNNER_LOADER_VARIABLE = "_HELD_BENCHMARK_AUTHORITY_RUNNER_LOADER"
HELD_BUILDER_LOADER_VARIABLE = "_HELD_BENCHMARK_AUTHORITY_BUILDER_LOADER"

# A caller must extract this literal from externally hash-pinned builder bytes
# and execute it with CPython 3.11 ``-I -S -B -c``.  The builder CLI refuses a
# direct/bare invocation.  No repository code executes until the complete held
# builder byte string and the running interpreter have been verified.
_HELD_BENCHMARK_AUTHORITY_BUILDER_LOADER = r"""
import hashlib, os, stat, sys
from pathlib import Path
flags = sys.flags
if not (sys.implementation.name == 'cpython'
        and sys.version_info[:2] == (3, 11)
        and flags.isolated == 1 and flags.ignore_environment == 1
        and flags.no_site == 1 and flags.safe_path is True
        and flags.dont_write_bytecode == 1):
    raise SystemExit(90)
values = tuple(sys.argv[1:])
try:
    boundary = values.index('--')
except ValueError:
    raise SystemExit(91)
bootstrap = values[:boundary]
arguments = values[boundary + 1:]
if len(bootstrap) != 3 or not arguments:
    raise SystemExit(92)
raw_path = Path(bootstrap[0])
expected_source_sha256 = bootstrap[1]
expected_python_sha256 = bootstrap[2]
if (not raw_path.is_absolute()
        or any(len(value) != 64
               or any(c not in '0123456789abcdef' for c in value)
               for value in (expected_source_sha256, expected_python_sha256))):
    raise SystemExit(93)
def identity(value):
    return (int(value.st_dev), int(value.st_ino), int(value.st_size),
            int(value.st_mtime_ns), int(stat.S_IFMT(value.st_mode)),
            int(getattr(value, 'st_file_attributes', 0)))
def hold(path, expected, limit):
    resolved = path.resolve(strict=True)
    before = os.stat(resolved, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode)
            or int(getattr(before, 'st_file_attributes', 0)) & 0x400
            or int(before.st_size) <= 0 or int(before.st_size) > limit):
        raise SystemExit(94)
    descriptor = os.open(resolved, os.O_RDONLY | int(getattr(os, 'O_BINARY', 0)))
    try:
        opened = os.fstat(descriptor)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        terminal = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = os.stat(resolved, follow_symlinks=False)
    source = b''.join(chunks)
    if (identity(before) != identity(opened)
            or identity(opened) != identity(terminal)
            or identity(terminal) != identity(after)
            or hashlib.sha256(source).hexdigest() != expected):
        raise SystemExit(95)
    return resolved, source
builder_path, builder_source = hold(raw_path, expected_source_sha256,
                                    64 * 1024 * 1024)
executable_path, executable_source = hold(Path(sys.executable),
                                          expected_python_sha256,
                                          256 * 1024 * 1024)
if executable_path != Path(sys.executable).resolve(strict=True):
    raise SystemExit(96)
sys.argv[:] = [str(builder_path), *arguments]
scope = {'__name__': '__main__', '__file__': str(builder_path),
         '__package__': None, '__cached__': None,
         '__builtins__': __builtins__,
         '__chili_held_builder_source_sha256__': expected_source_sha256,
         '__chili_held_python_executable_sha256__': expected_python_sha256}
exec(compile(builder_source, str(builder_path), 'exec', dont_inherit=True),
     scope, scope)
""".strip()

LOADER_ROLE_ORDER = (
    "benchmark",
    "contract",
    "runtime",
    "pressure_probe",
    "replay_errors",
    "first_dip_tape_policy",
    "stage0",
)
SOURCE_ROLE_PATHS: Mapping[str, Path] = {
    "benchmark": Path("scripts/benchmark_replay_capture_runtime.py"),
    "contract": Path(
        "app/services/trading/momentum_neural/replay_capture_contract.py"
    ),
    "runtime": Path(
        "app/services/trading/momentum_neural/replay_capture_runtime.py"
    ),
    "pressure_probe": Path("scripts/captured_paper_pressure_probe.py"),
    "replay_errors": Path(
        "app/services/trading/momentum_neural/replay_errors.py"
    ),
    "first_dip_tape_policy": Path(
        "app/services/trading/momentum_neural/first_dip_tape_policy.py"
    ),
    "stage0": Path("scripts/captured_paper_isolated_stage0.py"),
}
AUTHORITY_PROGRAM_PATHS: Mapping[str, Path] = {
    "builder": Path("scripts/build_captured_paper_benchmark_authority.py"),
    "runner": Path("scripts/run_captured_paper_benchmark_authority.py"),
}


def _expected_tracked_paths() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *(path.as_posix() for path in SOURCE_ROLE_PATHS.values()),
                *(path.as_posix() for path in AUTHORITY_PROGRAM_PATHS.values()),
            }
        )
    )

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+!-]{0,127}$")
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
_MAX_DEPENDENCY_FILES = 8_192
_MAX_DEPENDENCY_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_LOADER_BYTES = 128 * 1024
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_ARGUMENTS = 64
_MAX_ARGUMENT_BYTES = 16 * 1024
_BENCHMARK_TIMEOUT_SECONDS = 3_600

_VALUE_ARGUMENTS: Mapping[str, tuple[str, float, float]] = {
    "--events": ("int", 1_000, 1_000_000_000),
    "--symbols": ("int", 1, 1_000_000),
    "--queue-events": ("int", 1, 1_000_000_000),
    "--queue-mib": ("int", 1, 1_048_576),
    "--gap-keys": ("int", 1, 1_000_000_000),
    "--batch-events": ("int", 1, 1_000_000_000),
    "--batch-mib": ("int", 1, 1_048_576),
    "--poll-ms": ("float", 0.000_001, 3_600_000),
    "--flush-ms": ("float", 0.000_001, 3_600_000),
    "--writers": ("int", 2, 1_024),
    "--artifact-max-age-s": ("float", 0.000_001, 31_536_000),
    "--stop-timeout-s": ("float", 0.000_001, 86_400),
    "--rss-sample-ms": ("float", 0.000_001, 3_600_000),
    "--compression-level": ("int", 1, 22),
    "--payload-pack-records": ("int", 1, 1_000_000_000),
    "--payload-pack-mib": ("int", 1, 1_048_576),
    "--payload-pack-read-cache": ("int", 1, 1_000_000),
}
_CHOICE_ARGUMENTS: Mapping[str, frozenset[str]] = {
    "--compression-codec": frozenset({"zstd", "zlib"}),
}
_FLAG_ARGUMENTS = frozenset({"--keep"})
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


class CapturedPaperBenchmarkAuthorityError(RuntimeError):
    """Stable fail-closed construction error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True, slots=True)
class BuiltCapturedPaperBenchmarkAuthority:
    manifest_path: Path
    manifest_sha256: str
    receipt_path: Path
    receipt_sha256: str
    benchmark_argv: tuple[str, ...]
    runner_argv: tuple[str, ...]
    runner_authority_path: Path
    runner_authority_sha256: str


@dataclass(frozen=True, slots=True)
class _PinnedFile:
    path: Path
    sha256: str
    identity: tuple[int, int, int, int, int, int]
    max_bytes: int


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "NON_CANONICAL_VALUE", "authority value is not canonical JSON"
        ) from exc
    if not raw or len(raw) > _MAX_JSON_BYTES:
        raise CapturedPaperBenchmarkAuthorityError(
            "JSON_RESOURCE_BUDGET_EXCEEDED", "authority JSON exceeds its bound"
        )
    return raw


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(value: Any, *, field: str) -> str:
    candidate = str(value or "").lower()
    if _SHA256_RE.fullmatch(candidate) is None:
        raise CapturedPaperBenchmarkAuthorityError(
            "SHA256_INVALID", f"{field} must be one lowercase SHA-256"
        )
    return candidate


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _overlap(left: Path, right: Path) -> bool:
    return _inside(left, right) or _inside(right, left)


def _reject_nonlocal_spelling(path: Path, *, field: str) -> None:
    raw = os.fspath(path)
    normalized = raw.replace("/", "\\")
    if normalized.startswith(("\\\\", "\\?\\", "\\.\\")):
        raise CapturedPaperBenchmarkAuthorityError(
            "NONLOCAL_PATH", f"{field} must be local"
        )
    if not path.is_absolute():
        raise CapturedPaperBenchmarkAuthorityError(
            "PATH_INVALID", f"{field} must be absolute"
        )
    if os.name == "nt":
        drive, tail = os.path.splitdrive(raw)
        if not drive:
            raise CapturedPaperBenchmarkAuthorityError(
                "PATH_INVALID", f"{field} must have an explicit local drive"
            )
        if ":" in tail:
            raise CapturedPaperBenchmarkAuthorityError(
                "ADS_PATH_FORBIDDEN", f"{field} may not use an alternate stream"
            )


def _reject_network_drive(path: Path, *, field: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(str(Path(path.anchor))))
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "DRIVE_AUTHORITY_UNAVAILABLE", f"{field} drive type is unavailable"
        ) from exc
    if drive_type == 4:
        raise CapturedPaperBenchmarkAuthorityError(
            "NETWORK_PATH_FORBIDDEN", f"{field} may not use a mapped drive"
        )
    if drive_type not in {2, 3, 6}:
        raise CapturedPaperBenchmarkAuthorityError(
            "LOCAL_DRIVE_UNPROVEN", f"{field} is not on a proven local drive"
        )


def _reject_reparse_chain(path: Path, *, field: str) -> None:
    lexical = Path(os.path.abspath(os.fspath(path)))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current = current / part
        if not os.path.lexists(current):
            continue
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise CapturedPaperBenchmarkAuthorityError(
                "PATH_UNAVAILABLE", f"{field} path metadata is unavailable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or bool(
            int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_ATTRIBUTE
        ):
            raise CapturedPaperBenchmarkAuthorityError(
                "REPARSE_PATH_FORBIDDEN", f"{field} traverses a reparse point"
            )


def _has_canonical_spelling(path: Path, resolved: Path) -> bool:
    lexical = Path(os.path.abspath(os.fspath(path)))
    return _path_key(lexical) == _path_key(resolved)


def _canonical_directory(value: str | Path, *, field: str) -> Path:
    path = Path(value)
    _reject_nonlocal_spelling(path, field=field)
    if ".." in path.parts:
        raise CapturedPaperBenchmarkAuthorityError(
            "PATH_NOT_CANONICAL", f"{field} may not contain parent traversal"
        )
    _reject_network_drive(path, field=field)
    _reject_reparse_chain(path, field=field)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "PATH_UNAVAILABLE", f"{field} is unavailable"
        ) from exc
    if not _has_canonical_spelling(path, resolved) or not resolved.is_dir():
        raise CapturedPaperBenchmarkAuthorityError(
            "PATH_NOT_CANONICAL", f"{field} must be an exact directory"
        )
    if resolved.parent == resolved:
        raise CapturedPaperBenchmarkAuthorityError(
            "BROAD_ROOT_FORBIDDEN", f"{field} may not be a filesystem root"
        )
    _reject_reparse_chain(resolved, field=field)
    return resolved


def _canonical_file(value: str | Path, *, field: str) -> Path:
    path = Path(value)
    _reject_nonlocal_spelling(path, field=field)
    if ".." in path.parts:
        raise CapturedPaperBenchmarkAuthorityError(
            "PATH_NOT_CANONICAL", f"{field} may not contain parent traversal"
        )
    _reject_network_drive(path, field=field)
    _reject_reparse_chain(path, field=field)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "PATH_UNAVAILABLE", f"{field} is unavailable"
        ) from exc
    if not _has_canonical_spelling(path, resolved) or not resolved.is_file():
        raise CapturedPaperBenchmarkAuthorityError(
            "PATH_NOT_CANONICAL", f"{field} must be an exact file"
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


def _directory_identity(path: Path) -> Mapping[str, int]:
    _reject_reparse_chain(path, field="directory_identity")
    metadata = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise CapturedPaperBenchmarkAuthorityError(
            "DIRECTORY_IDENTITY_INVALID", "authority directory identity is invalid"
        )
    return {
        "st_dev": int(metadata.st_dev),
        "st_ino": int(metadata.st_ino),
        "st_mode": int(metadata.st_mode),
        "st_mtime_ns": int(metadata.st_mtime_ns),
    }


def _pin_file(path: Path, *, field: str, max_bytes: int) -> _PinnedFile:
    _reject_reparse_chain(path, field=field)
    before = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or int(before.st_size) <= 0
        or int(before.st_size) > max_bytes
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "FILE_INVALID", f"{field} must be a bounded regular file"
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
        raise CapturedPaperBenchmarkAuthorityError(
            "FILE_DRIFT", f"{field} changed during stable read"
        )
    return _PinnedFile(
        path=path,
        sha256=_sha256_bytes(first),
        identity=_file_identity(after),
        max_bytes=max_bytes,
    )


def _read_pinned_bytes(pin: _PinnedFile, *, field: str) -> bytes:
    before = os.stat(pin.path, follow_symlinks=False)
    raw = pin.path.read_bytes()
    after = os.stat(pin.path, follow_symlinks=False)
    if (
        _file_identity(before) != pin.identity
        or _file_identity(after) != pin.identity
        or len(raw) > pin.max_bytes
        or _sha256_bytes(raw) != pin.sha256
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "FILE_DRIFT", f"{field} changed after it was pinned"
        )
    return raw


def _recheck_pin(pin: _PinnedFile, *, field: str) -> None:
    _read_pinned_bytes(pin, field=field)


def _minimal_git_environment(sandbox: Path) -> Mapping[str, str]:
    allowed = {
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
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    hooks = sandbox / "empty-hooks"
    hooks.mkdir(mode=0o700, exist_ok=False)
    null_device = "NUL" if os.name == "nt" else "/dev/null"
    environment.update(
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
    return environment


def _run_git(
    git: Path, candidate_root: Path, arguments: Sequence[str], *, env: Mapping[str, str]
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
        raise CapturedPaperBenchmarkAuthorityError(
            "GIT_PROBE_FAILED", "sanitized Git verification failed"
        ) from exc
    if len(result.stdout) + len(result.stderr) > 16 * 1024 * 1024:
        raise CapturedPaperBenchmarkAuthorityError(
            "GIT_OUTPUT_OVERSIZED", "sanitized Git output is oversized"
        )
    return result


def _dangerous_ignored(relative: str) -> bool:
    path = Path(str(relative or "").replace("\\", "/"))
    return (
        path.name.casefold() in _DANGEROUS_IGNORED_NAMES
        or path.suffix.casefold() in _DANGEROUS_IGNORED_SUFFIXES
        or (
            path.suffix.casefold() == ".pyc"
            and "__pycache__" not in {part.casefold() for part in path.parts}
        )
    )


def _verify_git_worktree(
    *,
    git_pin: _PinnedFile,
    candidate_root: Path,
    expected_commit: str,
    expected_tracked_paths: Sequence[str],
) -> None:
    _recheck_pin(git_pin, field="git_executable")
    git = git_pin.path
    with tempfile.TemporaryDirectory(prefix="chili-benchmark-authority-git-") as raw:
        environment = _minimal_git_environment(Path(raw))
        def checked_git(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
            _recheck_pin(git_pin, field="git_executable")
            result = _run_git(
                git, candidate_root, arguments, env=environment
            )
            _recheck_pin(git_pin, field="git_executable")
            return result

        top = checked_git(("rev-parse", "--show-toplevel"))
        if (
            top.returncode != 0
            or _path_key(Path(top.stdout.strip()).resolve(strict=False))
            != _path_key(candidate_root)
        ):
            raise CapturedPaperBenchmarkAuthorityError(
                "GIT_ROOT_MISMATCH", "repo_root is not the exact Git root"
            )
        head = checked_git(("rev-parse", "HEAD"))
        if head.returncode != 0 or head.stdout.strip() != expected_commit:
            raise CapturedPaperBenchmarkAuthorityError(
                "GIT_HEAD_MISMATCH", "Git HEAD differs from expected clean commit"
            )
        status_result = checked_git(
            (
                "status",
                "--porcelain=v2",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ),
        )
        if status_result.returncode != 0 or status_result.stdout.strip():
            raise CapturedPaperBenchmarkAuthorityError(
                "WORKTREE_DIRTY", "benchmark candidate worktree is not clean"
            )
        tracked = checked_git(
            (
                "ls-files",
                "--error-unmatch",
                "-z",
                "--",
                *sorted(expected_tracked_paths),
            ),
        )
        observed = {item for item in tracked.stdout.split("\0") if item}
        if tracked.returncode != 0 or observed != set(expected_tracked_paths):
            raise CapturedPaperBenchmarkAuthorityError(
                "SOURCE_PATH_UNTRACKED", "source closure is not tracked exactly"
            )
        ignored = checked_git(
            ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
        )
        if ignored.returncode != 0:
            raise CapturedPaperBenchmarkAuthorityError(
                "IGNORED_INVENTORY_UNAVAILABLE", "ignored inventory is unavailable"
            )
        if any(
            _dangerous_ignored(item)
            for item in ignored.stdout.split("\0")
            if item
        ):
            raise CapturedPaperBenchmarkAuthorityError(
                "IGNORED_EXECUTABLE_PAYLOAD",
                "candidate contains ignored executable or importable payloads",
            )


def _probe_python311(executable: Path) -> None:
    probe = (
        "import sys;"
        "f=sys.flags;"
        "print('%s:%d:%d:%d:%d:%d:%d'%"
        "(sys.implementation.name,sys.version_info[0],sys.version_info[1],"
        "f.isolated,f.no_site,int(f.safe_path),f.dont_write_bytecode))"
    )
    allowed = {
        "COMSPEC",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    environment["PYTHONNOUSERSITE"] = "1"
    try:
        result = subprocess.run(
            [str(executable), "-I", "-S", "-B", "-c", probe],
            cwd=executable.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="ascii",
            errors="strict",
            timeout=30,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "PYTHON_PROBE_FAILED", "isolated Python version probe failed"
        ) from exc
    if (
        result.returncode != 0
        or result.stderr
        or result.stdout != "cpython:3:11:1:1:1:1\n"
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "PYTHON_RUNTIME_INVALID", "executable is not exact isolated CPython 3.11"
        )


def _sanitized_execution_environment() -> Mapping[str, str]:
    """Return the exact non-secret environment authorized for the benchmark."""

    allowed = {
        "LANG",
        "LC_ALL",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    if (
        any(
            type(key) is not str
            or not key
            or type(value) is not str
            or "\x00" in key
            or "=" in key
            or "\x00" in value
            or "\r" in value
            or "\n" in value
            for key, value in environment.items()
        )
        or sum(
            len(key.encode("utf-8")) + len(value.encode("utf-8"))
            for key, value in environment.items()
        )
        > 16 * 1024
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "EXECUTION_ENVIRONMENT_INVALID",
            "sanitized benchmark environment is malformed or oversized",
        )
    return dict(sorted(environment.items()))


def _extract_held_loader(pressure_probe_pin: _PinnedFile) -> str:
    raw = _read_pinned_bytes(pressure_probe_pin, field="pressure_probe")
    try:
        tree = ast.parse(raw, filename=str(pressure_probe_pin.path))
    except (SyntaxError, ValueError) as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "LOADER_AST_INVALID", "pressure-probe source cannot be parsed"
        ) from exc
    candidates: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == HELD_LOADER_VARIABLE
            for target in node.targets
        ):
            candidates.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == HELD_LOADER_VARIABLE
        ):
            candidates.append(node.value)
    if len(candidates) != 1:
        raise CapturedPaperBenchmarkAuthorityError(
            "LOADER_ASSIGNMENT_INVALID", "held benchmark loader assignment is not unique"
        )
    value = candidates[0]
    literal: str | None = None
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        literal = value.value
    elif (
        isinstance(value, ast.Call)
        and not value.args
        and not value.keywords
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "strip"
        and isinstance(value.func.value, ast.Constant)
        and isinstance(value.func.value.value, str)
    ):
        literal = value.func.value.value.strip()
    if (
        not literal
        or "\x00" in literal
        or len(literal.encode("utf-8")) > _MAX_LOADER_BYTES
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "LOADER_LITERAL_INVALID", "held benchmark loader is not one bounded literal"
        )
    try:
        loader_tree = ast.parse(literal, filename="<held-benchmark-loader>")
        compile(loader_tree, "<held-benchmark-loader>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "LOADER_LITERAL_INVALID", "held benchmark loader is not valid Python"
        ) from exc
    role_values: list[tuple[str, ...]] = []
    imported: set[str] = set()
    for node in loader_tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(str(node.module or ""))
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "roles" for target in node.targets)
        ):
            try:
                parsed_roles = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                parsed_roles = None
            if isinstance(parsed_roles, tuple) and all(
                isinstance(role, str) for role in parsed_roles
            ):
                role_values.append(parsed_roles)
    if role_values != [LOADER_ROLE_ORDER] or not imported.issubset(
        {"hashlib", "json", "os", "stat", "sys", "pathlib", "types"}
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "LOADER_CONTRACT_INVALID", "held benchmark loader closure drifted"
        )
    required_markers = (
        "len(bootstrap) != len(roles) * 2 + 5",
        "benchmark_authority_manifest_path",
        "expected_python_executable_sha256",
        "executable_sha != expected_python_executable_sha256",
        "benchmark_authority_manifest_sha256",
        "__chili_held_benchmark_sources__",
        "__chili_benchmark_dependency_identity_sha256__",
        "__chili_benchmark_python_executable_sha256__",
        "__chili_benchmark_authority_manifest_sha256__",
    )
    if any(marker not in literal for marker in required_markers):
        raise CapturedPaperBenchmarkAuthorityError(
            "LOADER_CONTRACT_INVALID", "held benchmark loader authority contract drifted"
        )
    return literal


def _extract_runner_loader(runner_pin: _PinnedFile) -> str:
    raw = _read_pinned_bytes(runner_pin, field="runner_source")
    try:
        tree = ast.parse(raw, filename=str(runner_pin.path))
    except (SyntaxError, ValueError) as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "RUNNER_LOADER_AST_INVALID", "runner source cannot be parsed"
        ) from exc
    candidates: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == HELD_RUNNER_LOADER_VARIABLE
            for target in node.targets
        ):
            candidates.append(node.value)
    if len(candidates) != 1:
        raise CapturedPaperBenchmarkAuthorityError(
            "RUNNER_LOADER_ASSIGNMENT_INVALID",
            "held runner loader assignment is not unique",
        )
    value = candidates[0]
    literal: str | None = None
    if (
        isinstance(value, ast.Call)
        and not value.args
        and not value.keywords
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "strip"
        and isinstance(value.func.value, ast.Constant)
        and isinstance(value.func.value.value, str)
    ):
        literal = value.func.value.value.strip()
    if not literal or len(literal.encode("utf-8")) > _MAX_LOADER_BYTES or "\x00" in literal:
        raise CapturedPaperBenchmarkAuthorityError(
            "RUNNER_LOADER_LITERAL_INVALID",
            "held runner loader is not one bounded literal",
        )
    try:
        loader_tree = ast.parse(literal, filename="<held-benchmark-authority-runner>")
        compile(loader_tree, "<held-benchmark-authority-runner>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "RUNNER_LOADER_LITERAL_INVALID", "held runner loader is invalid"
        ) from exc
    imported: set[str] = set()
    for node in loader_tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(str(node.module or ""))
    markers = (
        "len(bootstrap) != 5",
        "builder_expected",
        "runner_expected",
        "python_expected",
        "__chili_held_builder_module__",
        "__chili_held_runner_source_sha256__",
    )
    if not imported.issubset({"hashlib", "os", "stat", "sys", "types", "pathlib"}) or any(
        marker not in literal for marker in markers
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "RUNNER_LOADER_CONTRACT_INVALID", "held runner loader contract drifted"
        )
    return literal


def _dependency_path_is_excluded(relative: str) -> bool:
    parts = tuple(part.casefold() for part in Path(relative).parts)
    return "__pycache__" in parts or relative.casefold().endswith((".pyc", ".pyo"))


def _dependency_file_identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return metadata stable across Windows path and descriptor stat calls.

    Windows synthesizes executable permission bits from path suffixes such as
    ``.cmd`` and ``.exe``.  A descriptor has no suffix, so compare the file
    type via ``S_IFMT`` while retaining every mutation-relevant identity field.
    """

    return (
        int(item.st_dev),
        int(item.st_ino),
        int(item.st_size),
        int(item.st_mtime_ns),
        int(stat.S_IFMT(item.st_mode)),
    )


def _inventory_dependency_tree(root: Path) -> Mapping[str, Any]:
    _reject_reparse_chain(root, field="python_dependency_root")
    first_paths: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if not _dependency_path_is_excluded(relative):
            first_paths.append((relative, path))
    first_paths.sort(key=lambda item: (item[0].casefold(), item[0]))
    casefolded: set[str] = set()
    rows: list[Mapping[str, Any]] = []
    files: dict[str, Mapping[str, Any]] = {}
    file_count = 0
    directory_count = 0
    total_bytes = 0
    for relative, path in first_paths:
        folded = relative.casefold()
        if folded in casefolded:
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_CASEFOLD_COLLISION", "dependency paths collide by case"
            )
        casefolded.add(folded)
        _reject_reparse_chain(path, field="python_dependency_root")
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or bool(
            int(getattr(before, "st_file_attributes", 0)) & _REPARSE_ATTRIBUTE
        ):
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_REPARSE_PATH", "dependency tree contains a reparse path"
            )
        if stat.S_ISDIR(before.st_mode):
            rows.append({"path": relative, "type": "directory"})
            directory_count += 1
            continue
        if not stat.S_ISREG(before.st_mode):
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_SPECIAL_FILE", "dependency tree contains a special file"
            )
        descriptor = os.open(
            path, os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        )
        try:
            opened = os.fstat(descriptor)
            digest = hashlib.sha256()
            observed_size = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                observed_size += len(chunk)
                digest.update(chunk)
            terminal = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(terminal.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or _dependency_file_identity(before)
            != _dependency_file_identity(opened)
            or _dependency_file_identity(opened)
            != _dependency_file_identity(terminal)
            or _dependency_file_identity(terminal)
            != _dependency_file_identity(after)
            or observed_size != int(before.st_size)
        ):
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_TREE_DRIFT", "dependency file changed during inventory"
            )
        row = {
            "path": relative,
            "sha256": digest.hexdigest(),
            "size_bytes": observed_size,
            "type": "file",
        }
        rows.append(row)
        files[relative] = row
        file_count += 1
        total_bytes += observed_size
        if (
            file_count > _MAX_DEPENDENCY_FILES
            or total_bytes > _MAX_DEPENDENCY_TOTAL_BYTES
        ):
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_RESOURCE_BUDGET_EXCEEDED",
                "dependency tree exceeds its authority bound",
            )
    second = sorted(
        (
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if not _dependency_path_is_excluded(path.relative_to(root).as_posix())
        ),
        key=lambda value: (value.casefold(), value),
    )
    if [relative for relative, _path in first_paths] != second:
        raise CapturedPaperBenchmarkAuthorityError(
            "DEPENDENCY_TREE_DRIFT", "dependency tree changed during inventory"
        )
    body = {
        "entries": rows,
        "exclusion_policy": DEPENDENCY_EXCLUSION_POLICY,
        "schema_version": DEPENDENCY_TREE_SCHEMA_VERSION,
    }
    return {
        "directory_count": directory_count,
        "exclusion_policy": DEPENDENCY_EXCLUSION_POLICY,
        "file_count": file_count,
        "files": files,
        "total_bytes": total_bytes,
        "tree_sha256": _sha256_bytes(_canonical_json_bytes(body)),
    }


def _dependency_identity(
    *, root: Path, python_executable: Path, python_executable_sha256: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    tree = _inventory_dependency_tree(root)
    if not (
        root.name.casefold() == "site-packages"
        and root.parent.name.casefold() == str(tree["tree_sha256"])
        and root.parent.parent.name.casefold() == "dependencies"
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "DEPENDENCY_ROOT_NOT_SEALED",
            "dependency root is not in the content-addressed capsule layout",
        )
    metadata = os.stat(root, follow_symlinks=False)
    body = {
        "path": str(root),
        "python_executable_path": str(python_executable),
        "python_executable_sha256": python_executable_sha256,
        "schema_version": DEPENDENCY_ROOT_IDENTITY_SCHEMA_VERSION,
        "st_dev": int(metadata.st_dev),
        "st_ino": int(metadata.st_ino),
        "st_mode": int(metadata.st_mode),
        "st_mtime_ns": int(metadata.st_mtime_ns),
        "tree_directory_count": tree["directory_count"],
        "tree_exclusion_policy": tree["exclusion_policy"],
        "tree_file_count": tree["file_count"],
        "tree_sha256": tree["tree_sha256"],
        "tree_total_bytes": tree["total_bytes"],
    }
    return body, tree


def _dependency_distributions(
    root: Path, tree: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    files = tree["files"]
    for required_name in ("psutil", "zstandard"):
        candidates = sorted(
            path
            for path in root.glob("*.dist-info/METADATA")
            if path.parent.name.casefold().startswith(f"{required_name}-")
        )
        if len(candidates) != 1:
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_DISTRIBUTION_INVALID",
                f"sealed dependency root must contain exactly one {required_name} distribution",
            )
        metadata_path = candidates[0]
        relative = metadata_path.relative_to(root).as_posix()
        row = files.get(relative)
        if not isinstance(row, Mapping):
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_DISTRIBUTION_INVALID",
                f"{required_name} metadata is outside the sealed inventory",
            )
        raw = metadata_path.read_bytes()
        if len(raw) > 1024 * 1024 or _sha256_bytes(raw) != row.get("sha256"):
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_TREE_DRIFT", "dependency metadata changed"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_DISTRIBUTION_INVALID", "dependency metadata is not UTF-8"
            ) from exc
        names = [line[6:].strip() for line in text.splitlines() if line.startswith("Name: ")]
        versions = [
            line[9:].strip() for line in text.splitlines() if line.startswith("Version: ")
        ]
        if (
            names != [required_name]
            or len(versions) != 1
            or _VERSION_RE.fullmatch(versions[0]) is None
        ):
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_DISTRIBUTION_INVALID",
                f"{required_name} distribution metadata is malformed",
            )
        import_root = root / required_name
        if not import_root.is_dir():
            raise CapturedPaperBenchmarkAuthorityError(
                "DEPENDENCY_DISTRIBUTION_INVALID",
                f"{required_name} import root is absent",
            )
        _reject_reparse_chain(import_root, field=f"dependency.{required_name}")
        result.append(
            {
                "import_root": import_root.relative_to(root).as_posix(),
                "metadata_path": relative,
                "metadata_sha256": row["sha256"],
                "name": required_name,
                "version": versions[0],
            }
        )
    return result


def _storage_volume_identity(path: Path) -> tuple[Mapping[str, Any], str]:
    metadata = os.stat(path, follow_symlinks=False)
    body = {
        "normalized_anchor": os.path.normcase(
            os.path.normpath(path.anchor or os.sep)
        ),
        "schema_version": STORAGE_VOLUME_IDENTITY_SCHEMA_VERSION,
        "st_dev": int(metadata.st_dev),
    }
    return body, _sha256_bytes(_canonical_json_bytes(body))


def _validated_benchmark_arguments(
    arguments: Sequence[str], *, output_root: Path
) -> tuple[str, ...]:
    if isinstance(arguments, (str, bytes)):
        raise CapturedPaperBenchmarkAuthorityError(
            "BENCHMARK_ARGUMENTS_INVALID", "benchmark arguments must be an argv sequence"
        )
    values = tuple(arguments)
    if (
        not values
        or len(values) > _MAX_ARGUMENTS
        or any(type(value) is not str or not value or "\x00" in value for value in values)
        or sum(len(value.encode("utf-8")) for value in values) > _MAX_ARGUMENT_BYTES
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "BENCHMARK_ARGUMENTS_INVALID", "benchmark argv is empty or unbounded"
        )
    seen: set[str] = set()
    index = 0
    while index < len(values):
        option = values[index]
        if option == "--output-root" or option.startswith("--output-root="):
            raise CapturedPaperBenchmarkAuthorityError(
                "OUTPUT_ARGUMENT_FORBIDDEN", "output root is supplied by authority"
            )
        if option in _FLAG_ARGUMENTS:
            if option in seen:
                raise CapturedPaperBenchmarkAuthorityError(
                    "BENCHMARK_ARGUMENT_DUPLICATE", "benchmark option is duplicated"
                )
            seen.add(option)
            index += 1
            continue
        if option not in _VALUE_ARGUMENTS and option not in _CHOICE_ARGUMENTS:
            raise CapturedPaperBenchmarkAuthorityError(
                "BENCHMARK_ARGUMENT_UNKNOWN", "benchmark argv contains an unknown token"
            )
        if option in seen or index + 1 >= len(values):
            raise CapturedPaperBenchmarkAuthorityError(
                "BENCHMARK_ARGUMENT_DUPLICATE", "benchmark option is duplicate or incomplete"
            )
        raw = values[index + 1]
        if not raw or raw.startswith("--") or "\x00" in raw:
            raise CapturedPaperBenchmarkAuthorityError(
                "BENCHMARK_ARGUMENT_VALUE_INVALID", "benchmark option value is invalid"
            )
        seen.add(option)
        if option in _CHOICE_ARGUMENTS:
            if raw not in _CHOICE_ARGUMENTS[option]:
                raise CapturedPaperBenchmarkAuthorityError(
                    "BENCHMARK_ARGUMENT_VALUE_INVALID", "benchmark choice is unsupported"
                )
        else:
            kind, minimum, maximum = _VALUE_ARGUMENTS[option]
            try:
                if kind == "int":
                    if re.fullmatch(r"[1-9][0-9]*", raw) is None:
                        raise ValueError
                    numeric = float(int(raw))
                else:
                    numeric = float(raw)
            except (ValueError, OverflowError) as exc:
                raise CapturedPaperBenchmarkAuthorityError(
                    "BENCHMARK_ARGUMENT_VALUE_INVALID", "benchmark numeric value is invalid"
                ) from exc
            if not math.isfinite(numeric) or numeric < minimum or numeric > maximum:
                raise CapturedPaperBenchmarkAuthorityError(
                    "BENCHMARK_ARGUMENT_VALUE_INVALID", "benchmark numeric value is out of bounds"
                )
        index += 2
    if "--keep" not in seen:
        raise CapturedPaperBenchmarkAuthorityError(
            "BENCHMARK_RETENTION_REQUIRED", "terminal authority requires --keep"
        )
    return ("--output-root", str(output_root), *values)


def _fsync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
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
            0x40000000,
            0x00000007,
            None,
            3,
            0x02000000,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        try:
            if not kernel32.FlushFileBuffers(handle):
                raise OSError(ctypes.get_last_error(), "FlushFileBuffers failed")
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "DIRECTORY_DURABILITY_FAILED", "authority directory flush failed"
        ) from exc


def _ensure_private_directory(path: Path, *, root: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        child = current / part
        try:
            child.mkdir(mode=0o700, exist_ok=False)
            _fsync_directory(current)
        except FileExistsError:
            if not child.is_dir():
                raise CapturedPaperBenchmarkAuthorityError(
                    "ARTIFACT_PATH_CONFLICT", "authority parent is not a directory"
                )
        _reject_reparse_chain(child, field="authority_directory")
        child.chmod(0o700)
        current = child


def _publish_new_json(root: Path, *, kind: str, raw: bytes) -> tuple[Path, str]:
    digest = _sha256_bytes(raw)
    parent = root / "authority" / kind / digest[:2]
    _ensure_private_directory(parent, root=root)
    target = parent / f"{digest}.json"
    descriptor = -1
    staging: Path | None = None
    linked = False
    try:
        descriptor, staging_raw = tempfile.mkstemp(
            prefix=".pending-", suffix=".json", dir=parent
        )
        staging = Path(staging_raw)
        os.chmod(staging, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(staging, target)
        linked = True
        _fsync_directory(parent)
        staging.unlink()
        staging = None
        _fsync_directory(parent)
    except FileExistsError as exc:
        raise CapturedPaperBenchmarkAuthorityError(
            "APPEND_ONLY_CONFLICT", f"{kind} authority already exists"
        ) from exc
    except BaseException as exc:
        if linked and os.path.lexists(target):
            try:
                target.unlink()
                _fsync_directory(parent)
            except (OSError, CapturedPaperBenchmarkAuthorityError):
                pass
        if isinstance(exc, CapturedPaperBenchmarkAuthorityError):
            raise
        raise CapturedPaperBenchmarkAuthorityError(
            "PUBLISH_FAILED", f"{kind} authority publication failed"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if staging is not None and os.path.lexists(staging):
            try:
                staging.unlink()
                _fsync_directory(parent)
            except (OSError, CapturedPaperBenchmarkAuthorityError):
                pass
    observed = target.read_bytes()
    if observed != raw or _sha256_bytes(observed) != digest:
        raise CapturedPaperBenchmarkAuthorityError(
            "PUBLISH_VERIFY_FAILED", f"{kind} authority did not reread exactly"
        )
    return target.resolve(strict=True), digest


def build_captured_paper_benchmark_authority(
    *,
    repo_root: str | Path,
    authority_root: str | Path,
    expected_git_commit: str,
    git_executable: str | Path,
    git_executable_sha256: str,
    expected_source_sha256: Mapping[str, str],
    builder_source_path: str | Path,
    builder_source_sha256: str,
    runner_source_path: str | Path,
    runner_source_sha256: str,
    python_executable: str | Path,
    python_executable_sha256: str,
    python_dependency_root: str | Path,
    python_dependency_root_identity_sha256: str,
    benchmark_output_root: str | Path,
    benchmark_arguments: Sequence[str],
) -> BuiltCapturedPaperBenchmarkAuthority:
    """Publish one inert prebenchmark manifest and exact launch receipt."""

    commit = str(expected_git_commit or "").lower()
    if _GIT_COMMIT_RE.fullmatch(commit) is None:
        raise CapturedPaperBenchmarkAuthorityError(
            "GIT_COMMIT_INVALID", "expected_git_commit must be one full lowercase commit"
        )
    if not isinstance(expected_source_sha256, Mapping) or set(
        expected_source_sha256
    ) != set(SOURCE_ROLE_PATHS):
        raise CapturedPaperBenchmarkAuthorityError(
            "SOURCE_ROSTER_INVALID", "source hash map must contain the exact seven roles"
        )
    expected_hashes = {
        role: _sha256(expected_source_sha256[role], field=f"source.{role}")
        for role in SOURCE_ROLE_PATHS
    }
    expected_python_sha = _sha256(
        python_executable_sha256, field="python_executable_sha256"
    )
    expected_git_sha = _sha256(git_executable_sha256, field="git_executable_sha256")
    expected_builder_sha = _sha256(
        builder_source_sha256, field="builder_source_sha256"
    )
    expected_runner_sha = _sha256(
        runner_source_sha256, field="runner_source_sha256"
    )
    expected_dependency_identity = _sha256(
        python_dependency_root_identity_sha256,
        field="python_dependency_root_identity_sha256",
    )
    held_builder_sha = globals().get("__chili_held_builder_source_sha256__")
    held_python_sha = globals().get("__chili_held_python_executable_sha256__")
    if held_builder_sha is not None and held_builder_sha != expected_builder_sha:
        raise CapturedPaperBenchmarkAuthorityError(
            "HELD_BUILDER_AUTHORITY_MISMATCH",
            "builder argument differs from held-source authority",
        )
    if held_python_sha is not None and held_python_sha != expected_python_sha:
        raise CapturedPaperBenchmarkAuthorityError(
            "HELD_PYTHON_AUTHORITY_MISMATCH",
            "Python argument differs from held-source authority",
        )

    repo = _canonical_directory(repo_root, field="repo_root")
    authority = _canonical_directory(authority_root, field="authority_root")
    dependency = _canonical_directory(
        python_dependency_root, field="python_dependency_root"
    )
    output = _canonical_directory(
        benchmark_output_root, field="benchmark_output_root"
    )
    executable = _canonical_file(python_executable, field="python_executable")
    git = _canonical_file(git_executable, field="git_executable")
    builder_path = _canonical_file(builder_source_path, field="builder_source")
    runner_path = _canonical_file(runner_source_path, field="runner_source")
    if (
        builder_path != (repo / AUTHORITY_PROGRAM_PATHS["builder"]).resolve(strict=True)
        or runner_path != (repo / AUTHORITY_PROGRAM_PATHS["runner"]).resolve(strict=True)
        or builder_path == runner_path
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "AUTHORITY_PROGRAM_PATH_MISMATCH",
            "builder and runner must use their exact candidate paths",
        )
    roots = (repo, authority, dependency, output)
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if _overlap(left, right):
                raise CapturedPaperBenchmarkAuthorityError(
                    "AUTHORITY_ROOT_OVERLAP", "authority security roots overlap"
                )
    if any(authority.iterdir()):
        raise CapturedPaperBenchmarkAuthorityError(
            "AUTHORITY_ROOT_NOT_EMPTY", "authority_root must be caller-created and empty"
        )
    if any(output.iterdir()):
        raise CapturedPaperBenchmarkAuthorityError(
            "OUTPUT_ROOT_NOT_EMPTY", "benchmark output root must initially be empty"
        )
    output_identity = dict(_directory_identity(output))
    volume_body, volume_sha = _storage_volume_identity(output)
    full_benchmark_arguments = _validated_benchmark_arguments(
        benchmark_arguments, output_root=output
    )

    python_pin = _pin_file(
        executable, field="python_executable", max_bytes=_MAX_EXECUTABLE_BYTES
    )
    if python_pin.sha256 != expected_python_sha:
        raise CapturedPaperBenchmarkAuthorityError(
            "PYTHON_HASH_MISMATCH", "Python executable differs from its external hash"
        )
    _probe_python311(executable)
    _recheck_pin(python_pin, field="python_executable")
    git_pin = _pin_file(git, field="git_executable", max_bytes=_MAX_EXECUTABLE_BYTES)
    if git_pin.sha256 != expected_git_sha:
        raise CapturedPaperBenchmarkAuthorityError(
            "GIT_HASH_MISMATCH", "Git executable differs from its external hash"
        )
    builder_pin = _pin_file(
        builder_path, field="builder_source", max_bytes=_MAX_SOURCE_BYTES
    )
    runner_pin = _pin_file(
        runner_path, field="runner_source", max_bytes=_MAX_SOURCE_BYTES
    )
    if builder_pin.sha256 != expected_builder_sha or runner_pin.sha256 != expected_runner_sha:
        raise CapturedPaperBenchmarkAuthorityError(
            "AUTHORITY_PROGRAM_HASH_MISMATCH",
            "builder or runner differs from its external hash",
        )

    source_pins: dict[str, _PinnedFile] = {}
    source_total = 0
    for role, relative in SOURCE_ROLE_PATHS.items():
        path = _canonical_file(repo / relative, field=f"source.{role}")
        if path != (repo / relative).resolve(strict=True):
            raise CapturedPaperBenchmarkAuthorityError(
                "SOURCE_PATH_MISMATCH", f"source.{role} escaped its exact role path"
            )
        pin = _pin_file(path, field=f"source.{role}", max_bytes=_MAX_SOURCE_BYTES)
        if pin.sha256 != expected_hashes[role]:
            raise CapturedPaperBenchmarkAuthorityError(
                "SOURCE_HASH_MISMATCH", f"source.{role} differs from its external hash"
            )
        source_total += pin.identity[2]
        if source_total > _MAX_SOURCE_TOTAL_BYTES:
            raise CapturedPaperBenchmarkAuthorityError(
                "SOURCE_RESOURCE_BUDGET_EXCEEDED", "source closure exceeds its bound"
            )
        source_pins[role] = pin

    _verify_git_worktree(
        git_pin=git_pin,
        candidate_root=repo,
        expected_commit=commit,
        expected_tracked_paths=_expected_tracked_paths(),
    )
    loader = _extract_held_loader(source_pins["pressure_probe"])
    loader_sha = _sha256_bytes(loader.encode("utf-8"))

    dependency_body, dependency_tree = _dependency_identity(
        root=dependency,
        python_executable=executable,
        python_executable_sha256=expected_python_sha,
    )
    observed_dependency_identity = _sha256_bytes(
        _canonical_json_bytes(dict(dependency_body))
    )
    if observed_dependency_identity != expected_dependency_identity:
        raise CapturedPaperBenchmarkAuthorityError(
            "DEPENDENCY_IDENTITY_MISMATCH",
            "sealed dependency root differs from its external identity",
        )
    distributions = _dependency_distributions(dependency, dependency_tree)

    source_roster = [
        {
            "path": str(source_pins[role].path),
            "role": role,
            "sha256": source_pins[role].sha256,
        }
        for role in sorted(source_pins)
    ]
    source_roster_sha = _sha256_bytes(_canonical_json_bytes(source_roster))
    posture = {
        "benchmark_execution_authorized": True,
        "broker_contact_authorized": False,
        "database_access_authorized": False,
        "host_activation_authorized": False,
        "live_cash_authorized": False,
        "order_submission_authorized": False,
        "provider_contact_authorized": False,
    }
    execution_environment = dict(_sanitized_execution_environment())
    execution_environment_sha = _sha256_bytes(
        _canonical_json_bytes(execution_environment)
    )
    execution_context = {
        "cwd": str(repo),
        "environment": execution_environment,
        "environment_sha256": execution_environment_sha,
        "shell": False,
        "stderr": "bounded_binary_pipe_1mib",
        "stdin": "devnull",
        "stdout": "bounded_binary_pipe_64mib",
        "timeout_seconds": _BENCHMARK_TIMEOUT_SECONDS,
    }
    manifest_document = {
        "account_scope": ACCOUNT_SCOPE,
        "authority_mode": AUTHORITY_MODE,
        "benchmark_arguments": list(full_benchmark_arguments),
        "candidate_root": str(repo),
        "expected_benchmark_schema_version": EXPECTED_BENCHMARK_SCHEMA_VERSION,
        "expected_git_commit": commit,
        "execution_context": execution_context,
        "git": {"executable_path": str(git), "executable_sha256": expected_git_sha},
        "held_loader": {
            "sha256": loader_sha,
            "source_role": "pressure_probe",
            "variable": HELD_LOADER_VARIABLE,
        },
        "authority_programs": {
            "builder": {"path": str(builder_path), "sha256": expected_builder_sha},
            "runner": {"path": str(runner_path), "sha256": expected_runner_sha},
        },
        "output": {
            "root": str(output),
            "root_identity": output_identity,
            "storage_volume_identity": dict(volume_body),
            "storage_volume_identity_sha256": volume_sha,
        },
        "posture": posture,
        "python": {
            "executable_path": str(executable),
            "executable_sha256": expected_python_sha,
            "implementation": "cpython",
            "isolation_flags": ["-I", "-S", "-B"],
            "version": [3, 11],
        },
        "python_dependency_root": {
            "identity": dict(dependency_body),
            "identity_sha256": observed_dependency_identity,
            "path": str(dependency),
            "required_distributions": distributions,
            "tree_sha256": dependency_tree["tree_sha256"],
        },
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source_roster": source_roster,
        "source_roster_sha256": source_roster_sha,
    }
    manifest_raw = _canonical_json_bytes(manifest_document)
    manifest_path, manifest_sha = _publish_new_json(
        authority, kind="manifest", raw=manifest_raw
    )

    loader_bootstrap: list[str] = []
    for role in LOADER_ROLE_ORDER:
        loader_bootstrap.extend(
            (str(source_pins[role].path), source_pins[role].sha256)
        )
    loader_bootstrap.extend(
        (
            str(dependency),
            observed_dependency_identity,
            str(manifest_path),
            manifest_sha,
            expected_python_sha,
        )
    )
    benchmark_argv = (
        str(executable),
        "-I",
        "-S",
        "-B",
        "-c",
        loader,
        *loader_bootstrap,
        "--",
        *full_benchmark_arguments,
    )
    receipt_document = {
        "account_scope": ACCOUNT_SCOPE,
        "argv_is_shell_string": False,
        "authority_mode": AUTHORITY_MODE,
        "benchmark_argv": list(benchmark_argv),
        "benchmark_completed": False,
        "benchmark_report": None,
        "candidate_root": str(repo),
        "expected_git_commit": commit,
        "execution_context": execution_context,
        "git": {"executable_path": str(git), "executable_sha256": expected_git_sha},
        "authority_programs": manifest_document["authority_programs"],
        "held_loader_sha256": loader_sha,
        "invoked": False,
        "manifest": {"path": str(manifest_path), "sha256": manifest_sha},
        "output": {
            "root": str(output),
            "root_identity": output_identity,
            "storage_volume_identity_sha256": volume_sha,
        },
        "posture": {
            "benchmark_output_written": False,
            "broker_contacted": False,
            "cutover_performed": False,
            "database_accessed": False,
            "host_activation_performed": False,
            "live_cash_authorized": False,
            "orders_submitted": False,
            "provider_contacted": False,
            "task_scheduler_mutated": False,
        },
        "python": {
            "executable_path": str(executable),
            "executable_sha256": expected_python_sha,
        },
        "python_dependency_root": {
            "identity_sha256": observed_dependency_identity,
            "path": str(dependency),
            "tree_sha256": dependency_tree["tree_sha256"],
        },
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "source_roster": source_roster,
        "source_roster_sha256": source_roster_sha,
    }
    receipt_raw = _canonical_json_bytes(receipt_document)

    # The launch receipt is the executable capability.  Keep it unpublished
    # until every independently mutable input and the already-published
    # manifest have passed their last drift check.  A failed final check may
    # leave an inert orphan manifest, but can never leave a usable receipt.
    for role, pin in source_pins.items():
        _recheck_pin(pin, field=f"source.{role}")
    _recheck_pin(python_pin, field="python_executable")
    _recheck_pin(git_pin, field="git_executable")
    _recheck_pin(builder_pin, field="builder_source")
    _recheck_pin(runner_pin, field="runner_source")
    final_dependency_body, final_dependency_tree = _dependency_identity(
        root=dependency,
        python_executable=executable,
        python_executable_sha256=expected_python_sha,
    )
    if (
        _sha256_bytes(_canonical_json_bytes(dict(final_dependency_body)))
        != observed_dependency_identity
        or final_dependency_tree["tree_sha256"] != dependency_tree["tree_sha256"]
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "DEPENDENCY_ROOT_DRIFT",
            "sealed dependency root changed before receipt publication",
        )
    if dict(_directory_identity(output)) != output_identity or any(output.iterdir()):
        raise CapturedPaperBenchmarkAuthorityError(
            "OUTPUT_ROOT_DRIFT", "benchmark output root changed after publication"
        )
    _verify_git_worktree(
        git_pin=git_pin,
        candidate_root=repo,
        expected_commit=commit,
        expected_tracked_paths=_expected_tracked_paths(),
    )
    if manifest_path.read_bytes() != manifest_raw:
        raise CapturedPaperBenchmarkAuthorityError(
            "PUBLISHED_AUTHORITY_DRIFT", "published manifest bytes changed"
        )
    receipt_path, receipt_sha = _publish_new_json(
        authority, kind="receipt", raw=receipt_raw
    )
    runner_loader = _extract_runner_loader(runner_pin)
    runner_argv = (
        str(executable), "-I", "-S", "-B", "-c", runner_loader,
        str(builder_path), expected_builder_sha,
        str(runner_path), expected_runner_sha, expected_python_sha,
        "--", "--receipt", str(receipt_path),
        "--receipt-sha256", receipt_sha,
    )
    runner_authority_document = {
        "account_scope": ACCOUNT_SCOPE,
        "authority_mode": AUTHORITY_MODE,
        "argv_is_shell_string": False,
        "execution_context": execution_context,
        "git": {"executable_path": str(git), "executable_sha256": expected_git_sha},
        "launch_receipt": {"path": str(receipt_path), "sha256": receipt_sha},
        "manifest": {"path": str(manifest_path), "sha256": manifest_sha},
        "posture": receipt_document["posture"],
        "python": receipt_document["python"],
        "runner_argv": list(runner_argv),
        "runner_loader_sha256": _sha256_bytes(runner_loader.encode("utf-8")),
        "schema_version": RUNNER_AUTHORITY_SCHEMA_VERSION,
    }
    for role, pin in source_pins.items():
        _recheck_pin(pin, field=f"source.{role}")
    for field, pin in (
        ("python_executable", python_pin),
        ("git_executable", git_pin),
        ("builder_source", builder_pin),
        ("runner_source", runner_pin),
    ):
        _recheck_pin(pin, field=field)
    terminal_dependency_body, terminal_dependency_tree = _dependency_identity(
        root=dependency,
        python_executable=executable,
        python_executable_sha256=expected_python_sha,
    )
    if (
        _sha256_bytes(_canonical_json_bytes(dict(terminal_dependency_body)))
        != observed_dependency_identity
        or terminal_dependency_tree["tree_sha256"] != dependency_tree["tree_sha256"]
        or dict(_directory_identity(output)) != output_identity
        or any(output.iterdir())
    ):
        raise CapturedPaperBenchmarkAuthorityError(
            "TERMINAL_INPUT_DRIFT",
            "benchmark dependency or output authority changed before publication",
        )
    _verify_git_worktree(
        git_pin=git_pin,
        candidate_root=repo,
        expected_commit=commit,
        expected_tracked_paths=_expected_tracked_paths(),
    )
    if manifest_path.read_bytes() != manifest_raw or receipt_path.read_bytes() != receipt_raw:
        raise CapturedPaperBenchmarkAuthorityError(
            "PUBLISHED_AUTHORITY_DRIFT",
            "manifest or launch receipt changed before runner authority publication",
        )
    # This is the only externally executable capability and is deliberately
    # last: every manifest/receipt/input recheck above has completed.
    runner_authority_path, runner_authority_sha = _publish_new_json(
        authority, kind="runner-authority", raw=_canonical_json_bytes(runner_authority_document)
    )
    return BuiltCapturedPaperBenchmarkAuthority(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha,
        benchmark_argv=benchmark_argv,
        runner_argv=runner_argv,
        runner_authority_path=runner_authority_path,
        runner_authority_sha256=runner_authority_sha,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--authority-root", required=True)
    parser.add_argument("--expected-git-commit", required=True)
    parser.add_argument("--git-executable", required=True)
    parser.add_argument("--git-executable-sha256", required=True)
    parser.add_argument("--source-sha256", action="append", required=True)
    parser.add_argument("--builder-source-path", required=True)
    parser.add_argument("--builder-source-sha256", required=True)
    parser.add_argument("--runner-source-path", required=True)
    parser.add_argument("--runner-source-sha256", required=True)
    parser.add_argument("--python-executable", required=True)
    parser.add_argument("--python-executable-sha256", required=True)
    parser.add_argument("--python-dependency-root", required=True)
    parser.add_argument("--python-dependency-root-identity-sha256", required=True)
    parser.add_argument("--benchmark-output-root", required=True)
    parser.add_argument("benchmark_arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(argv) if argv is not None else None)
    source_hashes: dict[str, str] = {}
    try:
        for item in arguments.source_sha256:
            role, separator, digest = str(item).partition("=")
            if not separator or role in source_hashes:
                raise CapturedPaperBenchmarkAuthorityError(
                    "SOURCE_ROSTER_INVALID", "--source-sha256 must be unique ROLE=SHA"
                )
            source_hashes[role] = digest
        benchmark_arguments = list(arguments.benchmark_arguments)
        if benchmark_arguments[:1] == ["--"]:
            benchmark_arguments = benchmark_arguments[1:]
        built = build_captured_paper_benchmark_authority(
            repo_root=arguments.repo_root,
            authority_root=arguments.authority_root,
            expected_git_commit=arguments.expected_git_commit,
            git_executable=arguments.git_executable,
            git_executable_sha256=arguments.git_executable_sha256,
            expected_source_sha256=source_hashes,
            builder_source_path=arguments.builder_source_path,
            builder_source_sha256=arguments.builder_source_sha256,
            runner_source_path=arguments.runner_source_path,
            runner_source_sha256=arguments.runner_source_sha256,
            python_executable=arguments.python_executable,
            python_executable_sha256=arguments.python_executable_sha256,
            python_dependency_root=arguments.python_dependency_root,
            python_dependency_root_identity_sha256=(
                arguments.python_dependency_root_identity_sha256
            ),
            benchmark_output_root=arguments.benchmark_output_root,
            benchmark_arguments=benchmark_arguments,
        )
    except CapturedPaperBenchmarkAuthorityError as exc:
        sys.stderr.write(exc.code + "\n")
        return 2
    sys.stdout.buffer.write(
        _canonical_json_bytes(
            {
                "manifest": {
                    "path": str(built.manifest_path),
                    "sha256": built.manifest_sha256,
                },
                "receipt": {
                    "path": str(built.receipt_path),
                    "sha256": built.receipt_sha256,
                },
                "runner_authority": {
                    "path": str(built.runner_authority_path),
                    "sha256": built.runner_authority_sha256,
                },
                "schema_version": RECEIPT_SCHEMA_VERSION,
            }
        )
        + b"\n"
    )
    return 0


if __name__ == "__main__":
    held_builder_sha = globals().get("__chili_held_builder_source_sha256__")
    held_python_sha = globals().get("__chili_held_python_executable_sha256__")
    if (
        not isinstance(held_builder_sha, str)
        or _SHA256_RE.fullmatch(held_builder_sha) is None
        or not isinstance(held_python_sha, str)
        or _SHA256_RE.fullmatch(held_python_sha) is None
    ):
        raise SystemExit("BUILDER_HELD_BOOTSTRAP_REQUIRED")
    raise SystemExit(main())
