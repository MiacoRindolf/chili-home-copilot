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
DEFAULT_FAST_DELETE_BATCH_SIZE = 50_000


_FAST_RETENTION_TABLES: dict[str, tuple[str, str]] = {
    "fast_snapshots": ("fast_snapshots", "bar_close_at"),
    "fast_orderbook": ("fast_orderbook", "snapshot_at"),
    "fast_alerts": ("fast_alerts", "fired_at"),
    "fast_executions": ("fast_executions", "decided_at"),
    "fast_exits": ("fast_exits", "exited_at"),
}


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

    snap_d = snapshot_days or settings.brain_retention_snapshot_days
    batch_d = batch_job_days or settings.brain_retention_batch_job_days
    event_d = learning_event_days or settings.brain_retention_event_days
    alert_d = alert_days or settings.brain_retention_alert_days
    bt_d = backtest_days or settings.brain_retention_backtest_days

    results: dict[str, Any] = {}

    results["snapshots"] = _archive_old_snapshots(db, snap_d, dry_run)
    results["batch_jobs"] = _prune_batch_job_payloads(db, batch_d, dry_run)
    results["learning_events"] = _prune_old_events(db, event_d, dry_run)
    results["alerts"] = _prune_old_alerts(db, alert_d, dry_run)
    results["backtests"] = _archive_old_backtests(db, bt_d, dry_run)
    results["predictions"] = _prune_old_predictions(db, settings.brain_retention_prediction_days, dry_run)
    results["prescreen"] = _prune_old_prescreen(db, settings.brain_retention_prescreen_days, dry_run)
    results["proposals"] = _prune_old_proposals(db, settings.brain_retention_proposal_days, dry_run)
    results["paper_trades"] = _prune_old_paper_trades(db, settings.brain_retention_paper_trade_days, dry_run)
    results["stuck_jobs"] = _cleanup_stuck_batch_jobs(db, dry_run)
    results["setup_vitals_history"] = _prune_setup_vitals_history(db, 90, dry_run)
    results["ticker_vitals_stale"] = _prune_stale_ticker_vitals(db, 7, dry_run)
    results["fast_path"] = _prune_fast_path_tables(db, dry_run)

    if not dry_run:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

    logger.info("[retention] Sweep complete: %s", results)
    return results


def _safe_positive_int(value: Any, default: int) -> int:
    try:
        out = int(value)
        if out > 0:
            return out
    except (TypeError, ValueError):
        pass
    return int(default)


def _prune_fast_path_tables(db: Session, dry_run: bool) -> dict[str, Any]:
    """Batch-delete old fast-path rows.

    The fast lane writes much higher volume than the daily brain tables,
    especially ``fast_orderbook``. Deletes are capped per sweep so the
    scheduler does not attempt a single huge transaction against a multi-GB
    partition.
    """
    from ...config import settings

    batch_size = _safe_positive_int(
        getattr(settings, "brain_retention_fast_delete_batch_size", None),
        DEFAULT_FAST_DELETE_BATCH_SIZE,
    )
    policy = {
        "fast_snapshots": _safe_positive_int(
            getattr(settings, "brain_retention_fast_snapshot_days", None), 30
        ),
        "fast_orderbook": _safe_positive_int(
            getattr(settings, "brain_retention_fast_orderbook_days", None), 3
        ),
        "fast_alerts": _safe_positive_int(
            getattr(settings, "brain_retention_fast_alert_days", None), 14
        ),
        "fast_executions": _safe_positive_int(
            getattr(settings, "brain_retention_fast_execution_days", None), 30
        ),
        "fast_exits": _safe_positive_int(
            getattr(settings, "brain_retention_fast_exit_days", None), 90
        ),
    }

    results: dict[str, Any] = {"batch_size": batch_size}
    for key, retain_days in policy.items():
        table, ts_col = _FAST_RETENTION_TABLES[key]
        results[key] = _prune_fast_table_by_time(
            db,
            table=table,
            ts_col=ts_col,
            retain_days=retain_days,
            batch_size=batch_size,
            dry_run=dry_run,
        )
    return results


