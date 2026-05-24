"""Persist decision packets + candidates at runner entry boundary."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import TradingDecisionCandidate, TradingDecisionPacket, TradingDeploymentState
from .deployment_ladder_service import sync_initial_stage_from_viability
from .portfolio_allocator import allocate_momentum_session_entry, allocation_block_reason

DECISION_SNAPSHOT_VERSION = 1


def _shadow_packet_flag(alloc_result: dict[str, Any]) -> bool:
    if bool(alloc_result.get("shadow_override")):
        return True
    return bool(getattr(settings, "brain_expectancy_allocator_shadow_mode", False)) and not bool(
        alloc_result.get("capacity_blocked_flag")
    )


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _packet_stage_from_candidate(candidate: dict[str, Any]) -> str:
    stage = (
        candidate.get("lifecycle_stage")
        or candidate.get("promotion_status")
        or candidate.get("tier")
        or "shadow"
    )
    return str(stage).strip().lower()[:24] or "shadow"


def _candidate_net_edge(candidate: dict[str, Any]) -> float | None:
    direct = _as_float(candidate.get("expected_net_edge"))
    if direct is not None:
        return direct
    risk = candidate.get("net_edge_estimate")
    if isinstance(risk, dict):
        return _as_float(risk.get("expected_net_edge"))
    return None


def _candidate_risk_value(candidate: dict[str, Any], key: str) -> Any:
    val = candidate.get(key)
    if isinstance(val, dict):
        return val.get("score")
    return val


def _utc_iso(dt: datetime | None = None) -> str:
    base = dt or datetime.utcnow()
    if getattr(base, "tzinfo", None) is not None:
        base = base.replace(tzinfo=None)
    return base.isoformat() + "Z"


def _strip_decision_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _strip_decision_snapshot(v)
            for k, v in value.items()
            if str(k) != "decision_snapshot"
        }
    if isinstance(value, list):
        return [_strip_decision_snapshot(v) for v in value]
    if isinstance(value, tuple):
        return [_strip_decision_snapshot(v) for v in value]
    if isinstance(value, datetime):
        return _utc_iso(value)
    return value


def _stable_json(value: Any) -> str:
    return json.dumps(
        _strip_decision_snapshot(value),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _candidate_set_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    compact: list[dict[str, Any]] = []
    for row in rows:
        compact.append(
            {
                "rank": row.get("rank"),
                "ticker": row.get("ticker"),
                "scan_pattern_id": row.get("scan_pattern_id"),
                "expected_edge_gross": row.get("expected_edge_gross"),
                "expected_edge_net": row.get("expected_edge_net"),
                "expected_slippage_bps": row.get("expected_slippage_bps"),
                "expected_fill_probability": row.get("expected_fill_probability"),
                "size_cap_notional": row.get("size_cap_notional"),
                "was_selected": bool(row.get("was_selected")),
                "reject_reason_code": row.get("reject_reason_code"),
                "reject_reason_text": row.get("reject_reason_text"),
                "reject_detail_json": row.get("reject_detail_json"),
            }
        )
    fp = hashlib.sha256(_stable_json(compact).encode("utf-8")).hexdigest()
    return {
        "version": DECISION_SNAPSHOT_VERSION,
        "candidate_count": len(compact),
        "fingerprint_sha256": fp,
        "rows": compact,
    }


def _runner_feature_snapshot(*, session: Any, viability: Any, variant: Any) -> dict[str, Any]:
    freshness = getattr(viability, "freshness_ts", None)
    return {
        "version": DECISION_SNAPSHOT_VERSION,
        "session": {
            "id": getattr(session, "id", None),
            "symbol": getattr(session, "symbol", None),
            "mode": getattr(session, "mode", None),
            "state": getattr(session, "state", None),
            "execution_family": getattr(session, "execution_family", None),
            "correlation_id": getattr(session, "correlation_id", None),
        },
        "variant": {
            "id": getattr(variant, "id", None),
            "family": getattr(variant, "family", None),
            "variant_key": getattr(variant, "variant_key", None),
            "version": getattr(variant, "version", None),
            "execution_family": getattr(variant, "execution_family", None),
            "scan_pattern_id": getattr(variant, "scan_pattern_id", None),
            "params_json": getattr(variant, "params_json", None),
        },
        "viability": {
            "id": getattr(viability, "id", None),
            "symbol": getattr(viability, "symbol", None),
            "variant_id": getattr(viability, "variant_id", None),
            "viability_score": getattr(viability, "viability_score", None),
            "paper_eligible": getattr(viability, "paper_eligible", None),
            "live_eligible": getattr(viability, "live_eligible", None),
            "freshness_ts": _utc_iso(freshness) if isinstance(freshness, datetime) else freshness,
            "execution_readiness_json": getattr(viability, "execution_readiness_json", None),
            "regime_snapshot_json": getattr(viability, "regime_snapshot_json", None),
            "evidence_window_json": getattr(viability, "evidence_window_json", None),
        },
    }


def _packet_snapshot_payload(packet: TradingDecisionPacket) -> dict[str, Any]:
    return {
        "contract": "trading_decision_packet_snapshot",
        "version": DECISION_SNAPSHOT_VERSION,
        "packet_id": int(packet.id),
        "created_at": _utc_iso(packet.created_at),
        "user_id": packet.user_id,
        "automation_session_id": packet.automation_session_id,
        "scan_pattern_id": packet.scan_pattern_id,
        "chosen_ticker": packet.chosen_ticker,
        "decision_type": packet.decision_type,
        "execution_mode": packet.execution_mode,
        "deployment_stage": packet.deployment_stage,
        "source_surface": packet.source_surface,
        "regime_snapshot_json": packet.regime_snapshot_json,
        "allocator_input_json": packet.allocator_input_json,
        "allocator_output_json": packet.allocator_output_json,
        "portfolio_context_json": packet.portfolio_context_json,
        "expected_edge_gross": packet.expected_edge_gross,
        "expected_edge_net": packet.expected_edge_net,
        "expected_slippage_bps": packet.expected_slippage_bps,
        "expected_fill_probability": packet.expected_fill_probability,
        "expected_partial_fill_probability": packet.expected_partial_fill_probability,
        "expected_missed_fill_probability": packet.expected_missed_fill_probability,
        "size_notional": packet.size_notional,
        "abstain_reason_code": packet.abstain_reason_code,
        "abstain_reason_text": packet.abstain_reason_text,
        "candidate_count": packet.candidate_count,
        "capacity_blocked": bool(packet.capacity_blocked),
        "capacity_reason_json": packet.capacity_reason_json,
        "correlation_penalty": packet.correlation_penalty,
        "uncertainty_haircut": packet.uncertainty_haircut,
        "execution_penalty": packet.execution_penalty,
        "final_score": packet.final_score,
        "shadow_advisory_only": bool(packet.shadow_advisory_only),
    }


def seal_decision_packet_snapshot(
    packet: TradingDecisionPacket,
    *,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Attach a stable replay fingerprint to a decision packet's JSON payloads."""
    payload = _packet_snapshot_payload(packet)
    fp = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    seal = {
        "version": DECISION_SNAPSHOT_VERSION,
        "snapshot_id": f"tdp_{fp[:24]}",
        "fingerprint_sha256": fp,
        "as_of_utc": as_of_utc or _utc_iso(packet.created_at),
        "sealed_at_utc": _utc_iso(),
        "payload_contract": "trading_decision_packet_snapshot",
    }
    inp = dict(packet.allocator_input_json or {})
    inp["decision_snapshot"] = seal
    packet.allocator_input_json = inp
    ctx = dict(packet.research_vs_live_context_json or {})
    ctx["decision_snapshot"] = seal
    packet.research_vs_live_context_json = ctx
    return seal


