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


def get_retest_interval_days() -> int:
    """Days after which an active pattern is eligible for routine re-backtest (from Settings)."""
    from ...config import settings

    return max(1, int(getattr(settings, "brain_retest_interval_days", 7)))


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
    cutoff = datetime.utcnow() - timedelta(days=get_retest_interval_days())

    needs_backtest = or_(
        ScanPattern.backtest_priority > 0,
        ScanPattern.last_backtest_at.is_(None),
        ScanPattern.last_backtest_at < cutoff,
    )

    tier_rank = case(
        (ScanPattern.queue_tier == "prescreen", 0),
        else_=1,
    )
    q = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            needs_backtest,
        )
        .order_by(
            tier_rank.asc(),
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
    db.flush()
    try:
        from .brain_work.emitters import emit_backtest_requested_for_pattern

        emit_backtest_requested_for_pattern(db, pattern_id, source="operator_boost")
    except Exception:
        logger.debug("[backtest_queue] brain_work emit backtest_requested failed", exc_info=True)
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
    backtests_run: int | None = None,
) -> None:
    """Mark a pattern as tested and update its stats.

    Resets the boost priority after processing.

    FIX 35 (Bug #47, 2026-04-29): now also recomputes ``backtest_count`` from
    the canonical trading_backtests table. Previously this column was only
    updated by the slow-cycle ``run_learning_cycle`` path (via
    ``actual_bt_count = COUNT(*) FROM trading_backtests``) — the per-pattern
    queue-drain hot path skipped it entirely. With FIX 34's independent
    timer drain, that meant patterns could accumulate 25+ backtest rows
    while ``backtest_count`` stayed at 0, blocking the promotion gate
    (which checks ``backtest_count >= min_trades``). This regression
    persisted from the moment the queue path diverged from the cycle path.
    """
    import math as _math
    from sqlalchemy import func as _func
    from ...models.trading import BacktestResult as _TB

    pattern.last_backtest_at = datetime.utcnow()
    pattern.backtest_priority = 0  # Reset boost after processing

    # NaN/range guard (2026-04-28 audit found 11 NaN + 11 out-of-range rows
    # written by callers that didn't sanitize). Migration 193 adds a CHECK
    # constraint at the DB level — this guard avoids exception-on-commit
    # and gives us a clean log line instead.
    if win_rate is not None and _math.isfinite(win_rate) and 0.0 <= win_rate <= 1.0:
        pattern.win_rate = round(win_rate, 4)
    elif win_rate is not None:
        logger.warning(
            "[backtest_queue] mark_pattern_tested rejected invalid win_rate=%r for pattern id=%s",
            win_rate, getattr(pattern, "id", None),
        )
    if avg_return is not None and _math.isfinite(avg_return):
        pattern.avg_return_pct = round(avg_return, 2)
    elif avg_return is not None:
        logger.warning(
            "[backtest_queue] mark_pattern_tested rejected invalid avg_return=%r for pattern id=%s",
            avg_return, getattr(pattern, "id", None),
        )

    # FIX 35: recompute backtest_count from canonical source. Cheap query
    # (single COUNT, indexed on scan_pattern_id) and matches what the cycle
    # path computes at learning.py:7230. Avoids drift between the two paths.
    try:
        actual_bt_count = (
            db.query(_func.count(_TB.id))
            .filter(_TB.scan_pattern_id == pattern.id)
            .scalar()
            or 0
        )
        pattern.backtest_count = int(actual_bt_count)
    except Exception as e:
        logger.warning(
            "[backtest_queue] backtest_count recount failed for pattern id=%s: %s",
            getattr(pattern, "id", None), e,
        )

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

    cutoff = datetime.utcnow() - timedelta(days=get_retest_interval_days())

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


def get_exploration_pattern_ids(
    db: Session,
    exclude_ids: set[int],
    limit: int,
) -> list[int]:
    """Extra backtest targets when the retest queue is thin.

    Prefers **variant** rows (``parent_id`` set) for evolution signal, then oldest
    ``last_backtest_at`` so the worker keeps gathering fresh stats instead of idling.
    """
    if limit <= 0:
        return []
    q = db.query(ScanPattern.id).filter(ScanPattern.active.is_(True))
    if exclude_ids:
        q = q.filter(~ScanPattern.id.in_(exclude_ids))
    # Variants first (non-null parent_id), then stalest last_backtest
    variant_first = case((ScanPattern.parent_id.isnot(None), 0), else_=1)
    rows = (
        q.order_by(
            variant_first.asc(),
            ScanPattern.last_backtest_at.asc().nullsfirst(),
            ScanPattern.id.asc(),
        )
        .limit(limit)
        .all()
    )
    return [r[0] for r in rows]
