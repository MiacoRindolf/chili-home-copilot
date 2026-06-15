"""Idempotent emission of durable momentum automation outcomes (Phase 9)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import LedgerParityLog, MomentumAutomationOutcome, TradingAutomationSession, TradingDecisionPacket
from ..decision_ledger import finalize_packet_from_automation_outcome, verify_decision_packet_snapshot
from ..economic_ledger import automation_trade_id, mode_is_active as economic_ledger_active
from .evolution import compute_session_evidence_weight, ingest_session_outcome, outcome_needs_evolution_ingest
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


def _computed_existing_row_credit(
    db: Session,
    row: MomentumAutomationOutcome,
) -> tuple[dict[str, Any], dict[str, Any]]:
    summary = dict(row.extracted_summary_json or {})
    existing_credit = summary.get("evolution_credit") if isinstance(summary.get("evolution_credit"), dict) else {}
    packet_id = summary.get("entry_decision_packet_id")
    if packet_id is None:
        packet_id = existing_credit.get("entry_decision_packet_id")
    extracted = {
        "entry_occurred": summary.get("entry_occurred"),
        "entry_decision_packet_id": packet_id,
        "outcome_class": row.outcome_class,
        "mode": row.mode,
        "quote_source_at_entry": summary.get("quote_source_at_entry") or existing_credit.get("quote_source_at_entry"),
        "return_bps": row.return_bps,
        "realized_pnl_usd": row.realized_pnl_usd,
    }
    credit = _apply_decision_snapshot_credit_gate(db, outcome_evolution_credit_from_extracted(extracted))
    credit = _apply_economic_ledger_credit_gate(db, session_id=int(row.session_id), credit=credit)
    summary["evolution_credit"] = credit
    return summary, credit


def _recompute_existing_row_credit(db: Session, row: MomentumAutomationOutcome) -> dict[str, Any]:
    summary, credit = _computed_existing_row_credit(db, row)
    row.extracted_summary_json = summary
    row.contributes_to_evolution = bool(credit.get("contributes_to_evolution"))
    return credit


def _credit_reason_codes(credit: Any) -> list[str]:
    if not isinstance(credit, dict):
        return []
    return sorted({str(code) for code in (credit.get("reason_codes") or []) if str(code).strip()})


def _credit_payload(summary: Any) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    credit = summary.get("evolution_credit")
    return dict(credit) if isinstance(credit, dict) else {}


def _credit_regrade_item(
    row: MomentumAutomationOutcome,
    *,
    old_credit: dict[str, Any],
    new_credit: dict[str, Any],
    old_contributes: bool,
) -> dict[str, Any]:
    terminal_at = row.terminal_at.isoformat() + "Z" if isinstance(row.terminal_at, datetime) else None
    created_at = row.created_at.isoformat() + "Z" if isinstance(row.created_at, datetime) else None
    return {
        "outcome_id": int(row.id),
        "session_id": int(row.session_id),
        "user_id": int(row.user_id) if row.user_id is not None else None,
        "variant_id": int(row.variant_id) if row.variant_id is not None else None,
        "symbol": row.symbol,
        "mode": row.mode,
        "execution_family": row.execution_family,
        "terminal_at": terminal_at,
        "created_at": created_at,
        "entry_decision_packet_id": new_credit.get("entry_decision_packet_id"),
        "old_contributes_to_evolution": bool(old_contributes),
        "new_contributes_to_evolution": bool(new_credit.get("contributes_to_evolution")),
        "old_reason_codes": _credit_reason_codes(old_credit),
        "new_reason_codes": _credit_reason_codes(new_credit),
    }


def _stamp_credit_regrade(
    summary: dict[str, Any],
    *,
    item: dict[str, Any],
    requires_reingest: bool,
) -> dict[str, Any]:
    out = dict(summary or {})
    out["evolution_credit_regrade_v1"] = {
        "regraded_at_utc": datetime.utcnow().isoformat() + "Z",
        "old_contributes_to_evolution": bool(item.get("old_contributes_to_evolution")),
        "new_contributes_to_evolution": bool(item.get("new_contributes_to_evolution")),
        "old_reason_codes": list(item.get("old_reason_codes") or []),
        "new_reason_codes": list(item.get("new_reason_codes") or []),
        "requires_reingest": bool(requires_reingest),
        "reingested_at_utc": None,
    }
    return out


def regrade_momentum_outcome_evolution_credit(
    db: Session,
    *,
    days: int = 30,
    user_id: int | None = None,
    limit: int = 500,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Recompute existing momentum outcome evolution credit after lineage repairs.

    This intentionally does not ingest/re-ingest evolution weights. It only
    repairs the audit row's eligibility flag and credit explanation, so old
    rows can be promoted to training-grade after packet snapshots or ledger
    parity are repaired without silently double-counting learning state.
    """
    if not settings.chili_momentum_neural_feedback_enabled:
        return {"ok": True, "skipped": "feedback_disabled", "processed": 0}
    if not _outcomes_table_present(db):
        return {"ok": True, "skipped": "outcomes_table_missing", "processed": 0}

    window_days = max(1, min(int(days or 30), 365))
    limit_i = max(0, min(int(limit or 0), 10000))
    since = datetime.utcnow() - timedelta(days=window_days)
    query = db.query(MomentumAutomationOutcome).filter(MomentumAutomationOutcome.terminal_at >= since)
    if user_id is not None:
        query = query.filter(MomentumAutomationOutcome.user_id == int(user_id))
    rows = query.order_by(MomentumAutomationOutcome.created_at.desc()).limit(limit_i).all()

    candidates: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    upgraded = 0
    downgraded = 0
    needs_reingest = 0
    for row in rows:
        old_summary = dict(row.extracted_summary_json or {})
        old_credit = _credit_payload(old_summary)
        old_contributes = bool(row.contributes_to_evolution)
        new_summary, new_credit = _computed_existing_row_credit(db, row)
        new_contributes = bool(new_credit.get("contributes_to_evolution"))
        old_reasons = _credit_reason_codes(old_credit)
        new_reasons = _credit_reason_codes(new_credit)
        payload_missing = not isinstance(old_summary.get("evolution_credit"), dict)
        changed = payload_missing or old_contributes != new_contributes or old_reasons != new_reasons
        if not changed:
            continue

        item = _credit_regrade_item(
            row,
            old_credit=old_credit,
            new_credit=new_credit,
            old_contributes=old_contributes,
        )
        candidates.append(item)
        if old_contributes is False and new_contributes is True:
            upgraded += 1
            needs_reingest += 1
        elif old_contributes is True and new_contributes is False:
            downgraded += 1

        if not dry_run:
            new_summary = _stamp_credit_regrade(
                new_summary,
                item=item,
                requires_reingest=old_contributes is False and new_contributes is True,
            )
            row.extracted_summary_json = new_summary
            row.contributes_to_evolution = new_contributes
            applied.append(item)

    if not dry_run:
        try:
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise

    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "mode": "dry_run" if dry_run else "apply",
        "window_days": window_days,
        "limit": limit_i,
        "user_id": int(user_id) if user_id is not None else None,
        "processed": len(rows),
        "candidate_count": len(candidates),
        "applied_count": len(applied),
        "upgraded_to_training_grade": upgraded,
        "downgraded_to_audit_only": downgraded,
        "reingest_required_count": needs_reingest,
        "reingest_note": "Credit was repaired only; run explicit reingest if you want neural weights updated.",
        "candidates": candidates if dry_run else [],
        "applied": applied if not dry_run else [],
    }