def verify_decision_packet_snapshot(packet: TradingDecisionPacket) -> dict[str, Any]:
    snap = (packet.allocator_input_json or {}).get("decision_snapshot")
    if not isinstance(snap, dict) or not snap.get("fingerprint_sha256"):
        return {
            "ok": False,
            "reason": "missing_decision_snapshot",
            "snapshot_id": None,
        }
    current = hashlib.sha256(_stable_json(_packet_snapshot_payload(packet)).encode("utf-8")).hexdigest()
    expected = str(snap.get("fingerprint_sha256") or "")
    return {
        "ok": current == expected,
        "reason": None if current == expected else "fingerprint_mismatch",
        "snapshot_id": snap.get("snapshot_id"),
        "expected_fingerprint_sha256": expected,
        "current_fingerprint_sha256": current,
    }


def attach_shadow_signal_packets(
    db: Session,
    *,
    user_id: int | None,
    candidates: list[dict[str, Any]],
    source_surface: str,
    generated_at: datetime,
    data_as_of: str | None,
    ttl_seconds: int,
    commit: bool = True,
    require_board_setting: bool = True,
) -> dict[str, int]:
    """Attach/reuse shadow decision packets for non-executable signal surfaces.

    This is the generous learning lane: board/alert ideas become durable,
    replayable observations without making them live-trade approvals.
    """
    if not bool(getattr(settings, "brain_enable_decision_ledger", True)):
        return {"created": 0, "reused": 0}
    if require_board_setting and not bool(getattr(settings, "brain_opportunity_board_decision_packets_enabled", True)):
        return {"created": 0, "reused": 0}

    created = 0
    reused = 0
    now = generated_at.replace(tzinfo=None) if getattr(generated_at, "tzinfo", None) else generated_at
    cutoff = now - timedelta(seconds=max(30, int(ttl_seconds or 300)))
    surface = str(source_surface or "signal_surface")[:32]
    pool_count = len(candidates)

    for idx, candidate in enumerate(candidates):
        ticker = str(candidate.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        scan_pattern_id = candidate.get("scan_pattern_id")
        try:
            scan_pattern_id = int(scan_pattern_id) if scan_pattern_id is not None else None
        except (TypeError, ValueError):
            scan_pattern_id = None

        q = db.query(TradingDecisionPacket).filter(
            TradingDecisionPacket.source_surface == surface,
            TradingDecisionPacket.chosen_ticker == ticker,
            TradingDecisionPacket.created_at >= cutoff,
        )
        if user_id is None:
            q = q.filter(TradingDecisionPacket.user_id.is_(None))
        else:
            q = q.filter(TradingDecisionPacket.user_id == int(user_id))
        if scan_pattern_id is None:
            q = q.filter(TradingDecisionPacket.scan_pattern_id.is_(None))
        else:
            q = q.filter(TradingDecisionPacket.scan_pattern_id == scan_pattern_id)

        pkt = q.order_by(TradingDecisionPacket.created_at.desc()).first()
        if pkt is not None:
            candidate["decision_packet_id"] = int(pkt.id)
            snap = (pkt.allocator_input_json or {}).get("decision_snapshot")
            if isinstance(snap, dict) and snap.get("snapshot_id"):
                candidate["decision_snapshot_id"] = snap["snapshot_id"]
            reused += 1
            continue

        net_edge = _candidate_net_edge(candidate)
        data_quality_gate = (
            dict(candidate.get("data_quality_gate"))
            if isinstance(candidate.get("data_quality_gate"), dict)
            else {}
        )
        capital_lane = dict(candidate.get("capital_lane")) if isinstance(candidate.get("capital_lane"), dict) else {}
        data_quality_blocked = bool(data_quality_gate) and not bool(
            data_quality_gate.get("capital_lane_eligible", True)
        )
        reject_reason_code = "data_quality_blocked" if data_quality_blocked else "not_capital_lane"
        abstain_reason_code = (
            "data_quality_learning_observation" if data_quality_blocked else "learning_lane_observation"
        )
        abstain_reason_text = (
            "Opportunity Board signal recorded for learning, but board data quality blocks capital approval."
            if data_quality_blocked
            else "Opportunity Board signal recorded for outcome learning; no capital approval requested."
        )
        candidate_set = _candidate_set_snapshot(
            [
                {
                    "rank": idx,
                    "ticker": ticker,
                    "scan_pattern_id": scan_pattern_id,
                    "expected_edge_gross": _as_float(candidate.get("core_edge_score")),
                    "expected_edge_net": net_edge,
                    "expected_slippage_bps": (
                        (candidate.get("execution_risk") or {}).get("expected_slippage_bps")
                        if isinstance(candidate.get("execution_risk"), dict)
                        else None
                    ),
                    "expected_fill_probability": (
                        (candidate.get("execution_risk") or {}).get("expected_fill_probability")
                        if isinstance(candidate.get("execution_risk"), dict)
                        else None
                    ),
                    "was_selected": False,
                    "reject_reason_code": reject_reason_code,
                    "reject_reason_text": (
                        "Signal observation only; board data quality must pass before any capital lane."
                        if data_quality_blocked
                        else "Signal observation only; execution requires a runner allocation packet."
                    ),
                    "reject_detail_json": {
                        "source_surface": surface,
                        "candidate_pool_count": pool_count,
                    },
                }
            ]
        )
        risk_payload = {
            "tier": candidate.get("tier"),
            "sources": candidate.get("sources"),
            "source_strength": candidate.get("source_strength"),
            "entry": candidate.get("entry"),
            "stop": candidate.get("stop"),
            "target": candidate.get("target"),
            "price": candidate.get("price"),
            "extension_risk": candidate.get("extension_risk"),
            "execution_risk": candidate.get("execution_risk"),
            "structural_confirmation": candidate.get("structural_confirmation"),
            "liquidity_quality": candidate.get("liquidity_quality"),
            "net_edge_estimate": candidate.get("net_edge_estimate"),
            "data_quality_gate": data_quality_gate,
            "capital_lane": capital_lane,
            "alert_context": candidate.get("alert_context"),
            "data_as_of": data_as_of,
        }
        pkt = TradingDecisionPacket(
            user_id=int(user_id) if user_id is not None else None,
            automation_session_id=None,
            scan_pattern_id=scan_pattern_id,
            chosen_ticker=ticker,
            decision_type="manual_signal",
            execution_mode="shadow",
            deployment_stage=_packet_stage_from_candidate(candidate),
            regime_snapshot_json={},
            allocator_input_json={
                "source_surface": surface,
                "candidate_rank": idx,
                "candidate_pool_count": pool_count,
                "generated_at": generated_at.isoformat() + ("Z" if generated_at.tzinfo is None else ""),
                "candidate_set": candidate_set,
            },
            allocator_output_json=risk_payload,
            portfolio_context_json={
                "shadow_learning_lane": True,
                "capital_approval": "blocked_data_quality" if data_quality_blocked else "not_requested",
                "data_quality_gate": data_quality_gate,
                "capital_lane": capital_lane,
            },
            expected_edge_gross=_as_float(candidate.get("core_edge_score")),
            expected_edge_net=net_edge,
            expected_slippage_bps=_as_float(
                (candidate.get("execution_risk") or {}).get("expected_slippage_bps")
                if isinstance(candidate.get("execution_risk"), dict)
                else None
            ),
            expected_fill_probability=_as_float(
                (candidate.get("execution_risk") or {}).get("expected_fill_probability")
                if isinstance(candidate.get("execution_risk"), dict)
                else None
            ),
            size_notional=None,
            abstain_reason_code=abstain_reason_code,
            abstain_reason_text=abstain_reason_text,
            selected_candidate_rank=None,
            candidate_count=pool_count,
            capacity_blocked=False,
            capacity_reason_json={
                "execution_risk_score": _candidate_risk_value(candidate, "execution_risk"),
                "liquidity_quality_score": _candidate_risk_value(candidate, "liquidity_quality"),
            },
            correlation_penalty=None,
            uncertainty_haircut=None,
            execution_penalty=_as_float(_candidate_risk_value(candidate, "execution_risk")),
            final_score=net_edge if net_edge is not None else _as_float(candidate.get("core_edge_score")),
            source_surface=surface,
            outcome_status="observed",
            shadow_advisory_only=True,
        )
        db.add(pkt)
        db.flush()
        db.add(
            TradingDecisionCandidate(
                decision_packet_id=int(pkt.id),
                rank=idx,
                ticker=ticker,
                scan_pattern_id=scan_pattern_id,
                candidate_score_raw=_as_float(candidate.get("core_edge_score")),
                candidate_score_net=net_edge,
                expected_edge_gross=_as_float(candidate.get("core_edge_score")),
                expected_edge_net=net_edge,
                expected_slippage_bps=pkt.expected_slippage_bps,
                expected_fill_probability=pkt.expected_fill_probability,
                size_cap_notional=None,
                was_selected=False,
                reject_reason_code=reject_reason_code,
                reject_reason_text=(
                    "Signal observation only; board data quality must pass before any capital lane."
                    if data_quality_blocked
                    else "Signal observation only; execution requires a runner allocation packet."
                ),
                reject_detail_json=risk_payload,
            )
        )
        db.flush()
        seal = seal_decision_packet_snapshot(pkt, as_of_utc=data_as_of)
        db.flush()
        candidate["decision_packet_id"] = int(pkt.id)
        candidate["decision_snapshot_id"] = seal["snapshot_id"]
        created += 1

    if created and commit:
        db.commit()
    return {"created": created, "reused": reused}


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
        viability=viability,
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
    viability: Any,
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
    candidate_rows = list(alloc.get("candidates_payload") or [])
    candidate_set = _candidate_set_snapshot(candidate_rows)

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
            "peer_count": max(0, len(candidate_rows) - 1),
            "candidate_set": candidate_set,
            "runner_feature_snapshot": _runner_feature_snapshot(
                session=session,
                viability=viability,
                variant=variant,
            ),
        },
        allocator_output_json={
            "allocation_decision": alloc.get("allocation_decision"),
            "realism": realism,
            "capacity": cap,
            "shadow_override": alloc.get("shadow_override"),
            "net_edge_authoritative": alloc.get("net_edge_authoritative"),
            "net_edge_decision_id": alloc.get("net_edge_decision_id"),
            "pattern_capital_gate": alloc.get("pattern_capital_gate"),
        },
        portfolio_context_json={
            "deployment_size_mult": alloc.get("deployment_size_mult"),
            "allocation_decision_summary": (alloc.get("allocation_decision") or {}).get("action"),
            "portfolio_allocator_shadow_blocked": alloc.get("portfolio_allocator_shadow_blocked"),
            "portfolio_allocator_live_hard_block": alloc.get("portfolio_allocator_live_hard_block"),
            "portfolio_exposure": (alloc.get("allocation_decision") or {}).get("portfolio_exposure"),
            "pattern_capital_gate": alloc.get("pattern_capital_gate"),
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
        candidate_count=len(candidate_rows),
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

    for row in candidate_rows:
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
    seal_decision_packet_snapshot(pkt, as_of_utc=_utc_iso(pkt.created_at))
    db.flush()
    return pkt


def _proposal_confidence_probability(value: Any) -> float:
    raw = _as_float(value, 0.0) or 0.0
    if raw > 1.0:
        raw = raw / (10.0 if raw <= 10.0 else 100.0)
    return max(0.0, min(1.0, raw))


def record_strategy_proposal_decision(
    db: Session,
    *,
    proposal: Any,
    user_id: int | None,
    allocation: dict[str, Any] | None,
    broker: str | None,
    quantity: float | None,
    scan_pattern_id: int | None = None,
    decision_type: str = "trade",
    execution_mode: str = "live",
    deployment_stage: str = "proposal_approved",
    source_surface: str = "strategy_proposal",
    outcome_status: str = "pending",
    candidate_was_selected: bool = True,
    selected_candidate_rank: int | None = 0,
    shadow_advisory_only: bool = False,
    abstain_reason_code: str | None = None,
    abstain_reason_text: str | None = None,
) -> TradingDecisionPacket | None:
    """Persist a canonical packet for strategy proposal execution or terminal no-trade decisions."""
    if not bool(getattr(settings, "brain_enable_decision_ledger", True)):
        return None

    existing_id = None
    alloc_existing = getattr(proposal, "allocation_decision_json", None)
    if isinstance(alloc_existing, dict):
        existing_id = alloc_existing.get("decision_packet_id")
    try:
        existing_id = int(existing_id) if existing_id is not None else None
    except (TypeError, ValueError):
        existing_id = None
    if existing_id:
        row = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == existing_id).one_or_none()
        if row is not None:
            return row

    entry = _as_float(getattr(proposal, "entry_price", None), 0.0) or 0.0
    stop = _as_float(getattr(proposal, "stop_loss", None), 0.0) or 0.0
    target = _as_float(getattr(proposal, "take_profit", None), 0.0) or 0.0
    qty = _as_float(quantity, None)
    notional = (entry * qty) if entry > 0 and qty is not None and qty > 0 else None
    prob = _proposal_confidence_probability(getattr(proposal, "confidence", None))
    profit_pct = _as_float(getattr(proposal, "projected_profit_pct", None), 0.0) or 0.0
    loss_pct = abs(_as_float(getattr(proposal, "projected_loss_pct", None), 0.0) or 0.0)
    if profit_pct <= 0.0 and entry > 0 and target > 0:
        profit_pct = max(0.0, (target - entry) / entry * 100.0)
    if loss_pct <= 0.0 and entry > 0 and stop > 0:
        loss_pct = abs((entry - stop) / entry * 100.0)
    expected_edge_gross = profit_pct / 100.0 if profit_pct else None
    expected_edge_net = ((prob * profit_pct) - ((1.0 - prob) * loss_pct)) / 100.0
    alloc = dict(allocation or {})
    candidate_rows = [
        {
            "rank": 0,
            "ticker": str(getattr(proposal, "ticker", "") or "").upper(),
            "scan_pattern_id": scan_pattern_id or getattr(proposal, "scan_pattern_id", None),
            "expected_edge_gross": expected_edge_gross,
            "expected_edge_net": expected_edge_net,
            "expected_slippage_bps": None,
            "expected_fill_probability": None,
            "size_cap_notional": notional,
            "was_selected": bool(candidate_was_selected),
            "reject_reason_code": None,
            "reject_reason_text": None,
            "reject_detail_json": {
                "strategy_proposal_id": int(getattr(proposal, "id")),
                "allocation_blocked_reason": alloc.get("blocked_reason"),
                "allocation_allowed_if_enforced": alloc.get("allowed_if_enforced"),
            },
        }
    ]
    candidate_set = _candidate_set_snapshot(candidate_rows)
    indicator_json = getattr(proposal, "indicator_json", None)
    if not isinstance(indicator_json, dict):
        indicator_json = {}
    pkt = TradingDecisionPacket(
        user_id=user_id,
        automation_session_id=None,
        scan_pattern_id=scan_pattern_id or getattr(proposal, "scan_pattern_id", None),
        chosen_ticker=str(getattr(proposal, "ticker", "") or "").upper(),
        decision_type=str(decision_type or "trade")[:24],
        execution_mode=str(execution_mode or "live")[:16],
        deployment_stage=str(deployment_stage or "proposal_approved")[:24],
        regime_snapshot_json=dict(indicator_json.get("regime_snapshot") or {}),
        allocator_input_json={
            "strategy_proposal_id": int(getattr(proposal, "id")),
            "proposal": {
                "ticker": getattr(proposal, "ticker", None),
                "direction": getattr(proposal, "direction", None),
                "entry_price": entry,
                "stop_loss": stop,
                "take_profit": target,
                "quantity": qty,
                "timeframe": getattr(proposal, "timeframe", None),
                "confidence": getattr(proposal, "confidence", None),
                "risk_reward_ratio": getattr(proposal, "risk_reward_ratio", None),
                "brain_score": getattr(proposal, "brain_score", None),
                "ml_probability": getattr(proposal, "ml_probability", None),
                "scan_score": getattr(proposal, "scan_score", None),
            },
            "signals": getattr(proposal, "signals_json", None),
            "candidate_set": candidate_set,
        },
        allocator_output_json={
            "allocation_decision": alloc,
            "broker": broker,
            "proposal_status_at_packet": getattr(proposal, "status", None),
        },
        portfolio_context_json={
            "strategy_proposal_id": int(getattr(proposal, "id")),
            "allocation_decision_summary": alloc.get("action"),
            "portfolio_exposure": alloc.get("portfolio_exposure"),
            "blocked_reason": alloc.get("blocked_reason"),
            "broker": broker,
        },
        expected_edge_gross=expected_edge_gross,
        expected_edge_net=expected_edge_net,
        size_notional=notional,
        size_shares_or_qty=qty,
        abstain_reason_code=str(abstain_reason_code)[:64] if abstain_reason_code else None,
        abstain_reason_text=str(abstain_reason_text) if abstain_reason_text else None,
        selected_candidate_rank=selected_candidate_rank,
        candidate_count=1,
        capacity_blocked=not bool(alloc.get("allowed_if_enforced", True)),
        capacity_reason_json={"allocation": alloc},
        final_score=expected_edge_net,
        source_surface=str(source_surface or "strategy_proposal")[:32],
        outcome_status=str(outcome_status or "pending")[:24],
        shadow_advisory_only=bool(shadow_advisory_only),
    )
    db.add(pkt)
    db.flush()
    db.add(
        TradingDecisionCandidate(
            decision_packet_id=int(pkt.id),
            rank=0,
            ticker=str(getattr(proposal, "ticker", "") or "").upper(),
            scan_pattern_id=scan_pattern_id or getattr(proposal, "scan_pattern_id", None),
            candidate_score_raw=expected_edge_gross,
            candidate_score_net=expected_edge_net,
            expected_edge_gross=expected_edge_gross,
            expected_edge_net=expected_edge_net,
            size_cap_notional=notional,
            was_selected=bool(candidate_was_selected),
            reject_detail_json=dict(candidate_rows[0]["reject_detail_json"]),
        )
    )
    alloc_out = dict(alloc_existing or {})
    alloc_out.update(alloc)
    alloc_out["decision_packet_id"] = int(pkt.id)
    alloc_out["decision_snapshot_id"] = seal_decision_packet_snapshot(pkt, as_of_utc=_utc_iso(pkt.created_at))["snapshot_id"]
    proposal.allocation_decision_json = alloc_out
    db.flush()
    return pkt


