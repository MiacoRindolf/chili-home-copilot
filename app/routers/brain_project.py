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

from ..deps import get_db, get_identity_ctx, require_project_domain_enabled
from ..services.code_brain import deps_scanner as cb_deps
from ..services.code_brain import graph as cb_graph
from ..services.code_brain import indexer as cb_indexer
from ..services.code_brain import insights as cb_insights
from ..services.code_brain import learning as cb_learning
from ..services.code_brain import lenses as cb_lenses
from ..services.code_brain import reviewer as cb_reviewer
from ..services.code_brain import search as cb_search
from ..services.code_brain import trends as cb_trends
from ..services.project_brain import learning as pb_learning
from ..services.project_brain import registry as pb_registry

router = APIRouter(
    tags=["brain"],
    dependencies=[Depends(require_project_domain_enabled)],
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


def _first_active_repo_id(db: Session) -> int | None:
    from ..models.code_brain import CodeRepo

    first = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).first()
    return first.id if first else None


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
def api_brain_code_hotspots(db: Session = Depends(get_db)):
    """Top files by churn x complexity across all repos."""
    from ..models.code_brain import CodeHotspot

    hotspots = (
        db.query(CodeHotspot)
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


@router.delete("/api/brain/code/repos/{repo_id}")
def api_brain_code_remove_repo(repo_id: int, db: Session = Depends(get_db)):
    """Deactivate a repo."""
    result = cb_indexer.unregister_repo(db, repo_id)
    if "error" in result:
        return JSONResponse({"ok": False, "message": result["error"]}, status_code=404)
    return JSONResponse({"ok": True})


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
        result = cb_search.search_with_llm(
            db,
            body.query,
            repo_id=body.repo_id,
            user_id=ctx["user_id"],
        )
    else:
        results = cb_search.search_code(db, body.query, repo_id=body.repo_id)
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
    repo_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Metrics filtered through a specific role lens."""
    if not cb_lenses.get_lens(lens_name):
        return JSONResponse({"ok": False, "message": f"Unknown lens: {lens_name}"}, status_code=404)
    resolved_repo_id = repo_id if repo_id is not None else _first_active_repo_id(db)
    metrics = cb_lenses.get_lens_metrics(db, lens_name, repo_id=resolved_repo_id)
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


# Project Brain: autonomous agents


@router.get("/api/brain/project/agents")
def api_brain_project_agents(request: Request, db: Session = Depends(get_db)):
    """List all agents with their status and metrics."""
    ctx = get_identity_ctx(request, db)
    agents = [agent.get_metrics(db, ctx["user_id"]) for agent in pb_registry.AGENT_REGISTRY.values()]
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
    """Inter-agent message feed."""
    ctx = get_identity_ctx(request, db)
    feed = pb_registry.get_message_feed(db, ctx["user_id"])
    return JSONResponse({"ok": True, "messages": feed})


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
def api_brain_po_answer(question_id: int, body: _POAnswerBody, db: Session = Depends(get_db)):
    """Submit an answer to a PO question."""
    po = pb_registry.get_agent("product_owner")
    if not po:
        return _agent_not_found("product_owner", label="PO")
    q = po.answer_question(db, question_id, body.answer)
    if not q:
        return JSONResponse({"ok": False, "message": "Question not found"}, status_code=404)
    return JSONResponse({"ok": True, "question": {"id": q.id, "status": q.status, "answer": q.answer}})


@router.post("/api/brain/project/agent/product_owner/question/{question_id}/skip")
def api_brain_po_skip(question_id: int, db: Session = Depends(get_db)):
    """Skip a PO question."""
    po = pb_registry.get_agent("product_owner")
    if not po:
        return _agent_not_found("product_owner", label="PO")
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
def api_brain_planner_projects(db: Session = Depends(get_db)):
    """List all planner projects in the household for the project picker."""
    from ..services import planner_service

    projects = planner_service.list_all_projects(db)
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
    """Trigger a learning cycle for all active agents."""
    from ..db import SessionLocal
    from ..services.project_brain.learning import run_project_brain_cycle_background

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
