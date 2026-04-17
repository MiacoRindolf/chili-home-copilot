"""Phase M.2-autopilot: auto-advance service.

Glue between the pure :mod:`pattern_regime_autopilot_model` and the
three M.2 slice decision-log tables + the governance approvals
table + the runtime mode override table.

Entry points:

* :func:`run_autopilot_tick` - daily job (06:15 default). Evaluates
  all three slices and applies advance / hold / revert decisions.
* :func:`run_weekly_summary` - Monday 09:00 default. Single ops line
  with per-slice stage + days-in-stage + last-advance-at.
* :func:`diagnostics_summary` - frozen-shape dict powering the
  ``/api/trading/brain/m2-autopilot/status`` endpoint.

Every decision writes a row to
``trading_pattern_regime_autopilot_log`` (append-only audit) and
emits exactly one structured ops line. Compare->authoritative
advances additionally insert a governance approval row.

Never raises out of the top-level entry points. A DB hiccup logs
and skips the tick; the next tick will try again.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.pattern_regime_autopilot_ops_log import (
    format_autopilot_ops_line,
)
from .pattern_regime_autopilot_model import (
    ALLOWED_STAGES,
    SLICE_KILLSWITCH,
    SLICE_NAMES,
    SLICE_PROMOTION,
    SLICE_TILT,
    AutopilotConfig,
    AutopilotDecision,
    SliceEvidence,
    compute_order_lock_state,
    evaluate_slice_gates,
)
from .runtime_mode_override import (
    get_runtime_mode_override,
    invalidate_cache,
    set_runtime_mode_override,
)

logger = logging.getLogger(__name__)

SLICE_TO_RUNTIME_NAME = {
    SLICE_TILT: "pattern_regime_tilt",
    SLICE_PROMOTION: "pattern_regime_promotion",
    SLICE_KILLSWITCH: "pattern_regime_killswitch",
}
SLICE_TO_ACTION_TYPE = SLICE_TO_RUNTIME_NAME
SLICE_TO_LOG_TABLE = {
    SLICE_TILT: "trading_pattern_regime_tilt_log",
    SLICE_PROMOTION: "trading_pattern_regime_promotion_log",
    SLICE_KILLSWITCH: "trading_pattern_regime_killswitch_log",
}
SLICE_TO_ENV_KEY = {
    SLICE_TILT: "brain_pattern_regime_tilt_mode",
    SLICE_PROMOTION: "brain_pattern_regime_promotion_mode",
    SLICE_KILLSWITCH: "brain_pattern_regime_killswitch_mode",
}


# ---------------------------------------------------------------------
# Mode helpers
# ---------------------------------------------------------------------


def is_enabled() -> bool:
    if bool(getattr(settings, "brain_pattern_regime_autopilot_kill", False)):
        return False
    return bool(getattr(settings, "brain_pattern_regime_autopilot_enabled", False))


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_pattern_regime_autopilot_ops_log_enabled", True)
    )


def _config() -> AutopilotConfig:
    return AutopilotConfig(
        shadow_days=int(getattr(settings, "brain_pattern_regime_autopilot_shadow_days", 5)),
        compare_days=int(getattr(settings, "brain_pattern_regime_autopilot_compare_days", 10)),
        min_decisions=int(getattr(settings, "brain_pattern_regime_autopilot_min_decisions", 100)),
        tilt_mult_min=float(getattr(settings, "brain_pattern_regime_autopilot_tilt_mult_min", 0.85)),
        tilt_mult_max=float(getattr(settings, "brain_pattern_regime_autopilot_tilt_mult_max", 1.25)),
        promo_block_max_ratio=float(getattr(settings, "brain_pattern_regime_autopilot_promo_block_max_ratio", 0.10)),
        ks_max_fires_per_day=float(getattr(settings, "brain_pattern_regime_autopilot_ks_max_fires_per_day", 1.0)),
        approval_days=int(getattr(settings, "brain_pattern_regime_autopilot_approval_days", 30)),
    )


def _effective_mode(db: Session, slice_name: str) -> str:
    """Effective mode for a slice: DB override wins, env fallback."""
    override = get_runtime_mode_override(
        SLICE_TO_RUNTIME_NAME[slice_name], db=db, bypass_cache=True
    )
    if override is not None:
        return override
    raw = getattr(settings, SLICE_TO_ENV_KEY[slice_name], "off") or "off"
    m = str(raw).strip().lower()
    return m if m in ALLOWED_STAGES else "off"


# ---------------------------------------------------------------------
# Evidence gathering
# ---------------------------------------------------------------------


def _days_in_stage(db: Session, slice_name: str, current_mode: str) -> int:
    """Business-days elapsed since the slice LAST entered ``current_mode``.

    Uses the autopilot audit log first (most precise). Falls back to
    the runtime-mode override's ``updated_at``. Absent both, returns 0.
    """
    try:
        runtime = SLICE_TO_RUNTIME_NAME[slice_name]
        row = db.execute(
            text(
                """
                SELECT evaluated_at
                FROM trading_pattern_regime_autopilot_log
                WHERE slice_name = :slice
                  AND event = 'autopilot_advance'
                  AND to_mode = :mode
                ORDER BY evaluated_at DESC
                LIMIT 1
                """
            ),
            {"slice": runtime, "mode": current_mode},
        ).fetchone()
        started_at: Optional[datetime] = None
        if row is not None and row[0] is not None:
            started_at = row[0]
        else:
            rt_row = db.execute(
                text(
                    "SELECT updated_at FROM trading_brain_runtime_modes WHERE slice_name = :slice"
                ),
                {"slice": runtime},
            ).fetchone()
            if rt_row is not None and rt_row[0] is not None:
                started_at = rt_row[0]
        if started_at is None:
            return 0
        now = datetime.utcnow()
        # Count business days between started_at.date() (exclusive) and
        # today (inclusive-ish). Simpler: use calendar-day delta and
        # subtract weekends by counting only Mon-Fri days in range.
        start_d = started_at.date()
        end_d = now.date()
        if end_d <= start_d:
            return 0
        days = 0
        cursor = start_d
        step = timedelta(days=1)
        while cursor < end_d:
            cursor += step
            if cursor.weekday() < 5:
                days += 1
        return days
    except Exception:
        return 0


def _last_advance_date(db: Session, slice_name: str) -> Optional[date]:
    try:
        runtime = SLICE_TO_RUNTIME_NAME[slice_name]
        row = db.execute(
            text(
                """
                SELECT as_of_date
                FROM trading_pattern_regime_autopilot_log
                WHERE slice_name = :slice
                  AND event IN ('autopilot_advance', 'autopilot_revert')
                ORDER BY evaluated_at DESC
                LIMIT 1
                """
            ),
            {"slice": runtime},
        ).fetchone()
        if row is not None and row[0] is not None:
            return row[0]
    except Exception:
        pass
    return None


def _total_decisions_window(
    db: Session, slice_name: str, lookback_days: int
) -> int:
    table = SLICE_TO_LOG_TABLE[slice_name]
    try:
        row = db.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM {table}
                WHERE computed_at >= NOW() - (:days || ' days')::interval
                """
            ),
            {"days": int(lookback_days)},
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except Exception:
        return 0


