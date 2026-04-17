"""Phase M.2.c — Pattern x Regime kill-switch / auto-quarantine service.

Daily sweep (default cron 23:05) iterates candidate patterns and
invokes :func:`evaluate_pattern_killswitch`. Consecutive-day streak
logic lives in the pure model; this service is responsible for:

* DB IO to assemble history (prior aggregates of ledger cells).
* Circuit-breaker lookup against ``trading_pattern_regime_killswitch_log``
  so a pattern can't be quarantined more than
  ``brain_pattern_regime_killswitch_max_per_pattern_30d`` times in
  the rolling 30-day window.
* Authoritative-mode approval check + quarantine via
  :func:`lifecycle.transition_on_decay`.
* Audit row + ops log line per evaluation, plus a sweep-summary ops
  line at the end of each sweep.

Shadow / compare mode never calls ``transition_on_decay``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.pattern_regime_m2_ops_log import (
    format_killswitch_ops_line,
)
from .pattern_regime_killswitch_model import (
    DailyExpectancyPoint,
    KillSwitchConfig,
    KillSwitchDecision,
    evaluate_killswitch,
)
from .pattern_regime_ledger_lookup import (
    ResolvedContext,
    load_resolved_context,
    resolved_context_hash,
    summarise_context,
)
from .pattern_regime_m2_common import (
    has_live_approval,
    make_evaluation_id,
    mode_is_active as _mode_is_active_helper,
    mode_is_authoritative as _mode_is_auth_helper,
    normalize_mode,
)

logger = logging.getLogger(__name__)

ACTION_TYPE = "pattern_regime_killswitch"


def _raw_mode() -> str:
    """Effective slice mode.

    Phase M.2-autopilot: DB override table
    (``trading_brain_runtime_modes``) wins over env. Failure to read
    falls back to the env value silently.
    """
    try:
        from .runtime_mode_override import get_runtime_mode_override

        override = get_runtime_mode_override("pattern_regime_killswitch")
        if override is not None:
            return normalize_mode(override)
    except Exception:
        pass
    return normalize_mode(
        getattr(settings, "brain_pattern_regime_killswitch_mode", "off")
    )


def mode_is_active() -> bool:
    if bool(getattr(settings, "brain_pattern_regime_killswitch_kill", False)):
        return False
    return _mode_is_active_helper(_raw_mode())


def mode_is_authoritative() -> bool:
    if bool(getattr(settings, "brain_pattern_regime_killswitch_kill", False)):
        return False
    return _mode_is_auth_helper(_raw_mode())


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_pattern_regime_killswitch_ops_log_enabled", True)
    )


def _build_config() -> KillSwitchConfig:
    return KillSwitchConfig(
        consecutive_days_negative=int(
            getattr(
                settings,
                "brain_pattern_regime_killswitch_consecutive_days",
                3,
            )
        ),
        neg_expectancy_threshold=float(
            getattr(
                settings,
                "brain_pattern_regime_killswitch_neg_expectancy_threshold",
                -0.005,
            )
        ),
        min_confident_dimensions=int(
            getattr(
                settings,
                "brain_pattern_regime_promotion_min_confident_dimensions",
                3,
            )
        ),
        max_per_pattern_30d=int(
            getattr(
                settings,
                "brain_pattern_regime_killswitch_max_per_pattern_30d",
                1,
            )
        ),
    )


@dataclass(frozen=True)
class KillSwitchServiceResult:
    mode: str
    applied: bool
    consumer_quarantine: bool
    reason_code: str
    consecutive_days_negative: int
    evaluation_id: str
    log_id: Optional[int]
    fallback_used: bool


def _recent_quarantine_count(db: Session, *, pattern_id: int, as_of: date) -> int:
    try:
        row = db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM trading_pattern_regime_killswitch_log
                WHERE pattern_id = :pid
                  AND consumer_quarantine = TRUE
                  AND mode = 'authoritative'
                  AND as_of_date >= :cutoff
                """
            ),
            {"pid": int(pattern_id), "cutoff": as_of - timedelta(days=30)},
        ).scalar_one()
        return int(row or 0)
    except Exception:
        return 0


