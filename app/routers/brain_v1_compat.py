"""REST + WS compatibility for external Brain UIs (e.g. Chill on :3000).

Those clients often call ``/api/v1/brain-*`` on a separate API port. CHILI's native
worker control lives under ``/api/brain/trading/worker/*`` and ``/api/trading/brain/worker/*``.

If your SPA targets ``localhost:3333``, either proxy those paths to CHILI or set
``VITE_API_URL`` / equivalent to ``https://localhost:8000`` (CORS is already permissive).
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..db import DATA_DIR
from ..deps import get_db, get_identity_ctx

logger = logging.getLogger(__name__)

router = APIRouter(tags=["brain-v1-compat"])

_STATUS_FILE = DATA_DIR / "brain_worker_status.json"
_PAUSE_SIGNAL = DATA_DIR / "brain_worker_pause"
_WAKE_SIGNAL = DATA_DIR / "brain_worker_wake"


def _read_worker_json() -> dict:
    if not _STATUS_FILE.exists():
        return {}
    try:
        with open(_STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


@router.get("/api/v1/brain-status")
def v1_brain_status():
    """Lightweight cycle + worker snapshot (no DB required for core fields)."""
    from ..services.trading.learning import get_learning_status

    ls = get_learning_status()
    data = _read_worker_json()
    return JSONResponse(
        {
            "ok": True,
            "success": True,
            "cycle_in_progress": bool(ls.get("running")),
            "learning": ls,
            "worker": {
                "status": data.get("status", "stopped"),
                "current_step": data.get("current_step", ""),
                "current_progress": data.get("current_progress", ""),
                "pid": data.get("pid"),
            },
        }
    )


@router.get("/api/v1/brain-worker-stats")
def v1_brain_worker_stats(db: Session = Depends(get_db)):
    """Worker totals from status file plus live queue counts."""
    from ..services.trading.backtest_queue import get_queue_status

    data = _read_worker_json()
    totals = data.get("totals") or {}
    try:
        q = get_queue_status(db)
    except Exception as e:
        logger.warning("[brain-v1] queue status failed: %s", e)
        q = {"pending": 0, "boosted": 0, "total": 0, "queue_empty": True}
    return JSONResponse(
        {
            "ok": True,
            "success": True,
            "totals": totals,
            "queue": q,
            # Common aliases for external dashboards
            "queued": q.get("pending", 0),
            "boosted": q.get("boosted", 0),
        }
    )


@router.get("/api/v1/brain-logs")
def v1_brain_logs(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    from ..models.trading import LearningEvent

    try:
        ctx = get_identity_ctx(request, db)
        if ctx.get("demo"):
            return JSONResponse({"ok": False, "error": "Demo"}, status_code=403)
    except Exception as e:
        logger.warning("[brain-v1] brain-logs identity failed: %s", e)
        return JSONResponse({"ok": False, "error": "Service busy"}, status_code=503)

    cutoff = datetime.utcnow() - timedelta(hours=24)
    events = (
        db.query(LearningEvent)
        .filter(LearningEvent.created_at >= cutoff)
        .order_by(LearningEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return _logs_payload(events)


def _logs_payload(events) -> JSONResponse:
    logs = []
    for e in events:
        logs.append(
            {
                "id": e.id,
                "type": e.event_type,
                "summary": (e.description or "")[:500],
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
        )
    return JSONResponse({"ok": True, "success": True, "logs": logs, "activity": logs})


@router.get("/api/v1/brain-pending-items")
def v1_brain_pending_items(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
):
    from ..services.trading.backtest_queue import get_pending_patterns

    patterns = get_pending_patterns(db, limit=limit)
    items = []
    for p in patterns:
        items.append(
            {
                "id": p.id,
                "name": p.name,
                "priority": p.backtest_priority,
                "origin": p.origin,
                "active": p.active,
                "last_backtest_at": p.last_backtest_at.isoformat() if p.last_backtest_at else None,
            }
        )
    return JSONResponse({"ok": True, "success": True, "items": items, "count": len(items)})


@router.api_route("/api/v1/brain-next-cycle", methods=["GET", "POST", "OPTIONS"])
def v1_brain_next_cycle(request: Request, db: Session = Depends(get_db)):
    """Skip worker idle sleep — same as ``brain_worker_wake`` signal.

    Touches the wake file whenever the worker is not paused, even if the status
    file is missing (so we never 500 solely because psutil disagrees with disk).
    External SPAs often expect HTTP 200 with ``success: true``.
    """
    if request.method == "OPTIONS":
        return JSONResponse(content={})

    # External SPAs (e.g. Chill on :3000) do not send chili_device_token — that cookie is scoped to
    # https://localhost:8000, not http://localhost:3000 (different origins).
    # Optional BRAIN_V1_WAKE_SECRET + header X-Chili-Brain-Wake-Secret authorizes wake without cookies.
    _wake_hdr = "X-Chili-Brain-Wake-Secret"
    configured = (settings.brain_v1_wake_secret or "").strip()
    supplied = (request.headers.get(_wake_hdr) or "").strip()
    secret_ok = bool(configured) and secrets.compare_digest(supplied, configured)

    if not secret_ok:
        try:
            ctx = get_identity_ctx(request, db)
            if ctx.get("demo"):
                return JSONResponse({"ok": False, "success": False, "error": "Demo"}, status_code=403)
        except Exception as e:
            logger.warning("[brain-v1] next-cycle identity failed: %s", e)
            if configured:
                return JSONResponse(
                    {
                        "ok": False,
                        "success": False,
                        "error": (
                            "Unauthorized: Chill cannot use your CHILI cookie from another port. "
                            "Add BRAIN_V1_WAKE_SECRET to CHILI .env and send the same value in header "
                            f"{_wake_hdr} from the SPA, or proxy the SPA under https://localhost:8000."
                        ),
                    },
                    status_code=401,
                )
            return JSONResponse(
                {"ok": False, "success": False, "error": "Service busy"},
                status_code=503,
            )

    if _PAUSE_SIGNAL.exists():
        return JSONResponse(
            {"ok": False, "success": False, "error": "Worker is paused"},
            status_code=400,
        )

    from ..services.brain_worker_signals import set_wake_requested

    try:
        set_wake_requested(db)
        db.commit()
    except Exception as e:
        logger.exception("[brain-v1] wake DB failed")
        return JSONResponse(
            {"ok": False, "success": False, "error": str(e)},
            status_code=500,
        )

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _WAKE_SIGNAL.touch()
    except OSError as e:
        logger.warning("[brain-v1] wake file touch failed (DB wake still set): %s", e)

    return JSONResponse(
        {
            "ok": True,
            "success": True,
            "queued": True,
            "message": "Wake queued in database and file; worker polls DB while idle.",
        }
    )


@router.websocket("/ws")
async def v1_brain_compat_websocket(websocket: WebSocket):
    """Minimal socket so clients probing ``ws://host/ws`` get a connection (not refused)."""
    await websocket.accept()
    await websocket.send_json({"ok": True, "type": "connected", "service": "chili"})
    try:
        while True:
            await websocket.receive_text()
            await websocket.send_json({"ok": True, "type": "ack"})
    except WebSocketDisconnect:
        pass
