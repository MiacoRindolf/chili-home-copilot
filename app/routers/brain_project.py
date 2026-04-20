"""Project domain API routes for the Brain surface.

Keeps the `/brain` page entrypoint in `brain.py` while isolating the
project/code workspace APIs behind a dedicated router.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..deps import (
    get_db,
    get_identity_ctx,
    require_paired_identity,
    require_project_domain_enabled,
)
from ..services.code_brain import deps_scanner as cb_deps
from ..services.code_brain import graph as cb_graph
from ..services.code_brain import indexer as cb_indexer
from ..services.code_brain import insights as cb_insights
from ..services.code_brain import learning as cb_learning
from ..services.code_brain import lenses as cb_lenses
from ..services.code_brain import reviewer as cb_reviewer
from ..services.code_brain import search as cb_search
from ..services.code_brain import trends as cb_trends
from ..services import project_analysis
from ..services.project_domain_runs import list_timeline, record_completed_run, status_payload
from ..services.project_brain import learning as pb_learning
from ..services.project_brain import registry as pb_registry

router = APIRouter(
    tags=["brain"],
    dependencies=[Depends(require_project_domain_enabled), Depends(require_paired_identity)],
)


def _agent_not_found(name: str, *, label: str | None = None) -> JSONResponse:
    if label:
        message = f"{label} agent not found"
    else:
        message = f"Unknown agent: {name}"
    return JSONResponse({"ok": False, "message": message}, status_code=404)


def _parse_json_array(raw: str | None) -> list:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _not_found(message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=404)


def _visible_repo_ids(db: Session, user_id: int) -> list[int]:
    return cb_indexer.get_accessible_repo_ids(db, user_id, include_shared=True)


def _resolve_visible_repo_id(db: Session, user_id: int, repo_id: int | None = None) -> int | None:
    if repo_id is not None:
        repo = cb_indexer.get_accessible_repo(db, repo_id, user_id, include_shared=True)
        return int(repo.id) if repo is not None else None
    visible_ids = _visible_repo_ids(db, user_id)
    return visible_ids[0] if visible_ids else None


def _timeline_messages(db: Session, user_id: int | None) -> list[dict]:
    messages = []
    for item in list_timeline(db, user_id=user_id, limit=30):
        messages.append(
            {
                "id": item["id"],
                "from": "system",
                "to": "operator",
                "type": item["run_kind"],
                "summary": item.get("title") or item["run_kind"],
                "status": item.get("status"),
                "acknowledged": True,
                "created_at": item.get("created_at"),
            }
        )
    return messages


# Code Brain domain


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

    def _bg(user_id: int) -> None:
        sdb = SessionLocal()
        try:
            cb_learning.run_code_learning_cycle(sdb, user_id)
        finally:
            sdb.close()

    background_tasks.add_task(_bg, ctx["user_id"])
    return JSONResponse({"ok": True, "message": "Code learning cycle started"})


@router.get("/api/brain/code/hotspots")
def api_brain_code_hotspots(request: Request, db: Session = Depends(get_db)):
    """Top files by churn x complexity across visible repos."""
    from ..models.code_brain import CodeHotspot

    ctx = get_identity_ctx(request, db)
    repo_ids = _visible_repo_ids(db, ctx["user_id"])
    if not repo_ids:
        return JSONResponse({"ok": True, "hotspots": []})
    hotspots = (
        db.query(CodeHotspot)
        .filter(CodeHotspot.repo_id.in_(repo_ids))
        .order_by(CodeHotspot.combined_score.desc())
        .limit(30)
        .all()
    )
    return JSONResponse(
        {
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
        }
    )


@router.get("/api/brain/code/insights")
def api_brain_code_insights(
    request: Request,
    db: Session = Depends(get_db),
    category: str | None = None,
    repo_id: int | None = None,
):
    """Discovered patterns and conventions."""
    ctx = get_identity_ctx(request, db)
    resolved_repo_id = _resolve_visible_repo_id(db, ctx["user_id"], repo_id)
    if repo_id is not None and resolved_repo_id is None:
        return _not_found("Repo not found")
    data = cb_insights.get_insights(
        db,
        repo_id=resolved_repo_id if repo_id is not None else None,
        repo_ids=None if repo_id is not None else _visible_repo_ids(db, ctx["user_id"]),
        category=category,
    )
    return JSONResponse({"ok": True, "insights": data})


@router.get("/api/brain/code/repos")
def api_brain_code_repos(request: Request, db: Session = Depends(get_db)):
    """List registered repos with stats."""
    ctx = get_identity_ctx(request, db)
    repos = cb_indexer.get_registered_repos(db, user_id=ctx["user_id"], include_shared=True)
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


@router.delete("/api/brain/code/repos/{repo_id}")
def api_brain_code_remove_repo(repo_id: int, request: Request, db: Session = Depends(get_db)):
    """Deactivate a repo."""
    ctx = get_identity_ctx(request, db)
    result = cb_indexer.unregister_repo(db, repo_id, user_id=ctx["user_id"])
    if "error" in result:
        return JSONResponse({"ok": False, "message": result["error"]}, status_code=404)
    return JSONResponse({"ok": True})


@router.get("/api/brain/code/graph")
def api_brain_code_graph(request: Request, repo_id: int = Query(...), db: Session = Depends(get_db)):
    """Return the architecture dependency graph for a repo."""
    ctx = get_identity_ctx(request, db)
    resolved_repo_id = _resolve_visible_repo_id(db, ctx["user_id"], repo_id)
    if resolved_repo_id is None:
        return _not_found("Repo not found")
    data = cb_graph.get_graph_data(db, resolved_repo_id)
    return JSONResponse({"ok": True, **data})


@router.get("/api/brain/code/trends")
def api_brain_code_trends(request: Request, repo_id: int = Query(...), db: Session = Depends(get_db)):
    """Quality trend time series and deltas."""
    ctx = get_identity_ctx(request, db)
    resolved_repo_id = _resolve_visible_repo_id(db, ctx["user_id"], repo_id)
    if resolved_repo_id is None:
        return _not_found("Repo not found")
    series = cb_trends.get_quality_trends(db, resolved_repo_id, limit=30)
    deltas = cb_trends.compute_trend_deltas(db, resolved_repo_id)
    return JSONResponse({"ok": True, "series": series, "deltas": deltas})


@router.get("/api/brain/code/reviews")
def api_brain_code_reviews(
    request: Request,
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Recent LLM code reviews."""
    ctx = get_identity_ctx(request, db)
    resolved_repo_id = _resolve_visible_repo_id(db, ctx["user_id"], repo_id)
    if repo_id is not None and resolved_repo_id is None:
        return _not_found("Repo not found")
    reviews = cb_reviewer.get_recent_reviews(
        db,
        repo_id=resolved_repo_id if repo_id is not None else None,
        repo_ids=None if repo_id is not None else _visible_repo_ids(db, ctx["user_id"]),
        limit=20,
    )
    return JSONResponse({"ok": True, "reviews": reviews})


