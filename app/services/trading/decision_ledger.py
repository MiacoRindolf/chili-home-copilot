"""Persist decision packets + candidates at runner entry boundary."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import TradingDecisionCandidate, TradingDecisionPacket, TradingDeploymentState
from .deployment_ladder_service import sync_initial_stage_from_viability
from .portfolio_allocator import allocate_momentum_session_entry, allocation_block_reason


def _shadow_packet_flag(alloc_result: dict[str, Any]) -> bool:
    if bool(alloc_result.get("shadow_override")):
        return True
    return bool(getattr(settings, "brain_expectancy_allocator_shadow_mode", True)) and not bool(
        alloc_result.get("capacity_blocked_flag")
    )


def run_momentum_entry_decision(
    db: Session,
    *,
    session: Any,
    viability: Any,
    variant: Any,
    user_id: int | None,
    max_notional_policy: float,
    quote_mid: float | None,
    spread_bps: float,
    execution_mode: str,
    regime_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Build allocation outcome and persist packet when ledger enabled."""
    if not bool(getattr(settings, "brain_enable_decision_ledger", True)):
        alloc = allocate_momentum_session_entry(
            db,
            session=session,
            viability=viability,
            variant=variant,
            user_id=user_id,
            max_notional_policy=max_notional_policy,
            quote_mid=quote_mid,
            spread_bps=spread_bps,
            execution_mode=execution_mode,
            regime_snapshot=regime_snapshot,
            deployment_stage="paper",
        )
        return {"proceed": alloc["proceed"], "allocation": alloc, "packet_id": None, "packet_row": None}

    dep = sync_initial_stage_from_viability(
        db,
        session_id=int(session.id),
        variant_id=int(session.variant_id),
        user_id=user_id,
        paper_eligible=bool(getattr(viability, "paper_eligible", True)),
        live_eligible=bool(getattr(viability, "live_eligible", False)),
        mode=execution_mode,
    )
    stage = dep.current_stage
    mult = -1.0

    alloc = allocate_momentum_session_entry(
        db,
        session=session,
        viability=viability,
        variant=variant,
        user_id=user_id,
        max_notional_policy=max_notional_policy,
        quote_mid=quote_mid,
        spread_bps=spread_bps,
        execution_mode=execution_mode,
        regime_snapshot=regime_snapshot,
        deployment_stage=stage,
        deployment_size_mult=mult,
    )

    if execution_mode == "live" and allocation_block_reason(alloc.get("allocation_decision")):
        br = allocation_block_reason(alloc.get("allocation_decision"))
        alloc["proceed"] = False
        alloc["abstain_reason_code"] = "portfolio_allocator_hard_block"
        alloc["abstain_reason_text"] = br
        cp = alloc.get("candidates_payload") or []
        if cp:
            cp[0]["was_selected"] = False
            cp[0]["reject_reason_code"] = "portfolio_allocator_hard_block"
            cp[0]["reject_reason_text"] = br

    packet = _persist_packet_and_candidates(
        db,
        session=session,
        variant=variant,
        user_id=user_id,
        execution_mode=execution_mode,
        dep_stage=stage,
        alloc=alloc,
        regime_snapshot=regime_snapshot,
        policy_notional_cap=max_notional_policy,
        spread_bps=spread_bps,
    )
    return {"proceed": alloc["proceed"], "allocation": alloc, "packet_id": int(packet.id), "packet_row": packet}