def _history_points(
    db: Session, *, pattern_id: int, as_of: date, lookback_days: int
) -> List[DailyExpectancyPoint]:
    cutoff = as_of - timedelta(days=int(lookback_days))
    try:
        rows = db.execute(
            text(
                """
                SELECT as_of_date,
                       SUM(CASE WHEN has_confidence THEN 1 ELSE 0 END) AS n_confident,
                       AVG(CASE WHEN has_confidence THEN expectancy END) AS mean_exp,
                       MIN(CASE WHEN has_confidence THEN expectancy END) AS worst_exp
                FROM trading_pattern_regime_performance_daily
                WHERE pattern_id = :pid
                  AND as_of_date <= :as_of
                  AND as_of_date >= :cutoff
                GROUP BY as_of_date
                ORDER BY as_of_date ASC
                """
            ),
            {
                "pid": int(pattern_id),
                "as_of": as_of,
                "cutoff": cutoff,
            },
        ).fetchall()
        points: List[DailyExpectancyPoint] = []
        for r in rows:
            points.append(
                DailyExpectancyPoint(
                    as_of_date=r[0],
                    n_confident_dimensions=int(r[1] or 0),
                    mean_expectancy=float(r[2]) if r[2] is not None else None,
                    worst_expectancy=float(r[3]) if r[3] is not None else None,
                )
            )
        return points
    except Exception:
        return []


def evaluate_pattern_killswitch(
    db: Session,
    *,
    pattern_id: int,
    baseline_status: Optional[str],
    as_of_date: Optional[date] = None,
    persist: bool = True,
) -> Optional[KillSwitchServiceResult]:
    try:
        if not mode_is_active():
            return None
        if not pattern_id or not isinstance(pattern_id, int):
            return None

        as_of = as_of_date or datetime.utcnow().date()

        ctx = load_resolved_context(
            db,
            pattern_id=int(pattern_id),
            as_of_date=as_of,
            max_staleness_days=int(
                getattr(
                    settings,
                    "brain_pattern_regime_tilt_max_staleness_days",
                    5,
                )
            ),
        )
        ctx_hash = resolved_context_hash(ctx)
        lookback = int(
            getattr(
                settings,
                "brain_pattern_regime_killswitch_lookback_days",
                14,
            )
        )
        history = _history_points(
            db, pattern_id=int(pattern_id), as_of=as_of, lookback_days=lookback
        )
        config = _build_config()
        recent_q = _recent_quarantine_count(
            db, pattern_id=int(pattern_id), as_of=as_of
        )
        at_breaker = recent_q >= int(config.max_per_pattern_30d)

        decision = evaluate_killswitch(
            ctx,
            history=history,
            config=config,
            at_circuit_breaker=at_breaker,
            baseline_status=baseline_status,
        )

        mode = _raw_mode()
        consumer_quarantine = bool(decision.consumer_quarantine)
        reason = decision.reason_code
        fallback = bool(decision.fallback_used)
        applied = False

        if mode == "authoritative" and consumer_quarantine:
            if has_live_approval(db, action_type=ACTION_TYPE):
                applied = _apply_quarantine(db, pattern_id=int(pattern_id), reason=reason)
            else:
                reason = "refused_authoritative"
                consumer_quarantine = False
                fallback = True
                applied = False

        diff_category: Optional[str] = None
        if baseline_status:
            already_decayed = baseline_status in ("decayed", "retired", "challenged")
            if consumer_quarantine and not already_decayed:
                diff_category = "consumer_quarantines"
            elif not consumer_quarantine and already_decayed:
                diff_category = "baseline_only"
            else:
                diff_category = "agree"
        else:
            diff_category = "unknown"

        eval_id = make_evaluation_id(
            slice_name="killswitch",
            pattern_id=int(pattern_id),
            as_of_date=as_of,
            context_hash=ctx_hash,
        )

        log_id: Optional[int] = None
        if persist:
            log_id = _insert_killswitch_row(
                db,
                evaluation_id=eval_id,
                as_of_date=as_of,
                pattern_id=int(pattern_id),
                mode=mode,
                applied=applied,
                baseline_status=baseline_status,
                consumer_quarantine=consumer_quarantine,
                reason_code=reason,
                diff_category=diff_category,
                decision=decision,
                ctx=ctx,
                ctx_hash=ctx_hash,
                fallback_used=fallback,
            )

        if _ops_log_enabled():
            if reason == "refused_authoritative":
                event = "killswitch_refused_authoritative"
            elif reason == "circuit_breaker":
                event = "killswitch_circuit_breaker"
            elif applied:
                event = "killswitch_applied"
            elif fallback:
                event = "killswitch_fallback"
            else:
                event = "killswitch_evaluated"
            logger.info(
                format_killswitch_ops_line(
                    event=event,
                    mode=mode,
                    pattern_id=int(pattern_id),
                    baseline_status=baseline_status,
                    consumer_quarantine=consumer_quarantine,
                    reason_code=reason,
                    diff_category=diff_category,
                    consecutive_days_negative=int(decision.consecutive_days_negative),
                    worst_dimension=decision.worst_dimension,
                    worst_expectancy=decision.worst_expectancy,
                    n_confident_dimensions=int(decision.n_confident_dimensions),
                    fallback_used=fallback,
                    context_hash=ctx_hash,
                    evaluation_id=eval_id,
                )
            )

        return KillSwitchServiceResult(
            mode=mode,
            applied=applied,
            consumer_quarantine=consumer_quarantine,
            reason_code=reason,
            consecutive_days_negative=int(decision.consecutive_days_negative),
            evaluation_id=eval_id,
            log_id=log_id,
            fallback_used=fallback,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[pattern_regime_killswitch_service] evaluate failed: %s", exc
        )
        return None


