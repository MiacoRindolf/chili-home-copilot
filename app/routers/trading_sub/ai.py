"""AI Brain / learning endpoints for the trading module."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, cast

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func as sa_func, or_, text
from sqlalchemy.orm import Session

from ...db import DATA_DIR
from ...deps import get_db, get_identity_ctx
from ...json_safe import to_jsonable
from ...models.trading import LearningCycleAiReport
from ...logger import log_info, new_trace_id
from ...prompts import load_prompt
from ...services import trading_service as ts
from ...services import trading_scheduler
from ...services import ticker_universe
from ...services.trading.scan_pattern_label_alignment import (
    strategy_label_aligns_scan_pattern_name,
)
from ...services.trading.backtest_metrics import (
    backtest_win_rate_db_to_display_pct,
    normalize_win_rate_for_db,
)

router = APIRouter(tags=["trading-ai"])
_log = logging.getLogger(__name__)

# Sibling-linked BacktestResult rows only (no global keyword padding). Keep panel + chart pools aligned.
_EVIDENCE_LINKED_BACKTEST_LIMIT = 4000


def _research_kpi_summary_for_scan_patterns(
    db: Session,
    scan_pattern_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """Mean KPIs across recent BacktestResult rows per ScanPattern (from stored params.kpis)."""
    from collections import defaultdict

    from ...models.trading import BacktestResult
    from ...services.trading.research_kpis import aggregate_kpis_from_params_rows

    if not scan_pattern_ids:
        return {}
    cap = min(2000, max(200, len(scan_pattern_ids) * 60))
    rows = (
        db.query(BacktestResult.scan_pattern_id, BacktestResult.params)
        .filter(
            BacktestResult.scan_pattern_id.in_(scan_pattern_ids),
            BacktestResult.trade_count > 0,
        )
        .order_by(BacktestResult.ran_at.desc())
        .limit(cap)
        .all()
    )
    by_sp: dict[int, list[str | None]] = defaultdict(list)
    for spid, params in rows:
        if spid is None:
            continue
        by_sp[int(spid)].append(params)
    out: dict[int, dict[str, Any]] = {}
    for spid in scan_pattern_ids:
        agg = aggregate_kpis_from_params_rows(by_sp.get(spid, []), max_samples=60)
        if agg.get("sample_count", 0):
            out[spid] = agg
    return out


_TRADING_PROMPT: str | None = None
_TRADING_PROMPT_MTIME: float = 0.0


def _get_trading_prompt() -> str:
    """Load trading analyst prompt, auto-reloading when the file changes."""
    global _TRADING_PROMPT, _TRADING_PROMPT_MTIME
    from pathlib import Path
    prompt_path = Path(__file__).resolve().parent.parent.parent / "prompts" / "trading_analyst.txt"
    try:
        current_mtime = prompt_path.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    if _TRADING_PROMPT is None or current_mtime != _TRADING_PROMPT_MTIME:
        _TRADING_PROMPT = load_prompt("trading_analyst")
        _TRADING_PROMPT_MTIME = current_mtime
    return _TRADING_PROMPT


# ── AI Analysis ────────────────────────────────────────────────────────


class _NeuralMeshPublishBody(BaseModel):
    source_node_id: str | None = None
    cause: str = "manual_debug"
    confidence_delta: float = Field(0.25, ge=-1.0, le=1.0)
    signal_type: str = "snapshot_refresh"


@router.get("/api/trading/brain/graph/config")
def api_trading_brain_graph_config(request: Request, db: Session = Depends(get_db)):
    get_identity_ctx(request, db)
    from ...services.trading.brain_neural_mesh.schema import desk_graph_boot_config

    body = {"ok": True, **desk_graph_boot_config()}
    return JSONResponse(body)


@router.get("/api/trading/brain/graph")
def api_trading_brain_graph(
    request: Request,
    db: Session = Depends(get_db),
):
    get_identity_ctx(request, db)
    from ...services.trading.brain_neural_mesh.projection import build_neural_graph_projection

    return JSONResponse(build_neural_graph_projection(db))


# ── Neural momentum desk (Phase 10 — read-only; not learning-cycle) ─────────


@router.get("/api/trading/brain/momentum/desk")
def api_trading_brain_momentum_desk(request: Request, db: Session = Depends(get_db)):
    get_identity_ctx(request, db)
    from ...services.trading.momentum_neural.brain_desk_summary import get_momentum_brain_desk_payload

    return JSONResponse(get_momentum_brain_desk_payload(db))


@router.get("/api/trading/brain/momentum/summary")
def api_trading_brain_momentum_summary(request: Request, db: Session = Depends(get_db)):
    """Alias of desk payload for brain UX consumers."""
    get_identity_ctx(request, db)
    from ...services.trading.momentum_neural.brain_desk_summary import get_momentum_brain_desk_payload

    return JSONResponse(get_momentum_brain_desk_payload(db))


@router.get("/api/trading/brain/momentum/variants")
def api_trading_brain_momentum_variants(
    request: Request,
    db: Session = Depends(get_db),
    days: int = Query(14, ge=1, le=90),
):
    get_identity_ctx(request, db)
    from ...services.trading.momentum_neural.brain_desk_summary import get_momentum_variants_brain_summary

    return JSONResponse(get_momentum_variants_brain_summary(db, days=days))


@router.get("/api/trading/brain/momentum/feedback-summary")
def api_trading_brain_momentum_feedback_summary(request: Request, db: Session = Depends(get_db)):
    get_identity_ctx(request, db)
    from ...services.trading.momentum_neural.brain_desk_summary import get_momentum_feedback_brain_summary

    return JSONResponse(get_momentum_feedback_brain_summary(db))


@router.get("/api/trading/brain/momentum/evolution-trace")
def api_trading_brain_momentum_evolution_trace(request: Request, db: Session = Depends(get_db)):
    get_identity_ctx(request, db)
    from ...services.trading.momentum_neural.brain_desk_summary import get_momentum_evolution_trace_summary

    return JSONResponse(get_momentum_evolution_trace_summary(db))


@router.get("/api/trading/brain/graph/nodes/{node_id}")
def api_trading_brain_graph_node(request: Request, node_id: str, db: Session = Depends(get_db)):
    get_identity_ctx(request, db)
    from ...services.trading.brain_neural_mesh.projection import build_node_detail

    detail = build_node_detail(db, node_id)
    if not detail:
        raise HTTPException(status_code=404, detail="node_not_found")
    return JSONResponse({"ok": True, "node": detail})


@router.get("/api/trading/brain/graph/edges/{edge_id}")
def api_trading_brain_graph_edge(request: Request, edge_id: int, db: Session = Depends(get_db)):
    get_identity_ctx(request, db)
    from ...services.trading.brain_neural_mesh.projection import build_edge_detail

    detail = build_edge_detail(db, edge_id)
    if not detail:
        raise HTTPException(status_code=404, detail="edge_not_found")
    return JSONResponse({"ok": True, "edge": detail})


@router.get("/api/trading/brain/graph/activations")
def api_trading_brain_graph_activations(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(40, ge=1, le=200),
    since_iso: str | None = Query(None, description="UTC ISO timestamp filter"),
):
    get_identity_ctx(request, db)
    from datetime import datetime

    from ...services.trading.brain_neural_mesh.projection import list_recent_activations

    since_dt = None
    if since_iso:
        try:
            since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        except Exception:
            since_dt = None
    rows = list_recent_activations(db, limit=limit, since=since_dt)
    from ...services.trading.brain_neural_mesh.waves import group_activation_events_into_waves

    waves = group_activation_events_into_waves(rows, time_window_sec=2.0)
    return JSONResponse({"ok": True, "activations": rows, "waves": waves})


@router.get("/api/trading/brain/graph/live-overlay")
def api_trading_brain_graph_live_overlay(
    request: Request,
    db: Session = Depends(get_db),
    activation_limit: int = Query(80, ge=1, le=300),
    time_window_sec: float = Query(2.0, ge=0.5, le=10.0),
):
    get_identity_ctx(request, db)
    from ...services.trading.brain_neural_mesh.projection import build_live_activation_overlay
    from ...services.trading.brain_neural_mesh.schema import mesh_enabled

    if not mesh_enabled():
        return JSONResponse(
            {"ok": False, "error": "neural_mesh_disabled", "hint": "Set TRADING_BRAIN_NEURAL_MESH_ENABLED=1"},
            status_code=403,
        )
    return JSONResponse(
        build_live_activation_overlay(
            db,
            activation_limit=activation_limit,
            time_window_sec=time_window_sec,
        )
    )


@router.get("/api/trading/brain/graph/metrics")
def api_trading_brain_graph_metrics(request: Request, db: Session = Depends(get_db)):
    get_identity_ctx(request, db)
    from ...services.trading.brain_neural_mesh.metrics import get_counters, read_metrics_map

    return JSONResponse(
        {
            "ok": True,
            "db_metrics": read_metrics_map(db),
            "session_counters": {
                "events_published": get_counters().events_published,
                "events_processed": get_counters().events_processed,
                "node_fires": get_counters().node_fires,
                "suppressions": get_counters().suppressions,
                "inhibitions": get_counters().inhibitions,
            },
        }
    )


@router.post("/api/trading/brain/graph/publish")
def api_trading_brain_graph_publish(
    request: Request,
    db: Session = Depends(get_db),
    body: _NeuralMeshPublishBody | None = Body(default=None),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        raise HTTPException(status_code=403, detail="guest_forbidden")
    from ...services.trading.brain_neural_mesh.metrics import get_counters
    from ...services.trading.brain_neural_mesh.repository import enqueue_activation
    from ...services.trading.brain_neural_mesh.schema import mesh_enabled

    if not mesh_enabled():
        raise HTTPException(status_code=403, detail="neural_mesh_disabled")
    b = body or _NeuralMeshPublishBody()
    import uuid

    cid = str(uuid.uuid4())
    eid = enqueue_activation(
        db,
        source_node_id=b.source_node_id,
        cause=b.cause,
        payload={"signal_type": b.signal_type},
        confidence_delta=b.confidence_delta,
        propagation_depth=0,
        correlation_id=cid,
    )
    get_counters().note_publish(1)
    db.commit()
    return JSONResponse({"ok": True, "event_id": eid, "correlation_id": cid})


@router.post("/api/trading/brain/graph/propagate")
def api_trading_brain_graph_propagate(
    request: Request,
    db: Session = Depends(get_db),
    dry_run: int = Query(0, ge=0, le=1),
    source_node_id: str | None = Query(None),
    confidence_delta: float = Query(0.2),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        raise HTTPException(status_code=403, detail="guest_forbidden")
    from ...services.trading.brain_neural_mesh.activation_runner import (
        run_activation_batch,
        run_propagation_dry_run,
    )
    from ...services.trading.brain_neural_mesh.schema import mesh_enabled

    if not mesh_enabled():
        raise HTTPException(status_code=403, detail="neural_mesh_disabled")
    if dry_run:
        out: dict = {}
        try:
            with db.begin_nested():
                import uuid

                out = run_propagation_dry_run(
                    db,
                    source_node_id=source_node_id,
                    confidence_delta=confidence_delta,
                    propagation_depth=0,
                    correlation_id=str(uuid.uuid4()),
                    payload={"signal_type": "*"},
                )
                raise RuntimeError("__dry_run_rollback__")
        except RuntimeError as e:
            if str(e) != "__dry_run_rollback__":
                raise
        return JSONResponse({"ok": True, "dry_run": True, "simulated": out})
    summary = run_activation_batch(db, time_budget_sec=3.0, max_events=16)
    db.commit()
    return JSONResponse({"ok": True, "dry_run": False, **summary})


@router.get("/api/trading/brain/stats")
def api_brain_stats(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    stats = ts.get_brain_stats(db, ctx["user_id"])
    return JSONResponse({"ok": True, **stats})


@router.get("/api/trading/brain/performance")
def api_brain_performance(request: Request, db: Session = Depends(get_db)):
    """Comprehensive P&L performance dashboard data."""
    ctx = get_identity_ctx(request, db)
    from ...services.trading.portfolio import get_performance_dashboard
    data = get_performance_dashboard(db, ctx["user_id"])
    return JSONResponse(data)


@router.get("/api/trading/dashboard/overview")
def api_trading_dashboard_overview(request: Request, db: Session = Depends(get_db)):
    """Consolidated trading dashboard: performance, equity curve, risk, regime, execution quality."""
    ctx = get_identity_ctx(request, db)
    from ...services.trading.portfolio import get_trading_dashboard_overview
    data = get_trading_dashboard_overview(db, ctx["user_id"])
    return JSONResponse(data)


@router.get("/api/trading/opportunity-board")
def api_trading_opportunity_board(
    request: Request,
    db: Session = Depends(get_db),
    debug: int = Query(0, ge=0, le=1),
    include_research: int = Query(0, ge=0, le=1),
    max_tier_a: int | None = Query(None, ge=1, le=100),
    max_tier_b: int | None = Query(None, ge=1, le=100),
    max_tier_c: int | None = Query(None, ge=1, le=100),
    max_tier_d: int | None = Query(None, ge=1, le=100),
):
    """Tiered manual-trading opportunity surface (shared scoring with imminent alerts)."""
    ctx = get_identity_ctx(request, db)
    from ...services.trading.opportunity_board import get_trading_opportunity_board

    caps: dict[str, int] = {}
    if max_tier_a is not None:
        caps["A"] = max_tier_a
    if max_tier_b is not None:
        caps["B"] = max_tier_b
    if max_tier_c is not None:
        caps["C"] = max_tier_c
    if max_tier_d is not None:
        caps["D"] = max_tier_d

    try:
        data = get_trading_opportunity_board(
            db,
            ctx["user_id"],
            include_research=bool(include_research),
            include_debug=(debug == 1),
            max_per_tier=caps or None,
        )
        return JSONResponse(to_jsonable(data))
    except Exception as e:
        _log.exception("[opportunity-board] failed: %s", e)
        return JSONResponse(
            to_jsonable(
                {
                    "ok": False,
                    "error": "opportunity_board_failed",
                    "message": "Opportunity board could not be computed. Check server logs.",
                }
            ),
            status_code=500,
        )


@router.get("/api/trading/brain/data-health")
def api_data_health(db: Session = Depends(get_db)):
    """Diagnostic endpoint showing DB integrity metrics."""
    checks: dict[str, int] = {}
    try:
        checks["orphan_backtests"] = db.execute(
            text(
                "SELECT COUNT(*) FROM trading_backtests "
                "WHERE scan_pattern_id IS NOT NULL "
                "AND scan_pattern_id NOT IN (SELECT id FROM scan_patterns)"
            )
        ).scalar() or 0
    except Exception:
        checks["orphan_backtests"] = -1

    try:
        checks["win_rate_over_1"] = db.execute(
            text("SELECT COUNT(*) FROM scan_patterns WHERE win_rate > 1")
        ).scalar() or 0
    except Exception:
        checks["win_rate_over_1"] = -1

    try:
        checks["stuck_jobs"] = db.execute(
            text(
                "SELECT COUNT(*) FROM brain_batch_jobs "
                "WHERE status = 'running' AND started_at < NOW() - INTERVAL '2 hours'"
            )
        ).scalar() or 0
    except Exception:
        checks["stuck_jobs"] = -1

    try:
        row = db.execute(
            text(
                "SELECT "
                "  COUNT(*) FILTER (WHERE active = true) AS active_patterns, "
                "  COUNT(*) FILTER (WHERE lifecycle_stage = 'promoted') AS promoted, "
                "  COUNT(*) FILTER (WHERE lifecycle_stage = 'backtested') AS backtested, "
                "  COUNT(*) AS total "
                "FROM scan_patterns"
            )
        ).fetchone()
        checks["active_patterns"] = int(row[0]) if row and row[0] is not None else 0
        checks["promoted_patterns"] = int(row[1]) if row and row[1] is not None else 0
        checks["backtested_patterns"] = int(row[2]) if row and row[2] is not None else 0
        checks["total_patterns"] = int(row[3]) if row and row[3] is not None else 0
    except Exception:
        checks["active_patterns"] = -1
        checks["promoted_patterns"] = -1
        checks["backtested_patterns"] = -1
        checks["total_patterns"] = -1

    try:
        checks["total_backtests"] = db.execute(
            text("SELECT COUNT(*) FROM trading_backtests")
        ).scalar() or 0
    except Exception:
        checks["total_backtests"] = -1

    try:
        checks["total_trades"] = db.execute(
            text("SELECT COUNT(*) FROM trading_trades")
        ).scalar() or 0
    except Exception:
        checks["total_trades"] = -1

    all_ok = all(
        v == 0
        for k, v in checks.items()
        if k.startswith("orphan")
        or k.startswith("win_rate")
        or k == "stuck_jobs"
    )
    return {
        "status": "healthy" if all_ok else "issues_found",
        "checks": checks,
    }


@router.get("/api/trading/brain/paper")
def api_paper_dashboard(request: Request, db: Session = Depends(get_db)):
    """Paper trading simulation dashboard."""
    ctx = get_identity_ctx(request, db)
    from ...services.trading.paper_trading import get_paper_dashboard
    data = get_paper_dashboard(db, ctx["user_id"])
    return JSONResponse(data)


@router.get("/api/trading/brain/playbook")
def api_daily_playbook(request: Request, db: Session = Depends(get_db)):
    """Generate today's trading playbook with regime, ideas, risk budget."""
    ctx = get_identity_ctx(request, db)
    from ...services.trading.daily_playbook import generate_daily_playbook
    data = generate_daily_playbook(db, ctx["user_id"])
    return JSONResponse(data)


