"""Idempotent emission of durable momentum automation outcomes (Phase 9)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumAutomationOutcome, TradingAutomationSession
from .evolution import compute_session_evidence_weight, ingest_session_outcome
from .outcome_extract import (
    extract_momentum_session_outcome,
    feedback_row_exists,
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
            ingest_session_outcome(db, row)
            return {"ok": True, "reingested": True, "session_id": int(sess.id)}

    extracted = extract_momentum_session_outcome(db, sess)
    w = compute_session_evidence_weight(db, extracted)
    row = outcome_row_from_extracted(
        extracted,
        evidence_weight=w,
        contributes_to_evolution=True,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return {"ok": True, "deduped": True, "session_id": int(sess.id)}
    except Exception as ex:
        _log.warning("[momentum_feedback] insert failed session_id=%s: %s", sess.id, ex)
        return {"ok": False, "error": "insert_failed", "detail": str(ex)}

    try:
        ingest_session_outcome(db, row)
    except Exception as ex:
        _log.warning("[momentum_feedback] evolution ingest failed session_id=%s: %s", sess.id, ex)

    return {"ok": True, "emitted": True, "session_id": int(sess.id), "outcome_class": row.outcome_class}


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
