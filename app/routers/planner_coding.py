"""Phase 1: task-centric coding API (PO v2 + validation). Mounted under planner router."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..deps import get_db, require_project_domain_enabled
from ..pairing import DEVICE_COOKIE_NAME, get_identity_record
from ..models import PlanTask
from ..services import planner_service
from ..services.coding_task import po_v2
from ..services.coding_task.workspaces import WorkspaceUnbound
from ..services.coding_task.workflow_state import (
    WorkflowTransitionBlocked,
    assert_transition_allowed,
)
from ..services.coding_task.agent_suggest import run_agent_suggest_for_task
from ..services.coding_task.telemetry import log_event as _log_coding_event, timed_step as _coding_timed
from ..services.coding_task.agent_suggestion_store import (
    bound_payload_for_save,
    coerce_list_limit,
    get_suggestion_detail_dict,
    insert_suggestion,
    list_suggestion_metadata,
)

from ..services.coding_task.apply_audit_metadata import list_apply_attempts_metadata_dict
from ..services.coding_task.snapshot_apply import apply_stored_snapshot_diffs
from ..services.coding_task.service import (
    build_handoff_dict,
    get_coding_summary_dict,
    get_run_detail_dict,
    list_blockers_dict,
    list_validation_runs_metadata_dict,
    run_validation_for_task,
    update_coding_profile,
)

router = APIRouter(
    prefix="/api/planner",
    tags=["planner-coding"],
    dependencies=[Depends(require_project_domain_enabled)],
)


def _preflight_error_payload(db: Session, task: PlanTask, message: str) -> dict:
    oc = po_v2.open_clarification_count(db, task.id)
    payload: dict = {"error": message}
    if oc > 0:
        payload["open_clarification_count"] = oc
        payload["open_clarification_ids"] = po_v2.open_clarification_ids(db, task.id)
    return payload


# Named HTTP status constants (A7) — avoid magic numbers scattered at call sites.
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409


def _workflow_blocked_payload(exc: WorkflowTransitionBlocked) -> dict:
    return {
        "error": str(exc),
        "workflow_blocked": True,
        "action": exc.action,
        "current_state": exc.current,
        "required_state": exc.required,
    }


def _require_user(request: Request, db: Session) -> dict | None:
    identity = get_identity_record(db, request.cookies.get(DEVICE_COOKIE_NAME))
    if identity["is_guest"] or not identity.get("user_id"):
        return None
    return identity


def _get_task_editable(db: Session, task_id: int, user_id: int) -> PlanTask | None:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t:
        return None
    if not planner_service._user_can_edit(db, t.project_id, user_id):
        return None
    return t


def _get_task_readable(db: Session, task_id: int, user_id: int) -> PlanTask | None:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t:
        return None
    if not planner_service._user_can_access(db, t.project_id, user_id):
        return None
    return t


def require_editable_task(
    request: Request,
    db: Session,
    task_id: int,
) -> tuple[dict, PlanTask] | JSONResponse:
    """A7: single auth + lookup helper for mutation endpoints.

    Returns ``(identity, task)`` on success or a ``JSONResponse`` with the
    appropriate HTTP status (403 / 404) on failure. Endpoints use::

        res = require_editable_task(request, db, task_id)
        if isinstance(res, JSONResponse):
            return res
        identity, t = res
    """
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=HTTP_FORBIDDEN)
    t = _get_task_editable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=HTTP_NOT_FOUND)
    return identity, t


def require_readable_task(
    request: Request,
    db: Session,
    task_id: int,
) -> tuple[dict, PlanTask] | JSONResponse:
    """A7: single auth + lookup helper for read endpoints."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=HTTP_FORBIDDEN)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=HTTP_NOT_FOUND)
    return identity, t


def _clar_row_dict(c) -> dict:
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


class ClarificationCreateBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class ClarificationAnswerBody(BaseModel):
    answer: str = Field(min_length=1, max_length=8000)


class BriefBody(BaseModel):
    body: str = Field(default="", max_length=100_000)


class ProfileUpdateBody(BaseModel):
    code_repo_id: int | None = Field(default=None, ge=1)
    repo_name: str | None = Field(default=None, max_length=200)
    repo_index: int | None = Field(default=None, ge=0)
    sub_path: str | None = Field(default=None, max_length=2000)


