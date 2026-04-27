"""K Phase 3 S.3 — decision computation for the survival classifier.

Single entry point: ``compute_decision(db, scan_pattern_id, consumer,
input_context=None)``. Looks up the pattern's latest survival
prediction, applies consumer-specific policy from
``PATTERN_SURVIVAL_PHASE_3_DESIGN.md``, records a row in
``pattern_survival_decision_log`` (always, even on no_op), and returns
a dict the caller acts on.

Three consumers, all flag-gated:

  ``sizing``        — input_context: {"input_notional": float}
                      output: {"input_notional", "multiplier",
                               "output_notional"}
                      Multiplier = clamp(FLOOR + (1-FLOOR)*p, FLOOR, 1.0)
                      so even p=0 patterns keep FLOOR fraction. p=1.0
                      passes through unchanged.

  ``demote``        — input_context: optional {"current_lifecycle"}
                      output: {"proposed_lifecycle", "streak_days"}
                      live -> challenged when p < threshold (1 day).
                      challenged -> decayed when p < threshold for
                      ``demote_streak_required`` consecutive days.

  ``promote_gate``  — input_context: {"cpcv_passed": bool}
                      output: {"hold": bool, "reason"}
                      Holds CPCV-passed candidates whose first
                      prediction is below the promote threshold.

The function is the single shared touch-point so all three consumers
log to the same shape and the operator's audit query is uniform.

Flag-gating order (parent first, then sub-flag):

  1. parent ``chili_pattern_survival_decisions_enabled`` OFF
       -> decision='no_op', details={'skip_reason': 'parent_flag_off'}
  2. consumer's sub-flag (e.g. sizing_enabled) OFF
       -> decision='no_op', details={'skip_reason': 'consumer_flag_off'}
  3. no prediction row exists for this pattern (cold start)
       -> decision='no_op', details={'skip_reason': 'no_prediction'}
  4. otherwise apply policy.

The log row is written for every call with a real consumer, including
no_ops, so the operator can see what the gate considered (not just what
it acted on).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


_VALID_CONSUMERS = ("sizing", "demote", "promote_gate")


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────

def compute_decision(
    db: Session,
    *,
    scan_pattern_id: int,
    consumer: str,
    input_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Run the consumer's policy and record the decision.

    Returns a dict shaped like::

        {
          "consumer": str,
          "decision": "apply" | "no_op",
          "predicted_survival": float | None,
          "model_version": str | None,
          "threshold_used": float | None,
          "details": dict,    # consumer-specific
        }

    Always idempotent — calling twice with the same inputs produces two
    log rows but no other side effects.
    """
    if consumer not in _VALID_CONSUMERS:
        return {
            "consumer": consumer,
            "decision": "no_op",
            "predicted_survival": None,
            "model_version": None,
            "threshold_used": None,
            "details": {"skip_reason": "invalid_consumer"},
        }

    from ....config import settings as _settings

    # Gate 1: parent flag
    if not getattr(
        _settings, "chili_pattern_survival_decisions_enabled", False
    ):
        out = _no_op_result(consumer, "parent_flag_off")
        _record_decision(db, scan_pattern_id, out)
        return out

    # Gate 2: consumer sub-flag
    sub_flag_attr = {
        "sizing": "chili_pattern_survival_sizing_enabled",
        "demote": "chili_pattern_survival_demote_enabled",
        "promote_gate": "chili_pattern_survival_promote_gate_enabled",
    }[consumer]
    if not getattr(_settings, sub_flag_attr, False):
        out = _no_op_result(consumer, "consumer_flag_off")
        _record_decision(db, scan_pattern_id, out)
        return out

    # Gate 3: latest prediction available
    pred = _latest_prediction(db, scan_pattern_id)
    if pred is None:
        out = _no_op_result(consumer, "no_prediction")
        _record_decision(db, scan_pattern_id, out)
        return out

    # Apply consumer policy
    if consumer == "sizing":
        out = _decide_sizing(_settings, pred, input_context or {})
    elif consumer == "demote":
        out = _decide_demote(db, _settings, scan_pattern_id, pred,
                             input_context or {})
    else:
        out = _decide_promote_gate(_settings, pred, input_context or {})

    _record_decision(db, scan_pattern_id, out)
    return out


# ─────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────

def _no_op_result(consumer: str, skip_reason: str) -> dict[str, Any]:
    return {
        "consumer": consumer,
        "decision": "no_op",
        "predicted_survival": None,
        "model_version": None,
        "threshold_used": None,
        "details": {"skip_reason": skip_reason},
    }


def _latest_prediction(
    db: Session, scan_pattern_id: int
) -> Optional[dict[str, Any]]:
    """Latest prediction row for this pattern, or None if cold-start."""
    try:
        row = db.execute(
            text(
                """
                SELECT survival_probability, model_version, snapshot_date,
                       predicted_label, decision_threshold
                FROM pattern_survival_predictions
                WHERE scan_pattern_id = :p
                ORDER BY snapshot_date DESC, id DESC
                LIMIT 1
                """
            ),
            {"p": scan_pattern_id},
        ).fetchone()
    except Exception as e:
        logger.debug("[ps_decisions] latest_prediction query failed: %s", e)
        return None
    if row is None or row[0] is None:
        return None
    return {
        "survival_probability": float(row[0]),
        "model_version": row[1],
        "snapshot_date": row[2],
        "predicted_label": bool(row[3]) if row[3] is not None else None,
        "decision_threshold": float(row[4]) if row[4] is not None else None,
    }


