"""Autonomous execution loop: plan -> generate -> apply -> test -> diagnose -> fix -> verify.

Closes the iteration gap that makes a single-shot LLM weaker than a system
with persistent context.  Safety: max iterations, git branch isolation,
automatic rollback on failure.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from ...models.code_brain import CodeRepo
from ...models.coding_task import CodingExecutionIteration
from ..code_brain.agent import (
    _gather_context,
    _build_plan_prompt,
    _build_edit_prompt,
    _parse_plan_json,
    _read_file_content,
    _validate_diff,
    _MAX_FILE_LINES,
    _MAX_FILES_PER_EDIT,
)
from ..code_brain import insights as insights_mod
from .envelope import subprocess_safe_env, truncate_text
from .validator_runner import (
    run_pytest_targeted,
    run_ast_syntax,
    run_ruff_check,
    StepResult,
)

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 5
MAX_DURATION_SECONDS = 30 * 60  # 30 minutes
_GIT_BRANCH_PREFIX = "chili/auto/"


class LoopState(str, Enum):
    PLANNING = "planning"
    GENERATING = "generating"
    APPLYING = "applying"
    TESTING = "testing"
    DIAGNOSING = "diagnosing"
    FIXING = "fixing"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class IterationResult:
    iteration: int
    state: str
    plan_json: dict | None = None
    diffs: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    apply_status: str = ""
    test_exit_code: int | None = None
    test_output: str = ""
    diagnosis: str = ""
    error_category: str = ""
    model_used: str = ""
    duration_ms: int = 0


@dataclass
class LoopResult:
    run_id: str
    status: str  # "success" | "failed" | "rolled_back" | "max_iterations"
    iterations: list[IterationResult] = field(default_factory=list)
    branch_name: str = ""
    total_duration_ms: int = 0
    final_diffs: list[str] = field(default_factory=list)
    final_files_changed: list[str] = field(default_factory=list)
    summary: str = ""


def _llm_chat(messages: list[dict], system_prompt: str, trace_id: str, max_tokens: int = 2000) -> dict:
    from ...openai_client import chat as _chat, is_configured
    if not is_configured():
        return {"reply": "", "model": "none"}
    return _chat(
        messages=messages,
        system_prompt=system_prompt,
        trace_id=trace_id,
        user_message=messages[0].get("content", "") if messages else "",
        max_tokens=max_tokens,
    )


def _run_git(cwd: Path, args: list[str], timeout: float = 30) -> tuple[int, str]:
    """Run a git command at cwd. Returns (exit_code, combined_output)."""
    try:
        p = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            env=subprocess_safe_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 124, "git command timed out"
    except FileNotFoundError:
        return 127, "git executable not found"


def _create_branch(cwd: Path, run_id: str) -> str:
    """Create and checkout a fresh branch for this execution run."""
    branch = f"{_GIT_BRANCH_PREFIX}{run_id[:12]}"
    code, _ = _run_git(cwd, ["checkout", "-b", branch])
    if code != 0:
        # Branch might already exist; try just checkout
        _run_git(cwd, ["checkout", branch])
    return branch


def _rollback_branch(cwd: Path, original_branch: str, auto_branch: str) -> None:
    """Checkout the original branch and delete the auto branch."""
    _run_git(cwd, ["checkout", original_branch])
    _run_git(cwd, ["branch", "-D", auto_branch])


def _get_current_branch(cwd: Path) -> str:
    code, out = _run_git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out.strip() if code == 0 else "main"


def _apply_diffs(cwd: Path, diffs: list[str]) -> tuple[bool, str]:
    """Apply unified diffs via git apply. Returns (success, message)."""
    if not diffs:
        return False, "No diffs to apply"
    combined = "\n".join(d.strip("\n") + "\n" for d in diffs).encode("utf-8", "replace")
    # Dry-run first
    try:
        p = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "--check"],
            input=combined, cwd=str(cwd), env=subprocess_safe_env(),
            capture_output=True, timeout=60,
        )
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace")
            return False, f"Patch does not apply cleanly: {err}"
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)

    # Real apply
    try:
        p = subprocess.run(
            ["git", "apply", "--whitespace=nowarn"],
            input=combined, cwd=str(cwd), env=subprocess_safe_env(),
            capture_output=True, timeout=60,
        )
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace")
            return False, f"git apply failed: {err}"
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)

    # Stage and commit
    _run_git(cwd, ["add", "-A"])
    _run_git(cwd, ["commit", "-m", "chili: autonomous code change"])
    return True, "Applied successfully"


def _diagnose_failure(
    test_output: str,
    files_changed: list[str],
    original_prompt: str,
    iteration: int,
) -> tuple[str, str]:
    """Use LLM to diagnose test failure and produce a fix plan.

    Returns (diagnosis_text, error_category).
    """
    system = (
        "You are a debugging expert. Analyze the test failure output below and:\n"
        "1. Identify the root cause (be specific: file, line, error type)\n"
        "2. Classify the error: syntax | type | import | test_assertion | runtime | config\n"
        "3. Describe the minimal fix needed\n\n"
        "Return JSON:\n"
        '{"root_cause": "...", "error_category": "...", "fix_description": "...", '
        '"files_to_fix": ["path1", "path2"]}\n'
    )
    user_msg = (
        f"## Original request\n{original_prompt}\n\n"
        f"## Files changed\n{', '.join(files_changed)}\n\n"
        f"## Test output (iteration {iteration})\n{test_output[:6000]}"
    )
    result = _llm_chat(
        [{"role": "user", "content": user_msg}],
        system,
        f"exec-loop-diagnose-{iteration}",
        max_tokens=1000,
    )
    reply = result.get("reply", "")
    # Try to extract category
    category = "unknown"
    for cat in ("syntax", "type", "import", "test_assertion", "runtime", "config"):
        if cat in reply.lower():
            category = cat
            break
    return reply, category


def _run_test_validation(cwd: Path, files_changed: list[str]) -> tuple[int, str]:
    """Run targeted tests for changed files, falling back to full suite."""
    # First: AST syntax check (fast)
    ast_result = run_ast_syntax(cwd)
    if ast_result.exit_code != 0:
        return ast_result.exit_code, f"Syntax errors:\n{ast_result.stdout}"

    # Ruff lint check
    ruff_result = run_ruff_check(cwd)
    if ruff_result.exit_code != 0 and not ruff_result.skipped:
        return ruff_result.exit_code, f"Lint errors:\n{ruff_result.stdout}\n{ruff_result.stderr}"

    # Targeted pytest
    test_result = run_pytest_targeted(cwd, files_changed)
    output = test_result.stdout
    if test_result.stderr:
        output += "\n" + test_result.stderr
    return test_result.exit_code, output


def _persist_iteration(
    db: Session,
    run_id: str,
    iteration: IterationResult,
) -> None:
    """Write one iteration row to the database."""
    row = CodingExecutionIteration(
        run_id=run_id,
        iteration=iteration.iteration,
        state=iteration.state,
        prompt=None,  # stored only for iteration 0
        plan_json=json.dumps(iteration.plan_json) if iteration.plan_json else None,
        diffs_json=json.dumps(iteration.diffs) if iteration.diffs else None,
        files_changed_json=json.dumps(iteration.files_changed) if iteration.files_changed else None,
        apply_status=iteration.apply_status or None,
        test_exit_code=iteration.test_exit_code,
        test_output=truncate_text(iteration.test_output, 50_000)[0] if iteration.test_output else None,
        diagnosis=truncate_text(iteration.diagnosis, 10_000)[0] if iteration.diagnosis else None,
        error_category=iteration.error_category or None,
        model_used=iteration.model_used or None,
        duration_ms=iteration.duration_ms,
    )
    db.add(row)
    db.flush()


def run_execution_loop(
    db: Session,
    prompt: str,
    repo_id: int,
    *,
    user_id: int | None = None,
    on_progress: Callable[[str, dict], None] | None = None,
) -> LoopResult:
    """Run the autonomous plan→generate→apply→test→diagnose→fix loop.

    Args:
        db: SQLAlchemy session
        prompt: Natural language coding request
        repo_id: CodeRepo ID to operate on
        user_id: Optional user ID
        on_progress: Optional callback(event_type, data) for real-time progress streaming

    Returns:
        LoopResult with all iteration details
    """
    run_id = uuid.uuid4().hex[:16]
    start = time.monotonic()

    def emit(event: str, data: dict | None = None):
        if on_progress:
            try:
                on_progress(event, data or {})
            except Exception:
                pass

    # Resolve repo
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id, CodeRepo.active.is_(True)).first()
    if not repo:
        return LoopResult(run_id=run_id, status="failed", summary="Repository not found or inactive")

    cwd = Path(repo.path).resolve()
    if not cwd.is_dir():
        return LoopResult(run_id=run_id, status="failed", summary=f"Repository path not found: {cwd}")

    # Create isolated branch
    original_branch = _get_current_branch(cwd)
    branch = _create_branch(cwd, run_id)
    emit("branch_created", {"branch": branch, "run_id": run_id})

    result = LoopResult(run_id=run_id, status="running", branch_name=branch)
    current_prompt = prompt
    all_files_changed: list[str] = []
    all_diffs: list[str] = []

    try:
        for i in range(MAX_ITERATIONS):
            elapsed = time.monotonic() - start
            if elapsed > MAX_DURATION_SECONDS:
                result.status = "failed"
                result.summary = f"Timed out after {int(elapsed)}s"
                break

            iter_start = time.monotonic()
            iteration = IterationResult(iteration=i, state=LoopState.PLANNING)
            emit("iteration_start", {"iteration": i, "state": "planning"})

            # ── Step 1: Plan ──
            context = _gather_context(db, repo_id, current_prompt)
            plan_system = _build_plan_prompt(context)
            plan_result = _llm_chat(
                [{"role": "user", "content": current_prompt}],
                plan_system,
                f"exec-loop-plan-{run_id}-{i}",
                max_tokens=1500,
            )
            plan_reply = plan_result.get("reply", "")
            plan_json = _parse_plan_json(plan_reply)
            iteration.model_used = plan_result.get("model", "unknown")
            iteration.plan_json = plan_json

            if not plan_json or not plan_json.get("files"):
                iteration.state = LoopState.FAILED
                iteration.duration_ms = int((time.monotonic() - iter_start) * 1000)
                _persist_iteration(db, run_id, iteration)
                result.iterations.append(iteration)
                if i == 0:
                    result.status = "failed"
                    result.summary = f"Could not generate a plan: {plan_reply[:500]}"
                break

            emit("plan_ready", {"files": [f.get("path") for f in plan_json.get("files", [])]})

            # ── Step 2: Generate diffs ──
            iteration.state = LoopState.GENERATING
            emit("state_change", {"state": "generating", "iteration": i})

            plan_files = plan_json.get("files", [])[:_MAX_FILES_PER_EDIT]
            conventions = [
                ins["description"] for ins in context.get("insights", [])
                if ins.get("category") in ("convention", "pattern")
            ][:5]
            default_repo_path = repo.path

            iter_diffs: list[str] = []
            iter_files: list[str] = []

            for pf in plan_files:
                fpath = pf.get("path", "")
                action = pf.get("action", "modify")
                description = pf.get("description", "")

                if action == "create":
                    create_prompt = (
                        f"Create new file: {fpath}\nRequirements: {description}\n"
                        "Follow project conventions:\n" +
                        "\n".join(f"- {c}" for c in conventions[:3]) +
                        "\n\nReturn ONLY the file content in a code block."
                    )
                    cr = _llm_chat(
                        [{"role": "user", "content": create_prompt}],
                        "You are Chili Code Agent. Generate clean, production-quality code.",
                        f"exec-loop-create-{run_id}-{i}-{fpath}",
                        max_tokens=3000,
                    )
                    m = re.search(r"```\w*\n(.*?)```", cr.get("reply", ""), re.DOTALL)
                    if m:
                        content = m.group(1).strip()
                        diff = f"--- /dev/null\n+++ b/{fpath}\n@@ -0,0 +1,{len(content.splitlines())} @@\n"
                        diff += "\n".join("+" + l for l in content.splitlines())
                        iter_diffs.append(diff)
                        iter_files.append(fpath)
                    continue

                file_content = _read_file_content(default_repo_path, fpath)
                if file_content is None:
                    continue

                edit_system = _build_edit_prompt(fpath, file_content, description, conventions)
                edit_result = _llm_chat(
                    [{"role": "user", "content": f"Apply the change to {fpath} as described."}],
                    edit_system,
                    f"exec-loop-edit-{run_id}-{i}-{fpath}",
                    max_tokens=3000,
                )
                diff_blocks = re.findall(r"```diff\n(.*?)```", edit_result.get("reply", ""), re.DOTALL)
                for d in diff_blocks:
                    validation = _validate_diff(d, fpath, file_content)
                    if validation["valid"]:
                        iter_diffs.append(d)
                        if fpath not in iter_files:
                            iter_files.append(fpath)

            iteration.diffs = iter_diffs
            iteration.files_changed = iter_files

            if not iter_diffs:
                iteration.state = LoopState.FAILED
                iteration.duration_ms = int((time.monotonic() - iter_start) * 1000)
                _persist_iteration(db, run_id, iteration)
                result.iterations.append(iteration)
                if i == 0:
                    result.status = "failed"
                    result.summary = "No valid diffs generated"
                break

            # ── Step 3: Apply ──
            iteration.state = LoopState.APPLYING
            emit("state_change", {"state": "applying", "iteration": i, "files": iter_files})

            applied, apply_msg = _apply_diffs(cwd, iter_diffs)
            iteration.apply_status = "ok" if applied else "failed"

            if not applied:
                iteration.state = LoopState.FAILED
                iteration.duration_ms = int((time.monotonic() - iter_start) * 1000)
                _persist_iteration(db, run_id, iteration)
                result.iterations.append(iteration)
                # Rollback this iteration's commit
                _run_git(cwd, ["reset", "--hard", "HEAD~1"])
                if i == 0:
                    result.status = "failed"
                    result.summary = f"Diffs failed to apply: {apply_msg}"
                break

            all_diffs.extend(iter_diffs)
            all_files_changed.extend(f for f in iter_files if f not in all_files_changed)

            # ── Step 4: Test ──
            iteration.state = LoopState.TESTING
            emit("state_change", {"state": "testing", "iteration": i})

            test_exit, test_output = _run_test_validation(cwd, all_files_changed)
            iteration.test_exit_code = test_exit
            iteration.test_output = test_output

            if test_exit == 0:
                # Success!
                iteration.state = LoopState.DONE
                iteration.duration_ms = int((time.monotonic() - iter_start) * 1000)
                _persist_iteration(db, run_id, iteration)
                result.iterations.append(iteration)
                emit("tests_passed", {"iteration": i})
                result.status = "success"
                result.final_diffs = all_diffs
                result.final_files_changed = all_files_changed
                result.summary = (
                    f"All tests pass after {i + 1} iteration(s). "
                    f"Changed {len(all_files_changed)} file(s)."
                )
                break

            # ── Step 5: Diagnose ──
            iteration.state = LoopState.DIAGNOSING
            emit("state_change", {"state": "diagnosing", "iteration": i})

            diagnosis, error_cat = _diagnose_failure(
                test_output, iter_files, prompt, i,
            )
            iteration.diagnosis = diagnosis
            iteration.error_category = error_cat
            iteration.duration_ms = int((time.monotonic() - iter_start) * 1000)
            _persist_iteration(db, run_id, iteration)
            result.iterations.append(iteration)
            emit("diagnosis_ready", {"error_category": error_cat, "iteration": i})

            # Build fix prompt for next iteration
            current_prompt = (
                f"## Original request\n{prompt}\n\n"
                f"## Previous attempt failed (iteration {i})\n"
                f"### Files changed\n{', '.join(iter_files)}\n\n"
                f"### Test output\n{test_output[:4000]}\n\n"
                f"### Diagnosis\n{diagnosis}\n\n"
                f"## Fix instructions\n"
                f"Fix the errors described above. The previous diffs have already been applied. "
                f"Generate ONLY the additional changes needed to fix the failing tests."
            )
        else:
            # Exhausted all iterations
            if result.status == "running":
                result.status = "max_iterations"
                result.summary = f"Could not fix all issues in {MAX_ITERATIONS} iterations"

    except Exception as e:
        logger.exception("[execution_loop] Unexpected error in run %s", run_id)
        result.status = "failed"
        result.summary = f"Internal error: {str(e)[:500]}"

    # Finalize
    result.total_duration_ms = int((time.monotonic() - start) * 1000)
    result.final_diffs = all_diffs
    result.final_files_changed = all_files_changed

    # If failed, rollback to original branch
    if result.status in ("failed", "max_iterations", "rolled_back"):
        _run_git(cwd, ["checkout", original_branch])
        emit("rolled_back", {"branch": original_branch})

    db.commit()
    emit("loop_complete", {
        "status": result.status,
        "iterations": len(result.iterations),
        "files_changed": result.final_files_changed,
        "duration_ms": result.total_duration_ms,
    })
    return result
