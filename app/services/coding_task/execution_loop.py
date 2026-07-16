"""Autonomous execution loop: plan -> generate -> apply -> test -> diagnose -> fix -> verify.

Closes the iteration gap that makes a single-shot LLM weaker than a system
with persistent context.  Safety: max iterations, git branch isolation,
automatic rollback on failure.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
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
    """Create a legacy auto branch without switching the operator checkout."""
    branch = f"{_GIT_BRANCH_PREFIX}{run_id[:12]}"
    code, out = _run_git(cwd, ["branch", branch, "HEAD"])
    if code != 0:
        raise RuntimeError(f"Could not create isolated execution branch {branch}: {out}")
    return branch


def _rollback_branch(cwd: Path, original_branch: str, auto_branch: str) -> None:
    """Delete an unmerged legacy branch without switching or resetting checkout state."""
    if _get_current_branch(cwd) == auto_branch:
        raise RuntimeError("Refusing to delete the branch currently checked out by the operator.")
    code, out = _run_git(cwd, ["branch", "-D", auto_branch])
    if code != 0 and "not found" not in out.lower():
        raise RuntimeError(f"Could not delete isolated execution branch {auto_branch}: {out}")


def _get_current_branch(cwd: Path) -> str:
    code, out = _run_git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out.strip() if code == 0 else "main"


def _execution_worktree_path(run_id: str) -> Path:
    root = (Path(tempfile.gettempdir()) / "chili-coding-execution").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / run_id


def _create_execution_worktree(cwd: Path, run_id: str) -> tuple[str, Path, str]:
    """Create an isolated worktree at the operator checkout's current HEAD."""
    base_code, base_out = _run_git(cwd, ["rev-parse", "HEAD"])
    if base_code != 0:
        raise RuntimeError(f"Could not resolve execution base SHA: {base_out}")
    base_sha = base_out.strip()
    branch = f"{_GIT_BRANCH_PREFIX}{run_id[:12]}"
    worktree = _execution_worktree_path(run_id).resolve()
    root = worktree.parent.resolve()
    worktree.relative_to(root)
    if worktree.exists():
        _run_git(cwd, ["worktree", "remove", "--force", str(worktree)], timeout=60)
        if worktree.exists():
            shutil.rmtree(worktree)
    _run_git(cwd, ["worktree", "prune"], timeout=30)
    code, out = _run_git(cwd, ["worktree", "add", "-b", branch, str(worktree), base_sha], timeout=120)
    if code != 0 or not worktree.is_dir():
        raise RuntimeError(f"Could not create isolated execution worktree: {out}")
    return branch, worktree, base_sha


def _cleanup_execution_worktree(
    repo_path: Path,
    worktree: Path,
    branch: str,
    *,
    delete_branch: bool,
) -> None:
    root = (Path(tempfile.gettempdir()) / "chili-coding-execution").resolve()
    resolved = worktree.resolve()
    resolved.relative_to(root)
    _run_git(repo_path, ["worktree", "remove", "--force", str(resolved)], timeout=120)
    if resolved.exists():
        shutil.rmtree(resolved)
    _run_git(repo_path, ["worktree", "prune"], timeout=30)
    if delete_branch:
        code, out = _run_git(repo_path, ["branch", "-D", branch], timeout=30)
        if code != 0 and "not found" not in out.lower():
            logger.warning("[execution_loop] could not delete failed branch %s: %s", branch, out)


def _diff_target_paths(diffs: list[str]) -> list[str]:
    paths: list[str] = []
    for diff in diffs:
        for match in re.finditer(r"^\+\+\+\s+(?:b/)?(.+?)\s*$", diff, re.MULTILINE):
            raw = match.group(1).strip()
            if raw == "/dev/null":
                continue
            path = Path(raw.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"Unsafe generated patch path: {raw}")
            normalized = path.as_posix()
            if normalized and normalized not in paths:
                paths.append(normalized)
    if not paths:
        raise ValueError("Generated patch did not declare a target path.")
    return paths


