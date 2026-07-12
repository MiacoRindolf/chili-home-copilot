"""
Hard-coded Phase 1 validation steps only. Fail closed: no user argv, no installs, no network hooks.
"""
from __future__ import annotations

import ast
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ...config import settings
from .envelope import subprocess_safe_env, truncate_text

logger = logging.getLogger(__name__)

# Order and keys are fixed for Phase 1 (not user-configurable).
PHASE1_STEP_KEYS: tuple[str, ...] = (
    "ast_syntax",
    "ruff_check",
    "pytest_collect",
    "git_status",
    "git_diff_stat",
)

_MAX_PY_FILES = 200
_CHANGED_SYNTAX_SUFFIXES = frozenset(
    {".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".dart"}
)
_NODE_TYPESCRIPT_PARSE_SCRIPT = (
    'import { readFileSync } from "node:fs"; '
    'import { stripTypeScriptTypes } from "node:module"; '
    'stripTypeScriptTypes(readFileSync(process.argv[1], "utf8"), { mode: "transform" });'
)
_PYTEST_SAFE_DB_MESSAGES = (
    "Tests require TEST_DATABASE_URL",
    "TEST_DATABASE_URL must be a PostgreSQL URL",
    "database name must end with '_test'",
)
_PYTEST_SAFE_DB_SKIP_REASON = "safe TEST_DATABASE_URL not configured"


@dataclass
class StepResult:
    step_key: str
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    skipped: bool
    skip_reason: str | None = None
    # Optional honesty payload merged into the validation result by the
    # orchestrator's _step_result_payload (e.g. tests_executed=False when a
    # pytest step only collected instead of running real tests).
    metadata: dict | None = None


def _timeout() -> float:
    return float(max(5, getattr(settings, "coding_validation_step_timeout_seconds", 120)))


def _run_subprocess_allowlisted(
    argv: list[str],
    cwd: Path,
    *,
    timeout: float | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, bool, str, str]:
    """
    Run a fixed argv under cwd. Returns (exit_code, timed_out, stdout, stderr).
    On timeout, kills the process tree best-effort.
    """
    t = timeout if timeout is not None else _timeout()
    env = subprocess_safe_env()
    if extra_env:
        env = {**env, **extra_env}
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except FileNotFoundError:
        return 127, False, "", f"executable not found: {argv[0]!r}"
    try:
        out, err = proc.communicate(timeout=t)
        return proc.returncode or 0, False, out or "", err or ""
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        return -1, True, out or "", (err or "") + "\n[timeout]"


def _pytest_safe_database_guard_triggered(stdout: str, stderr: str) -> bool:
    combined = f"{stdout or ''}\n{stderr or ''}"
    return any(message in combined for message in _PYTEST_SAFE_DB_MESSAGES)


def _pytest_safe_database_skip(step_key: str, timed_out: bool, stdout: str, stderr: str) -> StepResult:
    out_t, _ = truncate_text(stdout or "")
    err_t, _ = truncate_text(stderr or "")
    return StepResult(
        step_key,
        0,
        timed_out,
        out_t,
        err_t,
        True,
        _PYTEST_SAFE_DB_SKIP_REASON,
    )


def _run_python_ast_syntax(cwd: Path, changed_files: list[str] | None = None) -> StepResult:
    """Read-only: parse .py files under cwd (bounded count).

    When ``changed_files`` is provided, parse ONLY those .py files (resolved
    under cwd; deleted/missing paths are skipped). This both scopes the check
    to what the run actually touched and avoids the repo-wide bounded walk,
    which on a large repo checks an arbitrary 200 files that may not include
    the changed ones at all.
    """
    py_files: list[Path] = []
    if changed_files:
        for rel in changed_files:
            if not str(rel).endswith(".py"):
                continue
            p = (cwd / str(rel).replace("\\", "/")).resolve()
            try:
                p.relative_to(cwd.resolve())
            except ValueError:
                continue  # path escapes the worktree — never read outside it
            if p.is_file():
                py_files.append(p)
            if len(py_files) >= _MAX_PY_FILES:
                break
    else:
        for p in cwd.rglob("*.py"):
            if "__pycache__" in p.parts or ".venv" in p.parts or "venv" in p.parts:
                continue
            py_files.append(p)
            if len(py_files) >= _MAX_PY_FILES:
                break
    buf: list[str] = []
    errors = 0
    for fp in py_files:
        try:
            src = fp.read_text(encoding="utf-8", errors="replace")
            ast.parse(src, filename=str(fp))
            buf.append(f"ok {fp.relative_to(cwd).as_posix()}")
        except SyntaxError as e:
            errors += 1
            buf.append(f"SyntaxError {fp.relative_to(cwd).as_posix()}: {e}")
        except OSError as e:
            errors += 1
            buf.append(f"os_error {fp.relative_to(cwd).as_posix()}: {e}")
    out = "\n".join(buf) if buf else "(no .py files under cwd)"
    code = 1 if errors else 0
    full, blen = truncate_text(out + (f"\n{errors} file(s) with syntax errors" if errors else ""))
    return StepResult(
        "ast_syntax",
        code,
        False,
        full,
        "",
        False,
        None,
    )


def _dart_executable() -> str:
    command = shutil.which("dart") or ""
    if not command:
        return ""
    path = Path(command)
    if sys.platform == "win32" and path.suffix.lower() in {".bat", ".cmd"}:
        executable = path.parent / "cache" / "dart-sdk" / "bin" / "dart.exe"
        if executable.is_file():
            return str(executable)
    return command


def run_ast_syntax(cwd: Path, changed_files: list[str] | None = None) -> StepResult:
    """Run bounded, allowlisted syntax checks for changed source files."""
    if changed_files is None:
        return _run_python_ast_syntax(cwd)

    root = cwd.resolve()
    scoped: list[tuple[str, Path]] = []
    for raw in changed_files:
        relative = str(raw).replace("\\", "/")
        if Path(relative).suffix.lower() not in _CHANGED_SYNTAX_SUFFIXES:
            continue
        path = (root / relative).resolve()
        try:
            safe_relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if path.is_file():
            scoped.append((safe_relative, path))
        if len(scoped) >= _MAX_PY_FILES:
            break

    python_files = [relative for relative, path in scoped if path.suffix.lower() == ".py"]
    python_result = (
        _run_python_ast_syntax(root, changed_files=python_files)
        if python_files
        else None
    )
    output_lines = [
        line
        for line in str(python_result.stdout if python_result else "").splitlines()
        if line.strip() and not line.startswith("(no .py files")
    ]
    errors = 1 if python_result is not None and python_result.exit_code != 0 else 0
    validated_files = list(python_files)
    languages = {"python"} if python_files else set()
    node = shutil.which("node") or ""
    dart = _dart_executable()

    for relative, path in scoped:
        suffix = path.suffix.lower()
        if suffix == ".py":
            continue
        if suffix in {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}:
            if not node:
                errors += 1
                output_lines.append(f"validator_unavailable {relative}: node")
                continue
            validated_files.append(relative)
            languages.add("typescript" if suffix in {".ts", ".mts", ".cts"} else "javascript")
            argv = [node, "--check", relative]
            if suffix in {".ts", ".mts", ".cts"}:
                # ``node --check`` does not parse stripped TypeScript syntax.
                # The built-in parser does, without evaluating repository code.
                argv = [
                    node,
                    "--no-warnings",
                    "--input-type=module",
                    "--eval",
                    _NODE_TYPESCRIPT_PARSE_SCRIPT,
                    relative,
                ]
            code, timed_out, stdout, stderr = _run_subprocess_allowlisted(argv, root)
            if code != 0 or timed_out:
                errors += 1
                detail = (stderr or stdout or "syntax check failed").strip()
                output_lines.append(f"SyntaxError {relative}: {detail}")
            else:
                output_lines.append(f"ok {relative}")
            continue
        if not dart:
            errors += 1
            output_lines.append(f"validator_unavailable {relative}: dart")
            continue
        validated_files.append(relative)
        languages.add("dart")
        with tempfile.TemporaryDirectory(prefix="chili_dart_analyzer_") as state_dir:
            appdata = Path(state_dir) / "appdata"
            localappdata = Path(state_dir) / "localappdata"
            appdata.mkdir()
            localappdata.mkdir()
            code, timed_out, stdout, stderr = _run_subprocess_allowlisted(
                [dart, "analyze", relative],
                root,
                extra_env={
                    "APPDATA": str(appdata),
                    "LOCALAPPDATA": str(localappdata),
                    "DART_DISABLE_ANALYTICS": "true",
                },
            )
        if code != 0 or timed_out:
            errors += 1
            detail = (stderr or stdout or "syntax check failed").strip()
            output_lines.append(f"SyntaxError {relative}: {detail}")
        else:
            output_lines.append(f"ok {relative}")

    output = (
        "\n".join(output_lines)
        if output_lines
        else "(no supported source files in validation scope)"
    )
    if errors:
        output += f"\n{errors} validator group(s) reported syntax errors"
    full, _byte_length = truncate_text(output)
    return StepResult(
        "ast_syntax",
        1 if errors else 0,
        False,
        full,
        "",
        False,
        None,
        {
            "changed_files": validated_files,
            "validation_scope": "changed_file_syntax",
            "syntax_languages": sorted(languages),
        },
    )


def run_ruff_check(cwd: Path) -> StepResult:
    ruff_cache = Path(tempfile.gettempdir()) / f"chili_ruff_{uuid.uuid4().hex}"
    ruff_cache.mkdir(parents=True, exist_ok=True)
    code, to, out, err = _run_subprocess_allowlisted(
        ["ruff", "check", ".", "--cache-dir", str(ruff_cache)],
        cwd,
    )
    if code == 127 and ("not found" in err.lower() or "executable not found" in err.lower()):
        msg, blen = truncate_text(out + "\n" + err + "\n[ruff not installed; step skipped]")
        return StepResult("ruff_check", 0, to, msg, "", True, "ruff not available")
    out_t, _ = truncate_text(out)
    err_t, _ = truncate_text(err)
    return StepResult("ruff_check", 0 if code == 0 else code, to, out_t, err_t, False, None)


def run_pytest_collect(cwd: Path, changed_files: list[str] | None = None) -> StepResult:
    """Collect-sanity step. When ``changed_files`` is provided, scope to the
    tests related to them — a repo-wide collect fails on any PRE-EXISTING
    breakage in the base branch (live: every dispatch run failed on an
    unrelated trading check baked into local main), which makes validation
    measure the baseline, not the change."""
    pc = Path(tempfile.gettempdir()) / f"chili_pytest_{uuid.uuid4().hex}"
    pc.mkdir(parents=True, exist_ok=True)
    targets: list[str] = []
    if changed_files:
        targets = _infer_test_files(cwd, changed_files)
        if not targets:
            return StepResult(
                "pytest_collect", 0, False,
                "(no tests related to the changed files; repo-wide collect "
                "intentionally skipped — it measures baseline breakage, not "
                "this change)",
                "", True, "no related tests",
                {"tests_executed": False, "tests_selected": []},
            )
    code, to, out, err = _run_subprocess_allowlisted(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            *(targets if targets else ["."]),
            "-o",
            f"cache_dir={pc}",
        ],
        cwd,
    )
    if code == 127 or (code == 1 and "No module named pytest" in err):
        msg, _ = truncate_text(out + "\n" + err + "\n[pytest not available; step skipped]")
        return StepResult("pytest_collect", 0, to, msg, "", True, "pytest not available")
    if _pytest_safe_database_guard_triggered(out, err):
        return _pytest_safe_database_skip("pytest_collect", to, out, err)
    out_t, _ = truncate_text(out)
    err_t, _ = truncate_text(err)
    # pytest exits 5 when no tests collected (still non-destructive).
    ok_pytest = code in (0, 5)
    return StepResult("pytest_collect", 0 if ok_pytest else code, to, out_t, err_t, False, None)


