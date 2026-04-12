"""Chili Brain: cross-domain intelligence hub.

Exposes status, metrics, and control endpoints for the Brain.
Domains: Trading (active), Code (active).
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..schemas.trading import TradingBrainAssistantChatResponse
from ..services import project_domain_service
from ..services import trading_service as ts
from ..services.code_brain import learning as cb_learning
from ..services.code_brain import lenses as cb_lenses
from ..services.reasoning_brain import learning as rb_learning
from ..services.project_brain import registry as pb_registry
from ..services.reasoning_brain import proactive_chat as rb_chat
from ..services.trading.brain_neural_mesh.schema import desk_graph_boot_config
from ..models import (
    BrainBatchJob,
    ReasoningAnticipation,
    ReasoningConfidenceSnapshot,
    ReasoningEvent,
    ReasoningHypothesis,
    ReasoningInterest,
    ReasoningLearningGoal,
    ReasoningResearch,
    ReasoningUserModel,
)
from ..services.trading.batch_job_constants import JOB_SCHEDULER_WORKER_HEARTBEAT

logger = logging.getLogger(__name__)

router = APIRouter(tags=["brain"])

_ALLOWED_BRAIN_DOMAINS = frozenset({"hub", "trading", "project", "reasoning"})


def _normalize_brain_domain_query(request: Request) -> str:
    """Map ?domain= to hub | trading | project | reasoning. Unknown → hub."""
    raw = (request.query_params.get("domain") or "").strip().lower()
    if raw == "code":
        return "project"
    if raw == "jobs":
        return "jobs"  # caller redirects
    if raw in _ALLOWED_BRAIN_DOMAINS:
        return raw
    if raw == "":
        return ""
    return "__invalid__"


def _brain_initial_domain_for_request(
    request: Request,
    planner_task_id: int | None,
    planner_project_id: int | None,
) -> str:
    """URL `domain` wins when set; planner params select project only when domain is omitted."""
    norm = _normalize_brain_domain_query(request)
    if norm == "jobs":
        return "jobs"
    if norm in ("trading", "project", "reasoning", "hub"):
        return norm
    if norm == "__invalid__":
        return "hub"
    # No domain param (or empty): planner handoff deep links default to project desk
    if planner_task_id is not None or planner_project_id is not None:
        return "project"
    return "hub"


@router.get("/api/v1/brain/users")
def legacy_api_v1_brain_users():
    """Empty list for embedded/legacy clients that probe this path (avoids 404 noise in console)."""
    return JSONResponse([])


# ── Page ────────────────────────────────────────────────────────────────

@router.get("/brain", response_class=HTMLResponse)
def brain_page(
    request: Request,
    db: Session = Depends(get_db),
    planner_task_id: int | None = Query(default=None, ge=1),
    planner_project_id: int | None = Query(default=None, ge=1),
):
    brain_initial_domain = _brain_initial_domain_for_request(
        request, planner_task_id, planner_project_id
    )
    if brain_initial_domain == "jobs":
        return RedirectResponse(url="/app/jobs", status_code=302)

    ctx = get_identity_ctx(request, db)
    desk = desk_graph_boot_config()
    neural_first_paint = bool(desk.get("mesh_enabled") and desk.get("effective_graph_mode") == "neural")
    resp = request.app.state.templates.TemplateResponse(
        request, "brain.html",
        {
            "title": "Chili Brain",
            "is_guest": ctx["is_guest"],
            "user_name": ctx["user_name"],
            "planner_task_id": planner_task_id,
            "planner_project_id": planner_project_id,
            "brain_initial_domain": brain_initial_domain,
            "trading_brain_desk_config": desk,
            "trading_brain_neural_first_paint": neural_first_paint,
        },
    )
    # Large inline script in template — avoid stale UI after deploy (Pine export, etc.).
    resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
    return resp


# ── Cross-domain status ────────────────────────────────────────────────

def _scheduler_worker_jobs_status(db: Session) -> dict:
    """Recent scheduler-worker heartbeat from brain_batch_jobs (ok rows)."""
    st = {"running": False, "last_run": None, "phase": "idle"}
    try:
        row = (
            db.query(BrainBatchJob)
            .filter(
                BrainBatchJob.job_type == JOB_SCHEDULER_WORKER_HEARTBEAT,
                BrainBatchJob.status == "ok",
            )
            .order_by(BrainBatchJob.ended_at.desc())
            .first()
        )
        if row and row.ended_at:
            st["last_run"] = row.ended_at.isoformat()
            age_sec = (datetime.utcnow() - row.ended_at).total_seconds()
            st["running"] = age_sec < 15 * 60
            st["phase"] = "heartbeat" if st["running"] else "quiet"
    except Exception:
        pass
    return st


@router.get("/api/brain/domains")
def api_brain_domains(db: Session = Depends(get_db)):
    """List all Brain domains and their high-level status."""
    trading_st = ts.get_learning_status()
    code_st = cb_learning.get_code_learning_status()
    reasoning_st = rb_learning.get_reasoning_status()
    jobs_st = _scheduler_worker_jobs_status(db)
    return JSONResponse({
        "ok": True,
        "domains": [
            {
                "id": "trading",
                "label": "Trading",
                "icon": "\U0001f4c8",
                "description": "Patterns, backtests, learning cycles, and desk metrics for your watchlists.",
                "status": "learning" if trading_st.get("running") else "idle",
                "last_run": trading_st.get("last_run"),
                "phase": trading_st.get("phase", "idle"),
            },
            {
                "id": "project",
                "label": "Project",
                "icon": "\U0001f3d7",
                "description": "Code brain, autonomous agents, and planner implementation handoff in one surface.",
                "status": "learning" if code_st.get("running") else "idle",
                "last_run": code_st.get("last_run"),
                "phase": code_st.get("phase", "idle"),
                "lenses": [l["name"] for l in cb_lenses.list_lenses()],
                "agents": pb_registry.list_agents(),
            },
            {
                "id": "reasoning",
                "label": "Reasoning",
                "icon": "\U0001f9e0",
                "description": "User model, interests, research threads, and proactive insight chat.",
                "status": "learning" if reasoning_st.get("running") else "idle",
                "last_run": reasoning_st.get("last_run"),
                "phase": reasoning_st.get("phase", "idle"),
            },
            {
                "id": "jobs",
                "label": "Jobs",
                "icon": "\U0001f4cb",
                "description": "Scheduled batch runs, scan payloads, and scheduler-worker heartbeat.",
                "navigate_url": "/app/jobs",
                "status": "learning" if jobs_st.get("running") else "idle",
                "last_run": jobs_st.get("last_run"),
                "phase": jobs_st.get("phase", "idle"),
            },
        ],
    })


@router.get("/api/brain/status")
def api_brain_status(db: Session = Depends(get_db)):
    """Unified Brain health across all domains. Partial status on per-domain errors."""
    trading_st = {"running": False, "last_run": None, "phase": "idle"}
    code_st = {"running": False, "last_run": None, "phase": "idle"}
    reasoning_st = {"running": False, "last_run": None, "phase": "idle"}
    try:
        trading_st = ts.get_learning_status()
    except Exception:
        pass
    try:
        code_st = cb_learning.get_code_learning_status()
    except Exception:
        pass
    try:
        reasoning_st = rb_learning.get_reasoning_status()
    except Exception:
        pass
    jobs_st = _scheduler_worker_jobs_status(db)
    return JSONResponse({
        "ok": True,
        "trading": trading_st,
        "code": code_st,
        "reasoning": reasoning_st,
        "jobs": jobs_st,
    })


@router.get("/api/brain/project/bootstrap")
def api_brain_project_bootstrap(
    request: Request,
    db: Session = Depends(get_db),
    planner_task_id: int | None = Query(default=None, ge=1),
):
    """Workspace-first bootstrap payload for the Project domain."""
    ctx = get_identity_ctx(request, db)
    payload = project_domain_service.build_project_bootstrap_payload(
        db,
        user_id=ctx["user_id"],
        is_guest=bool(ctx["is_guest"]),
        planner_task_id=planner_task_id,
    )
    return JSONResponse({"ok": True, **payload})


# ── Trading domain: metrics ────────────────────────────────────────────

@router.get("/api/brain/trading/metrics")
def api_brain_trading_metrics(request: Request, db: Session = Depends(get_db)):
    """Aggregate trading brain metrics (KPIs, patterns, predictions)."""
    ctx = get_identity_ctx(request, db)
    stats = ts.get_brain_stats(db, ctx["user_id"])
    return JSONResponse({"ok": True, **stats})


@router.get("/api/brain/trading/network-graph")
def api_brain_trading_network_graph(db: Session = Depends(get_db)):
    """Neural mesh graph for Trading Brain Network (skill-tree UI)."""
    from ..services.trading.brain_neural_mesh.projection import build_neural_graph_projection

    return JSONResponse(build_neural_graph_projection(db))


@router.get("/api/brain/network-graph")
def api_brain_network_graph_compat(db: Session = Depends(get_db)):
    """Same payload as ``/api/brain/trading/network-graph`` for external SPAs (e.g. dev on :3000)."""
    from ..services.trading.brain_neural_mesh.projection import build_neural_graph_projection

    return JSONResponse(build_neural_graph_projection(db))


# ── Trading domain: controls ───────────────────────────────────────────

@router.post("/api/brain/trading/learn")
def api_brain_trading_learn(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Trigger a full trading learning cycle in the background."""
    ctx = get_identity_ctx(request, db)
    learning = ts.get_learning_status()
    if learning.get("running"):
        return JSONResponse({"ok": False, "message": "Learning cycle already in progress"})

    from ..db import SessionLocal

    def _bg(user_id):
        sdb = SessionLocal()
        try:
            ts.run_learning_cycle(sdb, user_id, full_universe=True)
        finally:
            sdb.close()

    background_tasks.add_task(_bg, ctx["user_id"])
    return JSONResponse({"ok": True, "message": "Learning cycle started"})