def _apply_quarantine(db: Session, *, pattern_id: int, reason: str) -> bool:
    try:
        from ...models.trading import ScanPattern
        from .lifecycle import transition_on_decay

        pattern = (
            db.query(ScanPattern).filter(ScanPattern.id == int(pattern_id)).first()
        )
        if not pattern:
            return False
        transition_on_decay(db, pattern, reason=f"pattern_regime_killswitch:{reason}")
        db.commit()
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[pattern_regime_killswitch_service] transition_on_decay failed for %s: %s",
            pattern_id,
            exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _insert_killswitch_row(
    db: Session,
    *,
    evaluation_id: str,
    as_of_date: date,
    pattern_id: int,
    mode: str,
    applied: bool,
    baseline_status: Optional[str],
    consumer_quarantine: bool,
    reason_code: str,
    diff_category: Optional[str],
    decision: KillSwitchDecision,
    ctx: ResolvedContext,
    ctx_hash: str,
    fallback_used: bool,
) -> Optional[int]:
    payload = {
        "summary": summarise_context(ctx),
        "worst_dimension": decision.worst_dimension,
        "worst_expectancy": decision.worst_expectancy,
    }
    try:
        row = db.execute(
            text(
                """
                INSERT INTO trading_pattern_regime_killswitch_log (
                    evaluation_id, as_of_date, pattern_id, mode, applied,
                    baseline_status, consumer_quarantine, reason_code, diff_category,
                    consecutive_days_negative, worst_dimension, worst_expectancy,
                    n_confident_dimensions, fallback_used, context_hash,
                    payload_json, computed_at
                ) VALUES (
                    :evaluation_id, :as_of_date, :pattern_id, :mode, :applied,
                    :baseline_status, :consumer_quarantine, :reason_code, :diff_category,
                    :streak, :worst_dim, :worst_exp,
                    :n_confident, :fallback, :ctx_hash,
                    CAST(:payload AS JSONB), :now
                )
                RETURNING id
                """
            ),
            {
                "evaluation_id": evaluation_id,
                "as_of_date": as_of_date,
                "pattern_id": int(pattern_id),
                "mode": mode,
                "applied": bool(applied),
                "baseline_status": baseline_status,
                "consumer_quarantine": bool(consumer_quarantine),
                "reason_code": reason_code,
                "diff_category": diff_category,
                "streak": int(decision.consecutive_days_negative),
                "worst_dim": decision.worst_dimension,
                "worst_exp": decision.worst_expectancy,
                "n_confident": int(decision.n_confident_dimensions),
                "fallback": bool(fallback_used),
                "ctx_hash": ctx_hash,
                "payload": json.dumps(payload, default=str, separators=(",", ":")),
                "now": datetime.utcnow(),
            },
        )
        new_id = int(row.scalar_one())
        db.commit()
        return new_id
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[pattern_regime_killswitch_service] insert failed: %s", exc
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


