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
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import get_db, get_identity_ctx, require_project_domain_enabled
from ..schemas.trading import TradingBrainAssistantChatResponse
from ..services import project_domain_service
from ..services.coding_task.telemetry import log_event as _log_coding_event
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

_ALLOWED_BRAIN_DOMAINS = frozenset({"hub", "trading", "project", "reasoning", "context"})


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
    if norm in ("trading", "project", "reasoning", "context", "hub"):
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

    project_domain_enabled = bool(settings.project_domain_enabled)
    if brain_initial_domain == "project" and not project_domain_enabled:
        brain_initial_domain = "hub"

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
            "project_domain_enabled": project_domain_enabled,
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
    domains: list[dict] = [
        {
            "id": "trading",
            "label": "Trading",
            "icon": "\U0001f4c8",
            "description": "Patterns, backtests, learning cycles, and desk metrics for your watchlists.",
            "status": "learning" if trading_st.get("running") else "idle",
            "last_run": trading_st.get("last_run"),
            "phase": trading_st.get("phase", "idle"),
        },
    ]
    if settings.project_domain_enabled:
        domains.append(
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
            }
        )
    domains.extend([
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
    ])
    # Phase F — Context Brain. Adaptive LLM-context assembly: TurboQuant-style
    # retrieve→rank→budget→compose pipeline that learns which sources matter
    # for which intents. The status reflects whether the learning cycle is
    # active or the brain is idle/paused.
    context_st = _context_brain_status(db)
    domains.append(
        {
            "id": "context",
            "label": "Context",
            "icon": "\U0001f9e9",
            "description": (
                "Adaptive context for every chat: classifies intent, retrieves from "
                "9 sources in parallel, scores with learned weights, budgets, and "
                "composes a structured prompt. Learns from outcomes."
            ),
            "status": context_st.get("status", "idle"),
            "last_run": context_st.get("last_learning_cycle_at"),
            "phase": context_st.get("mode", "idle"),
        }
    )
    return JSONResponse({"ok": True, "domains": domains})


def _context_brain_status(db: Session) -> dict:
    """Read from context_brain_runtime_state without raising. Returns
    a dict with ``status``, ``mode``, ``last_learning_cycle_at``.
    """
    try:
        from sqlalchemy import text as _t
        row = db.execute(_t(
            "SELECT mode, learning_enabled, last_learning_cycle_at "
            "FROM context_brain_runtime_state WHERE id = 1"
        )).fetchone()
        if not row:
            return {"status": "idle", "mode": "unknown", "last_learning_cycle_at": None}
        mode = str(row[0] or "idle")
        learning = bool(row[1])
        last = row[2].isoformat() if row[2] is not None else None
        # We surface "learning" green dot only when the brain is actively
        # accumulating into the next learning cycle (not paused, learning enabled).
        active = (mode in ("reactive", "learning")) and learning
        return {
            "status": "learning" if active else "idle",
            "mode": mode,
            "last_learning_cycle_at": last,
        }
    except Exception:
        return {"status": "idle", "mode": "unknown", "last_learning_cycle_at": None}


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
    _: None = Depends(require_project_domain_enabled),
):
    """Workspace-first bootstrap payload for the Project domain."""
    ctx = get_identity_ctx(request, db)
    import time as _t
    _start = _t.monotonic()
    payload = project_domain_service.build_project_bootstrap_payload(
        db,
        user_id=ctx["user_id"],
        is_guest=bool(ctx["is_guest"]),
        planner_task_id=planner_task_id,
    )
    _duration_ms = int((_t.monotonic() - _start) * 1000)
    _log_coding_event(
        "bootstrap",
        "ok",
        duration_ms=_duration_ms,
        user_id=ctx["user_id"],
        task_id=planner_task_id,
        is_guest=bool(ctx["is_guest"]),
        workspace_bound=bool(
            (payload.get("planner_handoff") or {}).get("summary", {}).get("profile", {}).get("workspace_bound")
        ) if planner_task_id else None,
    )
    return JSONResponse({"ok": True, **payload})


