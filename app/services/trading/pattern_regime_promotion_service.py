"""Phase M.2.b — Pattern x Regime promotion-gate service.

Gates ``governance.request_pattern_to_live`` based on the M.1 ledger.
The consumer may *block* a baseline-allow decision, but never
up-grades a baseline-block to an allow (authoritative contract:
never adds risk vs. baseline).

In shadow / compare mode the baseline decision wins; this service
only emits audit rows + ops lines so operators can measure the
gate's impact before turning it authoritative.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.pattern_regime_m2_ops_log import (
    format_promotion_ops_line,
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
from .pattern_regime_promotion_model import (
    PromotionConfig,
    PromotionDecision,
    evaluate_promotion,
)

logger = logging.getLogger(__name__)

ACTION_TYPE = "pattern_regime_promotion"


def _raw_mode() -> str:
    """Effective slice mode.

    Phase M.2-autopilot: DB override table
    (``trading_brain_runtime_modes``) wins over env. Failure to read
    falls back to the env value silently.
    """
    try:
        from .runtime_mode_override import get_runtime_mode_override

        override = get_runtime_mode_override("pattern_regime_promotion")
        if override is not None:
            return normalize_mode(override)
    except Exception:
        pass
    return normalize_mode(
        getattr(settings, "brain_pattern_regime_promotion_mode", "off")
    )


def mode_is_active() -> bool:
    if bool(getattr(settings, "brain_pattern_regime_promotion_kill", False)):
        return False
    return _mode_is_active_helper(_raw_mode())


def mode_is_authoritative() -> bool:
    if bool(getattr(settings, "brain_pattern_regime_promotion_kill", False)):
        return False
    return _mode_is_auth_helper(_raw_mode())


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_pattern_regime_promotion_ops_log_enabled", True)
    )


def _build_config() -> PromotionConfig:
    return PromotionConfig(
        min_confident_dimensions=int(
            getattr(
                settings,
                "brain_pattern_regime_promotion_min_confident_dimensions",
                3,
            )
        ),
        min_blocking_dimensions=int(
            getattr(
                settings,
                "brain_pattern_regime_promotion_block_on_negative_dimensions",
                2,
            )
        ),
        min_mean_expectancy=float(
            getattr(
                settings,
                "brain_pattern_regime_promotion_min_mean_expectancy",
                0.0,
            )
        ),
    )


@dataclass(frozen=True)
class PromotionServiceResult:
    mode: str
    applied: bool
    consumer_allow: bool
    baseline_allow: Optional[bool]
    reason_code: str
    evaluation_id: str
    log_id: Optional[int]
    fallback_used: bool


def evaluate_promotion_for_pattern(
    db: Session,
    *,
    pattern_id: Optional[int],
    baseline_allow: Optional[bool],
    source: Optional[str] = None,
    as_of_date: Optional[date] = None,
    persist: bool = True,
) -> Optional[PromotionServiceResult]:
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
        config = _build_config()
        decision = evaluate_promotion(
            ctx, baseline_allow=baseline_allow, config=config
        )

        mode = _raw_mode()
        consumer_allow = bool(decision.consumer_allow)
        reason = decision.reason_code
        fallback = bool(decision.fallback_used)
        applied = False

        if mode == "authoritative":
            if has_live_approval(db, action_type=ACTION_TYPE):
                applied = True
            else:
                reason = "refused_authoritative"
                # Fall back to baseline — never fabricate an allow,
                # never block without approval.
                consumer_allow = bool(baseline_allow) if baseline_allow is not None else True
                fallback = True
                applied = False

        diff_category: Optional[str]
        if baseline_allow is None:
            diff_category = "unknown"
        elif bool(baseline_allow) == consumer_allow:
            diff_category = "agree"
        else:
            diff_category = "consumer_blocks" if not consumer_allow else "consumer_allows"

        eval_id = make_evaluation_id(
            slice_name="promotion",
            pattern_id=int(pattern_id),
            as_of_date=as_of,
            context_hash=ctx_hash,
        )

        log_id: Optional[int] = None
        if persist:
            log_id = _insert_promotion_row(
                db,
                evaluation_id=eval_id,
                as_of_date=as_of,
                pattern_id=int(pattern_id),
                source=source,
                mode=mode,
                applied=applied,
                baseline_allow=baseline_allow,
                consumer_allow=consumer_allow,
                reason_code=reason,
                diff_category=diff_category,
                decision=decision,
                ctx=ctx,
                ctx_hash=ctx_hash,
                fallback_used=fallback,
            )

        if _ops_log_enabled():
            event = (
                "promotion_refused_authoritative"
                if reason == "refused_authoritative"
                else (
                    "promotion_applied"
                    if applied
                    else (
                        "promotion_fallback"
                        if fallback
                        else "promotion_evaluated"
                    )
                )
            )
            logger.info(
                format_promotion_ops_line(
                    event=event,
                    mode=mode,
                    pattern_id=int(pattern_id),
                    source=source,
                    baseline_allow=baseline_allow,
                    consumer_allow=consumer_allow,
                    reason_code=reason,
                    diff_category=diff_category,
                    n_confident_dimensions=decision.n_confident_dimensions,
                    n_blocking_dimensions=len(decision.blocking_dimensions),
                    fallback_used=fallback,
                    context_hash=ctx_hash,
                    evaluation_id=eval_id,
                )
            )

        return PromotionServiceResult(
            mode=mode,
            applied=applied,
            consumer_allow=consumer_allow,
            baseline_allow=baseline_allow,
            reason_code=reason,
            evaluation_id=eval_id,
            log_id=log_id,
            fallback_used=fallback,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[pattern_regime_promotion_service] evaluate failed: %s", exc
        )
        return None


def _insert_promotion_row(
    db: Session,
    *,
    evaluation_id: str,
    as_of_date: date,
    pattern_id: int,
    source: Optional[str],
    mode: str,
    applied: bool,
    baseline_allow: Optional[bool],
    consumer_allow: bool,
    reason_code: str,
    diff_category: Optional[str],
    decision: PromotionDecision,
    ctx: ResolvedContext,
    ctx_hash: str,
    fallback_used: bool,
) -> Optional[int]:
    payload = {
        "summary": summarise_context(ctx),
        "blocking_dimensions": decision.blocking_dimensions,
        "mean_expectancy": decision.mean_expectancy,
    }
    try:
        row = db.execute(
            text(
                """
                INSERT INTO trading_pattern_regime_promotion_log (
                    evaluation_id, as_of_date, pattern_id, mode, applied,
                    baseline_allow, consumer_allow, reason_code, diff_category,
                    blocking_dimensions, n_confident_dimensions,
                    fallback_used, source, context_hash, payload_json, computed_at
                ) VALUES (
                    :evaluation_id, :as_of_date, :pattern_id, :mode, :applied,
                    :baseline_allow, :consumer_allow, :reason_code, :diff_category,
                    CAST(:blocking AS JSONB), :n_confident,
                    :fallback, :source, :ctx_hash, CAST(:payload AS JSONB), :now
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
                "baseline_allow": (
                    bool(baseline_allow) if baseline_allow is not None else None
                ),
                "consumer_allow": bool(consumer_allow),
                "reason_code": reason_code,
                "diff_category": diff_category,
                "blocking": json.dumps(
                    decision.blocking_dimensions,
                    default=str,
                    separators=(",", ":"),
                ),
                "n_confident": int(decision.n_confident_dimensions),
                "fallback": bool(fallback_used),
                "source": source,
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
            "[pattern_regime_promotion_service] insert failed: %s", exc
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


def diagnostics_summary(
    db: Session, *, lookback_hours: int = 168
) -> dict[str, Any]:
    mode = _raw_mode()
    approval = has_live_approval(db, action_type=ACTION_TYPE)
    total = int(
        db.execute(
            text(
                """
                SELECT COUNT(*) FROM trading_pattern_regime_promotion_log
                WHERE computed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
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
                SELECT reason_code, COUNT(*) FROM trading_pattern_regime_promotion_log
                WHERE computed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
                GROUP BY reason_code
                """
            ),
            {"lh": int(lookback_hours)},
        ).fetchall()
    }
    by_diff = {
        str(r[0] or "unknown"): int(r[1])
        for r in db.execute(
            text(
                """
                SELECT diff_category, COUNT(*) FROM trading_pattern_regime_promotion_log
                WHERE computed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
                GROUP BY diff_category
                """
            ),
            {"lh": int(lookback_hours)},
        ).fetchall()
    }
    blocks = int(
        db.execute(
            text(
                """
                SELECT COUNT(*) FROM trading_pattern_regime_promotion_log
                WHERE computed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
                  AND consumer_allow = FALSE
                """
            ),
            {"lh": int(lookback_hours)},
        ).scalar_one()
        or 0
    )
    latest = db.execute(
        text(
            """
            SELECT evaluation_id, pattern_id, baseline_allow, consumer_allow,
                   reason_code, diff_category, mode, applied, computed_at
            FROM trading_pattern_regime_promotion_log
            ORDER BY computed_at DESC LIMIT 1
            """
        )
    ).fetchone()
    return {
        "mode": mode,
        "approval_live": bool(approval),
        "lookback_hours": int(lookback_hours),
        "total_evaluations": total,
        "total_consumer_blocks": int(blocks),
        "by_reason_code": by_reason,
        "by_diff_category": by_diff,
        "latest": (
            {
                "evaluation_id": latest[0],
                "pattern_id": int(latest[1]) if latest[1] is not None else None,
                "baseline_allow": (
                    bool(latest[2]) if latest[2] is not None else None
                ),
                "consumer_allow": bool(latest[3]),
                "reason_code": latest[4],
                "diff_category": latest[5],
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
    "PromotionServiceResult",
    "diagnostics_summary",
    "evaluate_promotion_for_pattern",
    "mode_is_active",
    "mode_is_authoritative",
]
