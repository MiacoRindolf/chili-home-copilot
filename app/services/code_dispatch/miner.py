"""Pick the next coding task to dispatch.

Sources, in priority order:
  1. plan_tasks where coding_readiness_state is in the DISPATCH_TASK_STATUSES allowlist
  2. plan_tasks in retryable blocked state (validation failures) with strike limits
  3. code_hotspots with combined_score above threshold and no recent dispatch
  4. code_dep_alerts unresolved (not wired in this pass)

Set CHILI_DISPATCH_TASK_STATUSES to a comma list of planner ``coding_readiness_state``
values (e.g. ``ready_for_future_impl,brief_ready``) to broaden what the miner picks
up. Default is ``ready_for_dispatch`` (synthetic/operator-queued) so real planner
tasks are opt-in.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import bindparam, text

logger = logging.getLogger(__name__)

DISPATCH_TASK_STATUSES = [
    s.strip()
    for s in os.environ.get("CHILI_DISPATCH_TASK_STATUSES", "ready_for_dispatch").split(",")
    if s.strip()
]


@dataclass
class Candidate:
    task_id: Optional[int]
    repo_id: Optional[int]
    source: str           # 'planner' | 'retry' | 'hotspot' | 'dep_alert'
    reason: str
    estimated_diff_loc: int
    intended_files: list[str]
    prior_failure_count: int
    force_tier: Optional[int]


def pick_next_task() -> Optional[Candidate]:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            if not DISPATCH_TASK_STATUSES:
                return None

            # 1. Planner queue (plan_tasks = planner_coding / PlanTask; readiness not generic status)
            q_planner = (
                text(
                    "SELECT pt.id, prof.code_repo_id, 100, CAST('[]' AS jsonb), NULL::integer "
                    "FROM plan_tasks pt "
                    "LEFT JOIN plan_task_coding_profile prof ON prof.task_id = pt.id "
                    "WHERE pt.coding_readiness_state IN :statuses "
                    "ORDER BY pt.sort_order ASC, pt.id ASC "
                    "LIMIT 1"
                )
                .bindparams(bindparam("statuses", expanding=True))
            )
            row = sess.execute(
                q_planner,
                {"statuses": DISPATCH_TASK_STATUSES},
            ).fetchone()
            if row:
                return Candidate(
                    task_id=int(row[0]),
                    repo_id=int(row[1]) if row[1] is not None else None,
                    source="planner",
                    reason=f"planner_readiness_in({','.join(DISPATCH_TASK_STATUSES)})",
                    estimated_diff_loc=int(row[2]),
                    intended_files=row[3] if isinstance(row[3], list) else [],
                    prior_failure_count=0,
                    force_tier=row[4],
                )

            # 2. Retry pool (validation blocked: coding_readiness_state terminal \"blocked\")
            row = sess.execute(
                text(
                    "SELECT t.id, prof.code_repo_id, 100, "
                    "       CAST('[]' AS jsonb), NULL::integer, "
                    "       COUNT(r.id) "
                    "FROM plan_tasks t "
                    "LEFT JOIN plan_task_coding_profile prof ON prof.task_id = t.id "
                    "JOIN code_agent_runs r ON r.task_id = t.id "
                    "WHERE t.coding_readiness_state = 'blocked' "
                    "GROUP BY t.id, prof.code_repo_id "
                    "HAVING COUNT(r.id) FILTER (WHERE r.decision IN ('escalate','rollback')) < 3 "
                    "ORDER BY MAX(r.started_at) ASC NULLS LAST LIMIT 1"
                )
            ).fetchone()
            if row:
                return Candidate(
                    task_id=int(row[0]),
                    repo_id=int(row[1]) if row[1] is not None else None,
                    source="retry",
                    reason="prior_failure_under_strike_limit",
                    estimated_diff_loc=int(row[2]),
                    intended_files=row[3] if isinstance(row[3], list) else [],
                    prior_failure_count=int(row[5]),
                    force_tier=row[4],
                )

            # 3. Hotspot proposal — only if not dispatched in last 24h.
            # (Hotspots become tasks via a separate planner, but as a fallback
            # we surface them here with task_id=None so the upstream scorer
            # decides whether to materialize a task.)
            row = sess.execute(
                text(
                    "SELECT h.repo_id, h.file_path, h.combined_score "
                    "FROM code_hotspots h "
                    "WHERE h.combined_score > 0.7 "
                    "  AND h.snapshot_date > NOW() - INTERVAL '7 days' "
                    "ORDER BY h.combined_score DESC LIMIT 1"
                )
            ).fetchone()
            if row:
                return Candidate(
                    task_id=None,
                    repo_id=int(row[0]),
                    source="hotspot",
                    reason=f"hotspot_score={float(row[2]):.2f} file={row[1]}",
                    estimated_diff_loc=200,
                    intended_files=[str(row[1])],
                    prior_failure_count=0,
                    force_tier=None,
                )

            return None
        finally:
            sess.close()
    except Exception:
        logger.debug("[miner] pick failed", exc_info=True)
        return None
