"""Structured outcome extraction from terminal momentum automation sessions (Phase 9)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from .live_fsm import STATE_LIVE_CANCELLED, STATE_LIVE_ERROR, STATE_LIVE_FINISHED
from .outcome_labels import (
    OUTCOME_ARCHIVED,
    OUTCOME_BAILOUT,
    OUTCOME_CANCELLED_IN_TRADE,
    OUTCOME_CANCELLED_PRE_ENTRY,
    OUTCOME_ERROR_EXIT,
    OUTCOME_EXPIRED_PRE_RUN,
    OUTCOME_FLAT_UNKNOWN,
    OUTCOME_GOVERNANCE_EXIT,
    OUTCOME_NO_FILL,
    OUTCOME_RISK_BLOCK,
    OUTCOME_SMALL_WIN,
    OUTCOME_STALE_DATA_ABORT,
    OUTCOME_STOP_LOSS,
    OUTCOME_SUCCESS,
    OUTCOME_TIMED_EXIT,
)
from .paper_fsm import (
    STATE_ARCHIVED,
    STATE_CANCELLED,
    STATE_ERROR,
    STATE_EXPIRED,
    STATE_FINISHED,
)
from .risk_policy import RISK_SNAPSHOT_KEY

KEY_PAPER = "momentum_paper_execution"
KEY_LIVE = "momentum_live_execution"


def _utc(d: Optional[datetime]) -> Optional[datetime]:
    if d is None:
        return None
    if d.tzinfo is not None:
        return d.replace(tzinfo=None)
    return d


def session_terminal_for_feedback(mode: str, state: str) -> bool:
    """Whether this (mode, state) should emit a durable feedback row (at most once)."""
    m = (mode or "").lower()
    st = state or ""
    if m == "paper":
        return st in (STATE_FINISHED, STATE_CANCELLED, STATE_ERROR, STATE_EXPIRED, STATE_ARCHIVED)
    if m == "live":
        return st in (STATE_LIVE_FINISHED, STATE_LIVE_CANCELLED, STATE_LIVE_ERROR, STATE_EXPIRED, STATE_ARCHIVED)
    return False


def load_recent_automation_events(db: Session, session_id: int, *, limit: int = 40) -> list[TradingAutomationEvent]:
    return (
        db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.session_id == int(session_id))
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )


def _entry_occurred_from_events(events: list[TradingAutomationEvent], mode: str) -> bool:
    if mode == "paper":
        markers = {"paper_entry_filled", "paper_exit_filled", "paper_partial_exit"}
    else:
        markers = {"live_entry_filled", "live_exit_filled", "live_exit_submitted", "live_partial_exit"}
    for ev in events:
        if ev.event_type in markers:
            return True
    return False


def _governance_context_from_events(events: list[TradingAutomationEvent]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kill_switch_exit": False,
        "kill_switch_blocked": False,
        "risk_evaluation_block": False,
        "stale_quote_abort": False,
        "policy_drift": False,
    }
    for ev in events:
        et = ev.event_type
        payload = ev.payload_json if isinstance(ev.payload_json, dict) else {}
        reason = str(payload.get("reason") or "")
        if et in ("live_blocked_by_risk", "paper_blocked_by_risk"):
            if "kill_switch" in reason or payload.get("reason") == "kill_switch":
                out["kill_switch_blocked"] = True
            if "stale" in reason:
                out["stale_quote_abort"] = True
            errs = payload.get("errors") or payload.get("risk_evaluation")
            if errs:
                out["risk_evaluation_block"] = True
        if et == "paper_policy_drift":
            out["policy_drift"] = True
        if et == "live_exit_submitted" and payload.get("reason") == "kill_switch":
            out["kill_switch_exit"] = True
        if et == "live_exit_filled" and payload.get("reason") == "kill_switch":
            out["kill_switch_exit"] = True
    return out


def extract_momentum_session_outcome(
    db: Session,
    sess: TradingAutomationSession,
    *,
    variant: Optional[MomentumStrategyVariant] = None,
    events: Optional[list[TradingAutomationEvent]] = None,
) -> dict[str, Any]:
    """Build normalized extraction dict (not yet persisted)."""
    snap = dict(sess.risk_snapshot_json or {})
    pe = snap.get(KEY_PAPER) if isinstance(snap.get(KEY_PAPER), dict) else {}
    le = snap.get(KEY_LIVE) if isinstance(snap.get(KEY_LIVE), dict) else {}

    if events is None:
        events = load_recent_automation_events(db, int(sess.id))

    mode = (sess.mode or "paper").lower()
    gov = _governance_context_from_events(events)

    var = variant
    if var is None and sess.variant_id:
        var = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == int(sess.variant_id)).one_or_none()

    family = var.family if var else None
    variant_key = var.variant_key if var else None
    version = var.version if var else None

    risk_frozen = snap.get(RISK_SNAPSHOT_KEY) if isinstance(snap.get(RISK_SNAPSHOT_KEY), dict) else {}
    admission_snapshot_json = {
        RISK_SNAPSHOT_KEY: risk_frozen,
        "momentum_risk_policy_summary": snap.get("momentum_risk_policy_summary"),
        "momentum_policy_caps": snap.get("momentum_policy_caps"),
    }

    # Regime / readiness: prefer current viability row at extract time (traceable).
    regime_snapshot_json: dict[str, Any] = {}
    readiness_snapshot_json: dict[str, Any] = {}
    viability_score_at_extract: Optional[float] = None
    paper_eligible_snap: Optional[bool] = None
    live_eligible_snap: Optional[bool] = None
    from ....models.trading import MomentumSymbolViability

    via = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == int(sess.variant_id),
        )
        .one_or_none()
    )
    entry_regime_snapshot_json: dict[str, Any] = {}
    if mode == "paper" and isinstance(pe.get("entry_regime_snapshot_json"), dict):
        entry_regime_snapshot_json = dict(pe["entry_regime_snapshot_json"])

    if via:
        regime_snapshot_json = dict(via.regime_snapshot_json or {})
        readiness_snapshot_json = dict(via.execution_readiness_json or {})
        viability_score_at_extract = float(via.viability_score)
        paper_eligible_snap = bool(via.paper_eligible)
        live_eligible_snap = bool(via.live_eligible)

    admission_viability = risk_frozen.get("viability_at_admission") if isinstance(risk_frozen, dict) else None
    if isinstance(admission_viability, dict):
        admission_snapshot_json["viability_at_admission"] = admission_viability

    terminal_at = _utc(sess.ended_at) or _utc(sess.updated_at) or datetime.utcnow()
    started = _utc(sess.started_at)

    realized: Optional[float] = None
    exit_reason: Optional[str] = None
    partial_exit = False
    entry_occurred = False

    if mode == "paper":
        realized = pe.get("realized_pnl_usd")
        if realized is not None:
            try:
                realized = float(realized)
            except (TypeError, ValueError):
                realized = None
        exit_reason = pe.get("last_exit_reason")
        if isinstance(exit_reason, str):
            exit_reason = exit_reason.strip() or None
        pos = pe.get("position")
        entry_occurred = _entry_occurred_from_events(events, "paper") or (
            isinstance(pos, dict) and float(pos.get("quantity") or 0) > 0
        )
        for ev in events:
            if ev.event_type == "paper_partial_exit":
                partial_exit = True
    else:
        realized = le.get("realized_pnl_usd")
        if realized is not None:
            try:
                realized = float(realized)
            except (TypeError, ValueError):
                realized = None
        exit_reason = le.get("last_exit_reason")
        if isinstance(exit_reason, str):
            exit_reason = exit_reason.strip() or None
        pos = le.get("position")
        entry_occurred = _entry_occurred_from_events(events, "live") or (
            isinstance(pos, dict) and float(pos.get("quantity") or 0) > 0
        )
        for ev in events:
            if ev.event_type == "live_partial_exit":
                partial_exit = True

    hold_seconds: Optional[int] = None
    if started and terminal_at:
        hold_seconds = max(0, int((terminal_at - started).total_seconds()))

    notional_basis = 0.0
    if mode == "paper" and isinstance(pe.get("position"), dict):
        try:
            notional_basis = float(pe["position"].get("notional_usd") or 0)
        except (TypeError, ValueError):
            notional_basis = 0.0
    elif mode == "live" and isinstance(le.get("position"), dict):
        try:
            notional_basis = float(le["position"].get("notional_usd") or 0)
        except (TypeError, ValueError):
            notional_basis = 0.0
    if notional_basis <= 0 and mode == "paper" and isinstance(pe.get("position"), dict):
        posp = pe["position"]
        try:
            ep = float(posp.get("entry_price") or 0)
            q = float(posp.get("quantity") or 0)
            if ep > 0 and q > 0:
                notional_basis = abs(ep * q)
        except (TypeError, ValueError):
            pass

    return_bps: Optional[float] = None
    if realized is not None and notional_basis > 1e-9:
        return_bps = (realized / notional_basis) * 10000.0

    outcome_class = derive_outcome_class(
        mode=mode,
        terminal_state=sess.state,
        entry_occurred=entry_occurred,
        partial_exit=partial_exit,
        realized_pnl_usd=realized,
        return_bps=return_bps,
        exit_reason=exit_reason,
        governance_context=gov,
        events=events,
    )

    extracted = {
        "session_id": int(sess.id),
        "user_id": int(sess.user_id) if sess.user_id is not None else None,
        "variant_id": int(sess.variant_id),
        "variant_family": family,
        "variant_key": variant_key,
        "variant_version": version,
        "symbol": sess.symbol,
        "mode": mode,
        "execution_family": sess.execution_family or "coinbase_spot",
        "terminal_state": sess.state,
        "terminal_at_utc": terminal_at.isoformat(),
        "started_at_utc": started.isoformat() if started else None,
        "hold_seconds": hold_seconds,
        "outcome_class": outcome_class,
        "realized_pnl_usd": realized,
        "return_bps": return_bps,
        "exit_reason": exit_reason,
        "entry_occurred": entry_occurred,
        "partial_exit_occurred": partial_exit,
        "regime_snapshot_json": regime_snapshot_json,
        "entry_regime_snapshot_json": entry_regime_snapshot_json,
        "exit_regime_snapshot_json": dict(regime_snapshot_json),
        "readiness_snapshot_json": readiness_snapshot_json,
        "admission_snapshot_json": admission_snapshot_json,
        "governance_context_json": gov,
        "viability_score_at_extract": viability_score_at_extract,
        "paper_eligible_snapshot": paper_eligible_snap,
        "live_eligible_snapshot": live_eligible_snap,
        "correlation_id": sess.correlation_id,
        "source_node_id": sess.source_node_id,
    }
    return extracted


def derive_outcome_class(
    *,
    mode: str,
    terminal_state: str,
    entry_occurred: bool,
    partial_exit: bool,
    realized_pnl_usd: Optional[float],
    return_bps: Optional[float],
    exit_reason: Optional[str],
    governance_context: dict[str, Any],
    events: list[TradingAutomationEvent],
) -> str:
    """Deterministic label from terminal state + execution hints + recent events."""
    st = terminal_state or ""
    m = (mode or "paper").lower()

    if st == STATE_EXPIRED:
        return OUTCOME_EXPIRED_PRE_RUN
    if st == STATE_ARCHIVED:
        return OUTCOME_ARCHIVED
    if st in (STATE_CANCELLED, STATE_LIVE_CANCELLED):
        if entry_occurred or partial_exit:
            return OUTCOME_CANCELLED_IN_TRADE
        return OUTCOME_CANCELLED_PRE_ENTRY

    if st in (STATE_ERROR, STATE_LIVE_ERROR):
        for ev in events:
            if ev.event_type == "live_error" and isinstance(ev.payload_json, dict):
                if ev.payload_json.get("reason") == "zero_fill":
                    return OUTCOME_NO_FILL
            if ev.event_type == "paper_error" and isinstance(ev.payload_json, dict):
                r = str(ev.payload_json.get("reason") or "")
                if "missing_frozen" in r or "risk" in r.lower():
                    return OUTCOME_RISK_BLOCK
        if governance_context.get("stale_quote_abort"):
            return OUTCOME_STALE_DATA_ABORT
        if governance_context.get("kill_switch_blocked"):
            return OUTCOME_GOVERNANCE_EXIT
        if governance_context.get("risk_evaluation_block"):
            return OUTCOME_RISK_BLOCK
        return OUTCOME_ERROR_EXIT

    if st in (STATE_FINISHED, STATE_LIVE_FINISHED):
        if governance_context.get("kill_switch_exit"):
            return OUTCOME_GOVERNANCE_EXIT
        er = (exit_reason or "").lower()
        if "stop" in er or er == "stop":
            return OUTCOME_STOP_LOSS
        if "bailout" in er or er == "bailout":
            return OUTCOME_BAILOUT
        if "max_hold" in er or "timed" in er or er == "max_hold":
            return OUTCOME_TIMED_EXIT
        rb = return_bps
        rp = realized_pnl_usd
        if rb is not None:
            if rb >= 25.0:
                return OUTCOME_SUCCESS
            if rb > 0:
                return OUTCOME_SMALL_WIN
            if rb <= -25.0:
                return OUTCOME_STOP_LOSS
        if rp is not None:
            if rp > 0:
                return OUTCOME_SMALL_WIN
            if rp < 0:
                return OUTCOME_STOP_LOSS
        if entry_occurred:
            return OUTCOME_FLAT_UNKNOWN
        return OUTCOME_CANCELLED_PRE_ENTRY

    return OUTCOME_FLAT_UNKNOWN


def outcome_row_from_extracted(
    extracted: dict[str, Any],
    *,
    evidence_weight: float = 1.0,
    contributes_to_evolution: bool = True,
) -> MomentumAutomationOutcome:
    terminal_at = datetime.utcnow()
    try:
        terminal_at = datetime.fromisoformat(str(extracted["terminal_at_utc"]).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except Exception:
        pass

    summary = {k: extracted.get(k) for k in ("entry_occurred", "partial_exit_occurred", "variant_family", "variant_key")}

    return MomentumAutomationOutcome(
        session_id=int(extracted["session_id"]),
        user_id=extracted.get("user_id"),
        variant_id=int(extracted["variant_id"]),
        symbol=str(extracted["symbol"]),
        mode=str(extracted["mode"]),
        execution_family=str(extracted.get("execution_family") or "coinbase_spot"),
        terminal_state=str(extracted["terminal_state"]),
        terminal_at=terminal_at,
        outcome_class=str(extracted["outcome_class"]),
        realized_pnl_usd=extracted.get("realized_pnl_usd"),
        return_bps=extracted.get("return_bps"),
        hold_seconds=extracted.get("hold_seconds"),
        exit_reason=extracted.get("exit_reason"),
        regime_snapshot_json=dict(extracted.get("regime_snapshot_json") or {}),
        entry_regime_snapshot_json=dict(extracted.get("entry_regime_snapshot_json") or {}),
        exit_regime_snapshot_json=dict(extracted.get("exit_regime_snapshot_json") or {}),
        readiness_snapshot_json=dict(extracted.get("readiness_snapshot_json") or {}),
        admission_snapshot_json=dict(extracted.get("admission_snapshot_json") or {}),
        governance_context_json=dict(extracted.get("governance_context_json") or {}),
        extracted_summary_json=summary,
        evidence_weight=float(evidence_weight),
        contributes_to_evolution=bool(contributes_to_evolution),
    )


def feedback_row_exists(db: Session, session_id: int) -> bool:
    return (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(session_id))
        .one_or_none()
        is not None
    )
