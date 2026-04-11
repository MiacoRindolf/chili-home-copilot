"""Operator momentum actions: paper admission + live arm flow (paper runner FSM is ``paper_runner``)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationSession
from ..execution_family_registry import is_momentum_automation_implemented, normalize_execution_family
from .persistence import append_trading_automation_event, create_trading_automation_session
from .risk_evaluator import evaluate_proposed_momentum_automation
from .risk_policy import build_session_risk_snapshot, resolve_effective_risk_policy
from .live_fsm import STATE_QUEUED_LIVE
from .operator_readiness import (
    blocked_reason_for_session,
    build_momentum_operator_readiness,
    next_action_required,
)
from .paper_fsm import (
    STATE_ARCHIVED,
    STATE_ARMED_PENDING_RUNNER,
    STATE_BAILOUT,
    STATE_CANCELLED,
    STATE_COOLDOWN,
    STATE_DRAFT,
    STATE_ENTERED,
    STATE_ENTRY_CANDIDATE,
    STATE_ERROR,
    STATE_EXITED,
    STATE_EXPIRED,
    STATE_FINISHED,
    STATE_IDLE,
    STATE_LIVE_ARM_PENDING,
    STATE_PENDING_ENTRY,
    STATE_QUEUED,
    STATE_SCALING_OUT,
    STATE_TRAILING,
    STATE_WATCHING,
)
from .session_lifecycle import (
    canonical_operator_state,
    is_armed_only_live,
    is_live_orders_active,
)

_log = logging.getLogger(__name__)

ARM_TOKEN_TTL_SEC = 900

_PROMOTABLE_PAPER_STATES = frozenset(
    {
        STATE_DRAFT,
        STATE_IDLE,
        STATE_QUEUED,
        STATE_WATCHING,
        STATE_ENTRY_CANDIDATE,
        STATE_PENDING_ENTRY,
        STATE_ENTERED,
        STATE_SCALING_OUT,
        STATE_TRAILING,
        STATE_BAILOUT,
        STATE_EXITED,
        STATE_COOLDOWN,
    }
)
_TERMINAL_OPERATOR_STATES = frozenset(
    {
        STATE_CANCELLED,
        STATE_EXPIRED,
        STATE_ERROR,
        STATE_ARCHIVED,
        STATE_FINISHED,
        "live_finished",
        "live_cancelled",
        "live_error",
    }
)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _paper_promotion_gate(paper: TradingAutomationSession) -> tuple[bool, str]:
    if paper.mode != "paper":
        return False, "not_paper_session"
    if paper.state == STATE_ARCHIVED:
        return False, "archived"
    if paper.state in (STATE_CANCELLED, STATE_EXPIRED, STATE_ERROR):
        return False, "paper_not_promotable"
    if paper.state == STATE_FINISHED:
        ref = paper.ended_at or paper.updated_at
        if ref is None:
            return False, "paper_completed_no_timestamp"
        age = (_utcnow() - ref).total_seconds()
        if age > float(settings.chili_momentum_risk_viability_max_age_seconds):
            return False, "paper_completed_stale"
        return True, "ok"
    if paper.state in _PROMOTABLE_PAPER_STATES:
        return True, "ok"
    return False, "paper_state_not_promotable"


def _confirm_live_truth_payload(sess: TradingAutomationSession, *, runner_on: bool) -> dict[str, Any]:
    rd = build_momentum_operator_readiness(execution_family=sess.execution_family, symbol=sess.symbol)
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    canon = canonical_operator_state(mode=sess.mode, state=sess.state, risk_snapshot_json=snap)
    blocked = blocked_reason_for_session(mode=sess.mode, readiness=rd, canonical_state=canon)
    nxt = next_action_required(
        mode=sess.mode,
        state=sess.state,
        canonical_state=canon,
        readiness=rd,
        blocked=blocked,
    )
    if runner_on and sess.state == STATE_QUEUED_LIVE:
        msg = "Live arm confirmed; session queued for guarded live runner."
    elif not runner_on and sess.state == STATE_ARMED_PENDING_RUNNER:
        msg = "Live arm confirmed; runner disabled — armed only until CHILI_MOMENTUM_LIVE_RUNNER_ENABLED."
    else:
        msg = "Live arm confirmed."
    return {
        "operator_readiness": rd,
        "canonical_operator_state": canon,
        "session_status_message": msg,
        "blocked_reason": blocked,
        "next_action_required": nxt,
        "armed_only": is_armed_only_live(mode=sess.mode, state=sess.state),
        "runner_ready": runner_on,
        "broker_ready": bool(rd.get("broker_ready_for_live")),
        "execution_ready": bool(rd.get("execution_ready")),
        "scheduler_ready": bool(rd.get("live_scheduler_would_run")),
        "is_live_orders_active": is_live_orders_active(mode=sess.mode, state=sess.state),
    }


def _viability_brief(row: MomentumSymbolViability) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "variant_id": row.variant_id,
        "viability_score": row.viability_score,
        "paper_eligible": row.paper_eligible,
        "live_eligible": row.live_eligible,
        "freshness_ts": row.freshness_ts.isoformat() if row.freshness_ts else None,
    }


def _readiness_subset(row: MomentumSymbolViability) -> dict[str, Any]:
    ex = row.execution_readiness_json if isinstance(row.execution_readiness_json, dict) else {}
    keys = ("spread_bps", "slippage_estimate_bps", "fee_to_target_ratio", "product_tradable")
    return {k: ex.get(k) for k in keys if k in ex}


def enqueue_symbol_refresh(
    db: Session,
    *,
    symbol: str,
    execution_family: str = "coinbase_spot",
) -> dict[str, Any]:
    """Publish neural momentum_context_refresh with focused tickers."""
    from ..brain_neural_mesh.publisher import publish_momentum_context_refresh

    ef = normalize_execution_family(execution_family)
    if not is_momentum_automation_implemented(ef):
        return {
            "ok": False,
            "reason": "execution_family_not_implemented",
            "execution_family": ef,
        }

    sym = symbol.strip().upper()
    meta: dict[str, Any] = {"tickers": [sym], "execution_family": ef}
    return publish_momentum_context_refresh(db, meta=meta)


def create_paper_draft_session(
    db: Session,
    *,
    user_id: Optional[int],
    symbol: str,
    variant_id: int,
    execution_family: str = "coinbase_spot",
) -> dict[str, Any]:
    """Phase-4/6 UX: draft paper session + frozen risk snapshot (runner not started)."""
    if user_id is None:
        return {"ok": False, "error": "user_required", "message": "Paired user required."}

    sym = symbol.strip().upper()
    existing = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSession.symbol == sym,
            TradingAutomationSession.variant_id == int(variant_id),
            TradingAutomationSession.mode == "paper",
            TradingAutomationSession.state != STATE_ARCHIVED,
        )
        .order_by(TradingAutomationSession.updated_at.desc())
        .all()
    )
    for row in existing:
        if row.state not in _TERMINAL_OPERATOR_STATES:
            return {
                "ok": True,
                "session_id": int(row.id),
                "state": row.state,
                "mode": row.mode,
                "deduped": True,
                "message": "Existing paper automation session reused for this symbol/variant.",
            }
    policy_full = resolve_effective_risk_policy()
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(user_id),
        symbol=sym,
        variant_id=int(variant_id),
        mode="paper",
        execution_family=execution_family,
    )
    if not ev.get("allowed", False):
        return {
            "ok": False,
            "error": "risk_blocked",
            "message": "Risk policy blocks paper draft for this symbol/variant.",
            "risk_evaluation": ev,
        }

    row = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == sym, MomentumSymbolViability.variant_id == int(variant_id))
        .one_or_none()
    )
    vb = _viability_brief(row) if row else None
    rs = _readiness_subset(row) if row else None

    snap = build_session_risk_snapshot(
        policy_full=policy_full,
        evaluation=ev,
        viability_brief=vb,
        readiness_subset=rs,
        extra=None,
    )

    runner_on = bool(settings.chili_momentum_paper_runner_enabled)
    initial_state = STATE_QUEUED if runner_on else STATE_DRAFT
    ef = normalize_execution_family(execution_family)
    sess = create_trading_automation_session(
        db,
        user_id=user_id,
        venue="coinbase",
        execution_family=ef,
        mode="paper",
        symbol=sym,
        variant_id=int(variant_id),
        state=initial_state,
        risk_snapshot_json=snap,
        correlation_id=str(uuid.uuid4()),
        source_node_id="momentum_operator_api",
    )
    if runner_on:
        append_trading_automation_event(
            db,
            sess.id,
            "paper_runner_queued",
            {"symbol": sym, "variant_id": variant_id, "note": "phase7_admission"},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_operator_api",
        )
    else:
        append_trading_automation_event(
            db,
            sess.id,
            "paper_draft_created",
            {"symbol": sym, "variant_id": variant_id, "note": "phase6_risk_snapshot"},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_operator_api",
        )
    return {
        "ok": True,
        "session_id": sess.id,
        "state": sess.state,
        "mode": sess.mode,
        "paper_runner_enabled": runner_on,
        "message": (
            "Paper session queued for simulated runner (tick batch or scheduler)."
            if runner_on
            else "Paper session recorded as draft; enable CHILI_MOMENTUM_PAPER_RUNNER_ENABLED for Phase 7 runner."
        ),
        "risk_evaluation": ev,
    }


def begin_live_arm(
    db: Session,
    *,
    user_id: Optional[int],
    symbol: str,
    variant_id: int,
    execution_family: str = "coinbase_spot",
) -> dict[str, Any]:
    """Validate live_eligible + risk policy; create pending arm session + token."""
    if user_id is None:
        return {"ok": False, "error": "user_required", "message": "Paired user required."}

    sym = symbol.strip().upper()
    existing = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSession.symbol == sym,
            TradingAutomationSession.variant_id == int(variant_id),
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state != STATE_ARCHIVED,
        )
        .order_by(TradingAutomationSession.updated_at.desc())
        .all()
    )
    for row in existing:
        if row.state not in _TERMINAL_OPERATOR_STATES:
            snap = row.risk_snapshot_json if isinstance(row.risk_snapshot_json, dict) else {}
            return {
                "ok": True,
                "session_id": int(row.id),
                "arm_token": snap.get("arm_token"),
                "state": row.state,
                "mode": row.mode,
                "deduped": True,
                "message": "Existing live automation session reused for this symbol/variant.",
            }
    row = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sym,
            MomentumSymbolViability.variant_id == int(variant_id),
        )
        .one_or_none()
    )
    if not row:
        return {"ok": False, "error": "viability_not_found", "message": "No viability row for symbol/variant."}
    if not row.live_eligible:
        return {"ok": False, "error": "not_live_eligible", "message": "Strategy is not live-eligible."}

    policy_full = resolve_effective_risk_policy()
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(user_id),
        symbol=sym,
        variant_id=int(variant_id),
        mode="live",
        execution_family=execution_family,
    )
    if not ev.get("allowed", False):
        return {
            "ok": False,
            "error": "risk_blocked",
            "message": "Risk policy blocks live arm for this symbol/variant.",
            "risk_evaluation": ev,
        }

    ttl = int(
        min(
            ARM_TOKEN_TTL_SEC,
            float(policy_full.get("auto_expire_pending_live_arm_seconds", ARM_TOKEN_TTL_SEC)),
        )
    )
    token = str(uuid.uuid4())
    expires = (_utcnow() + timedelta(seconds=ttl)).isoformat()

    snap = build_session_risk_snapshot(
        policy_full=policy_full,
        evaluation=ev,
        viability_brief=_viability_brief(row),
        readiness_subset=_readiness_subset(row),
        extra={
            "arm_token": token,
            "expires_at_utc": expires,
            "phase": 6,
        },
    )

    ef_live = normalize_execution_family(execution_family)
    sess = create_trading_automation_session(
        db,
        user_id=user_id,
        venue="coinbase",
        execution_family=ef_live,
        mode="live",
        symbol=sym,
        variant_id=int(variant_id),
        state=STATE_LIVE_ARM_PENDING,
        risk_snapshot_json=snap,
        correlation_id=str(uuid.uuid4()),
        source_node_id="momentum_operator_api",
    )
    append_trading_automation_event(
        db,
        sess.id,
        "live_arm_requested",
        {"symbol": sym, "variant_id": variant_id, "arm_token_prefix": token[:8]},
        correlation_id=sess.correlation_id,
        source_node_id="momentum_operator_api",
    )
    return {
        "ok": True,
        "arm_token": token,
        "session_id": sess.id,
        "expires_at_utc": expires,
        "risk_evaluation": ev,
        "confirmation": {
            "symbol": sym,
            "variant_id": variant_id,
            "viability_score": row.viability_score,
            "live_eligible": row.live_eligible,
            "freshness_ts": row.freshness_ts.isoformat() if row.freshness_ts else None,
            "warnings": list((row.explain_json or {}).get("warnings") or []),
            "risk_severity": ev.get("severity"),
            "disclaimer": (
                "This step does not place orders or start automation. "
                "Phase 6 records risk snapshot + operator intent only."
            ),
        },
    }


def confirm_live_arm(
    db: Session,
    *,
    user_id: Optional[int],
    arm_token: str,
    confirm: bool,
) -> dict[str, Any]:
    """Re-evaluate risk; freeze final snapshot; transition to armed_pending_runner."""
    if not confirm:
        return {"ok": False, "error": "confirm_required", "message": "confirm must be true."}
    tok = (arm_token or "").strip()
    if not tok:
        return {"ok": False, "error": "missing_token", "message": "arm_token required."}

    q = db.query(TradingAutomationSession).filter(TradingAutomationSession.state == STATE_LIVE_ARM_PENDING)
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == user_id)
    candidates = q.order_by(TradingAutomationSession.id.desc()).limit(50).all()

    sess: Optional[TradingAutomationSession] = None
    for c in candidates:
        snap = c.risk_snapshot_json if isinstance(c.risk_snapshot_json, dict) else {}
        if snap.get("arm_token") == tok:
            sess = c
            break

    if not sess:
        return {"ok": False, "error": "invalid_token", "message": "No matching pending arm session."}

    exp_raw = (sess.risk_snapshot_json or {}).get("expires_at_utc")
    exp = None
    if isinstance(exp_raw, str):
        try:
            exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
        except Exception:
            exp = None
    if exp is not None:
        now = _utcnow()
        if exp.tzinfo:
            exp = exp.replace(tzinfo=None)
        if now > exp:
            return {"ok": False, "error": "token_expired", "message": "Arm token expired; start arm flow again."}

    row = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == sess.variant_id,
        )
        .one_or_none()
    )
    if not row or not row.live_eligible:
        return {"ok": False, "error": "no_longer_eligible", "message": "Strategy is no longer live-eligible."}

    if user_id is None:
        return {"ok": False, "error": "user_required", "message": "Paired user required."}

    rd0 = build_momentum_operator_readiness(execution_family=sess.execution_family, symbol=sess.symbol)
    if not rd0.get("broker_ready_for_live"):
        return {
            "ok": False,
            "error": "broker_not_ready",
            "message": "Broker not ready for live (connect Coinbase Advanced).",
            "operator_readiness": rd0,
        }

    policy_full = resolve_effective_risk_policy()
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(user_id),
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        mode="live",
        execution_family=sess.execution_family,
        exclude_session_id=int(sess.id),
    )
    if not ev.get("allowed", False):
        return {
            "ok": False,
            "error": "risk_blocked",
            "message": "Risk policy no longer allows confirming live arm.",
            "risk_evaluation": ev,
        }

    final_snap = build_session_risk_snapshot(
        policy_full=policy_full,
        evaluation=ev,
        viability_brief=_viability_brief(row),
        readiness_subset=_readiness_subset(row),
        extra=dict(sess.risk_snapshot_json or {}),
    )
    final_snap["arm_confirmed_at_utc"] = _utcnow().isoformat()
    final_snap["arm_confirmed"] = True

    runner_on = bool(settings.chili_momentum_live_runner_enabled)
    sess.state = STATE_QUEUED_LIVE if runner_on else STATE_ARMED_PENDING_RUNNER
    sess.risk_snapshot_json = final_snap
    sess.updated_at = _utcnow()

    append_trading_automation_event(
        db,
        sess.id,
        "live_arm_confirmed",
        {
            "symbol": sess.symbol,
            "variant_id": sess.variant_id,
            "risk_severity": ev.get("severity"),
            "live_runner_enabled": runner_on,
            "initial_runner_state": sess.state,
        },
        correlation_id=sess.correlation_id,
        source_node_id="momentum_operator_api",
    )

    truth = _confirm_live_truth_payload(sess, runner_on=runner_on)
    legacy_msg = truth["session_status_message"]

    return {
        "ok": True,
        "session_id": sess.id,
        "state": sess.state,
        "mode": sess.mode,
        "live_runner_enabled": runner_on,
        "message": legacy_msg,
        "risk_evaluation": ev,
        **truth,
    }


def promote_paper_session_to_live_arm(
    db: Session,
    *,
    user_id: Optional[int],
    paper_session_id: int,
    execution_family: Optional[str] = None,
) -> dict[str, Any]:
    """Create live_arm_pending session from an eligible paper session (audit lineage on new row)."""
    if user_id is None:
        return {"ok": False, "error": "user_required", "message": "Paired user required."}

    paper = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.id == int(paper_session_id),
            TradingAutomationSession.user_id == int(user_id),
        )
        .one_or_none()
    )
    if not paper:
        return {"ok": False, "error": "not_found", "message": "Paper session not found."}

    ok_gate, gate_reason = _paper_promotion_gate(paper)
    if not ok_gate:
        return {
            "ok": False,
            "error": gate_reason,
            "message": (
                "Completed paper session is too old to promote; start a fresh paper run or use Arm Live from Trading."
                if gate_reason == "paper_completed_stale"
                else "This paper session cannot be promoted to live."
            ),
        }

    ef = normalize_execution_family(execution_family or paper.execution_family)
    if not is_momentum_automation_implemented(ef):
        return {
            "ok": False,
            "error": "execution_family_not_implemented",
            "execution_family": ef,
            "message": "Execution family not implemented for automation.",
        }

    row = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == paper.symbol,
            MomentumSymbolViability.variant_id == int(paper.variant_id),
        )
        .one_or_none()
    )
    if not row:
        return {"ok": False, "error": "viability_not_found", "message": "No viability row for symbol/variant."}
    if not row.live_eligible:
        return {"ok": False, "error": "not_live_eligible", "message": "Strategy is not live-eligible."}

    policy_full = resolve_effective_risk_policy()
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(user_id),
        symbol=paper.symbol,
        variant_id=int(paper.variant_id),
        mode="live",
        execution_family=ef,
    )
    if not ev.get("allowed", False):
        return {
            "ok": False,
            "error": "risk_blocked",
            "message": "Risk policy blocks live arm for this symbol/variant.",
            "risk_evaluation": ev,
        }

    ttl = int(
        min(
            ARM_TOKEN_TTL_SEC,
            float(policy_full.get("auto_expire_pending_live_arm_seconds", ARM_TOKEN_TTL_SEC)),
        )
    )
    token = str(uuid.uuid4())
    expires = (_utcnow() + timedelta(seconds=ttl)).isoformat()

    paper_snap = paper.risk_snapshot_json if isinstance(paper.risk_snapshot_json, dict) else {}
    snap = build_session_risk_snapshot(
        policy_full=policy_full,
        evaluation=ev,
        viability_brief=_viability_brief(row),
        readiness_subset=_readiness_subset(row),
        extra={
            "arm_token": token,
            "expires_at_utc": expires,
            "phase": 6,
            "promoted_from_paper_session_id": int(paper.id),
            "paper_session_state_at_promote": paper.state,
            "paper_risk_snapshot_excerpt": {
                "momentum_policy_caps": paper_snap.get("momentum_policy_caps"),
                "severity": paper_snap.get("severity"),
            },
        },
    )

    sess = create_trading_automation_session(
        db,
        user_id=user_id,
        venue="coinbase",
        execution_family=ef,
        mode="live",
        symbol=paper.symbol,
        variant_id=int(paper.variant_id),
        state=STATE_LIVE_ARM_PENDING,
        risk_snapshot_json=snap,
        correlation_id=str(uuid.uuid4()),
        source_node_id="momentum_operator_api",
        source_paper_session_id=int(paper.id),
    )
    append_trading_automation_event(
        db,
        sess.id,
        "live_arm_requested",
        {
            "symbol": paper.symbol,
            "variant_id": paper.variant_id,
            "arm_token_prefix": token[:8],
            "promoted_from_paper_session_id": int(paper.id),
        },
        correlation_id=sess.correlation_id,
        source_node_id="momentum_operator_api",
    )
    append_trading_automation_event(
        db,
        paper.id,
        "paper_promoted_to_live_candidate",
        {"live_session_id": int(sess.id), "execution_family": ef},
        correlation_id=paper.correlation_id,
        source_node_id="momentum_operator_api",
    )

    return {
        "ok": True,
        "arm_token": token,
        "session_id": sess.id,
        "source_paper_session_id": int(paper.id),
        "expires_at_utc": expires,
        "risk_evaluation": ev,
        "confirmation": {
            "symbol": paper.symbol,
            "variant_id": paper.variant_id,
            "viability_score": row.viability_score,
            "live_eligible": row.live_eligible,
            "freshness_ts": row.freshness_ts.isoformat() if row.freshness_ts else None,
            "warnings": list((row.explain_json or {}).get("warnings") or []),
            "risk_severity": ev.get("severity"),
            "disclaimer": (
                "Promotion created a new live pending-arm session linked to this paper session. "
                "Confirm to proceed; no orders until runner executes."
            ),
        },
    }