def run_git_status(cwd: Path) -> StepResult:
    if not (cwd / ".git").exists():
        return StepResult(
            "git_status",
            0,
            False,
            "(skipped: not a git checkout)",
            "",
            True,
            "no .git",
        )
    code, to, out, err = _run_subprocess_allowlisted(["git", "status", "--porcelain"], cwd)
    out_t, _ = truncate_text(out)
    err_t, _ = truncate_text(err)
    return StepResult("git_status", 0 if code == 0 else code, to, out_t, err_t, False, None)


def run_git_diff_stat(cwd: Path) -> StepResult:
    if not (cwd / ".git").exists():
        return StepResult(
            "git_diff_stat",
            0,
            False,
            "(skipped: not a git checkout)",
            "",
            True,
            "no .git",
        )
    code, to, out, err = _run_subprocess_allowlisted(["git", "diff", "--stat"], cwd)
    out_t, _ = truncate_text(out)
    err_t, _ = truncate_text(err)
    return StepResult("git_diff_stat", 0 if code == 0 else code, to, out_t, err_t, False, None)


def _infer_test_files(cwd: Path, changed_files: list[str]) -> list[str]:
    """Map changed source files to likely test files.

    Strategy: for ``app/routers/auth.py`` look for ``tests/test_auth*.py``,
    ``tests/test_routers_auth*.py``, etc. Returns paths relative to *cwd*.
    """
    candidates: list[str] = []
    tests_dir = cwd / "tests"
    if not tests_dir.is_dir():
        return []
    for src in changed_files:
        stem = Path(src).stem  # e.g. "auth"
        for tp in tests_dir.rglob(f"test_{stem}*.py"):
            rel = str(tp.relative_to(cwd)).replace("\\", "/")
            if rel not in candidates:
                candidates.append(rel)
        # Also check parent-qualified: app/routers/auth.py -> test_routers_auth.py
        parts = Path(src).parts
        if len(parts) >= 2:
            qualified = f"test_{'_'.join(parts[-2:])}".replace(".py", "*.py").replace("/", "_")
            for tp in tests_dir.rglob(qualified):
                rel = str(tp.relative_to(cwd)).replace("\\", "/")
                if rel not in candidates:
                    candidates.append(rel)
    return candidates[:20]  # bounded