# ── Trading domain: metrics ────────────────────────────────────────────

@router.get("/api/brain/trading/metrics")
def api_brain_trading_metrics(request: Request, db: Session = Depends(get_db)):
    """Aggregate trading brain metrics (KPIs, patterns, predictions)."""
    ctx = get_identity_ctx(request, db)
    stats = ts.get_brain_stats(db, ctx["user_id"])
    return JSONResponse({"ok": True, **stats})


@router.get("/api/brain/cpcv_shadow_funnel")
def api_brain_cpcv_shadow_funnel(db: Session = Depends(get_db)):
    """7-day CPCV shadow funnel rollup per scanner (``cpcv_shadow_funnel_v``)."""
    try:
        result = db.execute(
            text("SELECT * FROM cpcv_shadow_funnel_v ORDER BY scanner")
        )
        rows = [dict(row._mapping) for row in result]
    except Exception as exc:
        logger.debug("[brain] cpcv_shadow_funnel unavailable: %s", exc)
        return JSONResponse({"ok": True, "rows": [], "view_available": False})
    return JSONResponse({"ok": True, "rows": rows, "view_available": True})


@router.get("/api/brain/portfolio_sizing")
def api_brain_portfolio_sizing(
    db: Session = Depends(get_db),
    limit: int = 50,
):
    """Q1.T5 — recent portfolio sizing decisions, naive vs HRP.

    Operator compares the two columns to assess whether HRP is producing
    meaningfully different sizing than the naive 2%-per-trade default.
    """
    try:
        result = db.execute(
            text(
                """
                SELECT id, symbol, decision_at, account_equity_usd,
                       naive_size_usd, hrp_size_usd, hrp_weight,
                       chosen_sizing, n_active_positions,
                       cov_condition_number, meta
                FROM portfolio_sizing_log
                ORDER BY decision_at DESC
                LIMIT :lim
                """
            ),
            {"lim": int(limit)},
        )
        rows = [dict(r._mapping) for r in result]
        agg = db.execute(
            text(
                """
                SELECT
                  COUNT(*) AS n_total,
                  COUNT(*) FILTER (WHERE chosen_sizing = 'hrp')   AS n_hrp,
                  COUNT(*) FILTER (WHERE chosen_sizing = 'naive') AS n_naive,
                  ROUND(AVG(hrp_size_usd / NULLIF(naive_size_usd, 0))::numeric, 4) AS avg_hrp_naive_ratio
                FROM portfolio_sizing_log
                WHERE decision_at >= NOW() - INTERVAL '7 days'
                """
            )
        ).fetchone()
        agg_dict = dict(agg._mapping) if agg else {}
        return JSONResponse({
            "ok": True,
            "rows": rows,
            "count": len(rows),
            "last_7d": agg_dict,
        })
    except Exception as exc:
        logger.debug("[brain] portfolio_sizing unavailable: %s", exc)
        return JSONResponse({"ok": False, "rows": [], "error": str(exc)[:200]})


@router.get("/api/brain/strategy_parameters")
def api_brain_strategy_parameters(
    db: Session = Depends(get_db),
    family: str = "",
):
    """Q1.T4 — list strategy parameters with current values + recent posterior."""
    try:
        clauses = ["1=1"]
        params: dict[str, Any] = {}
        if family:
            clauses.append("strategy_family = :f")
            params["f"] = family
        result = db.execute(
            text(
                "SELECT id, strategy_family, parameter_key, scope, scope_value, "
                "current_value, initial_value, min_value, max_value, "
                "param_type, locked, learning_state, description, updated_at "
                "FROM strategy_parameter WHERE " + " AND ".join(clauses) +
                " ORDER BY strategy_family, parameter_key"
            ),
            params,
        )
        rows = [dict(r._mapping) for r in result]
        return JSONResponse({"ok": True, "rows": rows, "count": len(rows)})
    except Exception as exc:
        logger.debug("[brain] strategy_parameters unavailable: %s", exc)
        return JSONResponse({"ok": False, "rows": [], "error": str(exc)[:200]})


