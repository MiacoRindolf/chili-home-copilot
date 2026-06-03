"""Adaptive pytest defaults for CHILI local and CI runs.

The goal is to keep pytest bounded without baking one workstation's limits into
the repo. Values here are derived from the selected test scope, available CPU
count, xdist hints, and explicit environment overrides.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

SECONDS_PER_MINUTE = 60
MILLISECONDS_PER_SECOND = 1_000
POSTGRES_IDENTIFIER_MAX_LENGTH = 63
POSTGRES_ADVISORY_LOCK_BYTES_PER_KEY = 4
BITS_PER_BYTE = 8
PYTEST_TEST_FILE_PREFIX = "test_"
PYTEST_TEST_FILE_SUFFIX = ".py"
PYTEST_DEFAULT_TEST_ROOT = "tests"
PYTEST_OPTIONS_WITH_VALUES = frozenset(
    {
        "-k",
        "-m",
        "-n",
        "-o",
        "-p",
        "--basetemp",
        "--confcutdir",
        "--cov",
        "--cov-config",
        "--cov-report",
        "--deselect",
        "--ignore",
        "--ignore-glob",
        "--junitxml",
        "--log-cli-level",
        "--log-level",
        "--maxfail",
        "--numprocesses",
        "--override-ini",
        "--rootdir",
        "--tb",
        "--timeout",
    }
)
SUPPORTED_PYTEST_SPEC = "pytest>=8.2,<9"
PYTEST_RUNTIME_SCHEMA_VERSION = "chili.pytest-runtime.v1"
PYTEST_RUNTIME_ENV_DIR = ".pytest_venv"
PYTEST_UNSUPPORTED_EXIT_CODE = 79
PYTEST_PYTHON_ENV_VAR = "CHILI_PYTEST_PYTHON"
PYTEST_RUNTIME_PROBE_TIMEOUT_SECONDS = 10
PYTEST_RUNTIME_REQUIRED_IMPORTS = (
    "fastapi",
    "httpx",
    "pydantic",
    "pydantic_settings",
    "sqlalchemy",
    "ta",
)
PYTEST_LOCAL_ENV_DIRS = (
    ".venv",
    "venv",
    ".pytest_venv",
    ".pytest-venv",
)


@dataclass(frozen=True)
class PytestAdaptiveProfile:
    repo_root: str
    cpu_count: int
    xdist_worker_count: int
    selected_test_file_count: int
    db_slot_count: int
    db_pool_timeout_seconds: int
    per_test_timeout_seconds: int
    wrapper_timeout_seconds: int
    heartbeat_interval_seconds: int
    poll_interval_seconds: int
    truncate_attempts: int
    truncate_lock_timeout_seconds: int
    db_lock_wait_seconds: int
    db_lock_stale_seconds: int
    db_lock_reap_interval_seconds: int
    db_lock_poll_seconds: float
    drop_timeout_milliseconds: int
    stale_cleanup_min_age_minutes: int


@dataclass(frozen=True)
class PytestRuntimeContract:
    status: str
    required: str
    actual: str
    python: str
    source: str
    candidate_count: int
    isolation_status: str
    include_system_site_packages: bool | None
    isolation_recovery: str
    missing_imports: tuple[str, ...]
    dependency_recovery: str
    recovery: str

    @property
    def passed(self) -> bool:
        return self.status == "passed"


@dataclass(frozen=True)
class PytestRuntimeCandidate:
    source: str
    python: str
    actual: str
    supported: bool
    isolation_status: str
    include_system_site_packages: bool | None
    missing_imports: tuple[str, ...]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def available_cpu_count() -> int:
    try:
        affinity = os.sched_getaffinity(0)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        affinity = None
    if affinity:
        return max(1, len(affinity))
    return max(1, os.cpu_count() or 1)


def _env_int(name: str, *, minimum: int | None = None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    value = int(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}; got {value}")
    return value


def _env_float(name: str, *, minimum: float | None = None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    value = float(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}; got {value}")
    return value


def _coerce_positive_int_token(token: str, *, cpu_count: int) -> int | None:
    lowered = token.strip().lower()
    if lowered in {"auto", "logical"}:
        return cpu_count
    try:
        return max(1, int(lowered))
    except ValueError:
        return None


def _iter_pytest_args(args: Sequence[str] | None) -> list[str]:
    return [str(arg) for arg in (args or []) if str(arg) != "--"]


def _version_parts(raw: str) -> tuple[int, int, int]:
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits or "0"))
        if len(parts) == 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def installed_pytest_version() -> str:
    try:
        return importlib.metadata.version("pytest")
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def _normalize_python_path(path: Path, *, root: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = root / expanded
    try:
        return expanded.resolve()
    except OSError:
        return expanded.absolute()


def _venv_python_path(path: Path) -> Path:
    if os.name == "nt":
        return path / "Scripts" / "python.exe"
    return path / "bin" / "python"


def _command_text(command: Sequence[str | Path]) -> str:
    parts = [str(part) for part in command]
    if os.name == "nt":
        quoted: list[str] = []
        for part in parts:
            if any(char in part for char in " \t<>&|^'\""):
                quoted.append("'" + part.replace("'", "''") + "'")
            else:
                quoted.append(part)
        return " ".join(quoted)
    return shlex.join(parts)


def default_runtime_env_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / PYTEST_RUNTIME_ENV_DIR


def runtime_env_python(env_dir: Path | None = None, *, root: Path | None = None) -> Path:
    return _venv_python_path(env_dir or default_runtime_env_dir(root))


def runtime_create_command(
    env_dir: Path | None = None,
    *,
    root: Path | None = None,
    clear: bool = False,
) -> list[str]:
    command = [sys.executable, "-m", "venv"]
    if clear:
        command.append("--clear")
    command.append(str(env_dir or default_runtime_env_dir(root)))
    return command


def runtime_install_command(env_dir: Path | None = None, *, root: Path | None = None) -> list[str]:
    resolved_root = root or repo_root()
    return [
        str(runtime_env_python(env_dir, root=root)),
        "-m",
        "pip",
        "install",
        "-r",
        str(resolved_root / "requirements.txt"),
        SUPPORTED_PYTEST_SPEC,
    ]


def runtime_env_var_command(env_dir: Path | None = None, *, root: Path | None = None) -> str:
    python = str(runtime_env_python(env_dir, root=root))
    if os.name == "nt":
        return f"$env:{PYTEST_PYTHON_ENV_VAR}={python!r}"
    return f"export {PYTEST_PYTHON_ENV_VAR}={shlex.quote(python)}"


def _venv_dir_for_python(python: str | Path) -> Path | None:
    path = Path(python)
    parent_name = path.parent.name.lower()
    if os.name == "nt" and parent_name == "scripts":
        return path.parent.parent
    if os.name != "nt" and parent_name == "bin":
        return path.parent.parent
    return None


def _pyvenv_config(python: str | Path) -> dict[str, str]:
    venv_dir = _venv_dir_for_python(python)
    if venv_dir is None:
        return {}
    cfg_path = venv_dir / "pyvenv.cfg"
    if not cfg_path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = cfg_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().lower()] = value.strip()
    return values


def pytest_runtime_isolation(
    python: str | Path,
    *,
    source: str,
) -> tuple[str, bool | None]:
    if source == "current":
        return "shared_runtime", None
    config = _pyvenv_config(python)
    if not config:
        return "unknown", None
    raw = config.get("include-system-site-packages", "").strip().lower()
    if raw in {"true", "1", "yes"}:
        return "shared_site_packages", True
    if raw in {"false", "0", "no"}:
        return "isolated", False
    return "unknown", None


def pytest_runtime_isolation_recovery(status: str) -> str:
    if status == "isolated":
        return "none"
    if status == "shared_site_packages":
        return (
            "run `python scripts/pytest_adaptive.py ensure-runtime --create` to "
            "recreate the repo-local pytest runtime without system site packages "
            "before treating full benchmark evidence as promotion-grade"
        )
    if status == "shared_runtime":
        return "create or select a repo-local pytest runtime before judging coding quality"
    return "inspect pyvenv.cfg and recreate the repo-local pytest runtime if isolation is unclear"


def _candidate_python_paths(root: Path | None = None) -> list[tuple[str, Path]]:
    resolved_root = root or repo_root()
    candidates: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add(source: str, raw_path: Path) -> None:
        path = _normalize_python_path(raw_path, root=resolved_root)
        key = str(path).lower() if os.name == "nt" else str(path)
        if key in seen or not path.exists():
            return
        seen.add(key)
        candidates.append((source, path))

    explicit = os.environ.get(PYTEST_PYTHON_ENV_VAR, "").strip()
    if explicit:
        add(PYTEST_PYTHON_ENV_VAR, Path(explicit))

    active_venv = os.environ.get("VIRTUAL_ENV", "").strip()
    if active_venv:
        add("VIRTUAL_ENV", _venv_python_path(Path(active_venv)))

    for dirname in PYTEST_LOCAL_ENV_DIRS:
        add(dirname, _venv_python_path(resolved_root / dirname))

    add("current", Path(sys.executable))
    return candidates


def pytest_version_for_python(python: str | Path) -> str:
    path = _normalize_python_path(Path(python), root=repo_root())
    current = _normalize_python_path(Path(sys.executable), root=repo_root())
    if path == current:
        return installed_pytest_version()
    probe = (
        "import importlib.metadata; "
        "print(importlib.metadata.version('pytest'))"
    )
    try:
        result = subprocess.run(
            [str(path), "-c", probe],
            capture_output=True,
            text=True,
            timeout=PYTEST_RUNTIME_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"probe_failed:{type(exc).__name__}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return f"probe_failed:{detail[0] if detail else result.returncode}"
    version = result.stdout.strip().splitlines()
    return version[-1].strip() if version else "missing"


def pytest_version_supported(version: str) -> bool:
    if version == "missing":
        return False
    major, minor, _patch = _version_parts(version)
    return major == 8 and minor >= 2


def pytest_runtime_missing_imports(python: str | Path) -> tuple[str, ...]:
    path = _normalize_python_path(Path(python), root=repo_root())
    current = _normalize_python_path(Path(sys.executable), root=repo_root())
    probe = (
        "import importlib.util, json, sys; "
        "missing=[name for name in sys.argv[1:] if importlib.util.find_spec(name) is None]; "
        "print(json.dumps(missing))"
    )
    command = [
        str(path),
        "-c",
        probe,
        *PYTEST_RUNTIME_REQUIRED_IMPORTS,
    ]
    if path == current:
        missing = [
            name
            for name in PYTEST_RUNTIME_REQUIRED_IMPORTS
            if importlib.util.find_spec(name) is None
        ]
        return tuple(missing)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PYTEST_RUNTIME_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (f"probe_failed:{type(exc).__name__}",)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return (f"probe_failed:{detail[0] if detail else result.returncode}",)
    try:
        payload = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return ("probe_failed:invalid_json",)
    if not isinstance(payload, list):
        return ("probe_failed:invalid_json",)
    return tuple(str(item) for item in payload if str(item).strip())


def pytest_runtime_dependency_recovery(missing_imports: Sequence[str]) -> str:
    if not missing_imports:
        return "none"
    missing = ", ".join(missing_imports)
    return (
        "install the repo test dependencies into the selected pytest runtime "
        f"before judging coding quality; missing imports: {missing}"
    )


def pytest_runtime_recovery(version: str, *, source: str = "current") -> str:
    return (
        f"create or select a repo-local Python environment with {SUPPORTED_PYTEST_SPEC} "
        "before running quality gates; do not judge coding quality from the shared "
        f"runtime while {source} pytest is {version}"
    )


def pytest_runtime_candidates(root: Path | None = None) -> list[PytestRuntimeCandidate]:
    return [
        PytestRuntimeCandidate(
            source=source,
            python=str(path),
            actual=version,
            supported=pytest_version_supported(version),
            isolation_status=isolation[0],
            include_system_site_packages=isolation[1],
            missing_imports=pytest_runtime_missing_imports(path),
        )
        for source, path in _candidate_python_paths(root)
        for version in [pytest_version_for_python(path)]
        for isolation in [pytest_runtime_isolation(path, source=source)]
    ]


def _select_pytest_runtime_candidate(
    candidates: Sequence[PytestRuntimeCandidate],
) -> PytestRuntimeCandidate:
    if candidates and candidates[0].source == PYTEST_PYTHON_ENV_VAR:
        return candidates[0]
    supported_isolated = next(
        (
            candidate
            for candidate in candidates
            if (
                candidate.supported
                and candidate.isolation_status == "isolated"
                and not candidate.missing_imports
            )
        ),
        None,
    )
    if supported_isolated is not None:
        return supported_isolated
    supported_isolated_missing_deps = next(
        (
            candidate
            for candidate in candidates
            if candidate.supported and candidate.isolation_status == "isolated"
        ),
        None,
    )
    if supported_isolated_missing_deps is not None:
        return supported_isolated_missing_deps
    supported = next((candidate for candidate in candidates if candidate.supported), None)
    if supported is not None:
        return supported
    if candidates:
        return candidates[-1]
    return PytestRuntimeCandidate(
        source="missing",
        python=sys.executable,
        actual="missing",
        supported=False,
        isolation_status="unknown",
        include_system_site_packages=None,
        missing_imports=("pytest",),
    )


def pytest_runtime_contract(root: Path | None = None) -> PytestRuntimeContract:
    candidates = pytest_runtime_candidates(root)
    selected = _select_pytest_runtime_candidate(candidates)
    return PytestRuntimeContract(
        status="passed" if selected.supported else "warning",
        required=SUPPORTED_PYTEST_SPEC,
        actual=selected.actual,
        python=selected.python,
        source=selected.source,
        candidate_count=len(candidates),
        isolation_status=selected.isolation_status,
        include_system_site_packages=selected.include_system_site_packages,
        isolation_recovery=pytest_runtime_isolation_recovery(
            selected.isolation_status,
        ),
        missing_imports=selected.missing_imports,
        dependency_recovery=pytest_runtime_dependency_recovery(selected.missing_imports),
        recovery=pytest_runtime_recovery(selected.actual, source=selected.source),
    )


def pytest_runtime_doctor(
    root: Path | None = None,
    *,
    env_dir: Path | None = None,
) -> dict[str, object]:
    resolved_root = root or repo_root()
    resolved_env_dir = env_dir or default_runtime_env_dir(resolved_root)
    contract = pytest_runtime_contract(resolved_root)
    candidates = pytest_runtime_candidates(resolved_root)
    create_command = runtime_create_command(resolved_env_dir, root=resolved_root)
    install_command = runtime_install_command(resolved_env_dir, root=resolved_root)
    next_action = "none"
    if not contract.passed:
        next_action = (
            "Create the repo-local pytest runtime with "
            f"`python scripts/pytest_adaptive.py ensure-runtime --create` "
            f"or point {PYTEST_PYTHON_ENV_VAR} at a Python with {SUPPORTED_PYTEST_SPEC}."
        )
    elif contract.isolation_status != "isolated":
        next_action = contract.isolation_recovery
    elif contract.missing_imports:
        next_action = contract.dependency_recovery
    status = contract.status
    if contract.passed and (
        contract.isolation_status != "isolated" or contract.missing_imports
    ):
        status = "warning"
    return {
        "schema": PYTEST_RUNTIME_SCHEMA_VERSION,
        "status": status,
        "required": SUPPORTED_PYTEST_SPEC,
        "runtime": asdict(contract),
        "candidates": [asdict(candidate) for candidate in candidates],
        "repo_root": str(resolved_root),
        "env_dir": str(resolved_env_dir),
        "env_python": str(runtime_env_python(resolved_env_dir, root=resolved_root)),
        "create_command": _command_text(create_command),
        "install_command": _command_text(install_command),
        "select_command": runtime_env_var_command(resolved_env_dir, root=resolved_root),
        "next_action": next_action,
        "safety": (
            "creates or uses a repo-local virtualenv only; does not mutate the "
            "shared/global Python runtime"
        ),
    }


def build_pytest_command(
    args: Sequence[str] | None = None,
    *,
    python: str | Path | None = None,
) -> list[str]:
    return [str(python or sys.executable), "-m", "pytest", *_iter_pytest_args(args)]


def run_pytest_with_runtime_contract(args: Sequence[str] | None = None) -> int:
    contract = pytest_runtime_contract()
    if not contract.passed:
        print(
            "pytest runtime unsupported: "
            f"required {contract.required}; actual {contract.actual}; "
            f"source {contract.source}; python {contract.python}. "
            f"Recovery: {contract.recovery}.",
            file=sys.stderr,
        )
        return PYTEST_UNSUPPORTED_EXIT_CODE
    if contract.missing_imports:
        print(
            "pytest runtime incomplete: "
            f"missing imports {', '.join(contract.missing_imports)}; "
            f"source {contract.source}; python {contract.python}. "
            f"Recovery: {contract.dependency_recovery}.",
            file=sys.stderr,
        )
        return PYTEST_UNSUPPORTED_EXIT_CODE
    profile = build_profile(args)
    env = os.environ.copy()
    env.setdefault("CHILI_PYTEST_TIMEOUT_SECONDS", str(profile.per_test_timeout_seconds))
    env.setdefault(
        "CHILI_PYTEST_WRAPPER_TIMEOUT_SECONDS",
        str(profile.wrapper_timeout_seconds),
    )
    env.setdefault("CHILI_PYTEST_HEARTBEAT_SECONDS", str(profile.heartbeat_interval_seconds))
    env.setdefault("CHILI_PYTEST_DB_POOL_SIZE", str(profile.db_slot_count))
    return subprocess.run(build_pytest_command(args, python=contract.python), env=env).returncode


def ensure_pytest_runtime(
    *,
    root: Path | None = None,
    env_dir: Path | None = None,
    create: bool = False,
) -> int:
    resolved_root = root or repo_root()
    resolved_env_dir = env_dir or default_runtime_env_dir(resolved_root)
    contract = pytest_runtime_contract(resolved_root)
    if (
        contract.passed
        and contract.isolation_status == "isolated"
        and not contract.missing_imports
    ):
        print(json.dumps(pytest_runtime_doctor(resolved_root, env_dir=resolved_env_dir), sort_keys=True))
        return 0
    if contract.passed and not create:
        print(json.dumps(pytest_runtime_doctor(resolved_root, env_dir=resolved_env_dir), sort_keys=True))
        return (
            0
            if contract.isolation_status == "isolated" and not contract.missing_imports
            else PYTEST_UNSUPPORTED_EXIT_CODE
        )
    if not create:
        print(json.dumps(pytest_runtime_doctor(resolved_root, env_dir=resolved_env_dir), sort_keys=True))
        return PYTEST_UNSUPPORTED_EXIT_CODE

    resolved_env_dir.parent.mkdir(parents=True, exist_ok=True)
    env_python = runtime_env_python(resolved_env_dir, root=resolved_root)
    recreate_degraded_runtime = (
        contract.passed
        and contract.isolation_status != "isolated"
        and resolved_env_dir.exists()
    )
    if recreate_degraded_runtime or not env_python.exists():
        create_result = subprocess.run(
            runtime_create_command(
                resolved_env_dir,
                root=resolved_root,
                clear=recreate_degraded_runtime,
            )
        )
        if create_result.returncode != 0:
            return create_result.returncode or 1
    install_result = subprocess.run(runtime_install_command(resolved_env_dir, root=resolved_root))
    if install_result.returncode != 0:
        return install_result.returncode or 1

    os.environ[PYTEST_PYTHON_ENV_VAR] = str(env_python)
    final_contract = pytest_runtime_contract(resolved_root)
    print(json.dumps(pytest_runtime_doctor(resolved_root, env_dir=resolved_env_dir), sort_keys=True))
    return (
        0
        if (
            final_contract.passed
            and final_contract.isolation_status == "isolated"
            and not final_contract.missing_imports
        )
        else PYTEST_UNSUPPORTED_EXIT_CODE
    )


def _xdist_workers_from_args(args: Sequence[str], *, cpu_count: int) -> int | None:
    for idx, arg in enumerate(args):
        if arg in {"-n", "--numprocesses"} and idx + 1 < len(args):
            return _coerce_positive_int_token(args[idx + 1], cpu_count=cpu_count)
        for prefix in ("-n=", "--numprocesses="):
            if arg.startswith(prefix):
                return _coerce_positive_int_token(arg.split("=", 1)[1], cpu_count=cpu_count)
    return None


def resolve_xdist_worker_count(args: Sequence[str] | None = None) -> int:
    cpu_count = available_cpu_count()
    env_workers = _env_int("PYTEST_XDIST_WORKER_COUNT", minimum=1)
    if env_workers:
        return env_workers
    argv = _iter_pytest_args(args)
    arg_workers = _xdist_workers_from_args(argv, cpu_count=cpu_count)
    if arg_workers:
        return arg_workers
    addopts = os.environ.get("PYTEST_ADDOPTS", "").strip()
    if addopts:
        addopts_workers = _xdist_workers_from_args(shlex.split(addopts), cpu_count=cpu_count)
        if addopts_workers:
            return addopts_workers
    return 1


def _pytest_path_candidates(args: Sequence[str], *, root: Path) -> list[Path]:
    candidates: list[Path] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if not arg or arg.startswith("-"):
            option_name = arg.split("=", 1)[0]
            if option_name in PYTEST_OPTIONS_WITH_VALUES and "=" not in arg:
                skip_next = True
            continue
        path_token = arg.split("::", 1)[0]
        candidate = Path(path_token)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.exists():
            candidates.append(candidate)
    if candidates:
        return candidates
    return [root / PYTEST_DEFAULT_TEST_ROOT]


def _count_test_files_under(path: Path) -> int:
    if path.is_file():
        return int(path.name.startswith(PYTEST_TEST_FILE_PREFIX) and path.suffix == PYTEST_TEST_FILE_SUFFIX)
    if not path.is_dir():
        return 0
    return sum(
        1
        for child in path.rglob(f"{PYTEST_TEST_FILE_PREFIX}*{PYTEST_TEST_FILE_SUFFIX}")
        if "__pycache__" not in child.parts
    )


def selected_test_file_count(args: Sequence[str] | None = None, *, root: Path | None = None) -> int:
    resolved_root = root or repo_root()
    candidates = _pytest_path_candidates(_iter_pytest_args(args), root=resolved_root)
    count = sum(_count_test_files_under(candidate) for candidate in candidates)
    return max(1, count)


def resolve_db_slot_count(args: Sequence[str] | None = None) -> int:
    env_slots = _env_int("CHILI_PYTEST_DB_POOL_SIZE", minimum=0)
    if env_slots is not None:
        return env_slots
    return max(available_cpu_count(), resolve_xdist_worker_count(args))


def resolve_db_pool_timeout_seconds() -> int:
    env_timeout = (
        _env_float("DATABASE_PYTEST_POOL_TIMEOUT_SECONDS", minimum=1.0)
        or _env_float("DATABASE_POOL_TIMEOUT_SECONDS", minimum=1.0)
    )
    if env_timeout is not None:
        return max(1, math.ceil(env_timeout))
    return max(1, math.ceil(SECONDS_PER_MINUTE / math.sqrt(available_cpu_count())))


def resolve_per_test_timeout_seconds(args: Sequence[str] | None = None) -> int:
    env_timeout = (
        _env_int("CHILI_PYTEST_TIMEOUT_SECONDS", minimum=1)
        or _env_int("PYTEST_TIMEOUT", minimum=1)
    )
    if env_timeout:
        return env_timeout
    cpu_count = available_cpu_count()
    file_count = selected_test_file_count(args)
    scope_pressure = math.log2(file_count + 1) / math.log2(cpu_count + 1)
    return max(SECONDS_PER_MINUTE, math.ceil(SECONDS_PER_MINUTE * scope_pressure))


def resolve_wrapper_timeout_seconds(args: Sequence[str] | None = None) -> int:
    env_timeout = _env_int("CHILI_PYTEST_WRAPPER_TIMEOUT_SECONDS", minimum=1)
    if env_timeout:
        return env_timeout
    file_count = selected_test_file_count(args)
    per_test_timeout = resolve_per_test_timeout_seconds(args)
    return math.ceil(per_test_timeout * (math.sqrt(file_count) + 1))


def resolve_heartbeat_interval_seconds(args: Sequence[str] | None = None) -> int:
    env_interval = _env_int("CHILI_PYTEST_HEARTBEAT_SECONDS", minimum=1)
    if env_interval:
        return env_interval
    return max(1, math.ceil(math.sqrt(resolve_wrapper_timeout_seconds(args))))


def resolve_poll_interval_seconds(args: Sequence[str] | None = None) -> int:
    env_interval = _env_int("CHILI_PYTEST_POLL_SECONDS", minimum=1)
    if env_interval:
        return env_interval
    return max(1, min(resolve_heartbeat_interval_seconds(args), resolve_db_pool_timeout_seconds()))


def resolve_truncate_attempts(args: Sequence[str] | None = None) -> int:
    env_attempts = _env_int("CHILI_PYTEST_TRUNCATE_ATTEMPTS", minimum=1)
    if env_attempts:
        return env_attempts
    concurrency_pressure = max(available_cpu_count(), resolve_xdist_worker_count(args))
    return max(1, math.ceil(math.log2(concurrency_pressure + 1)))


def resolve_truncate_lock_timeout_seconds(args: Sequence[str] | None = None) -> int:
    env_timeout = _env_int("CHILI_PYTEST_LOCK_TIMEOUT_S", minimum=1)
    if env_timeout:
        return env_timeout
    return resolve_per_test_timeout_seconds(args)


def resolve_db_lock_wait_seconds(args: Sequence[str] | None = None) -> int:
    env_timeout = _env_int("CHILI_PYTEST_DB_LOCK_WAIT_S", minimum=1)
    if env_timeout:
        return env_timeout
    return max(resolve_db_pool_timeout_seconds(), resolve_per_test_timeout_seconds(args))


def resolve_db_lock_stale_seconds(args: Sequence[str] | None = None) -> int:
    env_timeout = _env_int("CHILI_PYTEST_DB_LOCK_STALE_S", minimum=1)
    if env_timeout:
        return env_timeout
    return max(resolve_db_lock_wait_seconds(args), resolve_per_test_timeout_seconds(args))


def resolve_db_lock_reap_interval_seconds(args: Sequence[str] | None = None) -> int:
    env_interval = _env_int("CHILI_PYTEST_DB_LOCK_REAP_INTERVAL_S", minimum=1)
    if env_interval:
        return env_interval
    return max(1, math.ceil(math.sqrt(resolve_db_lock_stale_seconds(args))))


def resolve_db_lock_poll_seconds(args: Sequence[str] | None = None) -> float:
    env_interval = _env_float("CHILI_PYTEST_DB_LOCK_POLL_S", minimum=0.001)
    if env_interval:
        return env_interval
    return max(1.0 / available_cpu_count(), 1.0 / resolve_db_pool_timeout_seconds())


def resolve_retry_backoff_seconds(attempt_index: int, args: Sequence[str] | None = None) -> float:
    retry_number = max(1, attempt_index + 1)
    return min(float(resolve_db_pool_timeout_seconds()), math.sqrt(retry_number))


def resolve_drop_timeout_milliseconds(args: Sequence[str] | None = None) -> int:
    env_timeout = _env_int("CHILI_PYTEST_DROP_TIMEOUT_MS", minimum=1)
    if env_timeout:
        return env_timeout
    return resolve_db_pool_timeout_seconds() * MILLISECONDS_PER_SECOND


def resolve_stale_cleanup_min_age_minutes(args: Sequence[str] | None = None) -> int:
    env_minutes = _env_int("CHILI_PYTEST_STALE_CLEANUP_MIN_AGE_MINUTES", minimum=1)
    if env_minutes:
        return env_minutes
    return max(1, math.ceil(resolve_wrapper_timeout_seconds(args) / SECONDS_PER_MINUTE))


def advisory_lock_key_pair(*parts: object) -> tuple[int, int]:
    namespace = "|".join(str(part) for part in parts if str(part))
    digest = hashlib.blake2s(
        namespace.encode("utf-8"),
        digest_size=POSTGRES_ADVISORY_LOCK_BYTES_PER_KEY * 2,
    ).digest()
    return (
        _signed_postgres_lock_key(digest[:POSTGRES_ADVISORY_LOCK_BYTES_PER_KEY]),
        _signed_postgres_lock_key(digest[POSTGRES_ADVISORY_LOCK_BYTES_PER_KEY:]),
    )


def _signed_postgres_lock_key(raw: bytes) -> int:
    bits = len(raw) * BITS_PER_BYTE
    unsigned_value = int.from_bytes(raw, "big", signed=False)
    sign_bit = 1 << (bits - 1)
    modulus = 1 << bits
    if unsigned_value >= sign_bit:
        return unsigned_value - modulus
    return unsigned_value


def build_profile(args: Sequence[str] | None = None) -> PytestAdaptiveProfile:
    argv = _iter_pytest_args(args)
    root = repo_root()
    cpu_count = available_cpu_count()
    xdist_worker_count = resolve_xdist_worker_count(argv)
    selected_count = selected_test_file_count(argv, root=root)
    db_slot_count = resolve_db_slot_count(argv)
    db_pool_timeout = resolve_db_pool_timeout_seconds()

    per_test_timeout = (
        _env_int("CHILI_PYTEST_TIMEOUT_SECONDS", minimum=1)
        or _env_int("PYTEST_TIMEOUT", minimum=1)
    )
    if per_test_timeout is None:
        scope_pressure = math.log2(selected_count + 1) / math.log2(cpu_count + 1)
        per_test_timeout = max(
            SECONDS_PER_MINUTE,
            math.ceil(SECONDS_PER_MINUTE * scope_pressure),
        )

    wrapper_timeout = _env_int("CHILI_PYTEST_WRAPPER_TIMEOUT_SECONDS", minimum=1)
    if wrapper_timeout is None:
        wrapper_timeout = math.ceil(per_test_timeout * (math.sqrt(selected_count) + 1))

    heartbeat_interval = _env_int("CHILI_PYTEST_HEARTBEAT_SECONDS", minimum=1)
    if heartbeat_interval is None:
        heartbeat_interval = max(1, math.ceil(math.sqrt(wrapper_timeout)))

    poll_interval = _env_int("CHILI_PYTEST_POLL_SECONDS", minimum=1)
    if poll_interval is None:
        poll_interval = max(1, min(heartbeat_interval, db_pool_timeout))

    truncate_attempts = _env_int("CHILI_PYTEST_TRUNCATE_ATTEMPTS", minimum=1)
    if truncate_attempts is None:
        concurrency_pressure = max(cpu_count, xdist_worker_count)
        truncate_attempts = max(1, math.ceil(math.log2(concurrency_pressure + 1)))

    truncate_lock_timeout = _env_int("CHILI_PYTEST_LOCK_TIMEOUT_S", minimum=1)
    if truncate_lock_timeout is None:
        truncate_lock_timeout = per_test_timeout

    db_lock_wait = _env_int("CHILI_PYTEST_DB_LOCK_WAIT_S", minimum=1)
    if db_lock_wait is None:
        db_lock_wait = max(db_pool_timeout, per_test_timeout)

    db_lock_stale = _env_int("CHILI_PYTEST_DB_LOCK_STALE_S", minimum=1)
    if db_lock_stale is None:
        db_lock_stale = max(db_lock_wait, per_test_timeout)

    db_lock_reap_interval = _env_int("CHILI_PYTEST_DB_LOCK_REAP_INTERVAL_S", minimum=1)
    if db_lock_reap_interval is None:
        db_lock_reap_interval = max(1, math.ceil(math.sqrt(db_lock_stale)))

    db_lock_poll = _env_float("CHILI_PYTEST_DB_LOCK_POLL_S", minimum=0.001)
    if db_lock_poll is None:
        db_lock_poll = max(1.0 / cpu_count, 1.0 / db_pool_timeout)

    drop_timeout = _env_int("CHILI_PYTEST_DROP_TIMEOUT_MS", minimum=1)
    if drop_timeout is None:
        drop_timeout = db_pool_timeout * MILLISECONDS_PER_SECOND

    stale_cleanup_min_age = _env_int(
        "CHILI_PYTEST_STALE_CLEANUP_MIN_AGE_MINUTES",
        minimum=1,
    )
    if stale_cleanup_min_age is None:
        stale_cleanup_min_age = max(1, math.ceil(wrapper_timeout / SECONDS_PER_MINUTE))

    return PytestAdaptiveProfile(
        repo_root=str(root),
        cpu_count=cpu_count,
        xdist_worker_count=xdist_worker_count,
        selected_test_file_count=selected_count,
        db_slot_count=db_slot_count,
        db_pool_timeout_seconds=db_pool_timeout,
        per_test_timeout_seconds=per_test_timeout,
        wrapper_timeout_seconds=wrapper_timeout,
        heartbeat_interval_seconds=heartbeat_interval,
        poll_interval_seconds=poll_interval,
        truncate_attempts=truncate_attempts,
        truncate_lock_timeout_seconds=truncate_lock_timeout,
        db_lock_wait_seconds=db_lock_wait,
        db_lock_stale_seconds=db_lock_stale,
        db_lock_reap_interval_seconds=db_lock_reap_interval,
        db_lock_poll_seconds=db_lock_poll,
        drop_timeout_milliseconds=drop_timeout,
        stale_cleanup_min_age_minutes=stale_cleanup_min_age,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run pytest through CHILI's adaptive runtime guard.")
    subparsers = parser.add_subparsers(dest="command")
    profile_parser = subparsers.add_parser("profile")
    profile_parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    subparsers.add_parser("runtime")
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--env-dir", type=Path, default=None)
    ensure_parser = subparsers.add_parser("ensure-runtime")
    ensure_parser.add_argument("--create", action="store_true")
    ensure_parser.add_argument("--env-dir", type=Path, default=None)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    if ns.command == "profile":
        print(json.dumps(asdict(build_profile(ns.pytest_args)), sort_keys=True))
        return 0
    if ns.command == "runtime":
        print(json.dumps(asdict(pytest_runtime_contract()), sort_keys=True))
        return 0
    if ns.command == "doctor":
        print(json.dumps(pytest_runtime_doctor(env_dir=ns.env_dir), sort_keys=True))
        return 0
    if ns.command == "ensure-runtime":
        return ensure_pytest_runtime(create=ns.create, env_dir=ns.env_dir)
    if ns.command == "run":
        return run_pytest_with_runtime_contract(ns.pytest_args)
    if ns.command is None:
        parser.print_help()
        return 2
    parser.error(f"unknown command: {ns.command}")


if __name__ == "__main__":
    raise SystemExit(main())
