"""Read-only trading observability: session (non-guest) or optional Bearer secret.

No mutations, no broker credentials, no raw settings secrets in JSON.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...config import settings
from ...deps import get_db, get_identity_ctx
from ...json_safe import to_jsonable
from ...models.trading import BrainBatchJob, ScanResult
from ...services.trading.batch_job_constants import JOB_PATTERN_IMMINENT_SCANNER

router = APIRouter(tags=["trading-inspect"])
_log = logging.getLogger(__name__)


def trading_inspect_ctx(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Allow paired/session user, or valid Bearer when ``trading_inspect_bearer_secret`` is set."""
    ctx = get_identity_ctx(request, db)
    secret = (getattr(settings, "trading_inspect_bearer_secret", "") or "").strip()
    auth_h = (request.headers.get("authorization") or "").strip()
    if secret and auth_h.lower().startswith("bearer "):
        tok = auth_h[7:].strip()
        if secrets.compare_digest(tok, secret):
            return ctx
    if ctx.get("is_guest"):
        raise HTTPException(
            status_code=401,
            detail={"ok": False, "error": "inspect_auth_required", "message": "Sign in or use inspect Bearer token."},
        )
    return ctx


@router.get("/api/trading/inspect/opportunity-board")
def inspect_opportunity_board(
    request: Request,
    db: Session = Depends(get_db),
    ctx: dict[str, Any] = Depends(trading_inspect_ctx),
    debug: int = Query(0, ge=0, le=1),
    include_research: int = Query(0, ge=0, le=1),
):
    from ...services.trading.opportunity_board import get_trading_opportunity_board

    data = get_trading_opportunity_board(
        db,
        ctx["user_id"],
        include_research=bool(include_research),
        include_debug=(debug == 1),
    )
    return JSONResponse(to_jsonable({"ok": True, "board": data}))


@router.get("/api/trading/inspect/imminent-dry-run")
def inspect_imminent_dry_run(
    request: Request,
    db: Session = Depends(get_db),
    ctx: dict[str, Any] = Depends(trading_inspect_ctx),
):
    from ...services.trading.pattern_imminent_alerts import run_pattern_imminent_scan

    out = run_pattern_imminent_scan(db, ctx["user_id"], dry_run=True)
    return JSONResponse(to_jsonable({"ok": True, "imminent_dry_run": out}))


@router.get("/api/trading/inspect/sources")
def inspect_sources(
    request: Request,
    db: Session = Depends(get_db),
    ctx: dict[str, Any] = Depends(trading_inspect_ctx),
):
    from ...services.trading.learning import get_prediction_swr_cache_meta
    from ...services.trading.prescreen_job import count_active_global_candidates
    from ...services.trading.trading_source_freshness import collect_source_freshness, compute_board_data_as_of

    sf = collect_source_freshness(db)
    dao, keys = compute_board_data_as_of(sf)
    pred_meta = get_prediction_swr_cache_meta()
    scan_ct = int(db.query(func.count(ScanResult.id)).scalar() or 0)
    prescreen_ct = count_active_global_candidates(db)

    last_imminent: dict[str, Any] | None = None
    try:
        row = (
            db.query(BrainBatchJob)
            .filter(BrainBatchJob.job_type == JOB_PATTERN_IMMINENT_SCANNER)
            .order_by(BrainBatchJob.started_at.desc())
            .first()
        )
        if row:
            err = (row.error_message or "")[:240]
            last_imminent = {
                "status": row.status,
                "started_at_utc": row.started_at.isoformat() if row.started_at else None,
                "ended_at_utc": row.ended_at.isoformat() if row.ended_at else None,
                "error_message": err or None,
            }
    except Exception as e:
        _log.debug("[inspect_sources] last imminent job: %s", e)

    return JSONResponse(
        to_jsonable(
            {
                "ok": True,
                "source_freshness": sf,
                "data_as_of": dao,
                "data_as_of_min_keys": keys,
                "predictions_cache_meta": pred_meta,
                "scan_result_row_count": scan_ct,
                "prescreen_active_candidate_count": prescreen_ct,
                "last_pattern_imminent_job": last_imminent,
            }
        )
    )


@router.get("/api/trading/inspect/health")
def inspect_health(
    request: Request,
    db: Session = Depends(get_db),
    ctx: dict[str, Any] = Depends(trading_inspect_ctx),
):
    """Lightweight dependency sanity for operators (no full board build)."""
    from ...services import trading_scheduler
    from ...services.trading.learning import get_prediction_swr_cache_meta
    from ...services.trading.trading_source_freshness import collect_source_freshness, compute_board_data_as_of

    degraded: list[str] = []
    sf = collect_source_freshness(db)
    dao, _ = compute_board_data_as_of(sf)
    if dao is None:
        degraded.append("no_composite_data_as_of")
    if not any(sf.get(k) for k in sf):
        degraded.append("all_source_timestamps_null")

    pred = get_prediction_swr_cache_meta()
    if (pred.get("cached_result_count") or 0) == 0:
        degraded.append("predictions_cache_empty")

    scan_ct = int(db.query(func.count(ScanResult.id)).scalar() or 0)
    if scan_ct == 0:
        degraded.append("no_scan_results_in_db")

    sched: dict[str, Any] = {}
    try:
        st = trading_scheduler.get_scheduler_info()
        if isinstance(st, dict):
            sched = {
                "running": st.get("running"),
                "jobs_sample": (st.get("jobs") or [])[:12],
                "role": getattr(settings, "chili_scheduler_role", None),
            }
    except Exception as e:
        _log.debug("[inspect_health] scheduler info: %s", e)
        sched = {"error": "unavailable"}

    return JSONResponse(
        to_jsonable(
            {
                "ok": True,
                "degraded": degraded,
                "source_freshness": sf,
                "data_as_of": dao,
                "predictions_cache_meta": pred,
                "scan_result_row_count": scan_ct,
                "scheduler": sched,
            }
        )
    )