def mark_packet_executed(db: Session, packet_id: int) -> None:
    row = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(packet_id)).one_or_none()
    if row and row.outcome_status == "pending":
        row.outcome_status = "executed"
        row.updated_at = datetime.utcnow()
        db.flush()


def mark_packet_terminal(
    db: Session,
    packet_id: int | None,
    *,
    outcome_status: str,
    reason_code: str | None = None,
    reason_text: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    if not packet_id:
        return
    row = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(packet_id)).one_or_none()
    if not row or row.outcome_status != "pending":
        return
    ctx = dict(row.research_vs_live_context_json or {})
    events = list(ctx.get("terminal_events") or [])
    events.append(
        {
            "recorded_at_utc": _utc_iso(),
            "outcome_status": outcome_status,
            "reason_code": reason_code,
            "reason_text": reason_text,
            **dict(payload or {}),
        }
    )
    ctx["terminal_events"] = events[-10:]
    row.research_vs_live_context_json = ctx
    row.outcome_status = str(outcome_status or "failed")[:24]
    row.updated_at = datetime.utcnow()
    db.flush()


def mark_packet_linked_trade(db: Session, packet_id: int, trade_id: int) -> None:
    row = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(packet_id)).one_or_none()
    if row:
        row.linked_trade_id = int(trade_id)
        row.updated_at = datetime.utcnow()
        db.flush()