class AgentSuggestBody(BaseModel):
    extra_instructions: str | None = Field(default=None, max_length=2000)


class AgentSuggestionSaveBody(BaseModel):
    """Phase 16: exact Phase 15 success-field allowlist only (extra keys forbidden)."""

    model_config = ConfigDict(extra="forbid")

    response: str
    model: str
    diffs: list[str]
    files_changed: list[str]
    validation: list[dict[str, Any]]
    context_used: dict[str, Any]


class SnapshotApplyBody(BaseModel):
    """Phase 17: optional dry_run only; patches come from stored snapshot diffs_json."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False



class ValidationRunBody(BaseModel):
    """Phase 20: optional trigger_source; backward-compatible default matches historical manual runs."""

    model_config = ConfigDict(extra="forbid")

    trigger_source: Literal["manual", "post_apply"] = "manual"


@router.get("/tasks/{task_id}/coding/summary", response_class=JSONResponse)
def api_coding_summary(task_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    summary = get_coding_summary_dict(db, t, user_id=identity["user_id"])
    return {"ok": True, "summary": summary}


@router.get("/tasks/{task_id}/coding/handoff", response_class=JSONResponse)
def api_coding_handoff(task_id: int, request: Request, db: Session = Depends(get_db)):
    """Phase 5: JSON-only read-only handoff projection (no commits, no validator)."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    handoff = build_handoff_dict(db, t, user_id=identity["user_id"])
    return {"ok": True, "handoff": handoff}


def _coerce_validation_runs_limit(raw: str | None) -> int | None:
    """Invalid or empty -> None (service applies default 15). Avoids 422 for bad query strings."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip(), 10)
    except ValueError:
        return None


@router.get("/tasks/{task_id}/coding/validation/runs", response_class=JSONResponse)
def api_list_validation_runs(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    limit: str | None = Query(default=None),
):
    """Phase 8: metadata-only run list; id DESC; error_message truncated on this route only."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    runs = list_validation_runs_metadata_dict(db, task_id, _coerce_validation_runs_limit(limit))
    return {"ok": True, "runs": runs}


@router.post("/tasks/{task_id}/coding/clarifications", response_class=JSONResponse)
def api_add_clarification(
    task_id: int,
    body: ClarificationCreateBody,
    request: Request,
    db: Session = Depends(get_db),
):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_editable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    row = po_v2.add_clarification(db, t, body.question, identity["user_id"])
    db.commit()
    db.refresh(row)
    db.refresh(t)
    return {"ok": True, "clarification": _clar_row_dict(row)}


@router.patch("/tasks/{task_id}/coding/clarifications/{clarification_id}", response_class=JSONResponse)
def api_answer_clarification(
    task_id: int,
    clarification_id: int,
    body: ClarificationAnswerBody,
    request: Request,
    db: Session = Depends(get_db),
):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_editable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    row = po_v2.answer_clarification(db, t, clarification_id, body.answer)
    if not row:
        return JSONResponse({"error": "Not found"}, status_code=404)
    db.commit()
    db.refresh(row)
    db.refresh(t)
    return {"ok": True, "clarification": _clar_row_dict(row)}


@router.put("/tasks/{task_id}/coding/brief", response_class=JSONResponse)
def api_put_brief(
    task_id: int,
    body: BriefBody,
    request: Request,
    db: Session = Depends(get_db),
):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_editable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    br = po_v2.upsert_brief(db, t, body.body, identity["user_id"])
    db.commit()
    db.refresh(br)
    db.refresh(t)
    return {
        "ok": True,
        "brief": {
            "id": br.id,
            "body": br.body,
            "version": br.version,
            "created_at": br.created_at.isoformat() if br.created_at else None,
        },
    }


