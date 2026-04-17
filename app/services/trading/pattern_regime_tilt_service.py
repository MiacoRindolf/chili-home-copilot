"""Phase M.2.a — Pattern x Regime sizing-tilt service.

Glue layer that turns a
:class:`~app.services.trading.pattern_regime_ledger_lookup.ResolvedContext`
into a persisted audit row + ops log line, and returns a
:class:`~app.services.trading.pattern_regime_tilt_model.TiltDecision`
the caller can apply (authoritative) or mirror (shadow / compare).

Public entry point: :func:`evaluate_tilt_for_proposal`. The
``position_sizer_emitter`` hook calls it AFTER writing the Phase H
proposal so the M.2 audit row can reference the proposal notional
as ``baseline_size_dollars``. In shadow / compare mode the Phase H
notional is never mutated.

Authoritative mode requires a live approval row
(``action_type='pattern_regime_tilt'``); missing / expired approval
triggers a ``tilt_refused_authoritative`` ops line and the service
falls back to multiplier = 1.0.
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
    format_tilt_ops_line,
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
from .pattern_regime_tilt_model import (
    TiltConfig,
    TiltDecision,
    classify_diff,
    compute_tilt_multiplier,
)

logger = logging.getLogger(__name__)

ACTION_TYPE = "pattern_regime_tilt"


# ---------------------------------------------------------------------------
# Mode helpers
# ---------------------------------------------------------------------------


def _raw_mode() -> str:
    """Effective slice mode.

    Phase M.2-autopilot introduced a DB override table
    (``trading_brain_runtime_modes``). If the autopilot has written a
    row for this slice, it takes precedence over the env setting so
    mode advances/reverts do not require a service restart. If the DB
    lookup fails for any reason, we fall back to the env value so a
    DB hiccup can never worsen the slice's behavior.
    """
    try:
        from .runtime_mode_override import get_runtime_mode_override

        override = get_runtime_mode_override("pattern_regime_tilt")
        if override is not None:
            return normalize_mode(override)
    except Exception:
        pass
    return normalize_mode(getattr(settings, "brain_pattern_regime_tilt_mode", "off"))


def mode_is_active() -> bool:
    if bool(getattr(settings, "brain_pattern_regime_tilt_kill", False)):
        return False
    return _mode_is_active_helper(_raw_mode())


def mode_is_authoritative() -> bool:
    if bool(getattr(settings, "brain_pattern_regime_tilt_kill", False)):
        return False
    return _mode_is_auth_helper(_raw_mode())


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_pattern_regime_tilt_ops_log_enabled", True))


def _build_config() -> TiltConfig:
    return TiltConfig(
        min_multiplier=float(
            getattr(settings, "brain_pattern_regime_tilt_min_multiplier", 0.25)
        ),
        max_multiplier=float(
            getattr(settings, "brain_pattern_regime_tilt_max_multiplier", 2.00)
        ),
        min_confident_dimensions=int(
            getattr(
                settings,
                "brain_pattern_regime_tilt_min_confident_dimensions",
                3,
            )
        ),
    )


# ---------------------------------------------------------------------------
# Result dataclass returned to callers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TiltServiceResult:
    mode: str
    applied: bool
    multiplier: float
    reason_code: str
    baseline_notional: Optional[float]
    consumer_notional: Optional[float]
    diff_category: Optional[str]
    evaluation_id: str
    log_id: Optional[int]
    fallback_used: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_tilt_for_proposal(
    db: Session,
    *,
    pattern_id: Optional[int],
    ticker: Optional[str],
    source: Optional[str],
    baseline_notional: Optional[float],
    as_of_date: Optional[date] = None,
    persist: bool = True,
) -> Optional[TiltServiceResult]:
    """Compute + persist the tilt audit row for a single proposal.

    Returns ``None`` when the slice is off, inputs are invalid, or
    any unexpected error occurs. Never raises.
    """
    try:
        if not mode_is_active():
            return None
        if not pattern_id or not isinstance(pattern_id, int):
            return None
        if baseline_notional is None or baseline_notional <= 0:
            # Nothing to tilt against — still log as skipped? We
            # early-return; consumers get the legacy sizing.
            return None

        as_of = as_of_date or datetime.utcnow().date()
        max_stale = int(
            getattr(
                settings, "brain_pattern_regime_tilt_max_staleness_days", 5
            )
        )

        ctx = load_resolved_context(
            db,
            pattern_id=int(pattern_id),
            as_of_date=as_of,
            max_staleness_days=max_stale,
        )
        ctx_hash = resolved_context_hash(ctx)
        config = _build_config()
        decision = compute_tilt_multiplier(ctx, config=config)

        mode = _raw_mode()
        applied = False
        final_multiplier = float(decision.multiplier)
        reason = decision.reason_code
        fallback = bool(decision.fallback_used)

        # Authoritative gate: needs live approval row. Refuse
        # otherwise and fall back to neutral multiplier.
        if mode == "authoritative":
            if has_live_approval(db, action_type=ACTION_TYPE):
                applied = True
            else:
                reason = "refused_authoritative"
                final_multiplier = 1.0
                fallback = True
                applied = False

        consumer_notional = None
        if baseline_notional is not None:
            consumer_notional = float(baseline_notional) * final_multiplier

        diff_category = classify_diff(
            float(baseline_notional) if baseline_notional is not None else None,
            consumer_notional,
        )

        eval_id = make_evaluation_id(
            slice_name="tilt",
            pattern_id=int(pattern_id),
            as_of_date=as_of,
            context_hash=ctx_hash,
        )

        log_id: Optional[int] = None
        if persist:
            log_id = _insert_tilt_row(
                db,
                evaluation_id=eval_id,
                as_of_date=as_of,
                pattern_id=int(pattern_id),
                ticker=ticker,
                source=source,
                mode=mode,
                applied=applied,
                baseline_notional=baseline_notional,
                consumer_notional=consumer_notional,
                multiplier=final_multiplier,
                reason_code=reason,
                diff_category=diff_category,
                decision=decision,
                ctx=ctx,
                ctx_hash=ctx_hash,
                fallback_used=fallback,
            )

        if _ops_log_enabled():
            event = (
                "tilt_refused_authoritative"
                if reason == "refused_authoritative"
                else (
                    "tilt_applied"
                    if applied
                    else (
                        "tilt_fallback"
                        if fallback
                        else "tilt_computed"
                    )
                )
            )
            logger.info(
                format_tilt_ops_line(
                    event=event,
                    mode=mode,
                    pattern_id=int(pattern_id),
                    ticker=ticker,
                    source=source,
                    multiplier=final_multiplier,
                    baseline_size_dollars=(
                        float(baseline_notional)
                        if baseline_notional is not None
                        else None
                    ),
                    consumer_size_dollars=consumer_notional,
                    reason_code=reason,
                    diff_category=diff_category,
                    n_confident_dimensions=decision.n_confident_dimensions,
                    fallback_used=fallback,
                    context_hash=ctx_hash,
                    evaluation_id=eval_id,
                )
            )

        return TiltServiceResult(
            mode=mode,
            applied=applied,
            multiplier=final_multiplier,
            reason_code=reason,
            baseline_notional=(
                float(baseline_notional) if baseline_notional is not None else None
            ),
            consumer_notional=consumer_notional,
            diff_category=diff_category,
            evaluation_id=eval_id,
            log_id=log_id,
            fallback_used=fallback,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[pattern_regime_tilt_service] evaluate failed: %s", exc)
        return None


def _insert_tilt_row(
    db: Session,
    *,
    evaluation_id: str,
    as_of_date: date,
    pattern_id: int,
    ticker: Optional[str],
    source: Optional[str],
    mode: str,
    applied: bool,
    baseline_notional: Optional[float],
    consumer_notional: Optional[float],
    multiplier: float,
    reason_code: str,
    diff_category: Optional[str],
    decision: TiltDecision,
    ctx: ResolvedContext,
    ctx_hash: str,
    fallback_used: bool,
) -> Optional[int]:
    payload = {
        "summary": summarise_context(ctx),
        "contributing_dimensions": decision.contributing_dimensions,
        "mean_expectancy": decision.mean_expectancy,
    }
    try:
        row = db.execute(
            text(
                """
                INSERT INTO trading_pattern_regime_tilt_log (
                    evaluation_id, as_of_date, pattern_id, ticker, source,
                    mode, applied, baseline_size_dollars, consumer_size_dollars,
                    multiplier, reason_code, diff_category,
                    contributing_dimensions, n_confident_dimensions,
                    fallback_used, context_hash, payload_json, computed_at
                ) VALUES (
                    :evaluation_id, :as_of_date, :pattern_id, :ticker, :source,
                    :mode, :applied, :baseline, :consumer,
                    :mult, :reason_code, :diff_category,
                    CAST(:contrib AS JSONB), :n_confident,
                    :fallback, :ctx_hash, CAST(:payload AS JSONB), :now
                )
                RETURNING id
                """
            ),
            {
                "evaluation_id": evaluation_id,
                "as_of_date": as_of_date,
                "pattern_id": int(pattern_id),
                "ticker": ticker,
                "source": source,
                "mode": mode,
                "applied": bool(applied),
                "baseline": (
                    float(baseline_notional) if baseline_notional is not None else None
                ),
                "consumer": (
                    float(consumer_notional) if consumer_notional is not None else None
                ),
                "mult": float(multiplier),
                "reason_code": reason_code,
                "diff_category": diff_category,
                "contrib": json.dumps(
                    decision.contributing_dimensions, default=str, separators=(",", ":")
                ),
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
            "[pattern_regime_tilt_service] insert failed: %s", exc
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Diagnostics summary
# ---------------------------------------------------------------------------


def diagnostics_summary(
    db: Session, *, lookback_hours: int = 24
) -> dict[str, Any]:
    mode = _raw_mode()
    approval = has_live_approval(db, action_type=ACTION_TYPE)
    total = int(
        db.execute(
            text(
                """
                SELECT COUNT(*) FROM trading_pattern_regime_tilt_log
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
                SELECT reason_code, COUNT(*) FROM trading_pattern_regime_tilt_log
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
                SELECT diff_category, COUNT(*) FROM trading_pattern_regime_tilt_log
                WHERE computed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
                GROUP BY diff_category
                """
            ),
            {"lh": int(lookback_hours)},
        ).fetchall()
    }
    mean_row = db.execute(
        text(
            """
            SELECT AVG(multiplier), AVG(n_confident_dimensions)
            FROM trading_pattern_regime_tilt_log
            WHERE computed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
            """
        ),
        {"lh": int(lookback_hours)},
    ).fetchone()
    latest = db.execute(
        text(
            """
            SELECT evaluation_id, pattern_id, multiplier, reason_code,
                   diff_category, mode, applied, computed_at
            FROM trading_pattern_regime_tilt_log
            ORDER BY computed_at DESC LIMIT 1
            """
        )
    ).fetchone()
    return {
        "mode": mode,
        "approval_live": bool(approval),
        "lookback_hours": int(lookback_hours),
        "total_evaluations": total,
        "by_reason_code": by_reason,
        "by_diff_category": by_diff,
        "mean_multiplier": (
            float(mean_row[0]) if mean_row and mean_row[0] is not None else None
        ),
        "mean_confident_dimensions": (
            float(mean_row[1]) if mean_row and mean_row[1] is not None else None
        ),
        "latest": (
            {
                "evaluation_id": latest[0],
                "pattern_id": int(latest[1]) if latest[1] is not None else None,
                "multiplier": float(latest[2]) if latest[2] is not None else None,
                "reason_code": latest[3],
                "diff_category": latest[4],
                "mode": latest[5],
                "applied": bool(latest[6]),
                "computed_at": latest[7].isoformat() if latest[7] else None,
            }
            if latest
            else None
        ),
    }


__all__ = [
    "ACTION_TYPE",
    "TiltServiceResult",
    "diagnostics_summary",
    "evaluate_tilt_for_proposal",
    "mode_is_active",
    "mode_is_authoritative",
]
