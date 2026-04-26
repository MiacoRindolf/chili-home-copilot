"""Write rows to code_agent_runs. Mirrors trading auto_trader._audit()."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


def open_run(
    *,
    task_id: Optional[int],
    repo_id: Optional[int],
    cycle_step: str,
    rule_snapshot: dict[str, Any] | None = None,
) -> Optional[int]:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            row = sess.execute(
                text(
                    "INSERT INTO code_agent_runs (task_id, repo_id, cycle_step, rule_snapshot) "
                    "VALUES (:t, :r, :s, CAST(:rs AS jsonb)) RETURNING id"
                ),
                {
                    "t": task_id,
                    "r": repo_id,
                    "s": cycle_step,
                    "rs": json.dumps(rule_snapshot or {}),
                },
            ).fetchone()
            sess.commit()
            return int(row[0]) if row else None
        finally:
            sess.close()
    except Exception:
        logger.debug("[code_dispatch.audit] open_run failed", exc_info=True)
        return None


def close_run(
    run_id: int,
    *,
    decision: str,
    llm_snapshot: dict[str, Any] | None = None,
    diff_summary: dict[str, Any] | None = None,
    validation_run_id: Optional[int] = None,
    branch_name: Optional[str] = None,
    commit_sha: Optional[str] = None,
    merged_to: Optional[str] = None,
    escalation_reason: Optional[str] = None,
    notify_user: bool = False,
) -> None:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            sess.execute(
                text(
                    "UPDATE code_agent_runs SET "
                    "  finished_at = NOW(), "
                    "  decision = :d, "
                    "  llm_snapshot = CAST(:ls AS jsonb), "
                    "  diff_summary = CAST(:ds AS jsonb), "
                    "  validation_run_id = :vr, "
                    "  branch_name = :bn, "
                    "  commit_sha = :cs, "
                    "  merged_to = :mt, "
                    "  escalation_reason = :er, "
                    "  notify_user = :nu "
                    "WHERE id = :id"
                ),
                {
                    "id": run_id,
                    "d": decision,
                    "ls": json.dumps(llm_snapshot or {}),
                    "ds": json.dumps(diff_summary or {}),
                    "vr": validation_run_id,
                    "bn": branch_name,
                    "cs": commit_sha,
                    "mt": merged_to,
                    "er": escalation_reason,
                    "nu": notify_user,
                },
            )
            sess.commit()
        finally:
            sess.close()
    except Exception:
        logger.debug("[code_dispatch.audit] close_run failed", exc_info=True)