@router.get("/api/brain/code/deps")
def api_brain_code_deps(
    request: Request,
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Dependency health alerts."""
    ctx = get_identity_ctx(request, db)
    resolved_repo_id = _resolve_visible_repo_id(db, ctx["user_id"], repo_id)
    if repo_id is not None and resolved_repo_id is None:
        return _not_found("Repo not found")
    health = cb_deps.get_dep_health(
        db,
        repo_id=resolved_repo_id if repo_id is not None else None,
        repo_ids=None if repo_id is not None else _visible_repo_ids(db, ctx["user_id"]),
    )
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
    ctx = get_identity_ctx(request, db)
    resolved_repo_id = _resolve_visible_repo_id(db, ctx["user_id"], body.repo_id)
    if body.repo_id is not None and resolved_repo_id is None:
        return _not_found("Repo not found")
    # When the caller narrows to a specific repo, pass ``repo_id`` (singular)
    # so the search is scoped to exactly that repo. When no repo was
    # specified, pass the full ``repo_ids`` list — do NOT pass
    # ``resolved_repo_id`` in this branch: ``search_code`` treats ``repo_id``
    # as precedence over ``repo_ids``, so passing both silently narrowed
    # the search to the first visible repo and dropped shared repos.
    if body.repo_id is not None:
        search_repo_id: int | None = resolved_repo_id
        visible_repo_ids: list[int] | None = None
    else:
        search_repo_id = None
        visible_repo_ids = _visible_repo_ids(db, ctx["user_id"])
    if body.use_llm:
        result = cb_search.search_with_llm(
            db,
            body.query,
            repo_id=search_repo_id,
            user_id=ctx["user_id"],
            repo_ids=visible_repo_ids,
        )
    else:
        results = cb_search.search_code(
            db,
            body.query,
            repo_id=search_repo_id,
            repo_ids=visible_repo_ids,
        )
        result = {"query": body.query, "results": results}
    return JSONResponse({"ok": True, **result})


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
    if body.repo_id is not None and _resolve_visible_repo_id(db, ctx["user_id"], body.repo_id) is None:
        return _not_found("Repo not found")
    from ..services.code_brain.agent import run_code_agent

    result = await run_code_agent(db, body.prompt, repo_id=body.repo_id, user_id=ctx["user_id"])
    if "error" in result:
        return JSONResponse({"ok": False, "message": result["error"]}, status_code=400)
    return JSONResponse({"ok": True, **result})


# Project domain: role-based lenses over the shared Code Brain engine


@router.get("/api/brain/project/lenses")
def api_brain_project_lenses():
    """List all available project lenses."""
    return JSONResponse({"ok": True, "lenses": cb_lenses.list_lenses()})


@router.get("/api/brain/project/lens/{lens_name}/metrics")
def api_brain_project_lens_metrics(
    lens_name: str,
    request: Request,
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Metrics filtered through a specific role lens."""
    if not cb_lenses.get_lens(lens_name):
        return JSONResponse({"ok": False, "message": f"Unknown lens: {lens_name}"}, status_code=404)
    ctx = get_identity_ctx(request, db)
    resolved_repo_id = _resolve_visible_repo_id(db, ctx["user_id"], repo_id)
    if repo_id is not None and resolved_repo_id is None:
        return _not_found("Repo not found")
    lens_repo_id = resolved_repo_id if resolved_repo_id is not None else -1
    metrics = cb_lenses.get_lens_metrics(db, lens_name, repo_id=lens_repo_id)
    planner_data = None
    lens_obj = cb_lenses.get_lens(lens_name)
    if lens_obj and lens_obj.planner_integration:
        try:
            from ..services import planner_service

            planner_data = planner_service.get_user_task_summary_stats(db, ctx["user_id"])
        except Exception:
            planner_data = None
    return JSONResponse({"ok": True, **metrics, "planner": planner_data})


@router.get("/api/brain/project/lens/{lens_name}/hotspots")
def api_brain_project_lens_hotspots(
    lens_name: str,
    request: Request,
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Hotspots filtered by lens file patterns."""
    if not cb_lenses.get_lens(lens_name):
        return JSONResponse({"ok": False, "message": f"Unknown lens: {lens_name}"}, status_code=404)
    ctx = get_identity_ctx(request, db)
    resolved_repo_id = _resolve_visible_repo_id(db, ctx["user_id"], repo_id)
    if repo_id is not None and resolved_repo_id is None:
        return _not_found("Repo not found")
    data = cb_lenses.get_lens_hotspots(
        db,
        lens_name,
        repo_id=resolved_repo_id if resolved_repo_id is not None else -1,
    )
    return JSONResponse({"ok": True, "hotspots": data})


@router.get("/api/brain/project/lens/{lens_name}/insights")
def api_brain_project_lens_insights(
    lens_name: str,
    request: Request,
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Insights filtered by lens categories."""
    if not cb_lenses.get_lens(lens_name):
        return JSONResponse({"ok": False, "message": f"Unknown lens: {lens_name}"}, status_code=404)
    ctx = get_identity_ctx(request, db)
    resolved_repo_id = _resolve_visible_repo_id(db, ctx["user_id"], repo_id)
    if repo_id is not None and resolved_repo_id is None:
        return _not_found("Repo not found")
    data = cb_lenses.get_lens_insights(
        db,
        lens_name,
        repo_id=resolved_repo_id if resolved_repo_id is not None else -1,
    )
    return JSONResponse({"ok": True, "insights": data})


# Project Brain: autonomous agents


@router.get("/api/brain/project/agents")
def api_brain_project_agents(request: Request, db: Session = Depends(get_db)):
    """List analysis perspectives while keeping the historical route surface alive."""
    ctx = get_identity_ctx(request, db)
    latest = project_analysis.latest_analysis_snapshot(db, user_id=ctx["user_id"])
    if latest is None:
        agents = [
            {"agent": key, "name": key, "label": label, "active": True}
            for key, label, _lens in project_analysis.PERSPECTIVE_ORDER
        ]
        return JSONResponse({"ok": True, "agents": agents})
    agents = []
    for key, payload in (latest.get("perspectives") or {}).items():
        agents.append(
            {
                "agent": key,
                "name": key,
                "label": payload.get("label", key.title()),
                "active": True,
                "status": payload.get("status"),
                "headline": payload.get("headline"),
            }
        )
    return JSONResponse({"ok": True, "agents": agents})


@router.get("/api/brain/project/agent/{name}/metrics")
def api_brain_project_agent_metrics(name: str, request: Request, db: Session = Depends(get_db)):
    agent = pb_registry.get_agent(name)
    if not agent:
        return _agent_not_found(name)
    ctx = get_identity_ctx(request, db)
    return JSONResponse({"ok": True, **agent.get_metrics(db, ctx["user_id"])})


@router.get("/api/brain/project/agent/{name}/findings")
def api_brain_project_agent_findings(name: str, request: Request, db: Session = Depends(get_db)):
    agent = pb_registry.get_agent(name)
    if not agent:
        return _agent_not_found(name)
    ctx = get_identity_ctx(request, db)
    findings = agent.get_findings(db, ctx["user_id"])
    return JSONResponse(
        {
            "ok": True,
            "findings": [
                {
                    "id": f.id,
                    "category": f.category,
                    "title": f.title,
                    "description": f.description,
                    "severity": f.severity,
                    "status": f.status,
                    "created_at": f.created_at.isoformat() if f.created_at else None,
                }
                for f in findings
            ],
        }
    )


@router.get("/api/brain/project/agent/{name}/goals")
def api_brain_project_agent_goals(name: str, request: Request, db: Session = Depends(get_db)):
    agent = pb_registry.get_agent(name)
    if not agent:
        return _agent_not_found(name)
    ctx = get_identity_ctx(request, db)
    goals = agent.get_goals(db, ctx["user_id"], active_only=False)
    return JSONResponse(
        {
            "ok": True,
            "goals": [
                {
                    "id": g.id,
                    "description": g.description,
                    "goal_type": g.goal_type,
                    "status": g.status,
                    "progress": g.progress,
                    "evidence_count": g.evidence_count,
                    "created_at": g.created_at.isoformat() if g.created_at else None,
                }
                for g in goals
            ],
        }
    )


@router.get("/api/brain/project/agent/{name}/research")
def api_brain_project_agent_research(name: str, request: Request, db: Session = Depends(get_db)):
    agent = pb_registry.get_agent(name)
    if not agent:
        return _agent_not_found(name)
    ctx = get_identity_ctx(request, db)
    research = agent.get_research(db, ctx["user_id"])
    return JSONResponse(
        {
            "ok": True,
            "research": [
                {
                    "id": r.id,
                    "topic": r.topic,
                    "summary": r.summary,
                    "sources_json": r.sources_json,
                    "relevance_score": r.relevance_score,
                    "searched_at": r.searched_at.isoformat() if r.searched_at else None,
                }
                for r in research
            ],
        }
    )


@router.get("/api/brain/project/agent/{name}/evolution")
def api_brain_project_agent_evolution(name: str, request: Request, db: Session = Depends(get_db)):
    agent = pb_registry.get_agent(name)
    if not agent:
        return _agent_not_found(name)
    ctx = get_identity_ctx(request, db)
    evolutions = agent.get_evolution(db, ctx["user_id"])
    return JSONResponse(
        {
            "ok": True,
            "evolution": [
                {
                    "id": e.id,
                    "dimension": e.dimension,
                    "description": e.description,
                    "confidence_before": e.confidence_before,
                    "confidence_after": e.confidence_after,
                    "trigger": e.trigger,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in evolutions
            ],
        }
    )


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
        return _agent_not_found(name)
    status = pb_learning.get_project_brain_status()
    if status.get("running"):
        return JSONResponse({"ok": False, "message": "A cycle is already running"})
    ctx = get_identity_ctx(request, db)

    from ..db import SessionLocal

    def _bg(user_id: int, agent_name: str) -> None:
        sdb = SessionLocal()
        try:
            pb_learning.run_project_brain_cycle(sdb, user_id, agent_name=agent_name)
        finally:
            sdb.close()

    background_tasks.add_task(_bg, ctx["user_id"], name)
    return JSONResponse({"ok": True, "message": f"{agent.label} learning cycle started"})


@router.get("/api/brain/project/messages")
def api_brain_project_messages(request: Request, db: Session = Depends(get_db)):
    """Operator timeline feed (adapter on the historical route)."""
    ctx = get_identity_ctx(request, db)
    return JSONResponse({"ok": True, "messages": _timeline_messages(db, ctx["user_id"])})


class _ProjectAnalysisRunBody(BaseModel):
    planner_task_id: int | None = None


# Product owner endpoints


@router.get("/api/brain/project/agent/product_owner/question")
def api_brain_po_next_question(request: Request, db: Session = Depends(get_db)):
    """Get the next pending PO question for the user."""
    ctx = get_identity_ctx(request, db)
    po = pb_registry.get_agent("product_owner")
    if not po:
        return _agent_not_found("product_owner", label="PO")
    q = po.get_next_question(db, ctx["user_id"])
    if not q:
        return JSONResponse({"ok": True, "question": None})
    return JSONResponse(
        {
            "ok": True,
            "question": {
                "id": q.id,
                "question": q.question,
                "context": q.context,
                "category": q.category,
                "priority": q.priority,
                "options": _parse_json_array(q.options),
            },
        }
    )


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
        return _agent_not_found("product_owner", label="PO")
    questions = po.get_questions(db, ctx["user_id"], status=status)
    return JSONResponse(
        {
            "ok": True,
            "questions": [
                {
                    "id": q.id,
                    "question": q.question,
                    "context": q.context,
                    "category": q.category,
                    "priority": q.priority,
                    "status": q.status,
                    "answer": q.answer,
                    "options": _parse_json_array(q.options),
                    "asked_at": q.asked_at.isoformat() if q.asked_at else None,
                    "answered_at": q.answered_at.isoformat() if q.answered_at else None,
                }
                for q in questions
            ],
        }
    )


class _POAnswerBody(BaseModel):
    answer: str


@router.post("/api/brain/project/agent/product_owner/question/{question_id}/answer")
def api_brain_po_answer(
    question_id: int,
    body: _POAnswerBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Submit an answer to a PO question."""
    ctx = get_identity_ctx(request, db)
    po = pb_registry.get_agent("product_owner")
    if not po:
        return _agent_not_found("product_owner", label="PO")
    q = po.answer_question(db, ctx["user_id"], question_id, body.answer)
    if not q:
        return JSONResponse({"ok": False, "message": "Question not found"}, status_code=404)
    return JSONResponse({"ok": True, "question": {"id": q.id, "status": q.status, "answer": q.answer}})


@router.post("/api/brain/project/agent/product_owner/question/{question_id}/skip")
def api_brain_po_skip(question_id: int, request: Request, db: Session = Depends(get_db)):
    """Skip a PO question."""
    ctx = get_identity_ctx(request, db)
    po = pb_registry.get_agent("product_owner")
    if not po:
        return _agent_not_found("product_owner", label="PO")
    q = po.skip_question(db, ctx["user_id"], question_id)
    if not q:
        return JSONResponse({"ok": False, "message": "Question not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/api/brain/project/agent/product_owner/refresh-options")
def api_brain_po_refresh_options(request: Request, db: Session = Depends(get_db)):
    """Generate options for any pending questions that lack them."""
    ctx = get_identity_ctx(request, db)
    po = pb_registry.get_agent("product_owner")
    if not po:
        return _agent_not_found("product_owner", label="PO")
    context = po._review_context(db, ctx["user_id"])
    upgraded = po._upgrade_optionless_questions(db, ctx["user_id"], context)
    return JSONResponse({"ok": True, "upgraded": upgraded})


@router.get("/api/brain/project/agent/product_owner/requirements")
def api_brain_po_requirements(request: Request, db: Session = Depends(get_db)):
    """List PO-gathered requirements."""
    ctx = get_identity_ctx(request, db)
    po = pb_registry.get_agent("product_owner")
    if not po:
        return _agent_not_found("product_owner", label="PO")
    reqs = po.get_requirements(db, ctx["user_id"])
    return JSONResponse(
        {
            "ok": True,
            "requirements": [
                {
                    "id": r.id,
                    "title": r.title,
                    "description": r.description,
                    "priority": r.priority,
                    "status": r.status,
                    "acceptance_criteria": r.acceptance_criteria,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in reqs
            ],
        }
    )


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
        return _agent_not_found("product_owner", label="PO")
    pid = body.project_id if body else None
    result = po.push_requirement_to_planner(db, ctx["user_id"], requirement_id, project_id=pid)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status_code)


@router.get("/api/brain/project/planner-projects")
def api_brain_planner_projects(request: Request, db: Session = Depends(get_db)):
    """List planner projects visible to the paired user for the project picker."""
    from ..services import planner_service

    ctx = get_identity_ctx(request, db)
    projects = planner_service.list_projects(db, ctx["user_id"])
    return JSONResponse({"ok": True, "projects": projects})


# Project manager endpoints


@router.get("/api/brain/project/agent/project_manager/velocity")
def api_brain_pm_velocity(request: Request, db: Session = Depends(get_db)):
    """Current velocity and project health metrics."""
    ctx = get_identity_ctx(request, db)
    pm = pb_registry.get_agent("project_manager")
    if not pm:
        return _agent_not_found("project_manager", label="PM")
    return JSONResponse({"ok": True, **pm.get_velocity(db, ctx["user_id"])})


@router.get("/api/brain/project/agent/project_manager/health")
def api_brain_pm_health(request: Request, db: Session = Depends(get_db)):
    """Comprehensive project health report."""
    ctx = get_identity_ctx(request, db)
    pm = pb_registry.get_agent("project_manager")
    if not pm:
        return _agent_not_found("project_manager", label="PM")
    health = pm.get_project_health(db, ctx["user_id"])
    health.pop("projects", None)
    return JSONResponse({"ok": True, **health})


@router.get("/api/brain/project/agent/project_manager/breakdown")
def api_brain_pm_breakdown(request: Request, db: Session = Depends(get_db)):
    """Task status breakdown by project."""
    ctx = get_identity_ctx(request, db)
    pm = pb_registry.get_agent("project_manager")
    if not pm:
        return _agent_not_found("project_manager", label="PM")
    return JSONResponse({"ok": True, **pm.get_task_breakdown(db, ctx["user_id"])})


# Architect endpoints


@router.get("/api/brain/project/agent/architect/health")
def api_brain_arch_health(request: Request, db: Session = Depends(get_db)):
    """Comprehensive architecture health report."""
    ctx = get_identity_ctx(request, db)
    arch = pb_registry.get_agent("architect")
    if not arch:
        return _agent_not_found("architect", label="Architect")
    return JSONResponse({"ok": True, **arch.get_architecture_health(db, ctx["user_id"])})


# Global Project Brain endpoints


@router.post("/api/brain/project/cycle")
def api_brain_project_cycle_all(request: Request, db: Session = Depends(get_db)):
    """Historical adapter: trigger a single analysis run for the current operator."""
    ctx = get_identity_ctx(request, db)
    payload = project_analysis.build_analysis_payload(db, user_id=ctx["user_id"])
    summary = payload.get("summary") or {}
    repo_id = summary.get("repo_id")
    run = record_completed_run(
        db,
        "analysis",
        status="completed",
        user_id=ctx["user_id"],
        repo_id=repo_id,
        title="Refresh project analysis",
        detail=summary,
    )
    snapshot = project_analysis.store_analysis_snapshot(
        db,
        user_id=ctx["user_id"],
        planner_task_id=None,
        repo_id=repo_id,
        source_run_id=run.id,
        payload=payload,
    )
    db.commit()
    return JSONResponse({"ok": True, "message": "Project analysis refreshed", "snapshot_id": snapshot.id})


@router.get("/api/brain/project/status")
def api_brain_project_status(request: Request, db: Session = Depends(get_db)):
    """Durable project-domain status backed by persisted run records."""
    ctx = get_identity_ctx(request, db)
    return JSONResponse({"ok": True, **status_payload(db, user_id=ctx["user_id"])})


@router.post("/api/brain/project/analysis/run")
def api_brain_project_analysis_run(
    request: Request,
    body: _ProjectAnalysisRunBody | None = None,
    db: Session = Depends(get_db),
):
    """Run a single advisory report across all perspectives and persist it."""
    ctx = get_identity_ctx(request, db)
    planner_task_id = body.planner_task_id if body else None
    payload = project_analysis.build_analysis_payload(
        db,
        user_id=ctx["user_id"],
        planner_task_id=planner_task_id,
    )
    summary = payload.get("summary") or {}
    repo_id = summary.get("repo_id")
    run = record_completed_run(
        db,
        "analysis",
        status="completed",
        user_id=ctx["user_id"],
        task_id=planner_task_id,
        repo_id=repo_id,
        title="Run project analysis",
        detail=summary,
    )
    snapshot = project_analysis.store_analysis_snapshot(
        db,
        user_id=ctx["user_id"],
        planner_task_id=planner_task_id,
        repo_id=repo_id,
        source_run_id=run.id,
        payload=payload,
    )
    db.commit()
    latest = project_analysis.latest_analysis_snapshot(
        db,
        user_id=ctx["user_id"],
        planner_task_id=planner_task_id,
    )
    return JSONResponse({"ok": True, "snapshot": latest, "snapshot_id": snapshot.id})


@router.get("/api/brain/project/analysis/latest")
def api_brain_project_analysis_latest(
    request: Request,
    db: Session = Depends(get_db),
    planner_task_id: int | None = Query(default=None),
):
    """Return the latest stored analysis snapshot, or build an ephemeral one if none exists yet."""
    ctx = get_identity_ctx(request, db)
    latest = project_analysis.latest_analysis_snapshot(
        db,
        user_id=ctx["user_id"],
        planner_task_id=planner_task_id,
    )
    if latest is None:
        payload = project_analysis.build_analysis_payload(
            db,
            user_id=ctx["user_id"],
            planner_task_id=planner_task_id,
        )
        return JSONResponse({"ok": True, "snapshot": {"status": "ephemeral", **payload}})
    return JSONResponse({"ok": True, "snapshot": latest})


@router.get("/api/brain/project/metrics")
def api_brain_project_metrics(request: Request, db: Session = Depends(get_db)):
    """Aggregate metrics across all Project Brain agents."""
    from ..services.project_brain.learning import get_project_brain_metrics

    ctx = get_identity_ctx(request, db)
    return JSONResponse({"ok": True, **get_project_brain_metrics(db, ctx["user_id"])})


# QA engineer endpoints


@router.get("/api/brain/project/agent/qa/test-cases")
def api_brain_qa_test_cases(request: Request, db: Session = Depends(get_db)):
    """List QA test cases."""
    ctx = get_identity_ctx(request, db)
    qa = pb_registry.get_agent("qa")
    if not qa:
        return _agent_not_found("qa", label="QA")
    cases = qa.get_test_cases(db, ctx["user_id"])
    return JSONResponse(
        {
            "ok": True,
            "test_cases": [
                {
                    "id": c.id,
                    "name": c.name,
                    "priority": c.priority,
                    "status": c.status,
                    "steps": c.steps_json,
                    "expected": c.expected_json,
                    "last_run_at": c.last_run_at.isoformat() if c.last_run_at else None,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in cases
            ],
        }
    )


@router.get("/api/brain/project/agent/qa/test-runs")
def api_brain_qa_test_runs(request: Request, db: Session = Depends(get_db)):
    """List QA test execution history."""
    ctx = get_identity_ctx(request, db)
    qa = pb_registry.get_agent("qa")
    if not qa:
        return _agent_not_found("qa", label="QA")
    runs = qa.get_test_runs(db, ctx["user_id"])
    return JSONResponse(
        {
            "ok": True,
            "test_runs": [
                {
                    "id": r.id,
                    "test_name": r.test_name,
                    "passed": r.passed,
                    "duration_ms": r.duration_ms,
                    "errors": r.errors_json,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in runs
            ],
        }
    )


@router.get("/api/brain/project/agent/qa/bug-reports")
def api_brain_qa_bug_reports(request: Request, db: Session = Depends(get_db)):
    """List QA bug reports."""
    ctx = get_identity_ctx(request, db)
    qa = pb_registry.get_agent("qa")
    if not qa:
        return _agent_not_found("qa", label="QA")
    bugs = qa.get_bug_reports(db, ctx["user_id"])
    return JSONResponse(
        {
            "ok": True,
            "bug_reports": [
                {
                    "id": b.id,
                    "title": b.title,
                    "description": b.description,
                    "severity": b.severity,
                    "status": b.status,
                    "reproduction_steps": b.reproduction_steps,
                    "created_at": b.created_at.isoformat() if b.created_at else None,
                }
                for b in bugs
            ],
        }
    )
