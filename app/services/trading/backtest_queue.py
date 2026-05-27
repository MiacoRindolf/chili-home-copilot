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

from sqlalchemy import and_, case, false, func, or_
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)
RECERT_PROMOTED_LIFECYCLES = ("promoted", "live")
PROMOTION_PATH_DEBT_LIFECYCLES = ("shadow_promoted", "pilot_promoted")
ZERO_TRADE_DEMOTE_PROTECTED_LIFECYCLES = (
    RECERT_PROMOTED_LIFECYCLES + PROMOTION_PATH_DEBT_LIFECYCLES
)
PROMOTION_PATH_DEBT_REASONS = (
    "cpcv_n_paths_below_provisional_min",
    "provisional_small_paths",
)
QUEUE_TIER_FULL = "full"
QUEUE_TIER_PRESCREEN = "prescreen"
QUEUE_RANK_PRIORITY_RECERT = 0
QUEUE_RANK_RECERT = 1
QUEUE_RANK_PROMOTION_PATH_DEBT = 2
QUEUE_RANK_GENERIC_BACKLOG = 3
MIN_ZERO_TRADE_DEMOTE_THRESHOLD = 1
ZERO_TRADE_DEMOTE_DEFAULT_THRESHOLD = 3


def get_retest_interval_days() -> int:
    """Days after which an active pattern is eligible for routine re-backtest (from Settings)."""
    from ...config import settings

    return max(1, int(getattr(settings, "brain_retest_interval_days", 7)))


def get_priority_bypass_retest_floor() -> int:
    """Priority value that means an explicit boost may bypass retest staleness."""
    from ...config import BACKTEST_PRIORITY_DEFAULT_BYPASS_RETEST_FLOOR, settings

    return max(
        1,
        int(
            getattr(
                settings,
                "chili_backtest_priority_bypass_retest_floor",
                BACKTEST_PRIORITY_DEFAULT_BYPASS_RETEST_FLOOR,
            )
            or BACKTEST_PRIORITY_DEFAULT_BYPASS_RETEST_FLOOR
        ),
    )


def _priority_recert_pattern_ids() -> list[int]:
    try:
        from ...config import settings

        raw = str(getattr(settings, "brain_recert_queue_priority_pattern_ids", "") or "")
    except Exception:
        raw = ""
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            logger.debug("[backtest_queue] invalid priority recert pattern id: %s", token)
    return out


def _promotion_path_debt_enabled() -> bool:
    try:
        from ...config import settings

        return bool(
            getattr(settings, "chili_backtest_prioritize_promotion_path_debt", True)
        )
    except Exception:
        return True