@router.get("/api/brain/strategy_parameter_proposals")
def api_brain_strategy_parameter_proposals(
    db: Session = Depends(get_db),
    status: str = "pending",
):
    """Q1.T4 — list pending (or by status) parameter proposals."""
    try:
        result = db.execute(
            text(
                """
                SELECT pp.id, pp.parameter_id, p.strategy_family, p.parameter_key,
                       pp.current_value, pp.proposed_value, pp.confidence,
                       pp.sample_count, pp.justification, pp.severity,
                       pp.status, pp.decided_by, pp.decided_at, pp.created_at
                FROM strategy_parameter_proposal pp
                JOIN strategy_parameter p ON p.id = pp.parameter_id
                WHERE pp.status = :s
                ORDER BY pp.created_at DESC
                LIMIT 100
                """
            ),
            {"s": status},
        )
        rows = [dict(r._mapping) for r in result]
        return JSONResponse({"ok": True, "rows": rows, "count": len(rows)})
    except Exception as exc:
        logger.debug("[brain] strategy_parameter_proposals unavailable: %s", exc)
        return JSONResponse({"ok": False, "rows": [], "error": str(exc)[:200]})


@router.get("/api/brain/cpcv/readiness")
def api_brain_cpcv_readiness(db: Session = Depends(get_db)):
    """Q1.T1 flag-flip readiness check.

    Reports whether the operator-review criteria for flipping
    ``CHILI_CPCV_PROMOTION_GATE_ENABLED`` to ``True`` are met. Criteria
    (per ``docs/CPCV_PROMOTION_GATE_RUNBOOK.md``):

      1. ≥5 patterns evaluated under realized-PnL CPCV (``cpcv_n_paths IS NOT NULL``).
      2. Zero patterns demoted on a single procedural-count threshold
         (covers e.g. ``cpcv_n_paths_below_provisional_min`` regressions).
      3. Per-scanner demote distribution healthy (no scanner accounts for
         > 60 % of demotes among evaluated patterns).

    The endpoint is read-only. Operator decides whether to flip; this just
    reports whether criteria are met.
    """
    payload: dict[str, Any] = {
        "ok": True,
        "flag_currently": bool(
            getattr(settings, "chili_cpcv_promotion_gate_enabled", False)
        ),
        "min_evaluated_required": 5,
        "criteria": {},
        "ready": False,
        "blockers": [],
    }
    try:
        agg = db.execute(
            text(
                """
                SELECT
                  COUNT(*) FILTER (WHERE cpcv_n_paths IS NOT NULL)            AS evaluated,
                  COUNT(*) FILTER (WHERE promotion_gate_passed IS TRUE)       AS gate_passed,
                  COUNT(*) FILTER (WHERE promotion_gate_passed IS FALSE
                                   AND cpcv_n_paths IS NOT NULL)              AS gate_failed,
                  COUNT(*) FILTER (
                    WHERE promotion_gate_passed IS FALSE
                      AND cpcv_n_paths IS NOT NULL
                      AND (promotion_gate_reasons::text LIKE '%n_paths_below%'
                        OR promotion_gate_reasons::text LIKE '%cv_infeasible_for_sample%'
                        OR promotion_gate_reasons::text LIKE '%n_trades_below%')
                  )                                                            AS procedural_failures,
                  COUNT(*)                                                     AS total_promoted_or_live
                FROM scan_patterns
                WHERE lifecycle_stage IN ('promoted', 'live', 'challenged')
                """
            )
        ).fetchone()
        if agg:
            evaluated = int(agg[0] or 0)
            gate_passed = int(agg[1] or 0)
            gate_failed = int(agg[2] or 0)
            procedural = int(agg[3] or 0)
            total = int(agg[4] or 0)

            payload["criteria"]["evaluated"] = evaluated
            payload["criteria"]["gate_passed"] = gate_passed
            payload["criteria"]["gate_failed"] = gate_failed
            payload["criteria"]["procedural_failures"] = procedural
            payload["criteria"]["total_in_lifecycle"] = total

            # Criterion 1: ≥5 evaluated
            if evaluated < 5:
                payload["blockers"].append(
                    f"insufficient_evaluated_patterns ({evaluated} < 5) — "
                    "weekly CPCV backfill accumulates evidence as trade history grows"
                )
            # Criterion 2: zero procedural-count failures
            if procedural > 0:
                payload["blockers"].append(
                    f"procedural_count_failures ({procedural}) — "
                    "indicates a sample-size threshold miscalibration, not a real-edge failure"
                )
            # Criterion 3: per-scanner skew (only check when evaluated >= 5)
            if evaluated >= 5 and gate_failed > 0:
                rows = db.execute(
                    text(
                        """
                        SELECT scanner_bucket, COUNT(*) AS n
                        FROM (
                          SELECT
                            CASE
                              WHEN name ILIKE '%momentum%'           THEN 'momentum'
                              WHEN name ILIKE '%breakout%'           THEN 'breakout'
                              WHEN name ILIKE '%day%'
                                OR timeframe IN ('1m','5m','15m')    THEN 'day'
                              WHEN name ILIKE '%swing%'
                                OR timeframe IN ('1h','4h','1d')     THEN 'swing'
                              ELSE                                        'patterns'
                            END AS scanner_bucket
                          FROM scan_patterns
                          WHERE promotion_gate_passed IS FALSE
                            AND cpcv_n_paths IS NOT NULL
                        ) sub
                        GROUP BY scanner_bucket
                        ORDER BY n DESC
                        """
                    )
                ).fetchall()
                by_scanner = [
                    {"scanner": r[0], "n_failed": int(r[1])} for r in rows or []
                ]
                payload["criteria"]["failures_by_scanner"] = by_scanner
                if by_scanner:
                    top = by_scanner[0]
                    if top["n_failed"] / max(gate_failed, 1) > 0.60:
                        payload["blockers"].append(
                            f"scanner_demote_skew ({top['scanner']} = "
                            f"{top['n_failed']}/{gate_failed} failures > 60%) — "
                            "investigate per-scanner calibration before flipping"
                        )

            payload["ready"] = len(payload["blockers"]) == 0 and evaluated >= 5

        if payload["ready"]:
            payload["recommendation"] = (
                "Criteria met. Operator may flip CHILI_CPCV_PROMOTION_GATE_ENABLED=true "
                "in a maintenance window. Begin the 14-day shadow-to-momentum-only-enforce "
                "calendar from CPCV_PROMOTION_GATE_RUNBOOK.md."
            )
        else:
            payload["recommendation"] = (
                "Criteria not yet met. Keep flag OFF; weekly CPCV backfill is "
                "accumulating evidence. Re-check when blockers clear."
            )
    except Exception as exc:
        logger.debug("[brain] cpcv readiness query failed: %s", exc)
        payload["ok"] = False
        payload["error"] = str(exc)[:200]
    return JSONResponse(payload)


@router.get("/api/brain/regime_sharpe_heatmap")
def api_brain_regime_sharpe_heatmap(db: Session = Depends(get_db)):
    """30d Sharpe by HMM regime × scanner (closed trades); needs migration 165 + flag optional."""
    if not getattr(settings, "chili_regime_classifier_enabled", False):
        return JSONResponse(
            {
                "ok": False,
                "reason": "flag_off",
                "message": "Regime classifier not yet enabled",
            }
        )
    try:
        from ..services.trading.regime_classifier import build_regime_scanner_sharpe_heatmap

        payload = build_regime_scanner_sharpe_heatmap(db)
    except Exception as exc:
        logger.debug("[brain] regime_sharpe_heatmap unavailable: %s", exc)
        return JSONResponse(
            {
                "ok": False,
                "reason": "schema_or_error",
                "message": "Regime heatmap not available yet (apply migration 165_regime_snapshot_and_tagging).",
            }
        )
    return JSONResponse(payload)


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


