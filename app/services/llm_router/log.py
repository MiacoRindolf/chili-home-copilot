"""Persistent prompt/completion ledger.

This is the most important table in the system: without it there is no
training set. Every LLM call routed through the project should land here.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


def log_call(
    *,
    trace_id: str,
    cycle_id: Optional[int],
    provider: str,
    model: str,
    tier: int,
    purpose: str,
    system_prompt: Optional[str],
    user_prompt: str,
    completion: Optional[str],
    tokens_in: Optional[int],
    tokens_out: Optional[int],
    latency_ms: Optional[int],
    cost_usd: Optional[float],
    success: bool,
    weak_response: bool = False,
    failure_kind: Optional[str] = None,
    distillable: bool = False,
) -> Optional[int]:
    """Insert a single (prompt, completion) row. Returns the new row id."""
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            row = sess.execute(
                text(
                    "INSERT INTO llm_call_log "
                    "(trace_id, cycle_id, provider, model, tier, purpose, "
                    " system_prompt, user_prompt, completion, "
                    " tokens_in, tokens_out, latency_ms, cost_usd, "
                    " success, weak_response, failure_kind, distillable) "
                    "VALUES (:trace_id, :cycle_id, :provider, :model, :tier, :purpose, "
                    " :sys, :usr, :comp, :tin, :tout, :lat, :cost, "
                    " :ok, :weak, :fail, :distill) "
                    "RETURNING id"
                ),
                {
                    "trace_id": trace_id,
                    "cycle_id": cycle_id,
                    "provider": provider,
                    "model": model,
                    "tier": tier,
                    "purpose": purpose,
                    "sys": system_prompt,
                    "usr": user_prompt,
                    "comp": completion,
                    "tin": tokens_in,
                    "tout": tokens_out,
                    "lat": latency_ms,
                    "cost": cost_usd,
                    "ok": success,
                    "weak": weak_response,
                    "fail": failure_kind,
                    "distill": distillable,
                },
            ).fetchone()
            sess.commit()
            return int(row[0]) if row else None
        finally:
            sess.close()
    except Exception:
        # Never let logging break a production call path.
        logger.debug("[llm_router.log] insert failed", exc_info=True)
        return None


def mark_validation_outcome(call_id: int, status: str) -> None:
    """Tag a prior call with the validation result so the distiller can filter on it.

    status: 'passed' | 'failed'
    """
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            sess.execute(
                text(
                    "UPDATE llm_call_log SET validation_status = :s, "
                    "  distillable = (validation_status IS NULL AND :s = 'passed') "
                    "WHERE id = :id"
                ),
                {"id": call_id, "s": status},
            )
            sess.commit()
        finally:
            sess.close()
    except Exception:
        logger.debug("[llm_router.log] mark_validation_outcome failed", exc_info=True)
