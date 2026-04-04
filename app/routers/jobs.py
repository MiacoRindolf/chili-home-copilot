"""Jobs domain: batch job metrics and management (brain_batch_jobs)."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..services.trading.batch_job_constants import JOB_SCHEDULER_WORKER_HEARTBEAT
from ..services.trading.brain_batch_job_log import (
    batch_job_summary,
    fetch_batch_jobs_page,
    fetch_latest_ok_payload,
)
from ..services.trading.scanner import run_crypto_breakout_scan

router = APIRouter(tags=["jobs"])


def _require_user(request: Request, db: Session) -> dict:
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest") or ctx.get("user_id") is None:
        raise HTTPException(status_code=403, detail="Sign in required")
    return ctx


@router.get("/jobs", response_class=RedirectResponse)
def page_jobs_redirect():
    """Backward-compatible short path."""
    return RedirectResponse(url="/app/jobs", status_code=307)


@router.get("/app/jobs")
def page_jobs_metrics(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return RedirectResponse(url="/chat", status_code=302)
    return request.app.state.templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "title": "Jobs — metrics",
            "active_nav": "jobs",
        },
    )


@router.get("/app/jobs/manage")
def page_jobs_manage(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return RedirectResponse(url="/chat", status_code=302)
    return request.app.state.templates.TemplateResponse(
        request,
        "jobs_manage.html",
        {
            "title": "Jobs — manage",
            "active_nav": "jobs_manage",
        },
    )


def _row_to_item(row) -> dict:
    return {
        "id": row.id,
        "job_type": row.job_type,
        "status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "error_message": (row.error_message or "")[:500] if row.error_message else None,
        "meta_json": row.meta_json,
        "has_payload": row.payload_json is not None,
    }


@router.get("/api/jobs/summary")
def api_jobs_summary(
    request: Request,
    db: Session = Depends(get_db),
    hours: int = Query(168, ge=1, le=8760),
):
    _require_user(request, db)
    rows = batch_job_summary(db, hours=hours)
    hb_payload, hb_end, _hb_meta = fetch_latest_ok_payload(db, JOB_SCHEDULER_WORKER_HEARTBEAT)
    heartbeat = None
    if hb_end:
        heartbeat = {
            "last_ended_at": hb_end.isoformat(),
            "meta": hb_payload,
        }
    return JSONResponse({"ok": True, "hours": hours, "by_type": rows, "scheduler_heartbeat": heartbeat})


@router.get("/api/jobs/batch")
def api_jobs_batch(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    job_type: str | None = Query(None),
    status: str | None = Query(None),
):
    _require_user(request, db)
    rows, total = fetch_batch_jobs_page(
        db, limit=limit, offset=offset, job_type=job_type, status=status
    )
    return JSONResponse(
        {
            "ok": True,
            "total": total,
            "items": [_row_to_item(r) for r in rows],
        }
    )


@router.post("/api/jobs/trigger/crypto-breakout")
def api_trigger_crypto_breakout(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Queue a crypto breakout scan in the web process (writes brain_batch_jobs when done)."""
    _require_user(request, db)

    def _run():
        run_crypto_breakout_scan(max_results=20, batch_job_id=None, skip_db_ttl_check=True)

    background_tasks.add_task(_run)
    return JSONResponse(
        {"ok": True, "message": "Crypto breakout scan queued; check Jobs for a new batch row."}
    )


@router.get("/api/jobs/batch/{job_id}/payload")
def api_job_payload(request: Request, job_id: str, db: Session = Depends(get_db)):
    _require_user(request, db)
    from ..models.trading import BrainBatchJob

    row = db.query(BrainBatchJob).filter(BrainBatchJob.id == job_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(
        {
            "ok": True,
            "id": row.id,
            "job_type": row.job_type,
            "status": row.status,
            "meta_json": row.meta_json,
            "payload_json": row.payload_json,
        }
    )
