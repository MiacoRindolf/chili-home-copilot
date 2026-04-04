"""Chili Brain: cross-domain intelligence hub.

Exposes status, metrics, and control endpoints for the Brain.
Domains: Trading (active), Code (active).
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..services import trading_service as ts
from ..services.code_brain import learning as cb_learning
from ..services.code_brain import indexer as cb_indexer
from ..services.code_brain import insights as cb_insights
from ..services.code_brain import graph as cb_graph
from ..services.code_brain import trends as cb_trends
from ..services.code_brain import reviewer as cb_reviewer
from ..services.code_brain import deps_scanner as cb_deps
from ..services.code_brain import search as cb_search
from ..services.code_brain import lenses as cb_lenses
from ..services.reasoning_brain import learning as rb_learning
from ..services.project_brain import registry as pb_registry
from ..services.project_brain import learning as pb_learning
from ..services.reasoning_brain import proactive_chat as rb_chat
from ..services.trading.brain_network_graph import get_trading_brain_network_graph
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
    # region agent log
    import time as _time

    from ..debug_agent_log import agent_log as _agent_log

    _bp_t0 = _time.perf_counter()
    _agent_log("H3", "brain_page", "handler_entry", {})
    # endregion
    ctx = get_identity_ctx(request, db)
    resp = request.app.state.templates.TemplateResponse(
        request, "brain.html",
        {
            "title": "Chili Brain",
            "is_guest": ctx["is_guest"],
            "user_name": ctx["user_name"],
            "planner_task_id": planner_task_id,
            "planner_project_id": planner_project_id,
            "trading_brain_network_graph": get_trading_brain_network_graph(),
        },
    )
    # Large inline script in template — avoid stale UI after deploy (Pine export, etc.).
    resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
    # region agent log
    _agent_log(
        "H3",
        "brain_page",
        "handler_exit",
        {"ms": round((_time.perf_counter() - _bp_t0) * 1000, 1)},
    )
    # endregion
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


# ── Trading domain: metrics ────────────────────────────────────────────

@router.get("/api/brain/trading/metrics")
def api_brain_trading_metrics(request: Request, db: Session = Depends(get_db)):
    """Aggregate trading brain metrics (KPIs, patterns, predictions)."""
    ctx = get_identity_ctx(request, db)
    stats = ts.get_brain_stats(db, ctx["user_id"])
    return JSONResponse({"ok": True, **stats})


@router.get("/api/brain/trading/network-graph")
def api_brain_trading_network_graph():
    """Static governance graph for Trading Brain Network (skill-tree UI)."""
    return JSONResponse(get_trading_brain_network_graph())


@router.get("/api/brain/network-graph")
def api_brain_network_graph_compat():
    """Same payload as ``/api/brain/trading/network-graph`` for external SPAs (e.g. dev on :3000)."""
    return JSONResponse(get_trading_brain_network_graph())


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
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


# ══════════════════════════════════════════════════════════════════════════
# Code Brain domain
# ══════════════════════════════════════════════════════════════════════════

@router.get("/api/brain/code/metrics")
def api_brain_code_metrics(request: Request, db: Session = Depends(get_db)):
    """Code Brain dashboard metrics."""
    ctx = get_identity_ctx(request, db)
    metrics = cb_learning.get_code_brain_metrics(db, ctx["user_id"])
    return JSONResponse({"ok": True, **metrics})


@router.post("/api/brain/code/learn")
def api_brain_code_learn(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Trigger a Code Brain learning cycle in the background."""
    ctx = get_identity_ctx(request, db)
    status = cb_learning.get_code_learning_status()
    if status.get("running"):
        return JSONResponse({"ok": False, "message": "Code learning cycle already in progress"})

    from ..db import SessionLocal

    def _bg(user_id):
        sdb = SessionLocal()
        try:
            cb_learning.run_code_learning_cycle(sdb, user_id)
        finally:
            sdb.close()

    background_tasks.add_task(_bg, ctx["user_id"])
    return JSONResponse({"ok": True, "message": "Code learning cycle started"})


@router.get("/api/brain/code/hotspots")
def api_brain_code_hotspots(db: Session = Depends(get_db)):
    """Top files by churn x complexity across all repos."""
    from ..models.code_brain import CodeHotspot
    hotspots = (
        db.query(CodeHotspot)
        .order_by(CodeHotspot.combined_score.desc())
        .limit(30)
        .all()
    )
    return JSONResponse({
        "ok": True,
        "hotspots": [
            {
                "id": h.id,
                "repo_id": h.repo_id,
                "file": h.file_path,
                "churn": round(h.churn_score, 3),
                "complexity": round(h.complexity_score, 3),
                "combined": round(h.combined_score, 3),
                "commits": h.commit_count,
                "last_commit": h.last_commit_date.isoformat() if h.last_commit_date else None,
            }
            for h in hotspots
        ],
    })