@router.get("/api/trading/brain/governance")
def api_governance_dashboard(request: Request):
    """Governance dashboard: kill switch, approvals, velocity."""
    from ...services.trading.governance import get_governance_dashboard
    return JSONResponse(get_governance_dashboard())


@router.post("/api/trading/brain/governance/kill-switch")
def api_kill_switch(request: Request, action: str = Query("activate")):
    """Toggle the kill switch. action=activate|deactivate"""
    from ...services.trading.governance import activate_kill_switch, deactivate_kill_switch, get_kill_switch_status
    if action == "activate":
        activate_kill_switch("manual_api")
    elif action == "deactivate":
        deactivate_kill_switch()
    return JSONResponse(get_kill_switch_status())


@router.post("/api/trading/brain/governance/approve/{approval_id}")
def api_approve(approval_id: int, action: str = Query("approve")):
    """Approve or reject a pending governance request."""
    from ...services.trading.governance import approve, reject
    if action == "approve":
        ok = approve(approval_id)
    else:
        ok = reject(approval_id, reason="manual_rejection")
    return JSONResponse({"ok": ok})


@router.get("/api/trading/brain/tradeable-patterns")
def api_tradeable_patterns(
    request: Request,
    db: Session = Depends(get_db),
    limit: int | None = Query(None, ge=1, le=50),
    min_oos_wr: float | None = Query(None, ge=0.0, le=100.0),
    min_trades: int | None = Query(None, ge=0),
    include_candidates: bool = Query(False),
    require_bench_pass: bool = Query(False),
):
    """Active ScanPatterns that passed promotion (and optional OOS / trade-count gates).

    Used by Brain UI for a short list of patterns you can open in Trading backtests.
    """
    from ...config import settings
    from ...models.trading import ScanPattern, TradingInsight

    get_identity_ctx(request, db)

    lim = int(limit if limit is not None else settings.brain_tradeable_limit)
    lim = max(1, min(lim, 50))
    min_wr = float(
        min_oos_wr
        if min_oos_wr is not None
        else settings.brain_tradeable_min_oos_wr
    )
    min_tc = int(
        min_trades
        if min_trades is not None
        else settings.brain_tradeable_min_oos_trades
    )

    statuses = ["promoted"]
    if include_candidates:
        statuses.append("candidate")

    trade_count_effective = sa_func.coalesce(
        ScanPattern.oos_trade_count, ScanPattern.backtest_count, 0
    )
    wr_from_oos = and_(
        ScanPattern.oos_win_rate.isnot(None),
        ScanPattern.oos_win_rate * 100.0 >= min_wr,
    )
    wr_from_is = and_(
        ScanPattern.oos_win_rate.is_(None),
        ScanPattern.win_rate.isnot(None),
        ScanPattern.win_rate * 100.0 >= min_wr,
    )
    wr_ok = or_(wr_from_oos, wr_from_is)

    q = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.promotion_status.in_(statuses),
            trade_count_effective >= min_tc,
            wr_ok,
        )
        .order_by(
            ScanPattern.oos_win_rate.desc().nullslast(),
            (ScanPattern.win_rate * 100.0).desc().nullslast(),
            ScanPattern.id.desc(),
        )
    )
    if require_bench_pass:
        q = q.filter(
            ScanPattern.bench_walk_forward_json.isnot(None),
            ScanPattern.bench_walk_forward_json["passes_gate"].astext == "true",
        )

    rows = q.limit(lim).all()
    from ...services.trading.execution_robustness import (
        execution_robustness_summary as _execution_robustness_summary,
        execution_robustness_v2_summary as _execution_robustness_v2_summary,
    )
    from ...services.trading.live_drift import (
        live_drift_summary as _live_drift_summary,
        live_drift_v2_summary as _live_drift_v2_summary,
    )
    from ...services.trading.pattern_validation_projection import read_pattern_validation_projection

    sp_ids = [p.id for p in rows]
    kpi_by_sp = _research_kpi_summary_for_scan_patterns(db, sp_ids)
    insight_by_sp: dict[int, int] = {}
    if sp_ids:
        pairs = (
            db.query(TradingInsight.scan_pattern_id, TradingInsight.id)
            .filter(TradingInsight.scan_pattern_id.in_(sp_ids))
            .order_by(TradingInsight.id.desc())
            .all()
        )
        for spid, iid in pairs:
            if spid is not None and spid not in insight_by_sp:
                insight_by_sp[int(spid)] = int(iid)

    out = []
    for p in rows:
        bench = p.bench_walk_forward_json
        bench_pass = None
        if isinstance(bench, dict):
            bench_pass = bench.get("passes_gate")

        oos_wr = p.oos_win_rate
        if oos_wr is None and p.win_rate is not None:
            display_wr = round(float(p.win_rate) * 100.0, 1)
            wr_source = "in_sample"
        else:
            display_wr = (
                round(float(oos_wr) * 100.0, 1) if oos_wr is not None else None
            )
            wr_source = "oos" if oos_wr is not None else None

        tc = p.oos_trade_count if p.oos_trade_count is not None else p.backtest_count

        stress_pass = None
        if isinstance(bench, dict):
            stress_pass = bench.get("stress_passes_gate")

        oos_val = getattr(p, "oos_validation_json", None) or {}
        if not isinstance(oos_val, dict):
            oos_val = {}
        projection = read_pattern_validation_projection(p)
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "description": (p.description or "")[:500] or None,
                "timeframe": p.timeframe,
                "asset_class": p.asset_class,
                "lifecycle_stage": getattr(p, "lifecycle_stage", None),
                "promotion_status": p.promotion_status,
                "oos_win_rate": p.oos_win_rate,
                "oos_avg_return_pct": p.oos_avg_return_pct,
                "oos_trade_count": p.oos_trade_count,
                "win_rate": p.win_rate,
                "backtest_count": p.backtest_count,
                "display_win_rate_pct": display_wr,
                "display_wr_source": wr_source,
                "trade_count_for_gate": int(tc or 0),
                "bench_passes_gate": bench_pass,
                "bench_stress_passes_gate": stress_pass,
                "oos_validation": oos_val,
                "edge_evidence": projection.edge_evidence or None,
                "live_drift_summary": _live_drift_summary(projection.live_drift),
                "live_drift_v2_summary": _live_drift_v2_summary(projection.live_drift_v2),
                "execution_robustness_summary": _execution_robustness_summary(projection.execution_robustness),
                "execution_robustness_v2_summary": _execution_robustness_v2_summary(projection.execution_robustness_v2),
                "allocation_state": projection.allocation_state or None,
                "queue_tier": getattr(p, "queue_tier", None),
                "linked_insight_id": insight_by_sp.get(p.id),
                "research_kpi_summary": kpi_by_sp.get(p.id),
            }
        )

    return JSONResponse(
        to_jsonable(
            {
                "ok": True,
                "patterns": out,
                "filters": {
                    "limit": lim,
                    "min_oos_wr": min_wr,
                    "min_trades": min_tc,
                    "include_candidates": include_candidates,
                    "require_bench_pass": require_bench_pass,
                },
            }
        )
    )


@router.get("/api/trading/brain/research-edge-patterns")
def api_research_edge_patterns(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(16, ge=1, le=40),
):
    """Repeatable-edge patterns in ``validated`` or ``challenged`` lifecycle (Brain desk research lane).

    Gating for promotion/live remains server-side; this list is for visibility + edge_evidence only.
    """
    from ...models.trading import ScanPattern, TradingInsight

    get_identity_ctx(request, db)
    lim = max(1, min(int(limit), 40))
    rows = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.lifecycle_stage.in_(("validated", "challenged")),
        )
        .order_by(ScanPattern.updated_at.desc().nullslast(), ScanPattern.id.desc())
        .limit(lim)
        .all()
    )
    sp_ids = [p.id for p in rows]
    insight_by_sp: dict[int, int] = {}
    if sp_ids:
        pairs = (
            db.query(TradingInsight.scan_pattern_id, TradingInsight.id)
            .filter(TradingInsight.scan_pattern_id.in_(sp_ids))
            .order_by(TradingInsight.id.desc())
            .all()
        )
        for spid, iid in pairs:
            if spid is not None and spid not in insight_by_sp:
                insight_by_sp[int(spid)] = int(iid)

    from ...services.trading.execution_robustness import (
        execution_robustness_summary as _execution_robustness_summary_re,
        execution_robustness_v2_summary as _execution_robustness_v2_summary_re,
    )
    from ...services.trading.live_drift import (
        live_drift_summary as _live_drift_summary_re,
        live_drift_v2_summary as _live_drift_v2_summary_re,
    )
    from ...services.trading.pattern_validation_projection import read_pattern_validation_projection

    out = []
    for p in rows:
        oos_val = getattr(p, "oos_validation_json", None) or {}
        if not isinstance(oos_val, dict):
            oos_val = {}
        projection = read_pattern_validation_projection(p)
        oos_wr = p.oos_win_rate
        if oos_wr is None and p.win_rate is not None:
            display_wr = round(float(p.win_rate) * 100.0, 1)
            wr_source = "in_sample"
        else:
            display_wr = (
                round(float(oos_wr) * 100.0, 1) if oos_wr is not None else None
            )
            wr_source = "oos" if oos_wr is not None else None
        tc = p.oos_trade_count if p.oos_trade_count is not None else p.backtest_count
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "lifecycle_stage": getattr(p, "lifecycle_stage", None),
                "promotion_status": p.promotion_status,
                "display_win_rate_pct": display_wr,
                "display_wr_source": wr_source,
                "trade_count_for_gate": int(tc or 0),
                "edge_evidence": projection.edge_evidence or None,
                "oos_validation": oos_val,
                "live_drift_summary": _live_drift_summary_re(projection.live_drift),
                "live_drift_v2_summary": _live_drift_v2_summary_re(projection.live_drift_v2),
                "execution_robustness_summary": _execution_robustness_summary_re(projection.execution_robustness),
                "execution_robustness_v2_summary": _execution_robustness_v2_summary_re(projection.execution_robustness_v2),
                "allocation_state": projection.allocation_state or None,
                "linked_insight_id": insight_by_sp.get(p.id),
            }
        )

    return JSONResponse(to_jsonable({"ok": True, "patterns": out, "filters": {"limit": lim}}))