def _apply_diffs(cwd: Path, diffs: list[str]) -> tuple[bool, str]:
    """Apply unified diffs via git apply. Returns (success, message)."""
    if not diffs:
        return False, "No diffs to apply"
    try:
        target_paths = _diff_target_paths(diffs)
    except ValueError as exc:
        return False, str(exc)
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

    return True, f"Applied successfully to {', '.join(target_paths)}"


def _commit_reviewed_changes(cwd: Path, files_changed: list[str]) -> tuple[bool, str]:
    reviewed = []
    for raw in files_changed:
        path = Path(str(raw).replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            return False, f"Unsafe reviewed path: {raw}"
        rel = path.as_posix()
        if rel and rel not in reviewed:
            reviewed.append(rel)
    if not reviewed:
        return False, "No reviewed files to commit"
    code, out = _run_git(cwd, ["add", "-A", "--", *reviewed], timeout=60)
    if code != 0:
        return False, f"Could not stage reviewed files: {out}"
    code, _ = _run_git(cwd, ["diff", "--cached", "--quiet"], timeout=30)
    if code == 0:
        return False, "Reviewed patch did not produce a staged change"
    code, out = _run_git(cwd, ["commit", "-m", "chili: autonomous code change"], timeout=60)
    if code != 0:
        return False, f"Could not commit reviewed files: {out}"
    return True, "Committed validated reviewed files"


def acceptance_preflight(
    cwd: Path,
    *,
    run_id: str,
    execution_metadata: dict[str, Any],
    files_changed: list[str],
    final_state: str,
    test_exit_code: int | None,
) -> dict[str, Any]:
    """Fail-closed lineage check for the legacy developer-terminal branch."""
    blockers: list[str] = []
    if execution_metadata.get("schema") != "chili.coding-execution-trajectory.v1":
        blockers.append("execution trajectory metadata is missing or unsupported")
    if str(execution_metadata.get("run_id") or "") != run_id:
        blockers.append("run id does not match the recorded execution trajectory")
    if execution_metadata.get("worktree_isolated") is not True:
        blockers.append("execution was not proven to run in an isolated worktree")
    branch = str(execution_metadata.get("branch") or "")
    expected_branch = f"{_GIT_BRANCH_PREFIX}{run_id[:12]}"
    if branch != expected_branch:
        blockers.append("branch does not match the recorded execution run")
    base_branch = str(execution_metadata.get("base_branch") or "")
    base_sha = str(execution_metadata.get("base_sha") or "")
    if not base_branch or not base_sha:
        blockers.append("base branch or base SHA is missing")
    if str(final_state) != LoopState.DONE.value or test_exit_code != 0:
        blockers.append("the latest execution attempt is not a validated success")

    current_branch = _get_current_branch(cwd)
    head_code, head_out = _run_git(cwd, ["rev-parse", "HEAD"])
    current_head = head_out.strip() if head_code == 0 else ""
    if base_branch and current_branch != base_branch:
        blockers.append(f"operator checkout is on {current_branch!r}, expected {base_branch!r}")
    if base_sha and current_head != base_sha:
        blockers.append("operator checkout moved after the execution began")

    branch_code, _ = _run_git(cwd, ["rev-parse", "--verify", branch]) if branch else (1, "")
    if branch_code != 0:
        blockers.append("validated execution branch no longer exists")
    elif base_sha:
        ancestor_code, _ = _run_git(cwd, ["merge-base", "--is-ancestor", base_sha, branch])
        if ancestor_code != 0:
            blockers.append("execution branch is not descended from the recorded base SHA")
        diff_code, diff_out = _run_git(cwd, ["diff", "--name-only", f"{base_sha}..{branch}"])
        actual_files = sorted(line.strip().replace("\\", "/") for line in diff_out.splitlines() if line.strip())
        expected_files = sorted(dict.fromkeys(str(path).replace("\\", "/") for path in files_changed if str(path).strip()))
        if diff_code != 0 or actual_files != expected_files:
            blockers.append("execution branch file set no longer matches the validated attempt")

    status_code, status_out = _run_git(cwd, ["status", "--porcelain"])
    dirty_files = []
    if status_code == 0:
        for line in status_out.splitlines():
            raw = line[3:].split(" -> ")[-1].strip() if len(line) >= 4 else ""
            if raw:
                dirty_files.append(raw.replace("\\", "/"))
    overlap = sorted(set(dirty_files).intersection(str(path).replace("\\", "/") for path in files_changed))
    if overlap:
        blockers.append("operator checkout has dirty changes in the validated execution scope")

    return {
        "ok": not blockers,
        "branch": branch,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "current_branch": current_branch,
        "current_head": current_head,
        "files_changed": sorted(dict.fromkeys(files_changed)),
        "dirty_files": sorted(dict.fromkeys(dirty_files)),
        "blockers": blockers,
        "reason": "; ".join(blockers) if blockers else "Validated execution lineage is current and merge-ready.",
    }


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
    ast_result = run_ast_syntax(cwd, files_changed)
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
    behavior_required = any(
        Path(path).suffix.lower() in {".c", ".cc", ".cpp", ".dart", ".go", ".java", ".js", ".jsx", ".kt", ".py", ".rs", ".swift", ".ts", ".tsx"}
        and not (
            path.replace("\\", "/").lower().startswith("tests/")
            or "/tests/" in path.replace("\\", "/").lower()
            or Path(path).name.lower().startswith("test_")
        )
        for path in files_changed
    )
    metadata = test_result.metadata or {}
    targeted_behavior_ran = bool(
        not test_result.skipped
        and metadata.get("targeted") is True
        and metadata.get("fallback_collect_only") is not True
        and metadata.get("test_files")
    )
    if behavior_required and not targeted_behavior_ran:
        return 2, (
            f"{output}\nTargeted behavior tests did not execute for the changed source files; "
            "collect-only or skipped validation cannot establish autonomous success."
        ).strip()
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

    repo_path = Path(repo.path).resolve()
    if not repo_path.is_dir():
        return LoopResult(run_id=run_id, status="failed", summary=f"Repository path not found: {repo_path}")

    original_branch = _get_current_branch(repo_path)
    try:
        branch, cwd, base_sha = _create_execution_worktree(repo_path, run_id)
    except Exception as exc:
        return LoopResult(
            run_id=run_id,
            status="failed",
            summary=f"Could not create isolated execution worktree: {str(exc)[:500]}",
        )
    emit(
        "worktree_created",
        {
            "branch": branch,
            "run_id": run_id,
            "base_branch": original_branch,
            "base_sha": base_sha,
            "worktree": str(cwd),
        },
    )

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
            if isinstance(plan_json, dict):
                plan_json["_execution"] = {
                    "schema": "chili.coding-execution-trajectory.v1",
                    "run_id": run_id,
                    "repo_id": int(repo_id),
                    "base_branch": original_branch,
                    "base_sha": base_sha,
                    "branch": branch,
                    "worktree_isolated": True,
                }
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
            default_repo_path = str(cwd)

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
                committed, commit_message = _commit_reviewed_changes(cwd, all_files_changed)
                if not committed:
                    iteration.state = LoopState.FAILED
                    iteration.apply_status = "commit_failed"
                    iteration.test_output = f"{test_output}\n{commit_message}".strip()
                    iteration.duration_ms = int((time.monotonic() - iter_start) * 1000)
                    _persist_iteration(db, run_id, iteration)
                    result.iterations.append(iteration)
                    result.status = "failed"
                    result.summary = commit_message
                    break
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

    delete_branch = result.status != "success"
    try:
        _cleanup_execution_worktree(
            repo_path,
            cwd,
            branch,
            delete_branch=delete_branch,
        )
    except Exception as exc:
        logger.exception("[execution_loop] failed to clean worktree for run %s", run_id)
        if result.status != "success":
            result.summary = f"{result.summary}; cleanup failed: {str(exc)[:300]}".strip("; ")
    if delete_branch:
        emit("rolled_back", {"branch": original_branch, "operator_checkout_untouched": True})
    else:
        emit("validated_branch_ready", {"branch": branch, "base_sha": base_sha})

    db.commit()
    emit("loop_complete", {
        "status": result.status,
        "iterations": len(result.iterations),
        "files_changed": result.final_files_changed,
        "duration_ms": result.total_duration_ms,
    })
    return result
