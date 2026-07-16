"""Operator momentum actions: paper admission + live arm flow (paper runner FSM is ``paper_runner``)."""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationSession
from ..execution_family_registry import (
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
    is_momentum_automation_implemented,
    normalize_execution_family,
    venue_for_execution_family,
)
from ..venue.account_identity import (
    NON_ALPACA_ACCOUNT_IDENTITY_KEY,
    read_current_non_alpaca_account_identity,
    verify_frozen_non_alpaca_account_identity,
)
from .persistence import append_trading_automation_event, create_trading_automation_session
from .alpaca_orphan_claims import (
    ALPACA_EXECUTION_FAMILIES,
    acquire_action_claim,
    alpaca_asset_class_is_crypto,
    alpaca_symbol_is_crypto_like,
    guard_alpaca_entry_ownership,
    resolve_action_claim,
)
from .risk_evaluator import evaluate_proposed_momentum_automation
from .risk_policy import build_session_risk_snapshot, resolve_effective_risk_policy
from .captured_paper_service_fence import (
    try_acquire_generic_alpaca_arm_fence,
)
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
STATE_LIVE_ARM_EXPIRED = "live_arm_expired"  # FIX-18: a zombie live_arm_pending, terminalized
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
        STATE_LIVE_ARM_EXPIRED,
    }
)


def _alpaca_execution_quarantine_reason(
    execution_family: str | None,
    symbol: str | None,
    *,
    asset_class: Any = None,
) -> str | None:
    """Keep the currently certified Alpaca lane paper/equity/long-only.

    Existing configuration and API inputs can otherwise manufacture a live-mode
    session for the real Alpaca endpoint, a crypto pair, or the unfinished short
    family.  Reject those shapes before an arm token or broker-action claim is
    created; the order boundary repeats this check for already-persisted rows.
    """
    family = normalize_execution_family(execution_family)
    if family not in ALPACA_EXECUTION_FAMILIES:
        return None
    if not bool(getattr(settings, "chili_alpaca_paper", True)):
        return "alpaca_live_posture_not_certified"
    if (
        alpaca_symbol_is_crypto_like(symbol)
        or alpaca_asset_class_is_crypto(asset_class)
    ):
        return "alpaca_crypto_execution_not_certified"
    if family == "alpaca_short":
        return "alpaca_short_execution_not_certified"
    return None


def _frozen_alpaca_account_scope(
    sess: TradingAutomationSession,
) -> str | None:
    """Account identity persisted when this exact arm was created."""
    if normalize_execution_family(sess.execution_family) not in ALPACA_EXECUTION_FAMILIES:
        return None
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    scope = str(snap.get("alpaca_account_scope") or "").strip().lower()
    return scope or None


def _certified_alpaca_account_id(
    execution_family: str | None,
) -> tuple[str | None, str | None]:
    """Read the stable paper-account UUID when creating an execution generation."""
    if normalize_execution_family(execution_family) not in ALPACA_EXECUTION_FAMILIES:
        return None, None
    try:
        from ..venue.alpaca_spot import AlpacaSpotAdapter

        snap = AlpacaSpotAdapter().get_account_snapshot()
    except Exception:
        return None, "alpaca_account_identity_unavailable"
    snap = snap if isinstance(snap, dict) else {}
    account_id = str(snap.get("account_id") or "").strip()
    if not (snap.get("ok") is True and snap.get("paper") is True and account_id):
        return None, "alpaca_account_identity_unavailable"
    return account_id, None


def _certified_non_alpaca_account_identity(
    execution_family: str | None,
) -> tuple[str | None, str | None]:
    """Read the non-secret account generation before creating a live arm."""
    family = normalize_execution_family(execution_family)
    if family in ALPACA_EXECUTION_FAMILIES:
        return None, None
    truth = read_current_non_alpaca_account_identity(family)
    identity = str(truth.get("identity") or "").strip()
    if truth.get("ok") is not True or not identity:
        return None, str(
            truth.get("reason") or "non_alpaca_account_identity_unknown"
        )
    return identity, None


def _persisted_alpaca_execution_quarantine_reason(
    sess: TradingAutomationSession,
) -> str | None:
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    live = snap.get("momentum_live_execution")
    live = live if isinstance(live, dict) else {}
    position = live.get("position")
    position = position if isinstance(position, dict) else {}
    explicit_asset_class = (
        position.get("asset_class")
        or live.get("asset_class")
        or snap.get("asset_class")
    )
    reason = _alpaca_execution_quarantine_reason(
        sess.execution_family,
        sess.symbol,
        asset_class=explicit_asset_class,
    )
    if reason is not None:
        return reason
    if (
        normalize_execution_family(sess.execution_family) in ALPACA_EXECUTION_FAMILIES
        and _frozen_alpaca_account_scope(sess) != "alpaca:paper"
    ):
        return "alpaca_account_scope_unfrozen_or_mismatched"
    return None


