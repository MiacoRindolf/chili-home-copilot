"""Dry-run repair plan for the momentum closed-loop learning path."""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from ..decision_packet_coverage import (
    repair_automation_ledger_packet_links,
    repair_packet_snapshot_seals,
)
from ..economic_ledger import reconcile_missing_automation_outcome_parity
from .feedback_emit import (
    regrade_momentum_outcome_evolution_credit,
    reingest_regraded_momentum_outcomes,
)
from .feedback_query import evolution_credit_diagnostics


def _clamp_int(value: int | None, *, default: int, lo: int, hi: int) -> int:
    src = default if value is None else value
    return max(lo, min(int(src), hi))


def _int_payload(payload: dict[str, Any], key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _stage(
    *,
    stage: str,
    title: str,
    dry_run_endpoint: str,
    apply_endpoint: str | None,
    impact: str,
    action_count_key: str,
    fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    try:
        payload = fn()
    except Exception as exc:
        return {
            "stage": stage,
            "title": title,
            "ok": False,
            "actionable_count": 0,
            "action_count_key": action_count_key,
            "dry_run_endpoint": dry_run_endpoint,
            "apply_endpoint": apply_endpoint,
            "impact": impact,
            "error": str(exc),
            "payload": {},
        }

    return {
        "stage": stage,
        "title": title,
        "ok": bool(payload.get("ok", False)),
        "actionable_count": _int_payload(payload, action_count_key),
        "action_count_key": action_count_key,
        "dry_run_endpoint": dry_run_endpoint,
        "apply_endpoint": apply_endpoint,
        "impact": impact,
        "error": None,
        "payload": payload,
    }


def momentum_truth_repair_plan(
    db: Session,
    *,
    days: int = 30,
    lookback_hours: int | None = None,
    user_id: int | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Return a sequenced dry-run plan for restoring learning credit.

    The plan is read-only. It calls existing repair functions with
    ``dry_run=True`` and explains the order an operator should use when they
    want old momentum outcomes to become training-grade.
    """
    window_days = _clamp_int(days, default=30, lo=1, hi=365)
    hours = _clamp_int(
        lookback_hours,
        default=window_days * 24,
        lo=1,
        hi=8760,
    )
    limit_i = _clamp_int(limit, default=500, lo=1, hi=5000)
    user = int(user_id) if user_id is not None else None

    credit = evolution_credit_diagnostics(
        db,
        days=window_days,
        user_id=user,
        limit=min(limit_i, 10_000),
    )

    stages = [
        _stage(
            stage="packet_snapshot_seals",
            title="Seal replayable decision snapshots",
            dry_run_endpoint="POST /api/trading/brain/decision-packet-coverage/repair?target=packet_snapshots&apply=false",
            apply_endpoint="POST /api/trading/brain/decision-packet-coverage/repair?target=packet_snapshots&apply=true",
            impact="No trade-count impact; repairs decision packet replay metadata only.",
            action_count_key="candidate_count",
            fn=lambda: repair_packet_snapshot_seals(
                db,
                lookback_hours=hours,
                user_id=user,
                limit=limit_i,
                dry_run=True,
            ),
        ),
        _stage(
            stage="automation_ledger_packet_links",
            title="Backfill packet ids onto automation ledger fills",
            dry_run_endpoint="POST /api/trading/brain/decision-packet-coverage/repair?target=automation_ledger&apply=false",
            apply_endpoint="POST /api/trading/brain/decision-packet-coverage/repair?target=automation_ledger&apply=true",
            impact="No trade-count impact; repairs ledger fill provenance where a single packet is already known.",
            action_count_key="candidate_count",
            fn=lambda: repair_automation_ledger_packet_links(
                db,
                lookback_hours=hours,
                user_id=user,
                limit=limit_i,
                dry_run=True,
            ),
        ),
        _stage(
            stage="automation_ledger_parity",
            title="Reconcile automation outcome P&L to ledger fills",
            dry_run_endpoint="POST /api/trading/brain/ledger/automation-parity/reconcile?apply=false",
            apply_endpoint="POST /api/trading/brain/ledger/automation-parity/reconcile?apply=true",
            impact="No trade-count impact; apply mode writes parity logs only.",
            action_count_key="candidate_count",
            fn=lambda: reconcile_missing_automation_outcome_parity(
                db,
                days=window_days,
                user_id=user,
                limit=limit_i,
                dry_run=True,
            ),
        ),
        _stage(
            stage="evolution_credit_regrade",
            title="Recompute outcome evolution-credit eligibility",
            dry_run_endpoint="POST /api/trading/brain/momentum/evolution-credit/regrade?apply=false",
            apply_endpoint="POST /api/trading/brain/momentum/evolution-credit/regrade?apply=true",
            impact="No trade-count impact; apply mode changes outcome credit flags, not neural weights.",
            action_count_key="upgraded_to_training_grade",
            fn=lambda: regrade_momentum_outcome_evolution_credit(
                db,
                days=window_days,
                user_id=user,
                limit=min(limit_i, 10_000),
                dry_run=True,
            ),
        ),
        _stage(
            stage="evolution_reingest",
            title="Apply one-time neural reingest for upgraded outcomes",
            dry_run_endpoint="POST /api/trading/brain/momentum/evolution-credit/reingest?apply=false",
            apply_endpoint="POST /api/trading/brain/momentum/evolution-credit/reingest?apply=true",
            impact="No trade-count impact; apply mode updates neural learning state once per repaired outcome.",
            action_count_key="candidate_count",
            fn=lambda: reingest_regraded_momentum_outcomes(
                db,
                days=window_days,
                user_id=user,
                limit=min(limit_i, 1000),
                dry_run=True,
            ),
        ),
    ]

    stage_counts = {str(s["stage"]): int(s["actionable_count"]) for s in stages}
    prerequisite_count = (
        stage_counts.get("packet_snapshot_seals", 0)
        + stage_counts.get("automation_ledger_packet_links", 0)
        + stage_counts.get("automation_ledger_parity", 0)
    )
    regrade_ready = stage_counts.get("evolution_credit_regrade", 0)
    reingest_ready = stage_counts.get("evolution_reingest", 0)

    return {
        "ok": all(bool(s.get("ok")) for s in stages),
        "mode": "dry_run_plan",
        "window_days": window_days,
        "lookback_hours": hours,
        "limit": limit_i,
        "user_id": user,
        "policy_effect": "read_only_no_execution_change",
        "trade_count_impact": "none",
        "credit": {
            "total": credit.get("total", 0),
            "credited": credit.get("credited", 0),
            "blocked": credit.get("blocked", 0),
            "credit_rate": credit.get("credit_rate"),
            "reingest_required": credit.get("reingest_required", 0),
            "recommended_repairs": credit.get("recommended_repairs", []),
        },
        "summary": {
            "prerequisite_repair_candidates": prerequisite_count,
            "training_grade_upgrades_ready_now": regrade_ready,
            "neural_reingest_ready_now": reingest_ready,
            "actionable_stage_count": sum(1 for s in stages if int(s["actionable_count"]) > 0),
        },
        "sequence": stages,
        "operator_note": (
            "Run apply endpoints in sequence only after reviewing each dry-run payload; "
            "none of these stages changes entry policy or reduces trade frequency."
        ),
    }


__all__ = ["momentum_truth_repair_plan"]
