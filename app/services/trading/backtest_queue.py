"""Backtest Queue Manager - Priority-based pattern processing.

Tracks ScanPatterns that need backtesting and processes them in priority order.
Patterns are re-tested weekly or when manually boosted by the user.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)

RETEST_INTERVAL_DAYS = 7  # Re-backtest patterns weekly

# Dashboard polls this frequently; cache avoids hammering Postgres when the pool is busy.
_QUEUE_STATUS_LOCK = threading.Lock()
_QUEUE_STATUS_CACHE: dict[str, Any] | None = None
_QUEUE_STATUS_MONO: float = 0.0
QUEUE_STATUS_CACHE_TTL_S = 4.0


def invalidate_queue_status_cache() -> None:
    """Call after queue mutations (boost, tested, etc.)."""
    global _QUEUE_STATUS_CACHE
    with _QUEUE_STATUS_LOCK:
        _QUEUE_STATUS_CACHE = None


def get_pending_patterns(
    db: Session, limit: int = 50, ids_only: bool = False
) -> list[ScanPattern] | list[int]:
    """Get patterns needing backtests, ordered by priority.

    Priority order:
    1. Manually boosted patterns (highest backtest_priority first)
    2. Never-tested patterns (last_backtest_at is NULL)
    3. Oldest tested patterns (past the retest interval)

    Boosted patterns (backtest_priority > 0) are always eligible, even if last_backtest_at
    is within the retest window — otherwise 'boost to front' would no-op for recently
    tested patterns.

    When ids_only=True, returns a list of pattern IDs only (lighter; workers load by id).
    """
    cutoff = datetime.utcnow() - timedelta(days=RETEST_INTERVAL_DAYS)

    needs_backtest = or_(
        ScanPattern.backtest_priority > 0,
        ScanPattern.last_backtest_at.is_(None),
        ScanPattern.last_backtest_at < cutoff,
    )

    q = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            needs_backtest,
        )
        .order_by(
            ScanPattern.backtest_priority.desc(),
            ScanPattern.last_backtest_at.asc().nullsfirst(),
            ScanPattern.created_at.asc(),
        )
        .limit(limit)
    )
    if ids_only:
        rows = q.with_entities(ScanPattern.id).all()
        return [r[0] for r in rows]
    return q.all()


def get_boosted_patterns(db: Session) -> list[ScanPattern]:
    """Get patterns that have been manually boosted (priority > 0)."""
    return (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.backtest_priority > 0,
        )
        .order_by(ScanPattern.backtest_priority.desc())
        .all()
    )


def boost_pattern(db: Session, pattern_id: int, priority: int = 100) -> bool:
    """Manually boost a pattern to front of queue.
    
    Args:
        db: Database session
        pattern_id: ID of the ScanPattern to boost
        priority: Priority level (higher = processed first, default 100)
    
    Returns:
        True if pattern was found and boosted, False otherwise
    """
    pattern = db.query(ScanPattern).get(pattern_id)
    if not pattern:
        return False
    
    pattern.backtest_priority = priority
    db.commit()
    invalidate_queue_status_cache()

    logger.info(
        "[backtest_queue] Boosted pattern '%s' (id=%d) to priority %d",
        pattern.name, pattern_id, priority
    )
    return True


def clear_boost(db: Session, pattern_id: int) -> bool:
    """Clear the boost priority for a pattern (set back to 0)."""
    pattern = db.query(ScanPattern).get(pattern_id)
    if not pattern:
        return False
    
    pattern.backtest_priority = 0
    db.commit()
    return True


def mark_pattern_tested(
    db: Session,
    pattern: ScanPattern,
    win_rate: float | None = None,
    avg_return: float | None = None,
) -> None:
    """Mark a pattern as tested and update its stats.
    
    Resets the boost priority after processing.
    """
    pattern.last_backtest_at = datetime.utcnow()
    pattern.backtest_priority = 0  # Reset boost after processing
    
    if win_rate is not None:
        pattern.win_rate = round(win_rate, 4)
    if avg_return is not None:
        pattern.avg_return_pct = round(avg_return, 2)

    db.commit()
    invalidate_queue_status_cache()


def get_queue_status(db: Session, *, use_cache: bool = True) -> dict[str, Any]:
    """Return queue stats for dashboard display (single query with conditional aggregation)."""
    global _QUEUE_STATUS_MONO, _QUEUE_STATUS_CACHE
    now = time.monotonic()
    if use_cache:
        with _QUEUE_STATUS_LOCK:
            if (
                _QUEUE_STATUS_CACHE is not None
                and (now - _QUEUE_STATUS_MONO) < QUEUE_STATUS_CACHE_TTL_S
            ):
                return dict(_QUEUE_STATUS_CACHE)

    cutoff = datetime.utcnow() - timedelta(days=RETEST_INTERVAL_DAYS)

    base = db.query(ScanPattern).filter(ScanPattern.active.is_(True))
    needs_backtest_expr = or_(
        ScanPattern.backtest_priority > 0,
        ScanPattern.last_backtest_at.is_(None),
        ScanPattern.last_backtest_at < cutoff,
    )

    row = base.with_entities(
        func.count(ScanPattern.id).label("total_active"),
        func.count(case((ScanPattern.last_backtest_at.is_(None), 1))).label("never_tested"),
        func.count(
            case(
                (
                    and_(
                        ScanPattern.last_backtest_at.isnot(None),
                        ScanPattern.last_backtest_at < cutoff,
                    ),
                    1,
                )
            )
        ).label("needs_retest"),
        func.count(case((ScanPattern.backtest_priority > 0, 1))).label("boosted"),
        func.count(
            case(
                (
                    and_(
                        ScanPattern.last_backtest_at.isnot(None),
                        ScanPattern.last_backtest_at >= cutoff,
                    ),
                    1,
                )
            )
        ).label("recently_tested"),
        func.count(case((needs_backtest_expr, 1))).label("pending_queue"),
    ).first()

    total_active = row.total_active or 0
    never_tested = row.never_tested or 0
    needs_retest = row.needs_retest or 0
    boosted = row.boosted or 0
    recently_tested = row.recently_tested or 0
    pending = row.pending_queue or 0

    out = {
        "total": total_active,
        "pending": pending,
        "never_tested": never_tested,
        "needs_retest": needs_retest,
        "boosted": boosted,
        "recently_tested": recently_tested,
        "queue_empty": pending == 0,
    }
    if use_cache:
        with _QUEUE_STATUS_LOCK:
            _QUEUE_STATUS_MONO = time.monotonic()
            _QUEUE_STATUS_CACHE = dict(out)
    return out


def get_next_pattern(db: Session) -> ScanPattern | None:
    """Get the single highest-priority pattern to process next."""
    patterns = get_pending_patterns(db, limit=1)
    return patterns[0] if patterns else None