def _promotion_path_debt_expr():
    """Patterns close to pilot/live whose CPCV path debt should drain early."""
    if not _promotion_path_debt_enabled():
        return false()
    reason_exprs = [
        ScanPattern.promotion_gate_reasons.contains([reason])
        for reason in PROMOTION_PATH_DEBT_REASONS
    ]
    return and_(
        ScanPattern.lifecycle_stage.in_(PROMOTION_PATH_DEBT_LIFECYCLES),
        or_(*reason_exprs),
    )


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
    1. Explicitly prioritized promoted/live recerts
    2. Other promoted/live recerts
    3. Shadow/pilot promotion-path CPCV path debt
    4. Generic backlog by backtest_priority, staleness, and age

    Explicit boosts (backtest_priority >= configured bypass floor) are always
    eligible, even if last_backtest_at is within the retest window. Lower
    scored priorities order stale/untested rows but do not make freshly tested
    rows pending again.

    When ids_only=True, returns a list of pattern IDs only (lighter; workers load by id).
    """
    cutoff = datetime.utcnow() - timedelta(days=get_retest_interval_days())

    tier_rank = case(
        (ScanPattern.queue_tier == "prescreen", 0),
        else_=1,
    )
    recert_promoted = and_(
        ScanPattern.recert_required.is_(True),
        ScanPattern.lifecycle_stage.in_(RECERT_PROMOTED_LIFECYCLES),
    )
    priority_recert_ids = _priority_recert_pattern_ids()
    promotion_path_debt = _promotion_path_debt_expr()
    priority_bypass = ScanPattern.backtest_priority >= get_priority_bypass_retest_floor()
    needs_backtest = or_(
        priority_bypass,
        ScanPattern.last_backtest_at.is_(None),
        ScanPattern.last_backtest_at < cutoff,
        recert_promoted,
        promotion_path_debt,
    )
    recert_rank = case(
        (
            and_(
                recert_promoted,
                ScanPattern.id.in_(priority_recert_ids),
            ),
            QUEUE_RANK_PRIORITY_RECERT,
        ),
        (recert_promoted, QUEUE_RANK_RECERT),
        (promotion_path_debt, QUEUE_RANK_PROMOTION_PATH_DEBT),
        else_=QUEUE_RANK_GENERIC_BACKLOG,
    )
    q = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            needs_backtest,
        )
        .order_by(
            tier_rank.asc(),
            recert_rank.asc(),
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
    trade_bearing_tickers: int | None = None,
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

    def _non_negative_int(value: int | None) -> int | None:
        if value is None:
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    def _zero_trade_signal_count() -> int | None:
        trade_count = _non_negative_int(trade_bearing_tickers)
        if trade_count is not None:
            return trade_count
        return _non_negative_int(backtests_run)

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

    # Track consecutive zero-trade evidence runs using trade-bearing ticker
    # count, not ticker jobs attempted. Patterns that attempt many ticker
    # jobs but produce no realized backtest trades are queue-burn; after the
    # configured threshold they move to prescreen. Promoted/near-promoted
    # operational lanes keep their tier so recert debt is not starved.
    try:
        from ...config import settings as _s
        threshold = max(
            MIN_ZERO_TRADE_DEMOTE_THRESHOLD,
            int(
                getattr(
                    _s,
                    "chili_backtest_zero_trade_demote_threshold",
                    ZERO_TRADE_DEMOTE_DEFAULT_THRESHOLD,
                )
            ),
        )
        cur = max(0, int(getattr(pattern, "consecutive_zero_trade_runs", 0) or 0))
        trade_signal_count = _zero_trade_signal_count()
        lifecycle = str(getattr(pattern, "lifecycle_stage", "") or "").strip().lower()
        protected_lifecycle = lifecycle in ZERO_TRADE_DEMOTE_PROTECTED_LIFECYCLES
        if trade_signal_count == 0:
            new_count = cur + 1
            pattern.consecutive_zero_trade_runs = new_count
            if (
                new_count >= threshold
                and (pattern.queue_tier or QUEUE_TIER_FULL) != QUEUE_TIER_PRESCREEN
                and not protected_lifecycle
            ):
                logger.warning(
                    "[backtest_queue] zero_trade_demote pattern_id=%s '%s' has %s "
                    "consecutive zero-trade evidence runs (>= %s); demoting "
                    "queue_tier %s -> %s to free queue cycles.",
                    pattern.id, getattr(pattern, "name", "?"),
                    new_count, threshold,
                    pattern.queue_tier or QUEUE_TIER_FULL,
                    QUEUE_TIER_PRESCREEN,
                )
                pattern.queue_tier = QUEUE_TIER_PRESCREEN
            elif new_count >= threshold and protected_lifecycle:
                logger.info(
                    "[backtest_queue] zero_trade_counter protected pattern_id=%s "
                    "lifecycle=%s consecutive_zero_trade_runs=%s threshold=%s",
                    pattern.id,
                    lifecycle,
                    new_count,
                    threshold,
                )
        elif trade_signal_count is not None and trade_signal_count > 0:
            # Reset on any trade-bearing evidence run -- only consecutive
            # zero-trade evidence demotes.
            if cur != 0:
                pattern.consecutive_zero_trade_runs = 0
    except Exception:
        logger.debug(
            "[backtest_queue] zero-trade counter update failed for pattern id=%s",
            getattr(pattern, "id", None), exc_info=True,
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
    recert_promoted_expr = and_(
        ScanPattern.recert_required.is_(True),
        ScanPattern.lifecycle_stage.in_(RECERT_PROMOTED_LIFECYCLES),
    )
    promotion_path_debt_expr = _promotion_path_debt_expr()
    priority_bypass_expr = (
        ScanPattern.backtest_priority >= get_priority_bypass_retest_floor()
    )
    needs_backtest_expr = or_(
        priority_bypass_expr,
        ScanPattern.last_backtest_at.is_(None),
        ScanPattern.last_backtest_at < cutoff,
        recert_promoted_expr,
        promotion_path_debt_expr,
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
        func.count(case((priority_bypass_expr, 1))).label("priority_bypass"),
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
        func.count(
            case(
                (
                    and_(needs_backtest_expr, recert_promoted_expr),
                    1,
                )
            )
        ).label("recert_pending"),
        func.count(
            case(
                (
                    and_(needs_backtest_expr, promotion_path_debt_expr),
                    1,
                )
            )
        ).label("promotion_path_debt_pending"),
    ).first()

    total_active = row.total_active or 0
    never_tested = row.never_tested or 0
    needs_retest = row.needs_retest or 0
    boosted = row.boosted or 0
    priority_bypass = row.priority_bypass or 0
    recently_tested = row.recently_tested or 0
    pending = row.pending_queue or 0
    recert_pending = row.recert_pending or 0
    promotion_path_debt_pending = row.promotion_path_debt_pending or 0

    out = {
        "total": total_active,
        "pending": pending,
        "never_tested": never_tested,
        "needs_retest": needs_retest,
        "boosted": boosted,
        "priority_bypass": priority_bypass,
        "recently_tested": recently_tested,
        "recert_pending": recert_pending,
        "promotion_path_debt_pending": promotion_path_debt_pending,
        "priority_bypass_floor": get_priority_bypass_retest_floor(),
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
