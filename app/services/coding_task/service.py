"""Orchestration: coding summary + validation run persistence."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ...models import (
    PlanTask,
    PlanTaskCodingProfile,
    CodingTaskValidationRun,
    CodingValidationArtifact,
    CodingBlockerReport,
)
from .blockers import record_blockers_for_run
from .envelope import list_code_repo_roots, resolve_task_cwd, truncate_text
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


def _profile_dict(db: Session, task_id: int) -> dict:
    p = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task_id).first()
    if not p:
        return {"repo_index": 0, "sub_path": "", "brief_approved_at": None}
    return {
        "repo_index": p.repo_index,
        "sub_path": p.sub_path or "",
        "brief_approved_at": p.brief_approved_at.isoformat() if p.brief_approved_at else None,
    }


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


def _ops_hints_dict(db: Session, task: PlanTask) -> dict:
    """Minimal non-sensitive hints for cwd/repo alignment (counts and booleans only)."""
    roots = list_code_repo_roots()
    n = len(roots)
    p = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task.id).first()
    ri = p.repo_index if p else 0
    repo_index_valid = n == 0 or (0 <= ri < n)
    cwd_resolvable = False
    if n > 0 and repo_index_valid and p is not None:
        try:
            resolve_task_cwd(p.repo_index, p.sub_path or "")
            cwd_resolvable = True
        except Exception:
            cwd_resolvable = False
    return {
        "code_repos_configured_count": n,
        "repo_index_valid": repo_index_valid,
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


def get_coding_summary_dict(db: Session, task: PlanTask) -> dict:
    """Assemble coding summary read-only: no sync_readiness, no commits (Phase 2 read-path contract)."""
    br = latest_brief(db, task.id)
    runs = _validation_runs_for_task(db, task.id, _VALIDATION_RUNS_LIST_DEFAULT_LIMIT)
    return {
        "coding_workflow_mode": task.coding_workflow_mode or "tracked",
        "coding_readiness_state": preview_readiness(db, task),
        "profile": _profile_dict(db, task.id),
        "clarifications": [_clar_dict(c) for c in list_clarifications(db, task.id)],
        "brief": _brief_dict(br),
        "open_clarification_count": open_clarification_count(db, task.id),
        "ops_hints": _ops_hints_dict(db, task),
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
    repo_index: int | None = None,
    sub_path: str | None = None,
) -> dict:
    roots = list_code_repo_roots()
    p = get_or_create_profile(db, task.id)
    if repo_index is not None:
        ri = int(repo_index)
        if roots and ri >= len(roots):
            raise ValueError("repo_index out of range for code_brain_repos.")
        p.repo_index = ri
    if sub_path is not None:
        p.sub_path = sub_path.strip().replace("\\", "/")
    p.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    return _profile_dict(db, task.id)


def run_validation_for_task(
    db: Session,
    task: PlanTask,
    *,
    trigger_source: str = "manual",
) -> dict:
    if trigger_source not in ("manual", "post_apply"):
        trigger_source = "manual"
    assert_ready_for_validation(db, task)
    prof = get_or_create_profile(db, task.id)
    cwd = resolve_task_cwd(prof.repo_index, prof.sub_path or "")

    task.coding_readiness_state = "validation_pending"
    run = CodingTaskValidationRun(
        task_id=task.id,
        trigger_source=trigger_source,
        status="running",
        started_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()

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

    task.updated_at = datetime.utcnow()
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


def build_handoff_dict(db: Session, task: PlanTask) -> dict:
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
        "coding_readiness_state": task.coding_readiness_state or "not_started",
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
        "profile": {
            "repo_index": prof.repo_index if prof else 0,
            "sub_path": (prof.sub_path or "") if prof else "",
        },
        "ops_hints": _ops_hints_dict(db, task),
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
