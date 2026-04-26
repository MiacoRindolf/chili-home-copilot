"""Code Dispatch governance — kill switch independent of trading kill switch.

Mirrors app.services.trading.governance but for the code agent. Persists in
code_kill_switch_state (singleton row) so state survives restarts.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

_kill_switch = False
_kill_switch_reason: Optional[str] = None
_kill_switch_lock = threading.Lock()


def activate_code_kill_switch(reason: str = "manual", actor: str = "operator") -> None:
    global _kill_switch, _kill_switch_reason
    with _kill_switch_lock:
        _kill_switch = True
        _kill_switch_reason = reason
    _persist(active=True, reason=reason, actor=actor)
    logger.critical("[code_dispatch.governance] CODE KILL SWITCH ACTIVATED: %s", reason)


def deactivate_code_kill_switch(actor: str = "operator") -> None:
    global _kill_switch, _kill_switch_reason
    with _kill_switch_lock:
        _kill_switch = False
        _kill_switch_reason = None
    _persist(active=False, reason=None, actor=actor)
    logger.info("[code_dispatch.governance] Code kill switch deactivated by %s", actor)


def is_code_agent_enabled() -> bool:
    """True iff the code agent is allowed to take actions this cycle.

    Soft pause via env (CHILI_DISPATCH_PAUSE=1) returns False as well.
    """
    if os.environ.get("CHILI_DISPATCH_PAUSE") == "1":
        return False
    if os.environ.get("CHILI_DISPATCH_ENABLED", "0") != "1":
        return False
    with _kill_switch_lock:
        return not _kill_switch


def get_code_kill_switch_status() -> dict[str, Any]:
    with _kill_switch_lock:
        return {"active": _kill_switch, "reason": _kill_switch_reason}


def restore_from_db() -> None:
    """Restore kill-switch state from code_kill_switch_state on startup."""
    global _kill_switch, _kill_switch_reason
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            row = sess.execute(
                text("SELECT active, reason FROM code_kill_switch_state WHERE id = 1")
            ).fetchone()
            if row:
                with _kill_switch_lock:
                    _kill_switch = bool(row[0])
                    _kill_switch_reason = row[1]
        finally:
            sess.close()
    except Exception:
        logger.debug("[code_dispatch.governance] restore_from_db skipped", exc_info=True)


def record_consecutive_failure(run_id: Optional[int]) -> int:
    """Increment consecutive failure count; auto-trip if threshold hit. Returns new count."""
    threshold = int(os.environ.get("CHILI_DISPATCH_AUTO_TRIP_THRESHOLD", "5"))
    new_count = 0
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            row = sess.execute(
                text(
                    "UPDATE code_kill_switch_state "
                    "SET consecutive_failures = consecutive_failures + 1, "
                    "    last_run_id = COALESCE(:rid, last_run_id) "
                    "WHERE id = 1 "
                    "RETURNING consecutive_failures"
                ),
                {"rid": run_id},
            ).fetchone()
            sess.commit()
            new_count = int(row[0]) if row else 0
        finally:
            sess.close()
    except Exception:
        logger.debug("[code_dispatch.governance] record_failure failed", exc_info=True)
        return 0

    if new_count >= threshold:
        activate_code_kill_switch(
            reason=f"auto: {new_count} consecutive failures",
            actor="auto-tripped",
        )
    return new_count


def reset_consecutive_failures() -> None:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            sess.execute(
                text(
                    "UPDATE code_kill_switch_state "
                    "SET consecutive_failures = 0 WHERE id = 1"
                )
            )
            sess.commit()
        finally:
            sess.close()
    except Exception:
        logger.debug("[code_dispatch.governance] reset_failures failed", exc_info=True)


def _persist(active: bool, reason: Optional[str], actor: str) -> None:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            sess.execute(
                text(
                    "UPDATE code_kill_switch_state SET "
                    "  active = :active, "
                    "  reason = :reason, "
                    "  activated_at = CASE WHEN :active THEN NOW() ELSE activated_at END, "
                    "  activated_by = CASE WHEN :active THEN :actor ELSE activated_by END "
                    "WHERE id = 1"
                ),
                {"active": active, "reason": reason, "actor": actor},
            )
            sess.commit()
        finally:
            sess.close()
    except Exception:
        logger.debug("[code_dispatch.governance] persist failed", exc_info=True)