def mark_linked_trade_packets_executed(
    db: Session,
    *,
    trade_id: int | None,
    source: str,
    broker_order_id: str | None = None,
) -> int:
    """Mark pending decision packets as executed when broker sync observes a trade fill."""
    if trade_id is None:
        return 0
    rows = (
        db.query(TradingDecisionPacket)
        .filter(
            TradingDecisionPacket.linked_trade_id == int(trade_id),
            TradingDecisionPacket.outcome_status == "pending",
        )
        .all()
    )
    for row in rows:
        ctx = dict(row.research_vs_live_context_json or {})
        fills = list(ctx.get("execution_fill_confirmations") or [])
        fills.append(
            {
                "recorded_at_utc": _utc_iso(),
                "source": source,
                "trade_id": int(trade_id),
                "broker_order_id": broker_order_id,
            }
        )
        ctx["execution_fill_confirmations"] = fills[-10:]
        row.research_vs_live_context_json = ctx
        row.outcome_status = "executed"
        row.updated_at = datetime.utcnow()
    if rows:
        db.flush()
    return len(rows)


def mark_linked_trade_packets_terminal(
    db: Session,
    *,
    trade_id: int | None,
    outcome_status: str,
    source: str,
    reason_code: str | None = None,
    reason_text: str | None = None,
    broker_order_id: str | None = None,
) -> int:
    """Mark pending linked packets terminal when broker sync observes a no-fill terminal order."""
    if trade_id is None:
        return 0
    rows = (
        db.query(TradingDecisionPacket)
        .filter(
            TradingDecisionPacket.linked_trade_id == int(trade_id),
            TradingDecisionPacket.outcome_status == "pending",
        )
        .all()
    )
    for row in rows:
        ctx = dict(row.research_vs_live_context_json or {})
        events = list(ctx.get("terminal_events") or [])
        events.append(
            {
                "recorded_at_utc": _utc_iso(),
                "source": source,
                "outcome_status": outcome_status,
                "reason_code": reason_code,
                "reason_text": reason_text,
                "trade_id": int(trade_id),
                "broker_order_id": broker_order_id,
            }
        )
        ctx["terminal_events"] = events[-10:]
        row.research_vs_live_context_json = ctx
        row.outcome_status = str(outcome_status or "failed")[:24]
        row.updated_at = datetime.utcnow()
    if rows:
        db.flush()
    return len(rows)