def run_pytest_targeted(
    cwd: Path,
    changed_files: list[str] | None = None,
    *,
    selected_test_files: list[str] | None = None,
) -> StepResult:
    """Run pytest on test files related to changed source files.

    Falls back to full collection test if no targeted tests are found.
    """
    import tempfile

    pc = Path(tempfile.gettempdir()) / f"chili_pytest_{uuid.uuid4().hex}"
    pc.mkdir(parents=True, exist_ok=True)

    if selected_test_files is None:
        test_files = _infer_test_files(cwd, changed_files or []) if changed_files else []
    else:
        root = cwd.resolve()
        test_files = []
        for raw in selected_test_files:
            relative = str(raw).replace("\\", "/").lstrip("/")
            path = (root / relative).resolve()
            try:
                safe_relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if (
                safe_relative.startswith("tests/")
                and path.is_file()
                and path.suffix.lower() == ".py"
                and safe_relative not in test_files
            ):
                test_files.append(safe_relative)
        test_files = test_files[:20]

    if test_files:
        argv = [
            sys.executable, "-m", "pytest",
            "--tb=short",
            "-q",
            "-o", f"cache_dir={pc}",
        ] + test_files
    else:
        # No targeted tests found; run full suite with collect-only to verify nothing is broken
        argv = [
            sys.executable, "-m", "pytest",
            "--collect-only", "-q", ".",
            "-o", f"cache_dir={pc}",
        ]

    code, to, out, err = _run_subprocess_allowlisted(argv, cwd, timeout=300)
    if code == 127 or (code == 1 and "No module named pytest" in (err or "")):
        return StepResult("pytest_targeted", 0, to, out or "", err or "", True, "pytest not available",
                          {
                              "tests_executed": False,
                              "tests_selected": test_files,
                              "test_files": test_files,
                              "targeted": bool(test_files),
                              "validation_scope": "targeted_tests" if test_files else "collect_only",
                              "command": " ".join(argv),
                          })
    if _pytest_safe_database_guard_triggered(out, err):
        result = _pytest_safe_database_skip("pytest_targeted", to, out, err)
        result.metadata = {
            "tests_executed": False,
            "tests_selected": test_files,
            "test_files": test_files,
            "targeted": bool(test_files),
            "validation_scope": "targeted_tests" if test_files else "collect_only",
            "command": " ".join(argv),
        }
        return result
    out_t, _ = truncate_text(out or "")
    err_t, _ = truncate_text(err or "")
    combined_output = f"{out or ''}\n{err or ''}".lower()
    zero_test_markers = (
        "no tests ran",
        "collected 0 items",
        "0 tests collected",
    )
    tests_executed = bool(test_files) and code == 0 and not any(
        marker in combined_output for marker in zero_test_markers
    )
    # Exit 5 is acceptable only for the collect-only fallback. A targeted
    # selection that collected nothing is a failed evidence contract.
    ok = code == 0 or (not test_files and code == 5)
    # Honesty marker: "passed" with zero tests actually run is NOT the same
    # as passing tests. Surfaced so the orchestrator/UI can show it.
    return StepResult(
        "pytest_targeted",
        0 if ok else (code or 5),
        to,
        out_t,
        err_t,
        False,
        None,
        {
            "tests_executed": tests_executed,
            "tests_selected": test_files,
            "test_files": test_files,
            "targeted": bool(test_files),
            "validation_scope": "targeted_tests" if test_files else "collect_only",
            "command": " ".join(argv),
            "zero_tests_collected": bool(test_files) and not tests_executed,
        },
    )


