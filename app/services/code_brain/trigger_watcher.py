"""Reactive trigger watchers for the Code Brain.

Replaces the dumb 60-second ``IntervalTrigger`` that drove
``run_code_learning_cycle``. Instead of always calling the LLM, these
watchers do *cheap* DB reads to detect state transitions and enqueue
events. The decision router runs only when there's actually something
to do.

Watchers (all read-only against their domain tables, no LLM):

  * ``watch_plan_tasks`` — looks for plan_tasks rows that flipped to
    ``coding_readiness_state='ready_for_dispatch'`` since the last tick.
    Enqueues a ``plan_task_ready`` event per new row.

  * ``watch_validation_failures`` — looks for ``coding_task_validation_run``
    rows with ``passed=false`` that don't yet have a corresponding
    ``validation_failed`` event. Enqueues one each.

  * ``watch_pattern_drift`` — checks ``code_decision_router_log`` for a
    burst of consecutive ``outcome='failed'`` decisions on the same
    pattern (pattern is decaying). Phase 2 — stub for now.

These run on a coarse 30-second cadence — that's the WATCH cadence,
not the LLM cadence. The LLM only fires when route() returns Decision
PREMIUM or LOCAL_MODEL, which only happens for genuinely new work.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import event_bus
from . import runtime_state

logger = logging.getLogger(__name__)


def watch_plan_tasks(db: Session) -> int:
    """Enqueue ``plan_task_ready`` events for newly-ready tasks.

    Idempotent thanks to ``event_bus.enqueue(dedupe=True)`` — running this
    every 30s won't spam duplicate events for the same task.

    Returns the count of events enqueued (newly inserted, not deduped).
    """
    rows = db.execute(
        text(
            "SELECT id, COALESCE(title, '') AS title "
            "FROM plan_tasks "
            "WHERE coding_readiness_state = 'ready_for_dispatch' "
            "ORDER BY sort_order ASC NULLS LAST, id ASC "
            "LIMIT 25"
        )
    ).fetchall()

    new_count = 0
    for row in rows or []:
        before = db.execute(
            text(
                "SELECT id FROM code_brain_events "
                "WHERE event_type = :t "
                "  AND subject_kind = 'plan_task' "
                "  AND subject_id = :s "
                "  AND claimed_at IS NULL"
            ),
            {"t": event_bus.EVENT_PLAN_TASK_READY, "s": int(row[0])},
        ).fetchone()
        if before:
            continue
        event_bus.enqueue(
            db,
            event_type=event_bus.EVENT_PLAN_TASK_READY,
            subject_kind="plan_task",
            subject_id=int(row[0]),
            payload={"title": str(row[1])[:500]},
            priority=5,
            dedupe=True,
        )
        new_count += 1
    if new_count:
        logger.info(
            "[code_brain.trigger_watcher] watch_plan_tasks enqueued %d new event(s)",
            new_count,
        )
    return new_count


def watch_validation_failures(db: Session) -> int:
    """Enqueue ``validation_failed`` events for fresh validation-run failures.

    Looks at ``coding_task_validation_run`` rows in the last 30 minutes
    where ``passed=false``, and enqueues an event per task that doesn't
    already have an unclaimed event for it.

    Real-world payoff: a failed validation should re-route the task back
    through the brain so it can either pull from a different pattern or
    escalate to operator — instead of just sitting in ``ready_for_dispatch``.
    """
    # Guard: table may not exist on every branch's schema. Probing existence
    # in a single round-trip is cheaper than relying on except+rollback to
    # recover from a 'relation does not exist' error every 30 seconds.
    exists_row = db.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'coding_task_validation_run' LIMIT 1"
        )
    ).fetchone()
    if not exists_row:
        return 0

    rows = db.execute(
        text(
            "SELECT DISTINCT task_id FROM coding_task_validation_run "
            "WHERE created_at > NOW() - INTERVAL '30 minutes' "
            "  AND passed = false "
            "  AND task_id IS NOT NULL"
        )
    ).fetchall()

    new_count = 0
    for row in rows or []:
        task_id = int(row[0])
        existing = db.execute(
            text(
                "SELECT id FROM code_brain_events "
                "WHERE event_type = :t "
                "  AND subject_kind = 'plan_task' "
                "  AND subject_id = :s "
                "  AND claimed_at IS NULL"
            ),
            {"t": event_bus.EVENT_VALIDATION_FAILED, "s": task_id},
        ).fetchone()
        if existing:
            continue
        event_bus.enqueue(
            db,
            event_type=event_bus.EVENT_VALIDATION_FAILED,
            subject_kind="plan_task",
            subject_id=task_id,
            payload={"source": "watch_validation_failures"},
            priority=4,  # higher priority than plain new tasks
            dedupe=True,
        )
        new_count += 1
    if new_count:
        logger.info(
            "[code_brain.trigger_watcher] watch_validation_failures enqueued %d new event(s)",
            new_count,
        )
    return new_count


def run_all_watchers(db: Session) -> dict[str, Any]:
    """Run every watcher once. Cheap. No LLM. Suitable for a 30s tick.

    Skipped entirely if the brain mode is ``paused`` so operators can
    halt work without ripping out the scheduler job.
    """
    state = runtime_state.get_state(db)
    if state.mode == "paused":
        return {"skipped": True, "reason": "mode=paused"}

    # Reap stuck claims (a worker that crashed mid-event left them).
    reaped = event_bus.reap_stuck_claims(db, age_minutes=30)

    new_plan_tasks = watch_plan_tasks(db)
    new_validation_failures = 0
    try:
        new_validation_failures = watch_validation_failures(db)
    except Exception as e:  # validation table is optional
        # CRITICAL: rollback the failed transaction so subsequent queries on
        # the same session don't fail with InFailedSqlTransaction. Without
        # this, queue_depth() below blows up cascadingly.
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug(
            "[code_brain.trigger_watcher] watch_validation_failures skipped: %s", e
        )

    return {
        "skipped": False,
        "reaped_stuck": reaped,
        "new_plan_task_events": new_plan_tasks,
        "new_validation_failed_events": new_validation_failures,
        "queue_depth": event_bus.queue_depth(db),
    }
