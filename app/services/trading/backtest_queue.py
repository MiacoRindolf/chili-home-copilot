"""Backtest Queue Manager - Priority-based pattern processing.

Tracks ScanPatterns that need backtesting and processes them in priority order.
Patterns are re-tested weekly or when manually boosted by the user.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, false, func, not_, or_, text, true
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
QUEUE_RANK_EDGE_EVIDENCE_VARIANT = 3
QUEUE_RANK_GENERIC_BACKLOG = 4
EDGE_EVIDENCE_VARIANT_PROMOTION_STATUS = "edge_shadow_collecting_ev"
EDGE_EVIDENCE_VARIANT_ORIGIN = "edge_exit_variant"
BACKTEST_QUEUE_LOCK_NAMESPACE = 424242
MIN_ZERO_TRADE_DEMOTE_THRESHOLD = 1
ZERO_TRADE_DEMOTE_DEFAULT_THRESHOLD = 3
LANE_RECERT = "recert"
LANE_PROMOTION_PATH_DEBT = "promotion_path_debt"
LANE_EDGE_EVIDENCE = "edge_evidence"
LANE_PRESCREEN = "prescreen"
LANE_GENERIC = "generic"
QUEUE_LINEAGE_FIXED_CAP_DISABLED = 0
QUEUE_LINEAGE_DIVERSIFICATION_SHARE_DEFAULT = 0.10
QUEUE_LINEAGE_MIN_PER_BATCH_DEFAULT = 1


def _settings_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        from ...config import settings

        raw = getattr(settings, name, default)
    except Exception:
        raw = default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _settings_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        from ...config import settings

        raw = getattr(settings, name, default)
    except Exception:
        raw = default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def get_queue_lineage_cap_policy(limit: int) -> dict[str, Any]:
    """Resolve the queue lineage diversification cap for this batch size."""
    normalized_limit = max(0, int(limit or 0))
    fixed_override = _settings_int(
        "brain_queue_max_per_lineage_per_batch",
        QUEUE_LINEAGE_FIXED_CAP_DISABLED,
        minimum=0,
    )
    if fixed_override > QUEUE_LINEAGE_FIXED_CAP_DISABLED:
        return {
            "mode": "fixed_override",
            "cap": fixed_override,
            "fixed_override": fixed_override,
            "max_batch_share": None,
            "min_per_batch": None,
            "limit": normalized_limit,
        }

    max_batch_share = _settings_float(
        "brain_queue_lineage_max_batch_share",
        QUEUE_LINEAGE_DIVERSIFICATION_SHARE_DEFAULT,
        minimum=0.0,
        maximum=1.0,
    )
    min_per_batch = _settings_int(
        "brain_queue_lineage_min_per_batch",
        QUEUE_LINEAGE_MIN_PER_BATCH_DEFAULT,
        minimum=0,
    )
    if normalized_limit <= 0 or (max_batch_share <= 0.0 and min_per_batch <= 0):
        cap = 0
    else:
        cap = max(min_per_batch, int(math.ceil(normalized_limit * max_batch_share)))
    return {
        "mode": "adaptive_share" if cap > 0 else "disabled",
        "cap": cap,
        "fixed_override": fixed_override,
        "max_batch_share": max_batch_share,
        "min_per_batch": min_per_batch,
        "limit": normalized_limit,
    }


def _queue_lineage_cap(limit: int) -> int | None:
    cap = int(get_queue_lineage_cap_policy(limit).get("cap") or 0)
    return cap if cap > 0 else None


def _settings_bool(name: str, default: bool) -> bool:
    try:
        from ...config import settings

        raw = getattr(settings, name, default)
    except Exception:
        raw = default
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


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


def _sparse_promotion_debt_cooldown_expr(now: datetime | None = None):
    """Throttle near-promoted rows that just proved sparse again.

    Promotion-path debt is important, but a shadow/pilot pattern with repeated
    zero-trade queue runs should not burn the high-priority lane every cycle.
    Manual priority bypass still wins in ``_pending_expr``.
    """
    if not _settings_bool("brain_queue_sparse_promotion_debt_cooldown_enabled", True):
        return false()
    zero_floor = _settings_int(
        "brain_queue_sparse_promotion_debt_zero_runs",
        5,
        minimum=1,
    )
    cooldown_minutes = _settings_int(
        "brain_queue_sparse_promotion_debt_cooldown_minutes",
        360,
        minimum=0,
    )
    if cooldown_minutes <= 0:
        return false()
    cutoff = (now or datetime.utcnow()) - timedelta(minutes=cooldown_minutes)
    return and_(
        _promotion_path_debt_expr(),
        ScanPattern.consecutive_zero_trade_runs >= zero_floor,
        ScanPattern.last_backtest_at.isnot(None),
        ScanPattern.last_backtest_at >= cutoff,
        ScanPattern.backtest_priority < get_priority_bypass_retest_floor(),
    )


def _recert_promoted_expr():
    return and_(
        ScanPattern.recert_required.is_(True),
        ScanPattern.lifecycle_stage.in_(RECERT_PROMOTED_LIFECYCLES),
    )


def _recert_cooldown_expr(now: datetime | None = None):
    """Throttle unresolved recert debt after a fresh backtest attempt."""
    if not _settings_bool("brain_queue_recert_cooldown_enabled", True):
        return false()
    cooldown_minutes = _settings_int(
        "brain_queue_recert_cooldown_minutes",
        360,
        minimum=0,
    )
    if cooldown_minutes <= 0:
        return false()
    cutoff = (now or datetime.utcnow()) - timedelta(minutes=cooldown_minutes)
    return and_(
        _recert_promoted_expr(),
        ScanPattern.last_backtest_at.isnot(None),
        ScanPattern.last_backtest_at >= cutoff,
        ScanPattern.backtest_priority < get_priority_bypass_retest_floor(),
    )


def _edge_evidence_variant_expr():
    return and_(
        ScanPattern.parent_id.isnot(None),
        or_(
            ScanPattern.promotion_status == EDGE_EVIDENCE_VARIANT_PROMOTION_STATUS,
            ScanPattern.origin == EDGE_EVIDENCE_VARIANT_ORIGIN,
        ),
    )


def _prescreen_tier_expr():
    return ScanPattern.queue_tier == QUEUE_TIER_PRESCREEN


def _full_or_unknown_tier_expr():
    return or_(
        ScanPattern.queue_tier.is_(None),
        ScanPattern.queue_tier != QUEUE_TIER_PRESCREEN,
    )


def _pending_expr(cutoff: datetime):
    recert_promoted = _recert_promoted_expr()
    recert_cooldown = _recert_cooldown_expr()
    promotion_path_debt = _promotion_path_debt_expr()
    sparse_promotion_debt_cooldown = _sparse_promotion_debt_cooldown_expr()
    priority_bypass = ScanPattern.backtest_priority >= get_priority_bypass_retest_floor()
    return or_(
        priority_bypass,
        ScanPattern.last_backtest_at.is_(None),
        ScanPattern.last_backtest_at < cutoff,
        and_(recert_promoted, not_(recert_cooldown)),
        and_(promotion_path_debt, not_(sparse_promotion_debt_cooldown)),
    )


def _lane_expr(lane: str):
    recert_promoted = _recert_promoted_expr()
    promotion_path_debt = _promotion_path_debt_expr()
    edge_evidence = _edge_evidence_variant_expr()
    prescreen = _prescreen_tier_expr()
    if lane == LANE_RECERT:
        return recert_promoted
    if lane == LANE_PROMOTION_PATH_DEBT:
        return and_(promotion_path_debt, not_(recert_promoted))
    if lane == LANE_EDGE_EVIDENCE:
        return and_(
            edge_evidence,
            not_(recert_promoted),
            not_(promotion_path_debt),
        )
    if lane == LANE_PRESCREEN:
        return and_(
            prescreen,
            not_(recert_promoted),
            not_(promotion_path_debt),
            not_(edge_evidence),
        )
    if lane == LANE_GENERIC:
        return and_(
            _full_or_unknown_tier_expr(),
            not_(recert_promoted),
            not_(promotion_path_debt),
            not_(edge_evidence),
        )
    return true()


def classify_queue_lane(pattern: Any) -> str:
    """Return the queue lane a selected pattern belongs to."""
    lifecycle = str(getattr(pattern, "lifecycle_stage", "") or "").strip().lower()
    if (
        bool(getattr(pattern, "recert_required", False))
        and lifecycle in RECERT_PROMOTED_LIFECYCLES
    ):
        return LANE_RECERT

    reasons = getattr(pattern, "promotion_gate_reasons", None) or []
    if isinstance(reasons, str):
        reasons = [reasons]
    reason_set = {str(reason) for reason in reasons if reason}
    if (
        lifecycle in PROMOTION_PATH_DEBT_LIFECYCLES
        and bool(reason_set.intersection(PROMOTION_PATH_DEBT_REASONS))
    ):
        return LANE_PROMOTION_PATH_DEBT

    if getattr(pattern, "parent_id", None) is not None:
        promotion_status = str(getattr(pattern, "promotion_status", "") or "")
        origin = str(getattr(pattern, "origin", "") or "")
        if (
            promotion_status == EDGE_EVIDENCE_VARIANT_PROMOTION_STATUS
            or origin == EDGE_EVIDENCE_VARIANT_ORIGIN
        ):
            return LANE_EDGE_EVIDENCE

    queue_tier = str(getattr(pattern, "queue_tier", "") or "").strip().lower()
    if queue_tier == QUEUE_TIER_PRESCREEN:
        return LANE_PRESCREEN
    return LANE_GENERIC


def summarize_queue_batch(patterns: list[Any]) -> dict[str, Any]:
    """Compact batch-shape summary for queue observability."""
    lane_counts = Counter(classify_queue_lane(pattern) for pattern in patterns)
    tier_counts = Counter(
        str(getattr(pattern, "queue_tier", None) or QUEUE_TIER_FULL).strip().lower()
        for pattern in patterns
    )
    lifecycle_counts = Counter(
        str(getattr(pattern, "lifecycle_stage", None) or "unknown").strip().lower()
        for pattern in patterns
    )
    lineage_counts = Counter(_lineage_key(pattern) for pattern in patterns)
    max_lineage_count = max(lineage_counts.values(), default=0)
    top_lineages = [
        {"lineage": lineage, "count": count}
        for lineage, count in lineage_counts.most_common(5)
    ]
    return {
        "lanes": dict(sorted(lane_counts.items())),
        "tiers": dict(sorted(tier_counts.items())),
        "lifecycles": dict(sorted(lifecycle_counts.items())),
        "max_lineage_count": int(max_lineage_count),
        "top_lineages": top_lineages,
    }


def _pending_order_exprs() -> list[Any]:
    priority_recert_ids = _priority_recert_pattern_ids()
    recert_promoted = _recert_promoted_expr()
    promotion_path_debt = _promotion_path_debt_expr()
    edge_evidence = _edge_evidence_variant_expr()
    prescreen = _prescreen_tier_expr()
    priority_recert = and_(
        recert_promoted,
        ScanPattern.id.in_(priority_recert_ids),
    )
    lane_rank = case(
        (priority_recert, QUEUE_RANK_PRIORITY_RECERT),
        (recert_promoted, QUEUE_RANK_RECERT),
        (promotion_path_debt, QUEUE_RANK_PROMOTION_PATH_DEBT),
        (edge_evidence, QUEUE_RANK_EDGE_EVIDENCE_VARIANT),
        (prescreen, QUEUE_RANK_GENERIC_BACKLOG),
        else_=QUEUE_RANK_GENERIC_BACKLOG,
    )
    return [
        lane_rank.asc(),
        ScanPattern.backtest_priority.desc(),
        ScanPattern.last_backtest_at.asc().nullsfirst(),
        ScanPattern.created_at.asc(),
        ScanPattern.id.asc(),
    ]


def _legacy_pending_order_exprs() -> list[Any]:
    tier_rank = case(
        (ScanPattern.queue_tier == QUEUE_TIER_PRESCREEN, 0),
        else_=1,
    )
    return [tier_rank.asc(), *_pending_order_exprs()]


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
    """Get patterns needing backtests using the lane-aware queue planner.

    Priority order:
    1. Explicitly prioritized promoted/live recerts
    2. Other promoted/live recerts
    3. Shadow/pilot promotion-path CPCV path debt
    4. Edge-evidence child variants collecting research EV
    5. Generic backlog by backtest_priority, staleness, and age

    Explicit boosts (backtest_priority >= configured bypass floor) are always
    eligible, even if last_backtest_at is within the retest window. Lower
    scored priorities order stale/untested rows but do not make freshly tested
    rows pending again.

    When ids_only=True, returns a list of pattern IDs only (lighter; workers load by id).
    """
    if limit <= 0:
        return []
    if not bool(_settings_int("brain_queue_lane_planner_enabled", 1, minimum=0)):
        rows = _legacy_pending_patterns(db, limit=limit)
        return [p.id for p in rows] if ids_only else rows

    rows = _planned_pending_patterns(db, limit=limit)
    return [p.id for p in rows] if ids_only else rows


def _legacy_pending_patterns(db: Session, *, limit: int) -> list[ScanPattern]:
    cutoff = datetime.utcnow() - timedelta(days=get_retest_interval_days())

    return (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            _pending_expr(cutoff),
        )
        .order_by(*_legacy_pending_order_exprs())
        .limit(limit)
        .all()
    )


def _lineage_key(pattern: ScanPattern) -> int:
    try:
        return int(pattern.parent_id or pattern.id)
    except (TypeError, ValueError):
        return int(getattr(pattern, "id", 0) or 0)


def _append_planned_rows(
    selected: list[ScanPattern],
    rows: list[ScanPattern],
    *,
    selected_ids: set[int],
    lineage_counts: dict[int, int],
    limit: int,
    lineage_cap: int | None,
) -> None:
    for pattern in rows:
        if len(selected) >= limit:
            return
        try:
            pattern_id = int(pattern.id)
        except (TypeError, ValueError):
            continue
        if pattern_id in selected_ids:
            continue
        lineage = _lineage_key(pattern)
        if lineage_cap is not None and lineage_cap > 0:
            if lineage_counts.get(lineage, 0) >= lineage_cap:
                continue
        selected.append(pattern)
        selected_ids.add(pattern_id)
        lineage_counts[lineage] = lineage_counts.get(lineage, 0) + 1


def _query_lane(
    db: Session,
    *,
    cutoff: datetime,
    lane: str,
    limit: int,
    exclude_ids: set[int],
) -> list[ScanPattern]:
    if limit <= 0:
        return []
    q = db.query(ScanPattern).filter(
        ScanPattern.active.is_(True),
        _pending_expr(cutoff),
        _lane_expr(lane),
    )
    if exclude_ids:
        q = q.filter(not_(ScanPattern.id.in_(exclude_ids)))
    return q.order_by(*_pending_order_exprs()).limit(limit).all()


def _planned_pending_patterns(db: Session, *, limit: int) -> list[ScanPattern]:
    cutoff = datetime.utcnow() - timedelta(days=get_retest_interval_days())
    selected: list[ScanPattern] = []
    selected_ids: set[int] = set()
    lineage_counts: dict[int, int] = {}
    lineage_cap = _queue_lineage_cap(limit)
    fetch_multiplier = _settings_int(
        "brain_queue_lane_fetch_multiplier",
        4,
        minimum=1,
    )
    edge_default = max(1, min(12, max(2, limit // 5)))
    prescreen_default = max(1, min(20, max(2, limit // 4)))
    lane_plan: list[tuple[str, int, int | None]] = [
        (LANE_RECERT, limit, None),
        (LANE_PROMOTION_PATH_DEBT, limit, None),
        (
            LANE_EDGE_EVIDENCE,
            _settings_int(
                "brain_queue_edge_evidence_max_per_batch",
                edge_default,
                minimum=0,
            ),
            lineage_cap,
        ),
        (
            LANE_PRESCREEN,
            _settings_int(
                "brain_queue_prescreen_max_per_batch",
                prescreen_default,
                minimum=0,
            ),
            lineage_cap,
        ),
        (LANE_GENERIC, limit, lineage_cap),
    ]

    for lane, quota, cap in lane_plan:
        remaining = limit - len(selected)
        if remaining <= 0:
            break
        take = min(remaining, max(0, quota))
        if take <= 0:
            continue
        rows = _query_lane(
            db,
            cutoff=cutoff,
            lane=lane,
            limit=max(take, take * fetch_multiplier),
            exclude_ids=selected_ids,
        )
        _append_planned_rows(
            selected,
            rows,
            selected_ids=selected_ids,
            lineage_counts=lineage_counts,
            limit=limit,
            lineage_cap=cap,
        )

    if len(selected) < limit:
        rows = (
            db.query(ScanPattern)
            .filter(ScanPattern.active.is_(True), _pending_expr(cutoff))
            .filter(not_(ScanPattern.id.in_(selected_ids)) if selected_ids else true())
            .order_by(*_pending_order_exprs())
            .limit(limit - len(selected))
            .all()
        )
        _append_planned_rows(
            selected,
            rows,
            selected_ids=selected_ids,
            lineage_counts=lineage_counts,
            limit=limit,
            lineage_cap=lineage_cap,
        )

    return selected


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


def _postgres_bind(db: Session) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:
        return False


def try_acquire_pattern_backtest_lock(db: Session, pattern_id: int) -> bool:
    """Best-effort session advisory lock for one expensive pattern job.

    The queue is backed by ``scan_patterns`` rather than a claim table, so
    overlapping worker ticks can pick the same row. PostgreSQL advisory locks
    give us a schema-free lease: only one session should compute a given
    pattern at a time, and other sessions skip it cheaply.
    """
    if not _postgres_bind(db):
        return True
    try:
        ok = db.execute(
            text(
                "SELECT pg_try_advisory_lock(:namespace, :pattern_id)"
            ),
            {
                "namespace": BACKTEST_QUEUE_LOCK_NAMESPACE,
                "pattern_id": int(pattern_id),
            },
        ).scalar()
        return bool(ok)
    except Exception:
        logger.debug(
            "[backtest_queue] advisory lock acquire failed pattern_id=%s",
            pattern_id,
            exc_info=True,
        )
        return True


def release_pattern_backtest_lock(db: Session, pattern_id: int) -> None:
    """Release a pattern advisory lock if this session owns it."""
    if not _postgres_bind(db):
        return
    try:
        db.execute(
            text("SELECT pg_advisory_unlock(:namespace, :pattern_id)"),
            {
                "namespace": BACKTEST_QUEUE_LOCK_NAMESPACE,
                "pattern_id": int(pattern_id),
            },
        )
    except Exception:
        logger.debug(
            "[backtest_queue] advisory lock release failed pattern_id=%s",
            pattern_id,
            exc_info=True,
        )


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
    recert_promoted_expr = _recert_promoted_expr()
    recert_cooldown_expr = _recert_cooldown_expr()
    promotion_path_debt_expr = _promotion_path_debt_expr()
    sparse_promotion_debt_cooldown_expr = _sparse_promotion_debt_cooldown_expr()
    edge_evidence_expr = _lane_expr(LANE_EDGE_EVIDENCE)
    prescreen_expr = _lane_expr(LANE_PRESCREEN)
    generic_expr = _lane_expr(LANE_GENERIC)
    priority_bypass_expr = (
        ScanPattern.backtest_priority >= get_priority_bypass_retest_floor()
    )
    needs_backtest_expr = _pending_expr(cutoff)

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
                    recert_cooldown_expr,
                    1,
                )
            )
        ).label("recert_cooled"),
        func.count(
            case(
                (
                    and_(needs_backtest_expr, promotion_path_debt_expr),
                    1,
                )
            )
        ).label("promotion_path_debt_pending"),
        func.count(
            case(
                (
                    sparse_promotion_debt_cooldown_expr,
                    1,
                )
            )
        ).label("promotion_path_debt_cooled"),
        func.count(
            case(
                (
                    and_(needs_backtest_expr, edge_evidence_expr),
                    1,
                )
            )
        ).label("edge_evidence_pending"),
        func.count(
            case(
                (
                    and_(needs_backtest_expr, prescreen_expr),
                    1,
                )
            )
        ).label("prescreen_pending"),
        func.count(
            case(
                (
                    and_(needs_backtest_expr, generic_expr),
                    1,
                )
            )
        ).label("generic_pending"),
    ).first()

    total_active = row.total_active or 0
    never_tested = row.never_tested or 0
    needs_retest = row.needs_retest or 0
    boosted = row.boosted or 0
    priority_bypass = row.priority_bypass or 0
    recently_tested = row.recently_tested or 0
    pending = row.pending_queue or 0
    recert_pending = row.recert_pending or 0
    recert_cooled = row.recert_cooled or 0
    promotion_path_debt_pending = row.promotion_path_debt_pending or 0
    promotion_path_debt_cooled = row.promotion_path_debt_cooled or 0
    edge_evidence_pending = row.edge_evidence_pending or 0
    prescreen_pending = row.prescreen_pending or 0
    generic_pending = row.generic_pending or 0

    lineage_policy = get_queue_lineage_cap_policy(
        _settings_int("brain_queue_batch_size", max(1, int(pending or 1)), minimum=1)
    )
    out = {
        "total": total_active,
        "pending": pending,
        "never_tested": never_tested,
        "needs_retest": needs_retest,
        "boosted": boosted,
        "priority_bypass": priority_bypass,
        "recently_tested": recently_tested,
        "recert_pending": recert_pending,
        "recert_cooled": recert_cooled,
        "promotion_path_debt_pending": promotion_path_debt_pending,
        "promotion_path_debt_cooled": promotion_path_debt_cooled,
        "edge_evidence_pending": edge_evidence_pending,
        "prescreen_pending": prescreen_pending,
        "generic_pending": generic_pending,
        "lane_planner_enabled": bool(
            _settings_int("brain_queue_lane_planner_enabled", 1, minimum=0)
        ),
        "max_per_lineage_per_batch": lineage_policy["cap"],
        "lineage_cap_policy": lineage_policy["mode"],
        "lineage_cap_limit_basis": lineage_policy["limit"],
        "lineage_max_batch_share": lineage_policy["max_batch_share"],
        "lineage_min_per_batch": lineage_policy["min_per_batch"],
        "legacy_fixed_lineage_cap": lineage_policy["fixed_override"],
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

    lineage_cap = _queue_lineage_cap(limit + len(exclude_ids))
    fetch_multiplier = _settings_int(
        "brain_queue_lane_fetch_multiplier",
        4,
        minimum=1,
    )
    lineage_counts: dict[int, int] = {}
    if lineage_cap is not None and lineage_cap > 0 and exclude_ids:
        existing_rows = (
            db.query(ScanPattern)
            .filter(ScanPattern.id.in_(exclude_ids))
            .all()
        )
        lineage_counts = dict(Counter(_lineage_key(row) for row in existing_rows))

    q = db.query(ScanPattern).filter(ScanPattern.active.is_(True))
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
        .limit(max(limit, limit * fetch_multiplier))
        .all()
    )
    out: list[int] = []
    for pattern in rows:
        if len(out) >= limit:
            break
        pattern_id = int(pattern.id)
        lineage = _lineage_key(pattern)
        if (
            lineage_cap is not None
            and lineage_cap > 0
            and lineage_counts.get(lineage, 0) >= lineage_cap
        ):
            continue
        out.append(pattern_id)
        lineage_counts[lineage] = lineage_counts.get(lineage, 0) + 1
    return out
