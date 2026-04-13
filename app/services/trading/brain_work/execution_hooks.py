"""Authoritative ledger hooks for execution feedback (paper / live / broker)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....models.trading import PaperTrade, Trade
from ....config import settings
from .emitters import (
    emit_broker_fill_closed_outcome,
    emit_live_trade_closed_outcome,
    emit_paper_trade_closed_outcome,
)
from .ledger import enqueue_or_refresh_debounced_work

logger = logging.getLogger(__name__)


def _exec_feedback_debounce_s() -> int:
    return int(getattr(settings, "brain_work_exec_feedback_debounce_seconds", 45))


def on_paper_trade_closed(db: Session, pt: PaperTrade) -> None:
    """Call in the same transaction as the paper close (before commit)."""
    try:
        emit_paper_trade_closed_outcome(
            db,
            paper_trade_id=int(pt.id),
            user_id=pt.user_id,
            scan_pattern_id=pt.scan_pattern_id,
            ticker=(pt.ticker or "").strip(),
            pnl=pt.pnl,
            exit_reason=(pt.exit_reason or "").strip(),
        )
        uid = pt.user_id
        if uid is not None:
            enqueue_or_refresh_debounced_work(
                db,
                event_type="execution_feedback_digest",
                dedupe_key=f"exec_fb_digest:user:{int(uid)}",
                payload={"user_id": int(uid), "trigger": "paper_trade_closed"},
                debounce_seconds=_exec_feedback_debounce_s(),
                lease_scope="execution_feedback",
            )
    except Exception:
        logger.debug("[execution_hooks] on_paper_trade_closed failed", exc_info=True)


def on_live_trade_closed(
    db: Session,
    trade: Trade,
    *,
    source: str,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Portfolio or operator-initiated close of a live ``Trade`` row (before commit)."""
    try:
        emit_live_trade_closed_outcome(
            db,
            trade_id=int(trade.id),
            user_id=trade.user_id,
            ticker=(trade.ticker or "").strip(),
            source=source,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            extra=extra,
        )
        uid = trade.user_id
        if uid is not None:
            enqueue_or_refresh_debounced_work(
                db,
                event_type="execution_feedback_digest",
                dedupe_key=f"exec_fb_digest:user:{int(uid)}",
                payload={"user_id": int(uid), "trigger": "live_trade_closed", "source": source},
                debounce_seconds=_exec_feedback_debounce_s(),
                lease_scope="execution_feedback",
            )
    except Exception:
        logger.debug("[execution_hooks] on_live_trade_closed failed", exc_info=True)


def on_broker_reconciled_close(
    db: Session,
    trade: Trade,
    *,
    source: str,
) -> None:
    """Broker sync inferred close (position vanished, manual cleanup during RH sync, etc.)."""
    try:
        emit_broker_fill_closed_outcome(
            db,
            trade_id=int(trade.id),
            user_id=trade.user_id,
            ticker=(trade.ticker or "").strip(),
            broker_source=(getattr(trade, "broker_source", None) or "") or "unknown",
            source=source,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
        )
        uid = trade.user_id
        if uid is not None:
            enqueue_or_refresh_debounced_work(
                db,
                event_type="execution_feedback_digest",
                dedupe_key=f"exec_fb_digest:user:{int(uid)}",
                payload={"user_id": int(uid), "trigger": "broker_fill_closed", "source": source},
                debounce_seconds=_exec_feedback_debounce_s(),
                lease_scope="execution_feedback",
            )
    except Exception:
        logger.debug("[execution_hooks] on_broker_reconciled_close failed", exc_info=True)
