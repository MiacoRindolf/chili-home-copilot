"""Weekly sweep: re-evaluate the realized-EV gate on promoted patterns
whose trades have stopped firing entirely.

The per-trade-close demote handler (``handlers/demote.py``) covers the
active case -- when a promoted pattern's trades close, demote re-checks
the realized-EV gate and demotes if it now blocks. But patterns whose
trades have STOPPED FIRING entirely never get re-checked: they sit at
``lifecycle_stage='promoted'`` indefinitely, even when their evidence
no longer supports promotion.

The legacy ``run_learning_cycle`` had a periodic sweep
(``run_live_pattern_depromotion``) that caught these. The cycle is
gated off (f-kill-legacy-learning-cycle), so this cron sweep replaces
that coverage. Weekly cadence is enough -- patterns that haven't traded
in a week aren't moving the needle anyway, and a stale 'promoted'
status doesn't materially affect live trading until trades resume.

Author: 2026-05-06 (f-cron-stale-promoted).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _latest_exit_dates_by_pattern(db: "Session", pattern_ids: list[int]) -> dict[int, datetime]:
    if not pattern_ids:
        return {}

    from sqlalchemy import func

    from app.models.trading import Trade

    rows = (
        db.query(Trade.scan_pattern_id, func.max(Trade.exit_date))
        .filter(Trade.scan_pattern_id.in_(sorted(set(pattern_ids))))
        .group_by(Trade.scan_pattern_id)
        .all()
    )
    return {
        int(pattern_id): latest_exit
        for pattern_id, latest_exit in rows
        if pattern_id is not None
    }


def run_stale_promoted_sweep(db: "Session") -> dict[str, Any]:
    """Iterate promoted patterns; demote any whose realized-EV gate
    fails AND who haven't had a trade close in the last 7 days.

    Patterns with recent trade activity are skipped -- the per-trade-
    close demote handler covers them.

    Returns a stats dict for log visibility:
      {patterns_checked, patterns_skipped_recent, patterns_demoted}
    """
    from app.models.trading import ScanPattern
    from app.services.trading.realized_ev_gate import evaluate_realized_ev

    stale_cutoff = datetime.utcnow() - timedelta(days=7)
    patterns = db.query(ScanPattern).filter(
        ScanPattern.lifecycle_stage == "promoted",
        ScanPattern.active.is_(True),
    ).all()
    latest_exit_by_pattern = _latest_exit_dates_by_pattern(
        db,
        [int(p.id) for p in patterns if p.id is not None],
    )

    demoted = 0
    checked = 0
    skipped_recent = 0
    for p in patterns:
        last_exit = latest_exit_by_pattern.get(int(p.id)) if p.id is not None else None
        if last_exit is not None and last_exit >= stale_cutoff:
            skipped_recent += 1
            continue
        checked += 1

        try:
            result = evaluate_realized_ev(p)
        except Exception:
            logger.exception(
                "[stale_promoted_sweep] EV gate eval crashed pattern_id=%s",
                p.id,
            )
            continue

        if not result.passed:
            p.lifecycle_stage = "challenged"
            p.updated_at = datetime.utcnow()
            demoted += 1
            logger.info(
                "[stale_promoted_sweep] demoted pattern_id=%s name=%s "
                "reasons=%s",
                p.id, p.name, list(result.reasons),
            )

    db.commit()
    return {
        "patterns_checked": checked,
        "patterns_skipped_recent": skipped_recent,
        "patterns_demoted": demoted,
    }