@router.post("/tasks/{task_id}/coding/readiness/reopen-from-blocked", response_class=JSONResponse)
def api_reopen_coding_from_blocked(task_id: int, request: Request, db: Session = Depends(get_db)):
    """Phase 6: minimal reopen from blocked only; no validator, handoff, or summary sync."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_editable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        po_v2.reopen_from_blocked_for_edit(db, t)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    db.commit()
    db.refresh(t)
    return {"ok": True, "coding_readiness_state": t.coding_readiness_state}


@router.post("/tasks/{task_id}/coding/brief/approve", response_class=JSONResponse)
def api_approve_brief(task_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_editable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        po_v2.approve_brief(db, t)
    except ValueError as e:
        return JSONResponse(_preflight_error_payload(db, t, str(e)), status_code=400)
    db.commit()
    db.refresh(t)
    return {"ok": True, "coding_readiness_state": t.coding_readiness_state}


@router.put("/tasks/{task_id}/coding/profile", response_class=JSONResponse)
def api_update_profile(
    task_id: int,
    body: ProfileUpdateBody,
    request: Request,
    db: Session = Depends(get_db),
):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_editable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        prof = update_coding_profile(
            db,
            t,
            code_repo_id=body.code_repo_id,
            repo_name=body.repo_name,
            repo_index=body.repo_index,
            sub_path=body.sub_path,
            user_id=identity["user_id"],
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "profile": prof}


@router.post("/tasks/{task_id}/coding/validation/run", response_class=JSONResponse)
def api_run_validation(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    body: ValidationRunBody = Body(default_factory=ValidationRunBody),
):
    """Phase 1 validation run; Phase 20: optional JSON body sets trigger_source (default manual)."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_editable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        assert_transition_allowed(db, t, "validate", user_id=identity["user_id"])
        with _coding_timed("validate", task_id=task_id, user_id=identity["user_id"]):
            result = run_validation_for_task(
                db,
                t,
                user_id=identity["user_id"],
                trigger_source=body.trigger_source,
            )
    except WorkflowTransitionBlocked as e:
        _log_coding_event("validate", "blocked", task_id=task_id, user_id=identity["user_id"], current_state=e.current, required_state=e.required)
        return JSONResponse(_workflow_blocked_payload(e), status_code=409)
    except WorkspaceUnbound as e:
        _log_coding_event("validate", "blocked", task_id=task_id, user_id=identity["user_id"], reason="workspace_unbound")
        payload = _preflight_error_payload(db, t, str(e))
        payload["workspace_unbound"] = True
        payload["workspace_reason"] = e.reason
        return JSONResponse(payload, status_code=409)
    except ValueError as e:
        _log_coding_event("validate", "failed", task_id=task_id, user_id=identity["user_id"], reason=str(e)[:200])
        return JSONResponse(_preflight_error_payload(db, t, str(e)), status_code=400)
    return {"ok": True, **result}


@router.get("/tasks/{task_id}/coding/blockers", response_class=JSONResponse)
def api_list_blockers(task_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True, "blockers": list_blockers_dict(db, task_id)}


@router.get("/tasks/{task_id}/coding/validation/runs/{run_id}", response_class=JSONResponse)
def api_get_run(task_id: int, run_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    detail = get_run_detail_dict(db, task_id, run_id)
    if not detail:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True, "run": detail}


