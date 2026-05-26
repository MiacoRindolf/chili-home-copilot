"""Phase J.2-lite recert queue dispatcher.

The drift monitor already writes ``trading_pattern_recert_log`` proposals.
This consumer turns proposed rows into backtest-queue priority boosts, then
marks the recert row ``dispatched``. It does not promote/demote patterns; it
only makes sure the validation machinery actually re-tests stale or drifting
patterns.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from .backtest_queue import boost_pattern
from .recert_queue_service import (
    mode_is_active,
    reconcile_dispatched_recerts_from_backtests,
)

logger = logging.getLogger(__name__)


def dispatch_due_recerts(
    db: Session,
    *,
    limit: int | None = None,
    priority: int | None = None,
) -> dict[str, Any]:
    mode = (getattr(settings, "brain_recert_queue_mode", "off") or "off").lower()
    if not mode_is_active(mode) or mode == "authoritative":
        return {"ok": True, "skipped": True, "reason": f"mode:{mode}"}
    row_limit = int(limit if limit is not None else getattr(
        settings, "brain_recert_queue_dispatch_limit", 5,
    ) or 5)
    boost_priority = int(priority if priority is not None else getattr(
        settings, "brain_recert_queue_backtest_priority", 250,
    ) or 250)
    reconciled = reconcile_dispatched_recerts_from_backtests(db, limit=row_limit)
    rows = db.execute(text("""
        SELECT id, recert_id, scan_pattern_id, pattern_name, payload_json
        FROM trading_pattern_recert_log
        WHERE status = 'proposed'
          AND mode = :mode
        ORDER BY observed_at ASC, id ASC
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    """), {"mode": mode, "limit": row_limit}).mappings().all()
    dispatched = 0
    failed = 0
    failures: list[dict[str, Any]] = []
    for row in rows:
        rid = int(row["id"])
        pid = int(row["scan_pattern_id"])
        try:
            if not boost_pattern(db, pid, priority=boost_priority):
                raise RuntimeError("scan_pattern_not_found_or_inactive")
            payload = dict(row.get("payload_json") or {})
            payload["dispatch"] = {
                "dispatched_at": datetime.utcnow().isoformat(),
                "backtest_priority": boost_priority,
                "dispatcher": "recert_queue_consumer",
            }
            db.execute(text("""
                UPDATE trading_pattern_recert_log
                SET status = 'dispatched',
                    payload_json = CAST(:payload AS JSONB)
                WHERE id = :id
            """), {"id": rid, "payload": json.dumps(payload, default=str)})
            db.commit()
            dispatched += 1
            logger.info(
                "[recert_queue_consumer] dispatched recert_id=%s pattern_id=%s priority=%s",
                row["recert_id"], pid, boost_priority,
            )
        except Exception as exc:
            db.rollback()
            failed += 1
            failures.append({"id": rid, "scan_pattern_id": pid, "error": str(exc)[:200]})
            try:
                db.execute(text("""
                    UPDATE trading_pattern_recert_log
                    SET status = 'dispatch_failed',
                        payload_json = COALESCE(payload_json, '{}'::jsonb)
                            || CAST(:payload AS JSONB)
                    WHERE id = :id
                """), {
                    "id": rid,
                    "payload": json.dumps({
                        "dispatch_error": str(exc)[:500],
                        "dispatch_failed_at": datetime.utcnow().isoformat(),
                    }),
                })
                db.commit()
            except Exception:
                db.rollback()
            logger.warning(
                "[recert_queue_consumer] dispatch failed recert_log_id=%s pattern_id=%s: %s",
                rid, pid, exc,
            )
    return {
        "ok": True,
        "mode": mode,
        "limit": row_limit,
        "priority": boost_priority,
        "seen": len(rows),
        "dispatched": dispatched,
        "failed": failed,
        "failures": failures,
        "reconciled": reconciled,
    }


__all__ = ["dispatch_due_recerts"]