def _arm_pending_ttl_expired(row: TradingAutomationSession) -> bool:
    """FIX-18 (B1) — is this live_arm_pending session a stranded ZOMBIE past the dedupe TTL?

    A transient confirm failure strands a live_arm_pending session; without this the
    begin_live_arm dedupe returns it as "already active" for hours, blocking re-arm of the
    SAME symbol (80 zombies/7d, median 6.6h). Only live_arm_pending is eligible (a
    genuinely-active session is never expired). Age is measured off created_at (when the arm
    token was minted). Flag OFF / fresh pending / no timestamp => not expired (fresh dedupes,
    byte-identical legacy).
    """
    if not bool(getattr(settings, "chili_momentum_arm_pending_ttl_enabled", True)):
        return False
    if row.state != STATE_LIVE_ARM_PENDING:
        return False
    created = getattr(row, "created_at", None)
    if not isinstance(created, datetime):
        return False
    try:
        ttl = float(getattr(settings, "chili_momentum_arm_pending_ttl_seconds", 120.0) or 120.0)
    except (TypeError, ValueError):
        ttl = 120.0
    age_sec = (_utcnow() - created).total_seconds()
    return age_sec > ttl


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
    alloc = sess.allocation_decision_json if isinstance(getattr(sess, "allocation_decision_json", None), dict) else {}
    if (
        alloc
        and not alloc.get("allowed_if_enforced", True)
        and bool(settings.brain_allocator_live_hard_block_enabled)
    ):
        rd["_allocator_block_live"] = str(alloc.get("blocked_reason") or "allocator_blocked")
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
        "allocation": alloc or None,
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
    # Venue-aware dedup: key on EXECUTION_FAMILY too so a crypto name can hold
    # both its coinbase primary paper session AND an alpaca paper twin (the
    # fill-quality A/B). Without this the twin dedups against the primary and
    # never spawns. Mirrors the live dedup. (docs/DESIGN/ALPACA_LANE.md)
    _ef_dedup = normalize_execution_family(execution_family)
    existing = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSession.symbol == sym,
            TradingAutomationSession.variant_id == int(variant_id),
            TradingAutomationSession.mode == "paper",
            TradingAutomationSession.execution_family == _ef_dedup,
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
                "message": "Existing paper automation session reused for this symbol/variant/venue.",
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

    ef = normalize_execution_family(execution_family)
    snap = build_session_risk_snapshot(
        policy_full=policy_full,
        evaluation=ev,
        viability_brief=vb,
        readiness_subset=rs,
        extra=None,
        execution_family=ef,
        db=db,
    )

    runner_on = bool(settings.chili_momentum_paper_runner_enabled)
    initial_state = STATE_QUEUED if runner_on else STATE_DRAFT
    sess = create_trading_automation_session(
        db,
        user_id=user_id,
        venue=venue_for_execution_family(ef),
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



def _lock_live_symbol_arm(db: Session, *, user_id: int, symbol: str) -> bool:
    """Serialize live arms for one user/symbol across auto-arm AND event admission
    (pg_advisory_xact_lock releases at COMMIT: the racing second arm blocks, then
    sees the committed session and dedups — double-arm impossible by construction).
    Ported 2026-07-09 with the event-admission consumer."""
    try:
        bind = db.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", "") != "postgresql":
            return True
        from sqlalchemy import text as _sql_text

        key = f"momentum_live_arm:{int(user_id)}:{str(symbol or '').strip().upper()}"
        db.execute(_sql_text("select pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
        return True
    except Exception:
        _log.debug("[operator_actions] live symbol advisory lock unavailable", exc_info=True)
        return False


def _generic_alpaca_arm_process_fence_acquired(
    db: Session,
    *,
    execution_family: str,
) -> bool:
    """Exclude generic Alpaca arming while captured PAPER owns the process lane.

    Non-Alpaca families intentionally bypass this exact fence.  Alpaca callers
    hold the successful transaction advisory lock until their surrounding
    transaction ends, so a captured service cannot start halfway through an
    arm/promote mutation.  Any lock/read failure is a fail-closed rejection.
    """

    if normalize_execution_family(execution_family) not in ALPACA_EXECUTION_FAMILIES:
        return True
    return try_acquire_generic_alpaca_arm_fence(
        db,
        account_scope="alpaca:paper",
    ) is True


def _captured_paper_service_fence_rejection() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "captured_paper_service_owns_alpaca_arm_path",
        "message": (
            "The dedicated captured Alpaca PAPER service owns arm creation; "
            "the generic arm path made no change."
        ),
    }


def _live_symbol_arm_lock_acquired(
    db: Session,
    *,
    user_id: int,
    symbol: str,
) -> bool:
    """Treat a false result or an unexpected lock-helper failure identically."""
    try:
        return _lock_live_symbol_arm(
            db,
            user_id=int(user_id),
            symbol=str(symbol),
        ) is True
    except Exception:
        _log.debug(
            "[operator_actions] live symbol arm lock check failed",
            exc_info=True,
        )
        return False


def _active_live_arm_for_identity(
    db: Session,
    *,
    user_id: int,
    symbol: str,
    variant_id: int,
    execution_family: str,
) -> TradingAutomationSession | None:
    """Read the active generation after the per-user/symbol transaction fence."""
    rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSession.symbol == str(symbol).strip().upper(),
            TradingAutomationSession.variant_id == int(variant_id),
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.execution_family
            == normalize_execution_family(execution_family),
            TradingAutomationSession.state != STATE_ARCHIVED,
        )
        .order_by(TradingAutomationSession.updated_at.desc())
        .all()
    )
    return next(
        (row for row in rows if row.state not in _TERMINAL_OPERATOR_STATES),
        None,
    )


def _deduped_live_arm_payload(
    row: TradingAutomationSession,
) -> dict[str, Any]:
    snap = row.risk_snapshot_json if isinstance(row.risk_snapshot_json, dict) else {}
    return {
        "ok": True,
        "session_id": int(row.id),
        "arm_token": snap.get("arm_token"),
        "state": row.state,
        "mode": row.mode,
        "source_paper_session_id": row.source_paper_session_id,
        "deduped": True,
        "message": "Existing live automation session reused for this symbol/variant.",
    }


def _arm_generation_fingerprint(sess: TradingAutomationSession) -> tuple[str, ...]:
    """Immutable identity used to CAS one pending arm across long confirmation work."""
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    return (
        str(snap.get("arm_token") or ""),
        str(snap.get("expires_at_utc") or ""),
        str(snap.get("alpaca_symbol_claim_token") or ""),
        str(snap.get("alpaca_account_scope") or ""),
        str(snap.get("alpaca_account_id") or ""),
        str(snap.get(NON_ALPACA_ACCOUNT_IDENTITY_KEY) or ""),
    )


