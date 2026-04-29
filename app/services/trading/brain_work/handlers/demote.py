"""Demote handler — reacts to trade-close events that erode pattern evidence.

Replaces the live-pattern depromotion sweep that runs inside
``run_learning_cycle`` (via ``run_live_pattern_depromotion``). When a closed
trade is logged for a promoted pattern, this handler re-checks the realized
EV gate and demotes the pattern if it now fails.

Event triggers (subscribes to all three):
  * ``live_trade_closed`` — broker fill closed
  * ``paper_trade_closed`` — paper trade resolved
  * ``broker_fill_closed`` — fill-level close

Phase 2 of FIX 31 endgame, handler #4 of 5. Author: 2026-04-29 (FIX 39).

**Safety:** Demotion is conservative. We only flip lifecycle to 'challenged'
when the realized EV gate explicitly blocks based on accumulated evidence.
Single-trade noise should not demote — that's why we delegate to the EV
gate which has its own min-trades floor.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:demote]"


def handle_trade_closed(db: "Session", ev, user_id: int | None) -> None:
    """Re-check realized EV gate for the pattern of a just-closed trade.

    Demotes the pattern (lifecycle 'promoted' → 'challenged') only when the
    EV gate explicitly blocks. Conservative single-trade-can't-demote design
    is enforced by the EV gate's own min-trades floor.
    """
    from ....db import SessionLocal
    from ....models.trading import ScanPattern
    from ..promotion_surface import emit_promotion_surface_change

    payload = ev.payload if isinstance(ev.payload, dict) else {}
    pid = int(payload.get("scan_pattern_id") or 0)
    if pid <= 0:
        # Some trade-close events don't carry a pattern (e.g. discretionary
        # trades). Not an error — just nothing to demote.
        logger.debug("%s ev_id=%s no scan_pattern_id — skip", LOG_PREFIX, ev.id)
        return

    sess = SessionLocal()
    try:
        pattern = sess.get(ScanPattern, pid)
        if pattern is None:
            return

        old_lc = (pattern.lifecycle_stage or "").strip()
        old_promo = (pattern.promotion_status or "").strip()

        # Only re-evaluate currently-promoted patterns. Backtested-only
        # patterns get their lifecycle decisions through cpcv_gate handler;
        # candidates haven't graduated yet.
        if old_lc != "promoted":
            return

        try:
            from ...realized_ev_gate import check_realized_ev_blocking

            ev_blocked, ev_reasons, ev_snap = check_realized_ev_blocking(pattern)
        except Exception as e:
            logger.warning(
                "%s ev_gate eval crashed pattern_id=%d: %s",
                LOG_PREFIX, pid, e,
            )
            return

        if not ev_blocked:
            # Gate still passes — leave promoted.
            return

        # Gate now blocks — demote to challenged with reason.
        short_reason = (ev_reasons[0] if ev_reasons else "ev_gate_failed")[:32]
        pattern.lifecycle_stage = "challenged"
        pattern.promotion_status = f"challenged_ev_{short_reason[:14]}"
        pattern.lifecycle_changed_at = datetime.utcnow()
        sess.commit()

        try:
            emit_promotion_surface_change(
                sess,
                scan_pattern_id=pid,
                old_promotion_status=old_promo,
                old_lifecycle_stage=old_lc,
                new_promotion_status=(pattern.promotion_status or "").strip(),
                new_lifecycle_stage=(pattern.lifecycle_stage or "").strip(),
                source="demote_handler",
                extra={
                    "parent_work_event_id": int(ev.id),
                    "ev_reasons": ev_reasons,
                    "ev_snapshot": ev_snap,
                },
            )
        except Exception:
            logger.debug("%s promotion_surface emit failed", LOG_PREFIX, exc_info=True)

        logger.info(
            "%s ev_id=%s pattern_id=%d DEMOTED reasons=%s win_rate=%s n_trades=%s",
            LOG_PREFIX, ev.id, pid, ev_reasons,
            ev_snap.get("win_rate") if isinstance(ev_snap, dict) else None,
            ev_snap.get("trade_count") if isinstance(ev_snap, dict) else None,
        )
    except Exception as e:
        try:
            sess.rollback()
        except Exception:
            pass
        logger.warning("%s ev_id=%s failed: %s", LOG_PREFIX, ev.id, e, exc_info=True)
        raise
    finally:
        try:
            sess.close()
        except Exception:
            pass