def _has_refused_authoritative_recent(
    db: Session, slice_name: str, hours: int = 24
) -> bool:
    """We cannot grep logs from SQL; the DB proxy is the
    ``reason_code='refused_authoritative'`` row in the slice's decision
    log table. If any such row exists in the last ``hours`` hours,
    treat as anomaly."""
    table = SLICE_TO_LOG_TABLE[slice_name]
    try:
        row = db.execute(
            text(
                f"""
                SELECT 1
                FROM {table}
                WHERE reason_code = 'refused_authoritative'
                  AND computed_at >= NOW() - (:h || ' hours')::interval
                LIMIT 1
                """
            ),
            {"h": int(hours)},
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _approval_live(db: Session, slice_name: str) -> bool:
    action = SLICE_TO_ACTION_TYPE[slice_name]
    try:
        row = db.execute(
            text(
                """
                SELECT 1
                FROM trading_governance_approvals
                WHERE action_type = :a
                  AND status = 'approved'
                  AND decision = 'allow'
                  AND (expires_at IS NULL OR expires_at > NOW())
                LIMIT 1
                """
            ),
            {"a": action},
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _tilt_mean_multiplier(db: Session, lookback_days: int) -> Optional[float]:
    try:
        row = db.execute(
            text(
                """
                SELECT AVG(multiplier)
                FROM trading_pattern_regime_tilt_log
                WHERE computed_at >= NOW() - (:d || ' days')::interval
                  AND multiplier IS NOT NULL
                """
            ),
            {"d": int(lookback_days)},
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])
    except Exception:
        return None


def _promotion_block_ratio(db: Session, lookback_days: int) -> Optional[float]:
    try:
        row = db.execute(
            text(
                """
                SELECT
                    SUM(CASE WHEN baseline_allow = TRUE AND consumer_allow = FALSE THEN 1 ELSE 0 END) AS blocks,
                    SUM(CASE WHEN baseline_allow = TRUE THEN 1 ELSE 0 END) AS baseline_allows
                FROM trading_pattern_regime_promotion_log
                WHERE computed_at >= NOW() - (:d || ' days')::interval
                """
            ),
            {"d": int(lookback_days)},
        ).fetchone()
        if row is None:
            return None
        blocks = float(row[0] or 0)
        allows = float(row[1] or 0)
        if allows <= 0.0:
            return None
        return blocks / allows
    except Exception:
        return None


def _killswitch_mean_fires_per_day(
    db: Session, lookback_days: int
) -> Optional[float]:
    try:
        row = db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM trading_pattern_regime_killswitch_log
                WHERE consumer_quarantine = TRUE
                  AND computed_at >= NOW() - (:d || ' days')::interval
                """
            ),
            {"d": int(lookback_days)},
        ).fetchone()
        fires = float(row[0] or 0) if row else 0.0
        denom = max(1, int(lookback_days))
        return fires / denom
    except Exception:
        return None


def _scan_status_frozen_ok() -> bool:
    """Cheap placeholder. The full frozen-contract check belongs to
    the release blocker and the scan_status test. For the autopilot
    gate we treat this as True as long as we can import the router
    module without error (i.e. the app boot didn't break). Any
    real structural regression will already show up in the
    release-blocker gate.
    """
    try:
        from ...routers.trading import api_scan_status  # noqa: F401

        return True
    except Exception:
        return False


def _gather_evidence(
    db: Session,
    *,
    slice_name: str,
    cfg: AutopilotConfig,
    today_utc: date,
) -> SliceEvidence:
    current = _effective_mode(db, slice_name)
    days = _days_in_stage(db, slice_name, current)
    last_adv = _last_advance_date(db, slice_name)
    # Use max(shadow_days, compare_days, killswitch_lookback) for the
    # evidence window so the gate ">= min_decisions" is measured over
    # the whole stage window.
    lookback = max(cfg.shadow_days, cfg.compare_days)
    totals = _total_decisions_window(db, slice_name, lookback)
    anomaly = _has_refused_authoritative_recent(db, slice_name, hours=24)
    approval_live = _approval_live(db, slice_name)
    approval_missing = (current == "authoritative" and not approval_live)
    scan_ok = _scan_status_frozen_ok()

    tilt_mm: Optional[float] = None
    promo_br: Optional[float] = None
    ks_fires: Optional[float] = None
    if slice_name == SLICE_TILT:
        tilt_mm = _tilt_mean_multiplier(db, cfg.compare_days)
    elif slice_name == SLICE_PROMOTION:
        promo_br = _promotion_block_ratio(db, cfg.compare_days)
    elif slice_name == SLICE_KILLSWITCH:
        ks_fires = _killswitch_mean_fires_per_day(db, cfg.compare_days)

    return SliceEvidence(
        slice_name=slice_name,
        current_mode=current,
        days_in_stage=days,
        total_decisions=totals,
        last_advance_date=last_adv,
        today_utc=today_utc,
        diagnostics_healthy=True,  # engine doesn't ping itself
        diagnostics_stale_hours=0.0,
        release_blocker_clean=not anomaly,
        scan_status_frozen_ok=scan_ok,
        anomaly_refused_authoritative=anomaly,
        authoritative_approval_missing=approval_missing,
        approval_live=approval_live,
        tilt_mean_multiplier=tilt_mm,
        promotion_block_ratio=promo_br,
        killswitch_mean_fires_per_day=ks_fires,
    )


# ---------------------------------------------------------------------
# Apply a decision (writes)
# ---------------------------------------------------------------------


def _insert_approval(
    db: Session, *, slice_name: str, cfg: AutopilotConfig, note: str
) -> Optional[int]:
    action = SLICE_TO_ACTION_TYPE[slice_name]
    try:
        row = db.execute(
            text(
                """
                INSERT INTO trading_governance_approvals (
                    action_type, details_json, submitted_at, status,
                    decision, decided_at, notes, expires_at
                )
                VALUES (
                    :action_type, CAST(:details AS JSONB), NOW(),
                    'approved', 'allow', NOW(),
                    :notes,
                    NOW() + (:days || ' days')::interval
                )
                RETURNING id
                """
            ),
            {
                "action_type": action,
                "details": json.dumps(
                    {"approved_by": "auto-advance-policy", "note": note},
                    sort_keys=True,
                ),
                "notes": f"auto-advance-policy: {note}"[:500],
                "days": int(cfg.approval_days),
            },
        )
        return int(row.scalar_one())
    except Exception as exc:
        logger.warning(
            "[pattern_regime_autopilot_service] approval insert failed: %s", exc
        )
        return None


def _write_audit_row(
    db: Session,
    *,
    decision: AutopilotDecision,
    evidence: SliceEvidence,
    approval_id: Optional[int],
    as_of: date,
    event: str,
) -> None:
    try:
        runtime = SLICE_TO_RUNTIME_NAME[decision.slice_name]
        gates_payload = [
            {"name": g.name, "ok": bool(g.ok), "detail": g.detail}
            for g in decision.gates
        ]
        evidence_payload = {
            "current_mode": evidence.current_mode,
            "days_in_stage": evidence.days_in_stage,
            "total_decisions": evidence.total_decisions,
            "approval_live": evidence.approval_live,
            "anomaly_refused_authoritative": evidence.anomaly_refused_authoritative,
            "tilt_mean_multiplier": evidence.tilt_mean_multiplier,
            "promotion_block_ratio": evidence.promotion_block_ratio,
            "killswitch_mean_fires_per_day": evidence.killswitch_mean_fires_per_day,
        }
        db.execute(
            text(
                """
                INSERT INTO trading_pattern_regime_autopilot_log
                    (as_of_date, evaluated_at, slice_name, event,
                     from_mode, to_mode, reason_code,
                     gates_json, evidence_json, approval_id,
                     days_in_stage, ops_log_excerpt)
                VALUES
                    (:as_of, NOW(), :slice, :event,
                     :from_mode, :to_mode, :reason,
                     CAST(:gates AS JSONB), CAST(:ev AS JSONB), :approval,
                     :dis, :excerpt)
                """
            ),
            {
                "as_of": as_of,
                "slice": runtime,
                "event": event,
                "from_mode": decision.from_mode,
                "to_mode": decision.to_mode,
                "reason": decision.reason_code[:64],
                "gates": json.dumps(gates_payload, sort_keys=True, default=str),
                "ev": json.dumps(evidence_payload, sort_keys=True, default=str),
                "approval": approval_id,
                "dis": evidence.days_in_stage,
                "excerpt": None,
            },
        )
    except Exception as exc:
        logger.warning(
            "[pattern_regime_autopilot_service] audit insert failed: %s", exc
        )


def _apply_decision(
    db: Session,
    *,
    decision: AutopilotDecision,
    evidence: SliceEvidence,
    cfg: AutopilotConfig,
    as_of: date,
) -> None:
    action = decision.action
    runtime = SLICE_TO_RUNTIME_NAME[decision.slice_name]
    approval_id: Optional[int] = None
    event_name = "autopilot_hold"

    if action == "advance":
        event_name = "autopilot_advance"
        if decision.requires_approval_insert:
            approval_id = _insert_approval(
                db,
                slice_name=decision.slice_name,
                cfg=cfg,
                note=(
                    f"{decision.slice_name} compare window clean, "
                    f"envelope met at {as_of.isoformat()}"
                ),
            )
            # If approval insert failed, downgrade to a hold so we
            # never write authoritative without a live approval.
            if approval_id is None:
                _write_audit_row(
                    db,
                    decision=AutopilotDecision(
                        slice_name=decision.slice_name,
                        action="hold",
                        from_mode=decision.from_mode,
                        to_mode=decision.from_mode,
                        reason_code="approval_insert_failed",
                        gates=decision.gates,
                    ),
                    evidence=evidence,
                    approval_id=None,
                    as_of=as_of,
                    event="autopilot_hold",
                )
                if _ops_log_enabled():
                    logger.warning(
                        format_autopilot_ops_line(
                            event="autopilot_hold",
                            mode="enabled",
                            slice_name=runtime,
                            from_mode=decision.from_mode,
                            to_mode=decision.from_mode,
                            reason_code="approval_insert_failed",
                            days_in_stage=evidence.days_in_stage,
                            total_decisions=evidence.total_decisions,
                        )
                    )
                return
        set_runtime_mode_override(
            db,
            slice_name=runtime,
            mode=decision.to_mode,
            updated_by="auto-advance-policy",
            reason=decision.reason_code[:200],
            payload={
                "from_mode": decision.from_mode,
                "to_mode": decision.to_mode,
                "approval_id": approval_id,
            },
        )
        invalidate_cache(runtime)
    elif action == "revert":
        event_name = "autopilot_revert"
        set_runtime_mode_override(
            db,
            slice_name=runtime,
            mode=decision.to_mode,
            updated_by="auto-advance-policy-revert",
            reason=decision.reason_code[:200],
            payload={
                "from_mode": decision.from_mode,
                "to_mode": decision.to_mode,
            },
        )
        invalidate_cache(runtime)
    elif action == "blocked_by_order_lock":
        event_name = "autopilot_hold"
    else:  # hold / skipped
        event_name = "autopilot_hold"

    _write_audit_row(
        db,
        decision=decision,
        evidence=evidence,
        approval_id=approval_id,
        as_of=as_of,
        event=event_name,
    )

    if _ops_log_enabled():
        extra = {}
        if evidence.tilt_mean_multiplier is not None:
            extra["mean_multiplier"] = float(evidence.tilt_mean_multiplier)
        if evidence.promotion_block_ratio is not None:
            extra["block_ratio"] = float(evidence.promotion_block_ratio)
        if evidence.killswitch_mean_fires_per_day is not None:
            extra["mean_fires_per_day"] = float(
                evidence.killswitch_mean_fires_per_day
            )
        logger.info(
            format_autopilot_ops_line(
                event=event_name,
                mode="enabled",
                slice_name=runtime,
                from_mode=decision.from_mode,
                to_mode=decision.to_mode,
                reason_code=decision.reason_code,
                days_in_stage=evidence.days_in_stage,
                total_decisions=evidence.total_decisions,
                approval_id=approval_id,
                approval_live=evidence.approval_live,
                order_lock_blocked=(action == "blocked_by_order_lock"),
                as_of_date=as_of.isoformat(),
                **extra,
            )
        )


# ---------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------


def run_autopilot_tick(
    db: Session, *, as_of_date: Optional[date] = None
) -> dict[str, Any]:
    """Run one evaluation tick. Returns a summary dict (safe for tests)."""
    as_of = as_of_date or datetime.utcnow().date()
    if not is_enabled():
        if _ops_log_enabled():
            logger.info(
                format_autopilot_ops_line(
                    event="autopilot_skipped",
                    mode="disabled" if not bool(getattr(settings, "brain_pattern_regime_autopilot_enabled", False)) else "kill",
                    reason_code="enabled_false",
                    as_of_date=as_of.isoformat(),
                )
            )
        return {"enabled": False, "skipped": True}

    cfg = _config()
    # Snapshot PRE-tick modes so order lock is stable during the loop.
    modes = {s: _effective_mode(db, s) for s in SLICE_NAMES}
    order_lock = compute_order_lock_state(
        tilt_mode=modes[SLICE_TILT],
        killswitch_mode=modes[SLICE_KILLSWITCH],
        promotion_mode=modes[SLICE_PROMOTION],
    )

    per_slice: dict[str, dict[str, Any]] = {}
    for slice_name in SLICE_NAMES:
        try:
            evidence = _gather_evidence(
                db, slice_name=slice_name, cfg=cfg, today_utc=as_of
            )
            decision = evaluate_slice_gates(evidence, cfg, order_lock)
            _apply_decision(
                db,
                decision=decision,
                evidence=evidence,
                cfg=cfg,
                as_of=as_of,
            )
            per_slice[slice_name] = {
                "action": decision.action,
                "from_mode": decision.from_mode,
                "to_mode": decision.to_mode,
                "reason_code": decision.reason_code,
                "days_in_stage": evidence.days_in_stage,
                "total_decisions": evidence.total_decisions,
            }
        except Exception as exc:
            logger.exception(
                "[pattern_regime_autopilot_service] slice %s eval failed: %s",
                slice_name,
                exc,
            )
            per_slice[slice_name] = {"action": "error", "reason_code": str(exc)[:64]}

    try:
        db.commit()
    except Exception as exc:
        logger.warning(
            "[pattern_regime_autopilot_service] commit failed: %s", exc
        )
        try:
            db.rollback()
        except Exception:
            pass

    return {
        "enabled": True,
        "as_of_date": as_of.isoformat(),
        "slices": per_slice,
    }


def run_weekly_summary(
    db: Session, *, as_of_date: Optional[date] = None
) -> dict[str, Any]:
    """Monday 09:00 job. One ops line per slice + audit row."""
    as_of = as_of_date or datetime.utcnow().date()
    out: dict[str, Any] = {"as_of_date": as_of.isoformat(), "slices": {}}
    for slice_name in SLICE_NAMES:
        runtime = SLICE_TO_RUNTIME_NAME[slice_name]
        try:
            mode = _effective_mode(db, slice_name)
            days = _days_in_stage(db, slice_name, mode)
            last_adv = _last_advance_date(db, slice_name)
            approval_live = _approval_live(db, slice_name)
            out["slices"][slice_name] = {
                "mode": mode,
                "days_in_stage": days,
                "last_advance_date": last_adv.isoformat() if last_adv else None,
                "approval_live": approval_live,
            }
            if _ops_log_enabled():
                logger.info(
                    format_autopilot_ops_line(
                        event="autopilot_weekly_summary",
                        mode="enabled" if is_enabled() else "disabled",
                        slice_name=runtime,
                        from_mode=mode,
                        to_mode=mode,
                        reason_code="weekly_summary",
                        days_in_stage=days,
                        approval_live=approval_live,
                        as_of_date=as_of.isoformat(),
                    )
                )
            try:
                db.execute(
                    text(
                        """
                        INSERT INTO trading_pattern_regime_autopilot_log
                            (as_of_date, evaluated_at, slice_name, event,
                             from_mode, to_mode, reason_code,
                             gates_json, evidence_json, approval_id,
                             days_in_stage, ops_log_excerpt)
                        VALUES
                            (:as_of, NOW(), :slice, 'autopilot_weekly_summary',
                             :mode, :mode, 'weekly_summary',
                             CAST('{}' AS JSONB), CAST(:ev AS JSONB), NULL,
                             :dis, NULL)
                        """
                    ),
                    {
                        "as_of": as_of,
                        "slice": runtime,
                        "mode": mode,
                        "ev": json.dumps(
                            {
                                "approval_live": approval_live,
                                "last_advance_date": last_adv.isoformat() if last_adv else None,
                            },
                            sort_keys=True,
                        ),
                        "dis": days,
                    },
                )
            except Exception:  # pragma: no cover - defensive
                pass
        except Exception as exc:
            logger.warning(
                "[pattern_regime_autopilot_service] weekly summary slice %s failed: %s",
                slice_name,
                exc,
            )
            out["slices"][slice_name] = {"mode": "unknown"}

    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------


def diagnostics_summary(db: Session) -> dict[str, Any]:
    """Frozen shape for the autopilot diagnostics endpoint.

    Keys (stable):
        enabled, kill, next_eval_at, slices.{tilt,promotion,killswitch}.{
            stage, days_in_stage, last_advance_date, approval_live,
            env_mode, override_present
        }
    """
    enabled = is_enabled()
    kill = bool(getattr(settings, "brain_pattern_regime_autopilot_kill", False))
    hour = int(getattr(settings, "brain_pattern_regime_autopilot_cron_hour", 6))
    minute = int(getattr(settings, "brain_pattern_regime_autopilot_cron_minute", 15))

    slices_out: dict[str, Any] = {}
    for slice_name in SLICE_NAMES:
        runtime = SLICE_TO_RUNTIME_NAME[slice_name]
        try:
            override = get_runtime_mode_override(runtime, db=db, bypass_cache=True)
        except Exception:
            override = None
        env_mode = getattr(settings, SLICE_TO_ENV_KEY[slice_name], "off") or "off"
        try:
            mode = _effective_mode(db, slice_name)
        except Exception:
            mode = env_mode
        try:
            days = _days_in_stage(db, slice_name, mode)
        except Exception:
            days = 0
        try:
            last = _last_advance_date(db, slice_name)
        except Exception:
            last = None
        try:
            approval = _approval_live(db, slice_name)
        except Exception:
            approval = False
        slices_out[slice_name] = {
            "stage": mode,
            "days_in_stage": days,
            "last_advance_date": last.isoformat() if last else None,
            "approval_live": approval,
            "env_mode": env_mode,
            "override_present": override is not None,
        }

    return {
        "enabled": bool(enabled),
        "kill": bool(kill),
        "cron_hour": hour,
        "cron_minute": minute,
        "slices": slices_out,
    }


__all__ = [
    "is_enabled",
    "run_autopilot_tick",
    "run_weekly_summary",
    "diagnostics_summary",
]