@router.get("/api/trading/brain/confidence-history")
def api_confidence_history(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    history = ts.get_confidence_history(db, ctx["user_id"])
    return JSONResponse({"ok": True, "data": history})


@router.get("/api/trading/brain/activity")
def api_brain_activity(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    events = ts.get_learning_events(db, ctx["user_id"], limit=50)
    return JSONResponse({"ok": True, "events": [
        {
            "id": e.id,
            "event_type": e.event_type,
            "description": e.description,
            "confidence_before": e.confidence_before,
            "confidence_after": e.confidence_after,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]})


@router.get("/api/trading/brain/volatility")
def api_volatility_regime():
    regime = ts.get_volatility_regime()
    return JSONResponse({"ok": True, **regime})


@router.get("/api/trading/brain/predictions")
def api_brain_predictions(
    request: Request,
    db: Session = Depends(get_db),
    tickers: str = Query("", description="Comma-separated tickers to predict"),
):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()] if tickers else None
    predictions = ts.get_current_predictions(db, ticker_list)
    return JSONResponse({"ok": True, "predictions": predictions})


@router.get("/api/trading/brain/status")
def api_trading_brain_status(request: Request, db: Session = Depends(get_db)):
    """Learning pipeline + brain worker snapshot (alias for external Trading Brain UIs).

    If your UI is served over **HTTPS** (e.g. ``https://localhost:5001``), the CHILI API
    must also use **HTTPS** on the configured port — otherwise the browser blocks the call
    (mixed content). See ``docs/TRADING_BRAIN_HTTPS.md``.
    """
    learning: dict = {}
    try:
        learning = ts.get_learning_status()
    except Exception as e:
        _log.warning("api_trading_brain_status learning: %s", e)
        learning = {"running": False, "phase": "error", "error": str(e)}

    # Reuse worker status handler (defined below; resolved at call time).
    worker_resp = api_brain_worker_status(request, db)
    try:
        raw = worker_resp.body
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        worker = json.loads(raw.decode("utf-8"))
    except Exception as e:
        _log.warning("api_trading_brain_status worker decode: %s", e)
        worker = {"ok": False, "error": "worker_status_unavailable"}

    return JSONResponse({"ok": True, "learning": learning, "worker": worker})


@router.get("/api/trading/brain/thesis")
def api_brain_thesis(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    thesis = ts.generate_market_thesis(db, ctx["user_id"])
    return JSONResponse({"ok": True, **thesis})


def _cycle_reports_base_query(db: Session, user_id: int | None):
    q = db.query(LearningCycleAiReport)
    if user_id is not None:
        return q.filter(LearningCycleAiReport.user_id == user_id)
    return q.filter(LearningCycleAiReport.user_id.is_(None))


@router.get("/api/trading/brain/cycle-reports")
def api_brain_cycle_reports(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    ctx = get_identity_ctx(request, db)
    uid = ctx["user_id"]
    base = _cycle_reports_base_query(db, uid)
    total = base.count()
    rows = (
        base.order_by(LearningCycleAiReport.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = []
    for r in rows:
        preview_src = (r.content or "").strip().replace("\r\n", "\n")
        preview = preview_src[:200] + ("…" if len(preview_src) > 200 else "")
        if not preview and preview_src:
            preview = preview_src[:200]
        raw_metrics = r.metrics_json
        metrics_payload = (
            to_jsonable(raw_metrics) if isinstance(raw_metrics, dict) else {}
        )
        items.append({
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "preview": preview,
            "metrics": metrics_payload,
        })
    return JSONResponse({"ok": True, "items": items, "total": total, "offset": offset, "limit": limit})


@router.get("/api/trading/brain/cycle-reports/{report_id}")
def api_brain_cycle_report_detail(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    uid = ctx["user_id"]
    row = db.query(LearningCycleAiReport).filter(LearningCycleAiReport.id == report_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if uid is None:
        if row.user_id is not None:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif row.user_id != uid:
        raise HTTPException(status_code=403, detail="Forbidden")
    raw_metrics = row.metrics_json
    metrics = to_jsonable(raw_metrics) if isinstance(raw_metrics, dict) else {}
    return JSONResponse({
        "ok": True,
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "content": row.content or "",
        "metrics": metrics,
    })


@router.post("/api/trading/learn/weekly-review")
def api_weekly_review(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    review = ts.weekly_performance_review(db, ctx["user_id"])
    return JSONResponse({"ok": True, "review": review or "No trades to review yet."})


@router.post("/api/trading/scan/full")
def api_full_scan(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    status = ts.get_learning_status()

    if status["running"]:
        return JSONResponse({
            "ok": False,
            "message": "Learning cycle already in progress",
            "status": status,
        })

    from ...db import SessionLocal

    def _bg_full_learn(user_id):
        sdb = SessionLocal()
        try:
            ts.run_learning_cycle(sdb, user_id, full_universe=True)
        finally:
            sdb.close()

    background_tasks.add_task(_bg_full_learn, ctx["user_id"])
    return JSONResponse({
        "ok": True,
        "message": "Full market learning cycle started in background",
        "universe": ticker_universe.get_ticker_count(),
    })


def _safe_scan_status_part(label: str, fn, default):
    """Run *fn*; on failure log and return *default* so the Brain UI still gets 200 JSON."""
    try:
        return fn()
    except Exception:
        _log.exception("api_scan_status: %s failed", label)
        return default


@router.get("/api/trading/scan/status")
def api_scan_status():
    """Aggregate scan / learning / prescreen / scheduler state for the Brain UI.

    Values are passed through :func:`to_jsonable` so numpy/pandas scalars and
    non-finite floats cannot break JSON encoding (Starlette uses ``allow_nan=False``).

    Each subsystem is loaded independently so one exception cannot blank the whole response.
    """
    payload = {
        "ok": True,
        "scan": _safe_scan_status_part("scan", ts.get_scan_status, {}),
        "learning": _safe_scan_status_part("learning", ts.get_learning_status, {}),
        "prescreen": _safe_scan_status_part("prescreen", ts.get_prescreen_status, {}),
        "scheduler": _safe_scan_status_part(
            "scheduler",
            trading_scheduler.get_scheduler_info,
            {"running": False, "jobs": []},
        ),
    }
    try:
        return JSONResponse(to_jsonable(payload))
    except Exception:
        _log.exception("api_scan_status: JSON encode failed after to_jsonable")
        # Last resort: minimal safe payload (still 200 so DevTools isn't full of red)
        return JSONResponse(
            {
                "ok": True,
                "scan": {},
                "learning": {},
                "prescreen": {},
                "scheduler": {"running": False, "jobs": []},
                "encode_error": True,
            }
        )


@router.get("/api/trading/universe")
def api_ticker_universe():
    counts = ticker_universe.get_ticker_count()
    return JSONResponse({"ok": True, **counts})


@router.post("/api/trading/universe/refresh")
def api_refresh_universe():
    counts = ticker_universe.refresh_ticker_cache()
    return JSONResponse({"ok": True, "message": "Ticker cache refreshed", **counts})


@router.post("/api/trading/learn/trigger")
def api_trigger_learning(background_tasks: BackgroundTasks, request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)

    if ts.get_learning_status()["running"]:
        return JSONResponse({"ok": False, "message": "Already running"})

    from ...db import SessionLocal

    def _bg(user_id):
        sdb = SessionLocal()
        try:
            ts.run_learning_cycle(sdb, user_id, full_universe=True)
        finally:
            sdb.close()

    background_tasks.add_task(_bg, ctx["user_id"])
    return JSONResponse({"ok": True, "message": "Learning cycle triggered"})


@router.post("/api/trading/learn/deep-study")
def api_deep_study(request: Request, db: Session = Depends(get_db)):
    try:
        ctx = get_identity_ctx(request, db)
        result = ts.deep_study(db, ctx["user_id"])
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/trading/learn/retrain-ml")
def api_retrain_ml(request: Request, db: Session = Depends(get_db)):
    from app.services.trading.pattern_ml import get_meta_learner, apply_ml_feedback
    meta = get_meta_learner()
    result = meta.train(db)
    if result.get("ok"):
        imps = meta.get_pattern_importances()
        fb = apply_ml_feedback(db, imps)
        result["feedback"] = fb
    return JSONResponse({"ok": True, **result})


@router.get("/api/trading/brain/accuracy-detail")
def api_accuracy_detail(
    request: Request,
    db: Session = Depends(get_db),
    type: str = Query("all", description="all|stock|crypto|strong"),
    limit: int = Query(20, ge=1, le=100),
):
    rows = ts.get_accuracy_detail(db, detail_type=type, limit=limit)
    return JSONResponse({"ok": True, "rows": rows, "total": len(rows)})


@router.post("/api/trading/learn/dedup-patterns")
def api_dedup_patterns(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    result = ts.dedup_existing_patterns(db, ctx["user_id"])
    return JSONResponse({"ok": True, **result})


@router.post("/api/trading/learn/patterns/{pattern_id}/demote")
def api_demote_pattern(pattern_id: int, request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "reason": "Sign in required"}, status_code=401)
    from ...models.trading import TradingInsight
    ins = db.query(TradingInsight).filter(TradingInsight.id == pattern_id).first()
    if not ins:
        return JSONResponse({"ok": False, "reason": "Pattern not found"}, status_code=404)
    ins.active = False
    db.commit()
    ts.log_learning_event(
        db, ctx["user_id"], "demotion",
        f"Pattern manually demoted: {ins.pattern_description[:100]}",
        confidence_before=ins.confidence,
        related_insight_id=ins.id,
    )
    return JSONResponse({"ok": True, "message": "Pattern demoted"})


@router.get("/api/trading/learn/backfill-status")
def api_backfill_status():
    """Return progress of the background backtest backfill."""
    from ...main import _backfill_state
    return JSONResponse({"ok": True, **_backfill_state})


# ── Canonical backtest stats (single source of truth for WR) ──────────

def compute_pattern_bt_stats(
    db: Session,
    scan_pattern_ids: list[int],
) -> dict[int, dict]:
    """Return canonical backtest stats per scan_pattern_id.

    Single source of truth for win rate, used by every endpoint.
    Aggregates across **all** TradingInsight rows linked to each scan pattern (shared Brain pool).
    Returns {sp_id: {"wins": int, "losses": int, "total": int,
                     "win_rate": float|None, "avg_return_pct": float|None,
                     "tickers": list[str]}}.
    """
    if not scan_pattern_ids:
        return {}

    from sqlalchemy import func as sa_func, case as sa_case
    from ...models.trading import TradingInsight, BacktestResult

    sp_to_insights: dict[int, list[int]] = {}
    for row in (
        db.query(TradingInsight.scan_pattern_id, TradingInsight.id)
        .filter(TradingInsight.scan_pattern_id.in_(scan_pattern_ids))
        .all()
    ):
        sp_to_insights.setdefault(row[0], []).append(row[1])

    all_insight_ids: list[int] = []
    for ids in sp_to_insights.values():
        all_insight_ids.extend(ids)

    if not all_insight_ids:
        return {}

    ins_to_sp: dict[int, int] = {}
    for sp_id, ins_ids in sp_to_insights.items():
        for iid in ins_ids:
            ins_to_sp[iid] = sp_id

    stats: dict[int, dict] = {}

    for row in (
        db.query(
            BacktestResult.related_insight_id,
            sa_func.count(BacktestResult.id),
            sa_func.sum(sa_case((BacktestResult.return_pct > 0, 1), else_=0)),
            sa_func.avg(BacktestResult.return_pct),
        )
        .filter(
            BacktestResult.related_insight_id.in_(all_insight_ids),
            BacktestResult.trade_count > 0,
        )
        .group_by(BacktestResult.related_insight_id)
        .all()
    ):
        ins_id, total, wins, avg_ret = row
        sp_id = ins_to_sp.get(ins_id)
        if sp_id is None:
            continue
        existing = stats.get(sp_id)
        if existing:
            old_total = existing["total"]
            existing["total"] += (total or 0)
            existing["wins"] += int(wins or 0)
            new_total = existing["total"]
            existing["avg_return_pct"] = round(
                (existing["avg_return_pct"] * old_total + float(avg_ret or 0) * (total or 0)) / max(1, new_total), 2
            )
        else:
            stats[sp_id] = {
                "total": total or 0,
                "wins": int(wins or 0),
                "avg_return_pct": round(float(avg_ret or 0), 2),
            }

    for row in (
        db.query(BacktestResult.related_insight_id, BacktestResult.ticker)
        .filter(BacktestResult.related_insight_id.in_(all_insight_ids))
        .all()
    ):
        sp_id = ins_to_sp.get(row[0])
        if sp_id is None:
            continue
        stats.setdefault(sp_id, {"total": 0, "wins": 0, "avg_return_pct": None})
        stats[sp_id].setdefault("tickers", []).append(row[1])

    for sp_id, s in stats.items():
        total = s["total"]
        wins = s["wins"]
        losses = total - wins
        s["losses"] = losses
        s["win_rate"] = round(wins / max(1, total) * 100, 1) if total > 0 else None
        s["tickers"] = list(set(s.get("tickers", [])))

    return stats


def _resolve_scan_pattern_id_for_insight(db: Session, insight) -> int | None:
    """Best-effort ScanPattern id for aggregating backtests (single resolver)."""
    from ...services.trading.pattern_resolution import (
        resolve_scan_pattern_id_for_insight as _pattern_id_from_insight,
    )

    return _pattern_id_from_insight(db, insight)


def _sync_win_stats_in_description(
    desc: str,
    win_rate: float | None,
    wins: int,
    losses: int,
) -> str:
    """Replace stale '(NN% win, MMM samples)' in description with live numbers."""
    if win_rate is None or wins + losses <= 0:
        return desc
    total = wins + losses
    repl = f"({win_rate}% win, {total} samples)"
    patterns = [
        # Standard: (61% win, 152 samples)
        r"\(\s*\d+(?:\.\d+)?%\s*win\s*,\s*\d+\s*samples?\s*\)",
        # "wins" typo / variant
        r"\(\s*\d+(?:\.\d+)?%\s*wins?\s*,\s*\d+\s*samples?\s*\)",
    ]
    new_desc = desc
    for pat in patterns:
        new_desc, n = re.subn(pat, repl, new_desc, count=1, flags=re.IGNORECASE)
        if n:
            return new_desc
    # Any parenthetical that contains a % win clause (avoids matching "(RSI<25)")
    new_desc, n = re.subn(
        r"\(\s*\d+(?:\.\d+)?%\s*win[^)]*\)",
        repl,
        desc,
        count=1,
        flags=re.IGNORECASE,
    )
    return new_desc if n else desc


def _sibling_insight_ids_for_pattern(
    db: Session,
    primary_insight_id: int,
    scan_pattern_id: int | None,
    sp_resolved_id: int | None,
) -> list[int]:
    """Insight ids that share the same ``scan_pattern_id`` (FK sibling pool for backtests)."""
    from ...models.trading import TradingInsight

    sid = scan_pattern_id or sp_resolved_id
    if not sid:
        return [primary_insight_id]
    ids = [
        int(r[0])
        for r in db.query(TradingInsight.id)
        .filter(TradingInsight.scan_pattern_id == int(sid))
        .all()
    ]
    if primary_insight_id not in ids:
        ids = [*ids, primary_insight_id]
    return sorted(set(ids))


def _evidence_representative_backtest(
    rows: list[Any],
) -> Any:
    """When several stored runs share (ticker, strategy_name), prefer rows with trades over 0-trade reruns."""
    if not rows:
        raise ValueError("rows required")
    from datetime import datetime as _dt

    epoch = _dt(1970, 1, 1)

    def _sort_key(b: Any) -> tuple[int, float]:
        tc = int(getattr(b, "trade_count", None) or 0)
        ra = getattr(b, "ran_at", None) or epoch
        ts = ra.timestamp() if hasattr(ra, "timestamp") else 0.0
        return (tc, ts)

    return max(rows, key=_sort_key)


def _evidence_backtest_asset_universe(
    db: Session,
    desc: str,
    scan_pattern_id: int | None,
    insight_id: int | None = None,
    *,
    learning_event_descriptions: list[str] | None = None,
) -> str:
    """Match backtest_engine: crypto-only / stocks-only / all for evidence aggregation."""
    from ...models.trading import ScanPattern
    from ...services.trading.backtest_engine import (
        _extract_context,
        effective_backtest_asset_universe,
    )

    ac = None
    if scan_pattern_id:
        sp = db.query(ScanPattern).get(scan_pattern_id)
        if sp:
            ac = getattr(sp, "asset_class", None)
    ctx = _extract_context(
        desc or "",
        db=db,
        insight_id=insight_id,
        learning_event_descriptions=learning_event_descriptions,
    )
    return effective_backtest_asset_universe(ac, ctx)


def _brain_bench_card_fields(bench_json: Any) -> dict[str, Any]:
    """Compact fields for Brain pattern cards from ``ScanPattern.bench_walk_forward_json``."""
    out: dict[str, Any] = {
        "bench_fold_summary": None,
        "bench_passes_gate": None,
        "bench_evaluated_at": None,
    }
    if not bench_json or not isinstance(bench_json, dict):
        return out
    try:
        pg = bench_json.get("passes_gate")
        if pg is not None:
            out["bench_passes_gate"] = bool(pg)
        ev = bench_json.get("evaluated_at")
        if ev is not None:
            out["bench_evaluated_at"] = str(ev)
        tickers = bench_json.get("tickers")
        if not isinstance(tickers, dict):
            return out
        parts: list[str] = []
        for sym in sorted(tickers.keys(), key=lambda x: str(x)):
            r = tickers[sym]
            if not isinstance(r, dict):
                continue
            pos = r.get("positive_return_windows")
            nw = r.get("n_windows")
            if pos is None or nw is None:
                continue
            try:
                nw_i = int(nw)
                pos_i = int(pos)
            except (TypeError, ValueError):
                continue
            if nw_i <= 0:
                continue
            parts.append(f"{sym} {pos_i}/{nw_i}+")
        if parts:
            out["bench_fold_summary"] = " · ".join(parts)
    except Exception:
        pass
    return out


def _period_display_from_stored_params(params_raw: Any) -> str | None:
    """Human-readable window line for evidence table: period, interval, bar count, date span."""
    if not params_raw:
        return None
    try:
        p = json.loads(params_raw) if isinstance(params_raw, str) else dict(params_raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(p, dict):
        return None
    dp = p.get("data_provenance")
    if not isinstance(dp, dict):
        dp = {}
    parts: list[str] = []
    per = p.get("period") or dp.get("period")
    iv = p.get("interval") or dp.get("interval")
    if per:
        parts.append(str(per))
    if iv:
        parts.append(str(iv))
    nb = p.get("ohlc_bars")
    if nb is None and dp.get("ohlc_bars") is not None:
        nb = dp.get("ohlc_bars")
    if nb is not None:
        try:
            parts.append(f"{int(nb)} bars")
        except (TypeError, ValueError):
            pass
    cf, ct = p.get("chart_time_from"), p.get("chart_time_to")
    if (cf is None or ct is None) and dp.get("chart_time_from") is not None:
        cf = dp.get("chart_time_from")
        ct = dp.get("chart_time_to")
    if cf is not None and ct is not None:
        try:
            from datetime import datetime, timezone

            a = datetime.fromtimestamp(int(cf), tz=timezone.utc)
            b = datetime.fromtimestamp(int(ct), tz=timezone.utc)
            if a.date() != b.date():
                parts.append(f"{a.strftime('%b %Y')}–{b.strftime('%b %Y')}")
            else:
                parts.append(a.strftime("%Y-%m-%d"))
        except (TypeError, ValueError, OSError):
            pass
    return " · ".join(parts) if parts else None


def _evidence_backtest_matches_scan_pattern(bt, scan_pattern_id: int | None) -> bool:
    """Include only rows with a non-null ``scan_pattern_id`` matching the evidence pattern.

    Rows whose ``strategy_name`` does not match the linked ``ScanPattern.name`` are excluded
    in ``_compute_deduped_backtest_win_stats``; conflicting rows should be deleted in DB
    (see ``scripts/delete_backtests_scan_pattern_strategy_mismatch.py``) so the learning cycle
    can replace them.
    """
    if scan_pattern_id is None:
        return True
    row_sp = getattr(bt, "scan_pattern_id", None)
    if row_sp is None:
        return False
    try:
        return int(row_sp) == int(scan_pattern_id)
    except (TypeError, ValueError):
        return False


def _compute_deduped_backtest_win_stats(
    db: Session,
    sibling_insight_ids: list[int],
    *,
    linked_limit: int = _EVIDENCE_LINKED_BACKTEST_LIMIT,
    asset_universe: str = "all",
    scan_pattern_id: int | None = None,
) -> dict:
    """Backtest list + win stats for the pattern evidence modal.

    ``sibling_insight_ids`` should be **one id** (the opened insight): rows are keyed in DB by
    ``(related_insight_id, ticker, strategy_name)``. Pooling multiple insights that share a
    ``scan_pattern_id`` merged divergent runs per ticker and made metrics look random.
    When ``scan_pattern_id`` is set, rows must match that FK and ``strategy_name`` must align
    with ``ScanPattern.name``.

    Aggregate **win_rate** / W–L in the header matches the per-row **win_rate** columns (simulated
    trades): trade-weighted across deduped rows, not ``return_pct > 0`` (a backtest can lose money
    while still showing a partial trade win rate).
    """
    from ...models.trading import BacktestResult, ScanPattern
    from ...services.trading.market_data import is_crypto as _is_crypto_bt
    from ...services.trading.research_kpis import parse_kpis_from_backtest_params

    from collections import defaultdict

    backtests_out: list[dict] = []
    deduped_bt: list[BacktestResult] = []
    _pattern_name_for_label: str | None = None
    if scan_pattern_id is not None:
        _spn_row = (
            db.query(ScanPattern.name)
            .filter(ScanPattern.id == int(scan_pattern_id))
            .first()
        )
        if _spn_row and (_spn_row[0] or "").strip():
            _pattern_name_for_label = str(_spn_row[0]).strip()

    try:
        linked_bts = (
            db.query(BacktestResult)
            .filter(BacktestResult.related_insight_id.in_(sibling_insight_ids))
            .order_by(BacktestResult.ran_at.desc())
            .limit(linked_limit)
            .all()
        )
        _cand: list[BacktestResult] = []
        for bt in linked_bts:
            if asset_universe == "crypto" and not _is_crypto_bt(bt.ticker or ""):
                continue
            if asset_universe == "stocks" and _is_crypto_bt(bt.ticker or ""):
                continue
            if not _evidence_backtest_matches_scan_pattern(bt, scan_pattern_id):
                continue
            if _pattern_name_for_label and not strategy_label_aligns_scan_pattern_name(
                bt.strategy_name, _pattern_name_for_label
            ):
                continue
            _cand.append(bt)
        _by_dk: dict[tuple[str, str], list[BacktestResult]] = defaultdict(list)
        for bt in _cand:
            _by_dk[(bt.ticker or "", bt.strategy_name or "")].append(bt)
        from ...services.trading.backtest_param_sets import materialize_backtest_params

        for _dk in sorted(_by_dk.keys(), key=lambda k: (k[0], k[1])):
            bt = _evidence_representative_backtest(_by_dk[_dk])
            deduped_bt.append(bt)
            _mp = materialize_backtest_params(db, bt)
            pdisp = _period_display_from_stored_params(_mp)
            if not pdisp:
                try:
                    if isinstance(_mp, dict) and _mp.get("period"):
                        pdisp = str(_mp["period"])
                except (TypeError, ValueError):
                    pdisp = None
            _kpis = parse_kpis_from_backtest_params(_mp)
            _wr_ui = backtest_win_rate_db_to_display_pct(bt.win_rate)
            backtests_out.append({
                "id": bt.id,
                "ticker": bt.ticker,
                "strategy_name": bt.strategy_name,
                "return_pct": bt.return_pct,
                "win_rate": _wr_ui,
                "sharpe": bt.sharpe,
                "max_drawdown": bt.max_drawdown,
                "trade_count": bt.trade_count,
                "ran_at": bt.ran_at.isoformat() if bt.ran_at else None,
                "params": _mp,
                "period_display": pdisp or "--",
                "relevance": 100,
                "kpis": _kpis,
            })
        backtests_out.sort(
            key=lambda x: (
                0 if (x.get("trade_count") or 0) > 0 else 1,
                -(x.get("return_pct") or 0),
                -x.get("relevance", 0),
            )
        )
    except Exception:
        pass

    bt_with_trades = [b for b in deduped_bt if (b.trade_count or 0) > 0]
    bt_total_trades = int(sum(int(b.trade_count or 0) for b in bt_with_trades))
    bt_win_rate = None
    bt_wins = 0
    bt_losses = 0
    if bt_with_trades and bt_total_trades > 0:
        tw_sum = 0.0
        for b in bt_with_trades:
            pct = backtest_win_rate_db_to_display_pct(b.win_rate)
            fr = (float(pct) / 100.0) if pct is not None else 0.0
            tw_sum += fr * int(b.trade_count or 0)
        bt_win_rate = round(tw_sum / float(bt_total_trades) * 100.0, 1)
        bt_wins = int(round(tw_sum))
        bt_losses = max(0, bt_total_trades - bt_wins)
    bt_avg_return = (
        round(sum(float(b.return_pct or 0) for b in bt_with_trades) / len(bt_with_trades), 1)
        if bt_with_trades else None
    )
    dd_vals = [
        float(b.max_drawdown)
        for b in bt_with_trades
        if b.max_drawdown is not None
    ]
    bt_worst_max_drawdown = round(min(dd_vals), 2) if dd_vals else None
    return {
        "backtests_out": backtests_out,
        "bt_wins": bt_wins,
        "bt_losses": bt_losses,
        "bt_win_rate": bt_win_rate,
        "bt_avg_return": bt_avg_return,
        "bt_total_trades": bt_total_trades,
        "bt_runs_with_trades": len(bt_with_trades),
        "bt_worst_max_drawdown": bt_worst_max_drawdown,
    }


def _deduped_win_rate_progress_series(
    db: Session,
    sibling_insight_ids: list[int],
    *,
    row_limit: int = _EVIDENCE_LINKED_BACKTEST_LIMIT,
    asset_universe: str = "all",
    scan_pattern_id: int | None = None,
) -> list[dict]:
    """Chronological win-rate series for the same insight-linked backtest pool as the panel.

    Uses the same recent row pool as the panel: newest ``row_limit`` runs, then replayed in time
    order. State is updated for every run (including 0-trade). Win rate is **trade-weighted** over
    deduped tickers (same formula as ``_compute_deduped_backtest_win_stats``). Last point aligns
    with that panel for the same replayed pool.
    """
    from ...models.trading import BacktestResult, ScanPattern
    from ...services.trading.market_data import is_crypto as _is_crypto_bt

    _pn_prog: str | None = None
    if scan_pattern_id is not None:
        _r = (
            db.query(ScanPattern.name)
            .filter(ScanPattern.id == int(scan_pattern_id))
            .first()
        )
        if _r and (_r[0] or "").strip():
            _pn_prog = str(_r[0]).strip()

    points: list[dict] = []
    try:
        raw = (
            db.query(BacktestResult)
            .filter(BacktestResult.related_insight_id.in_(sibling_insight_ids))
            .order_by(BacktestResult.ran_at.desc())
            .limit(row_limit)
            .all()
        )
    except Exception:
        return points

    rows = list(reversed(raw))
    state: dict[tuple[str, str], BacktestResult] = {}
    last_ts: int | None = None
    for bt in rows:
        if asset_universe == "crypto" and not _is_crypto_bt(bt.ticker or ""):
            continue
        if asset_universe == "stocks" and _is_crypto_bt(bt.ticker or ""):
            continue
        if not _evidence_backtest_matches_scan_pattern(bt, scan_pattern_id):
            continue
        if _pn_prog and not strategy_label_aligns_scan_pattern_name(
            bt.strategy_name, _pn_prog
        ):
            continue
        ran_at = bt.ran_at
        if not ran_at:
            continue
        key = (bt.ticker or "", bt.strategy_name or "")
        prev = state.get(key)
        if prev is None:
            state[key] = bt
        else:
            state[key] = _evidence_representative_backtest([prev, bt])
        with_trades = [v for v in state.values() if (v.trade_count or 0) > 0]
        total_t = sum(int(v.trade_count or 0) for v in with_trades)
        if total_t > 0:
            tw_sum = sum(
                (float(backtest_win_rate_db_to_display_pct(v.win_rate) or 0) / 100.0)
                * int(v.trade_count or 0)
                for v in with_trades
            )
            wr = round(tw_sum / float(total_t) * 100.0, 1)
            wins = int(round(tw_sum))
            losses = max(0, total_t - wins)
        else:
            wr = 0.0
            wins = 0
            losses = 0
            total_t = 0
        ts = int(ran_at.timestamp())
        if last_ts is not None and ts <= last_ts:
            ts = last_ts + 1
        last_ts = ts
        points.append({
            "time": ts,
            "ran_at": ran_at.isoformat(),
            "win_rate": wr,
            "wins": wins,
            "losses": losses,
            "deduped_runs": len(with_trades),
            "simulated_trades": total_t,
        })
    return points


@router.get("/api/trading/learn/patterns")
def api_learned_patterns(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    from collections import defaultdict

    from ...models.trading import LearningEvent, TradingInsight, ScanPattern

    # Shared Brain: all users who can open this page see the full insight pool (not scoped by owner).
    all_insights = (
        db.query(TradingInsight)
        .order_by(TradingInsight.confidence.desc())
        .limit(200)
        .all()
    )

    scan_patterns_by_id: dict[int, ScanPattern] = {}
    for sp in db.query(ScanPattern).all():
        scan_patterns_by_id[sp.id] = sp

    _SECTOR_TICKERS: dict[str, set[str]] = {
        "tech": {"AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AVGO","ORCL","CRM","ADBE","AMD","INTC","QCOM","TXN","NFLX","DDOG","NET","SNOW","PLTR","SHOP"},
        "finance": {"JPM","V","MA","BAC","GS","MS","AXP","BLK","SCHW","CME","HOOD","SOFI","COIN","SQ","PYPL"},
        "healthcare": {"UNH","JNJ","LLY","ABBV","MRK","PFE","TMO","AMGN","GILD","VRTX","REGN","ISRG","MRNA"},
        "consumer": {"WMT","COST","HD","LOW","TGT","PG","KO","PEP","MCD","SBUX","NKE","LULU","CMG"},
        "industrial": {"CAT","DE","HON","UPS","BA","LMT","RTX","GE","EMR","ETN","AXON"},
        "energy": {"XOM","CVX","COP","SLB","EOG","MPC","OXY","HAL","ENPH","FSLR"},
    }

    def _detect_sectors(tickers: list[str]) -> list[str]:
        has_crypto = any(t.endswith("-USD") for t in tickers)
        sectors: list[str] = []
        if has_crypto:
            sectors.append("crypto")
        for sec, sec_set in _SECTOR_TICKERS.items():
            if any(t in sec_set for t in tickers):
                sectors.append(sec)
        return sectors

    # Batch prefetch LearningEvents (avoid N+1). Backtests are **per insight row** only — pooling
    # all insights that share a scan_pattern_id mixed divergent stored rows per ticker.
    _insight_ids = [int(i.id) for i in all_insights]

    _le_by_insight: dict[int, list[str]] = defaultdict(list)
    if _insight_ids:
        for rid, det in (
            db.query(LearningEvent.related_insight_id, LearningEvent.description)
            .filter(LearningEvent.related_insight_id.in_(_insight_ids))
            .order_by(LearningEvent.id.desc())
            .all()
        ):
            if not det:
                continue
            rid_i = int(rid)
            if len(_le_by_insight[rid_i]) >= 20:
                continue
            _le_by_insight[rid_i].append(det)

    _bt_panel_cache: dict[tuple[tuple[int, ...], str, int | None], dict] = {}

    active = []
    demoted = []
    for ins in all_insights:
        desc = ins.pattern_description or ""
        desc_lower = desc.lower()
        if any(w in desc_lower for w in ("bullish", "oversold", "buy", "uptrend", "gained", "above")):
            signal_type = "bullish"
        elif any(w in desc_lower for w in ("bearish", "overbought", "sell", "downtrend", "lost", "below")):
            signal_type = "bearish"
        else:
            signal_type = "neutral"
        win_match = re.search(r"(\d+(?:\.\d+)?)%\s*win", desc)
        ret_match = re.search(r"([+-]?\d+(?:\.\d+)?)%\s*(?:avg|average|return)", desc)
        ticker_match = re.findall(r"\b([A-Z]{1,5}(?:-USD)?)\b", desc)
        tickers_found = [t for t in ticker_match if len(t) >= 2 and t not in {
            "RSI", "MACD", "EMA", "SMA", "ADX", "ATR", "AND", "THE", "FOR",
            "OBV", "MFI", "CCI", "SAR", "USD", "AVG", "NET", "LOW", "HIGH",
        }][:5]

        raw_sid = getattr(ins, "scan_pattern_id", None)
        if raw_sid is not None and int(raw_sid) in scan_patterns_by_id:
            sp_resolved = int(raw_sid)
        else:
            sp_resolved = _resolve_scan_pattern_id_for_insight(db, ins)
        sibs = [int(ins.id)]
        _le_descs = _le_by_insight.get(int(ins.id), [])
        _bt_univ = _evidence_backtest_asset_universe(
            db,
            desc,
            sp_resolved,
            insight_id=ins.id,
            learning_event_descriptions=_le_descs,
        )
        _panel_key = (tuple(sorted(sibs)), _bt_univ, sp_resolved)
        if _panel_key not in _bt_panel_cache:
            _bt_panel_cache[_panel_key] = _compute_deduped_backtest_win_stats(
                db,
                sibs,
                asset_universe=_bt_univ,
                scan_pattern_id=sp_resolved,
            )
        panel = _bt_panel_cache[_panel_key]
        wc = int(panel["bt_wins"])
        lc = int(panel["bt_losses"])
        bt_total_trades = int(panel.get("bt_total_trades") or 0)
        bt_worst_dd = panel.get("bt_worst_max_drawdown")
        real_wr = panel["bt_win_rate"]
        bt_tickers = list({
            b["ticker"] for b in panel["backtests_out"] if b.get("ticker")
        })
        all_tickers = list(set(tickers_found + bt_tickers))
        sectors = _detect_sectors(all_tickers)
        is_crypto = "crypto" in sectors

        # Win rate / W–L here come only from deduped BacktestResult rows (same source as evidence modal).
        # Do not fall back to TradingInsight.win_count — those can be stale or from pre-cleanup runs.

        pattern_display = (
            _sync_win_stats_in_description(desc, real_wr, wc, lc)
            if real_wr is not None and (wc + lc) > 0
            else desc
        )

        # Card scan_pattern_id drives Pine export and /patterns/{id}/backtest — FK only.
        effective_sp = scan_patterns_by_id.get(int(ins.scan_pattern_id))
        variant_info = None
        if effective_sp and effective_sp.parent_id is not None:
            parent_sp = scan_patterns_by_id.get(effective_sp.parent_id)
            variant_info = {
                "label": effective_sp.variant_label,
                "parent_id": effective_sp.parent_id,
                "generation": effective_sp.generation or 0,
                "exit_config": effective_sp.exit_config,
                "origin": effective_sp.origin,
                "parent_name": parent_sp.name if parent_sp else None,
            }
        best_exit = None
        if effective_sp and effective_sp.parent_id is None and effective_sp.exit_config:
            best_exit = effective_sp.variant_label or "evolved"

        _bench_fields = _brain_bench_card_fields(
            getattr(effective_sp, "bench_walk_forward_json", None) if effective_sp else None
        )

        entry = {
            "id": ins.id,
            "insight_user_id": getattr(ins, "user_id", None),
            "insight_scope": "global" if getattr(ins, "user_id", None) is None else "user",
            "hypothesis_family": getattr(ins, "hypothesis_family", None)
            or (getattr(effective_sp, "hypothesis_family", None) if effective_sp else None),
            "pattern": desc,
            "pattern_display": pattern_display,
            "confidence": round(ins.confidence * 100, 1),
            "evidence_count": ins.evidence_count,
            "active": ins.active,
            "signal_type": signal_type,
            "win_rate": real_wr,
            "win_count": wc,
            "loss_count": lc,
            "avg_return": float(ret_match.group(1)) if ret_match else None,
            "example_tickers": tickers_found,
            "bt_tickers": list(set(bt_tickers))[:8],
            "is_crypto": is_crypto,
            "sectors": sectors,
            "created_at": ins.created_at.isoformat(),
            "last_seen": ins.last_seen.isoformat() if ins.last_seen else None,
            "variant": variant_info,
            "best_exit": best_exit,
            "scan_pattern_id": effective_sp.id if effective_sp else None,
            "parent_scan_pattern_id": effective_sp.parent_id if effective_sp else None,
            "ticker_scope": getattr(effective_sp, "ticker_scope", "universal") if effective_sp else "universal",
            "scope_tickers": getattr(effective_sp, "scope_tickers", None) if effective_sp else None,
            "promotion_status": getattr(effective_sp, "promotion_status", None) if effective_sp else None,
            "oos_win_rate": getattr(effective_sp, "oos_win_rate", None) if effective_sp else None,
            "oos_trade_count": getattr(effective_sp, "oos_trade_count", None) if effective_sp else None,
            "oos_avg_return_pct": getattr(effective_sp, "oos_avg_return_pct", None) if effective_sp else None,
            "scan_pattern_is_win_rate": getattr(effective_sp, "win_rate", None) if effective_sp else None,
            "bt_total_trades": bt_total_trades,
            "bt_worst_max_drawdown": bt_worst_dd,
            "backtest_spread_used": getattr(effective_sp, "backtest_spread_used", None) if effective_sp else None,
            "backtest_commission_used": getattr(effective_sp, "backtest_commission_used", None) if effective_sp else None,
            **_bench_fields,
        }
        if ins.active:
            active.append(entry)
        else:
            demoted.append(entry)

    return JSONResponse(
        to_jsonable({
            "ok": True,
            "active": active,
            "demoted": demoted,
            "total_active": len(active),
            "total_demoted": len(demoted),
        })
    )


@router.get("/api/trading/learn/patterns/{insight_id}/export/pine")
def api_export_insight_pine(
    insight_id: int,
    kind: str = "strategy",
    db: Session = Depends(get_db),
):
    """Export Pine for the ``ScanPattern`` linked to this ``TradingInsight`` (server-resolved).

    Brain must call this with **TradingInsight.id**, not ``ScanPattern.id``, so the export
    always matches the open evidence card.
    """
    from ...models.trading import ScanPattern, TradingInsight
    from ...services.trading.pine_export import scan_pattern_to_pine

    insight = db.query(TradingInsight).filter(TradingInsight.id == insight_id).first()
    if not insight:
        return JSONResponse({"ok": False, "error": "Insight not found"}, status_code=404)

    p = db.get(ScanPattern, int(insight.scan_pattern_id))
    if not p:
        return JSONResponse(
            {
                "ok": False,
                "error": "ScanPattern not found for this insight (broken FK).",
            },
            status_code=404,
        )

    k = (kind or "strategy").strip().lower()
    if k not in ("strategy", "indicator"):
        return JSONResponse(
            {"ok": False, "error": "kind must be strategy or indicator"},
            status_code=400,
        )

    pine, warnings = scan_pattern_to_pine(
        p,
        kind=cast(Literal["strategy", "indicator"], k),
        trading_insight_id=insight_id,
    )
    return JSONResponse(
        to_jsonable(
            {
                "ok": True,
                "pine": pine,
                "warnings": warnings,
                "pattern_id": p.id,
                "name": p.name,
                "insight_id": insight_id,
                "kind": k,
            }
        )
    )


@router.api_route(
    "/api/trading/learn/patterns/{pattern_id}/evidence",
    methods=["GET", "POST"],
)
def api_pattern_evidence(pattern_id: int, request: Request, db: Session = Depends(get_db)):
    """Assemble comprehensive evidence for a pattern from all available data sources."""
    ctx = get_identity_ctx(request, db)
    from ...models.trading import TradingInsight

    insight = db.query(TradingInsight).filter(TradingInsight.id == pattern_id).first()
    if not insight:
        return JSONResponse({"ok": False, "reason": "Pattern not found"}, status_code=404)

    try:
        return _api_pattern_evidence_response(db, ctx, pattern_id, insight)
    except Exception:
        _log.exception(
            "api_pattern_evidence failed pattern_id=%s user_id=%s",
            pattern_id,
            ctx.get("user_id"),
        )
        return JSONResponse(
            to_jsonable(
                {
                    "ok": False,
                    "error": "evidence_load_failed",
                    "pattern_id": pattern_id,
                }
            ),
            status_code=500,
        )


def _api_pattern_evidence_response(
    db: Session,
    ctx: dict,
    pattern_id: int,
    insight,
) -> JSONResponse:
    from ...models.trading import LearningEvent, TradingHypothesis, Trade

    desc = insight.pattern_description or ""
    sp_resolved_id = _resolve_scan_pattern_id_for_insight(db, insight)
    # One Brain card = one TradingInsight; backtests are stored per (insight, ticker, strategy).
    sibling_ids = [int(pattern_id)]

    # 1. Learning events linked to this insight
    events = (
        db.query(LearningEvent)
        .filter(LearningEvent.related_insight_id == pattern_id)
        .order_by(LearningEvent.created_at.desc())
        .limit(30)
        .all()
    )
    timeline = [
        {
            "id": e.id,
            "event_type": e.event_type,
            "description": e.description,
            "confidence_before": e.confidence_before,
            "confidence_after": e.confidence_after,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]

    # 2. Related hypotheses — match by keyword overlap or pattern_id link
    keywords = _extract_keywords_for_matching(desc)
    all_hyps = db.query(TradingHypothesis).limit(100).all()
    hypotheses = []
    for h in all_hyps:
        h_desc = (h.description or "").lower()
        if any(kw in h_desc for kw in keywords):
            confirm_rate = (
                (h.times_confirmed or 0) / max(1, h.times_tested or 1)
            )
            last_result = None
            if h.last_result_json:
                try:
                    last_result = json.loads(h.last_result_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            hypotheses.append({
                "id": h.id,
                "description": h.description,
                "status": h.status,
                "origin": h.origin,
                "times_tested": h.times_tested or 0,
                "times_confirmed": h.times_confirmed or 0,
                "times_rejected": h.times_rejected or 0,
                "confirm_rate": round(confirm_rate * 100, 1),
                "expected_winner": h.expected_winner,
                "condition_a": h.condition_a,
                "condition_b": h.condition_b,
                "last_result": last_result,
                "last_tested_at": h.last_tested_at.isoformat() if h.last_tested_at else None,
            })

    # 3. Matching trades by pattern_tags
    trades_out = []
    try:
        trade_keywords = _extract_keywords_for_matching(desc, min_len=3)
        all_trades = (
            db.query(Trade)
            .filter(Trade.user_id == ctx["user_id"], Trade.pattern_tags.isnot(None))
            .order_by(Trade.entry_date.desc())
            .limit(200)
            .all()
        )
        for t in all_trades:
            tags_lower = (t.pattern_tags or "").lower()
            if any(kw in tags_lower for kw in trade_keywords):
                trades_out.append({
                    "id": t.id,
                    "ticker": t.ticker,
                    "direction": t.direction,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "pnl": t.pnl,
                    "status": t.status,
                    "entry_date": t.entry_date.isoformat() if t.entry_date else None,
                    "exit_date": t.exit_date.isoformat() if t.exit_date else None,
                    "pattern_tags": t.pattern_tags,
                })
                if len(trades_out) >= 20:
                    break
    except Exception:
        pass

    # 4–5. Backtests + win rate: deduped list (latest per ticker/strategy), same as pattern cards
    _ev_univ = _evidence_backtest_asset_universe(
        db, desc, sp_resolved_id, insight_id=pattern_id,
    )
    panel = _compute_deduped_backtest_win_stats(
        db, sibling_ids, asset_universe=_ev_univ,
        scan_pattern_id=sp_resolved_id,
    )
    backtests_out = panel["backtests_out"]
    bt_wins = int(panel["bt_wins"])
    bt_losses = int(panel["bt_losses"])
    bt_win_rate = panel["bt_win_rate"]
    bt_avg_return = panel["bt_avg_return"]
    win_rate_progress = _deduped_win_rate_progress_series(
        db, sibling_ids, asset_universe=_ev_univ,
        scan_pattern_id=sp_resolved_id,
    )
    if bt_win_rate is not None and (bt_wins + bt_losses) > 0:
        _sync_tail = {
            "win_rate": bt_win_rate,
            "wins": bt_wins,
            "losses": bt_losses,
            "deduped_runs": int(panel.get("bt_runs_with_trades") or 0),
            "simulated_trades": int(panel.get("bt_total_trades") or 0),
        }
        if win_rate_progress:
            _last_pt = win_rate_progress[-1]
            win_rate_progress[-1] = {**_last_pt, **_sync_tail}
        else:
            from datetime import datetime, timezone

            _now = datetime.now(timezone.utc)
            win_rate_progress = [
                {
                    "time": int(_now.timestamp()),
                    "ran_at": _now.isoformat(),
                    **_sync_tail,
                }
            ]

    pattern_display = (
        _sync_win_stats_in_description(desc, bt_win_rate, bt_wins, bt_losses)
        if bt_win_rate is not None and (bt_wins + bt_losses) > 0
        else desc
    )

    computed_stats = _compute_evidence_stats(
        timeline, hypotheses, trades_out, backtests_out,
        bt_wins, bt_losses, bt_win_rate, bt_avg_return,
        backtest_total_displayed=len(backtests_out),
        bt_runs_with_trades=int(panel.get("bt_runs_with_trades") or 0),
    )

    return JSONResponse(
        to_jsonable(
            {
                "ok": True,
                "resolved_scan_pattern_id": sp_resolved_id,
                "pattern_display": pattern_display,
                "win_rate_progress": win_rate_progress,
                "insight": {
                    "id": insight.id,
                    "pattern": desc,
                    "pattern_display": pattern_display,
                    "confidence": round(insight.confidence * 100, 1),
                    "evidence_count": insight.evidence_count,
                    # Header WR: trade-weighted simulated WR; win/loss = rounded simulated wins vs losses (not # of backtest rows).
                    "win_count": bt_wins,
                    "loss_count": bt_losses,
                    "win_rate": bt_win_rate,
                    "has_saved_backtest_stats": (bt_wins + bt_losses) > 0,
                    "active": insight.active,
                    "created_at": insight.created_at.isoformat(),
                    "last_seen": insight.last_seen.isoformat() if insight.last_seen else None,
                },
                "computed_stats": computed_stats,
                "timeline": timeline,
                "hypotheses": hypotheses,
                "trades": trades_out,
                "backtests": backtests_out,
            }
        )
    )


def _load_backtest_for_evidence_read(
    ctx: dict,
    db: Session,
    bt_id: int,
):
    """Return ``(backtest_row, None)`` or ``(None, JSONResponse)`` on 404."""
    from ...models.trading import BacktestResult

    bt = db.query(BacktestResult).filter(BacktestResult.id == bt_id).first()
    if not bt:
        return None, JSONResponse(
            to_jsonable({"ok": False, "error": "Backtest not found"}),
            status_code=404,
        )

    # Shared Brain: any stored backtest row is readable if the row exists.
    return bt, None


@router.get("/api/trading/learn/backtest/{bt_id}")
def api_get_stored_backtest(bt_id: int, request: Request, db: Session = Depends(get_db)):
    """Return stored BacktestResult by id. Used so the expanded chart matches the table header.

    Both header and chart come from the same DB row. Returns 404 if not found or not accessible.
    """
    ctx = get_identity_ctx(request, db)
    bt, err = _load_backtest_for_evidence_read(ctx, db, bt_id)
    if err:
        return err

    eq = []
    try:
        if bt.equity_curve:
            eq = json.loads(bt.equity_curve) if isinstance(bt.equity_curve, str) else (bt.equity_curve or [])
    except Exception:
        pass

    from ...services.trading.backtest_param_sets import materialize_backtest_params

    return JSONResponse({
        "ok": True,
        "id": bt.id,
        "ticker": bt.ticker,
        "strategy_name": bt.strategy_name,
        "return_pct": float(bt.return_pct) if bt.return_pct is not None else None,
        "win_rate": backtest_win_rate_db_to_display_pct(bt.win_rate),
        "trade_count": bt.trade_count or 0,
        "sharpe": float(bt.sharpe) if bt.sharpe is not None else None,
        "max_drawdown": float(bt.max_drawdown) if bt.max_drawdown is not None else None,
        "equity_curve": eq,
        "params": materialize_backtest_params(db, bt),
    })


@router.api_route(
    "/api/trading-brain/brain/backtest/{bt_id}/trades",
    methods=["GET", "POST"],
)
@router.api_route(
    "/api/trading/learn/backtest/{bt_id}/trades",
    methods=["GET", "POST"],
)
def api_stored_backtest_trades(bt_id: int, request: Request, db: Session = Depends(get_db)):
    """Per-trade rows for a stored backtest (Chill / external UI compat).

    Data comes from ``PatternTradeRow`` when trade analytics were persisted; otherwise ``trades`` is empty.
    """
    ctx = get_identity_ctx(request, db)
    from ...models.trading import PatternTradeRow

    _bt, err = _load_backtest_for_evidence_read(ctx, db, bt_id)
    if err:
        return err

    rows = (
        db.query(PatternTradeRow)
        .filter(PatternTradeRow.backtest_result_id == bt_id)
        .order_by(PatternTradeRow.as_of_ts.asc())
        .all()
    )
    trades_out = []
    for r in rows:
        feats = r.features_json if isinstance(r.features_json, dict) else {}
        trades_out.append(
            {
                "id": r.id,
                "ticker": r.ticker,
                "as_of_ts": r.as_of_ts.isoformat() if r.as_of_ts else None,
                "timeframe": r.timeframe,
                "outcome_return_pct": r.outcome_return_pct,
                "label_win": r.label_win,
                "features": feats,
            }
        )

    return JSONResponse(
        to_jsonable(
            {
                "ok": True,
                "backtest_id": bt_id,
                "trades": trades_out,
            }
        )
    )


def _access_backtest_row(
    ctx: dict,
    db: Session,
    bt,
    ins,
) -> bool:
    """Return True if the user may read/update this BacktestResult row (shared Brain pool)."""
    return True


@router.post("/api/trading/learn/backtest/{bt_id}/rerun")
def api_rerun_stored_backtest(bt_id: int, request: Request, db: Session = Depends(get_db)):
    """Re-run using stored ``BacktestResult.params`` when present (``period``,
    ``interval``, and ``chart_time_from`` / ``chart_time_to`` for OHLC fetch), else
    **brain** ``get_brain_backtest_window(ScanPattern.timeframe)`` and current rules.

    Aligning the OHLC window with the saved row reduces ``Not enough data`` errors
    when expanding the evidence mini-chart after a successful batch run.

    Results can still differ from an older save if OHLCV, rules, or providers changed.

    **404 "Backtest not found"**: no ``BacktestResult`` row for ``bt_id`` (deleted DB row,
    different ``DATABASE_URL``, or Pattern Evidence UI holding a stale ``id`` after
    migrations or dedupe — reopen the modal or hard-refresh the Brain page).
    """
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...models.trading import BacktestResult, TradingInsight
    from ...services.trading.stored_backtest_rerun import rerun_stored_backtest_by_id

    bt = db.query(BacktestResult).filter(BacktestResult.id == bt_id).first()
    if not bt:
        return JSONResponse({"ok": False, "error": "Backtest not found"}, status_code=404)

    ins = (
        db.query(TradingInsight).filter(TradingInsight.id == bt.related_insight_id).first()
        if bt.related_insight_id
        else None
    )
    if ins and not _access_backtest_row(ctx, db, bt, ins):
        return JSONResponse({"ok": False, "error": "Access denied"}, status_code=404)

    out = rerun_stored_backtest_by_id(db, bt_id)
    if not out.get("ok"):
        err = out.get("error", "backtest failed")
        code = 404 if err in ("Backtest not found", "Pattern not found") else 400
        return JSONResponse(to_jsonable({"ok": False, "error": err}), status_code=code)
    return JSONResponse(to_jsonable(out))


@router.post("/api/trading/learn/patterns/{insight_id}/rerun-stored-backtests")
def api_rerun_all_stored_backtests_for_insight(
    insight_id: int,
    request: Request,
    db: Session = Depends(get_db),
    limit: int | None = Query(
        None,
        ge=1,
        le=500,
        description="Optional cap on how many listed rows to rerun (default: all).",
    ),
):
    """Re-run every deduped backtest row shown in Pattern Evidence for this insight.

    Uses each row's saved period / OHLC window (same as single-row **Rerun**). Work runs
    in a **background thread** so the HTTP request returns immediately; watch server logs
    for ``[rerun_all_insight]`` progress, then reload Pattern Evidence.
    """
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...models.trading import TradingInsight
    from ...services.trading.stored_backtest_rerun import (
        collect_evidence_listed_backtest_ids,
        spawn_insight_stored_backtests_rerun_thread,
    )

    ins = db.get(TradingInsight, int(insight_id))
    if not ins:
        return JSONResponse({"ok": False, "error": "Insight not found"}, status_code=404)

    ids, err = collect_evidence_listed_backtest_ids(db, int(insight_id), limit=limit)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    if not ids:
        return JSONResponse(
            {
                "ok": False,
                "error": "No stored backtests match this insight / pattern filters",
            },
            status_code=400,
        )

    spawn_insight_stored_backtests_rerun_thread(int(insight_id), limit=limit)
    return JSONResponse(
        to_jsonable(
            {
                "ok": True,
                "queued": len(ids),
                "insight_id": int(insight_id),
                "message": (
                    "Reruns started in the background. Reload Pattern Evidence when server "
                    "logs show [rerun_all_insight] done (may take many minutes for 100+ rows)."
                ),
            }
        )
    )


@router.post("/api/trading/learn/backtest/{bt_id}/refresh")
async def api_refresh_backtest(bt_id: int, request: Request, db: Session = Depends(get_db)):
    """Update stored BacktestResult with fresh backtest results. Keeps header and chart in sync."""
    ctx = get_identity_ctx(request, db)
    from datetime import datetime as dt
    from ...models.trading import BacktestResult

    bt = db.query(BacktestResult).filter(BacktestResult.id == bt_id).first()
    if not bt:
        return JSONResponse({"ok": False, "error": "Backtest not found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    ret = body.get("return_pct")
    bt.return_pct = float(ret) if ret is not None else bt.return_pct
    wr = body.get("win_rate")
    if wr is not None:
        _nw = normalize_win_rate_for_db(float(wr))
        bt.win_rate = float(_nw) if _nw is not None else bt.win_rate
    sh = body.get("sharpe")
    bt.sharpe = float(sh) if sh is not None else None
    md = body.get("max_drawdown")
    bt.max_drawdown = float(md) if md is not None else bt.max_drawdown
    bt.trade_count = int(body.get("trade_count", bt.trade_count) or 0)
    if body.get("equity_curve") is not None:
        bt.equity_curve = json.dumps(body["equity_curve"]) if isinstance(body["equity_curve"], list) else body["equity_curve"]

    from ...services.trading.backtest_param_sets import get_or_create_backtest_param_set, materialize_backtest_params

    # Merge chart window metadata from client (same run as stats) so stored params match the mini-chart.
    curp = materialize_backtest_params(db, bt)
    params_merged = False
    if isinstance(body.get("params"), dict):
        for k, v in body["params"].items():
            if v is not None:
                curp[k] = v
                params_merged = True
    else:
        for k in (
            "period", "interval", "ohlc_bars", "chart_time_from", "chart_time_to",
            "strategy_id",
        ):
            if body.get(k) is not None:
                curp[k] = body[k]
                params_merged = True
    if params_merged:
        bt.params = json.dumps(curp)
        sid = get_or_create_backtest_param_set(db, curp)
        if sid is not None:
            bt.param_set_id = int(sid)

    bt.ran_at = dt.utcnow()
    db.commit()
    db.refresh(bt)

    _mp = materialize_backtest_params(db, bt)
    pdisp = _period_display_from_stored_params(_mp)
    if not pdisp:
        try:
            if isinstance(_mp, dict) and _mp.get("period"):
                pdisp = str(_mp["period"])
        except (TypeError, ValueError):
            pdisp = None

    return JSONResponse({
        "ok": True,
        "id": bt.id,
        "return_pct": float(bt.return_pct),
        "win_rate": backtest_win_rate_db_to_display_pct(bt.win_rate),
        "sharpe": float(bt.sharpe) if bt.sharpe is not None else None,
        "max_drawdown": float(bt.max_drawdown),
        "trade_count": bt.trade_count,
        "ran_at": bt.ran_at.isoformat(),
        "period_display": pdisp or "--",
        "params": _mp,
    })


@router.get("/api/trading/learn/patterns/{pattern_id}/evolution")
def api_pattern_evolution(pattern_id: int, request: Request, db: Session = Depends(get_db)):
    """Return the full evolution tree for a pattern's ScanPattern lineage."""
    ctx = get_identity_ctx(request, db)
    from ...models.trading import (
        TradingInsight, ScanPattern, TradingHypothesis,
    )

    insight = db.query(TradingInsight).filter(TradingInsight.id == pattern_id).first()
    if not insight:
        return JSONResponse({"ok": False, "reason": "not found"}, 404)

    sp_id = getattr(insight, "scan_pattern_id", None)
    if not sp_id:
        return JSONResponse({"ok": True, "root": None, "current_scan_pattern_id": None})

    current_sp = db.query(ScanPattern).get(sp_id)
    if not current_sp:
        return JSONResponse({"ok": True, "root": None, "current_scan_pattern_id": None})

    root = current_sp
    visited = {root.id}
    while root.parent_id is not None:
        parent = db.query(ScanPattern).get(root.parent_id)
        if not parent or parent.id in visited:
            break
        visited.add(parent.id)
        root = parent

    all_patterns = [root]
    current_ids = [root.id]
    while current_ids:
        children = (
            db.query(ScanPattern)
            .filter(ScanPattern.parent_id.in_(current_ids))
            .order_by(ScanPattern.generation, ScanPattern.id)
            .all()
        )
        all_patterns.extend(children)
        current_ids = [c.id for c in children]

    all_sp_ids = [p.id for p in all_patterns]
    bt_stats = compute_pattern_bt_stats(db, all_sp_ids)

    hyp_by_sp: dict[int, list] = {sp_id: [] for sp_id in all_sp_ids}
    for h in (
        db.query(TradingHypothesis)
        .filter(TradingHypothesis.related_pattern_id.in_(all_sp_ids))
        .all()
    ):
        confirm_rate = (
            (h.times_confirmed or 0) / max(1, h.times_tested or 1)
        )
        hyp_by_sp.setdefault(h.related_pattern_id, []).append({
            "id": h.id,
            "description": h.description,
            "status": h.status,
            "confirm_rate": round(confirm_rate * 100, 1),
            "times_tested": h.times_tested or 0,
        })

    sp_map = {p.id: p for p in all_patterns}
    children_map: dict[int, list[int]] = {}
    for p in all_patterns:
        if p.parent_id is not None:
            children_map.setdefault(p.parent_id, []).append(p.id)

    def _build_node(sp: "ScanPattern") -> dict:
        stats = bt_stats.get(sp.id, {})
        total = stats.get("total", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        wr = stats.get("win_rate")

        exit_cfg = None
        if sp.exit_config:
            try:
                exit_cfg = json.loads(sp.exit_config)
            except (json.JSONDecodeError, TypeError):
                pass

        child_ids = children_map.get(sp.id, [])
        children_nodes = [
            _build_node(sp_map[cid]) for cid in child_ids if cid in sp_map
        ]

        origin = sp.origin or ""
        if "entry" in origin:
            mutation_type = "entry"
        elif "combo" in origin or "cross" in origin:
            mutation_type = "combo"
        elif "exit" in origin or "mut-" in (sp.variant_label or ""):
            mutation_type = "exit"
        else:
            mutation_type = "root"

        return {
            "id": sp.id,
            "name": sp.name,
            "generation": sp.generation or 0,
            "variant_label": sp.variant_label,
            "origin": sp.origin,
            "mutation_type": mutation_type,
            "active": sp.active,
            "exit_config": exit_cfg,
            "win_rate": wr,
            "wins": wins,
            "losses": losses,
            "avg_return_pct": stats.get("avg_return_pct"),
            "backtest_count": total,
            "is_current": sp.id == current_sp.id,
            "hypotheses": hyp_by_sp.get(sp.id, []),
            "children": children_nodes,
        }

    return JSONResponse({
        "ok": True,
        "root": _build_node(root),
        "current_scan_pattern_id": current_sp.id,
    })


# ── Backtest Queue Management ────────────────────────────────────────────

@router.get("/api/trading/backtest-queue/status")
def api_backtest_queue_status(db: Session = Depends(get_db)):
    """Get the current backtest queue status."""
    from app.services.trading.backtest_queue import get_queue_status
    status = get_queue_status(db)
    return JSONResponse({"ok": True, **status})


@router.post("/api/trading/patterns/{pattern_id}/boost")
def api_boost_pattern(pattern_id: int, db: Session = Depends(get_db)):
    """Boost a ScanPattern to the front of the backtest queue."""
    from app.services.trading.backtest_queue import boost_pattern
    from ...models.trading import ScanPattern
    
    pattern = db.query(ScanPattern).get(pattern_id)
    if not pattern:
        return JSONResponse({"ok": False, "reason": "Pattern not found"}, status_code=404)
    
    success = boost_pattern(db, pattern_id, priority=100)
    if success:
        return JSONResponse({
            "ok": True,
            "message": f"Pattern '{pattern.name}' boosted to front of queue",
            "pattern_id": pattern_id,
        })
    return JSONResponse({"ok": False, "reason": "Failed to boost pattern"}, status_code=500)


@router.post("/api/trading/patterns/{pattern_id}/clear-boost")
def api_clear_pattern_boost(pattern_id: int, db: Session = Depends(get_db)):
    """Clear the boost priority for a ScanPattern."""
    from app.services.trading.backtest_queue import clear_boost
    from ...models.trading import ScanPattern
    
    pattern = db.query(ScanPattern).get(pattern_id)
    if not pattern:
        return JSONResponse({"ok": False, "reason": "Pattern not found"}, status_code=404)
    
    success = clear_boost(db, pattern_id)
    if success:
        return JSONResponse({
            "ok": True,
            "message": f"Boost cleared for pattern '{pattern.name}'",
            "pattern_id": pattern_id,
        })
    return JSONResponse({"ok": False, "reason": "Failed to clear boost"}, status_code=500)


@router.get("/api/trading/backtest-queue/pending")
def api_backtest_queue_pending(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    """Get the list of pending patterns in the backtest queue."""
    from app.services.trading.backtest_queue import get_pending_patterns
    
    patterns = get_pending_patterns(db, limit=limit)
    return JSONResponse({
        "ok": True,
        "patterns": [
            {
                "id": p.id,
                "name": p.name,
                "origin": p.origin,
                "priority": p.backtest_priority,
                "last_backtest_at": p.last_backtest_at.isoformat() if p.last_backtest_at else None,
                "win_rate": p.win_rate,
                "active": p.active,
            }
            for p in patterns
        ],
        "count": len(patterns),
    })


def _compute_evidence_stats(
    timeline: list,
    hypotheses: list,
    trades: list,
    backtests: list,
    bt_wins: int = 0,
    bt_losses: int = 0,
    bt_win_rate: float | None = None,
    bt_avg_return: float | None = None,
    backtest_total_displayed: int | None = None,
    *,
    bt_runs_with_trades: int | None = None,
) -> dict:
    """Compute live aggregate stats from the actual evidence data.

    When bt_avg_return is provided, it is used for backtest_avg_return
    (computed from displayed backtests with trades). Otherwise falls back
    to averaging all backtests.
    """
    stats: dict = {}

    if hypotheses:
        tested = [h for h in hypotheses if h.get("times_tested", 0) > 0]
        if tested:
            avg_confirm = sum(h["confirm_rate"] for h in tested) / len(tested)
            stats["hypothesis_confirm_rate"] = round(avg_confirm, 1)
            stats["hypotheses_tested"] = len(tested)

    if trades:
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = sum(1 for t in closed if t["pnl"] > 0)
            stats["trade_win_rate"] = round(wins / len(closed) * 100, 1)
            stats["trade_count"] = len(closed)
            stats["total_pnl"] = round(sum(t["pnl"] for t in closed), 2)

    # bt_wins/bt_losses are simulated-trade tallies (trade-weighted panel). backtest_count in the UI
    # is deduped rows with trades (e.g. "4/38 with trades"), not total simulated trades.
    sim_trade_total = bt_wins + bt_losses
    if sim_trade_total > 0 and bt_win_rate is not None:
        stats["backtest_avg_win_rate"] = bt_win_rate
        stats["backtest_count"] = (
            int(bt_runs_with_trades)
            if bt_runs_with_trades is not None
            else sim_trade_total
        )
        stats["backtest_simulated_trades"] = sim_trade_total
    if backtest_total_displayed is not None:
        stats["backtest_total_displayed"] = backtest_total_displayed
    if bt_avg_return is not None:
        stats["backtest_avg_return"] = round(bt_avg_return, 1)
    elif backtests:
        stats["backtest_avg_return"] = round(
            sum(b.get("return_pct", 0) for b in backtests) / len(backtests), 1
        )

    if timeline:
        stats["confirmations"] = sum(
            1 for e in timeline if e.get("event_type") in ("discovery", "real_trade_validation")
        )
        stats["challenges"] = sum(
            1 for e in timeline if e.get("event_type") == "hypothesis_challenged"
        )

    return stats


def _extract_keywords_for_matching(text: str, min_len: int = 4) -> list[str]:
    """Extract meaningful lowercase keywords from a pattern description for fuzzy matching."""
    stop_words = {
        "chili", "validated", "challenge", "discovery", "data", "says", "otherwise",
        "confirmed", "actually", "outperform", "outperforms", "samples", "better",
        "with", "than", "from", "that", "this", "entries", "positive", "negative",
        "above", "below", "signal", "pattern", "average", "return", "based",
        "when", "have", "been", "more", "less", "into", "over", "under",
        "trend", "following", "bullish", "bearish", "rate", "combo", "bonus",
        "synergy", "baseline", "versus", "compared", "strong", "weak",
        "high", "very", "just", "only", "also", "some", "each", "every",
    }
    words = re.findall(r'[a-z_]+', text.lower())
    keywords = []
    seen = set()
    for w in words:
        if len(w) >= min_len and w not in stop_words and w not in seen:
            seen.add(w)
            keywords.append(w)
    return keywords[:15]


# ── Pattern trade analytics (evidence-driven evolution) ─────────────────

@router.get("/api/trading/brain/pattern/{pattern_id}/trade-analytics")
def api_pattern_trade_analytics(
    request: Request,
    pattern_id: int,
    db: Session = Depends(get_db),
    window_days: int = Query(180, ge=30, le=730),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...services.trading.pattern_trade_analysis import analyze_pattern_trades

    report = analyze_pattern_trades(db, pattern_id, window_days=window_days)
    return JSONResponse({"ok": True, "report": report.to_json()})


@router.post("/api/trading/brain/pattern/{pattern_id}/evidence/propose")
def api_pattern_evidence_propose(
    request: Request,
    pattern_id: int,
    db: Session = Depends(get_db),
    window_days: int = Query(180, ge=30, le=730),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...services.trading.pattern_evidence_service import propose_from_analysis

    hyps = propose_from_analysis(db, pattern_id, window_days=window_days, user_id=ctx.get("user_id"))
    return JSONResponse({
        "ok": True,
        "created": len(hyps),
        "ids": [h.id for h in hyps],
    })


@router.post("/api/trading/brain/evidence/{hypothesis_id}/walk-forward")
def api_pattern_evidence_walk_forward(
    request: Request,
    hypothesis_id: int,
    db: Session = Depends(get_db),
    is_days: int = Query(90, ge=20, le=400),
    oos_days: int = Query(90, ge=20, le=400),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...services.trading.pattern_evidence_service import walk_forward_validate

    out = walk_forward_validate(db, hypothesis_id, is_days=is_days, oos_days=oos_days)
    return JSONResponse(out)


@router.post("/api/trading/brain/evidence/{hypothesis_id}/apply")
def api_pattern_evidence_apply(
    request: Request,
    hypothesis_id: int,
    db: Session = Depends(get_db),
    dry_run: bool = Query(True),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...services.trading.pattern_evolution_apply import apply_evidence_hypothesis

    out = apply_evidence_hypothesis(
        db, hypothesis_id, dry_run=dry_run, user_id=ctx.get("user_id"),
    )
    return JSONResponse(out)


@router.get("/api/trading/brain/pattern/{pattern_id}/trade-ml")
def api_pattern_trade_ml(
    request: Request,
    pattern_id: int,
    db: Session = Depends(get_db),
    window_days: int = Query(365, ge=60, le=900),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...services.trading.pattern_trade_ml import train_on_pattern_trades

    return JSONResponse(train_on_pattern_trades(db, pattern_id, window_days=window_days))


# ── Brain Worker Control ────────────────────────────────────────────────

from pathlib import Path

_BRAIN_WORKER_STATUS_FILE = DATA_DIR / "brain_worker_status.json"
_BRAIN_WORKER_STOP_SIGNAL = DATA_DIR / "brain_worker_stop"
_BRAIN_WORKER_PAUSE_SIGNAL = DATA_DIR / "brain_worker_pause"
_BRAIN_WORKER_WAKE_SIGNAL = DATA_DIR / "brain_worker_wake"


def _merge_brain_worker_db_fields(db: Session, payload: dict) -> None:
    """Attach PostgreSQL control row fields for UI / debugging."""
    try:
        from ...services.brain_worker_signals import (
            get_worker_control_snapshot,
            heartbeat_is_stale,
        )

        ctrl = get_worker_control_snapshot(db)
        if ctrl is None:
            return
        payload["db_wake_requested"] = bool(ctrl.wake_requested)
        payload["db_stop_requested"] = bool(ctrl.stop_requested)
        if ctrl.last_heartbeat_at:
            payload["last_heartbeat_at"] = ctrl.last_heartbeat_at.isoformat() + "Z"
        payload["heartbeat_stale"] = heartbeat_is_stale(ctrl.last_heartbeat_at)
    except Exception:
        pass


def _merge_trading_insight_debug_counts(db: Session, payload: dict) -> None:
    """Counts for verifying worker vs UI: global (NULL user_id) vs total insights."""
    try:
        from ...config import settings
        from ...models.trading import TradingInsight

        payload["brain_default_user_id"] = settings.brain_default_user_id
        null_n = (
            db.query(sa_func.count(TradingInsight.id))
            .filter(TradingInsight.user_id.is_(None))
            .scalar()
        )
        payload["trading_insights_null_user_count"] = int(null_n or 0)
        total_n = db.query(sa_func.count(TradingInsight.id)).scalar()
        payload["trading_insights_total_count"] = int(total_n or 0)
    except Exception:
        pass


def _clear_stale_brain_worker_status_if_needed(db: Session, force: bool) -> dict:
    """Remove stale brain_worker_status.json when PID is dead, unknown+stale heartbeat, or force."""
    from ...services.brain_worker_signals import get_worker_control_snapshot, heartbeat_is_stale

    extra: dict = {"stale_status_cleared": False}
    if not _BRAIN_WORKER_STATUS_FILE.exists():
        return extra

    ctrl = get_worker_control_snapshot(db)
    stale_hb = heartbeat_is_stale(ctrl.last_heartbeat_at if ctrl else None)

    try:
        with open(_BRAIN_WORKER_STATUS_FILE, "r") as f:
            data = json.load(f)
        pid = data.get("pid")
    except Exception:
        return extra

    if not pid:
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
            extra["stale_status_cleared"] = True
        except OSError:
            pass
        return extra

    liv = _worker_liveness(int(pid) if pid else None)
    if liv == "dead":
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
            extra["stale_status_cleared"] = True
        except OSError:
            pass
        return extra

    if force:
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
            extra["stale_status_cleared"] = True
        except OSError:
            pass
        return extra

    if liv == "unknown" and stale_hb:
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
            extra["stale_status_cleared"] = True
        except OSError:
            pass
        return extra

    if liv == "alive" and stale_hb:
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
            extra["stale_status_cleared"] = True
        except OSError:
            pass
        return extra

    return extra


def _brain_worker_liveness(pid: int) -> str:
    """Return 'alive' | 'dead' | 'unknown'.

    'unknown' = psutil could not inspect the PID (e.g. AccessDenied on Windows).
    Callers must NOT delete brain_worker_status.json when unknown — that broke wake.
    """
    try:
        import psutil
    except ImportError:
        return "unknown"

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return "dead"
    except psutil.AccessDenied:
        return "unknown"
    except Exception:
        return "unknown"

    try:
        if not proc.is_running():
            return "dead"
    except psutil.NoSuchProcess:
        return "dead"
    except Exception:
        return "unknown"

    try:
        name = (proc.name() or "").lower()
        cmd = " ".join(proc.cmdline() or []).lower()
    except (psutil.AccessDenied, Exception):
        return "unknown"

    if "brain_worker" in cmd or "brain_worker.py" in cmd:
        return "alive"
    if "python" in name or name in ("py.exe", "py") or name.startswith("python"):
        return "alive"
    return "dead"


def _worker_liveness(pid: int | None) -> str:
    """Prefer Docker Compose ``brain-worker`` container state when that container exists."""
    try:
        from ...services.brain_worker_docker import find_brain_worker_container, brain_worker_liveness_for_ui

        if find_brain_worker_container() is not None:
            return brain_worker_liveness_for_ui()
    except Exception:
        pass
    if pid is not None:
        return _brain_worker_liveness(int(pid))
    return "dead"


@router.get("/api/trading/brain/worker/status")
def api_brain_worker_status(request: Request, db: Session = Depends(get_db)):
    """Get brain worker status.
    
    Non-blocking: reads status file first, then checks process liveness.
    DB identity check is done but failures are tolerated to avoid blocking
    when the database is locked by the brain worker.
    """
    # Try to get identity, but don't block on DB lock
    try:
        ctx = get_identity_ctx(request, db)
        if ctx.get("demo"):
            return JSONResponse({"ok": False, "error": "Demo users cannot access worker status"}, status_code=403)
    except Exception:
        # DB likely locked — proceed anyway for status reads
        pass
    
    default_stopped = {
        "ok": True,
        "status": "stopped",
        "pid": None,
        "current_step": "",
        "current_progress": "",
        "last_cycle": {},
        "totals": {},
    }
    if not _BRAIN_WORKER_STATUS_FILE.exists():
        out = {**default_stopped}
        _merge_brain_worker_db_fields(db, out)
        _merge_trading_insight_debug_counts(db, out)
        return JSONResponse(out)

    try:
        with open(_BRAIN_WORKER_STATUS_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        out = {**default_stopped}
        _merge_brain_worker_db_fields(db, out)
        _merge_trading_insight_debug_counts(db, out)
        return JSONResponse(out)
    except (json.JSONDecodeError, OSError) as e:
        # Corrupt or partial write (e.g. worker wrote mid-read) — return stopped so UI doesn't 500
        out = {**default_stopped, "error": str(e)}
        _merge_brain_worker_db_fields(db, out)
        _merge_trading_insight_debug_counts(db, out)
        return JSONResponse(out)

    pid = data.get("pid")
    liv = _worker_liveness(int(pid) if pid else None)
    if liv == "dead":
        data["status"] = "stopped"
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
        except Exception:
            pass
    elif liv == "unknown":
        data["pid_liveness"] = "unknown"

    # Merge live queue status so UI can show pending when worker says "Queue empty"
    try:
        from app.services.trading.backtest_queue import get_queue_status
        qstatus = get_queue_status(db)
        data["queue_pending_live"] = qstatus.get("pending", 0)
        data["queue_empty_live"] = qstatus.get("queue_empty", True)
    except Exception:
        data["queue_pending_live"] = 0
        data["queue_empty_live"] = True

    _merge_brain_worker_db_fields(db, data)
    _merge_trading_insight_debug_counts(db, data)

    return JSONResponse({"ok": True, **data})


@router.post("/api/trading/brain/worker/start")
async def api_brain_worker_start(request: Request, db: Session = Depends(get_db)):
    """Start the Docker Compose ``brain-worker`` service (not a subprocess on this host)."""
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot control worker"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        body = {}

    force = bool(body.get("force"))
    interval = body.get("interval", 30)

    from ...services.brain_worker_docker import brain_worker_container_running, brain_worker_start_docker

    stale_meta = _clear_stale_brain_worker_status_if_needed(db, force)

    if brain_worker_container_running():
        return JSONResponse(
            {
                "ok": False,
                "error": "Worker already running (Docker)",
                "mode": "docker",
                **stale_meta,
            },
        )

    if _BRAIN_WORKER_STATUS_FILE.exists():
        try:
            with open(_BRAIN_WORKER_STATUS_FILE, "r") as f:
                data = json.load(f)
            pid = data.get("pid")
            if pid:
                from ...services.brain_worker_signals import (
                    get_worker_control_snapshot,
                    heartbeat_is_stale,
                )

                liv = _worker_liveness(int(pid) if pid else None)
                ctrl = get_worker_control_snapshot(db)
                stale_hb = heartbeat_is_stale(ctrl.last_heartbeat_at if ctrl else None)
                if liv == "alive" and not stale_hb:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "Worker already running",
                            "pid": pid,
                        },
                    )
                if liv == "unknown" and not stale_hb:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "Worker may already be running (PID could not be verified). Use force=true to clear status and start.",
                            "pid": pid,
                        },
                    )
        except Exception:
            pass

    if _BRAIN_WORKER_STOP_SIGNAL.exists():
        _BRAIN_WORKER_STOP_SIGNAL.unlink()
    if _BRAIN_WORKER_PAUSE_SIGNAL.exists():
        _BRAIN_WORKER_PAUSE_SIGNAL.unlink()

    try:
        from ...services.brain_worker_signals import clear_stop_requested, clear_worker_heartbeat

        clear_stop_requested(db)
        clear_worker_heartbeat(db)
        db.commit()
    except Exception as e:
        _log.warning("brain worker start: could not reset DB control row: %s", e)
        try:
            db.rollback()
        except Exception:
            pass

    res = brain_worker_start_docker()
    if not res.get("ok"):
        return JSONResponse(
            {
                "ok": False,
                "error": res.get("error", "docker_start_failed"),
                "hint": res.get("hint"),
                **stale_meta,
            },
            status_code=500,
        )
    return JSONResponse(
        {
            "ok": True,
            "mode": "docker",
            "interval_requested": interval,
            "interval_note": "Cycle interval is configured on the brain-worker service in docker-compose.yml (not from this request).",
            "docker": res,
            **stale_meta,
        }
    )


@router.post("/api/trading/brain/worker/stop")
def api_brain_worker_stop(request: Request, db: Session = Depends(get_db)):
    """Stop the brain worker gracefully."""
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot control worker"}, status_code=403)

    try:
        from ...services.brain_worker_signals import set_stop_requested

        set_stop_requested(db)
        db.commit()
    except Exception as e:
        _log.exception("brain worker stop DB failed")
        return JSONResponse({"ok": False, "error": f"Could not set DB stop: {e}"}, status_code=500)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _BRAIN_WORKER_STOP_SIGNAL.touch()
    except OSError:
        pass

    from ...services.brain_worker_docker import brain_worker_stop_docker

    dr = brain_worker_stop_docker()
    if not dr.get("ok"):
        return JSONResponse(
            {
                "ok": False,
                "error": dr.get("error", "docker_stop_failed"),
                "message": "Database stop was set; Docker stop failed.",
                "docker": dr,
            },
            status_code=500,
        )
    return JSONResponse(
        {
            "ok": True,
            "message": "Stop queued in database; brain-worker container stopped.",
            "docker": dr,
        }
    )


@router.post("/api/trading/brain/worker/pause")
def api_brain_worker_pause(request: Request, db: Session = Depends(get_db)):
    """Toggle pause state of the brain worker."""
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot control worker"}, status_code=403)
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if _BRAIN_WORKER_PAUSE_SIGNAL.exists():
        _BRAIN_WORKER_PAUSE_SIGNAL.unlink()
        return JSONResponse({"ok": True, "paused": False, "message": "Worker resumed"})
    else:
        _BRAIN_WORKER_PAUSE_SIGNAL.touch()
        return JSONResponse({"ok": True, "paused": True, "message": "Worker paused"})


@router.post("/api/trading/brain/worker/wake-cycle")
def api_brain_worker_wake_cycle(request: Request, db: Session = Depends(get_db)):
    """Signal the brain worker to skip remaining idle sleep and start the next cycle soon.

    Wake is stored in PostgreSQL (``brain_worker_control``) so it works even when the
    API and worker disagree on ``data/`` paths or ``brain_worker_status.json`` is missing.
    A legacy file in ``data/brain_worker_wake`` is also touched when possible.
    """
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot control worker"}, status_code=403)

    if _BRAIN_WORKER_PAUSE_SIGNAL.exists():
        return JSONResponse(
            {"ok": False, "error": "Worker is paused — resume first, then run next cycle."},
            status_code=400,
        )

    from ...services.brain_worker_signals import set_wake_requested

    try:
        set_wake_requested(db)
        db.commit()
    except Exception as e:
        _log.exception("brain wake DB failed")
        return JSONResponse({"ok": False, "error": f"Could not queue wake: {e}"}, status_code=500)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _BRAIN_WORKER_WAKE_SIGNAL.touch()
    except OSError:
        pass

    notes: list[str] = []
    if _BRAIN_WORKER_STATUS_FILE.exists():
        try:
            with open(_BRAIN_WORKER_STATUS_FILE, "r") as f:
                sdata = json.load(f)
            pid = sdata.get("pid")
            if pid:
                liv = _worker_liveness(int(pid) if pid else None)
                if liv == "dead":
                    notes.append(
                        "Local status PID looks dead; DB wake is still set for any worker using this database."
                    )
                elif liv == "unknown":
                    notes.append("Could not verify local PID; DB wake is authoritative.")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            notes.append("Could not read local status file; DB wake is authoritative.")
    else:
        notes.append("No brain_worker_status.json — DB wake will still reach the worker on the same database.")

    from ...services.brain_worker_signals import (
        get_worker_control_snapshot,
        heartbeat_is_stale,
    )

    ctrl = get_worker_control_snapshot(db)
    worker_heartbeat_fresh = bool(
        ctrl and ctrl.last_heartbeat_at and not heartbeat_is_stale(ctrl.last_heartbeat_at)
    )
    warnings: list[str] = []
    if not worker_heartbeat_fresh:
        warnings.append(
            "No recent worker heartbeat was detected. A separate process must be running "
            "(Start on this page runs scripts/brain_worker.py) with the same DATABASE_URL as this app. "
            "Otherwise the wake is stored but nothing will act on it. "
            'To drain the backtest queue without the worker, use "Process queue on server".'
        )

    last_hb = None
    if ctrl and ctrl.last_heartbeat_at:
        last_hb = ctrl.last_heartbeat_at.isoformat() + "Z"

    return JSONResponse(
        {
            "ok": True,
            "message": "Wake queued in database. The worker checks this every few seconds while idle.",
            "notes": notes,
            "worker_heartbeat_fresh": worker_heartbeat_fresh,
            "warnings": warnings,
            "last_heartbeat_at": last_hb,
        },
    )


@router.get("/api/trading/brain/worker/recent-activity")
def api_brain_worker_recent_activity(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    """Get recent learning activity for the brain worker dashboard."""
    from ...models.trading import LearningEvent
    from datetime import datetime, timedelta
    
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot access activity"}, status_code=403)
    
    cutoff = datetime.utcnow() - timedelta(hours=24)
    
    events = (
        db.query(LearningEvent)
        .filter(LearningEvent.created_at >= cutoff)
        .order_by(LearningEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    
    activity = []
    for e in events:
        activity.append({
            "id": e.id,
            "type": e.event_type,
            "summary": e.description[:200] if e.description else "",
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })
    
    return JSONResponse({"ok": True, "activity": activity})


@router.get("/api/trading/brain/worker/queue-debug")
def api_brain_worker_queue_debug(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(30, ge=1, le=200),
):
    """First N scan_pattern IDs eligible for the backtest queue (same rules as the worker).

    Signed-in users only (not demo). For debugging 'pending' vs dashboard counts.
    """
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot access queue debug"}, status_code=403)

    from ...services.trading.backtest_queue import get_pending_patterns, get_retest_interval_days

    ids = get_pending_patterns(db, limit=limit, ids_only=True)
    rd = get_retest_interval_days()
    return JSONResponse(
        {
            "ok": True,
            "eligible_pending_pattern_ids": ids,
            "limit": limit,
            "retest_interval_days": rd,
            "note": (
                f"Same eligibility as worker queue: active patterns that are boosted, "
                f"never backtested, or last_backtest older than {rd} days."
            ),
        }
    )


@router.post("/api/trading/brain/worker/run-queue-batch")
async def api_brain_worker_run_queue_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Run a single ``_auto_backtest_from_queue`` batch in the web process (BackgroundTasks).

    Does not require ``brain_worker.py``. Useful when the worker is idle or not running.
    Optional JSON body: ``{\"batch_size\": 20}`` (must be positive int; else uses settings default).
    """
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot run queue batch"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_bs = body.get("batch_size")
    batch_size = None
    if isinstance(raw_bs, int) and raw_bs > 0:
        batch_size = raw_bs
    elif isinstance(raw_bs, str) and raw_bs.isdigit():
        batch_size = int(raw_bs)

    from ...db import SessionLocal

    def _bg_queue_batch(user_id: int, bs: int | None) -> None:
        sdb = SessionLocal()
        try:
            from ...services.trading.learning import _auto_backtest_from_queue

            result = _auto_backtest_from_queue(sdb, user_id, batch_size=bs)
            _log.info("[brain_worker] run-queue-batch finished: %s", result)
        except Exception as e:
            _log.exception("[brain_worker] run-queue-batch failed: %s", e)
        finally:
            sdb.close()

    background_tasks.add_task(_bg_queue_batch, ctx["user_id"], batch_size)
    return JSONResponse(
        {
            "ok": True,
            "message": (
                "Backtest queue batch started in background. "
                "Check server logs for lines starting with [learning] Queue backtest."
            ),
        }
    )


# ── Config profiles ──────────────────────────────────────────────────────────

@router.get("/api/trading/brain/config/profiles")
def api_config_profiles(request: Request, db: Session = Depends(get_db)):
    """List available config profiles with their settings."""
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot access config profiles"}, status_code=403)
    from ...services.trading.config_profiles import list_profiles

    return JSONResponse({"ok": True, "profiles": list_profiles()})


@router.post("/api/trading/brain/config/apply-profile")
async def api_apply_config_profile(request: Request, db: Session = Depends(get_db)):
    """Apply a named config profile to the running Settings object.

    Body: ``{"profile": "conservative" | "moderate" | "aggressive"}``

    Applies overrides to the in-memory ``Settings()`` singleton for the
    current process.  Changes persist until the server restarts — to make
    them permanent, set the corresponding env vars or ``.env`` entries.
    """
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot modify config"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    profile_name = body.get("profile", "").strip().lower()
    from ...services.trading.config_profiles import PROFILES

    if profile_name not in PROFILES:
        return JSONResponse(
            {"ok": False, "error": f"Unknown profile '{profile_name}'. Choose from: {list(PROFILES.keys())}"},
            status_code=400,
        )

    from ...config import Settings

    settings = Settings()
    profile = PROFILES[profile_name]
    applied: dict[str, Any] = {}
    for key, value in profile.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
            applied[key] = value

    _log.info("[config_profiles] Applied profile '%s': %d settings", profile_name, len(applied))
    return JSONResponse({"ok": True, "profile": profile_name, "applied": applied})


# ── Notification preferences ─────────────────────────────────────────────────

@router.get("/api/trading/brain/notification-preferences")
def api_notification_preferences(request: Request, db: Session = Depends(get_db)):
    """Return current per-channel, per-tier notification preferences."""
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot access notifications"}, status_code=403)
    from ...services.sms_service import get_notification_preferences

    return JSONResponse({"ok": True, "preferences": get_notification_preferences()})


@router.post("/api/trading/brain/notification-preferences")
def api_update_notification_preferences(request: Request, db: Session = Depends(get_db)):
    """Update per-channel, per-tier notification preferences.

    Body: ``{"preferences": {"telegram": {"A": true, "B": true, "C": false}, ...}}``
    """
    import asyncio

    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot modify notifications"}, status_code=403)
    try:
        body = asyncio.get_event_loop().run_until_complete(request.json())
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    prefs = body.get("preferences")
    if not isinstance(prefs, dict):
        return JSONResponse({"ok": False, "error": "preferences must be a dict"}, status_code=400)

    from ...services.sms_service import set_notification_preferences

    set_notification_preferences(prefs)
    return JSONResponse({"ok": True, "message": "Preferences updated"})


@router.post("/api/notifications/subscribe")
def api_push_subscribe(request: Request, db: Session = Depends(get_db)):
    """Register a Web Push subscription for the current user.

    Body: PushSubscription JSON from the browser (``endpoint``, ``keys``).
    """
    import asyncio

    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo"}, status_code=403)
    try:
        body = asyncio.get_event_loop().run_until_complete(request.json())
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    if not body.get("endpoint"):
        return JSONResponse({"ok": False, "error": "Missing endpoint in subscription"}, status_code=400)

    from ...services.sms_service import register_push_subscription

    register_push_subscription(body)
    return JSONResponse({"ok": True, "message": "Push subscription registered"})
