"""6-hourly cron: validate_and_evolve hypothesis-weight evolution.

Wraps the legacy ``run_learning_cycle`` step that evolves hypothesis
weights based on accumulated mined market data. Stale weights mean the
brain doesn't react to regime changes in feature predictiveness.

Why cron, not event-handler: the function mines OHLCV history for 500
tickers via a thread pool then evaluates hypotheses against the data.
Heavy, broad-market work -- the natural trigger is "every N hours,"
not "this thing happened." Per the brief's decision rule
(f-overnight-jumbo Phase 5 Step 5.1): "If it reads broader market
state, cron is better."

6-hour cadence (configurable via env var). At default of 6h this runs
4 times a day; the function's own 30-row data minimum gates whether
each run actually does work.

Author: 2026-05-06 (f-handler-validate-evolve).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def run_validate_evolve(db: "Session", user_id: int | None = None) -> dict[str, Any]:
    """Wrap ``learning.validate_and_evolve`` with this cron's logging
    contract. Failures swallowed at the cron-job boundary; this
    function returns the raw stats from the underlying call.
    """
    from app.services.trading.learning import validate_and_evolve
    return validate_and_evolve(db, user_id)
