"""Periodic sweep of brain_batch_jobs for orphaned 'running' rows.

Background
----------
``brain_batch_jobs`` gets a single startup-time sweep at app boot
(migration 081, line ~3073 in migrations.py) that marks running rows
older than 4h as ``timeout``. But during long uptimes new orphans
accumulate -- on 2026-04-27 we found 40 stale 'running' rows from the
morning that had been there 13+ hours without ever transitioning,
because no periodic reconciler exists.

This module fills that gap: a small idempotent function that scans for
'running' rows that are stale (either by heartbeat or by absolute age)
and marks them ``orphaned`` with a reason. Wired into the scheduler in
``trading_scheduler.py`` (every 5 minutes, scheduler-worker only).

Heartbeat columns added by migration 191. Worker code can update
``heartbeat_at`` periodically once we plumb that through; until then the
fallback is age-of-``started_at``.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_batch_reconciler]"


def reconcile_stale_batch_jobs(
    db: Session,
    *,
    stale_no_heartbeat_minutes: int = 30,
    stale_heartbeat_minutes: int = 10,
) -> dict[str, Any]:
    """Mark stale 'running' rows as 'orphaned'.

    Two cases:
      1. Row HAS a heartbeat but it's older than ``stale_heartbeat_minutes``.
      2. Row has NO heartbeat AND ``started_at`` is older than
         ``stale_no_heartbeat_minutes`` (fallback for jobs that don't yet
         emit heartbeats).

    Returns a summary dict for logging / observability:
        {
          "orphaned_total": int,
          "stale_heartbeat": int,
          "no_heartbeat": int,
          "by_job_type": {job_type: count, ...},
        }
    """
    now = datetime.utcnow()
    no_hb_cutoff = now - timedelta(minutes=stale_no_heartbeat_minutes)
    hb_cutoff = now - timedelta(minutes=stale_heartbeat_minutes)

    # FIX C6 (2026-04-29 third-pass audit): capture job age in
    # final_state_reason instead of the flat 'stale_heartbeat' /
    # 'no_heartbeat' tokens. The audit found 32 orphans in 24h all tagged
    # with the same flat token; the underlying ages varied from minutes to
    # 12+ hours. Including the age (in minutes, computed from started_at)
    # tells ops at a glance whether this is a mid-run stall or a job that
    # never produced a heartbeat in its entire lifetime.
    #
    # R25 fix (2026-04-30): SQLAlchemy text() treats every ``:identifier``
    # token as a bind parameter regardless of single-quote context, so
    # the original ``':last_hb_min='`` literal was being parsed as a
    # bind reference to a parameter that did not exist -- the reconciler
    # was raising InvalidRequestError on every 5min run since deploy
    # (25 stale rows accumulated). Switched the separator to a comma so
    # no ``:`` appears inside the literal.
    rows_with_stale_hb = (
        db.execute(
            text(
                """
                UPDATE brain_batch_jobs
                SET status = 'orphaned',
                    ended_at = :now,
                    orphaned_at = :now,
                    final_state_reason =
                        'stale_heartbeat,age_min='
                        || ROUND(EXTRACT(EPOCH FROM (:now - started_at))/60.0)::int::text
                        || ',last_hb_min='
                        || ROUND(EXTRACT(EPOCH FROM (:now - heartbeat_at))/60.0)::int::text
                WHERE status = 'running'
                  AND heartbeat_at IS NOT NULL
                  AND heartbeat_at < :hb_cutoff
                RETURNING id, job_type
                """
            ),
            {"now": now, "hb_cutoff": hb_cutoff},
        )
        .fetchall()
    )

    rows_no_hb = (
        db.execute(
            text(
                """
                UPDATE brain_batch_jobs
                SET status = 'orphaned',
                    ended_at = :now,
                    orphaned_at = :now,
                    final_state_reason =
                        'no_heartbeat,age_min='
                        || ROUND(EXTRACT(EPOCH FROM (:now - started_at))/60.0)::int::text
                WHERE status = 'running'
                  AND heartbeat_at IS NULL
                  AND started_at < :no_hb_cutoff
                RETURNING id, job_type
                """
            ),
            {"now": now, "no_hb_cutoff": no_hb_cutoff},
        )
        .fetchall()
    )

    db.commit()

    by_type: Counter[str] = Counter()
    for r in rows_with_stale_hb:
        by_type[r.job_type or "unknown"] += 1
    for r in rows_no_hb:
        by_type[r.job_type or "unknown"] += 1

    total = len(rows_with_stale_hb) + len(rows_no_hb)
    if total > 0:
        logger.info(
            "%s reconciled %d orphaned rows (stale_hb=%d, no_hb=%d): %s",
            LOG_PREFIX,
            total,
            len(rows_with_stale_hb),
            len(rows_no_hb),
            ", ".join(f"{t}={n}" for t, n in by_type.most_common()),
        )

    return {
        "orphaned_total": total,
        "stale_heartbeat": len(rows_with_stale_hb),
        "no_heartbeat": len(rows_no_hb),
        "by_job_type": dict(by_type),
    }