def record_packet_execution_intent(db: Session, packet_id: int | None, payload: dict[str, Any]) -> None:
    if not packet_id:
        return
    row = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(packet_id)).one_or_none()
    if not row:
        return
    ctx = dict(row.research_vs_live_context_json or {})
    intents = list(ctx.get("execution_intents") or [])
    intents.append({"recorded_at_utc": _utc_iso(), **dict(payload or {})})
    ctx["execution_intents"] = intents[-10:]
    row.research_vs_live_context_json = ctx
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


def finalize_signal_packet_directional_outcome(
    db: Session,
    *,
    packet_id: int | None,
    alert_id: int,
    ticker: str,
    scan_pattern_id: int | None,
    directional_correct: bool,
    max_favorable_pct: float | None,
    max_adverse_pct: float | None,
    entry_price: float | None,
    hold_window_hours: int | None,
    evaluated_at: datetime | None = None,
) -> None:
    """Attach a directional-outcome result to a shadow signal packet."""
    if not packet_id:
        return
    row = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(packet_id)).one_or_none()
    if not row or not bool(row.shadow_advisory_only):
        return
    ctx = dict(row.research_vs_live_context_json or {})
    outcomes = list(ctx.get("directional_outcomes") or [])
    outcomes.append(
        {
            "recorded_at_utc": _utc_iso(evaluated_at),
            "alert_id": int(alert_id),
            "ticker": ticker,
            "scan_pattern_id": scan_pattern_id,
            "directional_correct": bool(directional_correct),
            "max_favorable_pct": max_favorable_pct,
            "max_adverse_pct": max_adverse_pct,
            "entry_price": entry_price,
            "hold_window_hours": hold_window_hours,
        }
    )
    ctx["directional_outcomes"] = outcomes[-10:]
    row.research_vs_live_context_json = ctx
    row.outcome_status = "won" if bool(directional_correct) else "lost"
    row.updated_at = datetime.utcnow()
    db.flush()