def _decide_sizing(
    settings, pred: dict[str, Any], ctx: dict[str, Any],
) -> dict[str, Any]:
    p = pred["survival_probability"]
    floor = float(getattr(
        settings, "chili_pattern_survival_sizing_floor", 0.25
    ))
    floor = max(0.05, min(0.95, floor))   # defensive clamp
    raw = floor + (1.0 - floor) * p
    multiplier = max(floor, min(1.0, raw))

    input_notional = float(ctx.get("input_notional", 0.0) or 0.0)
    output_notional = round(input_notional * multiplier, 2)

    return {
        "consumer": "sizing",
        "decision": "apply",
        "predicted_survival": p,
        "model_version": pred["model_version"],
        "threshold_used": None,
        "details": {
            "input_notional": input_notional,
            "multiplier": round(multiplier, 4),
            "output_notional": output_notional,
            "sizing_floor": floor,
        },
    }


def _decide_demote(
    db: Session,
    settings,
    scan_pattern_id: int,
    pred: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    p = pred["survival_probability"]
    threshold = float(getattr(
        settings, "chili_pattern_survival_demote_threshold", 0.30
    ))
    streak_required = int(getattr(
        settings, "chili_pattern_survival_demote_streak_required", 3
    ))

    # Current lifecycle + streak
    cur = ctx.get("current_lifecycle")
    streak = 0
    if cur is None:
        try:
            row = db.execute(
                text(
                    "SELECT lifecycle_stage, "
                    "       COALESCE(survival_at_risk_streak_days, 0) "
                    "FROM scan_patterns WHERE id = :p"
                ),
                {"p": scan_pattern_id},
            ).fetchone()
            if row:
                cur = row[0]
                streak = int(row[1])
        except Exception as e:
            logger.debug("[ps_decisions] lifecycle lookup failed: %s", e)

    proposed = cur
    if p < threshold:
        # Tick the streak; subsequent state changes use the bumped streak.
        new_streak = streak + 1
        if cur == "live":
            # First-stage warning fires immediately on a single bad day.
            proposed = "challenged"
        elif cur == "challenged" and new_streak >= streak_required:
            proposed = "decayed"
    else:
        new_streak = 0  # reset

    decision = "apply" if proposed != cur else "no_op"
    return {
        "consumer": "demote",
        "decision": decision,
        "predicted_survival": p,
        "model_version": pred["model_version"],
        "threshold_used": threshold,
        "details": {
            "current_lifecycle": cur,
            "proposed_lifecycle": proposed,
            "streak_days_before": streak,
            "streak_days_after": new_streak,
            "streak_required": streak_required,
        },
    }


def _decide_promote_gate(
    settings, pred: dict[str, Any], ctx: dict[str, Any],
) -> dict[str, Any]:
    p = pred["survival_probability"]
    threshold = float(getattr(
        settings, "chili_pattern_survival_promote_gate_threshold", 0.40
    ))
    cpcv_passed = bool(ctx.get("cpcv_passed", True))

    # Phase 3.C is advisory: only fires when CPCV already passed. If
    # CPCV rejected, the pattern doesn't promote regardless of survival
    # — this gate is moot.
    if not cpcv_passed:
        return {
            "consumer": "promote_gate",
            "decision": "no_op",
            "predicted_survival": p,
            "model_version": pred["model_version"],
            "threshold_used": threshold,
            "details": {
                "skip_reason": "cpcv_rejected",
                "hold": False,
            },
        }

    hold = p < threshold
    return {
        "consumer": "promote_gate",
        "decision": "apply" if hold else "no_op",
        "predicted_survival": p,
        "model_version": pred["model_version"],
        "threshold_used": threshold,
        "details": {
            "cpcv_passed": True,
            "hold": hold,
            "reason": "low_survival" if hold else "passed_both_gates",
        },
    }


def _record_decision(
    db: Session,
    scan_pattern_id: int,
    result: dict[str, Any],
) -> None:
    """Insert one pattern_survival_decision_log row. Best-effort."""
    try:
        db.execute(
            text(
                """
                INSERT INTO pattern_survival_decision_log
                    (scan_pattern_id, consumer, predicted_survival,
                     threshold_used, decision, details, model_version)
                VALUES (:p, :c, :ps, :tu, :d, CAST(:dt AS jsonb), :mv)
                """
            ),
            {
                "p": scan_pattern_id,
                "c": result["consumer"],
                "ps": result.get("predicted_survival"),
                "tu": result.get("threshold_used"),
                "d": result["decision"],
                "dt": json.dumps(result.get("details") or {}),
                "mv": result.get("model_version"),
            },
        )
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "[ps_decisions] log row insert failed: pattern=%s consumer=%s: %s",
            scan_pattern_id, result.get("consumer"), e,
        )
