"""CPCV gate handler — reacts to ``backtest_completed`` outcome events.

Replaces the OOS validation step of ``run_learning_cycle`` (the block at
``learning.py:7186-7249``). When a pattern's backtest completes (the existing
``_handle_backtest_requested`` emits ``backtest_completed``), this handler:

  1. Loads ``PatternTradeRow`` rows for the pattern (the per-occurrence
     ledger that the gate consumes).
  2. Runs ``check_promotion_ready`` — CPCV (deflated Sharpe + PBO + paths).
  3. Persists the gate fields back to ``ScanPattern``: ``cpcv_median_sharpe``,
     ``deflated_sharpe``, etc.
  4. Sets lifecycle_stage based on the gate result:
        - pass → ``backtested`` (eligible for downstream promotion handler)
        - fail (CPCV blocks) → ``challenged`` (with reason)
        - insufficient evidence → leave as ``candidate`` (waits for more)

Phase 2 of FIX 31 endgame, handler #2 of 5. Author: 2026-04-29 (FIX 37).

**Important:** This handler does NOT auto-promote. Promotion is a separate
handler (#3 ``promote.py``) that subscribes to a downstream event we'll emit
on gate-pass. Keeping promotion separate makes the gate side-effect-free with
respect to the live trading scope, which is critical for safety.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:cpcv_gate]"

# Same threshold the cycle path uses (learning.py:7214). The CPCV gate has
# its own internal floor (``cpcv_n_paths_below_provisional_min``) at lower
# trade counts. We pass min_trades=30 to be consistent with cycle behavior.
_MIN_TRADES_FOR_GATE = 30


def handle_backtest_completed(db: "Session", ev, user_id: int | None) -> None:
    """Run CPCV gate for the pattern that just finished backtesting."""
    from ....db import SessionLocal
    from ....models.trading import PatternTradeRow as _PTR
    from ....models.trading import ScanPattern
    from ...mining_validation import check_promotion_ready
    from ...promotion_gate import (
        cpcv_eval_to_scan_pattern_fields,
        persist_cpcv_shadow_eval,
    )
    from ..promotion_surface import emit_promotion_surface_change

    payload = ev.payload if isinstance(ev.payload, dict) else {}
    pid = int(payload.get("scan_pattern_id") or 0)
    if pid <= 0:
        raise ValueError("backtest_completed missing scan_pattern_id")

    sess = SessionLocal()
    try:
        pattern = sess.get(ScanPattern, pid)
        if pattern is None:
            logger.warning("%s ev_id=%s pattern_id=%d not found", LOG_PREFIX, ev.id, pid)
            return

        old_promo = (pattern.promotion_status or "").strip()
        old_lc = (pattern.lifecycle_stage or "").strip()

        # Don't re-evaluate already-promoted or already-retired patterns
        # — this handler is for advancing candidates, not re-litigating
        # historical decisions.
        if old_lc in ("promoted", "retired"):
            logger.info(
                "%s ev_id=%s pattern_id=%d lifecycle=%s — skip (terminal)",
                LOG_PREFIX, ev.id, pid, old_lc,
            )
            return

        # Pull PTR rows for this pattern (same query shape as learning.py:7191).
        ptr_rows = (
            sess.query(_PTR)
            .filter(
                _PTR.scan_pattern_id == pid,
                _PTR.outcome_return_pct.isnot(None),
            )
            .order_by(_PTR.as_of_ts.asc())
            .all()
        )

        if len(ptr_rows) < _MIN_TRADES_FOR_GATE:
            logger.info(
                "%s ev_id=%s pattern_id=%d ptr_rows=%d below_min=%d — leave candidate",
                LOG_PREFIX, ev.id, pid, len(ptr_rows), _MIN_TRADES_FOR_GATE,
            )
            return

        # Build ensemble rows (same shape as learning.py:7200-7211).
        from ...promotion_gate import normalize_ptr_row_features

        ensemble_rows: list[dict[str, Any]] = []
        for r in ptr_rows:
            fj = r.features_json if isinstance(r.features_json, dict) else {}
            row_d = normalize_ptr_row_features(
                outcome_return_pct=r.outcome_return_pct,
                as_of_ts=r.as_of_ts,
                ticker=r.ticker,
                timeframe=r.timeframe,
                features_json=fj,
            )
            row_d["ret_5d"] = float(r.outcome_return_pct or 0.0)
            ensemble_rows.append(row_d)

        ok, detail = check_promotion_ready(
            ensemble_rows,
            min_trades=_MIN_TRADES_FOR_GATE,
            n_hypotheses_tested=1,  # single-pattern eval; cycle uses higher
            scan_pattern=pattern,
        )

        # Always persist the shadow log so the gate decision is auditable
        # even when the promotion side-effect is gated by a flag.
        try:
            persist_cpcv_shadow_eval(sess, pattern, detail.get("cpcv_promotion_gate") or {})
        except Exception:
            logger.debug("%s cpcv_shadow_log failed", LOG_PREFIX, exc_info=True)

        # Apply the cpcv_* numeric fields back to the pattern row.
        cpcv_patch = cpcv_eval_to_scan_pattern_fields(detail.get("cpcv_promotion_gate") or {})
        for k, v in (cpcv_patch or {}).items():
            setattr(pattern, k, v)

        # Lifecycle decision. Promotion happens in handler #3, not here.
        gate_payload = detail.get("cpcv_promotion_gate") or {}
        gate_pass = bool(gate_payload.get("pass"))
        if ok and gate_pass:
            pattern.lifecycle_stage = "backtested"
            pattern.lifecycle_changed_at = datetime.utcnow()
            new_status = "eligible_promotion"
            pattern.promotion_status = new_status
            from ..emitters import enqueue_outcome_event

            # Hand off to the promote handler (#3). The promote handler is
            # the only thing that flips lifecycle to 'promoted' + makes the
            # pattern live. Keeping that gated for safety.
            enqueue_outcome_event(
                sess,
                event_type="pattern_eligible_promotion",
                dedupe_key=f"eligible:cpcv:{pid}:{ev.id}",
                payload={
                    "scan_pattern_id": pid,
                    "parent_work_event_id": int(ev.id),
                    "cpcv_median_sharpe": gate_payload.get("median_sharpe"),
                    "deflated_sharpe": gate_payload.get("deflated_sharpe"),
                    "paths": gate_payload.get("paths"),
                },
                parent_event_id=int(ev.id),
            )
            logger.info(
                "%s ev_id=%s pattern_id=%d gate=PASS med_sh=%s dsr=%s — eligible_promotion",
                LOG_PREFIX, ev.id, pid,
                gate_payload.get("median_sharpe"),
                gate_payload.get("deflated_sharpe"),
            )
        elif not gate_pass:
            # Gate explicitly failed (e.g. cpcv_n_paths_below_provisional_min,
            # deflated_sharpe_below_threshold). Move to challenged.
            pattern.lifecycle_stage = "challenged"
            pattern.lifecycle_changed_at = datetime.utcnow()
            reasons = gate_payload.get("reasons") or []
            short_reason = (reasons[0] if reasons else "cpcv_gate_failed")[:32]
            pattern.promotion_status = f"challenged_cpcv_{short_reason[:12]}"
            logger.info(
                "%s ev_id=%s pattern_id=%d gate=FAIL reasons=%s — challenged",
                LOG_PREFIX, ev.id, pid, reasons,
            )

        new_promo = (pattern.promotion_status or "").strip()
        new_lc = (pattern.lifecycle_stage or "").strip()

        if new_lc != old_lc or new_promo != old_promo:
            try:
                emit_promotion_surface_change(
                    sess,
                    scan_pattern_id=pid,
                    old_promotion_status=old_promo,
                    old_lifecycle_stage=old_lc,
                    new_promotion_status=new_promo,
                    new_lifecycle_stage=new_lc,
                    source="cpcv_gate_handler",
                    extra={"parent_work_event_id": int(ev.id)},
                )
            except Exception:
                logger.debug("%s promotion_surface emit failed", LOG_PREFIX, exc_info=True)

        sess.commit()
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
