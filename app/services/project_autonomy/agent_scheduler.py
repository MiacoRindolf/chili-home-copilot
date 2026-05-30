"""Lightweight scheduler for Project Autopilot Agent OS cycles."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from ...config import settings
from ...db import SessionLocal
from . import orchestrator

logger = logging.getLogger(__name__)

SCHEDULER_ROLE_NONE = "none"
SCHEDULER_ROLE_UNSET = "all"
SCHEDULER_THREAD_NAME = "project-autopilot-agent-scheduler"
SCHEDULER_WORKER_THREAD_PREFIX = "project-autopilot-agent-run"
SCHEDULER_MIN_INTERVAL_SECONDS = 30
SCHEDULER_DEFAULT_INTERVAL_SECONDS = 60
SCHEDULER_DEFAULT_DUE_LIMIT = 3
SCHEDULER_DEFAULT_MAX_WORKERS = 2
SCHEDULER_STOP_JOIN_SECONDS = 5.0
SCHEDULER_INFO_LAST_POLL_AT = "last_poll_at"
SCHEDULER_INFO_NEXT_POLL_AT = "next_poll_at"
SCHEDULER_INFO_LAST_RESULT = "last_result"
SCHEDULER_INFO_LAST_ERROR = "last_error"
SCHEDULER_INFO_ACTIVE_WORKERS = "active_workers"
SCHEDULER_INFO_MAX_WORKERS = "max_workers"
SCHEDULER_RESULT_STARTED = "started"
SCHEDULER_RESULT_SKIPPED = "skipped"
SCHEDULER_RESULT_RUNS = "runs"
SCHEDULER_RESULT_CHECKED = "checked"
SCHEDULER_RESULT_SOURCE = "source"
SCHEDULER_RESULT_SOURCE_AUTO = "auto_poll"
SCHEDULER_RESULT_SOURCE_MANUAL = "manual_wake"
SCHEDULER_RESULT_WORKER_STARTED = "worker_started"
SCHEDULER_RESULT_WORKER_DEFERRED = "worker_deferred"
SCHEDULER_WORKER_START_STARTED = "started"
SCHEDULER_WORKER_START_REASON = "reason"
SCHEDULER_WORKER_START_REASON_ALREADY_RUNNING = "already_running"
SCHEDULER_WORKER_START_REASON_AT_CAPACITY = "worker_capacity"

_lock = threading.RLock()
_stop_event = threading.Event()
_thread: threading.Thread | None = None
_last_poll_at: datetime | None = None
_last_result: dict[str, Any] | None = None
_last_error: str | None = None
_active_worker_run_ids: set[str] = set()


def should_start_standalone_scheduler(scheduler_role: str | None) -> bool:
    """Return true when Autopilot should self-schedule outside APScheduler."""
    role = (scheduler_role or SCHEDULER_ROLE_UNSET).strip().lower()
    return role == SCHEDULER_ROLE_NONE


def scheduler_info() -> dict[str, Any]:
    with _lock:
        running = _thread is not None and _thread.is_alive()
        last_poll_at = _last_poll_at
        last_result = dict(_last_result or {})
        last_error = _last_error
        active_workers = len(_active_worker_run_ids)
    interval = _interval_seconds()
    next_poll_at = (
        last_poll_at + timedelta(seconds=interval)
        if running and last_poll_at is not None
        else None
    )
    return {
        "running": running,
        "thread_name": SCHEDULER_THREAD_NAME if running else None,
        "interval_seconds": interval,
        "enabled": bool(getattr(settings, "project_autonomy_agent_scheduler_enabled", True)),
        SCHEDULER_INFO_LAST_POLL_AT: last_poll_at.isoformat() if last_poll_at else None,
        SCHEDULER_INFO_NEXT_POLL_AT: next_poll_at.isoformat() if next_poll_at else None,
        SCHEDULER_INFO_LAST_RESULT: last_result,
        SCHEDULER_INFO_LAST_ERROR: last_error,
        SCHEDULER_INFO_ACTIVE_WORKERS: active_workers,
        SCHEDULER_INFO_MAX_WORKERS: _max_workers(),
    }


def start_standalone_scheduler(scheduler_role: str | None = None) -> dict[str, Any]:
    """Start the local Agent OS scheduler for API-only desktop runs."""
    if not getattr(settings, "project_autonomy_agent_scheduler_enabled", True):
        return {"started": False, "reason": "disabled", **scheduler_info()}
    if not should_start_standalone_scheduler(scheduler_role):
        return {"started": False, "reason": "delegated_to_apscheduler", **scheduler_info()}

    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return {"started": False, "reason": "already_running", **scheduler_info()}
        _stop_event.clear()
        _thread = threading.Thread(
            target=_scheduler_loop,
            name=SCHEDULER_THREAD_NAME,
            daemon=True,
        )
        _thread.start()
    info = scheduler_info()
    logger.info(
        "[project_autonomy_scheduler] started interval_seconds=%s",
        info["interval_seconds"],
    )
    return {"started": True, **info}


def stop_standalone_scheduler() -> None:
    global _thread
    with _lock:
        thread = _thread
        _stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=SCHEDULER_STOP_JOIN_SECONDS)
    with _lock:
        if _thread is thread:
            _thread = None
    logger.info("[project_autonomy_scheduler] stopped")


def run_once() -> dict[str, Any]:
    """Poll due profiles once and start workers for any created runs."""
    if not getattr(settings, "project_autonomy_agent_scheduler_enabled", True):
        result = {
            SCHEDULER_RESULT_STARTED: 0,
            SCHEDULER_RESULT_SKIPPED: [],
            SCHEDULER_RESULT_RUNS: [],
            SCHEDULER_RESULT_CHECKED: 0,
            "enabled": False,
        }
        _record_poll_result(result)
        return result

    db = SessionLocal()
    try:
        result = orchestrator.run_due_agent_cycles(db, limit=_due_limit())
        worker_started = 0
        worker_deferred: list[dict[str, Any]] = []
        for run in result.get("runs", []):
            run_id = str(run.get("run_id") or "").strip()
            if run_id:
                worker_result = start_worker(run_id)
                if worker_result.get(SCHEDULER_WORKER_START_STARTED):
                    worker_started += 1
                else:
                    worker_deferred.append(
                        {
                            "run_id": run_id,
                            "reason": worker_result.get(SCHEDULER_WORKER_START_REASON),
                        }
                    )
        result[SCHEDULER_RESULT_WORKER_STARTED] = worker_started
        result[SCHEDULER_RESULT_WORKER_DEFERRED] = worker_deferred
        if result.get("started") or result.get("skipped"):
            logger.info("[project_autonomy_scheduler] due cycles: %s", result)
        _record_poll_result(result)
        return result
    except Exception as exc:
        logger.error("[project_autonomy_scheduler] due cycle poll failed: %s", exc)
        result = {
            SCHEDULER_RESULT_STARTED: 0,
            SCHEDULER_RESULT_SKIPPED: [],
            SCHEDULER_RESULT_RUNS: [],
            SCHEDULER_RESULT_CHECKED: 0,
            "error": str(exc),
        }
        _record_poll_result(result, error=str(exc))
        return result
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def record_manual_wake_result(result: dict[str, Any]) -> None:
    """Expose route-triggered wake results through scheduler telemetry."""
    _record_poll_result(result, source=SCHEDULER_RESULT_SOURCE_MANUAL)


def start_worker(run_id: str) -> dict[str, Any]:
    """Start one Autopilot worker without exceeding local Agent OS capacity."""
    clean_run_id = str(run_id or "").strip()
    if not clean_run_id:
        return {
            SCHEDULER_WORKER_START_STARTED: False,
            SCHEDULER_WORKER_START_REASON: "missing_run_id",
        }
    with _lock:
        if clean_run_id in _active_worker_run_ids:
            return {
                SCHEDULER_WORKER_START_STARTED: False,
                SCHEDULER_WORKER_START_REASON: SCHEDULER_WORKER_START_REASON_ALREADY_RUNNING,
                SCHEDULER_INFO_ACTIVE_WORKERS: len(_active_worker_run_ids),
                SCHEDULER_INFO_MAX_WORKERS: _max_workers(),
            }
        if len(_active_worker_run_ids) >= _max_workers():
            return {
                SCHEDULER_WORKER_START_STARTED: False,
                SCHEDULER_WORKER_START_REASON: SCHEDULER_WORKER_START_REASON_AT_CAPACITY,
                SCHEDULER_INFO_ACTIVE_WORKERS: len(_active_worker_run_ids),
                SCHEDULER_INFO_MAX_WORKERS: _max_workers(),
            }
        _active_worker_run_ids.add(clean_run_id)
    thread = threading.Thread(
        target=_run_worker,
        args=(clean_run_id,),
        name=f"{SCHEDULER_WORKER_THREAD_PREFIX}-{clean_run_id}",
        daemon=True,
    )
    thread.start()
    return {
        SCHEDULER_WORKER_START_STARTED: True,
        "run_id": clean_run_id,
        SCHEDULER_INFO_ACTIVE_WORKERS: active_worker_count(),
        SCHEDULER_INFO_MAX_WORKERS: _max_workers(),
    }


def active_worker_count() -> int:
    with _lock:
        return len(_active_worker_run_ids)


def _scheduler_loop() -> None:
    while not _stop_event.is_set():
        run_once()
        _stop_event.wait(_interval_seconds())


def _interval_seconds() -> int:
    raw = getattr(
        settings,
        "project_autonomy_agent_scheduler_interval_seconds",
        SCHEDULER_DEFAULT_INTERVAL_SECONDS,
    )
    return max(SCHEDULER_MIN_INTERVAL_SECONDS, int(raw or SCHEDULER_DEFAULT_INTERVAL_SECONDS))


def _due_limit() -> int:
    raw = getattr(
        settings,
        "project_autonomy_agent_scheduler_due_limit",
        SCHEDULER_DEFAULT_DUE_LIMIT,
    )
    return max(1, int(raw or SCHEDULER_DEFAULT_DUE_LIMIT))


def _max_workers() -> int:
    raw = getattr(
        settings,
        "project_autonomy_agent_scheduler_max_workers",
        SCHEDULER_DEFAULT_MAX_WORKERS,
    )
    return max(1, int(raw or SCHEDULER_DEFAULT_MAX_WORKERS))


def _record_poll_result(
    result: dict[str, Any],
    *,
    error: str | None = None,
    source: str = SCHEDULER_RESULT_SOURCE_AUTO,
) -> None:
    summary = {
        SCHEDULER_RESULT_STARTED: int(result.get(SCHEDULER_RESULT_STARTED) or 0),
        SCHEDULER_RESULT_CHECKED: int(result.get(SCHEDULER_RESULT_CHECKED) or 0),
        "skipped_count": len(result.get(SCHEDULER_RESULT_SKIPPED) or []),
        "run_count": len(result.get(SCHEDULER_RESULT_RUNS) or []),
        SCHEDULER_RESULT_WORKER_STARTED: (
            int(result.get(SCHEDULER_RESULT_WORKER_STARTED) or 0)
            if SCHEDULER_RESULT_WORKER_STARTED in result
            else int(result.get(SCHEDULER_RESULT_STARTED) or 0)
        ),
        "worker_deferred_count": len(
            result.get(SCHEDULER_RESULT_WORKER_DEFERRED) or []
        ),
        SCHEDULER_RESULT_SOURCE: source,
    }
    if error:
        summary["error"] = error
    global _last_poll_at, _last_result, _last_error
    with _lock:
        _last_poll_at = datetime.utcnow()
        _last_result = summary
        _last_error = error


def _run_worker(run_id: str) -> None:
    db = None
    try:
        db = SessionLocal()
        orchestrator.run_autonomy_sync(db, run_id)
    finally:
        try:
            if db is not None:
                db.close()
        finally:
            with _lock:
                _active_worker_run_ids.discard(str(run_id))