def _reingest_regrade_marker(row: MomentumAutomationOutcome) -> dict[str, Any]:
    summary = row.extracted_summary_json if isinstance(row.extracted_summary_json, dict) else {}
    marker = summary.get("evolution_credit_regrade_v1")
    return dict(marker) if isinstance(marker, dict) else {}


def _reingest_candidate_item(row: MomentumAutomationOutcome) -> dict[str, Any]:
    marker = _reingest_regrade_marker(row)
    terminal_at = row.terminal_at.isoformat() + "Z" if isinstance(row.terminal_at, datetime) else None
    return {
        "outcome_id": int(row.id),
        "session_id": int(row.session_id),
        "user_id": int(row.user_id) if row.user_id is not None else None,
        "variant_id": int(row.variant_id) if row.variant_id is not None else None,
        "symbol": row.symbol,
        "mode": row.mode,
        "execution_family": row.execution_family,
        "terminal_at": terminal_at,
        "regraded_at_utc": marker.get("regraded_at_utc"),
    }


def reingest_regraded_momentum_outcomes(
    db: Session,
    *,
    days: int = 30,
    user_id: int | None = None,
    limit: int = 100,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Explicitly apply learning once for rows upgraded by credit regrade."""
    if not settings.chili_momentum_neural_feedback_enabled:
        return {"ok": True, "skipped": "feedback_disabled", "processed": 0}
    if not _outcomes_table_present(db):
        return {"ok": True, "skipped": "outcomes_table_missing", "processed": 0}

    window_days = max(1, min(int(days or 30), 365))
    limit_i = max(0, min(int(limit or 0), 1000))
    since = datetime.utcnow() - timedelta(days=window_days)
    query = db.query(MomentumAutomationOutcome).filter(
        MomentumAutomationOutcome.terminal_at >= since,
        MomentumAutomationOutcome.contributes_to_evolution.is_(True),
    )
    if user_id is not None:
        query = query.filter(MomentumAutomationOutcome.user_id == int(user_id))
    rows = query.order_by(MomentumAutomationOutcome.created_at.desc()).limit(limit_i * 5 if limit_i else 0).all()

    candidates: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    for row in rows:
        marker = _reingest_regrade_marker(row)
        if not bool(marker.get("requires_reingest")):
            continue
        if marker.get("reingested_at_utc"):
            continue
        if not outcome_needs_evolution_ingest(row):
            continue
        item = _reingest_candidate_item(row)
        candidates.append(item)
        if not dry_run:
            result = ingest_session_outcome(db, row, source="evolution_credit_regrade")
            summary = dict(row.extracted_summary_json or {})
            updated_marker = dict(summary.get("evolution_credit_regrade_v1") or {})
            updated_marker["reingested_at_utc"] = datetime.utcnow().isoformat() + "Z"
            updated_marker["reingest_result"] = result
            summary["evolution_credit_regrade_v1"] = updated_marker
            row.extracted_summary_json = summary
            applied.append({**item, "reingest_result": result})
        if len(candidates) >= limit_i:
            break

    if not dry_run:
        try:
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise

    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "mode": "dry_run" if dry_run else "apply",
        "window_days": window_days,
        "limit": limit_i,
        "user_id": int(user_id) if user_id is not None else None,
        "processed": len(rows),
        "candidate_count": len(candidates),
        "applied_count": len(applied),
        "candidates": candidates if dry_run else [],
        "applied": applied if not dry_run else [],
    }


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
            ingest_session_outcome(db, row, force=True, source="feedback_emit_force_reingest")
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
        ingest_session_outcome(db, row, source="feedback_emit")
    except Exception as ex:
        _log.warning("[momentum_feedback] evolution ingest failed session_id=%s: %s", sess.id, ex)

    # After a momentum session terminates with realized PnL, re-check the
    # daily-loss cap. Per-broker: blocks only the breached broker (the aggregate
    # backstop still trips the true global kill switch); legacy: the single global
    # check, now sized off the session's broker equity (not the None->Coinbase default).
    try:
        from ...config import settings as _s

        if bool(getattr(_s, "chili_per_broker_daily_loss_enabled", True)):
            from ..governance import check_per_broker_daily_loss

            check_per_broker_daily_loss(db, user_id=sess.user_id)
        else:
            from ..governance import check_daily_loss_breach
            from .risk_policy import _account_equity_usd

            _ef = getattr(sess, "execution_family", None)
            check_daily_loss_breach(
                db,
                user_id=sess.user_id,
                equity_usd=_account_equity_usd(_ef, prefer_real_equity=True),
            )
    except Exception as ex:
        _log.debug("[momentum_feedback] daily-loss check skipped: %s", ex)

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
