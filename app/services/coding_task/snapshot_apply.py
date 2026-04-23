"""Phase 17: all-or-nothing git apply of stored snapshot diffs at resolved repo root."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sqlalchemy.orm import Session

from ...models import PlanTask
from ...models.coding_task import (
    CodingAgentSuggestion,
    CodingAgentSuggestionApply,
    PlanTaskCodingProfile,
)
from ..code_brain.runtime import resolve_repo_runtime_path
from .envelope import subprocess_safe_env, truncate_text
from .workspaces import (
    get_bound_workspace_repo_for_profile,
    workspace_binding_reason,
)

_AUDIT_MESSAGE_MAX = 4000
_APPLY_TIMEOUT_SEC = 120


def get_suggestion_row_for_apply(
    db: Session,
    task_id: int,
    suggestion_id: int,
) -> CodingAgentSuggestion | None:
    """Load snapshot row for task; apply uses only diffs_json from this row."""
    return (
        db.query(CodingAgentSuggestion)
        .filter(
            CodingAgentSuggestion.id == suggestion_id,
            CodingAgentSuggestion.task_id == task_id,
        )
        .first()
    )


def _repo_root_for_task(db: Session, task: PlanTask, user_id: int) -> Path | None:
    prof = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task.id).first()
    repo = get_bound_workspace_repo_for_profile(db, prof, user_id=user_id)
    if repo is None:
        return None
    return resolve_repo_runtime_path(repo)


def _combine_diffs(diffs: list[str]) -> bytes:
    parts = []
    for d in diffs:
        s = str(d).strip("\n") + "\n"
        parts.append(s)
    return "\n".join(parts).encode("utf-8", errors="replace")


def _run_git_apply(cwd: Path, patch: bytes, *, check_only: bool) -> tuple[int, str]:
    cmd = ["git", "apply", "--whitespace=nowarn"]
    if check_only:
        cmd.append("--check")
    try:
        p = subprocess.run(
            cmd,
            input=patch,
            cwd=str(cwd),
            env=subprocess_safe_env(),
            capture_output=True,
            timeout=_APPLY_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return 124, "git apply timed out"
    except FileNotFoundError:
        return 127, "git executable not found"
    out = (p.stdout or b"").decode("utf-8", errors="replace").strip()
    err = (p.stderr or b"").decode("utf-8", errors="replace").strip()
    msg = err or out or f"exit {p.returncode}"
    return p.returncode, msg


def _bound_audit_message(s: str) -> str:
    t, _ = truncate_text(s, _AUDIT_MESSAGE_MAX)
    return t

def apply_stored_snapshot_diffs(
    db: Session,
    task: PlanTask,
    user_id: int,
    suggestion_id: int,
    *,
    dry_run: bool,
) -> tuple[dict, int]:
    """
    Snapshot-derived diffs only. All-or-nothing: --check on full combined patch first.
    Returns (response_dict, http_status). Always inserts one audit row.
    """
    snap = get_suggestion_row_for_apply(db, task.id, suggestion_id)
    if not snap:
        aid = _insert_audit(
            db,
            suggestion_id=suggestion_id,
            task_id=task.id,
            user_id=user_id,
            dry_run=dry_run,
            status="failed",
            message=_bound_audit_message("Snapshot not found for this task."),
        )
        db.commit()
        return {"ok": False, "message": "Snapshot not found", "audit_id": aid}, 404

    try:
        diffs = json.loads(snap.diffs_json or "[]")
    except json.JSONDecodeError:
        aid = _insert_audit(
            db,
            suggestion_id=suggestion_id,
            task_id=task.id,
            user_id=user_id,
            dry_run=dry_run,
            status="failed",
            message=_bound_audit_message("Invalid diffs_json on snapshot."),
        )
        db.commit()
        return {"ok": False, "message": "Invalid snapshot diffs", "audit_id": aid}, 400

    if not isinstance(diffs, list) or not all(isinstance(x, str) for x in diffs):
        aid = _insert_audit(
            db,
            suggestion_id=suggestion_id,
            task_id=task.id,
            user_id=user_id,
            dry_run=dry_run,
            status="failed",
            message=_bound_audit_message("Snapshot diffs must be a list of strings."),
        )
        db.commit()
        return {"ok": False, "message": "Invalid snapshot diffs shape", "audit_id": aid}, 400

    if len(diffs) == 0:
        aid = _insert_audit(
            db,
            suggestion_id=suggestion_id,
            task_id=task.id,
            user_id=user_id,
            dry_run=dry_run,
            status="failed",
            message=_bound_audit_message("No diffs in snapshot to apply."),
        )
        db.commit()
        return {"ok": False, "message": "No diffs in snapshot", "audit_id": aid}, 400

    root = _repo_root_for_task(db, task, user_id)
    if root is None or not root.is_dir():
        prof = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task.id).first()
        reason = workspace_binding_reason(db, prof, user_id=user_id) or (
            "Could not resolve active CodeRepo root for this task (fail-closed)."
        )
        aid = _insert_audit(
            db,
            suggestion_id=suggestion_id,
            task_id=task.id,
            user_id=user_id,
            dry_run=dry_run,
            status="failed",
            message=_bound_audit_message(reason),
        )
        db.commit()
        return {
            "ok": False,
            "message": reason,
            "workspace_unbound": True,
            "workspace_reason": reason,
            "audit_id": aid,
        }, 409

    patch = _combine_diffs(diffs)
    code, msg = _run_git_apply(root, patch, check_only=True)
    if code != 0:
        aid = _insert_audit(
            db,
            suggestion_id=suggestion_id,
            task_id=task.id,
            user_id=user_id,
            dry_run=dry_run,
            status="failed",
            message=_bound_audit_message(f"git apply --check failed: {msg}"),
        )
        db.commit()
        return {"ok": False, "message": f"Patch does not apply cleanly: {msg}", "audit_id": aid}, 400

    if dry_run:
        aid = _insert_audit(
            db,
            suggestion_id=suggestion_id,
            task_id=task.id,
            user_id=user_id,
            dry_run=True,
            status="completed",
            message=_bound_audit_message(
                f"Dry run OK: {len(diffs)} patch block(s) would apply at repo root."
            ),
        )
        db.commit()
        return {
            "ok": True,
            "dry_run": True,
            "patches": len(diffs),
            "audit_id": aid,
        }, 200

    code2, msg2 = _run_git_apply(root, patch, check_only=False)
    if code2 != 0:
        aid = _insert_audit(
            db,
            suggestion_id=suggestion_id,
            task_id=task.id,
            user_id=user_id,
            dry_run=False,
            status="failed",
            message=_bound_audit_message(f"git apply failed after successful check: {msg2}"),
        )
        db.commit()
        return {
            "ok": False,
            "message": (
                "Apply failed (workspace may be inconsistent; consider git checkout): "
                f"{msg2}"
            ),
            "audit_id": aid,
        }, 400

    aid = _insert_audit(
        db,
        suggestion_id=suggestion_id,
        task_id=task.id,
        user_id=user_id,
        dry_run=False,
        status="completed",
        message=_bound_audit_message(f"Applied {len(diffs)} patch block(s) at repo root."),
    )
    db.commit()
    return {"ok": True, "dry_run": False, "patches": len(diffs), "audit_id": aid}, 200


def _insert_audit(
    db: Session,
    *,
    suggestion_id: int,
    task_id: int,
    user_id: int,
    dry_run: bool,
    status: str,
    message: str,
) -> int:
    row = CodingAgentSuggestionApply(
        suggestion_id=suggestion_id,
        task_id=task_id,
        user_id=user_id,
        dry_run=dry_run,
        status=status,
        message=message,
    )
    db.add(row)
    db.flush()
    return int(row.id)
