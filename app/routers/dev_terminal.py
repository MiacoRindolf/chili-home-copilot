"""Dev Terminal: minimal command interface for autonomous code development.

Single dark terminal UI — type what you want, watch it execute autonomously,
accept/reject the result.  No panes, no dashboards.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from queue import Queue, Empty

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from ..deps import get_db, get_identity_ctx
from ..models.code_brain import CodeRepo
from ..models.coding_task import CodingExecutionIteration
from ..routers.chat_streaming import sse_event, sse_done, sse_error

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dev_terminal"])


# ── Page ─────────────────────────────────────────────────────────


@router.get("/dev", response_class=HTMLResponse)
def dev_terminal_page(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse("dev_terminal.html", {
        "request": request,
        "nav_modules": getattr(request.app.state, "nav_modules", []),
    })


# ── API: status / context ────────────────────────────────────────


@router.get("/api/dev/status")
def api_dev_status(request: Request, db: Session = Depends(get_db)):
    """Quick status: active repos, recent runs, system readiness."""
    ctx = get_identity_ctx(request, db)
    user_id = ctx.get("user_id")

    repos_q = db.query(CodeRepo).filter(CodeRepo.active.is_(True))
    if user_id is not None:
        repos_q = repos_q.filter((CodeRepo.user_id == user_id) | (CodeRepo.user_id.is_(None)))
    repos = repos_q.all()

    recent = (
        db.query(CodingExecutionIteration)
        .order_by(CodingExecutionIteration.id.desc())
        .limit(10)
        .all()
    )
    # Group by run_id, show latest state per run
    runs: dict[str, dict] = {}
    for row in recent:
        if row.run_id not in runs:
            runs[row.run_id] = {
                "run_id": row.run_id,
                "state": row.state,
                "iteration": row.iteration,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "test_exit_code": row.test_exit_code,
                "error_category": row.error_category,
            }

    from ..openai_client import is_configured as llm_ready
    return JSONResponse({
        "ok": True,
        "repos": [{"id": r.id, "name": r.name, "path": r.path} for r in repos],
        "recent_runs": list(runs.values())[:5],
        "llm_configured": llm_ready(),
    })


@router.get("/api/dev/context")
def api_dev_context(request: Request, db: Session = Depends(get_db)):
    """Show what the system knows about the active repo."""
    ctx = get_identity_ctx(request, db)
    user_id = ctx.get("user_id")

    repo = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).first()
    if not repo:
        return JSONResponse({"ok": False, "error": "No active repo"})

    from ..services.code_brain import insights as ins_mod
    insights = ins_mod.get_insights(db, repo_id=repo.id)

    lang_stats = json.loads(repo.language_stats) if repo.language_stats else {}

    return JSONResponse({
        "ok": True,
        "repo": {
            "id": repo.id,
            "name": repo.name,
            "path": repo.path,
            "file_count": repo.file_count,
            "total_lines": repo.total_lines,
            "languages": lang_stats,
            "last_indexed": repo.last_indexed.isoformat() if repo.last_indexed else None,
        },
        "insights_count": len(insights),
        "insights_sample": insights[:10],
    })


# ── API: autonomous execution with SSE ───────────────────────────


class DevRunRequest(BaseModel):
    prompt: str
    repo_id: int | None = None


@router.post("/api/dev/run")
def api_dev_run(body: DevRunRequest, request: Request, db: Session = Depends(get_db)):
    """Start an autonomous execution loop, streaming progress via SSE."""
    ctx = get_identity_ctx(request, db)
    if ctx["is_guest"]:
        return JSONResponse({"ok": False, "error": "Guests cannot run autonomous tasks"}, status_code=403)

    user_id = ctx.get("user_id")

    # Validate repo access
    repo_id = body.repo_id
    if repo_id is None:
        repo = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).first()
        if not repo:
            return JSONResponse({"ok": False, "error": "No active repo registered"}, status_code=400)
        repo_id = repo.id

    # Use a queue to bridge the background thread and SSE generator
    event_queue: Queue = Queue()

    def on_progress(event: str, data: dict):
        event_queue.put({"event": event, **data})

    def _run_in_thread():
        from ..db import SessionLocal
        thread_db = SessionLocal()
        try:
            from ..services.coding_task.service import run_autonomous_task
            result = run_autonomous_task(
                thread_db,
                body.prompt,
                repo_id=repo_id,
                user_id=user_id,
                on_progress=on_progress,
            )
            event_queue.put({"event": "result", **result})
        except Exception as e:
            logger.exception("[dev_terminal] execution failed")
            event_queue.put({"event": "error", "message": str(e)[:500]})
        finally:
            event_queue.put(None)  # sentinel
            thread_db.close()

    thread = threading.Thread(target=_run_in_thread, daemon=True)
    thread.start()

    def generate():
        while True:
            try:
                item = event_queue.get(timeout=60)
            except Empty:
                yield sse_event({"event": "heartbeat"})
                continue
            if item is None:
                yield sse_done()
                break
            yield sse_event(item)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── API: review a run ────────────────────────────────────────────


@router.get("/api/dev/run/{run_id}")
def api_dev_run_detail(run_id: str, db: Session = Depends(get_db)):
    """Get full detail of a completed execution run."""
    rows = (
        db.query(CodingExecutionIteration)
        .filter(CodingExecutionIteration.run_id == run_id)
        .order_by(CodingExecutionIteration.iteration.asc())
        .all()
    )
    if not rows:
        return JSONResponse({"ok": False, "error": "Run not found"}, status_code=404)

    iterations = []
    for r in rows:
        iterations.append({
            "iteration": r.iteration,
            "state": r.state,
            "plan_json": json.loads(r.plan_json) if r.plan_json else None,
            "diffs": json.loads(r.diffs_json) if r.diffs_json else [],
            "files_changed": json.loads(r.files_changed_json) if r.files_changed_json else [],
            "apply_status": r.apply_status,
            "test_exit_code": r.test_exit_code,
            "test_output": r.test_output,
            "diagnosis": r.diagnosis,
            "error_category": r.error_category,
            "model_used": r.model_used,
            "duration_ms": r.duration_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    last = rows[-1]
    return JSONResponse({
        "ok": True,
        "run_id": run_id,
        "status": last.state,
        "iterations": iterations,
    })


# ── API: accept/reject a run ─────────────────────────────────────


class DevAcceptRequest(BaseModel):
    run_id: str
    action: str  # "accept" | "reject"


@router.post("/api/dev/accept")
def api_dev_accept(body: DevAcceptRequest, request: Request, db: Session = Depends(get_db)):
    """Accept (merge branch to original) or reject (delete branch) a run."""
    ctx = get_identity_ctx(request, db)
    if ctx["is_guest"]:
        return JSONResponse({"ok": False, "error": "Guests cannot accept/reject"}, status_code=403)

    # Find the repo from the run's iteration data
    rows = (
        db.query(CodingExecutionIteration)
        .filter(CodingExecutionIteration.run_id == body.run_id)
        .order_by(CodingExecutionIteration.iteration.asc(), CodingExecutionIteration.id.asc())
        .all()
    )
    if not rows:
        return JSONResponse({"ok": False, "error": "Run not found"}, status_code=404)

    execution_metadata = {}
    for row in rows:
        try:
            plan = json.loads(row.plan_json) if row.plan_json else {}
        except (TypeError, ValueError):
            plan = {}
        if isinstance(plan, dict) and isinstance(plan.get("_execution"), dict):
            execution_metadata = plan["_execution"]
            break
    try:
        recorded_repo_id = int(execution_metadata.get("repo_id"))
    except (TypeError, ValueError):
        return JSONResponse(
            {
                "ok": False,
                "error": "This legacy run has no repo-bound execution lineage and cannot be merged safely. Re-run it in Project Autopilot.",
            },
            status_code=409,
        )
    repo_query = db.query(CodeRepo).filter(CodeRepo.id == recorded_repo_id, CodeRepo.active.is_(True))
    if ctx.get("user_id") is not None:
        repo_query = repo_query.filter(
            (CodeRepo.user_id == ctx["user_id"]) | (CodeRepo.user_id.is_(None))
        )
    repo = repo_query.first()
    if not repo:
        return JSONResponse({"ok": False, "error": "Recorded execution repo is unavailable"}, status_code=400)

    from ..services.coding_task.execution_loop import acceptance_preflight, _run_git, _get_current_branch
    from pathlib import Path

    cwd = Path(repo.path).resolve()
    branch = str(execution_metadata.get("branch") or "")
    current = _get_current_branch(cwd)
    files_changed = sorted(
        dict.fromkeys(
            str(path)
            for row in rows
            for path in (json.loads(row.files_changed_json) if row.files_changed_json else [])
            if str(path).strip()
        )
    )
    latest = rows[-1]

    if body.action == "accept":
        preflight = acceptance_preflight(
            cwd,
            run_id=body.run_id,
            execution_metadata=execution_metadata,
            files_changed=files_changed,
            final_state=str(latest.state or ""),
            test_exit_code=latest.test_exit_code,
        )
        if not preflight["ok"]:
            return JSONResponse(
                {"ok": False, "error": preflight["reason"], "preflight": preflight},
                status_code=409,
            )
        code, out = _run_git(cwd, ["merge", "--ff-only", branch])
        if code != 0:
            return JSONResponse({"ok": False, "error": f"Merge failed: {out}"}, status_code=400)
        _run_git(cwd, ["branch", "-d", branch])
        return JSONResponse({"ok": True, "message": "Changes merged successfully"})

    elif body.action == "reject":
        if current == branch:
            return JSONResponse(
                {"ok": False, "error": "Refusing to delete the branch currently checked out by the operator."},
                status_code=409,
            )
        code, out = _run_git(cwd, ["branch", "-D", branch])
        if code != 0:
            return JSONResponse({"ok": False, "error": f"Reject failed: {out}"}, status_code=400)
        return JSONResponse({"ok": True, "message": "Changes rejected and branch deleted"})

    return JSONResponse({"ok": False, "error": f"Unknown action: {body.action}"}, status_code=400)
