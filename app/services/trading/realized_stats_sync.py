"""Sync ``ScanPattern.{trade_count, win_rate, avg_return_pct}`` from ``trading_trades``.

Background (2026-04-28): the audit showed many patterns with stored
``win_rate`` but ``trade_count = 0``. The EWMA-drop write paths in
:mod:`learning.py` only fire on the alert-feedback / closed-trade
update loops; patterns whose live trades came in via other code paths
(broker reconcile, manual close, etc.) never have their column synced.

This module is the source-of-truth sync. It reads ``trading_trades``
and recomputes the stats for every pattern that has at least one
closed trade. Idempotent. Cheap (one GROUP BY query + one UPDATE per
pattern). Safe to run on every brain-worker cycle.

Tunable::

    chili_realized_sync_enabled        = True
    chili_realized_sync_lookback_days  = 365   # all-time by default? 365 keeps it bounded
    chili_realized_sync_min_n          = 1     # don\'t bother for patterns with no trades
"""
from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def sync_realized_stats(sess: Session, *, dry_run: bool = False) -> dict[str, int]:
    """Recompute ``trade_count`` / ``win_rate`` / ``avg_return_pct`` from
    ``trading_trades``. Returns counts of patterns updated / skipped.
    """
    if not bool(_settings_get("chili_realized_sync_enabled", True)):
        logger.info("[realized_sync] disabled via chili_realized_sync_enabled")
        return {"updated": 0, "skipped": 0, "no_trades": 0}

    lookback = int(_settings_get("chili_realized_sync_lookback_days", 365))
    min_n = max(1, int(_settings_get("chili_realized_sync_min_n", 1)))

    # Realized stats per pattern from trading_trades. Mean-of-trade-returns
    # IS the EV. We compute pct return from entry/exit prices (matches what
    # learning.py does for the EWMA-replacement path).
    rows = sess.execute(text("""
        SELECT scan_pattern_id,
               count(*) AS n,
               sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
               avg(
                 CASE
                   WHEN entry_price IS NOT NULL AND entry_price > 0
                        AND exit_price IS NOT NULL
                   THEN ((exit_price - entry_price) / entry_price) * 100.0
                   ELSE NULL
                 END
               ) AS avg_ret_pct
        FROM trading_trades
        WHERE status = 'closed'
          AND scan_pattern_id IS NOT NULL
          AND exit_date > NOW() - make_interval(days => :lookback)
        GROUP BY scan_pattern_id
        HAVING count(*) >= :min_n
    """), {"lookback": lookback, "min_n": min_n}).fetchall()

    updated = 0
    skipped = 0
    for r in rows:
        pid = int(r.scan_pattern_id)
        n = int(r.n)
        wins = int(r.wins or 0)
        wr = (wins / n) if n > 0 else None
        avg_ret = float(r.avg_ret_pct) if r.avg_ret_pct is not None else None

        # NaN/range safety. Migration 193 added a CHECK that win_rate must be
        # in [0, 1]; respect that here so we never trigger an IntegrityError.
        if wr is not None and (not math.isfinite(wr) or wr < 0.0 or wr > 1.0):
            logger.warning(
                "[realized_sync] skipping pattern_id=%s — computed wr=%s out of range", pid, wr,
            )
            skipped += 1
            continue
        if avg_ret is not None and not math.isfinite(avg_ret):
            avg_ret = None

        if dry_run:
            logger.info(
                "[realized_sync] DRY pattern_id=%s n=%s wr=%.4f avg_ret_pct=%s",
                pid, n, wr or 0.0, f"{avg_ret:.2f}" if avg_ret is not None else "None",
            )
            updated += 1
            continue

        sess.execute(text("""
            UPDATE scan_patterns
            SET trade_count = :n,
                win_rate = :wr,
                avg_return_pct = :ret,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :pid
        """), {"pid": pid, "n": n, "wr": wr, "ret": avg_ret})
        updated += 1

    if not dry_run:
        sess.commit()

    no_trades = sess.execute(text("""
        SELECT count(*) FROM scan_patterns
        WHERE NOT EXISTS (
            SELECT 1 FROM trading_trades
            WHERE scan_pattern_id = scan_patterns.id AND status = 'closed'
        )
    """)).scalar() or 0

    logger.info(
        "[realized_sync] complete: updated=%s skipped=%s patterns_with_no_closed_trades=%s",
        updated, skipped, no_trades,
    )
    return {"updated": updated, "skipped": skipped, "no_trades": int(no_trades)}
