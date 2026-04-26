"""Operator status and controls for the code dispatch loop."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..models import PlanProject
from ..services.planner_service import _user_can_edit

router = APIRouter(prefix="/api/brain/dispatch", tags=["dispatch"])

_DISPATCH_REPO_NAME = "chili-home-copilot"


def _guest_guard(ctx: dict) -> bool:
    return bool(ctx.get("is_guest", True) or ctx.get("user_id") is None)


def _kill_switch_from_db(db: Session) -> dict:
    row = db.execute(
        text("SELECT active, reason FROM code_kill_switch_state WHERE id = 1")
    ).fetchone()
    if row is None:
        return {"active": False, "reason": None}
    return {"active": bool(row[0]), "reason": row[1]}


def _get_code_kill_switch_status(db: Session) -> dict:
    try:
        from ..services.code_dispatch.governance import (  # type: ignore[import-not-found]
            get_code_kill_switch_status,
        )

        return get_code_kill_switch_status()
    except ImportError:
        return _kill_switch_from_db(db)


def _jsonify_snapshot(val) -> dict | list | None:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, (bytes, memoryview)):
        try:
            return json.loads(bytes(val).decode("utf-8"))
        except Exception:
            return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    return None


class QueueTaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=8000)
    project_id: int
    intended_files: list[str] = Field(default_factory=list)
    force_tier: int | None = None
    # Phase E.2 — optional dynamic source. The resolver accepts:
    #   * Local Windows path (C:\dev\foo) — must be under C:\dev
    #   * Container path (/workspace, /host_dev/foo)
    #   * GitHub HTTPS URL
    #   * GitHub SSH URL (translated to HTTPS for clone)
    #   * USER/REPO shorthand
    #   * bare repo name (must already exist in code_repos)
    # If omitted, the task binds to the legacy default repo (chili-home-copilot).
    source_input: str | None = Field(default=None, max_length=2048)


class KillSwitchBody(BaseModel):
    active: bool
    reason: str | None = None


@router.get("/status")
def dispatch_status(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    if _guest_guard(ctx):
        return JSONResponse(content={"error": "unauthorized"}, status_code=403)

    rows = db.execute(
        text(
            "SELECT COALESCE(decision,'unknown') AS decision, COUNT(*) "
            "FROM code_agent_runs "
            "WHERE started_at > NOW() - INTERVAL '5 minutes' "
            "GROUP BY 1 ORDER BY 1"
        )
    ).fetchall()
    counters_5min = {str(r[0]): int(r[1]) for r in rows}

    spend = db.execute(
        text(
            "SELECT provider, COUNT(*) AS calls, "
            "       COALESCE(SUM(cost_usd),0) AS spend_usd, "
            "       COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) AS tokens "
            "FROM llm_call_log "
            "WHERE created_at > NOW() - INTERVAL '24 hours' "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
    ).fetchall()
    spend_24h = [
        {
            "provider": r[0],
            "calls": int(r[1]),
            "spend_usd": float(r[2] or 0.0),
            "tokens": int(r[3] or 0),
        }
        for r in spend
    ]

    spend_day = db.execute(
        text(
            "SELECT provider, COUNT(*) AS calls, "
            "       COALESCE(SUM(cost_usd),0) AS spend_usd, "
            "       COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) AS tokens "
            "FROM llm_call_log "
            "WHERE date_trunc('day', created_at) = date_trunc('day', NOW()) "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
    ).fetchall()
    spend_today = [
        {
            "provider": r[0],
            "calls": int(r[1]),
            "spend_usd": float(r[2] or 0.0),
            "tokens": int(r[3] or 0),
        }
        for r in spend_day
    ]

    act_row = db.execute(
        text("SELECT MAX(started_at) FROM code_agent_runs")
    ).scalar()
    last_dispatch_activity_at = act_row.isoformat() if act_row is not None else None

    recent = db.execute(
        text(
            "SELECT id, started_at, cycle_step, decision, task_id, "
            "       escalation_reason "
            "FROM code_agent_runs "
            "ORDER BY id DESC LIMIT 10"
        )
    ).fetchall()
    recent_rows = [
        {
            "id": int(r[0]),
            "started_at": r[1].isoformat() if r[1] is not None else None,
            "cycle_step": r[2],
            "decision": r[3],
            "task_id": int(r[4]) if r[4] is not None else None,
            "escalation_reason": r[5],
        }
        for r in recent
    ]

    return {
        "kill_switch": _get_code_kill_switch_status(db),
        "counters_5min": counters_5min,
        "spend_24h": spend_24h,
        "spend_today": spend_today,
        "last_dispatch_activity_at": last_dispatch_activity_at,
        "recent_runs": recent_rows,
    }


@router.get("/runs")
def dispatch_runs(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=200),
    task_id: int | None = Query(None),
):
    ctx = get_identity_ctx(request, db)
    if _guest_guard(ctx):
        return JSONResponse(content={"error": "unauthorized"}, status_code=403)

    lim = max(1, min(limit, 200))
    # Phase F enhancement — also surface branch/commit/push so the
    # History UI can show a clickable GitHub link, AND notify_user so
    # we can highlight rows needing operator review.
    select_cols = (
        "id, started_at, finished_at, task_id, cycle_step, decision, "
        "escalation_reason, llm_snapshot, validation_run_id, "
        "branch_name, commit_sha, diff_summary, notify_user"
    )
    if task_id is not None:
        rows = db.execute(
            text(
                f"SELECT {select_cols} FROM code_agent_runs "
                "WHERE task_id = :tid "
                "ORDER BY id DESC LIMIT :lim"
            ),
            {"tid": task_id, "lim": lim},
        ).fetchall()
    else:
        rows = db.execute(
            text(
                f"SELECT {select_cols} FROM code_agent_runs "
                "ORDER BY id DESC LIMIT :lim"
            ),
            {"lim": lim},
        ).fetchall()

    out = []
    for r in rows:
        snap = _jsonify_snapshot(r[7])
        diff_summary = _jsonify_snapshot(r[11]) if r[11] is not None else None
        out.append(
            {
                "id": int(r[0]),
                "started_at": r[1].isoformat() if r[1] is not None else None,
                "finished_at": r[2].isoformat() if r[2] is not None else None,
                "task_id": int(r[3]) if r[3] is not None else None,
                "cycle_step": r[4],
                "decision": r[5],
                "escalation_reason": r[6],
                "llm_snapshot": snap,
                "validation_run_id": int(r[8]) if r[8] is not None else None,
                "branch_name": r[9],
                "commit_sha": r[10],
                "diff_summary": diff_summary,
                "notify_user": bool(r[12]) if r[12] is not None else False,
            }
        )
    return {"runs": out}


@router.get("/projects")
def dispatch_list_projects(request: Request, db: Session = Depends(get_db)):
    """Planner-compatible project list for Bearer-authenticated clients."""
    ctx = get_identity_ctx(request, db)
    if _guest_guard(ctx):
        return JSONResponse(content={"error": "unauthorized"}, status_code=403)
    from ..services import planner_service

    return {"projects": planner_service.list_projects(db, ctx["user_id"])}


@router.post("/queue")
def queue_task(
    request: Request,
    payload: QueueTaskBody,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    if _guest_guard(ctx):
        return JSONResponse(content={"error": "unauthorized"}, status_code=403)

    uid = ctx["user_id"]
    proj = db.query(PlanProject).filter(PlanProject.id == payload.project_id).first()
    if not proj:
        return JSONResponse(content={"error": "not_found"}, status_code=404)
    if not _user_can_edit(db, payload.project_id, uid):
        return JSONResponse(content={"error": "forbidden"}, status_code=403)

    # Phase E.2 — when the operator provides source_input, resolve it
    # dynamically (local path / GitHub URL / shorthand / name lookup) and
    # bind THIS task to whatever repo we end up with. Fall back to the
    # legacy single-repo path otherwise.
    repo_id: int | None = None
    resolver_notes: list[str] = []
    if (payload.source_input or "").strip():
        try:
            from ..services.code_brain import repo_resolver  # type: ignore
            result = repo_resolver.resolve_or_register(db, payload.source_input)
            repo_id = int(result.repo.id)
            resolver_notes = list(result.notes)
        except ValueError as e:
            return JSONResponse(
                content={"error": "source_input_invalid", "detail": str(e)},
                status_code=400,
            )
        except RuntimeError as e:
            return JSONResponse(
                content={"error": "source_input_failed", "detail": str(e)},
                status_code=500,
            )

    if repo_id is None:
        repo_row = db.execute(
            text("SELECT id FROM code_repos WHERE name = :n AND active IS TRUE LIMIT 1"),
            {"n": _DISPATCH_REPO_NAME},
        ).fetchone()
        if not repo_row:
            repo_row = db.execute(
                text("SELECT id FROM code_repos WHERE name = :n LIMIT 1"),
                {"n": _DISPATCH_REPO_NAME},
            ).fetchone()
        if not repo_row:
            return JSONResponse(
                content={"error": "code_repo_missing", "error_kind": "code_repo_missing"},
                status_code=500,
            )
        repo_id = int(repo_row[0])

    now = datetime.now(timezone.utc)
    desc = payload.description
    if payload.intended_files:
        desc = f"{desc}\n\n[intended_files] {json.dumps(payload.intended_files)}"
    if payload.force_tier is not None:
        desc = f"{desc}\n\n[force_tier] {payload.force_tier}"

    ins = db.execute(
        text(
            "INSERT INTO plan_tasks ("
            " project_id, title, description, status, priority, sort_order, "
            " coding_readiness_state, reporter_id, created_at, updated_at, "
            " coding_workflow_mode, coding_workflow_state, coding_workflow_state_updated_at, "
            " progress"
            ") VALUES ("
            " :project_id, :title, :description, 'todo', 'high', -1, "
            " 'ready_for_dispatch', :reporter_id, :created_at, :updated_at, "
            " 'tracked', 'unbound', :created_at, 0"
            ") RETURNING id"
        ),
        {
            "project_id": payload.project_id,
            "title": payload.title,
            "description": desc,
            "reporter_id": uid,
            "created_at": now,
            "updated_at": now,
        },
    ).fetchone()
    if not ins:
        return JSONResponse(
            content={"error": "insert_failed", "error_kind": "insert_failed"},
            status_code=500,
        )
    tid = int(ins[0])

    db.execute(
        text(
            "INSERT INTO plan_task_coding_profile (task_id, repo_index, sub_path, code_repo_id, updated_at) "
            "VALUES (:task_id, 0, '', :repo_id, :updated_at)"
        ),
        {"task_id": tid, "repo_id": repo_id, "updated_at": now},
    )
    db.commit()

    return {
        "task_id": tid,
        "queued_at": now.isoformat(),
        "code_repo_id": repo_id,
        "resolver_notes": resolver_notes,
    }


@router.post("/kill-switch")
def kill_switch_toggle(
    request: Request,
    body: KillSwitchBody,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    if _guest_guard(ctx):
        return JSONResponse(content={"error": "unauthorized"}, status_code=403)

    via = "helper"
    try:
        from ..services.code_dispatch.governance import (  # type: ignore[import-not-found]
            activate_code_kill_switch,
            deactivate_code_kill_switch,
            get_code_kill_switch_status,
        )

        if body.active:
            activate_code_kill_switch(body.reason or "brain_ui")
        else:
            deactivate_code_kill_switch()
        state = get_code_kill_switch_status()
        return {
            "active": bool(state.get("active")),
            "reason": state.get("reason"),
            "via": via,
        }
    except ImportError:
        via = "sql"
        try:
            reason = body.reason if body.active else None
            db.execute(
                text(
                    "INSERT INTO code_kill_switch_state "
                    "(id, active, reason, activated_at, activated_by) "
                    "VALUES (1, :active, :reason, NOW(), 'brain_ui') "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "active = EXCLUDED.active, reason = EXCLUDED.reason, "
                    "activated_at = EXCLUDED.activated_at, activated_by = EXCLUDED.activated_by"
                ),
                {"active": body.active, "reason": reason},
            )
            db.commit()
            state = _kill_switch_from_db(db)
            return {
                "active": bool(state.get("active")),
                "reason": state.get("reason"),
                "via": via,
            }
        except Exception as exc:
            db.rollback()
            return JSONResponse(
                status_code=500,
                content={"error": str(exc), "error_kind": "kill_switch_update_failed"},
            )
