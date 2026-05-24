"""Idempotent emission of durable momentum automation outcomes (Phase 9)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import LedgerParityLog, MomentumAutomationOutcome, TradingAutomationSession, TradingDecisionPacket
from ..decision_ledger import finalize_packet_from_automation_outcome, verify_decision_packet_snapshot
from ..economic_ledger import automation_trade_id, mode_is_active as economic_ledger_active
from .evolution import compute_session_evidence_weight, ingest_session_outcome
from .outcome_extract import (
    extract_momentum_session_outcome,
    feedback_row_exists,
    outcome_evolution_credit_from_extracted,
    outcome_row_from_extracted,
    session_terminal_for_feedback,
)

_log = logging.getLogger(__name__)


def _outcomes_table_present(db: Session) -> bool:
    try:
        names = set(sa_inspect(db.bind).get_table_names())
    except Exception:
        return False
    return "momentum_automation_outcomes" in names


def _apply_decision_snapshot_credit_gate(db: Session, credit: dict[str, Any]) -> dict[str, Any]:
    out = dict(credit or {})
    packet_id = out.get("entry_decision_packet_id")
    try:
        packet_id = int(packet_id) if packet_id is not None else None
    except (TypeError, ValueError):
        packet_id = None
    if packet_id is None:
        return out

    packet = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(packet_id)).one_or_none()
    if packet is None:
        reasons = list(out.get("reason_codes") or [])
        reasons.append("entry_decision_packet_missing")
        out["reason_codes"] = sorted(set(reasons))
        out["contributes_to_evolution"] = False
        out["decision_snapshot_verification"] = {"ok": False, "reason": "packet_missing"}
        return out

    verification = verify_decision_packet_snapshot(packet)
    out["decision_snapshot_verification"] = verification
    if not verification.get("ok"):
        reasons = list(out.get("reason_codes") or [])
        reasons.append("decision_snapshot_invalid")
        out["reason_codes"] = sorted(set(reasons))
        out["contributes_to_evolution"] = False
    return out


def _apply_economic_ledger_credit_gate(
    db: Session,
    *,
    session_id: int,
    credit: dict[str, Any],
) -> dict[str, Any]:
    out = dict(credit or {})
    if not economic_ledger_active():
        return out
    parity = (
        db.query(LedgerParityLog)
        .filter(
            LedgerParityLog.source == "automation",
            LedgerParityLog.trade_id == automation_trade_id(int(session_id)),
        )
        .order_by(LedgerParityLog.created_at.desc())
        .first()
    )
    if parity is None:
        out["economic_ledger_verification"] = {"ok": None, "reason": "parity_missing"}
        if bool(getattr(settings, "brain_economic_ledger_require_parity_for_evolution", True)):
            reasons = list(out.get("reason_codes") or [])
            reasons.append("economic_ledger_parity_missing")
            out["reason_codes"] = sorted(set(reasons))
            out["contributes_to_evolution"] = False
        return out
    verification = {
        "ok": bool(parity.agree_bool),
        "parity_log_id": int(parity.id),
        "legacy_pnl": parity.legacy_pnl,
        "ledger_pnl": parity.ledger_pnl,
        "delta_abs": parity.delta_abs,
        "tolerance_usd": parity.tolerance_usd,
        "mode": parity.mode,
    }
    out["economic_ledger_verification"] = verification
    if not parity.agree_bool:
        reasons = list(out.get("reason_codes") or [])
        reasons.append("economic_ledger_parity_mismatch")
        out["reason_codes"] = sorted(set(reasons))
        out["contributes_to_evolution"] = False
    return out


def _recompute_existing_row_credit(db: Session, row: MomentumAutomationOutcome) -> dict[str, Any]:
    summary = dict(row.extracted_summary_json or {})
    extracted = {
        "entry_occurred": summary.get("entry_occurred"),
        "entry_decision_packet_id": summary.get("entry_decision_packet_id"),
        "outcome_class": row.outcome_class,
        "mode": row.mode,
        "quote_source_at_entry": summary.get("quote_source_at_entry"),
        "return_bps": row.return_bps,
        "realized_pnl_usd": row.realized_pnl_usd,
    }
    credit = _apply_decision_snapshot_credit_gate(db, outcome_evolution_credit_from_extracted(extracted))
    credit = _apply_economic_ledger_credit_gate(db, session_id=int(row.session_id), credit=credit)
    summary["evolution_credit"] = credit
    row.extracted_summary_json = summary
    row.contributes_to_evolution = bool(credit.get("contributes_to_evolution"))
    return credit


def try_emit_momentum_session_feedback(
    db: Session,
    sess: TradingAutomationSession,
    *,
    force_reingest_evolution: bool = False,
) -> dict[str, Any]:
    """
    If session is in a feedback terminal state, persist one MomentumAutomationOutcome row (deduped by session_id)
    and ingest into neural evolution. Safe to call repeatedly.
    """
    if not settings.chili_momentum_neural_feedback_enabled:
        return {"ok": True, "skipped": "feedback_disabled"}

    if not _outcomes_table_present(db):
        return {"ok": True, "skipped": "outcomes_table_missing"}

    if not session_terminal_for_feedback(sess.mode or "paper", sess.state):
        return {"ok": True, "skipped": "not_terminal_for_feedback", "state": sess.state}

    if feedback_row_exists(db, int(sess.id)) and not force_reingest_evolution:
        return {"ok": True, "deduped": True, "session_id": int(sess.id)}

    if feedback_row_exists(db, int(sess.id)) and force_reingest_evolution:
        row = (
            db.query(MomentumAutomationOutcome)
            .filter(MomentumAutomationOutcome.session_id == int(sess.id))
            .one_or_none()
        )
        if row:
            credit = _recompute_existing_row_credit(db, row)
            finalize_packet_from_automation_outcome(db, row)
            ingest_session_outcome(db, row)
            return {"ok": True, "reingested": True, "session_id": int(sess.id), "evolution_credit": credit}

    extracted = extract_momentum_session_outcome(db, sess)
    w = compute_session_evidence_weight(db, extracted)
    credit = _apply_decision_snapshot_credit_gate(db, outcome_evolution_credit_from_extracted(extracted))
    credit = _apply_economic_ledger_credit_gate(db, session_id=int(sess.id), credit=credit)
    row = outcome_row_from_extracted(
        extracted,
        evidence_weight=w,
        contributes_to_evolution=credit["contributes_to_evolution"],
    )
    summary = dict(row.extracted_summary_json or {})
    summary["evolution_credit"] = credit
    row.extracted_summary_json = summary
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
            finalize_packet_from_automation_outcome(db, row)
    except IntegrityError:
        return {"ok": True, "deduped": True, "session_id": int(sess.id)}
    except Exception as ex:
        _log.warning("[momentum_feedback] insert failed session_id=%s: %s", sess.id, ex)
        return {"ok": False, "error": "insert_failed", "detail": str(ex)}

    try:
        ingest_session_outcome(db, row)
    except Exception as ex:
        _log.warning("[momentum_feedback] evolution ingest failed session_id=%s: %s", sess.id, ex)

    # P0.2 — after a momentum session terminates with realized PnL, re-check
    # the global daily-loss cap so a mixed-path drawdown (autotrader + momentum)
    # can trip the kill switch. No-ops if already active or no limits configured.
    try:
        from ..governance import check_daily_loss_breach
        check_daily_loss_breach(db, user_id=sess.user_id)
    except Exception as ex:
        _log.debug("[momentum_feedback] global daily-loss check skipped: %s", ex)

    return {
        "ok": True,
        "emitted": True,
        "session_id": int(sess.id),
        "outcome_class": row.outcome_class,
        "contributes_to_evolution": bool(row.contributes_to_evolution),
        "evolution_credit": credit,
    }


def emit_feedback_after_terminal_transition(db: Session, sess: TradingAutomationSession) -> None:
    """Call from runners / monitor after mutating session into a terminal feedback state."""
    try:
        try_emit_momentum_session_feedback(db, sess)
    except Exception as ex:
        _log.debug("[momentum_feedback] emit_after_terminal skipped: %s", ex)


def scan_terminal_sessions_missing_feedback(db: Session, *, limit: int = 50) -> dict[str, Any]:
    """Backfill: terminal sessions without a feedback row (idempotent)."""
    if not settings.chili_momentum_neural_feedback_enabled:
        return {"ok": True, "skipped": "feedback_disabled", "processed": 0}
    if not _outcomes_table_present(db):
        return {"ok": True, "skipped": "outcomes_table_missing", "processed": 0}

    lim = max(1, min(int(limit), 500))
    terminal_states = (
        "finished",
        "cancelled",
        "error",
        "expired",
        "archived",
        "live_finished",
        "live_cancelled",
        "live_error",
    )
    rows = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.state.in_(terminal_states))
        .order_by(TradingAutomationSession.updated_at.asc())
        .limit(lim * 3)
        .all()
    )
    processed = 0
    emitted = 0
    for sess in rows:
        if not session_terminal_for_feedback(sess.mode or "paper", sess.state):
            continue
        if feedback_row_exists(db, int(sess.id)):
            continue
        processed += 1
        r = try_emit_momentum_session_feedback(db, sess)
        if r.get("emitted"):
            emitted += 1
        if processed >= lim:
            break
    return {"ok": True, "processed": processed, "emitted": emitted}
