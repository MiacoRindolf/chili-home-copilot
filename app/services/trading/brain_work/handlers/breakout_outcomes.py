"""Phase 2 handler: pattern-evidence update from breakout-alert outcomes.

Subscribes to ``breakout_alert_resolved``. For each event, calls
``learn_from_breakout_outcomes`` for the resolved alert's user. The
function is the legacy cycle's secondary-evidence path -- it aggregates
alert outcomes (winner / fakeout / loser / expired) into pattern
confidence updates for patterns that don't have closed trades yet.

Author: 2026-05-06 (f-handler-breakout-outcomes). Seventh Phase 2
handler in the brain_work/ tree.

Design rules:
  * Per-event, NOT per-trade-affected. The function buckets internally.
  * Idempotent: re-running on the same set of resolved alerts yields
    the same pattern updates.
  * Failures swallowed at the handler boundary so a broken pattern's
    update can't poison subsequent events.
  * Fresh SessionLocal -- mirror of ``demote.py`` and
    ``pattern_stats.py``. Isolates the function's internal commits
    from the dispatcher's transaction.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:breakout_outcomes]"


def handle_breakout_alert_resolved(
    db: "Session", ev: Any, user_id: int | None,
) -> None:
    """Handler entry for ``breakout_alert_resolved`` events."""
    # Absolute imports -- mirror the f-handler-pattern-stats finding
    # (4-dot relative imports resolve to nonexistent app.services.X).
    from app.db import SessionLocal
    from app.services.trading.learning import learn_from_breakout_outcomes

    payload = getattr(ev, "payload", None)
    if isinstance(payload, dict):
        payload_uid = payload.get("user_id")
        if payload_uid is not None:
            try:
                user_id = int(payload_uid)
            except (TypeError, ValueError):
                pass

    sess = SessionLocal()
    try:
        result = learn_from_breakout_outcomes(sess, user_id)
        logger.info(
            "%s event_id=%s user_id=%s patterns_learned=%d",
            LOG_PREFIX,
            getattr(ev, "id", None),
            user_id,
            int(result.get("patterns_learned", 0) or 0),
        )
    except Exception as e:
        logger.exception(
            "%s event_id=%s failed: %s",
            LOG_PREFIX,
            getattr(ev, "id", None),
            e,
        )
    finally:
        try:
            sess.close()
        except Exception:
            pass