def _packet_outcome_status(*, outcome_class: str | None, realized_pnl_usd: float | None, entry_occurred: bool) -> str:
    if not entry_occurred:
        return "closed_no_entry"
    if realized_pnl_usd is not None:
        if float(realized_pnl_usd) > 0:
            return "closed_win"
        if float(realized_pnl_usd) < 0:
            return "closed_loss"
        return "closed_flat"
    oc = str(outcome_class or "").lower()
    if any(token in oc for token in ("success", "small_win")):
        return "closed_win"
    if any(token in oc for token in ("stop", "loss", "bailout", "error")):
        return "closed_loss"
    return "closed_unknown"


def finalize_packet_from_automation_outcome(db: Session, outcome_row: Any) -> None:
    summary = dict(getattr(outcome_row, "extracted_summary_json", None) or {})
    packet_id = summary.get("entry_decision_packet_id")
    try:
        packet_id = int(packet_id) if packet_id is not None else None
    except (TypeError, ValueError):
        packet_id = None
    if not packet_id:
        return
    row = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(packet_id)).one_or_none()
    if not row:
        return
    realized = getattr(outcome_row, "realized_pnl_usd", None)
    entry_occurred = bool(summary.get("entry_occurred"))
    ctx = dict(row.research_vs_live_context_json or {})
    ctx["automation_outcome_id"] = getattr(outcome_row, "id", None)
    ctx["automation_outcome_class"] = getattr(outcome_row, "outcome_class", None)
    ctx["automation_terminal_state"] = getattr(outcome_row, "terminal_state", None)
    ctx["realized_pnl_usd"] = realized
    ctx["return_bps"] = getattr(outcome_row, "return_bps", None)
    ctx["contributes_to_evolution"] = bool(getattr(outcome_row, "contributes_to_evolution", False))
    row.research_vs_live_context_json = ctx
    row.outcome_status = _packet_outcome_status(
        outcome_class=getattr(outcome_row, "outcome_class", None),
        realized_pnl_usd=realized,
        entry_occurred=entry_occurred,
    )
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