def run_daily_sweep(db: Session, *, as_of_date: Optional[date] = None) -> dict[str, Any]:
    """Sweep all promotable / live patterns through the kill-switch.

    Candidate set = patterns whose ``lifecycle_stage`` is
    ``promoted`` or ``live`` (the only stages where quarantine has
    any product effect). Returns an aggregate summary + emits a
    ``killswitch_sweep_summary`` ops line.
    """
    if not mode_is_active():
        return {"mode": _raw_mode(), "skipped": True, "reason": "mode_off"}

    as_of = as_of_date or datetime.utcnow().date()
    try:
        rows = db.execute(
            text(
                """
                SELECT id, lifecycle_stage
                FROM scan_patterns
                WHERE lifecycle_stage IN ('promoted', 'live')
                ORDER BY id ASC
                """
            )
        ).fetchall()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[pattern_regime_killswitch_service] sweep pattern fetch failed: %s",
            exc,
        )
        rows = []

    evaluated = 0
    quarantined = 0
    refused = 0
    for r in rows:
        pid = int(r[0])
        stage = str(r[1] or "") or None
        result = evaluate_pattern_killswitch(
            db,
            pattern_id=pid,
            baseline_status=stage,
            as_of_date=as_of,
        )
        if result is None:
            continue
        evaluated += 1
        if result.applied and result.consumer_quarantine:
            quarantined += 1
        if result.reason_code == "refused_authoritative":
            refused += 1

    if _ops_log_enabled():
        logger.info(
            format_killswitch_ops_line(
                event="killswitch_sweep_summary",
                mode=_raw_mode(),
                patterns_evaluated=evaluated,
                patterns_quarantined=quarantined,
                reason=f"refused={refused}",
            )
        )

    return {
        "mode": _raw_mode(),
        "as_of_date": as_of.isoformat(),
        "patterns_evaluated": evaluated,
        "patterns_quarantined": quarantined,
        "refused_authoritative": refused,
    }


def diagnostics_summary(
    db: Session, *, lookback_hours: int = 168
) -> dict[str, Any]:
    mode = _raw_mode()
    approval = has_live_approval(db, action_type=ACTION_TYPE)
    total = int(
        db.execute(
            text(
                """
                SELECT COUNT(*) FROM trading_pattern_regime_killswitch_log
                WHERE computed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
                """
            ),
            {"lh": int(lookback_hours)},
        ).scalar_one()
        or 0
    )
    quarantines = int(
        db.execute(
            text(
                """
                SELECT COUNT(*) FROM trading_pattern_regime_killswitch_log
                WHERE computed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
                  AND consumer_quarantine = TRUE
                """
            ),
            {"lh": int(lookback_hours)},
        ).scalar_one()
        or 0
    )
    by_reason = {
        str(r[0]): int(r[1])
        for r in db.execute(
            text(
                """
                SELECT reason_code, COUNT(*) FROM trading_pattern_regime_killswitch_log
                WHERE computed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
                GROUP BY reason_code
                """
            ),
            {"lh": int(lookback_hours)},
        ).fetchall()
    }
    latest = db.execute(
        text(
            """
            SELECT evaluation_id, pattern_id, consumer_quarantine, reason_code,
                   consecutive_days_negative, worst_dimension, mode, applied,
                   computed_at
            FROM trading_pattern_regime_killswitch_log
            ORDER BY computed_at DESC LIMIT 1
            """
        )
    ).fetchone()
    return {
        "mode": mode,
        "approval_live": bool(approval),
        "lookback_hours": int(lookback_hours),
        "total_evaluations": total,
        "total_consumer_quarantines": quarantines,
        "by_reason_code": by_reason,
        "latest": (
            {
                "evaluation_id": latest[0],
                "pattern_id": int(latest[1]) if latest[1] is not None else None,
                "consumer_quarantine": bool(latest[2]),
                "reason_code": latest[3],
                "consecutive_days_negative": (
                    int(latest[4]) if latest[4] is not None else None
                ),
                "worst_dimension": latest[5],
                "mode": latest[6],
                "applied": bool(latest[7]),
                "computed_at": latest[8].isoformat() if latest[8] else None,
            }
            if latest
            else None
        ),
    }


__all__ = [
    "ACTION_TYPE",
    "KillSwitchServiceResult",
    "diagnostics_summary",
    "evaluate_pattern_killswitch",
    "mode_is_active",
    "mode_is_authoritative",
    "run_daily_sweep",
]