def _reload_pending_arm_generation_for_update(
    db: Session,
    *,
    session_id: int,
    user_id: int,
    expected_generation: tuple[str, ...],
) -> TradingAutomationSession | None:
    """Reload the exact pending generation under a durable row lock."""
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.id == int(session_id),
        TradingAutomationSession.user_id == int(user_id),
    )
    try:
        q = q.with_for_update()
    except Exception:
        pass
    try:
        q = q.populate_existing()
    except Exception:
        pass
    row = q.one_or_none()
    if (
        row is None
        or row.state != STATE_LIVE_ARM_PENDING
        or _arm_generation_fingerprint(row) != expected_generation
    ):
        return None
    return row


def _arm_expired(sess: TradingAutomationSession, *, now: datetime | None = None) -> bool:
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    raw = snap.get("expires_at_utc")
    if not isinstance(raw, str):
        return False
    try:
        exp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return False
    if exp.tzinfo is not None:
        exp = exp.replace(tzinfo=None)
    return (now or _utcnow()) > exp


def _terminalize_expired_arm_generation(
    db: Session,
    sess: TradingAutomationSession,
    *,
    reason: str,
) -> dict[str, Any]:
    """Resolve only a proven pre-HTTP claim, then expire the fenced generation."""
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    claim_token = str(snap.get("alpaca_symbol_claim_token") or "").strip()
    if normalize_execution_family(sess.execution_family) in ALPACA_EXECUTION_FAMILIES:
        scope = _frozen_alpaca_account_scope(sess)
        if scope != "alpaca:paper" or not claim_token:
            return {
                "ok": False,
                "error": "expired_arm_claim_not_pre_http",
                "message": (
                    "The expired arm lacks certified frozen claim identity; "
                    "it remains non-terminal for reconciliation."
                ),
            }
        if not resolve_action_claim(
            db,
            symbol=sess.symbol,
            claim_token=claim_token,
            client_order_id=None,
            broker_order_id=None,
            broker_order_status="not_submitted",
            proven_no_transport=True,
            metadata={"reason": str(reason)},
            account_scope=scope,
        ):
            return {
                "ok": False,
                "error": "expired_arm_claim_not_pre_http",
                "message": (
                    "The expired arm has broker-side order evidence and cannot be "
                    "terminalized before exact reconciliation."
                ),
            }
    now = _utcnow()
    sess.state = STATE_LIVE_ARM_EXPIRED
    sess.ended_at = now
    sess.updated_at = now
    return {
        "ok": False,
        "error": "token_expired",
        "state": sess.state,
        "message": "Arm token expired; start arm flow again.",
    }


