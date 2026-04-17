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
from .execution_attribution import trade_close_attribution_dict
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


def _phase_a_economic_ledger_live_shadow(
    db: Session, trade: Trade, *, ledger_source: str
) -> None:
    """Phase A shadow hook: record entry + exit fills against the canonical
    economic ledger and reconcile against ``trade.pnl``.

    Shadow-only — does not modify the legacy Trade row. Swallows all errors
    so the execution feedback path never breaks on ledger bugs.
    """
    try:
        from ....services.trading import economic_ledger as _ledger
        if not _ledger.mode_is_active():
            return
    except Exception:
        return

    try:
        ticker = (trade.ticker or "").strip().upper()
        if not ticker:
            return
        entry_qty = float(
            getattr(trade, "filled_quantity", None)
            or getattr(trade, "quantity", None)
            or 0.0
        )
        entry_price = float(
            getattr(trade, "avg_fill_price", None)
            or getattr(trade, "entry_price", None)
            or 0.0
        )
        if entry_qty > 0 and entry_price > 0:
            _ledger.record_entry_fill(
                db,
                source="live",
                trade_id=int(trade.id),
                user_id=trade.user_id,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                ticker=ticker,
                direction=(trade.direction or "long"),
                quantity=entry_qty,
                fill_price=entry_price,
                fee=0.0,
                broker_source=getattr(trade, "broker_source", None),
                event_ts=getattr(trade, "filled_at", None) or getattr(trade, "entry_date", None),
                provenance={"legacy_path": f"on_{ledger_source}", "lazy_emit": True},
            )

        exit_qty = float(
            getattr(trade, "filled_quantity", None)
            or getattr(trade, "quantity", None)
            or 0.0
        )
        exit_price = float(getattr(trade, "exit_price", None) or 0.0)
        if exit_qty > 0 and exit_price > 0 and entry_price > 0:
            _ledger.record_exit_fill(
                db,
                source="live",
                trade_id=int(trade.id),
                user_id=trade.user_id,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                ticker=ticker,
                direction=(trade.direction or "long"),
                quantity=exit_qty,
                fill_price=exit_price,
                entry_price=entry_price,
                fee=0.0,
                broker_source=getattr(trade, "broker_source", None),
                event_ts=getattr(trade, "exit_date", None),
                provenance={"legacy_path": f"on_{ledger_source}", "exit_reason": getattr(trade, "exit_reason", None)},
            )

        legacy_pnl = getattr(trade, "pnl", None)
        if legacy_pnl is not None:
            _ledger.reconcile_trade(
                db,
                source="live",
                trade_id=int(trade.id),
                user_id=trade.user_id,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                ticker=ticker,
                legacy_pnl=float(legacy_pnl),
                provenance={"legacy_path": f"on_{ledger_source}"},
            )
    except Exception:
        logger.debug("[execution_hooks] economic_ledger live hook failed", exc_info=True)


def on_live_trade_closed(
    db: Session,
    trade: Trade,
    *,
    source: str,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Portfolio or operator-initiated close of a live ``Trade`` row (before commit)."""
    try:
        merged = {**trade_close_attribution_dict(trade), **(extra or {})}
        emit_live_trade_closed_outcome(
            db,
            trade_id=int(trade.id),
            user_id=trade.user_id,
            ticker=(trade.ticker or "").strip(),
            source=source,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            extra=merged,
        )
        _phase_a_economic_ledger_live_shadow(db, trade, ledger_source="live_trade_closed")
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
        att = trade_close_attribution_dict(trade)
        emit_broker_fill_closed_outcome(
            db,
            trade_id=int(trade.id),
            user_id=trade.user_id,
            ticker=(trade.ticker or "").strip(),
            broker_source=(getattr(trade, "broker_source", None) or "") or "unknown",
            source=source,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            extra=att,
        )
        _phase_a_economic_ledger_live_shadow(db, trade, ledger_source="broker_reconciled_close")
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
