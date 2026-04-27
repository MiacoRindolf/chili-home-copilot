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


def demote_policy(
    *,
    p: float,
    current_lifecycle: Optional[str],
    current_streak: int,
    threshold: float,
    streak_required: int,
) -> dict[str, Any]:
    """Pure-function policy: given inputs, return proposed lifecycle +
    new streak.

    Extracted so the daily demote-pass scheduler hook can share the
    exact same logic as ``compute_decision(consumer='demote')`` without
    duplicating thresholds/state-machine rules.

    Returns::

        {
          "current_lifecycle": str | None,
          "proposed_lifecycle": str | None,
          "streak_days_before": int,
          "streak_days_after": int,
          "streak_required": int,
          "would_apply": bool,
        }

    The pass uses ``streak_days_after`` to update
    ``scan_patterns.survival_at_risk_streak_days`` and uses
    ``proposed_lifecycle`` (only when ``would_apply``) to update
    ``lifecycle_stage``.
    """
    proposed = current_lifecycle
    new_streak: int
    if p < threshold:
        new_streak = current_streak + 1
        if current_lifecycle == "live":
            proposed = "challenged"
        elif current_lifecycle == "challenged" and new_streak >= streak_required:
            proposed = "decayed"
    else:
        new_streak = 0
    return {
        "current_lifecycle": current_lifecycle,
        "proposed_lifecycle": proposed,
        "streak_days_before": int(current_streak),
        "streak_days_after": int(new_streak),
        "streak_required": int(streak_required),
        "would_apply": proposed != current_lifecycle,
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
    else:
        # Caller passed current_lifecycle but we still need streak from DB.
        try:
            row = db.execute(
                text(
                    "SELECT COALESCE(survival_at_risk_streak_days, 0) "
                    "FROM scan_patterns WHERE id = :p"
                ),
                {"p": scan_pattern_id},
            ).fetchone()
            if row:
                streak = int(row[0])
        except Exception:
            streak = 0

    pol = demote_policy(
        p=p,
        current_lifecycle=cur,
        current_streak=streak,
        threshold=threshold,
        streak_required=streak_required,
    )
    decision = "apply" if pol["would_apply"] else "no_op"
    return {
        "consumer": "demote",
        "decision": decision,
        "predicted_survival": p,
        "model_version": pred["model_version"],
        "threshold_used": threshold,
        "details": {
            "current_lifecycle": pol["current_lifecycle"],
            "proposed_lifecycle": pol["proposed_lifecycle"],
            "streak_days_before": pol["streak_days_before"],
            "streak_days_after": pol["streak_days_after"],
            "streak_required": pol["streak_required"],
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


# ─────────────────────────────────────────────────────────────────────
# S.5 — daily demote scheduler pass
# ─────────────────────────────────────────────────────────────────────

def run_pattern_survival_demote_pass(db: Session) -> dict[str, Any]:
    """Daily pass: walk lifecycle in (live, challenged), update streak,
    apply demote if the demote flag is on.

    Three-flag interaction:

      * ``chili_pattern_survival_classifier_enabled`` OFF (the parent
        feature-collection flag): pass returns {'skipped':
        'classifier_flag_off'} and does nothing. Without features
        there are no predictions, so the demote pass is moot.

      * ``chili_pattern_survival_decisions_enabled`` parent OFF:
        compute_decision logs no_op rows but the pass still runs the
        policy locally to update ``survival_at_risk_streak_days``.
        Streak continuity is preserved across flag flips.

      * ``chili_pattern_survival_demote_enabled`` OFF:
        same as above — streak is updated, log rows are written,
        but lifecycle changes are NOT applied. This is the shadow
        mode the operator runs for ~2 weeks before flipping demote
        on per the activation runbook.

      * Both flags ON + policy says apply:
        lifecycle_stage and lifecycle_changed_at are updated.
        survival_at_risk_streak_days is also updated.

    Returns counts for the scheduler log line.
    """
    from ....config import settings as _settings

    if not getattr(
        _settings, "chili_pattern_survival_classifier_enabled", False
    ):
        return {"skipped": "classifier_flag_off"}

    threshold = float(getattr(
        _settings, "chili_pattern_survival_demote_threshold", 0.30
    ))
    streak_required = int(getattr(
        _settings, "chili_pattern_survival_demote_streak_required", 3
    ))
    demote_enabled = bool(getattr(
        _settings, "chili_pattern_survival_demote_enabled", False
    ))

    try:
        rows = db.execute(text(
            """
            SELECT id, lifecycle_stage,
                   COALESCE(survival_at_risk_streak_days, 0)
            FROM scan_patterns
            WHERE lifecycle_stage IN ('live', 'challenged')
            ORDER BY id
            """
        )).fetchall()
    except Exception as e:
        logger.warning("[ps_demote_pass] enumerate failed: %s", e)
        return {"error": str(e)[:200]}

    inspected = 0
    streak_updated = 0
    demoted = 0
    skipped_no_pred = 0
    errors = 0

    for r in rows or []:
        pid = int(r[0])
        cur_lifecycle = r[1]
        cur_streak = int(r[2])
        inspected += 1

        # Always log via compute_decision so the audit row lands on
        # every pass (parent-flag-OFF case included).
        try:
            log_result = compute_decision(
                db,
                scan_pattern_id=pid,
                consumer="demote",
                input_context={"current_lifecycle": cur_lifecycle},
            )
        except Exception as e:
            logger.warning(
                "[ps_demote_pass] compute_decision failed pid=%s: %s", pid, e
            )
            errors += 1
            continue

        # Pull the latest prediction directly so we can run the policy
        # even when consumer flag is OFF (compute_decision short-circuits
        # before computing in that case).
        pred = _latest_prediction(db, pid)
        if pred is None:
            skipped_no_pred += 1
            continue

        pol = demote_policy(
            p=pred["survival_probability"],
            current_lifecycle=cur_lifecycle,
            current_streak=cur_streak,
            threshold=threshold,
            streak_required=streak_required,
        )

        # Always write streak update (continuity across flag flips).
        try:
            db.execute(text(
                "UPDATE scan_patterns "
                "SET survival_at_risk_streak_days = :s "
                "WHERE id = :p"
            ), {"s": pol["streak_days_after"], "p": pid})
            if pol["streak_days_after"] != cur_streak:
                streak_updated += 1
        except Exception as e:
            logger.warning(
                "[ps_demote_pass] streak update pid=%s failed: %s", pid, e
            )
            errors += 1

        # Apply lifecycle change ONLY if demote flag is on AND policy
        # would-apply.
        if demote_enabled and pol["would_apply"]:
            try:
                db.execute(text(
                    "UPDATE scan_patterns "
                    "SET lifecycle_stage = :ls, "
                    "    lifecycle_changed_at = NOW() "
                    "WHERE id = :p AND lifecycle_stage = :cur"
                ), {
                    "ls": pol["proposed_lifecycle"],
                    "p": pid,
                    "cur": cur_lifecycle,
                })
                demoted += 1
                logger.warning(
                    "[ps_demote_pass] demoted pattern %s: %s -> %s "
                    "(p=%.3f, streak=%d)",
                    pid, cur_lifecycle, pol["proposed_lifecycle"],
                    pred["survival_probability"], pol["streak_days_after"],
                )
            except Exception as e:
                logger.warning(
                    "[ps_demote_pass] lifecycle update pid=%s failed: %s",
                    pid, e,
                )
                errors += 1

    db.commit()

    summary = {
        "inspected": inspected,
        "streak_updated": streak_updated,
        "demoted": demoted,
        "skipped_no_prediction": skipped_no_pred,
        "errors": errors,
        "demote_flag_enabled": demote_enabled,
    }
    logger.info("[ps_demote_pass] complete: %s", summary)
    return summary
