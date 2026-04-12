"""
Hard-coded Phase 1 validation steps only. Fail closed: no user argv, no installs, no network hooks.
"""
from __future__ import annotations

import ast
import logging
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


@dataclass
class StepResult:
    step_key: str
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    skipped: bool
    skip_reason: str | None = None


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


def run_ast_syntax(cwd: Path) -> StepResult:
    """Read-only: parse .py files under cwd (bounded count)."""
    py_files: list[Path] = []
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
            buf.append(f"ok {fp.relative_to(cwd)}")
        except SyntaxError as e:
            errors += 1
            buf.append(f"SyntaxError {fp.relative_to(cwd)}: {e}")
        except OSError as e:
            errors += 1
            buf.append(f"os_error {fp.relative_to(cwd)}: {e}")
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


def run_pytest_collect(cwd: Path) -> StepResult:
    pc = Path(tempfile.gettempdir()) / f"chili_pytest_{uuid.uuid4().hex}"
    pc.mkdir(parents=True, exist_ok=True)
    code, to, out, err = _run_subprocess_allowlisted(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            ".",
            "-o",
            f"cache_dir={pc}",
        ],
        cwd,
    )
    if code == 127 or (code == 1 and "No module named pytest" in err):
        msg, _ = truncate_text(out + "\n" + err + "\n[pytest not available; step skipped]")
        return StepResult("pytest_collect", 0, to, msg, "", True, "pytest not available")
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


def run_pytest_targeted(cwd: Path, changed_files: list[str] | None = None) -> StepResult:
    """Run pytest on test files related to changed source files.

    Falls back to full collection test if no targeted tests are found.
    """
    import tempfile

    pc = Path(tempfile.gettempdir()) / f"chili_pytest_{uuid.uuid4().hex}"
    pc.mkdir(parents=True, exist_ok=True)

    test_files = _infer_test_files(cwd, changed_files or []) if changed_files else []

    if test_files:
        argv = [
            sys.executable, "-m", "pytest",
            "-x",  # stop on first failure for faster feedback
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
        return StepResult("pytest_targeted", 0, to, out or "", err or "", True, "pytest not available")
    out_t, _ = truncate_text(out or "")
    err_t, _ = truncate_text(err or "")
    ok = code in (0, 5)  # 5 = no tests collected
    return StepResult("pytest_targeted", 0 if ok else code, to, out_t, err_t, False, None)


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


def run_phase1_validation(cwd: Path) -> list[StepResult]:
    """Execute every Phase 1 step in order (hard-coded)."""
    results: list[StepResult] = []
    for key in PHASE1_STEP_KEYS:
        assert_allowlisted_step(key)
        runner = _STEP_RUNNERS[key]
        try:
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
