"""Daily backtest_priority scorer for scan_patterns.

Round-12 (2026-04-30) follow-on to the third-pass audit: the backtest
queue was pulling patterns with no priority signal -- 726 of 732
patterns had ``backtest_priority=0``, so the queue was effectively FIFO
and high-value patterns (no realized data, recently demoted) had no way
to jump the line.

This module computes a priority score per pattern and writes it back to
``scan_patterns.backtest_priority``. The queue worker already orders by
priority DESC; once this scorer runs, the next batch will pick the
most-needed patterns first.

Scoring (higher = test sooner):

    +60  pattern is promoted (currently being traded - any bug here is live risk)
    +50  pattern is challenged, unless it already has hard negative EV evidence
    +40  no realized stats AND active candidate (the 442 NULL-arp patterns)
    +30  has been promoted but trade_count < 5 (insufficient evidence)
    +20  last_backtest_at older than 7 days (stale)
    +10  last_backtest_at older than 14 days (very stale)
    + 5  active=True but never tested
       0 (default - no urgency signal)
    -50  pattern lifecycle is retired or decayed (de-prioritize)
    -40  pattern is demoted_evidence_gap
    -100 inactive

Score is clamped to [0, 100] so the existing queue ordering still works.

Per the no-hardcoded-fallback principle (operator feedback 2026-04-29):
the scoring weights above are all *signal weights*, not fallback values
substituted for missing measurements. Each pattern's score is computed
ENTIRELY from observed columns; nothing falls back to "neutral 0.5" or
similar synthetic defaults.

Idempotent: re-running the scorer produces the same result for the same
pattern state. Safe to run on a cron.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def run_priority_scoring(db: Session) -> dict[str, Any]:
    """Re-score every active pattern's ``backtest_priority``.

    Returns a summary::

        {
          "scored": int,            # total patterns updated
          "prescreened": int,       # thin or hard-negative rows routed to cheap tier
          "high_priority":  int,    # final score >= 50
          "med_priority":   int,    # final score in [10, 50)
          "low_priority":   int,    # final score in (0, 10)
          "deprioritized":  int,    # final score == 0
        }
    """
    res = db.execute(text(
        """
        UPDATE scan_patterns sp
        SET backtest_priority = LEAST(100, GREATEST(0, sub.score)),
            updated_at = CURRENT_TIMESTAMP
        FROM (
            SELECT id,
                CASE WHEN lifecycle_stage = 'promoted'   THEN 60 ELSE 0 END
              + CASE
                    WHEN lifecycle_stage = 'challenged'
                         AND COALESCE(promotion_status, '') <> 'demoted_evidence_gap'
                         AND NOT (
                             (
                                COALESCE(promotion_status, '') LIKE 'challenged_cpcv_ev:%'
                                OR COALESCE(promotion_status, '') LIKE 'challenged_ev_%'
                             )
                             AND COALESCE(backtest_count, 0) >= 50
                             AND (
                                (avg_return_pct IS NOT NULL AND avg_return_pct < 0.0)
                                OR (win_rate IS NOT NULL AND win_rate < 0.35)
                             )
                         )
                    THEN 50 ELSE 0
                END
              + CASE
                    WHEN avg_return_pct IS NULL
                         AND lifecycle_stage IN ('candidate', 'backtested')
                         AND active = TRUE
                    THEN 40 ELSE 0
                END
              + CASE
                    WHEN lifecycle_stage = 'promoted'
                         AND COALESCE(trade_count, 0) < 5
                    THEN 30 ELSE 0
                END
              + CASE
                    WHEN last_backtest_at IS NULL AND active = TRUE
                    THEN 5 ELSE 0
                END
              + CASE
                    WHEN last_backtest_at IS NOT NULL
                         AND last_backtest_at < NOW() - INTERVAL '7 days'
                    THEN 20 ELSE 0
                END
              + CASE
                    WHEN last_backtest_at IS NOT NULL
                         AND last_backtest_at < NOW() - INTERVAL '14 days'
                    THEN 10 ELSE 0
                END
              - CASE WHEN COALESCE(promotion_status, '') = 'demoted_evidence_gap' THEN 40 ELSE 0 END
              - CASE WHEN lifecycle_stage IN ('retired', 'decayed') THEN 50 ELSE 0 END
              - CASE WHEN active = FALSE THEN 100 ELSE 0 END
              AS score
            FROM scan_patterns
        ) sub
        WHERE sp.id = sub.id
          AND sp.backtest_priority IS DISTINCT FROM LEAST(100, GREATEST(0, sub.score))
        RETURNING sp.id, sp.backtest_priority
        """
    ))
    rows = res.fetchall()
    prescreen_res = db.execute(text(
        """
        UPDATE scan_patterns
        SET queue_tier = 'prescreen',
            updated_at = CURRENT_TIMESTAMP
        WHERE active = TRUE
          AND COALESCE(queue_tier, 'full') <> 'prescreen'
          AND COALESCE(recert_required, FALSE) = FALSE
          AND (
                (
                    lifecycle_stage IN ('candidate', 'backtested')
                    AND (
                        COALESCE(backtest_count, 0) = 0
                        OR avg_return_pct IS NULL
                        OR win_rate IS NULL
                    )
                )
                OR (
                    lifecycle_stage = 'challenged'
                    AND COALESCE(promotion_status, '') = 'demoted_evidence_gap'
                )
                OR (
                    lifecycle_stage = 'challenged'
                    AND COALESCE(backtest_count, 0) >= 50
                    AND (
                        COALESCE(promotion_status, '') LIKE 'challenged_cpcv_ev:%'
                        OR COALESCE(promotion_status, '') LIKE 'challenged_ev_%'
                    )
                    AND (
                        (avg_return_pct IS NOT NULL AND avg_return_pct < 0.0)
                        OR (win_rate IS NOT NULL AND win_rate < 0.35)
                    )
                )
          )
        RETURNING id
        """
    ))
    prescreen_rows = prescreen_res.fetchall()
    db.commit()

    scored = len(rows)
    hi = sum(1 for r in rows if r.backtest_priority >= 50)
    md = sum(1 for r in rows if 10 <= r.backtest_priority < 50)
    lo = sum(1 for r in rows if 0 < r.backtest_priority < 10)
    zr = sum(1 for r in rows if r.backtest_priority == 0)

    summary = {
        "scored": scored,
        "prescreened": len(prescreen_rows),
        "high_priority": hi,
        "med_priority": md,
        "low_priority": lo,
        "deprioritized": zr,
    }
    logger.info("[backtest_queue_priority] %s", summary)
    return summary
