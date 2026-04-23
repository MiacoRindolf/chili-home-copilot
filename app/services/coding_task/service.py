"""Orchestration: coding summary + validation run persistence + autonomous execution."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from ...models import (
    PlanTask,
    PlanTaskCodingProfile,
    CodingTaskValidationRun,
    CodingValidationArtifact,
    CodingBlockerReport,
)
from ...models.code_brain import CodeRepo
from .blockers import record_blockers_for_run
from .envelope import list_code_repo_roots, truncate_text
from .po_v2 import (
    assert_ready_for_validation,
    get_or_create_profile,
    latest_brief,
    list_clarifications,
    open_clarification_count,
    preview_readiness,
    sync_readiness,
)
from .validator_runner import run_phase1_validation
from .workflow_state import sync_task_workflow_state
from .workspaces import (
    bind_profile_workspace,
    build_workspace_binding_dict,
    first_reachable_workspace_repo,
    get_bound_workspace_repo_for_profile,
    resolve_profile_cwd,
    select_runtime_workspace_repo_for_task,
    workspace_binding_reason,
)


def _profile_dict(db: Session, task_id: int, *, user_id: int | None = None) -> dict:
    p = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task_id).first()
    if not p:
        profile = build_workspace_binding_dict(db, None, user_id=user_id)
        profile["brief_approved_at"] = None
        return profile
    profile = build_workspace_binding_dict(db, p, user_id=user_id)
    profile["brief_approved_at"] = p.brief_approved_at.isoformat() if p.brief_approved_at else None
    return profile


def _clar_dict(c) -> dict:
    return {
        "id": c.id,
        "task_id": c.task_id,
        "question": c.question,
        "answer": c.answer,
        "status": c.status,
        "sort_order": c.sort_order,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _brief_dict(b) -> dict | None:
    if not b:
        return None
    return {
        "id": b.id,
        "body": b.body or "",
        "version": b.version,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


def _ops_hints_dict(db: Session, task: PlanTask, *, user_id: int | None = None) -> dict:
    """Minimal non-sensitive hints for cwd/repo alignment (counts and booleans only)."""
    roots = list_code_repo_roots()
    p = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task.id).first()
    ri = p.repo_index if p else 0
    repo_index_valid = len(roots) == 0 or (0 <= ri < len(roots))
    repo = get_bound_workspace_repo_for_profile(db, p, user_id=user_id)
    cwd_resolvable = False
    if p is not None and repo is not None:
        try:
            resolve_profile_cwd(db, p, user_id=user_id)
            cwd_resolvable = True
        except ValueError:
            cwd_resolvable = False
    repo_count_q = db.query(CodeRepo).filter(CodeRepo.active.is_(True))
    if user_id is not None:
        repo_count_q = repo_count_q.filter(
            (CodeRepo.user_id == user_id) | (CodeRepo.user_id.is_(None))
        )
    return {
        "code_repos_configured_count": repo_count_q.count(),
        "repo_index_valid": repo_index_valid,
        "workspace_bound": repo is not None,
        "workspace_indexed": bool(
            repo
            and not repo.last_index_error
            and (
                repo.last_successful_indexed_at
                or repo.last_indexed
                or (repo.last_successful_file_count or repo.file_count or 0) > 0
            )
        ),
        "workspace_reason": workspace_binding_reason(db, p, user_id=user_id),
        "cwd_resolvable": cwd_resolvable,
    }


# Phase 8: shared validation-run query (newest first = id DESC). Summary uses raw error_message; list GET truncates.
_VALIDATION_RUNS_LIST_DEFAULT_LIMIT = 15
_VALIDATION_RUNS_LIST_MAX_LIMIT = 50


def _validation_runs_for_task(db: Session, task_id: int, limit: int) -> list:
    return (
        db.query(CodingTaskValidationRun)
        .filter(CodingTaskValidationRun.task_id == task_id)
        .order_by(CodingTaskValidationRun.id.desc())
        .limit(limit)
        .all()
    )


def get_coding_summary_dict(db: Session, task: PlanTask, *, user_id: int | None = None) -> dict:
    """Assemble coding summary read-only: no sync_readiness, no commits (Phase 2 read-path contract)."""
    br = latest_brief(db, task.id)
    runs = _validation_runs_for_task(db, task.id, _VALIDATION_RUNS_LIST_DEFAULT_LIMIT)
    return {
        "coding_workflow_mode": task.coding_workflow_mode or "tracked",
        "coding_readiness_state": preview_readiness(db, task),
        "profile": _profile_dict(db, task.id, user_id=user_id),
        "clarifications": [_clar_dict(c) for c in list_clarifications(db, task.id)],
        "brief": _brief_dict(br),
        "open_clarification_count": open_clarification_count(db, task.id),
        "ops_hints": _ops_hints_dict(db, task, user_id=user_id),
        "validation_runs": [
            {
                "id": r.id,
                "status": r.status,
                "trigger_source": r.trigger_source,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "exit_code": r.exit_code,
                "timed_out": r.timed_out,
                "error_message": r.error_message,
            }
            for r in runs
        ],
    }


def update_coding_profile(
    db: Session,
    task: PlanTask,
    *,
    code_repo_id: int | None = None,
    repo_name: str | None = None,
    repo_index: int | None = None,
    sub_path: str | None = None,
    user_id: int | None = None,
) -> dict:
    roots = list_code_repo_roots()
    p = get_or_create_profile(db, task.id)
    if repo_index is not None:
        ri = int(repo_index)
        if ri < 0 or (roots and ri >= len(roots)):
            raise ValueError("repo_index out of range for code_brain_repos.")
    if code_repo_id is not None or repo_name is not None or repo_index is not None:
        bind_profile_workspace(
            db,
            p,
            code_repo_id=code_repo_id,
            repo_name=repo_name,
            repo_index=repo_index,
            user_id=user_id,
        )
    if sub_path is not None:
        p.sub_path = sub_path.strip().replace("\\", "/")
    p.updated_at = datetime.utcnow()
    sync_task_workflow_state(db, task, user_id=user_id)
    db.commit()
    db.refresh(task)
    return _profile_dict(db, task.id, user_id=user_id)


def run_validation_for_task(
    db: Session,
    task: PlanTask,
    *,
    user_id: int | None = None,
    trigger_source: str = "manual",
) -> dict:
    if trigger_source not in ("manual", "post_apply"):
        trigger_source = "manual"
    assert_ready_for_validation(db, task)
    prof = get_or_create_profile(db, task.id)
    # Fail-closed preflight: resolve cwd BEFORE mutating any state so a stale
    # workspace binding surfaces as 409 with no task-state side effects.
    cwd = resolve_profile_cwd(db, prof, user_id=user_id)

    # Persist the run row before the task state transition so the run id
    # always anchors the "validation_pending" window. The transition is only
    # visible to other readers after the final commit below — the finally
    # guard below guarantees we never commit with a non-terminal pair.
    run = CodingTaskValidationRun(
        task_id=task.id,
        trigger_source=trigger_source,
        status="running",
        started_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    task.coding_readiness_state = "validation_pending"

    try:
        try:
            steps = run_phase1_validation(cwd)
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
                        run_id=run.id,
                        step_key=s.step_key,
                        kind="skip" if s.skipped else "log",
                        content=text,
                        byte_length=blen,
                    )
                )
            record_blockers_for_run(db, task_id=task.id, run_id=run.id, steps=steps)
            any_timeout = any(s.timed_out for s in steps)
            failed = any((not s.skipped and (s.timed_out or s.exit_code != 0)) for s in steps)
            run.timed_out = any_timeout
            run.exit_code = 1 if failed else 0
            run.status = "completed"
            run.finished_at = datetime.utcnow()
            if failed:
                task.coding_readiness_state = "blocked"
            else:
                task.coding_readiness_state = "ready_for_future_impl"
        except ValueError as e:
            run.status = "failed"
            run.exit_code = 1
            run.error_message = str(e)[:4000]
            run.finished_at = datetime.utcnow()
            task.coding_readiness_state = "blocked"
            msg, blen = truncate_text(str(e))
            db.add(
                CodingValidationArtifact(
                    run_id=run.id,
                    step_key="envelope",
                    kind="error",
                    content=msg,
                    byte_length=blen,
                )
            )
        except Exception as e:
            run.status = "failed"
            run.exit_code = 1
            run.error_message = str(e)[:4000]
            run.finished_at = datetime.utcnow()
            task.coding_readiness_state = "blocked"
            msg, blen = truncate_text(str(e))
            db.add(
                CodingValidationArtifact(
                    run_id=run.id,
                    step_key="internal",
                    kind="error",
                    content=msg,
                    byte_length=blen,
                )
            )
    finally:
        # Terminal-state guarantee: if any handler above leaves the run or task
        # in a non-terminal state (e.g. an exception raised inside an except
        # block), coerce both to the blocked/failed terminus before commit so
        # the (task_state, run_status) pair can never observe as
        # (validation_pending, running) outside this transaction.
        if run.status == "running":
            run.status = "failed"
            run.exit_code = 1 if run.exit_code is None else run.exit_code
            run.error_message = run.error_message or "validation run ended without a terminal status"
            run.finished_at = run.finished_at or datetime.utcnow()
        if task.coding_readiness_state == "validation_pending":
            task.coding_readiness_state = "blocked"

    task.updated_at = datetime.utcnow()
    sync_task_workflow_state(db, task, user_id=user_id)
    db.commit()
    db.refresh(run)
    db.refresh(task)
    return {
        "run": {
            "id": run.id,
            "status": run.status,
            "exit_code": run.exit_code,
            "timed_out": run.timed_out,
            "error_message": run.error_message,
        },
        "coding_readiness_state": task.coding_readiness_state,
    }


# Phase 5 handoff: strict allowlist + truncation (bytes); projection-only, no writes.
_HANDOFF_BRIEF_MAX_BYTES = 32_000
_HANDOFF_ERR_MAX_BYTES = 4_000
_HANDOFF_BLOCKER_SUMMARY_MAX_BYTES = 2_000
_HANDOFF_ARTIFACT_PREVIEW_MAX_BYTES = 8_000
_HANDOFF_BLOCKERS_LIMIT = 20
_HANDOFF_ARTIFACTS_LIMIT = 5
# Phase 7: task_clarification rows only (no chat/agent/cross-task).
_HANDOFF_CLARIFICATIONS_LIMIT = 50
_HANDOFF_CLAR_QUESTION_MAX_BYTES = 12_000
_HANDOFF_CLAR_ANSWER_MAX_BYTES = 24_000


def list_validation_runs_metadata_dict(db: Session, task_id: int, limit: int | None) -> list[dict]:
    """
    Phase 8: metadata-only for GET .../coding/validation/runs. Truncates error_message only here;
    no artifacts, blockers, or brief context. Read-only.
    """
    if limit is None:
        n = _VALIDATION_RUNS_LIST_DEFAULT_LIMIT
    else:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = _VALIDATION_RUNS_LIST_DEFAULT_LIMIT
    if n < 1:
        n = _VALIDATION_RUNS_LIST_DEFAULT_LIMIT
    n = min(n, _VALIDATION_RUNS_LIST_MAX_LIMIT)

    rows = _validation_runs_for_task(db, task_id, n)
    out: list[dict] = []
    for r in rows:
        em = r.error_message
        if em:
            em_t, _ = truncate_text(em, _HANDOFF_ERR_MAX_BYTES)
        else:
            em_t = None
        out.append(
            {
                "id": r.id,
                "status": r.status,
                "trigger_source": r.trigger_source,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "exit_code": r.exit_code,
                "timed_out": r.timed_out,
                "error_message": em_t,
            }
        )
    return out


def build_handoff_dict(db: Session, task: PlanTask, *, user_id: int | None = None) -> dict:
    """
    Read-only implementation handoff: existing rows only, latest validation run only for
    artifact_previews. Phase 7: clarifications from task_clarification only; readiness_context
    from stored task/profile/clar counts only (no preview_readiness, sync_readiness,
    get_or_create_profile, validator, or commits).
    """
    br = latest_brief(db, task.id)
    prof = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task.id).first()

    latest = (
        db.query(CodingTaskValidationRun)
        .filter(CodingTaskValidationRun.task_id == task.id)
        .order_by(CodingTaskValidationRun.id.desc())
        .first()
    )

    blockers_rows = (
        db.query(CodingBlockerReport)
        .filter(CodingBlockerReport.task_id == task.id)
        .order_by(CodingBlockerReport.id.desc())
        .limit(_HANDOFF_BLOCKERS_LIMIT)
        .all()
    )

    artifact_previews: list[dict] = []
    if latest is not None:
        arts = (
            db.query(CodingValidationArtifact)
            .filter(CodingValidationArtifact.run_id == latest.id)
            .order_by(CodingValidationArtifact.id.asc())
            .limit(_HANDOFF_ARTIFACTS_LIMIT)
            .all()
        )
        for a in arts:
            prev, _ = truncate_text(a.content or "", _HANDOFF_ARTIFACT_PREVIEW_MAX_BYTES)
            artifact_previews.append(
                {
                    "step_key": a.step_key,
                    "kind": a.kind,
                    "content_preview": prev,
                }
            )

    brief_out = None
    if br:
        body_t, _ = truncate_text(br.body or "", _HANDOFF_BRIEF_MAX_BYTES)
        brief_out = {
            "id": br.id,
            "version": br.version,
            "body": body_t,
        }

    validation_latest = None
    if latest is not None:
        em = latest.error_message or ""
        if em:
            em_t, _ = truncate_text(em, _HANDOFF_ERR_MAX_BYTES)
        else:
            em_t = None
        validation_latest = {
            "id": latest.id,
            "status": latest.status,
            "trigger_source": latest.trigger_source,
            "exit_code": latest.exit_code,
            "timed_out": latest.timed_out,
            "error_message": em_t,
            "started_at": latest.started_at.isoformat() if latest.started_at else None,
            "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
        }

    blockers_out = []
    for r in blockers_rows:
        sm, _ = truncate_text(r.summary or "", _HANDOFF_BLOCKER_SUMMARY_MAX_BYTES)
        blockers_out.append(
            {
                "category": r.category,
                "severity": r.severity,
                "summary": sm,
            }
        )

    clar_rows = list_clarifications(db, task.id)[:_HANDOFF_CLARIFICATIONS_LIMIT]
    clarifications_out: list[dict] = []
    for c in clar_rows:
        q_t, _ = truncate_text(c.question or "", _HANDOFF_CLAR_QUESTION_MAX_BYTES)
        ans = c.answer
        if ans:
            a_t, _ = truncate_text(ans, _HANDOFF_CLAR_ANSWER_MAX_BYTES)
        else:
            a_t = None
        clarifications_out.append(
            {
                "id": c.id,
                "question": q_t,
                "answer": a_t,
                "status": c.status,
                "sort_order": c.sort_order,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
        )

    readiness_context = {
        # A4: single source of truth — same derivation as get_coding_summary_dict.
        "coding_readiness_state": preview_readiness(db, task),
        "open_clarification_count": open_clarification_count(db, task.id),
        "brief_approved_at": prof.brief_approved_at.isoformat() if prof and prof.brief_approved_at else None,
    }

    return {
        "task": {
            "id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "coding_readiness_state": task.coding_readiness_state or "not_started",
            "coding_workflow_mode": task.coding_workflow_mode or "tracked",
        },
        "brief": brief_out,
        "profile": _profile_dict(db, task.id, user_id=user_id),
        "selected_repo": select_runtime_workspace_repo_for_task(db, task.id, user_id=user_id),
        "ops_hints": _ops_hints_dict(db, task, user_id=user_id),
        "validation_latest": validation_latest,
        "blockers": blockers_out,
        "artifact_previews": artifact_previews,
        "clarifications": clarifications_out,
        "readiness_context": readiness_context,
    }


def list_blockers_dict(db: Session, task_id: int, limit: int = 50) -> list[dict]:
    rows = (
        db.query(CodingBlockerReport)
        .filter(CodingBlockerReport.task_id == task_id)
        .order_by(CodingBlockerReport.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "run_id": r.run_id,
            "category": r.category,
            "severity": r.severity,
            "summary": r.summary,
            "detail_json": r.detail_json,
        }
        for r in rows
    ]


def get_run_detail_dict(db: Session, task_id: int, run_id: int) -> dict | None:
    run = (
        db.query(CodingTaskValidationRun)
        .filter(
            CodingTaskValidationRun.id == run_id,
            CodingTaskValidationRun.task_id == task_id,
        )
        .first()
    )
    if not run:
        return None
    arts = (
        db.query(CodingValidationArtifact)
        .filter(CodingValidationArtifact.run_id == run.id)
        .order_by(CodingValidationArtifact.id.asc())
        .all()
    )
    return {
        "id": run.id,
        "status": run.status,
        "trigger_source": run.trigger_source,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "exit_code": run.exit_code,
        "timed_out": run.timed_out,
        "error_message": run.error_message,
        "artifacts": [
            {
                "id": a.id,
                "step_key": a.step_key,
                "kind": a.kind,
                "content": a.content,
                "byte_length": a.byte_length,
            }
            for a in arts
        ],
    }


# ── Autonomous execution ──────────────────────────────────────────


def _first_active_repo(db: Session, user_id: int | None = None) -> CodeRepo | None:
    return first_reachable_workspace_repo(db, user_id=user_id)


def run_autonomous_task(
    db: Session,
    prompt: str,
    *,
    repo_id: int | None = None,
    user_id: int | None = None,
    on_progress: Callable[[str, dict], None] | None = None,
) -> dict:
    """Run the autonomous execution loop for a natural-language coding request.

    If ``repo_id`` is not provided, uses the first active CodeRepo.
    Returns a summary dict suitable for JSON serialization.
    """
    from .execution_loop import run_execution_loop

    if repo_id is None:
        repo = _first_active_repo(db, user_id)
        if repo is None:
            return {"ok": False, "error": "No active code repository registered. Add one first."}
        repo_id = repo.id

    result = run_execution_loop(
        db, prompt, repo_id, user_id=user_id, on_progress=on_progress,
    )

    return {
        "ok": result.status == "success",
        "run_id": result.run_id,
        "status": result.status,
        "branch": result.branch_name,
        "iterations": len(result.iterations),
        "files_changed": result.final_files_changed,
        "diffs": result.final_diffs,
        "duration_ms": result.total_duration_ms,
        "summary": result.summary,
        "iteration_details": [
            {
                "iteration": it.iteration,
                "state": it.state,
                "test_exit_code": it.test_exit_code,
                "error_category": it.error_category,
                "diagnosis": it.diagnosis[:500] if it.diagnosis else None,
                "files_changed": it.files_changed,
                "duration_ms": it.duration_ms,
            }
            for it in result.iterations
        ],
    }
