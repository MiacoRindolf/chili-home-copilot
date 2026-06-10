"""Replay v2 API — run/inspect high-fidelity momentum replays from the web UI.

The engine takes minutes (per-symbol bar fetches), so POST /run starts a background
THREAD (single-flight: one replay at a time) and the page polls /status. Results
persist as JSON under the shared /app/data mount, so runs are visible from any
container and survive restarts.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading/momentum/replay", tags=["trading-replay"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# single-flight job state (module-level; one replay at a time — the engine is heavy)
_lock = threading.Lock()
_job: dict[str, Any] = {"state": "idle", "date": None, "started_at": None, "error": None}


def _run_in_thread(date: str) -> None:
    global _job
    try:
        from ...services.trading.momentum_neural.replay_v2 import run_replay

        result = run_replay(date)
        with _lock:
            _job = {
                "state": "done" if not result.get("error") else "error",
                "date": date,
                "started_at": _job.get("started_at"),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": result.get("error"),
                "total_usd": result.get("total_usd"),
                "n_trades": len(result.get("trades") or []),
            }
    except Exception as exc:
        logger.warning("[replay_api] run failed for %s", date, exc_info=True)
        with _lock:
            _job = {"state": "error", "date": date, "started_at": _job.get("started_at"),
                    "finished_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)[:300]}


@router.post("/run")
def run_replay_endpoint(payload: dict):
    date = str((payload or {}).get("date") or "").strip()
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    with _lock:
        if _job.get("state") == "running":
            return {"ok": False, "error": "replay_already_running", "job": _job}
        _job.clear()
        _job.update({"state": "running", "date": date,
                     "started_at": datetime.now(timezone.utc).isoformat(), "error": None})
    t = threading.Thread(target=_run_in_thread, args=(date,), name=f"replay-v2-{date}", daemon=True)
    t.start()
    return {"ok": True, "job": dict(_job)}


@router.get("/status")
def replay_status():
    with _lock:
        return {"ok": True, "job": dict(_job)}


@router.get("/list")
def replay_list():
    from ...services.trading.momentum_neural.replay_v2 import list_results

    return {"ok": True, "results": list_results()}


@router.get("/result/{date}")
def replay_result(date: str):
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    from ...services.trading.momentum_neural.replay_v2 import load_result

    r = load_result(date)
    if r is None:
        raise HTTPException(status_code=404, detail="no result for this date — run it first")
    return {"ok": True, "result": r}
