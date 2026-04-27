"""Promotion-evidence completeness audit (Codex stabilization plan #6).

Background
----------
Codex's audit on 2026-04-27 reported that of 31 patterns with
``promotion_status='promoted'``, 20 were missing OOS win rate, 18 had
zero OOS trades, 30 didn't have ``promotion_gate_passed=true``, and 29
had zero deflated Sharpe. That's near-total absence of the evidence the
promotion gate is supposed to enforce.

Most of those rows are leftover from the legacy ``promotion_status``
column that pre-dates the canonical ``lifecycle_stage`` FSM; the
canonical-column count of "promoted" is much smaller (~10) — but even
within that smaller set, several rows are missing CPCV / deflated Sharpe.

This module:
  * Computes a snapshot of promoted-pattern evidence completeness (per
    both columns, since they don't agree)
  * Logs the summary every run (so operators can track drift)
  * If ``chili_pattern_evidence_auto_demote`` is True, demotes
    evidence-incomplete patterns (lifecycle_stage -> ``challenged``) and
    records the actions

The auto-demote flag is OFF by default. The audit-only mode is safe to
run on a schedule (no side effects). Enabling auto-demote is an
operator-grade decision under the kill-switch / promotion runbook; do
not flip it on without reviewing the report first.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings

logger = logging.getLogger(__name__)
LOG_PREFIX = "[promotion_evidence_audit]"


def _criteria() -> dict[str, str]:
    """Operator-readable description of what 'evidence-complete' means."""
    return {
        "oos_win_rate": "must be IS NOT NULL",
        "oos_trade_count": "must be > 0",
        "promotion_gate_passed": "must be TRUE",
        "deflated_sharpe": "must be IS NOT NULL",
        "cpcv_median_sharpe": "must be IS NOT NULL",
    }


def audit_promoted_pattern_evidence(db: Session) -> dict[str, Any]:
    """Return a snapshot of promoted-pattern evidence completeness.

    Pure-read; never mutates. ``run_promotion_evidence_audit`` wraps this
    and adds the optional auto-demote action when the env flag is set.
    """
    audited_at = datetime.utcnow().isoformat()

    # Both column conventions (canonical + legacy) so we can show drift.
    promoted_lifecycle = db.execute(
        text(
            "SELECT count(*) FROM scan_patterns "
            "WHERE lifecycle_stage IN ('promoted', 'live')"
        )
    ).scalar() or 0
    promoted_legacy = db.execute(
        text(
            "SELECT count(*) FROM scan_patterns "
            "WHERE promotion_status = 'promoted'"
        )
    ).scalar() or 0

    # The audit set is the UNION of both — anything currently treated as
    # "promoted" by either convention should have evidence on file.
    rows = db.execute(
        text(
            """
            SELECT
                id,
                name,
                lifecycle_stage,
                promotion_status,
                oos_win_rate,
                oos_trade_count,
                promotion_gate_passed,
                deflated_sharpe,
                cpcv_median_sharpe
            FROM scan_patterns
            WHERE lifecycle_stage IN ('promoted', 'live')
               OR promotion_status = 'promoted'
            """
        )
    ).fetchall()

    by_missing: dict[str, int] = {
        "oos_win_rate_null": 0,
        "oos_trade_count_zero_or_null": 0,
        "promotion_gate_not_passed": 0,
        "deflated_sharpe_null": 0,
        "cpcv_median_sharpe_null": 0,
    }
    incomplete_ids: list[int] = []
    incomplete_details: list[dict[str, Any]] = []
    complete = 0

    for r in rows:
        missing: list[str] = []
        if r.oos_win_rate is None:
            by_missing["oos_win_rate_null"] += 1
            missing.append("oos_win_rate_null")
        if r.oos_trade_count is None or (r.oos_trade_count or 0) <= 0:
            by_missing["oos_trade_count_zero_or_null"] += 1
            missing.append("oos_trade_count_zero_or_null")
        if not (r.promotion_gate_passed is True):
            by_missing["promotion_gate_not_passed"] += 1
            missing.append("promotion_gate_not_passed")
        if r.deflated_sharpe is None:
            by_missing["deflated_sharpe_null"] += 1
            missing.append("deflated_sharpe_null")
        if r.cpcv_median_sharpe is None:
            by_missing["cpcv_median_sharpe_null"] += 1
            missing.append("cpcv_median_sharpe_null")
        if missing:
            incomplete_ids.append(int(r.id))
            incomplete_details.append({
                "id": int(r.id),
                "name": r.name,
                "lifecycle_stage": r.lifecycle_stage,
                "promotion_status": r.promotion_status,
                "missing": missing,
            })
        else:
            complete += 1

    return {
        "audited_at": audited_at,
        "criteria": _criteria(),
        "promoted_count_lifecycle": int(promoted_lifecycle),
        "promoted_count_legacy": int(promoted_legacy),
        "audit_universe_size": len(rows),
        "evidence_complete": complete,
        "evidence_incomplete": len(incomplete_ids),
        "by_missing_field": by_missing,
        "incomplete_ids": incomplete_ids,
        "incomplete_details": incomplete_details,
    }


def _auto_demote_enabled() -> bool:
    return bool(getattr(settings, "chili_pattern_evidence_auto_demote", False))


def _auto_demote_dry_run() -> bool:
    """If True, log the demotions that would happen without applying them."""
    return bool(getattr(settings, "chili_pattern_evidence_auto_demote_dry_run", False))


def run_promotion_evidence_audit(db: Session) -> dict[str, Any]:
    """Scheduler entrypoint. Runs the audit; optionally auto-demotes."""
    summary = audit_promoted_pattern_evidence(db)

    if summary["evidence_incomplete"] == 0:
        logger.info(
            "%s OK: %d promoted patterns, all evidence-complete",
            LOG_PREFIX,
            summary["audit_universe_size"],
        )
    else:
        logger.warning(
            "%s INCOMPLETE: %d/%d promoted patterns missing evidence; by_field=%s",
            LOG_PREFIX,
            summary["evidence_incomplete"],
            summary["audit_universe_size"],
            summary["by_missing_field"],
        )

    summary["auto_demote_enabled"] = _auto_demote_enabled()
    summary["auto_demote_dry_run"] = _auto_demote_dry_run()
    summary["auto_demote_actions"] = []

    if _auto_demote_enabled() and summary["incomplete_ids"]:
        ids = summary["incomplete_ids"]
        if _auto_demote_dry_run():
            logger.warning(
                "%s DRY-RUN: would demote %d patterns to 'challenged' (ids=%s)",
                LOG_PREFIX,
                len(ids),
                ids[:50],
            )
            summary["auto_demote_actions"] = [
                {"id": pid, "applied": False, "reason": "dry_run"} for pid in ids
            ]
        else:
            now = datetime.utcnow()
            db.execute(
                text(
                    """
                    UPDATE scan_patterns
                    SET lifecycle_stage = 'challenged',
                        lifecycle_changed_at = :now,
                        promotion_status = 'demoted_evidence_gap'
                    WHERE id = ANY(:ids)
                      AND (lifecycle_stage IN ('promoted', 'live')
                           OR promotion_status = 'promoted')
                    """
                ),
                {"now": now, "ids": ids},
            )
            db.commit()
            logger.error(
                "%s AUTO-DEMOTED %d patterns to 'challenged' for evidence gap (ids=%s)",
                LOG_PREFIX,
                len(ids),
                ids[:50],
            )
            summary["auto_demote_actions"] = [
                {"id": pid, "applied": True, "reason": "evidence_incomplete"} for pid in ids
            ]

    return summary
