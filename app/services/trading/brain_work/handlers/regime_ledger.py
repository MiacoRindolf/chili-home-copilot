"""Regime ledger handler — reacts to trade-close events.

Replaces the ``pattern_regime_ledger`` subtask (which runs every 12 cycles
of ``run_learning_cycle``). When a trade closes, this handler rebuilds the
ledger so the most-recent trade contributes to per-pattern × regime stats
without waiting for the next cycle.

Phase 2 of FIX 31 endgame, handler #5 of 5. Author: 2026-04-29 (FIX 39).

**Note:** ``build_ledger`` is idempotent (UPSERT semantics). Calling it
multiple times when several trades close in quick succession is safe; it
just rebuilds the same window. To avoid pointless rebuilds we use a simple
in-process throttle (rebuild at most every N seconds).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:regime_ledger]"

# Throttle: rebuild at most every N seconds. Trade-close events can burst
# (the autotrader closes several positions at once during a regime shift)
# and we don't want to rebuild the ledger N times for the same window.
_REBUILD_THROTTLE_S = 60.0
_LAST_REBUILD_AT: float = 0.0


def handle_trade_closed_for_ledger(db: "Session", ev, user_id: int | None) -> None:
    """Rebuild pattern_regime_ledger when a trade closes (throttled)."""
    global _LAST_REBUILD_AT
    from ....db import SessionLocal
    from ...pattern_regime_ledger import build_ledger

    now = time.time()
    if (now - _LAST_REBUILD_AT) < _REBUILD_THROTTLE_S:
        logger.debug(
            "%s ev_id=%s throttled (last rebuild %.1fs ago)",
            LOG_PREFIX, ev.id, now - _LAST_REBUILD_AT,
        )
        return

    sess = SessionLocal()
    try:
        result = build_ledger(sess)
        sess.commit()
        _LAST_REBUILD_AT = now
        rows = result.get("rows_written") if isinstance(result, dict) else None
        logger.info(
            "%s ev_id=%s ledger rebuilt rows=%s skipped=%s",
            LOG_PREFIX, ev.id, rows,
            (result.get("skipped") if isinstance(result, dict) else None),
        )
    except Exception as e:
        try:
            sess.rollback()
        except Exception:
            pass
        logger.warning("%s ev_id=%s build_ledger failed: %s", LOG_PREFIX, ev.id, e, exc_info=True)
        raise
    finally:
        try:
            sess.close()
        except Exception:
            pass
