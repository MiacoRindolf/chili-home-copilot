"""Data retention policies for the trading brain.

Prevents unbounded table growth by archiving old rows and pruning
stale data.  Run periodically (e.g., daily via scheduler).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_RETAIN_DAYS = 180
DEFAULT_BATCH_JOB_RETAIN_DAYS = 90
DEFAULT_LEARNING_EVENT_RETAIN_DAYS = 120
DEFAULT_ALERT_RETAIN_DAYS = 90
DEFAULT_BACKTEST_RETAIN_DAYS = 180


def run_retention_policy(
    db: Session,
    *,
    snapshot_days: int | None = None,
    batch_job_days: int | None = None,
    learning_event_days: int | None = None,
    alert_days: int | None = None,
    backtest_days: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute the full retention sweep. Returns counts of archived/deleted rows."""
    from ...config import settings

    snap_d = snapshot_days or int(getattr(settings, "brain_retention_snapshot_days", DEFAULT_SNAPSHOT_RETAIN_DAYS))
    batch_d = batch_job_days or int(getattr(settings, "brain_retention_batch_job_days", DEFAULT_BATCH_JOB_RETAIN_DAYS))
    event_d = learning_event_days or int(getattr(settings, "brain_retention_event_days", DEFAULT_LEARNING_EVENT_RETAIN_DAYS))
    alert_d = alert_days or int(getattr(settings, "brain_retention_alert_days", DEFAULT_ALERT_RETAIN_DAYS))
    bt_d = backtest_days or int(getattr(settings, "brain_retention_backtest_days", DEFAULT_BACKTEST_RETAIN_DAYS))

    results: dict[str, Any] = {}

    results["snapshots"] = _archive_old_snapshots(db, snap_d, dry_run)
    results["batch_jobs"] = _prune_batch_job_payloads(db, batch_d, dry_run)
    results["learning_events"] = _prune_old_events(db, event_d, dry_run)
    results["alerts"] = _prune_old_alerts(db, alert_d, dry_run)
    results["backtests"] = _archive_old_backtests(db, bt_d, dry_run)

    if not dry_run:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

    logger.info("[retention] Sweep complete: %s", results)
    return results


def _archive_old_snapshots(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    """Soft-archive snapshots older than retain_days that already have future_return_5d filled."""
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("""
        SELECT COUNT(*) FROM trading_snapshots
        WHERE snapshot_date < :cutoff
          AND future_return_5d IS NOT NULL
          AND archived_at IS NULL
    """)
    count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    if not dry_run and count > 0:
        db.execute(text("""
            UPDATE trading_snapshots
            SET archived_at = NOW()
            WHERE snapshot_date < :cutoff
              AND future_return_5d IS NOT NULL
              AND archived_at IS NULL
        """), {"cutoff": cutoff})
    return {"eligible": count, "archived": count if not dry_run else 0}


def _prune_batch_job_payloads(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    """Null out large payload_json from completed batch jobs older than retain_days."""
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("""
        SELECT COUNT(*) FROM brain_batch_jobs
        WHERE created_at < :cutoff
          AND payload_json IS NOT NULL
          AND status IN ('ok', 'error')
    """)
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "pruned": 0}
    if not dry_run and count > 0:
        db.execute(text("""
            UPDATE brain_batch_jobs
            SET payload_json = NULL, archived_at = NOW()
            WHERE created_at < :cutoff
              AND payload_json IS NOT NULL
              AND status IN ('ok', 'error')
        """), {"cutoff": cutoff})
    return {"eligible": count, "pruned": count if not dry_run else 0}


def _prune_old_events(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    """Delete learning events older than retain_days."""
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("SELECT COUNT(*) FROM trading_learning_events WHERE timestamp < :cutoff")
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "deleted": 0}
    if not dry_run and count > 0:
        db.execute(text("DELETE FROM trading_learning_events WHERE timestamp < :cutoff"), {"cutoff": cutoff})
    return {"eligible": count, "deleted": count if not dry_run else 0}


def _prune_old_alerts(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    """Delete alert history older than retain_days."""
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("SELECT COUNT(*) FROM trading_alert_history WHERE created_at < :cutoff")
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "deleted": 0}
    if not dry_run and count > 0:
        db.execute(text("DELETE FROM trading_alert_history WHERE created_at < :cutoff"), {"cutoff": cutoff})
    return {"eligible": count, "deleted": count if not dry_run else 0}


def _archive_old_backtests(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    """Soft-archive backtests older than retain_days (null out equity_curve to save space)."""
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("""
        SELECT COUNT(*) FROM trading_backtests
        WHERE ran_at < :cutoff
          AND equity_curve IS NOT NULL
          AND archived_at IS NULL
    """)
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "archived": 0}
    if not dry_run and count > 0:
        db.execute(text("""
            UPDATE trading_backtests
            SET equity_curve = NULL, archived_at = NOW()
            WHERE ran_at < :cutoff
              AND equity_curve IS NOT NULL
              AND archived_at IS NULL
        """), {"cutoff": cutoff})
    return {"eligible": count, "archived": count if not dry_run else 0}