@router.get("/api/brain/code/insights")
def api_brain_code_insights(
    db: Session = Depends(get_db),
    category: str | None = None,
    repo_id: int | None = None,
):
    """Discovered patterns and conventions."""
    data = cb_insights.get_insights(db, repo_id=repo_id, category=category)
    return JSONResponse({"ok": True, "insights": data})


@router.get("/api/brain/code/repos")
def api_brain_code_repos(request: Request, db: Session = Depends(get_db)):
    """List registered repos with stats."""
    ctx = get_identity_ctx(request, db)
    repos = cb_indexer.get_registered_repos(db, user_id=ctx["user_id"])
    return JSONResponse({"ok": True, "repos": repos})


class _AddRepoBody(BaseModel):
    path: str
    name: str | None = None


@router.post("/api/brain/code/repos")
def api_brain_code_add_repo(
    body: _AddRepoBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Register a new local repo for Code Brain indexing."""
    ctx = get_identity_ctx(request, db)
    result = cb_indexer.register_repo(db, body.path, name=body.name, user_id=ctx["user_id"])
    if "error" in result:
        return JSONResponse({"ok": False, "message": result["error"]}, status_code=400)
    return JSONResponse({"ok": True, **result})


# ══════════════════════════════════════════════════════════════════════════
# Reasoning Brain domain
# ══════════════════════════════════════════════════════════════════════════


@router.get("/api/brain/reasoning/metrics")
def api_brain_reasoning_metrics(request: Request, db: Session = Depends(get_db)):
    """Metrics + high-level status for Reasoning Brain."""
    ctx = get_identity_ctx(request, db)
    metrics = rb_learning.get_reasoning_metrics(db, ctx["user_id"])
    return JSONResponse({"ok": True, **metrics})


@router.post("/api/brain/reasoning/learn")
def api_brain_reasoning_learn(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Trigger a Reasoning Brain learning cycle for this user."""
    ctx = get_identity_ctx(request, db)
    status = rb_learning.get_reasoning_status()
    if status.get("running"):
        return JSONResponse({"ok": False, "message": "Reasoning cycle already in progress"})

    from ..db import SessionLocal

    def _bg(user_id: int):
        sdb = SessionLocal()
        try:
            rb_learning.run_reasoning_cycle(sdb, user_id, trace_id="manual")
        finally:
            sdb.close()

    background_tasks.add_task(_bg, ctx["user_id"])
    return JSONResponse({"ok": True, "message": "Reasoning cycle started"})


@router.get("/api/brain/reasoning/model")
def api_brain_reasoning_model(request: Request, db: Session = Depends(get_db)):
    """Current ReasoningUserModel snapshot."""
    ctx = get_identity_ctx(request, db)
    um = (
        db.query(ReasoningUserModel)
        .filter(ReasoningUserModel.user_id == ctx["user_id"], ReasoningUserModel.active.is_(True))
        .order_by(ReasoningUserModel.created_at.desc())
        .first()
    )
    if not um:
        return JSONResponse({"ok": True, "model": None})
    return JSONResponse({
        "ok": True,
        "model": {
            "decision_style": um.decision_style,
            "risk_tolerance": um.risk_tolerance,
            "communication_prefs": um.communication_prefs,
            "active_goals": um.active_goals,
            "knowledge_gaps": um.knowledge_gaps,
            "created_at": um.created_at.isoformat() if um.created_at else None,
        },
    })


@router.get("/api/brain/reasoning/interests")
def api_brain_reasoning_interests(request: Request, db: Session = Depends(get_db)):
    """Weighted interest list for this user."""
    ctx = get_identity_ctx(request, db)
    rows = (
        db.query(ReasoningInterest)
        .filter(ReasoningInterest.user_id == ctx["user_id"])
        .order_by(ReasoningInterest.weight.desc())
        .limit(100)
        .all()
    )
    return JSONResponse({
        "ok": True,
        "interests": [
            {
                "topic": r.topic,
                "category": r.category,
                "weight": float(r.weight or 0.0),
                "source": r.source,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
            for r in rows
        ],
    })


@router.get("/api/brain/reasoning/research")
def api_brain_reasoning_research(request: Request, db: Session = Depends(get_db)):
    """Recent web research summaries."""
    ctx = get_identity_ctx(request, db)
    rows = (
        db.query(ReasoningResearch)
        .filter(ReasoningResearch.user_id == ctx["user_id"])
        .order_by(ReasoningResearch.searched_at.desc())
        .limit(50)
        .all()
    )
    return JSONResponse({
        "ok": True,
        "research": [
            {
                "id": r.id,
                "topic": r.topic,
                "summary": r.summary,
                "sources": r.sources,
                "relevance_score": float(r.relevance_score or 0.0),
                "searched_at": r.searched_at.isoformat() if r.searched_at else None,
                "stale": bool(r.stale),
            }
            for r in rows
        ],
    })


@router.get("/api/brain/reasoning/anticipations")
def api_brain_reasoning_anticipations(request: Request, db: Session = Depends(get_db)):
    """Current anticipations that haven't been dismissed."""
    ctx = get_identity_ctx(request, db)
    rows = (
        db.query(ReasoningAnticipation)
        .filter(
            ReasoningAnticipation.user_id == ctx["user_id"],
            ReasoningAnticipation.dismissed.is_(False),
        )
        .order_by(ReasoningAnticipation.created_at.desc())
        .limit(50)
        .all()
    )
    return JSONResponse({
        "ok": True,
        "anticipations": [
            {
                "id": a.id,
                "description": a.description,
                "domain": a.domain,
                "context": a.context,
                "confidence": float(a.confidence or 0.0),
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "acted_on": bool(a.acted_on),
                "dismissed": bool(a.dismissed),
            }
            for a in rows
        ],
    })


@router.post("/api/brain/reasoning/anticipations/{anticipation_id}/dismiss")
def api_brain_reasoning_dismiss_anticipation(
    anticipation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Dismiss an anticipation as not helpful."""
    ctx = get_identity_ctx(request, db)
    row = (
        db.query(ReasoningAnticipation)
        .filter(
            ReasoningAnticipation.id == anticipation_id,
            ReasoningAnticipation.user_id == ctx["user_id"],
        )
        .first()
    )
    if not row:
        return JSONResponse({"ok": False, "message": "Not found"}, status_code=404)
    row.dismissed = True
    db.add(row)
    db.commit()
    return JSONResponse({"ok": True})


@router.get("/api/brain/reasoning/goals")
def api_brain_reasoning_goals(request: Request, db: Session = Depends(get_db)):
    """List reasoning learning goals for this user."""
    ctx = get_identity_ctx(request, db)
    rows = (
        db.query(ReasoningLearningGoal)
        .filter(ReasoningLearningGoal.user_id == ctx["user_id"])
        .order_by(ReasoningLearningGoal.created_at.desc())
        .all()
    )
    return JSONResponse({
        "ok": True,
        "goals": [
            {
                "id": g.id,
                "dimension": g.dimension,
                "description": g.description,
                "status": g.status,
                "confidence_before": g.confidence_before,
                "confidence_after": g.confidence_after,
                "evidence_count": g.evidence_count,
                "created_at": g.created_at.isoformat() if g.created_at else None,
                "completed_at": g.completed_at.isoformat() if g.completed_at else None,
            }
            for g in rows
        ],
    })


@router.get("/api/brain/reasoning/hypotheses")
def api_brain_reasoning_hypotheses(request: Request, db: Session = Depends(get_db)):
    """List reasoning hypotheses with confidence/evidence."""
    ctx = get_identity_ctx(request, db)
    rows = (
        db.query(ReasoningHypothesis)
        .filter(ReasoningHypothesis.user_id == ctx["user_id"])
        .order_by(ReasoningHypothesis.created_at.desc())
        .limit(100)
        .all()
    )
    return JSONResponse({
        "ok": True,
        "hypotheses": [
            {
                "id": h.id,
                "claim": h.claim,
                "domain": h.domain,
                "confidence": float(h.confidence or 0.0),
                "evidence_for": h.evidence_for,
                "evidence_against": h.evidence_against,
                "tested_at": h.tested_at.isoformat() if h.tested_at else None,
                "created_at": h.created_at.isoformat() if h.created_at else None,
                "active": bool(h.active),
            }
            for h in rows
        ],
    })


@router.get("/api/brain/reasoning/confidence-history")
def api_brain_reasoning_conf_history(
    request: Request,
    db: Session = Depends(get_db),
    dimension: str | None = None,
):
    """Confidence history per dimension for Reasoning Brain chart."""
    ctx = get_identity_ctx(request, db)
    q = db.query(ReasoningConfidenceSnapshot).filter(
        ReasoningConfidenceSnapshot.user_id == ctx["user_id"]
    )
    if dimension:
        q = q.filter(ReasoningConfidenceSnapshot.dimension == dimension)
    rows = q.order_by(ReasoningConfidenceSnapshot.snapshot_date.asc()).all()
    return JSONResponse({
        "ok": True,
        "data": [
            {
                "time": int(r.snapshot_date.timestamp()),
                "value": round(float(r.confidence_value or 0.0) * 100, 1),
                "dimension": r.dimension,
            }
            for r in rows
        ],
    })


@router.get("/api/brain/reasoning/insight-chat/opener")
def api_brain_reasoning_insight_opener(request: Request, db: Session = Depends(get_db)):
    """Return or generate Chili's next Insight Chat opener."""
    ctx = get_identity_ctx(request, db)
    opener = rb_chat.get_pending_opener(db, ctx["user_id"])
    if not opener:
        return JSONResponse({"ok": True, "opener": None})
    return JSONResponse({"ok": True, "opener": opener})


class _InsightReplyBody(BaseModel):
    message: str
    goal_id: int


@router.post("/api/brain/reasoning/insight-chat/reply")
def api_brain_reasoning_insight_reply(
    body: _InsightReplyBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Process a user reply in the Insight Chat."""
    ctx = get_identity_ctx(request, db)
    result = rb_chat.process_insight_reply(db, ctx["user_id"], body.message, body.goal_id)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@router.post("/api/brain/reasoning/insight-chat/generate")
def api_brain_reasoning_insight_generate(request: Request, db: Session = Depends(get_db)):
    """Force-generate a new Insight Chat opener for the current user."""
    ctx = get_identity_ctx(request, db)
    goal = (
        db.query(ReasoningLearningGoal)
        .filter(
            ReasoningLearningGoal.user_id == ctx["user_id"],
            ReasoningLearningGoal.status.in_(["pending", "active"]),
        )
        .order_by(ReasoningLearningGoal.created_at.asc())
        .first()
    )
    if not goal:
        goal = ReasoningLearningGoal(
            user_id=ctx["user_id"],
            dimension="general_personality",
            description="Understand the user's general preferences and priorities.",
            status="active",
            created_at=datetime.utcnow(),
        )
        db.add(goal)
        db.commit()
    opener = rb_chat.generate_opening_message(db, ctx["user_id"], goal)
    if not opener:
        return JSONResponse({"ok": False, "message": "Failed to generate opener"}, status_code=400)
    return JSONResponse({"ok": True, "opener": opener})


@router.delete("/api/brain/code/repos/{repo_id}")
def api_brain_code_remove_repo(repo_id: int, db: Session = Depends(get_db)):
    """Deactivate a repo."""
    result = cb_indexer.unregister_repo(db, repo_id)
    if "error" in result:
        return JSONResponse({"ok": False, "message": result["error"]}, status_code=404)
    return JSONResponse({"ok": True})


# ── Code Brain: Graph, Trends, Reviews, Deps, Search ──────────────────

@router.get("/api/brain/code/graph")
def api_brain_code_graph(repo_id: int = Query(...), db: Session = Depends(get_db)):
    """Return the architecture dependency graph for a repo."""
    data = cb_graph.get_graph_data(db, repo_id)
    return JSONResponse({"ok": True, **data})


@router.get("/api/brain/code/trends")
def api_brain_code_trends(repo_id: int = Query(...), db: Session = Depends(get_db)):
    """Quality trend time series and deltas."""
    series = cb_trends.get_quality_trends(db, repo_id, limit=30)
    deltas = cb_trends.compute_trend_deltas(db, repo_id)
    return JSONResponse({"ok": True, "series": series, "deltas": deltas})


@router.get("/api/brain/code/reviews")
def api_brain_code_reviews(
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Recent LLM code reviews."""
    reviews = cb_reviewer.get_recent_reviews(db, repo_id=repo_id, limit=20)
    return JSONResponse({"ok": True, "reviews": reviews})


@router.get("/api/brain/code/deps")
def api_brain_code_deps(
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Dependency health alerts."""
    health = cb_deps.get_dep_health(db, repo_id=repo_id)
    return JSONResponse({"ok": True, **health})


class _CodeSearchBody(BaseModel):
    query: str
    repo_id: int | None = None
    use_llm: bool = False


@router.post("/api/brain/code/search")
def api_brain_code_search(
    body: _CodeSearchBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Search code symbols and optionally ask the LLM."""
    if body.use_llm:
        ctx = get_identity_ctx(request, db)
        result = cb_search.search_with_llm(db, body.query, repo_id=body.repo_id, user_id=ctx["user_id"])
    else:
        results = cb_search.search_code(db, body.query, repo_id=body.repo_id)
        result = {"query": body.query, "results": results}
    return JSONResponse({"ok": True, **result})


# ── Code Agent ─────────────────────────────────────────────────────────

class _AgentRequest(BaseModel):
    prompt: str
    repo_id: int | None = None
    apply: bool = False


@router.post("/api/brain/code/agent")
async def api_brain_code_agent(
    body: _AgentRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Run the Code Agent: analyze request -> gather context -> propose changes."""
    ctx = get_identity_ctx(request, db)
    from ..services.code_brain.agent import run_code_agent
    result = await run_code_agent(db, body.prompt, repo_id=body.repo_id, user_id=ctx["user_id"])
    if "error" in result:
        return JSONResponse({"ok": False, "message": result["error"]}, status_code=400)
    return JSONResponse({"ok": True, **result})


# ══════════════════════════════════════════════════════════════════════════
# Project domain — role-based lenses over the shared Code Brain engine
# ══════════════════════════════════════════════════════════════════════════

@router.get("/api/brain/project/lenses")
def api_brain_project_lenses():
    """List all available project lenses."""
    return JSONResponse({"ok": True, "lenses": cb_lenses.list_lenses()})


@router.get("/api/brain/project/lens/{lens_name}/metrics")
def api_brain_project_lens_metrics(
    lens_name: str,
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Metrics filtered through a specific role lens."""
    if not cb_lenses.get_lens(lens_name):
        return JSONResponse({"ok": False, "message": f"Unknown lens: {lens_name}"}, status_code=404)
    if repo_id is None:
        from ..models.code_brain import CodeRepo
        first = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).first()
        repo_id = first.id if first else None
    metrics = cb_lenses.get_lens_metrics(db, lens_name, repo_id=repo_id)
    planner_data = None
    lens_obj = cb_lenses.get_lens(lens_name)
    if lens_obj and lens_obj.planner_integration:
        try:
            from ..services import planner_service
            planner_data = planner_service.get_all_users_task_summary(db)
        except Exception:
            planner_data = None
    return JSONResponse({"ok": True, **metrics, "planner": planner_data})


@router.get("/api/brain/project/lens/{lens_name}/hotspots")
def api_brain_project_lens_hotspots(
    lens_name: str,
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Hotspots filtered by lens file patterns."""
    if not cb_lenses.get_lens(lens_name):
        return JSONResponse({"ok": False, "message": f"Unknown lens: {lens_name}"}, status_code=404)
    data = cb_lenses.get_lens_hotspots(db, lens_name, repo_id=repo_id)
    return JSONResponse({"ok": True, "hotspots": data})


@router.get("/api/brain/project/lens/{lens_name}/insights")
def api_brain_project_lens_insights(
    lens_name: str,
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Insights filtered by lens categories."""
    if not cb_lenses.get_lens(lens_name):
        return JSONResponse({"ok": False, "message": f"Unknown lens: {lens_name}"}, status_code=404)
    data = cb_lenses.get_lens_insights(db, lens_name, repo_id=repo_id)
    return JSONResponse({"ok": True, "insights": data})


# ══════════════════════════════════════════════════════════════════════════
# Project Brain — Autonomous Agents
# ══════════════════════════════════════════════════════════════════════════

@router.get("/api/brain/project/agents")
def api_brain_project_agents(request: Request, db: Session = Depends(get_db)):
    """List all agents with their status and metrics."""
    ctx = get_identity_ctx(request, db)
    agents = []
    for name, agent in pb_registry.AGENT_REGISTRY.items():
        agents.append(agent.get_metrics(db, ctx["user_id"]))
    return JSONResponse({"ok": True, "agents": agents})


@router.get("/api/brain/project/agent/{name}/metrics")
def api_brain_project_agent_metrics(name: str, request: Request, db: Session = Depends(get_db)):
    agent = pb_registry.get_agent(name)
    if not agent:
        return JSONResponse({"ok": False, "message": f"Unknown agent: {name}"}, status_code=404)
    ctx = get_identity_ctx(request, db)
    return JSONResponse({"ok": True, **agent.get_metrics(db, ctx["user_id"])})


@router.get("/api/brain/project/agent/{name}/findings")
def api_brain_project_agent_findings(name: str, request: Request, db: Session = Depends(get_db)):
    agent = pb_registry.get_agent(name)
    if not agent:
        return JSONResponse({"ok": False, "message": f"Unknown agent: {name}"}, status_code=404)
    ctx = get_identity_ctx(request, db)
    findings = agent.get_findings(db, ctx["user_id"])
    return JSONResponse({"ok": True, "findings": [
        {
            "id": f.id, "category": f.category, "title": f.title,
            "description": f.description, "severity": f.severity,
            "status": f.status,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in findings
    ]})


@router.get("/api/brain/project/agent/{name}/goals")
def api_brain_project_agent_goals(name: str, request: Request, db: Session = Depends(get_db)):
    agent = pb_registry.get_agent(name)
    if not agent:
        return JSONResponse({"ok": False, "message": f"Unknown agent: {name}"}, status_code=404)
    ctx = get_identity_ctx(request, db)
    goals = agent.get_goals(db, ctx["user_id"], active_only=False)
    return JSONResponse({"ok": True, "goals": [
        {
            "id": g.id, "description": g.description, "goal_type": g.goal_type,
            "status": g.status, "progress": g.progress, "evidence_count": g.evidence_count,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        }
        for g in goals
    ]})


@router.get("/api/brain/project/agent/{name}/research")
def api_brain_project_agent_research(name: str, request: Request, db: Session = Depends(get_db)):
    agent = pb_registry.get_agent(name)
    if not agent:
        return JSONResponse({"ok": False, "message": f"Unknown agent: {name}"}, status_code=404)
    ctx = get_identity_ctx(request, db)
    research = agent.get_research(db, ctx["user_id"])
    return JSONResponse({"ok": True, "research": [
        {
            "id": r.id, "topic": r.topic, "summary": r.summary,
            "sources_json": r.sources_json,
            "relevance_score": r.relevance_score,
            "searched_at": r.searched_at.isoformat() if r.searched_at else None,
        }
        for r in research
    ]})


@router.get("/api/brain/project/agent/{name}/evolution")
def api_brain_project_agent_evolution(name: str, request: Request, db: Session = Depends(get_db)):
    agent = pb_registry.get_agent(name)
    if not agent:
        return JSONResponse({"ok": False, "message": f"Unknown agent: {name}"}, status_code=404)
    ctx = get_identity_ctx(request, db)
    evolutions = agent.get_evolution(db, ctx["user_id"])
    return JSONResponse({"ok": True, "evolution": [
        {
            "id": e.id, "dimension": e.dimension, "description": e.description,
            "confidence_before": e.confidence_before, "confidence_after": e.confidence_after,
            "trigger": e.trigger,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in evolutions
    ]})


@router.post("/api/brain/project/agent/{name}/cycle")
def api_brain_project_agent_cycle(
    name: str,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Trigger a learning cycle for a specific agent."""
    agent = pb_registry.get_agent(name)
    if not agent:
        return JSONResponse({"ok": False, "message": f"Unknown agent: {name}"}, status_code=404)
    status = pb_learning.get_project_brain_status()
    if status.get("running"):
        return JSONResponse({"ok": False, "message": "A cycle is already running"})
    ctx = get_identity_ctx(request, db)

    from ..db import SessionLocal

    def _bg(user_id, agent_name):
        sdb = SessionLocal()
        try:
            pb_learning.run_project_brain_cycle(sdb, user_id, agent_name=agent_name)
        finally:
            sdb.close()

    background_tasks.add_task(_bg, ctx["user_id"], name)
    return JSONResponse({"ok": True, "message": f"{agent.label} learning cycle started"})


@router.get("/api/brain/project/messages")
def api_brain_project_messages(request: Request, db: Session = Depends(get_db)):
    """Inter-agent message feed."""
    ctx = get_identity_ctx(request, db)
    feed = pb_registry.get_message_feed(db, ctx["user_id"])
    return JSONResponse({"ok": True, "messages": feed})


# ── Product Owner specific endpoints ──────────────────────────────────

@router.get("/api/brain/project/agent/product_owner/question")
def api_brain_po_next_question(request: Request, db: Session = Depends(get_db)):
    """Get the next pending PO question for the user."""
    ctx = get_identity_ctx(request, db)
    po = pb_registry.get_agent("product_owner")
    if not po:
        return JSONResponse({"ok": False, "message": "PO agent not found"}, status_code=404)
    q = po.get_next_question(db, ctx["user_id"])
    if not q:
        return JSONResponse({"ok": True, "question": None})
    import json as _json
    opts = []
    if q.options:
        try:
            opts = _json.loads(q.options)
        except Exception:
            pass
    return JSONResponse({"ok": True, "question": {
        "id": q.id, "question": q.question, "context": q.context,
        "category": q.category, "priority": q.priority, "options": opts,
    }})


@router.get("/api/brain/project/agent/product_owner/questions")
def api_brain_po_questions(
    request: Request,
    db: Session = Depends(get_db),
    status: str | None = Query(None),
):
    """List PO questions with optional status filter."""
    ctx = get_identity_ctx(request, db)
    po = pb_registry.get_agent("product_owner")
    if not po:
        return JSONResponse({"ok": False, "message": "PO agent not found"}, status_code=404)
    import json as _json
    questions = po.get_questions(db, ctx["user_id"], status=status)

    def _parse_opts(raw):
        if not raw:
            return []
        try:
            return _json.loads(raw)
        except Exception:
            return []

    return JSONResponse({"ok": True, "questions": [
        {
            "id": q.id, "question": q.question, "context": q.context,
            "category": q.category, "priority": q.priority,
            "status": q.status, "answer": q.answer,
            "options": _parse_opts(q.options),
            "asked_at": q.asked_at.isoformat() if q.asked_at else None,
            "answered_at": q.answered_at.isoformat() if q.answered_at else None,
        }
        for q in questions
    ]})


class _POAnswerBody(BaseModel):
    answer: str


@router.post("/api/brain/project/agent/product_owner/question/{question_id}/answer")
def api_brain_po_answer(question_id: int, body: _POAnswerBody, db: Session = Depends(get_db)):
    """Submit an answer to a PO question."""
    po = pb_registry.get_agent("product_owner")
    if not po:
        return JSONResponse({"ok": False, "message": "PO agent not found"}, status_code=404)
    q = po.answer_question(db, question_id, body.answer)
    if not q:
        return JSONResponse({"ok": False, "message": "Question not found"}, status_code=404)
    return JSONResponse({"ok": True, "question": {
        "id": q.id, "status": q.status, "answer": q.answer,
    }})


@router.post("/api/brain/project/agent/product_owner/question/{question_id}/skip")
def api_brain_po_skip(question_id: int, db: Session = Depends(get_db)):
    """Skip a PO question."""
    po = pb_registry.get_agent("product_owner")
    if not po:
        return JSONResponse({"ok": False, "message": "PO agent not found"}, status_code=404)
    q = po.skip_question(db, question_id)
    if not q:
        return JSONResponse({"ok": False, "message": "Question not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/api/brain/project/agent/product_owner/refresh-options")
def api_brain_po_refresh_options(request: Request, db: Session = Depends(get_db)):
    """Generate options for any pending questions that lack them."""
    ctx = get_identity_ctx(request, db)
    po = pb_registry.get_agent("product_owner")
    if not po:
        return JSONResponse({"ok": False, "message": "PO agent not found"}, status_code=404)
    context = po._review_context(db, ctx["user_id"])
    upgraded = po._upgrade_optionless_questions(db, ctx["user_id"], context)
    return JSONResponse({"ok": True, "upgraded": upgraded})


@router.get("/api/brain/project/agent/product_owner/requirements")
def api_brain_po_requirements(request: Request, db: Session = Depends(get_db)):
    """List PO-gathered requirements."""
    ctx = get_identity_ctx(request, db)
    po = pb_registry.get_agent("product_owner")
    if not po:
        return JSONResponse({"ok": False, "message": "PO agent not found"}, status_code=404)
    reqs = po.get_requirements(db, ctx["user_id"])
    return JSONResponse({"ok": True, "requirements": [
        {
            "id": r.id, "title": r.title, "description": r.description,
            "priority": r.priority, "status": r.status,
            "acceptance_criteria": r.acceptance_criteria,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in reqs
    ]})


class _ReqToTaskBody(BaseModel):
    project_id: int | None = None


@router.post("/api/brain/project/agent/product_owner/requirement/{requirement_id}/to-task")
def api_brain_po_req_to_task(
    requirement_id: int,
    request: Request,
    body: _ReqToTaskBody | None = None,
    db: Session = Depends(get_db),
):
    """Push a PO requirement to the Planner as a task."""
    ctx = get_identity_ctx(request, db)
    po = pb_registry.get_agent("product_owner")
    if not po:
        return JSONResponse({"ok": False, "message": "PO agent not found"}, status_code=404)
    pid = body.project_id if body else None
    result = po.push_requirement_to_planner(db, ctx["user_id"], requirement_id, project_id=pid)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status_code)


@router.get("/api/brain/project/planner-projects")
def api_brain_planner_projects(db: Session = Depends(get_db)):
    """List all planner projects in the household for the project picker."""
    from ..services import planner_service
    projects = planner_service.list_all_projects(db)
    return JSONResponse({"ok": True, "projects": projects})


# ── Project Manager specific endpoints ────────────────────────────────

@router.get("/api/brain/project/agent/project_manager/velocity")
def api_brain_pm_velocity(request: Request, db: Session = Depends(get_db)):
    """Current velocity and project health metrics."""
    ctx = get_identity_ctx(request, db)
    pm = pb_registry.get_agent("project_manager")
    if not pm:
        return JSONResponse({"ok": False, "message": "PM agent not found"}, status_code=404)
    return JSONResponse({"ok": True, **pm.get_velocity(db, ctx["user_id"])})


@router.get("/api/brain/project/agent/project_manager/health")
def api_brain_pm_health(request: Request, db: Session = Depends(get_db)):
    """Comprehensive project health report."""
    ctx = get_identity_ctx(request, db)
    pm = pb_registry.get_agent("project_manager")
    if not pm:
        return JSONResponse({"ok": False, "message": "PM agent not found"}, status_code=404)
    health = pm.get_project_health(db, ctx["user_id"])
    health.pop("projects", None)
    return JSONResponse({"ok": True, **health})


@router.get("/api/brain/project/agent/project_manager/breakdown")
def api_brain_pm_breakdown(request: Request, db: Session = Depends(get_db)):
    """Task status breakdown by project."""
    ctx = get_identity_ctx(request, db)
    pm = pb_registry.get_agent("project_manager")
    if not pm:
        return JSONResponse({"ok": False, "message": "PM agent not found"}, status_code=404)
    return JSONResponse({"ok": True, **pm.get_task_breakdown(db, ctx["user_id"])})


# ── Architect specific endpoints ──────────────────────────────────────

@router.get("/api/brain/project/agent/architect/health")
def api_brain_arch_health(request: Request, db: Session = Depends(get_db)):
    """Comprehensive architecture health report."""
    ctx = get_identity_ctx(request, db)
    arch = pb_registry.get_agent("architect")
    if not arch:
        return JSONResponse({"ok": False, "message": "Architect agent not found"}, status_code=404)
    return JSONResponse({"ok": True, **arch.get_architecture_health(db, ctx["user_id"])})


# ── Global Project Brain endpoints ────────────────────────────────────

@router.post("/api/brain/project/cycle")
def api_brain_project_cycle_all(request: Request, db: Session = Depends(get_db), background_tasks: BackgroundTasks = None):
    """Trigger a learning cycle for ALL active agents."""
    from ..services.project_brain.learning import run_project_brain_cycle_background
    from ..db import SessionLocal
    ctx = get_identity_ctx(request, db)
    run_project_brain_cycle_background(SessionLocal, ctx["user_id"])
    return JSONResponse({"ok": True, "message": "All-agent cycle started in background"})


@router.get("/api/brain/project/status")
def api_brain_project_status():
    """Global Project Brain status (running, progress, last run)."""
    from ..services.project_brain.learning import get_project_brain_status
    return JSONResponse({"ok": True, **get_project_brain_status()})


@router.get("/api/brain/project/metrics")
def api_brain_project_metrics(request: Request, db: Session = Depends(get_db)):
    """Aggregate metrics across all Project Brain agents."""
    from ..services.project_brain.learning import get_project_brain_metrics
    ctx = get_identity_ctx(request, db)
    return JSONResponse({"ok": True, **get_project_brain_metrics(db, ctx["user_id"])})


# ── QA Engineer specific endpoints ───────────────────────────────────

@router.get("/api/brain/project/agent/qa/test-cases")
def api_brain_qa_test_cases(request: Request, db: Session = Depends(get_db)):
    """List QA test cases."""
    ctx = get_identity_ctx(request, db)
    qa = pb_registry.get_agent("qa")
    if not qa:
        return JSONResponse({"ok": False, "message": "QA agent not found"}, status_code=404)
    cases = qa.get_test_cases(db, ctx["user_id"])
    return JSONResponse({"ok": True, "test_cases": [
        {"id": c.id, "name": c.name, "priority": c.priority, "status": c.status,
         "steps": c.steps_json, "expected": c.expected_json,
         "last_run_at": c.last_run_at.isoformat() if c.last_run_at else None,
         "created_at": c.created_at.isoformat() if c.created_at else None}
        for c in cases
    ]})


@router.get("/api/brain/project/agent/qa/test-runs")
def api_brain_qa_test_runs(request: Request, db: Session = Depends(get_db)):
    """List QA test execution history."""
    ctx = get_identity_ctx(request, db)
    qa = pb_registry.get_agent("qa")
    if not qa:
        return JSONResponse({"ok": False, "message": "QA agent not found"}, status_code=404)
    runs = qa.get_test_runs(db, ctx["user_id"])
    return JSONResponse({"ok": True, "test_runs": [
        {"id": r.id, "test_name": r.test_name, "passed": r.passed,
         "duration_ms": r.duration_ms, "errors": r.errors_json,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in runs
    ]})


@router.get("/api/brain/project/agent/qa/bug-reports")
def api_brain_qa_bug_reports(request: Request, db: Session = Depends(get_db)):
    """List QA bug reports."""
    ctx = get_identity_ctx(request, db)
    qa = pb_registry.get_agent("qa")
    if not qa:
        return JSONResponse({"ok": False, "message": "QA agent not found"}, status_code=404)
    bugs = qa.get_bug_reports(db, ctx["user_id"])
    return JSONResponse({"ok": True, "bug_reports": [
        {"id": b.id, "title": b.title, "description": b.description,
         "severity": b.severity, "status": b.status,
         "reproduction_steps": b.reproduction_steps,
         "created_at": b.created_at.isoformat() if b.created_at else None}
        for b in bugs
    ]})
