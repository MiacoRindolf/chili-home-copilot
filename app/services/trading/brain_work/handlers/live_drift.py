"""Phase 2 handler: live-drift detection on trade close.

Subscribes to ``live_trade_closed`` / ``paper_trade_closed`` /
``broker_fill_closed``. For each event, calls
``run_live_drift_refresh`` to detect when promoted patterns' live
behaviour has drifted from backtest expectations.

Author: 2026-05-06 (f-handler-live-drift, Phase 6 of
f-overnight-jumbo). Eighth Phase 2 handler in the brain_work/ tree.

Design rules:
  * Failures swallowed at the handler boundary so a broken pattern
    can't poison subsequent events.
  * Fresh SessionLocal -- mirror of demote.py / pattern_stats.py.
  * Absolute imports (handlers at depth 5 -- 4-dot relative resolves
    to nonexistent app.services.X per the f-handler-pattern-stats
    finding).
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:live_drift]"


def _run_refresh(ev: Any) -> None:
    from app.db import SessionLocal
    from app.services.trading.live_drift import run_live_drift_refresh

    sess = SessionLocal()
    try:
        result = run_live_drift_refresh(sess)
        logger.info(
            "%s event_id=%s result=%s",
            LOG_PREFIX, getattr(ev, "id", None), result,
        )
    except Exception as e:
        logger.exception(
            "%s event_id=%s failed: %s",
            LOG_PREFIX, getattr(ev, "id", None), e,
        )
    finally:
        try:
            sess.close()
        except Exception:
            pass


def handle_paper_trade_closed(db: "Session", ev: Any, user_id: int | None) -> None:
    _run_refresh(ev)


def handle_live_trade_closed(db: "Session", ev: Any, user_id: int | None) -> None:
    _run_refresh(ev)


def handle_broker_fill_closed(db: "Session", ev: Any, user_id: int | None) -> None:
    _run_refresh(ev)