def _persist_packet_and_candidates(
    db: Session,
    *,
    session: Any,
    variant: Any,
    user_id: int | None,
    execution_mode: str,
    dep_stage: str,
    alloc: dict[str, Any],
    regime_snapshot: dict[str, Any],
    policy_notional_cap: float,
    spread_bps: float,
) -> TradingDecisionPacket:
    scan_pattern_id = None
    if variant and getattr(variant, "scan_pattern_id", None):
        scan_pattern_id = int(variant.scan_pattern_id)

    decision_type = "trade" if alloc.get("proceed") else "abstain"
    realism = alloc.get("realism") or {}
    cap = alloc.get("capacity") or {}

    pkt = TradingDecisionPacket(
        user_id=user_id,
        automation_session_id=int(session.id),
        scan_pattern_id=scan_pattern_id,
        chosen_ticker=(session.symbol or "").upper() if alloc.get("proceed") else None,
        decision_type=decision_type,
        execution_mode=execution_mode,
        deployment_stage=dep_stage,
        regime_snapshot_json=dict(regime_snapshot or {}),
        allocator_input_json={
            "policy_notional_cap": policy_notional_cap,
            "spread_bps": spread_bps,
            "peer_count": max(0, len(alloc.get("candidates_payload") or []) - 1),
        },
        allocator_output_json={
            "allocation_decision": alloc.get("allocation_decision"),
            "realism": realism,
            "capacity": cap,
            "shadow_override": alloc.get("shadow_override"),
        },
        portfolio_context_json={
            "deployment_size_mult": alloc.get("deployment_size_mult"),
            "allocation_decision_summary": (alloc.get("allocation_decision") or {}).get("action"),
        },
        expected_edge_gross=alloc.get("expected_edge_gross"),
        expected_edge_net=alloc.get("expected_edge_net"),
        expected_slippage_bps=realism.get("expected_slippage_bps"),
        expected_fill_probability=realism.get("expected_fill_probability"),
        expected_partial_fill_probability=realism.get("expected_partial_fill_probability"),
        expected_missed_fill_probability=realism.get("expected_missed_fill_probability"),
        size_notional=alloc.get("recommended_notional") if alloc.get("proceed") else None,
        abstain_reason_code=alloc.get("abstain_reason_code"),
        abstain_reason_text=alloc.get("abstain_reason_text"),
        selected_candidate_rank=0 if alloc.get("proceed") else None,
        candidate_count=len(alloc.get("candidates_payload") or []),
        capacity_blocked=bool(cap.get("capacity_hard_signals")),
        capacity_reason_json={"reasons": cap.get("capacity_reasons"), "eval": cap},
        correlation_penalty=alloc.get("correlation_penalty"),
        uncertainty_haircut=alloc.get("uncertainty_haircut"),
        execution_penalty=alloc.get("execution_penalty"),
        final_score=alloc.get("expected_edge_net"),
        source_surface="autopilot",
        outcome_status="pending" if alloc.get("proceed") else "skipped",
        shadow_advisory_only=_shadow_packet_flag(alloc),
    )
    db.add(pkt)
    db.flush()

    for row in alloc.get("candidates_payload") or []:
        cand = TradingDecisionCandidate(
            decision_packet_id=int(pkt.id),
            rank=int(row.get("rank") or 0),
            ticker=str(row.get("ticker") or ""),
            scan_pattern_id=row.get("scan_pattern_id"),
            candidate_score_raw=row.get("expected_edge_gross"),
            candidate_score_net=row.get("expected_edge_net"),
            expected_edge_gross=row.get("expected_edge_gross"),
            expected_edge_net=row.get("expected_edge_net"),
            expected_slippage_bps=row.get("expected_slippage_bps"),
            expected_fill_probability=row.get("expected_fill_probability"),
            size_cap_notional=row.get("size_cap_notional"),
            was_selected=bool(row.get("was_selected")),
            reject_reason_code=row.get("reject_reason_code"),
            reject_reason_text=row.get("reject_reason_text"),
            reject_detail_json=dict(row.get("reject_detail_json") or {}),
        )
        db.add(cand)
    db.flush()
    return pkt


def mark_packet_executed(db: Session, packet_id: int) -> None:
    row = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(packet_id)).one_or_none()
    if row and row.outcome_status == "pending":
        row.outcome_status = "executed"
        row.updated_at = datetime.utcnow()
        db.flush()


def mark_packet_linked_trade(db: Session, packet_id: int, trade_id: int) -> None:
    row = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(packet_id)).one_or_none()
    if row:
        row.linked_trade_id = int(trade_id)
        row.updated_at = datetime.utcnow()
        db.flush()


def finalize_packet_after_simulated_exit(
    db: Session,
    *,
    packet_id: int | None,
    realized_pnl_usd: float | None,
    slippage_bps: float | None,
) -> None:
    if not packet_id:
        return
    row = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(packet_id)).one_or_none()
    if not row:
        return
    ctx = dict(row.research_vs_live_context_json or {})
    ctx["realized_pnl_usd"] = realized_pnl_usd
    ctx["realized_slippage_bps"] = slippage_bps
    row.research_vs_live_context_json = ctx
    row.outcome_status = "linked"
    row.updated_at = datetime.utcnow()
    db.flush()


def recent_packets(
    db: Session,
    *,
    user_id: int,
    limit: int = 50,
    abstain_only: bool = False,
) -> list[TradingDecisionPacket]:
    q = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.user_id == int(user_id))
    if abstain_only:
        q = q.filter(TradingDecisionPacket.decision_type == "abstain")
    return q.order_by(TradingDecisionPacket.created_at.desc()).limit(limit).all()


def deployment_summary_for_user(db: Session, *, user_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(TradingDeploymentState)
        .filter(TradingDeploymentState.user_id == int(user_id))
        .order_by(TradingDeploymentState.updated_at.desc())
        .limit(80)
        .all()
    )
    return [
        {
            "scope_type": r.scope_type,
            "scope_key": r.scope_key,
            "current_stage": r.current_stage,
            "rolling_expectancy_net": r.rolling_expectancy_net,
            "rolling_slippage_bps": r.rolling_slippage_bps,
            "rolling_drawdown_pct": r.rolling_drawdown_pct,
            "last_reason_code": r.last_reason_code,
            "updated_at": r.updated_at.isoformat() + "Z" if r.updated_at else None,
        }
        for r in rows
    ]