def _prune_fast_table_by_time(
    db: Session,
    *,
    table: str,
    ts_col: str,
    retain_days: int,
    batch_size: int,
    dry_run: bool,
) -> dict[str, int]:
    if table not in _FAST_RETENTION_TABLES:
        raise ValueError(f"unsupported fast retention table: {table}")
    expected_table, expected_ts = _FAST_RETENTION_TABLES[table]
    if table != expected_table or ts_col != expected_ts:
        raise ValueError(f"unsupported fast retention timestamp: {table}.{ts_col}")

    if not _fast_retention_has_leading_time_index(db, table, ts_col):
        return {
            "retain_days": retain_days,
            "eligible_batch": 0,
            "deleted": 0,
            "skipped": 1,
        }

    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_sql = text(f"""
        SELECT COUNT(*)
        FROM (
            SELECT 1
            FROM {table}
            WHERE {ts_col} < :cutoff
            ORDER BY id ASC
            LIMIT :limit
        ) limited
    """)
    try:
        eligible_batch = int(db.execute(
            count_sql, {"cutoff": cutoff, "limit": int(batch_size)}
        ).scalar() or 0)
    except Exception:
        return {"retain_days": retain_days, "eligible_batch": 0, "deleted": 0}

    deleted = 0
    if not dry_run and eligible_batch > 0:
        delete_sql = text(f"""
            WITH doomed AS (
                SELECT id, {ts_col}
                FROM {table}
                WHERE {ts_col} < :cutoff
                ORDER BY id ASC
                LIMIT :limit
            )
            DELETE FROM {table} t
            USING doomed
            WHERE t.id = doomed.id
              AND t.{ts_col} = doomed.{ts_col}
        """)
        result = db.execute(
            delete_sql, {"cutoff": cutoff, "limit": int(batch_size)}
        )
        deleted = int(result.rowcount or 0)

    return {
        "retain_days": retain_days,
        "eligible_batch": eligible_batch,
        "deleted": deleted,
    }


def _fast_retention_has_leading_time_index(db: Session, table: str, ts_col: str) -> bool:
    """Avoid accidental sequential scans on high-volume fast tables.

    ``fast_orderbook`` can be tens of GB. Retention only runs when a btree
    index starts with the table's timestamp column. Existing ticker-first
    indexes are good for UI lookups but not for retention pruning.
    """
    try:
        rows = db.execute(text("""
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = ANY (current_schemas(false))
              AND tablename IN (:table, :default_table)
        """), {"table": table, "default_table": f"{table}_default"}).fetchall()
    except Exception:
        return False

    needle = f"using btree ({ts_col.lower()}"
    for row in rows:
        indexdef = str(row[0] or "").lower()
        if needle in indexdef:
            return True
    return False


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


def _prune_old_predictions(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("SELECT COUNT(*) FROM brain_prediction_snapshot WHERE as_of_ts < :cutoff")
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "deleted": 0}
    if not dry_run and count > 0:
        db.execute(text("DELETE FROM brain_prediction_snapshot WHERE as_of_ts < :cutoff"), {"cutoff": cutoff})
    return {"eligible": count, "deleted": count if not dry_run else 0}


def _prune_old_prescreen(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text(
        "SELECT COUNT(*) FROM trading_prescreen_candidates WHERE first_seen_at < :cutoff AND active = false"
    )
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "deleted": 0}
    if not dry_run and count > 0:
        db.execute(
            text(
                "DELETE FROM trading_prescreen_candidates WHERE first_seen_at < :cutoff AND active = false"
            ),
            {"cutoff": cutoff},
        )
    return {"eligible": count, "deleted": count if not dry_run else 0}


