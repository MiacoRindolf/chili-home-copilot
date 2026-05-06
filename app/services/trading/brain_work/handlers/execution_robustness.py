"""Phase 2 handler: execution-robustness check on trade close.

Subscribes to ``live_trade_closed`` / ``paper_trade_closed`` /
``broker_fill_closed``. For each event, calls
``run_execution_robustness_refresh`` to track whether live executions
meet expected slippage/cost profiles. Detects venue-side regressions.

Author: 2026-05-06 (f-handler-execution-robustness, Phase 6 of
f-overnight-jumbo). Ninth Phase 2 handler in the brain_work/ tree.
Bundle-shipped with f-handler-live-drift -- both subscribe to the
same close events; bundling the brief saves redundant scaffolding.

Design rules: same as live_drift.py.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:execution_robustness]"


def _run_refresh(ev: Any) -> None:
    from app.db import SessionLocal
    from app.services.trading.execution_robustness import (
        run_execution_robustness_refresh,
    )

    sess = SessionLocal()
    try:
        result = run_execution_robustness_refresh(sess)
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