def run_mypy_check(cwd: Path) -> StepResult:
    """Run mypy type checking on changed Python files."""
    code, to, out, err = _run_subprocess_allowlisted(
        [sys.executable, "-m", "mypy", ".", "--ignore-missing-imports", "--no-error-summary"],
        cwd,
        timeout=120,
    )
    if code == 127 or "No module named mypy" in (err or ""):
        return StepResult("mypy_check", 0, to, out or "", err or "", True, "mypy not available")
    out_t, _ = truncate_text(out or "")
    err_t, _ = truncate_text(err or "")
    return StepResult("mypy_check", 0 if code == 0 else code, to, out_t, err_t, False, None)


_STEP_RUNNERS: dict[str, Callable[[Path], StepResult]] = {
    "ast_syntax": run_ast_syntax,
    "ruff_check": run_ruff_check,
    "pytest_collect": run_pytest_collect,
    "git_status": run_git_status,
    "git_diff_stat": run_git_diff_stat,
}


def assert_allowlisted_step(step_key: str) -> None:
    if step_key not in PHASE1_STEP_KEYS:
        raise ValueError(f"Disallowed validation step: {step_key!r}")


def run_phase1_validation(cwd: Path, changed_files: list[str] | None = None) -> list[StepResult]:
    """Execute every Phase 1 step in order (hard-coded keys; ast/pytest
    scope to ``changed_files`` when provided so validation measures the
    CHANGE, not pre-existing baseline breakage)."""
    results: list[StepResult] = []
    for key in PHASE1_STEP_KEYS:
        assert_allowlisted_step(key)
        runner = _STEP_RUNNERS[key]
        try:
            if changed_files and key in ("ast_syntax", "pytest_collect"):
                results.append(runner(cwd, changed_files))  # type: ignore[call-arg]
            else:
                results.append(runner(cwd))
        except Exception as e:
            logger.exception("[coding_task] step %s failed", key)
            msg, _ = truncate_text(str(e))
            results.append(
                StepResult(
                    key,
                    1,
                    False,
                    "",
                    msg,
                    False,
                    None,
                )
            )
    return results
