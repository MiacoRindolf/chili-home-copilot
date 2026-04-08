"""Read-model helpers for momentum automation outcomes (Phase 9)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, inspect as sa_inspect
from sqlalchemy.orm import Session

from ....models.trading import MomentumAutomationOutcome, MomentumStrategyVariant
from .evolution import (
    aggregate_recent_outcomes_for_symbol_variant,
    aggregate_recent_outcomes_for_variant,
    evolution_summary_for_operator,
    paper_vs_live_performance_slices,
)


def momentum_outcomes_table_present(db: Session) -> bool:
    try:
        return "momentum_automation_outcomes" in set(sa_inspect(db.bind).get_table_names())
    except Exception:
        return False


def list_recent_momentum_outcomes(
    db: Session,
    *,
    limit: int = 40,
    user_id: Optional[int] = None,
    variant_id: Optional[int] = None,
    symbol: Optional[str] = None,
    mode: Optional[str] = None,
    execution_family: Optional[str] = None,
) -> list[dict[str, Any]]:
    if not momentum_outcomes_table_present(db):
        return []
    lim = max(1, min(int(limit), 200))
    q = db.query(MomentumAutomationOutcome).order_by(MomentumAutomationOutcome.created_at.desc())
    if user_id is not None:
        q = q.filter(MomentumAutomationOutcome.user_id == int(user_id))
    if variant_id is not None:
        q = q.filter(MomentumAutomationOutcome.variant_id == int(variant_id))
    if symbol:
        q = q.filter(MomentumAutomationOutcome.symbol == symbol.strip().upper())
    if mode:
        q = q.filter(MomentumAutomationOutcome.mode == mode.lower().strip())
    if execution_family:
        q = q.filter(
            MomentumAutomationOutcome.execution_family == execution_family.strip().lower()
        )
    rows = q.limit(lim).all()
    return [_outcome_brief(r) for r in rows]


def _outcome_brief(r: MomentumAutomationOutcome) -> dict[str, Any]:
    return {
        "id": r.id,
        "session_id": r.session_id,
        "symbol": r.symbol,
        "variant_id": r.variant_id,
        "execution_family": r.execution_family,
        "mode": r.mode,
        "terminal_state": r.terminal_state,
        "outcome_class": r.outcome_class,
        "realized_pnl_usd": r.realized_pnl_usd,
        "return_bps": r.return_bps,
        "hold_seconds": r.hold_seconds,
        "exit_reason": r.exit_reason,
        "evidence_weight": r.evidence_weight,
        "terminal_at": r.terminal_at.isoformat() if r.terminal_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def get_variant_feedback_summary(db: Session, *, variant_id: int, days: int = 14) -> dict[str, Any]:
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == int(variant_id)).one_or_none()
    brief = _variant_brief(v) if v else None
    return {
        "variant": brief,
        "paper_vs_live": paper_vs_live_performance_slices(db, variant_id=int(variant_id), days=days),
        "evolution": evolution_summary_for_operator(db, variant_id=int(variant_id)),
    }


def get_symbol_variant_feedback_summary(
    db: Session, *, symbol: str, variant_id: int, days: int = 14
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    agg = aggregate_recent_outcomes_for_symbol_variant(db, symbol=sym, variant_id=int(variant_id), days=days)
    paper = aggregate_recent_outcomes_for_variant(db, variant_id=int(variant_id), days=days, mode="paper")
    live = aggregate_recent_outcomes_for_variant(db, variant_id=int(variant_id), days=days, mode="live")
    return {
        "symbol": sym,
        "variant_id": int(variant_id),
        "symbol_variant_window": agg,
        "variant_paper_slice": paper,
        "variant_live_slice": live,
    }


def _variant_brief(v: Optional[MomentumStrategyVariant]) -> Optional[dict[str, Any]]:
    if not v:
        return None
    return {
        "id": v.id,
        "family": v.family,
        "strategy_family": v.family,
        "variant_key": v.variant_key,
        "version": v.version,
        "label": v.label,
        "execution_family": v.execution_family,
    }


def aggregate_outcome_counts_by_execution_family(
    db: Session,
    *,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Read-model: durable outcome rows grouped by execution_family (Phase 11 seam)."""
    if not momentum_outcomes_table_present(db):
        return []
    since = datetime.utcnow() - timedelta(days=max(1, min(int(days), 120)))
    rows = (
        db.query(MomentumAutomationOutcome.execution_family, func.count(MomentumAutomationOutcome.id))
        .filter(MomentumAutomationOutcome.terminal_at >= since)
        .group_by(MomentumAutomationOutcome.execution_family)
        .all()
    )
    return [{"execution_family": str(ef), "n": int(c)} for ef, c in rows]


def get_session_feedback_row(db: Session, *, session_id: int) -> Optional[dict[str, Any]]:
    if not momentum_outcomes_table_present(db):
        return None
    r = (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(session_id))
        .one_or_none()
    )
    if not r:
        return None
    d = _outcome_brief(r)
    d["governance_context_json"] = r.governance_context_json
    d["extracted_summary_json"] = r.extracted_summary_json
    return d