def _prune_old_proposals(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text(
        "SELECT COUNT(*) FROM trading_proposals WHERE proposed_at < :cutoff "
        "AND status IN ('expired', 'rejected')"
    )
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "deleted": 0}
    if not dry_run and count > 0:
        db.execute(
            text(
                "DELETE FROM trading_proposals WHERE proposed_at < :cutoff "
                "AND status IN ('expired', 'rejected')"
            ),
            {"cutoff": cutoff},
        )
    return {"eligible": count, "deleted": count if not dry_run else 0}


def _prune_setup_vitals_history(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    """Delete old setup vitals history rows (per-trade trajectory log)."""
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("SELECT COUNT(*) FROM trading_setup_vitals_history WHERE created_at < :cutoff")
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "deleted": 0}
    if not dry_run and count > 0:
        db.execute(
            text("DELETE FROM trading_setup_vitals_history WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        )
    return {"eligible": count, "deleted": count if not dry_run else 0}


def _prune_stale_ticker_vitals(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    """Remove ticker vitals cache rows older than retain_days (refreshed on demand)."""
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("SELECT COUNT(*) FROM trading_ticker_vitals WHERE computed_at < :cutoff")
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "deleted": 0}
    if not dry_run and count > 0:
        db.execute(
            text("DELETE FROM trading_ticker_vitals WHERE computed_at < :cutoff"),
            {"cutoff": cutoff},
        )
    return {"eligible": count, "deleted": count if not dry_run else 0}


def _prune_old_paper_trades(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text(
        "SELECT COUNT(*) FROM trading_paper_trades WHERE created_at < :cutoff "
        "AND status IN ('closed', 'expired', 'cancelled')"
    )
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "deleted": 0}
    if not dry_run and count > 0:
        db.execute(
            text(
                "DELETE FROM trading_paper_trades WHERE created_at < :cutoff "
                "AND status IN ('closed', 'expired', 'cancelled')"
            ),
            {"cutoff": cutoff},
        )
    return {"eligible": count, "deleted": count if not dry_run else 0}


def _cleanup_stuck_batch_jobs(db: Session, dry_run: bool) -> dict[str, int]:
    cutoff = datetime.utcnow() - timedelta(hours=2)
    count_q = text("SELECT COUNT(*) FROM brain_batch_jobs WHERE status = 'running' AND started_at < :cutoff")
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "fixed": 0}
    if not dry_run and count > 0:
        db.execute(
            text("UPDATE brain_batch_jobs SET status = 'timeout' WHERE status = 'running' AND started_at < :cutoff"),
            {"cutoff": cutoff},
        )
    return {"eligible": count, "fixed": count if not dry_run else 0}


def _prune_batch_job_payloads(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    """Null out large payload_json from completed batch jobs older than retain_days."""
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("""
        SELECT COUNT(*) FROM brain_batch_jobs
        WHERE started_at < :cutoff
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
            WHERE started_at < :cutoff
              AND payload_json IS NOT NULL
              AND status IN ('ok', 'error')
        """), {"cutoff": cutoff})
    return {"eligible": count, "pruned": count if not dry_run else 0}


def _prune_old_events(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    """Delete learning events older than retain_days."""
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("SELECT COUNT(*) FROM trading_learning_events WHERE created_at < :cutoff")
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "deleted": 0}
    if not dry_run and count > 0:
        db.execute(text("DELETE FROM trading_learning_events WHERE created_at < :cutoff"), {"cutoff": cutoff})
    return {"eligible": count, "deleted": count if not dry_run else 0}


def _prune_old_alerts(db: Session, retain_days: int, dry_run: bool) -> dict[str, int]:
    """Delete alert history older than retain_days."""
    cutoff = datetime.utcnow() - timedelta(days=retain_days)
    count_q = text("SELECT COUNT(*) FROM trading_alerts WHERE created_at < :cutoff")
    try:
        count = db.execute(count_q, {"cutoff": cutoff}).scalar() or 0
    except Exception:
        return {"eligible": 0, "deleted": 0}
    if not dry_run and count > 0:
        db.execute(text("DELETE FROM trading_alerts WHERE created_at < :cutoff"), {"cutoff": cutoff})
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