@router.post("/api/brain/trading/worker/wake-cycle")
def api_brain_trading_worker_wake_cycle(request: Request, db: Session = Depends(get_db)):
    """Skip brain worker idle sleep (delegates to trading worker API)."""
    from ..routers.trading_sub.ai import api_brain_worker_wake_cycle as _wake
    return _wake(request, db)


@router.post("/api/brain/trading/worker/stop")
def api_brain_trading_worker_stop(request: Request, db: Session = Depends(get_db)):
    """Stop brain worker (delegates to trading worker API)."""
    from ..routers.trading_sub.ai import api_brain_worker_stop as _stop
    return _stop(request, db)


@router.post("/api/brain/trading/worker/pause")
def api_brain_trading_worker_pause(request: Request, db: Session = Depends(get_db)):
    """Pause / resume brain worker (delegates to trading worker API)."""
    from ..routers.trading_sub.ai import api_brain_worker_pause as _pause
    return _pause(request, db)


@router.post("/api/brain/trading/worker/run-queue-batch")
async def api_brain_trading_worker_run_queue_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Run one backtest queue batch in the web process (delegates to trading worker API)."""
    from ..routers.trading_sub.ai import api_brain_worker_run_queue_batch as _run
    return await _run(request, background_tasks, db)


class _TradingAssistantMessage(BaseModel):
    role: str
    content: str


class _TradingAssistantChatBody(BaseModel):
    messages: list[_TradingAssistantMessage]
    include_pattern_search: bool = True
    refresh: bool = False


@router.post("/api/brain/trading/assistant/chat")
def api_brain_trading_assistant_chat(
    body: _TradingAssistantChatBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Chat with the Trading Brain Assistant (LLM grounded in trading DB and worker state)."""
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot use the assistant"}, status_code=403)
    from ..services.trading.brain_assistant import chat as trading_assistant_chat
    conversation = [{"role": m.role, "content": m.content} for m in body.messages]
    result = trading_assistant_chat(
        db,
        ctx["user_id"],
        conversation,
        include_pattern_search=body.include_pattern_search,
        refresh=body.refresh,
    )
    try:
        result = TradingBrainAssistantChatResponse(**result).model_dump()
    except Exception:
        pass
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