def begin_live_arm(
    db: Session,
    *,
    user_id: Optional[int],
    symbol: str,
    variant_id: int,
    execution_family: str = "coinbase_spot",
    expected_guarded_account_scope: str | None = None,
    expected_guarded_account_identity: str | None = None,
) -> dict[str, Any]:
    """Validate live eligibility/risk and create a pending arm.

    ``expected_guarded_account_*`` is a consistency fence, never an identity
    override. Auto-arm supplies the exact account generation whose loss history
    it just checked; this function independently re-reads/freeze-binds current
    broker identity and rejects A->B rotation before a claim or session exists.
    """
    if user_id is None:
        return {"ok": False, "error": "user_required", "message": "Paired user required."}
    from ..portfolio_allocator import build_session_allocation_decision

    sym = symbol.strip().upper()
    _ef_norm = normalize_execution_family(execution_family)
    _quarantine_reason = _alpaca_execution_quarantine_reason(_ef_norm, sym)
    if _quarantine_reason:
        return {
            "ok": False,
            "error": _quarantine_reason,
            "message": "This Alpaca execution posture is quarantined pending certification.",
        }
    if not _generic_alpaca_arm_process_fence_acquired(
        db,
        execution_family=_ef_norm,
    ):
        return _captured_paper_service_fence_rejection()
    if not _live_symbol_arm_lock_acquired(
        db,
        user_id=int(user_id),
        symbol=sym,
    ):
        return {
            "ok": False,
            "error": "live_arm_generation_lock_unavailable",
            "message": "Could not safely serialize this live arm; no session was created.",
        }
    # Venue-aware dedup: key on (symbol, variant, mode, EXECUTION_FAMILY) so the SAME name can
    # hold one live session PER VENUE (e.g. an A/B of robinhood_spot real vs alpaca_spot paper).
    # Same-venue same-symbol+variant still dedups -> no double-arm / double real-money exposure
    # on a single venue. (docs/DESIGN/ALPACA_LANE.md — the same-name A/B enabler.)
    existing = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSession.symbol == sym,
            TradingAutomationSession.variant_id == int(variant_id),
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.execution_family == _ef_norm,
            TradingAutomationSession.state != STATE_ARCHIVED,
        )
        .order_by(TradingAutomationSession.updated_at.desc())
        .all()
    )
    for row in existing:
        if row.state not in _TERMINAL_OPERATOR_STATES:
            # FIX-18 (B1) ZOMBIE-WALL TTL: a live_arm_pending stranded by a transient confirm
            # failure would dedupe forever, blocking re-arm of the SAME symbol for hours.
            # Terminalize an EXPIRED pending (live_arm_expired) and fall through to re-arm.
            # A genuinely-active session or a FRESH pending still dedupes (no double-arm).
            if _arm_pending_ttl_expired(row):
                _expired_snap = (
                    row.risk_snapshot_json
                    if isinstance(row.risk_snapshot_json, dict)
                    else {}
                )
                _expired_claim_token = str(
                    _expired_snap.get("alpaca_symbol_claim_token") or ""
                ).strip()
                if _ef_norm in ALPACA_EXECUTION_FAMILIES:
                    _expired_scope = _frozen_alpaca_account_scope(row)
                    if _expired_scope != "alpaca:paper" or not _expired_claim_token:
                        return {
                            "ok": False,
                            "error": "expired_arm_claim_not_pre_http",
                            "message": (
                                "The expired arm has no certified frozen paper claim "
                                "identity and cannot be replaced automatically."
                            ),
                        }
                    if not resolve_action_claim(
                        db,
                        symbol=sym,
                        claim_token=_expired_claim_token,
                        client_order_id=None,
                        broker_order_id=None,
                        broker_order_status="not_submitted",
                        proven_no_transport=True,
                        metadata={"reason": "arm_pending_ttl_expired_before_submit"},
                        account_scope=_expired_scope,
                    ):
                        return {
                            "ok": False,
                            "error": "expired_arm_claim_not_pre_http",
                            "message": (
                                "The expired arm has broker-side order evidence and "
                                "cannot be replaced until exact reconciliation."
                            ),
                            "symbol_claim_token": _expired_claim_token,
                        }
                row.state = STATE_LIVE_ARM_EXPIRED
                row.ended_at = _utcnow()
                row.updated_at = _utcnow()
                append_trading_automation_event(
                    db,
                    row.id,
                    "live_arm_expired",
                    {
                        "reason": "arm_pending_ttl",
                        "symbol": sym,
                        "variant_id": int(variant_id),
                        "age_sec": round((_utcnow() - row.created_at).total_seconds(), 1),
                    },
                    correlation_id=getattr(row, "correlation_id", None),
                    source_node_id="momentum_operator_api",
                )
                _log.warning(
                    "[operator_actions] terminalized zombie live_arm_pending session=%s "
                    "symbol=%s (age>%ss) -> live_arm_expired; allowing re-arm",
                    int(row.id), sym,
                    getattr(settings, "chili_momentum_arm_pending_ttl_seconds", 120.0),
                )
                continue
            snap = row.risk_snapshot_json if isinstance(row.risk_snapshot_json, dict) else {}
            _expected_identity = str(
                expected_guarded_account_identity or ""
            ).strip()
            _expected_scope = str(
                expected_guarded_account_scope or ""
            ).strip().lower()
            if _ef_norm in ALPACA_EXECUTION_FAMILIES:
                _dedup_identity = str(snap.get("alpaca_account_id") or "").strip()
                _dedup_scope = str(
                    snap.get("alpaca_account_scope") or ""
                ).strip().lower()
            else:
                _dedup_identity = str(
                    snap.get(NON_ALPACA_ACCOUNT_IDENTITY_KEY) or ""
                ).strip()
                _dedup_scope = ""
            if _expected_scope and _expected_scope != _dedup_scope:
                return {
                    "ok": False,
                    "error": "account_scope_changed_since_loss_guard",
                    "message": (
                        "The existing arm belongs to a different guarded account "
                        "scope; it was not reused."
                    ),
                }
            if _expected_identity and _expected_identity != _dedup_identity:
                return {
                    "ok": False,
                    "error": "account_identity_changed_since_loss_guard",
                    "message": (
                        "The existing arm belongs to a different guarded account "
                        "generation; it was not reused."
                    ),
                }
            if _ef_norm in ALPACA_EXECUTION_FAMILIES:
                _current_identity, _current_identity_error = (
                    _certified_alpaca_account_id(_ef_norm)
                )
                _current_scope = "alpaca:paper"
            else:
                _current_identity, _current_identity_error = (
                    _certified_non_alpaca_account_identity(_ef_norm)
                )
                _current_scope = ""
            if _current_identity_error is not None or not _current_identity:
                return {
                    "ok": False,
                    "error": str(
                        _current_identity_error
                        or "account_identity_unavailable_before_dedup"
                    ),
                    "message": (
                        "The current broker account identity could not be verified; "
                        "the existing arm was not reused."
                    ),
                }
            if _expected_scope and _expected_scope != _current_scope:
                return {
                    "ok": False,
                    "error": "account_scope_changed_since_loss_guard",
                    "message": (
                        "The broker account scope changed after loss-history "
                        "admission; the existing arm was not reused."
                    ),
                }
            if _expected_identity and _expected_identity != _current_identity:
                return {
                    "ok": False,
                    "error": "account_identity_changed_since_loss_guard",
                    "message": (
                        "The broker account generation changed after loss-history "
                        "admission; the existing arm was not reused."
                    ),
                }
            if _dedup_scope != _current_scope:
                return {
                    "ok": False,
                    "error": "existing_arm_account_scope_mismatch",
                    "message": (
                        "The existing arm belongs to a different current broker "
                        "account scope; it was not reused."
                    ),
                }
            if _dedup_identity != _current_identity:
                return {
                    "ok": False,
                    "error": "existing_arm_account_identity_mismatch",
                    "message": (
                        "The existing arm belongs to a different current broker "
                        "account generation; it was not reused."
                    ),
                }
            return {
                "ok": True,
                "session_id": int(row.id),
                "arm_token": snap.get("arm_token"),
                "state": row.state,
                "mode": row.mode,
                "deduped": True,
                "message": "Existing live automation session reused for this symbol/variant.",
            }
    _ownership_ok, _orphan_claim, _ownership_reason = guard_alpaca_entry_ownership(
        db,
        symbol=sym,
        execution_family=_ef_norm,
        account_scope=("alpaca:paper" if _ef_norm in ALPACA_EXECUTION_FAMILIES else None),
    )
    if not _ownership_ok:
        return {
            "ok": False,
            "error": _ownership_reason or "orphan_flatten_claim_pending",
            "message": "An unresolved broker action owns this symbol; live arm is blocked.",
            "symbol_claim_token": (
                str(_orphan_claim.get("claim_token")) if _orphan_claim is not None else None
            ),
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
    _ownership_claim_token = f"arm-{token}"
    _alpaca_account_id, _alpaca_account_error = _certified_alpaca_account_id(
        _ef_norm
    )
    if _alpaca_account_error is not None:
        return {
            "ok": False,
            "error": _alpaca_account_error,
            "message": "The paper Alpaca account identity could not be frozen safely.",
        }
    _non_alpaca_account_identity, _non_alpaca_account_error = (
        _certified_non_alpaca_account_identity(_ef_norm)
    )
    if _non_alpaca_account_error is not None:
        return {
            "ok": False,
            "error": _non_alpaca_account_error,
            "message": "The broker account identity could not be frozen safely.",
        }
    _expected_identity = str(expected_guarded_account_identity or "").strip()
    _expected_scope = str(expected_guarded_account_scope or "").strip().lower()
    _frozen_identity = (
        _alpaca_account_id
        if _ef_norm in ALPACA_EXECUTION_FAMILIES
        else _non_alpaca_account_identity
    )
    _frozen_scope = (
        "alpaca:paper" if _ef_norm in ALPACA_EXECUTION_FAMILIES else None
    )
    if _expected_scope and _expected_scope != str(_frozen_scope or "").lower():
        return {
            "ok": False,
            "error": "account_scope_changed_since_loss_guard",
            "message": (
                "The broker account scope changed after loss-history admission; "
                "no arm or broker-action claim was created."
            ),
        }
    if _expected_identity and _expected_identity != str(_frozen_identity or ""):
        return {
            "ok": False,
            "error": "account_identity_changed_since_loss_guard",
            "message": (
                "The broker account generation changed after loss-history admission; "
                "no arm or broker-action claim was created."
            ),
        }
    if _ef_norm in ALPACA_EXECUTION_FAMILIES:
        _claim = acquire_action_claim(
            db,
            symbol=sym,
            action="entry",
            claim_token=_ownership_claim_token,
            owner_session_id=None,
            metadata={
                "stage": "begin_live_arm",
                "variant_id": int(variant_id),
                "alpaca_account_id": _alpaca_account_id,
            },
            account_scope="alpaca:paper",
        )
        if not _claim.get("ok"):
            return {
                "ok": False,
                "error": _claim.get("reason") or "symbol_action_claimed",
                "message": "The Alpaca account/symbol is owned by another unresolved broker action.",
            }

    snap = build_session_risk_snapshot(
        policy_full=policy_full,
        evaluation=ev,
        viability_brief=_viability_brief(row),
        readiness_subset=_readiness_subset(row),
        extra={
            "arm_token": token,
            "expires_at_utc": expires,
            "phase": 6,
            "alpaca_symbol_claim_token": (
                _ownership_claim_token if _ef_norm in ALPACA_EXECUTION_FAMILIES else None
            ),
            "alpaca_account_scope": (
                "alpaca:paper" if _ef_norm in ALPACA_EXECUTION_FAMILIES else None
            ),
            "alpaca_account_id": _alpaca_account_id,
            NON_ALPACA_ACCOUNT_IDENTITY_KEY: _non_alpaca_account_identity,
        },
        execution_family=execution_family,
        db=db,
    )

    ef_live = normalize_execution_family(execution_family)

    sess = create_trading_automation_session(
        db,
        user_id=user_id,
        venue=venue_for_execution_family(ef_live),
        execution_family=ef_live,
        mode="live",
        symbol=sym,
        variant_id=int(variant_id),
        state=STATE_LIVE_ARM_PENDING,
        risk_snapshot_json=snap,
        correlation_id=str(uuid.uuid4()),
        source_node_id="momentum_operator_api",
    )
    if _ef_norm in ALPACA_EXECUTION_FAMILIES:
        _bound_claim = acquire_action_claim(
            db,
            symbol=sym,
            action="entry",
            claim_token=_ownership_claim_token,
            owner_session_id=int(sess.id),
            metadata={
                "stage": "live_arm_reserved",
                "variant_id": int(variant_id),
                "alpaca_account_id": _alpaca_account_id,
            },
            account_scope="alpaca:paper",
        )
        if not _bound_claim.get("ok"):
            resolve_action_claim(
                db,
                symbol=sym,
                claim_token=_ownership_claim_token,
                client_order_id=None,
                broker_order_id=None,
                broker_order_status="not_submitted",
                proven_no_transport=True,
                metadata={"reason": "owner_bind_failed"},
                account_scope="alpaca:paper",
            )
            sess.state = STATE_ERROR
            return {
                "ok": False,
                "error": "symbol_action_claim_bind_failed",
                "message": "Could not bind the Alpaca entry reservation to this session.",
            }
    allocation = build_session_allocation_decision(
        db,
        sess,
        user_id=user_id,
        context="momentum_live_request",
    )
    if (
        not allocation.get("allowed_if_enforced", True)
        and bool(settings.brain_allocator_live_hard_block_enabled)
    ):
        if _ef_norm in ALPACA_EXECUTION_FAMILIES:
            resolve_action_claim(
                db,
                symbol=sym,
                claim_token=_ownership_claim_token,
                client_order_id=None,
                broker_order_id=None,
                broker_order_status="not_submitted",
                proven_no_transport=True,
                metadata={"reason": "allocator_blocked"},
                account_scope="alpaca:paper",
            )
        sess.state = STATE_ERROR
        sess.updated_at = _utcnow()
        append_trading_automation_event(
            db,
            sess.id,
            "live_arm_blocked_allocator",
            {"blocked_reason": allocation.get("blocked_reason")},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_operator_api",
        )
        return {
            "ok": False,
            "error": allocation.get("blocked_reason") or "allocator_blocked",
            "message": "Portfolio allocator blocks live arm for this symbol/variant.",
            "allocation": allocation,
        }
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
        "allocation": allocation,
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

    arm_generation = _arm_generation_fingerprint(sess)
    _quarantine_reason = _persisted_alpaca_execution_quarantine_reason(sess)
    if _quarantine_reason:
        return {
            "ok": False,
            "error": _quarantine_reason,
            "message": "This Alpaca execution posture is quarantined pending certification.",
        }
    if not _generic_alpaca_arm_process_fence_acquired(
        db,
        execution_family=sess.execution_family,
    ):
        return _captured_paper_service_fence_rejection()

    if _arm_expired(sess):
        if user_id is None:
            return {"ok": False, "error": "user_required", "message": "Paired user required."}
        if not _live_symbol_arm_lock_acquired(
            db,
            user_id=int(user_id),
            symbol=sess.symbol,
        ):
            return {
                "ok": False,
                "error": "live_arm_generation_lock_unavailable",
                "message": "Could not safely fence this arm generation; confirmation was deferred.",
            }
        locked = _reload_pending_arm_generation_for_update(
            db,
            session_id=int(sess.id),
            user_id=int(user_id),
            expected_generation=arm_generation,
        )
        if locked is None:
            return {
                "ok": False,
                "error": "arm_generation_changed",
                "message": "This pending arm changed or expired while confirmation was in progress.",
            }
        sess = locked
        if _arm_expired(sess):
            return _terminalize_expired_arm_generation(
                db,
                sess,
                reason="arm_token_expired_before_submit",
            )

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

    # WAVE-4 ITEM-6(b) — ARM-TIME MINIMUM-REMAINING-BUDGET refresh. A viability row already
    # past HALF the max-age has < 0.5x the freshness budget left, so the entry can go stale
    # mid-tick (DXST confirmed at 537s/600s and died). Inline re-score the ONE symbol via the
    # existing pipeline seam and RE-READ the row, so we confirm on a FRESH score — never
    # blind-touch freshness_ts (that fakes freshness without re-validating live-eligibility).
    if bool(getattr(settings, "chili_momentum_arm_time_viability_refresh_enabled", True)):
        try:
            _max_age = float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0)
            if not math.isfinite(_max_age) or _max_age <= 0.0:
                raise ValueError("invalid viability freshness budget")
            _ft = getattr(row, "freshness_ts", None)
            _age = (_utcnow() - _ft).total_seconds() if isinstance(_ft, datetime) else None
            if _age is None or not math.isfinite(_age):
                raise ValueError("viability freshness timestamp unavailable")
            if _age is not None and _age > 0.5 * _max_age:
                from .pipeline import run_momentum_neural_tick

                run_momentum_neural_tick(db, meta={"tickers": [sess.symbol]})
                db.expire(row)  # force a re-read of the just-refreshed row
                row = (
                    db.query(MomentumSymbolViability)
                    .filter(
                        MomentumSymbolViability.symbol == sess.symbol,
                        MomentumSymbolViability.variant_id == sess.variant_id,
                    )
                    .one_or_none()
                )
                # Confirm ONLY on a fresh, still-eligible score. A re-score that dropped the
                # name below live-eligibility (or failed to write a row) BLOCKS the confirm —
                # we never arm on a stale row we could not refresh.
                if not row or not row.live_eligible:
                    return {
                        "ok": False,
                        "error": "no_longer_eligible",
                        "message": "Strategy is no longer live-eligible after the arm-time viability refresh.",
                    }
                _ft2 = getattr(row, "freshness_ts", None)
                _age2 = (_utcnow() - _ft2).total_seconds() if isinstance(_ft2, datetime) else None
                if _age2 is None or not math.isfinite(_age2) or _age2 > _max_age:
                    return {
                        "ok": False,
                        "error": "viability_stale",
                        "message": "Viability could not be refreshed within the freshness budget; not confirming.",
                    }
        except Exception as exc:
            _log.warning(
                "[operator_actions] required arm-time viability refresh failed",
                exc_info=True,
            )
            return {
                "ok": False,
                "error": "viability_refresh_unavailable",
                "message": "Required arm-time viability refresh failed; confirmation was deferred.",
                "detail": {"error_type": type(exc).__name__},
            }

    if user_id is None:
        return {"ok": False, "error": "user_required", "message": "Paired user required."}
    from ..portfolio_allocator import build_session_allocation_decision

    if not _live_symbol_arm_lock_acquired(
        db,
        user_id=int(user_id),
        symbol=sess.symbol,
    ):
        return {
            "ok": False,
            "error": "live_arm_generation_lock_unavailable",
            "message": "Could not safely fence this arm generation; confirmation was deferred.",
        }
    locked = _reload_pending_arm_generation_for_update(
        db,
        session_id=int(sess.id),
        user_id=int(user_id),
        expected_generation=arm_generation,
    )
    if locked is None:
        return {
            "ok": False,
            "error": "arm_generation_changed",
            "message": "This pending arm changed or expired while confirmation was in progress.",
        }
    sess = locked
    _quarantine_reason = _persisted_alpaca_execution_quarantine_reason(sess)
    if _quarantine_reason:
        return {
            "ok": False,
            "error": _quarantine_reason,
            "message": "This Alpaca execution posture is quarantined pending certification.",
        }
    if _arm_expired(sess):
        return _terminalize_expired_arm_generation(
            db,
            sess,
            reason="arm_token_expired_during_confirm",
        )

    _frozen_account_id = ""
    if normalize_execution_family(sess.execution_family) in ALPACA_EXECUTION_FAMILIES:
        _current_account_id, _account_error = _certified_alpaca_account_id(
            sess.execution_family
        )
        _frozen_account_id = str(
            (sess.risk_snapshot_json or {}).get("alpaca_account_id")
            if isinstance(sess.risk_snapshot_json, dict)
            else ""
        ).strip()
        if (
            _account_error is not None
            or not _frozen_account_id
            or _current_account_id != _frozen_account_id
        ):
            return {
                "ok": False,
                "error": (
                    "alpaca_account_identity_mismatch"
                    if _current_account_id and _frozen_account_id
                    else "alpaca_account_identity_unavailable"
                ),
                "message": "The paper Alpaca account no longer matches this arm generation.",
            }
    _frozen_non_alpaca_identity = ""
    if normalize_execution_family(sess.execution_family) not in ALPACA_EXECUTION_FAMILIES:
        _account_generation = verify_frozen_non_alpaca_account_identity(sess)
        _frozen_non_alpaca_identity = str(
            _account_generation.get("frozen_identity") or ""
        ).strip()
        if _account_generation.get("ok") is not True:
            return {
                "ok": False,
                "error": str(
                    _account_generation.get("reason")
                    or "non_alpaca_account_identity_unknown"
                ),
                "message": "The broker account no longer matches this arm generation.",
            }

    _ownership_ok, _orphan_claim, _ownership_reason = guard_alpaca_entry_ownership(
        db,
        symbol=sess.symbol,
        execution_family=sess.execution_family,
        owner_session_id=int(sess.id),
        account_scope=_frozen_alpaca_account_scope(sess),
    )
    if not _ownership_ok:
        return {
            "ok": False,
            "error": _ownership_reason or "orphan_flatten_claim_pending",
            "message": "An unresolved broker orphan-flatten claim blocks live confirmation.",
            "symbol_claim_token": (
                str(_orphan_claim.get("claim_token")) if _orphan_claim is not None else None
            ),
        }

    rd0 = build_momentum_operator_readiness(execution_family=sess.execution_family, symbol=sess.symbol)
    if not rd0.get("broker_ready_for_live"):
        _venue_msg = (
            "connect Robinhood + enable the Robinhood spot adapter"
            if normalize_execution_family(sess.execution_family) == EXECUTION_FAMILY_ROBINHOOD_SPOT
            else "connect Coinbase Advanced"
        )
        return {
            "ok": False,
            "error": "broker_not_ready",
            "message": f"Broker not ready for live ({_venue_msg}).",
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
        execution_family=sess.execution_family,
        db=db,
    )
    final_snap["arm_confirmed_at_utc"] = _utcnow().isoformat()
    final_snap["arm_confirmed"] = True
    if normalize_execution_family(sess.execution_family) in ALPACA_EXECUTION_FAMILIES:
        final_snap["alpaca_account_scope"] = "alpaca:paper"
        final_snap["alpaca_account_id"] = _frozen_account_id
    else:
        final_snap[NON_ALPACA_ACCOUNT_IDENTITY_KEY] = _frozen_non_alpaca_identity
    # Live-eligibility recency-grace ANCHOR (UPC +500% TOCTOU miss). At THIS point the name
    # is provably live-eligible (row.live_eligible re-checked above) AND the risk eval
    # allowed it — so stamp the confirm instant as the arm-time live-eligibility anchor the
    # runner's entry gate reads. If neural re-scoring later FLICKERS live_eligible False at
    # the exact entry instant, the recency grace tolerates it (within the window + live
    # forward momentum). Absent stamp ⇒ no grace ⇒ today's block (fail-safe).
    final_snap["live_eligible_at_utc"] = final_snap["arm_confirmed_at_utc"]
    allocation = build_session_allocation_decision(
        db,
        sess,
        user_id=user_id,
        context="momentum_live_confirm",
    )
    if (
        not allocation.get("allowed_if_enforced", True)
        and bool(settings.brain_allocator_live_hard_block_enabled)
    ):
        return {
            "ok": False,
            "error": allocation.get("blocked_reason") or "allocator_blocked",
            "message": "Portfolio allocator blocks confirming live arm.",
            "allocation": allocation,
        }

    if (
        sess.state != STATE_LIVE_ARM_PENDING
        or _arm_generation_fingerprint(sess) != arm_generation
    ):
        return {
            "ok": False,
            "error": "arm_generation_changed",
            "message": "This pending arm changed while confirmation was in progress.",
        }
    if _arm_expired(sess):
        return _terminalize_expired_arm_generation(
            db,
            sess,
            reason="arm_token_expired_pre_queued_transition",
        )
    if normalize_execution_family(sess.execution_family) in ALPACA_EXECUTION_FAMILIES:
        _current_account_id, _account_error = _certified_alpaca_account_id(
            sess.execution_family
        )
        if (
            _account_error is not None
            or not _frozen_account_id
            or _current_account_id != _frozen_account_id
        ):
            return {
                "ok": False,
                "error": (
                    "alpaca_account_identity_mismatch"
                    if _current_account_id and _frozen_account_id
                    else "alpaca_account_identity_unavailable"
                ),
                "message": "The paper Alpaca account changed before final confirmation.",
            }
    else:
        _account_generation = verify_frozen_non_alpaca_account_identity(sess)
        if (
            _account_generation.get("ok") is not True
            or str(_account_generation.get("frozen_identity") or "").strip()
            != _frozen_non_alpaca_identity
        ):
            return {
                "ok": False,
                "error": str(
                    _account_generation.get("reason")
                    or "non_alpaca_account_identity_unknown"
                ),
                "message": "The broker account changed before final confirmation.",
            }

    # Durable admission proof for the runner.  This is stamped only after the
    # exact generation survived the final row-lock CAS, expiry check, claim gate,
    # risk evaluation, and account-ID recheck above.
    final_snap["confirmed_arm_generation"] = {
        "version": 1,
        "session_id": int(sess.id),
        "arm_token": arm_generation[0],
        "expires_at_utc": arm_generation[1],
        "alpaca_symbol_claim_token": arm_generation[2],
        "alpaca_account_scope": arm_generation[3],
        "alpaca_account_id": arm_generation[4],
        "non_alpaca_account_identity": arm_generation[5],
        "confirmed_at_utc": str(final_snap["arm_confirmed_at_utc"]),
    }

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
        "allocation": allocation,
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
    from ..portfolio_allocator import build_session_allocation_decision

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
    _quarantine_reason = _alpaca_execution_quarantine_reason(ef, paper.symbol)
    if _quarantine_reason:
        return {
            "ok": False,
            "error": _quarantine_reason,
            "message": "This Alpaca execution posture is quarantined pending certification.",
        }

    if not _generic_alpaca_arm_process_fence_acquired(
        db,
        execution_family=ef,
    ):
        return _captured_paper_service_fence_rejection()

    if not _live_symbol_arm_lock_acquired(
        db,
        user_id=int(user_id),
        symbol=paper.symbol,
    ):
        return {
            "ok": False,
            "error": "live_arm_generation_lock_unavailable",
            "message": "Could not safely serialize this live promotion; no session was created.",
        }
    existing_live = _active_live_arm_for_identity(
        db,
        user_id=int(user_id),
        symbol=paper.symbol,
        variant_id=int(paper.variant_id),
        execution_family=ef,
    )
    if existing_live is not None:
        return _deduped_live_arm_payload(existing_live)
    _ownership_ok, _orphan_claim, _ownership_reason = guard_alpaca_entry_ownership(
        db,
        symbol=paper.symbol,
        execution_family=ef,
        account_scope=("alpaca:paper" if ef in ALPACA_EXECUTION_FAMILIES else None),
    )
    if not _ownership_ok:
        return {
            "ok": False,
            "error": _ownership_reason or "orphan_flatten_claim_pending",
            "message": "An unresolved broker orphan-flatten claim blocks live promotion.",
            "symbol_claim_token": (
                str(_orphan_claim.get("claim_token")) if _orphan_claim is not None else None
            ),
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
    _ownership_claim_token = f"arm-{token}"
    _alpaca_account_id, _alpaca_account_error = _certified_alpaca_account_id(ef)
    if _alpaca_account_error is not None:
        return {
            "ok": False,
            "error": _alpaca_account_error,
            "message": "The paper Alpaca account identity could not be frozen safely.",
        }
    _non_alpaca_account_identity, _non_alpaca_account_error = (
        _certified_non_alpaca_account_identity(ef)
    )
    if _non_alpaca_account_error is not None:
        return {
            "ok": False,
            "error": _non_alpaca_account_error,
            "message": "The broker account identity could not be frozen safely.",
        }
    if ef in ALPACA_EXECUTION_FAMILIES:
        _claim = acquire_action_claim(
            db,
            symbol=paper.symbol,
            action="entry",
            claim_token=_ownership_claim_token,
            owner_session_id=None,
            metadata={
                "stage": "promote_paper_session",
                "paper_session_id": int(paper.id),
                "alpaca_account_id": _alpaca_account_id,
            },
            account_scope="alpaca:paper",
        )
        if not _claim.get("ok"):
            return {
                "ok": False,
                "error": _claim.get("reason") or "symbol_action_claimed",
                "message": "The Alpaca account/symbol is owned by another unresolved broker action.",
            }

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
            "alpaca_symbol_claim_token": (
                _ownership_claim_token if ef in ALPACA_EXECUTION_FAMILIES else None
            ),
            "alpaca_account_scope": (
                "alpaca:paper" if ef in ALPACA_EXECUTION_FAMILIES else None
            ),
            "alpaca_account_id": _alpaca_account_id,
            NON_ALPACA_ACCOUNT_IDENTITY_KEY: _non_alpaca_account_identity,
            "promoted_from_paper_session_id": int(paper.id),
            "paper_session_state_at_promote": paper.state,
            "paper_risk_snapshot_excerpt": {
                "momentum_policy_caps": paper_snap.get("momentum_policy_caps"),
                "severity": paper_snap.get("severity"),
            },
        },
        execution_family=ef,
        db=db,
    )

    sess = create_trading_automation_session(
        db,
        user_id=user_id,
        venue=venue_for_execution_family(ef),
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
    if ef in ALPACA_EXECUTION_FAMILIES:
        _bound_claim = acquire_action_claim(
            db,
            symbol=paper.symbol,
            action="entry",
            claim_token=_ownership_claim_token,
            owner_session_id=int(sess.id),
            metadata={
                "stage": "promoted_live_arm_reserved",
                "alpaca_account_id": _alpaca_account_id,
            },
            account_scope="alpaca:paper",
        )
        if not _bound_claim.get("ok"):
            resolve_action_claim(
                db,
                symbol=paper.symbol,
                claim_token=_ownership_claim_token,
                client_order_id=None,
                broker_order_id=None,
                broker_order_status="not_submitted",
                proven_no_transport=True,
                metadata={"reason": "owner_bind_failed"},
                account_scope="alpaca:paper",
            )
            sess.state = STATE_ERROR
            return {
                "ok": False,
                "error": "symbol_action_claim_bind_failed",
                "message": "Could not bind the Alpaca entry reservation to this session.",
            }
    allocation = build_session_allocation_decision(
        db,
        sess,
        user_id=user_id,
        context="momentum_live_request_from_paper",
    )
    if (
        not allocation.get("allowed_if_enforced", True)
        and bool(settings.brain_allocator_live_hard_block_enabled)
    ):
        if ef in ALPACA_EXECUTION_FAMILIES:
            resolve_action_claim(
                db,
                symbol=paper.symbol,
                claim_token=_ownership_claim_token,
                client_order_id=None,
                broker_order_id=None,
                broker_order_status="not_submitted",
                proven_no_transport=True,
                metadata={"reason": "allocator_blocked"},
                account_scope="alpaca:paper",
            )
        sess.state = STATE_ERROR
        sess.updated_at = _utcnow()
        append_trading_automation_event(
            db,
            sess.id,
            "live_arm_blocked_allocator",
            {"blocked_reason": allocation.get("blocked_reason")},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_operator_api",
        )
        return {
            "ok": False,
            "error": allocation.get("blocked_reason") or "allocator_blocked",
            "message": "Portfolio allocator blocks live arm for this promoted paper session.",
            "allocation": allocation,
        }
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
        "allocation": allocation,
    }
