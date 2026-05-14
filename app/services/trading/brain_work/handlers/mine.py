"""Mine handler — reacts to ``market_snapshots_batch`` outcome events.

Replaces Step 1 of ``run_learning_cycle`` (the "mine" step). When the
scheduler-worker emits a ``market_snapshots_batch`` outcome event indicating
fresh OHLCV+indicator data has landed, this handler kicks off pattern
discovery for the affected universe.

Phase 2 of FIX 31 endgame. Until all 5 handlers ship and prove out,
``run_learning_cycle`` continues to run as a fallback (FIX 31 gate decides).

Author: 2026-04-29 (FIX 36).
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:mine]"


def _is_obsolete_snapshot_batch(
    db: "Session",
    ev,
    *,
    grace_seconds: int,
) -> bool:
    """Return True when a newer snapshot batch makes this event redundant."""
    created_at = getattr(ev, "created_at", None)
    if created_at is None:
        return False
    try:
        from sqlalchemy import text

        latest_created_at = db.execute(
            text(
                """
                SELECT max(created_at)
                FROM brain_work_events
                WHERE domain = 'trading'
                  AND event_type = 'market_snapshots_batch'
                  AND id <> :event_id
                """
            ),
            {"event_id": int(getattr(ev, "id", 0) or 0)},
        ).scalar()
    except Exception:
        logger.debug(
            "%s obsolete check failed ev_id=%s",
            LOG_PREFIX,
            getattr(ev, "id", None),
            exc_info=True,
        )
        return False
    if latest_created_at is None:
        return False
    return latest_created_at > (created_at + timedelta(seconds=max(0, grace_seconds)))


def handle_market_snapshots_batch(db: "Session", ev, user_id: int | None) -> None:
    """Run pattern mining triggered by a fresh snapshots batch.

    Payload (from ``emit_market_snapshots_batch_outcome``)::

        {
            "snapshots_taken_daily": int,
            "intraday_snapshots_taken": int,
            "universe_size": int,
            "job_id": str | None,
            "snapshot_driver": str | None,
        }

    Mining uses the default ticker universe (``ALL_SCAN_TICKERS`` + watchlist
    + trending crypto) since the event itself doesn't carry a ticker list —
    the snapshots batch refreshed the universe, so re-mining the universe
    is the correct response.
    """
    # f-handler-pattern-stats audit (2026-05-05) found the original
    # ``....config`` / ``....db`` resolved to ``app.services.{config,db}``
    # (4 dots = up 3 packages, lands on ``app.services``) -- both modules
    # don't exist there. Absolute imports are unambiguous.
    from app.config import settings
    from app.db import SessionLocal
    from ..ledger import enqueue_outcome_event  # noqa: F401  (planned: emit pattern_added)
    from ...learning import mine_patterns

    payload = ev.payload if isinstance(ev.payload, dict) else {}
    universe_size = int(payload.get("universe_size") or 0)
    job_id = (payload.get("job_id") or "").strip()
    daily = int(payload.get("snapshots_taken_daily") or 0)
    intraday = int(payload.get("intraday_snapshots_taken") or 0)

    # Skip-empty guard: if the snapshots batch landed nothing actionable,
    # re-mining the same data is wasted compute. Threshold is conservative —
    # 10 fresh bars across the universe is a reasonable floor.
    min_snapshots = int(
        os.environ.get(
            "CHILI_BRAIN_MINE_HANDLER_MIN_SNAPSHOTS",
            str(getattr(settings, "brain_mine_handler_min_snapshots", 10)),
        )
    )
    if (daily + intraday) < min_snapshots:
        logger.info(
            "%s skip ev_id=%s job_id=%s daily=%d intraday=%d below_floor=%d",
            LOG_PREFIX, ev.id, job_id, daily, intraday, min_snapshots,
        )
        return

    obsolete_grace_s = int(
        os.environ.get(
            "CHILI_BRAIN_MINE_HANDLER_OBSOLETE_EVENT_GRACE_SECONDS",
            str(getattr(settings, "brain_mine_handler_obsolete_event_grace_seconds", 900)),
        )
    )
    if _is_obsolete_snapshot_batch(
        db,
        ev,
        grace_seconds=obsolete_grace_s,
    ):
        logger.info(
            "%s skip ev_id=%s job_id=%s obsolete_snapshot_batch grace_s=%d",
            LOG_PREFIX,
            ev.id,
            job_id,
            obsolete_grace_s,
        )
        return

    uid = user_id
    if uid is None:
        uid = getattr(settings, "brain_default_user_id", None)

    # Run mining in its own session so a long mine doesn't hold the dispatcher's
    # session open (the dispatcher's `db` is shared across handlers in the batch).
    sess = SessionLocal()
    try:
        logger.info(
            "%s ev_id=%s job_id=%s starting mine (universe~%d, daily=%d intraday=%d)",
            LOG_PREFIX, ev.id, job_id, universe_size, daily, intraday,
        )
        discoveries = mine_patterns(sess, uid)
        sess.commit()
        n = len(discoveries) if isinstance(discoveries, list) else 0
        logger.info(
            "%s ev_id=%s job_id=%s mined patterns=%d", LOG_PREFIX, ev.id, job_id, n
        )
        # NOTE: pattern_added emitter is the natural next step here so
        # cpcv_gate handler can immediately backtest fresh discoveries.
        # Skipping for Phase 2 first-cut — the existing fast_backtest
        # independent timer (FIX 34) will pick them up on its 60s tick.
    except Exception as e:
        try:
            sess.rollback()
        except Exception:
            pass
        logger.warning(
            "%s ev_id=%s mine failed: %s", LOG_PREFIX, ev.id, e, exc_info=True
        )
        raise
    finally:
        try:
            sess.close()
        except Exception:
            pass
