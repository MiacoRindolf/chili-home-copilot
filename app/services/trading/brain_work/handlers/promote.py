"""Promote handler — reacts to ``pattern_eligible_promotion`` events.

Replaces the promotion finalize step of ``run_learning_cycle``. The CPCV gate
handler (#2) emits ``pattern_eligible_promotion`` when a pattern's gate
evaluation passes. This handler performs the actual promotion:

  1. Re-checks the pattern is still in ``backtested`` lifecycle (no race
     where another handler retired it between the gate eval and now).
  2. Applies the realized-EV gate as a final safety filter (the secondary
     gate added 2026-04-28 — see reference_promotion_gates.md).
  3. If both gates still pass, sets ``lifecycle_stage='promoted'`` and
     ``promotion_status='promoted_via_cpcv_gate'`` (dated suffix optional).
  4. Emits ``promotion_surface_change`` so autotrader scope refresh picks up
     the new promoted pattern on its next tick.
  5. Logs the promotion event so the audit trail is complete.

Phase 2 of FIX 31 endgame, handler #3 of 5. Author: 2026-04-29 (FIX 38).

**Safety:** This handler is the ONLY thing that can flip a pattern to
``lifecycle_stage='promoted'``. Concentrating promotion authority here
makes it easy to audit and lock down. Two-gate enforcement (CPCV + EV)
matches the existing cycle-path discipline.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:promote]"


def handle_pattern_eligible_promotion(db: "Session", ev, user_id: int | None) -> None:
    """Finalize promotion for a pattern that passed CPCV gate."""
    from ....db import SessionLocal
    from ....models.trading import ScanPattern
    from ..promotion_surface import emit_promotion_surface_change

    payload = ev.payload if isinstance(ev.payload, dict) else {}
    pid = int(payload.get("scan_pattern_id") or 0)
    if pid <= 0:
        raise ValueError("pattern_eligible_promotion missing scan_pattern_id")

    sess = SessionLocal()
    try:
        pattern = sess.get(ScanPattern, pid)
        if pattern is None:
            logger.warning("%s ev_id=%s pattern_id=%d not found", LOG_PREFIX, ev.id, pid)
            return

        old_promo = (pattern.promotion_status or "").strip()
        old_lc = (pattern.lifecycle_stage or "").strip()

        # Don't re-promote what's already promoted; don't promote what was
        # demoted between the gate eval and now.
        if old_lc == "promoted":
            logger.info(
                "%s ev_id=%s pattern_id=%d already promoted — skip",
                LOG_PREFIX, ev.id, pid,
            )
            return
        if old_lc not in ("backtested", "candidate"):
            logger.info(
                "%s ev_id=%s pattern_id=%d lifecycle=%s — refuse promote (not eligible)",
                LOG_PREFIX, ev.id, pid, old_lc,
            )
            return

        # Apply the realized-EV gate as a safety net. This matches the cycle
        # path's two-gate discipline. The EV gate uses pattern.win_rate +
        # avg_return_pct + trade_count — for a never-traded pattern, it
        # blocks with realized_n_below_min, which is the correct behavior
        # (no realized evidence yet → don't promote based on backtest only).
        try:
            from ...realized_ev_gate import check_realized_ev_blocking

            ev_blocked, ev_reasons, _ev_snap = check_realized_ev_blocking(pattern)
        except ImportError:
            # Fallback: realized_ev_gate module may not exist in older code paths.
            logger.warning(
                "%s realized_ev_gate import failed — using CPCV-only promotion",
                LOG_PREFIX,
            )
            ev_blocked = False
            ev_reasons = []
        except Exception as e:
            logger.warning(
                "%s realized_ev_gate eval crashed pattern_id=%d: %s",
                LOG_PREFIX, pid, e,
            )
            ev_blocked = True
            ev_reasons = [f"ev_gate_error:{type(e).__name__}"]

        if ev_blocked:
            # CPCV passed but realized EV insufficient — leave as backtested
            # with a marker. Promote handler will fire again later when the
            # pattern accumulates real trades and re-passes both gates.
            pattern.promotion_status = "eligible_pending_ev"
            pattern.lifecycle_changed_at = datetime.utcnow()
            sess.commit()
            logger.info(
                "%s ev_id=%s pattern_id=%d EV_BLOCK reasons=%s — leave backtested",
                LOG_PREFIX, ev.id, pid, ev_reasons,
            )
            return

        # Both gates pass — promote.
        pattern.lifecycle_stage = "promoted"
        pattern.promotion_status = "promoted_via_cpcv_gate"
        pattern.lifecycle_changed_at = datetime.utcnow()
        pattern.active = True  # Promotion implies active=True
        sess.commit()

        try:
            emit_promotion_surface_change(
                sess,
                scan_pattern_id=pid,
                old_promotion_status=old_promo,
                old_lifecycle_stage=old_lc,
                new_promotion_status=(pattern.promotion_status or "").strip(),
                new_lifecycle_stage=(pattern.lifecycle_stage or "").strip(),
                source="promote_handler",
                extra={
                    "parent_work_event_id": int(ev.id),
                    "cpcv_median_sharpe": payload.get("cpcv_median_sharpe"),
                    "deflated_sharpe": payload.get("deflated_sharpe"),
                },
            )
        except Exception:
            logger.debug("%s promotion_surface emit failed", LOG_PREFIX, exc_info=True)

        logger.info(
            "%s ev_id=%s pattern_id=%d PROMOTED med_sh=%s dsr=%s",
            LOG_PREFIX, ev.id, pid,
            payload.get("cpcv_median_sharpe"),
            payload.get("deflated_sharpe"),
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
