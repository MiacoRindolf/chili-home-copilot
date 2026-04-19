"""PO v2 first slice: clarifications, brief, readiness sync (no implementation automation)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...models import PlanTask, PlanTaskCodingProfile, TaskClarification, CodingTaskBrief


WORKFLOW_MODES = frozenset({"tracked", "planned", "assisted", "executing"})
READINESS_STATES = frozenset(
    {
        "not_started",
        "needs_clarification",
        "brief_ready",
        "validation_pending",
        "blocked",
        "ready_for_future_impl",
    }
)


def get_or_create_profile(db: Session, task_id: int) -> PlanTaskCodingProfile:
    p = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task_id).first()
    if p:
        return p
    p = PlanTaskCodingProfile(task_id=task_id)
    db.add(p)
    db.flush()
    return p


def latest_brief(db: Session, task_id: int) -> CodingTaskBrief | None:
    return (
        db.query(CodingTaskBrief)
        .filter(CodingTaskBrief.task_id == task_id)
        .order_by(CodingTaskBrief.id.desc())
        .first()
    )


def open_clarification_count(db: Session, task_id: int) -> int:
    return (
        db.query(func.count(TaskClarification.id))
        .filter(
            TaskClarification.task_id == task_id,
            TaskClarification.status == "open",
        )
        .scalar()
        or 0
    )


def open_clarification_ids(db: Session, task_id: int) -> list[int]:
    rows = (
        db.query(TaskClarification.id)
        .filter(
            TaskClarification.task_id == task_id,
            TaskClarification.status == "open",
        )
        .order_by(TaskClarification.id)
        .all()
    )
    return [r[0] for r in rows]


def _compute_readiness_state(db: Session, task: PlanTask) -> str:
    """Single source of truth for the derived ``coding_readiness_state``.

    Pure: does not mutate any row. ``preview_readiness`` and ``sync_readiness``
    both delegate here so the summary payload, handoff payload, and the
    persisted value can never diverge (A4).
    """
    stored = task.coding_readiness_state or "not_started"
    # Terminal stored states are authoritative — they reflect a validation
    # run outcome that the derived signals cannot overturn on their own.
    if stored == "validation_pending":
        return "validation_pending"
    if stored in ("blocked", "ready_for_future_impl"):
        # For blocked/ready we still upgrade to needs_clarification if the
        # user has added a new open question, so the UI nags them correctly;
        # sync_readiness has that same precedence below.
        oc = open_clarification_count(db, task.id)
        if oc > 0 and stored == "ready_for_future_impl":
            # A completed task with a new open clarification goes back to
            # needs_clarification — this matches sync_readiness's ordering
            # where clarifications win over brief state.
            return "needs_clarification"
        return stored

    oc = open_clarification_count(db, task.id)
    if oc > 0:
        return "needs_clarification"

    br = latest_brief(db, task.id)
    body = (br.body or "").strip() if br else ""
    if not body:
        return "needs_clarification"

    prof = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task.id).first()
    if prof is None or prof.brief_approved_at is None:
        return "brief_ready"

    return "brief_ready"


def preview_readiness(db: Session, task: PlanTask) -> str:
    """Read-only readiness for API summaries. Delegates to
    ``_compute_readiness_state``; does not mutate.
    """
    return _compute_readiness_state(db, task)


def sync_readiness(db: Session, task: PlanTask) -> None:
    """Derive ``coding_readiness_state`` and persist the result onto ``task``.
    Delegates state computation to ``_compute_readiness_state`` — the only
    writer of readiness-derived state in the system (A4).
    """
    if task.coding_readiness_state == "validation_pending":
        return
    if task.coding_readiness_state in ("blocked", "ready_for_future_impl"):
        # Preserve terminal stored states unless a new clarification warrants
        # reopening — mirrors _compute_readiness_state's precedence.
        if task.coding_readiness_state == "ready_for_future_impl":
            oc = open_clarification_count(db, task.id)
            if oc > 0:
                task.coding_readiness_state = "needs_clarification"
        return
    task.coding_readiness_state = _compute_readiness_state(db, task)


def add_clarification(db: Session, task: PlanTask, question: str, user_id: int | None) -> TaskClarification:
    get_or_create_profile(db, task.id)
    mx = (
        db.query(func.max(TaskClarification.sort_order))
        .filter(TaskClarification.task_id == task.id)
        .scalar()
        or 0
    )
    row = TaskClarification(
        task_id=task.id,
        question=question.strip(),
        status="open",
        sort_order=int(mx) + 1,
    )
    db.add(row)
    db.flush()
    sync_readiness(db, task)
    return row


def answer_clarification(
    db: Session,
    task: PlanTask,
    clarification_id: int,
    answer: str,
) -> TaskClarification | None:
    row = (
        db.query(TaskClarification)
        .filter(
            TaskClarification.id == clarification_id,
            TaskClarification.task_id == task.id,
        )
        .first()
    )
    if not row:
        return None
    row.answer = answer.strip()
    row.status = "resolved"
    row.updated_at = datetime.utcnow()
    sync_readiness(db, task)
    return row


def upsert_brief(db: Session, task: PlanTask, body: str, user_id: int | None) -> CodingTaskBrief:
    prev = latest_brief(db, task.id)
    ver = (prev.version + 1) if prev else 1
    row = CodingTaskBrief(task_id=task.id, body=body or "", version=ver, created_by=user_id)
    db.add(row)
    prof = get_or_create_profile(db, task.id)
    prof.brief_approved_at = None
    prof.updated_at = datetime.utcnow()
    db.flush()
    task.coding_readiness_state = "not_started"
    sync_readiness(db, task)
    return row


def approve_brief(db: Session, task: PlanTask) -> None:
    br = latest_brief(db, task.id)
    if not br or not (br.body or "").strip():
        raise ValueError("Brief is empty.")
    oc = open_clarification_count(db, task.id)
    if oc > 0:
        raise ValueError(
            "Resolve open clarifications before approving the brief."
            f" ({oc} open)"
        )
    prof = get_or_create_profile(db, task.id)
    prof.brief_approved_at = datetime.utcnow()
    prof.updated_at = datetime.utcnow()
    db.flush()
    sync_readiness(db, task)


def list_clarifications(db: Session, task_id: int) -> list[TaskClarification]:
    return (
        db.query(TaskClarification)
        .filter(TaskClarification.task_id == task_id)
        .order_by(TaskClarification.sort_order, TaskClarification.id)
        .all()
    )


def validate_workflow_mode(mode: str | None) -> str:
    if mode in WORKFLOW_MODES:
        return mode  # type: ignore[return-value]
    return "tracked"


def validate_readiness(state: str | None) -> str:
    if state in READINESS_STATES:
        return state  # type: ignore[return-value]
    return "not_started"


def reopen_from_blocked_for_edit(db: Session, task: PlanTask) -> None:
    """
    Phase 6: leave blocked for PO edits. Schema-free: only mutates plan_tasks and,
    if a profile row already exists, clears brief_approved_at. Does not create a profile.
    Target state: non-empty latest brief body -> brief_ready, else not_started.
    """
    if (task.coding_readiness_state or "") != "blocked":
        raise ValueError("Reopen is only allowed when coding readiness is blocked.")

    br = latest_brief(db, task.id)
    body = (br.body or "").strip() if br else ""
    if body:
        task.coding_readiness_state = "brief_ready"
    else:
        task.coding_readiness_state = "not_started"

    prof = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task.id).first()
    if prof is not None:
        prof.brief_approved_at = None
        prof.updated_at = datetime.utcnow()
    db.flush()


def assert_ready_for_validation(db: Session, task: PlanTask) -> None:
    oc = open_clarification_count(db, task.id)
    if oc > 0:
        raise ValueError(
            f"Open clarifications must be resolved before validation. ({oc} open)"
        )
    br = latest_brief(db, task.id)
    if not br or not (br.body or "").strip():
        raise ValueError("Implementation brief is required before validation.")
    prof = db.query(PlanTaskCodingProfile).filter(PlanTaskCodingProfile.task_id == task.id).first()
    if prof is None or prof.brief_approved_at is None:
        raise ValueError("Approve the implementation brief before running validation.")