@router.post("/tasks/{task_id}/coding/agent-suggest", response_class=JSONResponse)
async def api_agent_suggest(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    body: AgentSuggestBody | None = Body(default=None),
):
    """Task-first bridge: bounded prompt from coding substrate → Code Agent (reviewable result only)."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    extra = body.extra_instructions if body else None
    try:
        assert_transition_allowed(db, t, "suggest", user_id=identity["user_id"])
    except WorkflowTransitionBlocked as e:
        _log_coding_event("suggest", "blocked", task_id=task_id, user_id=identity["user_id"], current_state=e.current, required_state=e.required)
        return JSONResponse(_workflow_blocked_payload(e), status_code=409)
    import time as _t
    _start = _t.monotonic()
    result = await run_agent_suggest_for_task(db, t, identity["user_id"], extra_instructions=extra)
    _duration_ms = int((_t.monotonic() - _start) * 1000)
    if result.get("error"):
        outcome = "blocked" if result.get("workspace_unbound") else "failed"
        _log_coding_event("suggest", outcome, task_id=task_id, user_id=identity["user_id"], duration_ms=_duration_ms, reason=result.get("workspace_reason") or result["error"][:200])
        status = 409 if result.get("workspace_unbound") else 400
        body_out = {"ok": False, "message": result["error"]}
        if result.get("workspace_unbound"):
            body_out["workspace_unbound"] = True
            body_out["workspace_reason"] = result.get("workspace_reason") or result["error"]
        return JSONResponse(body_out, status_code=status)
    _log_coding_event("suggest", "ok", task_id=task_id, user_id=identity["user_id"], duration_ms=_duration_ms)
    return JSONResponse({"ok": True, **result})


@router.post("/tasks/{task_id}/coding/agent-suggestions", response_class=JSONResponse)
def api_save_agent_suggestion(
    task_id: int,
    body: AgentSuggestionSaveBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Phase 16: explicit save of bounded Phase 15 agent-suggest success payload (append-only)."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    cols, err = bound_payload_for_save(body.model_dump())
    if err:
        return JSONResponse({"ok": False, "message": err}, status_code=400)
    assert cols is not None
    sid = insert_suggestion(db, task_id, identity["user_id"], cols)
    db.commit()
    return JSONResponse({"ok": True, "id": sid}, status_code=201)


@router.get("/tasks/{task_id}/coding/agent-suggestions", response_class=JSONResponse)
def api_list_agent_suggestions(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    limit: str | None = Query(default=None),
):
    """Phase 16: metadata-only list (newest first)."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    lim = coerce_list_limit(limit, 20)
    items = list_suggestion_metadata(db, task_id, lim)
    return {"ok": True, "suggestions": items}


@router.get("/tasks/{task_id}/coding/agent-suggestions/{suggestion_id}", response_class=JSONResponse)
def api_get_agent_suggestion(
    task_id: int,
    suggestion_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Phase 16: bounded stored snapshot (faithful decode of persisted columns)."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    detail = get_suggestion_detail_dict(db, task_id, suggestion_id)
    if not detail:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True, "suggestion": detail}


@router.post(
    "/tasks/{task_id}/coding/agent-suggestions/{suggestion_id}/apply",
    response_class=JSONResponse,
)
def api_apply_agent_suggestion(
    task_id: int,
    suggestion_id: int,
    request: Request,
    db: Session = Depends(get_db),
    body: SnapshotApplyBody | None = Body(default=None),
):
    """Phase 17: all-or-nothing git apply of stored diffs at repo root; append-only audit."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_editable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    dry = body.dry_run if body else False
    step_name = "dry_run" if dry else "apply"
    try:
        # Dry-run is always allowed once a snapshot exists; real apply requires
        # a green dry-run first, so the workflow state machine enforces that gate.
        assert_transition_allowed(db, t, step_name, user_id=identity["user_id"])
    except WorkflowTransitionBlocked as e:
        _log_coding_event(step_name, "blocked", task_id=task_id, user_id=identity["user_id"], suggestion_id=suggestion_id, current_state=e.current, required_state=e.required)
        return JSONResponse(_workflow_blocked_payload(e), status_code=409)
    import time as _t
    _start = _t.monotonic()
    payload, status = apply_stored_snapshot_diffs(
        db, t, identity["user_id"], suggestion_id, dry_run=dry
    )
    _duration_ms = int((_t.monotonic() - _start) * 1000)
    if status == 200:
        outcome = "ok"
    elif payload.get("workspace_unbound"):
        outcome = "blocked"
    else:
        outcome = "failed"
    _log_coding_event(
        step_name,
        outcome,
        task_id=task_id,
        user_id=identity["user_id"],
        suggestion_id=suggestion_id,
        duration_ms=_duration_ms,
        http_status=status,
    )
    return JSONResponse(payload, status_code=status)


@router.get(
    "/tasks/{task_id}/coding/agent-suggestions/{suggestion_id}/apply-attempts",
    response_class=JSONResponse,
)
def api_list_agent_suggestion_apply_attempts(
    task_id: int,
    suggestion_id: int,
    request: Request,
    db: Session = Depends(get_db),
    limit: str | None = Query(default=None),
):
    """Phase 18: metadata-only apply audit list; readable access; message_preview truncated here only."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = _get_task_readable(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    items = list_apply_attempts_metadata_dict(
        db, task_id, suggestion_id, _coerce_validation_runs_limit(limit)
    )
    if items is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True, "apply_attempts": items}
