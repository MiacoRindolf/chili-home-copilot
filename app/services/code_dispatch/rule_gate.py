"""Deterministic rule gate — runs BEFORE any LLM call.

Mirrors trading's passes_rule_gate(): no model in the loop, just hard checks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text

from .governance import is_code_agent_enabled


@dataclass
class RuleGateContext:
    task_id: int
    repo_id: Optional[int]
    estimated_diff_loc: int
    intended_files: list[str]
    prior_failure_count: int


@dataclass
class RuleGateResult:
    proceed: bool
    reason: str
    snapshot: dict


def passes_code_rule_gate(ctx: RuleGateContext) -> RuleGateResult:
    snap: dict = {}

    # 1. Code agent enabled?
    if not is_code_agent_enabled():
        return RuleGateResult(False, "code_agent_disabled", {"enabled": False})

    # 2. Velocity limit — at most N runs per minute (defaults to 4).
    velocity_cap = int(os.environ.get("CHILI_DISPATCH_VELOCITY_PER_MIN", "4"))
    recent = _runs_in_last_minute()
    snap["recent_runs_1min"] = recent
    if recent >= velocity_cap:
        return RuleGateResult(False, "velocity_cap", snap)

    # 3. Prior failures on this task (3 strikes).
    if ctx.prior_failure_count >= 3:
        return RuleGateResult(False, "task_failure_strikeout", {"prior_failures": ctx.prior_failure_count})

    # 4. Diff size sanity — guard against runaway tasks.
    max_loc = int(os.environ.get("CHILI_DISPATCH_MAX_DIFF_LOC", "1500"))
    if ctx.estimated_diff_loc > max_loc:
        return RuleGateResult(
            False,
            "diff_too_large",
            {"estimated_loc": ctx.estimated_diff_loc, "max": max_loc},
        )

    # 5. Budget cap.
    daily_cap = float(os.environ.get("CHILI_DISPATCH_DAILY_USD_CAP", "5.00"))
    spend_today = _spend_today_usd()
    snap["spend_today_usd"] = spend_today
    if spend_today >= daily_cap:
        return RuleGateResult(False, "daily_budget_cap", snap)

    return RuleGateResult(True, "ok", snap)


def _runs_in_last_minute() -> int:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            row = sess.execute(
                text(
                    "SELECT COUNT(*) FROM code_agent_runs "
                    "WHERE started_at > NOW() - INTERVAL '1 minute'"
                )
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            sess.close()
    except Exception:
        return 0


def _spend_today_usd() -> float:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            row = sess.execute(
                text(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_call_log "
                    "WHERE created_at > date_trunc('day', NOW())"
                )
            ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            sess.close()
    except Exception:
        return 0.0
