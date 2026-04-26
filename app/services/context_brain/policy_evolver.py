"""F.6 — Policy evolver.

Reads ``gateway_pattern`` rows produced by the F.4 distiller and proposes
adjustments to ``llm_purpose_policy``. Two severities:

  * ``low``  — auto-applied immediately. Today: ``max_chunks`` ±1 within
              [3, 10] when latency-vs-quality trade is clear.
  * ``high`` — written as a ``policy_change_proposal`` with status=pending.
              Operator approves via the API. Today: ``routing_strategy``
              flips and any change to high-stakes purposes.

Decision logic is intentionally conservative — we only propose a change
when:

  1. Pattern confidence >= 0.6 (≥30 samples)
  2. The "winning" alternative beats the current setting by a threshold
     that scales with the metric (10pp success rate, 0.10 quality signal,
     20% latency improvement)
  3. The opposing setting has at least min_samples too — never propose a
     flip based on a single side of the comparison

Every evolver pass is logged to ``gateway_learning_run`` so operators can
see when proposals were generated and how many auto-applied.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Purposes that should never auto-flip strategy or stake settings — even
# with high pattern confidence, the human should approve.
_FROZEN_PURPOSES = {
    "code_dispatch_create",
    "code_dispatch_edit",
    "code_dispatch_plan",
    "trading_analyze",
    "trading_smart_pick",
    "trading_pattern_adjust",
}

_MIN_CONFIDENCE = 0.6
_QUALITY_DELTA_THRESHOLD = 0.10
_SUCCESS_DELTA_THRESHOLD = 0.10
_LATENCY_DELTA_THRESHOLD = 0.20


def _start_run(db: Session) -> Optional[int]:
    try:
        row = db.execute(
            text(
                "INSERT INTO gateway_learning_run (phase) VALUES ('evolver') "
                "RETURNING id"
            )
        ).fetchone()
        db.commit()
        return int(row[0]) if row else None
    except Exception as e:  # pragma: no cover
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[evolver] start_run failed: %s", e)
        return None


def _end_run(
    db: Session,
    run_id: Optional[int],
    *,
    success: bool,
    proposals_created: int = 0,
    proposals_auto_applied: int = 0,
    error: Optional[str] = None,
) -> None:
    if run_id is None:
        return
    try:
        db.execute(
            text(
                "UPDATE gateway_learning_run SET ended_at=NOW(), success=:s, "
                "proposals_created=:c, proposals_auto_applied=:a, "
                "error_message=:e WHERE id=:rid"
            ),
            {
                "s": success,
                "c": proposals_created,
                "a": proposals_auto_applied,
                "e": error,
                "rid": run_id,
            },
        )
        db.commit()
    except Exception as e:  # pragma: no cover
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[evolver] end_run failed: %s", e)


def _has_pending_proposal(
    db: Session, *, purpose: str, field_name: str
) -> bool:
    try:
        row = db.execute(
            text(
                "SELECT 1 FROM policy_change_proposal "
                "WHERE purpose = :p AND field_name = :f AND status = 'pending' "
                "LIMIT 1"
            ),
            {"p": purpose, "f": field_name},
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _create_proposal(
    db: Session,
    *,
    purpose: str,
    field_name: str,
    current_value: str,
    proposed_value: str,
    justification: str,
    pattern_id: Optional[int],
    severity: str,
) -> Optional[int]:
    try:
        row = db.execute(
            text(
                """
                INSERT INTO policy_change_proposal
                    (purpose, field_name, current_value, proposed_value,
                     justification, pattern_id, severity, status)
                VALUES (:p, :f, :cv, :pv, :j, :pid, :sv, 'pending')
                RETURNING id
                """
            ),
            {
                "p": purpose,
                "f": field_name,
                "cv": current_value,
                "pv": proposed_value,
                "j": justification,
                "pid": pattern_id,
                "sv": severity,
            },
        ).fetchone()
        db.commit()
        return int(row[0]) if row else None
    except Exception as e:  # pragma: no cover
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[evolver] create_proposal failed: %s", e)
        return None


def _apply_proposal(db: Session, proposal_id: int) -> bool:
    """Apply a pending proposal to llm_purpose_policy and mark auto_applied."""
    try:
        prop = db.execute(
            text(
                "SELECT purpose, field_name, proposed_value FROM "
                "policy_change_proposal WHERE id = :pid AND status = 'pending'"
            ),
            {"pid": proposal_id},
        ).fetchone()
        if not prop:
            return False
        purpose, field_name, proposed_value = prop

        # Whitelist of fields safe to write — never let a malformed pattern
        # field name turn into arbitrary SQL.
        if field_name not in (
            "routing_strategy",
            "decompose",
            "cross_examine",
            "use_premium_synthesis",
            "high_stakes",
            "max_chunks",
            "chunk_timeout_sec",
            "primary_local_model",
            "secondary_local_model",
            "synthesizer_model",
            "enabled",
        ):
            return False

        # Cast for the boolean / int fields.
        if field_name in ("decompose", "cross_examine", "use_premium_synthesis",
                          "high_stakes", "enabled"):
            value: object = proposed_value.lower() in ("1", "true", "t", "yes")
        elif field_name in ("max_chunks", "chunk_timeout_sec"):
            value = int(proposed_value)
        else:
            value = proposed_value

        db.execute(
            text(
                f"UPDATE llm_purpose_policy SET {field_name} = :v, updated_at = NOW() "
                "WHERE purpose = :p"
            ),
            {"v": value, "p": purpose},
        )
        db.execute(
            text(
                "UPDATE policy_change_proposal "
                "SET status = 'auto_applied', decided_by = 'evolver', decided_at = NOW() "
                "WHERE id = :pid"
            ),
            {"pid": proposal_id},
        )
        db.commit()
        return True
    except Exception as e:  # pragma: no cover
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[evolver] apply_proposal %s failed: %s", proposal_id, e)
        return False


def _current_policy(db: Session, purpose: str) -> Optional[dict]:
    try:
        row = db.execute(
            text(
                "SELECT routing_strategy, decompose, cross_examine, "
                "use_premium_synthesis, high_stakes, max_chunks, "
                "chunk_timeout_sec, enabled FROM llm_purpose_policy "
                "WHERE purpose = :p"
            ),
            {"p": purpose},
        ).fetchone()
        if not row:
            return None
        return {
            "routing_strategy": row[0],
            "decompose": row[1],
            "cross_examine": row[2],
            "use_premium_synthesis": row[3],
            "high_stakes": row[4],
            "max_chunks": row[5],
            "chunk_timeout_sec": row[6],
            "enabled": row[7],
        }
    except Exception:
        return None


def _evolve_strategy(
    db: Session,
    *,
    purpose: str,
    current_strategy: str,
    is_frozen: bool,
) -> tuple[int, int]:
    """Look at strategy_vs_outcome patterns; propose flips if a clear winner."""
    proposals = 0
    auto = 0
    try:
        rows = db.execute(
            text(
                """
                SELECT id, pattern_key, sample_count, avg_quality,
                       success_rate, avg_latency_ms, confidence
                FROM gateway_pattern
                WHERE purpose = :p AND pattern_kind = 'strategy_vs_outcome'
                  AND confidence >= :mc
                """
            ),
            {"p": purpose, "mc": _MIN_CONFIDENCE},
        ).fetchall()

        if not rows or len(rows) < 2:
            return (0, 0)

        # Build map by strategy.
        by_strat = {r[1]: r for r in rows}
        if current_strategy not in by_strat:
            return (0, 0)

        cur = by_strat[current_strategy]
        cur_q = float(cur[3]) if cur[3] is not None else None
        cur_sr = float(cur[4]) if cur[4] is not None else None
        cur_lat = float(cur[5]) if cur[5] is not None else None

        # Find best alternative beating the current setting clearly.
        for strat, r in by_strat.items():
            if strat == current_strategy:
                continue
            alt_q = float(r[3]) if r[3] is not None else None
            alt_sr = float(r[4]) if r[4] is not None else None
            alt_lat = float(r[5]) if r[5] is not None else None

            beats_q = (alt_q is not None and cur_q is not None
                       and alt_q - cur_q >= _QUALITY_DELTA_THRESHOLD)
            beats_sr = (alt_sr is not None and cur_sr is not None
                        and alt_sr - cur_sr >= _SUCCESS_DELTA_THRESHOLD)

            if not (beats_q or beats_sr):
                continue

            if _has_pending_proposal(db, purpose=purpose,
                                     field_name="routing_strategy"):
                continue

            justification = (
                f"Pattern: current={current_strategy} "
                f"(q={cur_q}, sr={cur_sr}, n={cur[2]}); "
                f"proposed={strat} "
                f"(q={alt_q}, sr={alt_sr}, n={r[2]}, conf={r[6]}). "
                f"Improvement: Δq={(alt_q or 0) - (cur_q or 0):+.3f}, "
                f"Δsr={(alt_sr or 0) - (cur_sr or 0):+.3f}"
            )
            severity = "high" if is_frozen else "high"  # strategy flips always gated
            pid = _create_proposal(
                db,
                purpose=purpose,
                field_name="routing_strategy",
                current_value=current_strategy,
                proposed_value=strat,
                justification=justification,
                pattern_id=int(r[0]),
                severity=severity,
            )
            if pid:
                proposals += 1
            break  # one proposal per pass per purpose
    except Exception as e:  # pragma: no cover
        logger.warning("[evolver] strategy %s failed: %s", purpose, e)
    return (proposals, auto)


def _evolve_chunks(
    db: Session,
    *,
    purpose: str,
    current_max_chunks: int,
    is_frozen: bool,
) -> tuple[int, int]:
    """Auto-tune max_chunks ±1 based on bucket performance."""
    proposals = 0
    auto = 0
    try:
        rows = db.execute(
            text(
                """
                SELECT id, pattern_key, sample_count, avg_quality,
                       success_rate, avg_latency_ms, confidence
                FROM gateway_pattern
                WHERE purpose = :p AND pattern_kind = 'chunks_vs_outcome'
                  AND confidence >= :mc
                """
            ),
            {"p": purpose, "mc": _MIN_CONFIDENCE},
        ).fetchall()
        if not rows:
            return (0, 0)

        # Find best bucket by avg_quality (fall back to success_rate).
        def _score(r):
            q = float(r[3]) if r[3] is not None else None
            sr = float(r[4]) if r[4] is not None else None
            return (q if q is not None else (sr if sr is not None else 0.0))

        best = max(rows, key=_score)
        # Map best bucket back to a target max_chunks.
        bucket = best[1]
        target = {
            "0": 0,
            "1-2": 2,
            "3-4": 4,
            "5-6": 6,
            "7+": 8,
        }.get(bucket, current_max_chunks)
        if target == 0:
            return (0, 0)  # purpose isn't using chunks; no-op

        # Only nudge by ±1 per pass; clamp to [3, 10].
        new_val = current_max_chunks
        if target > current_max_chunks:
            new_val = min(10, current_max_chunks + 1)
        elif target < current_max_chunks:
            new_val = max(3, current_max_chunks - 1)
        if new_val == current_max_chunks:
            return (0, 0)

        if _has_pending_proposal(db, purpose=purpose, field_name="max_chunks"):
            return (0, 0)

        justification = (
            f"Best chunk bucket={bucket} (n={best[2]}, q={best[3]}, "
            f"sr={best[4]}, conf={best[6]}). Current max_chunks="
            f"{current_max_chunks}; nudging toward {new_val}."
        )
        severity = "high" if is_frozen else "low"
        pid = _create_proposal(
            db,
            purpose=purpose,
            field_name="max_chunks",
            current_value=str(current_max_chunks),
            proposed_value=str(new_val),
            justification=justification,
            pattern_id=int(best[0]),
            severity=severity,
        )
        if pid:
            proposals += 1
            if severity == "low":
                if _apply_proposal(db, pid):
                    auto += 1
    except Exception as e:  # pragma: no cover
        logger.warning("[evolver] chunks %s failed: %s", purpose, e)
    return (proposals, auto)


def evolve_policies(db: Session) -> dict:
    """One evolver pass over all purposes that have recent patterns."""
    run_id = _start_run(db)
    proposals = 0
    auto = 0
    try:
        # Discover purposes with patterns in the last day.
        purposes = [
            r[0]
            for r in db.execute(
                text(
                    "SELECT DISTINCT purpose FROM gateway_pattern "
                    "WHERE last_seen_at >= NOW() - INTERVAL '1 day'"
                )
            ).fetchall()
        ]

        for purpose in purposes:
            policy = _current_policy(db, purpose)
            if policy is None:
                continue
            is_frozen = purpose in _FROZEN_PURPOSES

            p, a = _evolve_strategy(
                db, purpose=purpose,
                current_strategy=policy["routing_strategy"],
                is_frozen=is_frozen,
            )
            proposals += p
            auto += a

            p, a = _evolve_chunks(
                db, purpose=purpose,
                current_max_chunks=int(policy["max_chunks"] or 8),
                is_frozen=is_frozen,
            )
            proposals += p
            auto += a

        _end_run(db, run_id, success=True,
                 proposals_created=proposals, proposals_auto_applied=auto)
        return {"ok": True, "proposals": proposals, "auto_applied": auto,
                "run_id": run_id}
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("[evolver] pass failed")
        _end_run(db, run_id, success=False, error=str(e)[:500])
        return {"ok": False, "proposals": proposals,
                "auto_applied": auto, "run_id": run_id, "error": str(e)}
