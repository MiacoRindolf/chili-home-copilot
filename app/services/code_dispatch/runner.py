"""Sandboxed apply + validate + (optionally) commit/merge.

The runner builds on existing planner_coding capabilities — it does NOT
re-implement diff application or validation. It wraps the existing flow in
a worktree so a failed cycle never touches the working tree.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class RunnerTimeout(Exception):
    """Validation exceeded ``validation_timeout_sec`` (subprocess killed)."""


@dataclass
class WorktreeHandle:
    branch: str
    path: str


@dataclass
class RunOutcome:
    branch: str
    worktree_path: str
    validation_run_id: Optional[int]
    validation_passed: bool
    diff_files: list[str]
    diff_loc: int
    commit_sha: Optional[str]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_dispatch_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "chili-dispatch")


def create_dispatch_worktree(repo_root: str, task_id: int) -> WorktreeHandle:
    branch = f"dispatch/{task_id}"
    base = os.environ.get("CHILI_DISPATCH_WORKTREE_DIR") or _default_dispatch_dir()
    path = os.path.join(base, f"task-{task_id}")
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)

    # ── DO NOT REMOVE — orphan-worktree cleanup (Phase D.2.7+) ─────────
    # The /workspace/.git directory is bind-mounted from the host and
    # persists across scheduler-worker container restarts. The worktree
    # PATHS however live under the container-local /tmp/chili-dispatch
    # tree, which is wiped on every container recreate. Result: stale
    # entries in .git/worktrees/task-N/ point at paths that no longer
    # exist, and `git worktree add` refuses with exit 128.
    #
    # Critical detail: a previous container that was killed mid-operation
    # may have left a `locked` file in .git/worktrees/task-N/, which
    # makes both `prune` and `remove --force` IGNORE the entry. Unlock
    # explicitly first, then prune. All three commands are idempotent
    # and silent on success/no-op, so we always run all three.
    subprocess.run(
        ["git", "worktree", "unlock", path],
        cwd=repo_root, check=False, capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=repo_root, check=False, capture_output=True,
    )

    # If a current-container worktree path still exists on disk, remove
    # it (covers the case where the path is real but the registration
    # is broken, or vice versa).
    if os.path.exists(path):
        subprocess.run(
            ["git", "worktree", "remove", "--force", path],
            cwd=repo_root, check=False, capture_output=True,
        )

    # Capture stderr so the error message tells us WHY git refused — not just
    # exit 128. Common cases: branch already checked out elsewhere, untracked
    # files in target path, dirty index in repo_root, .git/worktrees lock.
    add_proc = subprocess.run(
        ["git", "worktree", "add", "-B", branch, path, "main"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if add_proc.returncode != 0:
        # Snapshot useful state for the audit row's escalation_reason.
        list_proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True,
        )
        diag = (
            f"worktree add exit={add_proc.returncode} "
            f"stderr={(add_proc.stderr or '').strip()[:600]} "
            f"stdout={(add_proc.stdout or '').strip()[:200]} "
            f"existing_worktrees={(list_proc.stdout or '').strip()[:600]}"
        )
        logger.error("[code_dispatch.runner] %s", diag)
        raise subprocess.CalledProcessError(
            add_proc.returncode,
            add_proc.args,
            output=add_proc.stdout,
            stderr=diag,
        )
    return WorktreeHandle(branch=branch, path=path)


def _diff_loc_from_files(files: list[str], worktree: Path) -> int:
    n = 0
    for rel in files:
        p = worktree / rel.replace("\\", "/").lstrip("/")
        if not p.is_file():
            continue
        try:
            n += len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
    return n


# ── Phase E.1: autonomous commit + push helpers ─────────────────────
# All three are no-ops unless CHILI_DISPATCH_GIT_PUSH_ENABLED=1 is set
# in the scheduler-worker environment AND a GitHub token is provided.
# Push goes to the dispatch/<task_id> branch only — never to main/master.

def _git_push_enabled() -> bool:
    return os.environ.get("CHILI_DISPATCH_GIT_PUSH_ENABLED", "0") == "1"


def _git_token() -> Optional[str]:
    tok = (os.environ.get("CHILI_DISPATCH_GITHUB_TOKEN") or "").strip()
    return tok or None


def _git_remote_user() -> Optional[str]:
    val = (os.environ.get("CHILI_DISPATCH_GIT_REMOTE_USER") or "").strip()
    return val or None


def _redact_url(url: str) -> str:
    """Strip the token out of a remote URL before logging."""
    return re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", url)


def commit_and_push(
    repo_root: str,
    handle: WorktreeHandle,
    task_id: int,
    task_title: str,
) -> dict[str, Any]:
    """Stage everything in the worktree, commit, and push the branch.

    Returns a dict with ``committed`` (bool), ``commit_sha`` (str|None),
    ``pushed`` (bool), ``push_url`` (str|None, redacted), ``message`` (str).
    Never raises — all subprocess errors are captured into the result.
    """
    out: dict[str, Any] = {
        "committed": False,
        "commit_sha": None,
        "pushed": False,
        "push_url": None,
        "message": "",
    }
    if not _git_push_enabled():
        out["message"] = "git push disabled (set CHILI_DISPATCH_GIT_PUSH_ENABLED=1)"
        return out

    wt = handle.path
    branch = handle.branch

    # 1. Stage everything that the apply step touched.
    add_proc = subprocess.run(
        ["git", "-C", wt, "add", "-A"],
        capture_output=True, text=True,
    )
    if add_proc.returncode != 0:
        out["message"] = f"git add failed: {(add_proc.stderr or '').strip()[:400]}"
        return out

    # 2. Bail early if there's nothing to commit (apply was a no-op).
    diff_proc = subprocess.run(
        ["git", "-C", wt, "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    if diff_proc.returncode == 0:
        out["message"] = "nothing staged after apply (skipping commit)"
        return out

    # 3. Commit. Author identity is read from the host's .git/config
    # (mounted via /workspace/.git), set up earlier via `git config user.name`.
    safe_title = (task_title or "").replace("\n", " ").replace("\r", " ").strip()[:120]
    commit_msg = f"[dispatch] task {task_id}: {safe_title or '(no title)'}\n\nGenerated by CHILI Code Brain (Phase E reactive)."
    commit_proc = subprocess.run(
        ["git", "-C", wt, "commit", "-m", commit_msg],
        capture_output=True, text=True,
    )
    if commit_proc.returncode != 0:
        out["message"] = f"git commit failed: {(commit_proc.stderr or '').strip()[:400]}"
        return out

    sha_proc = subprocess.run(
        ["git", "-C", wt, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if sha_proc.returncode == 0:
        out["commit_sha"] = (sha_proc.stdout or "").strip()
    out["committed"] = True

    # 4. Push to the dispatch/<task_id> branch via PAT-in-URL.
    token = _git_token()
    remote_user = _git_remote_user()
    if not token or not remote_user:
        out["message"] = (
            "committed but push skipped: "
            f"token_present={bool(token)} remote_user_present={bool(remote_user)}"
        )
        return out

    push_url = f"https://x-access-token:{token}@github.com/{remote_user}.git"
    out["push_url"] = _redact_url(push_url)

    push_proc = subprocess.run(
        ["git", "-C", wt, "push", "--set-upstream", push_url, branch],
        capture_output=True, text=True,
    )
    if push_proc.returncode != 0:
        # Strip token from any echoed URL in the error message.
        err = _redact_url((push_proc.stderr or "").strip())[:600]
        out["message"] = f"committed sha={out['commit_sha']}; push failed: {err}"
        return out

    out["pushed"] = True
    out["message"] = f"committed + pushed sha={out['commit_sha']} branch={branch}"
    logger.info(
        "[code_dispatch.runner] committed + pushed sha=%s branch=%s remote=%s",
        out["commit_sha"], branch, _redact_url(push_url),
    )
    return out


def apply_suggestion_in_worktree(
    db: Session,
    task_id: int,
    user_id: int,
    suggestion_id: int,
    handle: WorktreeHandle,
    *,
    task_title: str = "",
    repo_root: str = "",
) -> dict[str, Any]:
    """Apply stored snapshot diffs inside the worktree via ``git apply`` (same as snapshot_apply).

    Phase E.1: when CHILI_DISPATCH_GIT_PUSH_ENABLED=1, ALSO commits and
    pushes the dispatch/<task_id> branch via PAT auth. The push step is
    safe to leave on by default because:
      - we only push the dispatch/<id> branch, never main
      - token only enters the URL at push time, never logged or persisted
      - push failures don't fail the run (commit_sha is still recorded)

    Returns ``files``, ``loc``, ``ok``, ``message`` plus (when push wired):
    ``commit_sha``, ``pushed``, ``push_url`` (redacted).
    """
    from ..coding_task.snapshot_apply import _combine_diffs, _run_git_apply
    from ..coding_task.snapshot_apply import get_suggestion_row_for_apply

    snap = get_suggestion_row_for_apply(db, task_id, suggestion_id)
    if not snap:
        return {"ok": False, "message": "Snapshot not found", "files": [], "loc": 0}

    try:
        diffs = json.loads(snap.diffs_json or "[]")
    except json.JSONDecodeError:
        return {"ok": False, "message": "Invalid diffs_json", "files": [], "loc": 0}

    if not isinstance(diffs, list) or not all(isinstance(x, str) for x in diffs):
        return {"ok": False, "message": "Invalid diffs shape", "files": [], "loc": 0}

    if not diffs:
        return {"ok": False, "message": "No diffs in snapshot", "files": [], "loc": 0}

    wt = Path(handle.path)
    patch = _combine_diffs(diffs)
    code, msg = _run_git_apply(wt, patch, check_only=True)
    if code != 0:
        return {"ok": False, "message": f"git apply --check failed: {msg}", "files": [], "loc": 0}

    code2, msg2 = _run_git_apply(wt, patch, check_only=False)
    if code2 != 0:
        return {
            "ok": False,
            "message": f"git apply failed: {msg2}",
            "files": [],
            "loc": 0,
        }

    try:
        files_changed = json.loads(snap.files_changed_json or "[]")
    except json.JSONDecodeError:
        files_changed = []
    if not isinstance(files_changed, list):
        files_changed = []
    loc = _diff_loc_from_files([str(x) for x in files_changed], wt)

    result: dict[str, Any] = {
        "ok": True,
        "message": "applied",
        "files": [str(x) for x in files_changed],
        "loc": int(loc),
    }

    # Phase E.1 — commit + push (no-op when CHILI_DISPATCH_GIT_PUSH_ENABLED=0).
    push_outcome = commit_and_push(repo_root, handle, task_id, task_title)
    result["committed"] = bool(push_outcome.get("committed"))
    result["commit_sha"] = push_outcome.get("commit_sha")
    result["pushed"] = bool(push_outcome.get("pushed"))
    result["push_url"] = push_outcome.get("push_url")
    if push_outcome.get("message"):
        # Append the push outcome to the audit message so code_agent_runs
        # sees both the apply success and the push status in one row.
        result["message"] = f"applied; {push_outcome['message']}"

    return result


def run_validation_in_worktree(
    db: Session,
    task_id: int,
    worktree: Path,
    *,
    validation_timeout_sec: int,
) -> tuple[Optional[int], bool, bool]:
    """Run Phase-1 validation in ``worktree`` with a **total** wall-clock cap.

    Creates a ``coding_task_validation_run`` row + artifacts. Does **not**
    mutate ``PlanTask.coding_readiness_state`` (sandbox).

    Returns ``(validation_run_id, passed, timed_out)``. On timeout, run row
    is marked failed and ``timed_out`` is True.
    """
    from ...models import CodingTaskValidationRun, CodingValidationArtifact
    from ..coding_task.blockers import record_blockers_for_run
    from ..coding_task.envelope import truncate_text
    from ..coding_task.validator_runner import StepResult, run_phase1_validation

    run = CodingTaskValidationRun(
        task_id=task_id,
        trigger_source="chili_dispatch",
        status="running",
        started_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    run_id = int(run.id)

    root = str(_project_root())
    wt = str(worktree.resolve())
    code = (
        "import os,sys,pickle;"
        "r=os.environ['CHILI_DP_ROOT'];sys.path.insert(0,r);os.chdir(r);"
        "from pathlib import Path;"
        "from app.services.coding_task.validator_runner import run_phase1_validation;"
        "out=run_phase1_validation(Path(os.environ['CHILI_DP_WT']));"
        "sys.stdout.buffer.write(pickle.dumps(out))"
    )
    env = {**os.environ, "CHILI_DP_ROOT": root, "CHILI_DP_WT": wt}
    timed_out = False
    steps: list[StepResult] = []
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            cwd=root,
            capture_output=True,
            timeout=max(1, int(validation_timeout_sec)),
        )
        if proc.returncode != 0 or not proc.stdout:
            err = (proc.stderr or b"").decode("utf-8", errors="replace")[:4000]
            run.status = "failed"
            run.exit_code = proc.returncode or 1
            run.error_message = err or "validation subprocess failed"
            run.finished_at = datetime.utcnow()
            em = run.error_message or ""
            t_sub, b_sub = truncate_text(em)
            db.add(
                CodingValidationArtifact(
                    run_id=run_id,
                    step_key="subprocess",
                    kind="error",
                    content=t_sub,
                    byte_length=b_sub,
                )
            )
            db.commit()
            return run_id, False, False

        steps = pickle.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        timed_out = True
        run.status = "failed"
        run.exit_code = 1
        run.timed_out = True
        run.error_message = f"validation exceeded {validation_timeout_sec}s (killed)"
        run.finished_at = datetime.utcnow()
        msg, blen = truncate_text(run.error_message or "")
        db.add(
            CodingValidationArtifact(
                run_id=run_id,
                step_key="timeout",
                kind="error",
                content=msg,
                byte_length=blen,
            )
        )
        db.commit()
        return run_id, False, True
    except Exception as e:
        run.status = "failed"
        run.exit_code = 1
        run.error_message = str(e)[:4000]
        run.finished_at = datetime.utcnow()
        msg, blen = truncate_text(str(e))
        db.add(
            CodingValidationArtifact(
                run_id=run_id,
                step_key="internal",
                kind="error",
                content=msg,
                byte_length=blen,
            )
        )
        db.commit()
        return run_id, False, False

    # Persist step artifacts (mirrors service.run_validation_for_task)
    for s in steps:
        merged = ""
        if s.stdout:
            merged += "=== stdout ===\n" + s.stdout
        if s.stderr:
            merged += "\n=== stderr ===\n" + s.stderr
        if s.skip_reason:
            merged += f"\n[skipped: {s.skip_reason}]"
        text, blen = truncate_text(merged or "(no output)")
        db.add(
            CodingValidationArtifact(
                run_id=run_id,
                step_key=s.step_key,
                kind="skip" if s.skipped else "log",
                content=text,
                byte_length=blen,
            )
        )
    try:
        record_blockers_for_run(db, task_id=task_id, run_id=run_id, steps=steps)
    except Exception:
        logger.debug("[runner] record_blockers failed", exc_info=True)

    any_timeout = any(s.timed_out for s in steps)
    failed = any((not s.skipped and (s.timed_out or s.exit_code != 0)) for s in steps)
    run.timed_out = any_timeout
    run.exit_code = 1 if failed else 0
    run.status = "completed"
    run.finished_at = datetime.utcnow()
    db.commit()
    return run_id, not failed, False


def run_outcome_from_parts(
    handle: WorktreeHandle,
    *,
    validation_run_id: Optional[int],
    validation_passed: bool,
    diff_files: list[str],
    diff_loc: int,
) -> RunOutcome:
    return RunOutcome(
        branch=handle.branch,
        worktree_path=handle.path,
        validation_run_id=validation_run_id,
        validation_passed=validation_passed,
        diff_files=diff_files,
        diff_loc=diff_loc,
        commit_sha=None,
    )


def cleanup_worktree(handle: WorktreeHandle, repo_root: str, *, keep_branch: bool = True) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", handle.path], cwd=repo_root, check=False)
    if not keep_branch:
        subprocess.run(["git", "branch", "-D", handle.branch], cwd=repo_root, check=False)


def push_branch(handle: WorktreeHandle, repo_root: str) -> None:
    subprocess.run(["git", "push", "-u", "origin", handle.branch], cwd=repo_root, check=False)


def merge_to_main(handle: WorktreeHandle, repo_root: str) -> Optional[str]:
    """Fast-forward merge of the dispatch branch to main. Returns merged sha, or None on conflict."""
    proc = subprocess.run(
        ["git", "merge", "--ff-only", handle.branch], cwd=repo_root, capture_output=True, text=True
    )
    if proc.returncode != 0:
        logger.warning("[runner] merge --ff-only failed: %s", proc.stderr.strip())
        return None
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
    )
    return rev.stdout.strip()
