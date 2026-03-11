"""Background scheduler for continuous trading AI learning.

Runs learning cycles (scan → snapshot → backfill → mine → journal)
automatically on a schedule so the AI Brain is always growing.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def _run_learning_job():
    """Executed by APScheduler in a background thread."""
    from ..db import SessionLocal
    from . import trading_service as ts

    logger.info("[scheduler] Starting scheduled learning cycle")
    db = SessionLocal()
    try:
        result = ts.run_learning_cycle(db, user_id=None, full_universe=True)
        logger.info(f"[scheduler] Learning cycle result: {result}")
    except Exception as e:
        logger.error(f"[scheduler] Learning cycle failed: {e}")
    finally:
        db.close()


def _run_weekly_review_job():
    """Weekly performance review job."""
    from ..db import SessionLocal
    from . import trading_service as ts

    logger.info("[scheduler] Starting weekly review")
    db = SessionLocal()
    try:
        ts.weekly_performance_review(db, user_id=None)
    except Exception as e:
        logger.error(f"[scheduler] Weekly review failed: {e}")
    finally:
        db.close()


def _run_broker_sync_job():
    """Sync Robinhood positions to local DB during market hours."""
    from . import broker_service

    if not broker_service.is_connected():
        return

    from ..db import SessionLocal
    logger.info("[scheduler] Starting Robinhood position sync")
    db = SessionLocal()
    try:
        result = broker_service.sync_positions_to_db(db, user_id=None)
        logger.info(f"[scheduler] Broker sync result: {result}")
    except Exception as e:
        logger.error(f"[scheduler] Broker sync failed: {e}")
    finally:
        db.close()


def _run_price_monitor_job():
    """Check positions/breakouts/picks and dispatch alerts every 5 minutes."""
    from ..db import SessionLocal
    from .trading.alerts import run_price_monitor

    logger.info("[scheduler] Starting price monitor check")
    db = SessionLocal()
    try:
        result = run_price_monitor(db, user_id=None)
        logger.info(f"[scheduler] Price monitor result: {result}")
    except Exception as e:
        logger.error(f"[scheduler] Price monitor failed: {e}")
    finally:
        db.close()


def start_scheduler():
    """Start the background scheduler. Safe to call multiple times."""
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return

        _scheduler = BackgroundScheduler(daemon=True)

        _scheduler.add_job(
            _run_learning_job,
            trigger=IntervalTrigger(hours=4),
            id="learning_cycle",
            name="Full market learning cycle",
            replace_existing=True,
            max_instances=1,
            next_run_time=datetime.now(),  # run immediately on startup
        )

        _scheduler.add_job(
            _run_weekly_review_job,
            trigger=CronTrigger(day_of_week="sun", hour=18, minute=0),
            id="weekly_review",
            name="Weekly performance review",
            replace_existing=True,
            max_instances=1,
        )

        _scheduler.add_job(
            _run_broker_sync_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour="9-16",
                minute="*/15",
            ),
            id="broker_sync",
            name="Robinhood position sync (market hours)",
            replace_existing=True,
            max_instances=1,
        )

        _scheduler.add_job(
            _run_price_monitor_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour="9-16",
                minute="*/5",
            ),
            id="price_monitor",
            name="Price monitor & alerts (market hours every 5min)",
            replace_existing=True,
            max_instances=1,
        )

        _scheduler.start()
        logger.info("[scheduler] Trading scheduler started (learning every 4h, weekly review Sun 6PM, broker sync every 15min, price monitor every 5min market hours)")


def stop_scheduler():
    """Gracefully stop the scheduler and signal background tasks to abort."""
    global _scheduler
    from . import trading_service as ts
    ts.signal_shutdown()
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=True)
            _scheduler = None
            logger.info("[scheduler] Trading scheduler stopped")


def get_scheduler_info() -> dict:
    """Info about the scheduler and its jobs for the Brain dashboard."""
    if _scheduler is None:
        return {"running": False, "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        })

    return {
        "running": _scheduler.running,
        "jobs": jobs,
    }


def trigger_learning_now():
    """Manually trigger a learning cycle if not already running."""
    from . import trading_service as ts
    if ts.get_learning_status()["running"]:
        return False

    thread = threading.Thread(target=_run_learning_job, daemon=True)
    thread.start()
    return True
