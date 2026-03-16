"""Chili Brain: cross-domain intelligence hub.

Exposes status, metrics, and control endpoints for the Brain.
Domains: Trading (active), Code (active).
"""
from __future__ import annotations

import logging

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
from ..services.reasoning_brain import proactive_chat as rb_chat
from ..models import (
    ReasoningAnticipation,
    ReasoningConfidenceSnapshot,
    ReasoningEvent,
    ReasoningHypothesis,
    ReasoningInterest,
    ReasoningLearningGoal,
    ReasoningResearch,
    ReasoningUserModel,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["brain"])


# ── Page ────────────────────────────────────────────────────────────────

@router.get("/brain", response_class=HTMLResponse)
def brain_page(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    return request.app.state.templates.TemplateResponse(
        request, "brain.html",
        {
            "title": "Chili Brain",
            "is_guest": ctx["is_guest"],
            "user_name": ctx["user_name"],
        },
    )


# ── Cross-domain status ────────────────────────────────────────────────

@router.get("/api/brain/domains")
def api_brain_domains():
    """List all Brain domains and their high-level status."""
    trading_st = ts.get_learning_status()
    code_st = cb_learning.get_code_learning_status()
    reasoning_st = rb_learning.get_reasoning_status()
    return JSONResponse({
        "ok": True,
        "domains": [
            {
                "id": "trading",
                "label": "Trading",
                "status": "learning" if trading_st.get("running") else "idle",
                "last_run": trading_st.get("last_run"),
                "phase": trading_st.get("phase", "idle"),
            },
            {
                "id": "project",
                "label": "Project",
                "status": "learning" if code_st.get("running") else "idle",
                "last_run": code_st.get("last_run"),
                "phase": code_st.get("phase", "idle"),
                "lenses": [l["name"] for l in cb_lenses.list_lenses()],
            },
            {
                "id": "reasoning",
                "label": "Reasoning",
                "status": "learning" if reasoning_st.get("running") else "idle",
                "last_run": reasoning_st.get("last_run"),
                "phase": reasoning_st.get("phase", "idle"),
            },
        ],
    })


@router.get("/api/brain/status")
def api_brain_status():
    """Unified Brain health across all domains."""
    trading_st = ts.get_learning_status()
    code_st = cb_learning.get_code_learning_status()
    reasoning_st = rb_learning.get_reasoning_status()
    return JSONResponse({
        "ok": True,
        "trading": trading_st,
        "code": code_st,
        "reasoning": reasoning_st,
    })


# ── Trading domain: metrics ────────────────────────────────────────────

@router.get("/api/brain/trading/metrics")
def api_brain_trading_metrics(request: Request, db: Session = Depends(get_db)):
    """Aggregate trading brain metrics (KPIs, patterns, predictions)."""
    ctx = get_identity_ctx(request, db)
    stats = ts.get_brain_stats(db, ctx["user_id"])
    return JSONResponse({"ok": True, **stats})


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
