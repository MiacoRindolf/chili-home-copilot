"""Claim brain_work_events and run registered handlers (bounded batch, multi-type)."""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

from sqlalchemy.orm import Session

from ....config import settings
from .emitters import emit_execution_quality_updated_outcome
from .ledger import (
    brain_work_ledger_enabled,
    claim_work_batch,
    enqueue_outcome_event,
    mark_work_done,
    mark_work_retry_or_dead,
    release_stale_leases,
)
from .promotion_surface import emit_promotion_surface_change

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work_dispatch]"


def _holder_id() -> str:
    try:
        host = socket.gethostname()[:40]
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def _handle_backtest_requested(db: Session, ev, user_id: int | None) -> None:
    from ....db import SessionLocal
    from ....models.trading import ScanPattern
    from ..backtest_queue_worker import execute_queue_backtest_for_pattern

    payload = ev.payload if isinstance(ev.payload, dict) else {}
    pid = int(payload.get("scan_pattern_id") or 0)
    if pid <= 0:
        raise ValueError("backtest_requested missing scan_pattern_id")

    s0 = SessionLocal()
    try:
        p0 = s0.get(ScanPattern, pid)
        if p0 is None:
            raise ValueError(f"scan_pattern_id={pid} not found")
        old_promo = (p0.promotion_status or "").strip()
        old_lc = (p0.lifecycle_stage or "").strip()
    finally:
        s0.close()

    bt_run, _proc = execute_queue_backtest_for_pattern(pid, user_id)

    s1 = SessionLocal()
    try:
        p1 = s1.get(ScanPattern, pid)
        new_promo = (p1.promotion_status or "").strip() if p1 else ""
        new_lc = (p1.lifecycle_stage or "").strip() if p1 else ""

        enqueue_outcome_event(
            s1,
            event_type="backtest_completed",
            dedupe_key=f"bt_done:req:{ev.id}",
            payload={
                "scan_pattern_id": pid,
                "parent_work_event_id": ev.id,
                "backtests_run": bt_run,
            },
            parent_event_id=int(ev.id),
        )
        emit_promotion_surface_change(
            s1,
            scan_pattern_id=pid,
            old_promotion_status=old_promo,
            old_lifecycle_stage=old_lc,
            new_promotion_status=new_promo,
            new_lifecycle_stage=new_lc,
            source="queue_backtest",
            extra={"parent_work_event_id": int(ev.id)},
        )
        s1.commit()
    finally:
        s1.close()

    try:
        from ..brain_neural_mesh.publisher import publish_brain_work_outcome

        publish_brain_work_outcome(
            db,
            outcome_type="backtest_completed",
            scan_pattern_id=pid,
            extra={"work_event_id": int(ev.id), "backtests_run": bt_run},
        )
    except Exception as e:
        logger.debug("%s mesh publish skipped: %s", LOG_PREFIX, e)


def _handle_execution_feedback_digest(db: Session, ev, user_id: int | None) -> None:
    """Debounced: execution stats, adaptive spread hint, live depromotion sweep."""
    from ..execution_quality import compute_execution_stats, suggest_adaptive_spread
    from ..learning import run_live_pattern_depromotion

    payload = ev.payload if isinstance(ev.payload, dict) else {}
    uid = payload.get("user_id")
    if uid is None:
        uid = user_id
    if uid is None:
        raise ValueError("execution_feedback_digest missing user_id")

    stats = compute_execution_stats(db, int(uid), lookback_days=90)
    spread = suggest_adaptive_spread(db, int(uid), lookback_days=60)
    dep = run_live_pattern_depromotion(db)

    stats_summary = {
        "trades_analyzed": stats.get("trades_analyzed", 0),
        "measurable": stats.get("measurable", 0),
        "avg_slippage_pct": stats.get("avg_slippage_pct"),
        "p90_slippage_pct": stats.get("p90_slippage_pct"),
    }
    spread_hint = {
        "current_spread": spread.get("current_spread"),
        "suggested_spread": spread.get("suggested_spread"),
        "should_update": spread.get("should_update"),
        "reason": spread.get("reason"),
    }
    emit_execution_quality_updated_outcome(
        db,
        user_id=int(uid),
        stats_summary=stats_summary,
        spread_hint=spread_hint,
        depromotion=dep if isinstance(dep, dict) else {"raw": dep},
        parent_work_event_id=int(ev.id),
    )
    try:
        from ..brain_neural_mesh.publisher import publish_brain_work_outcome

        publish_brain_work_outcome(
            db,
            outcome_type="execution_quality_updated",
            scan_pattern_id=None,
            extra={"work_event_id": int(ev.id), "user_id": int(uid)},
        )
    except Exception as e:
        logger.debug("%s mesh exec-quality publish skipped: %s", LOG_PREFIX, e)


def _dispatch_limits(
    *,
    max_backtest: int | None = None,
    max_exec_feedback: int | None = None,
) -> list[tuple[str, int]]:
    """Order: execution feedback first (short), then backtests."""
    bt = int(max_backtest if max_backtest is not None else getattr(settings, "brain_work_dispatch_batch_size", 8))
    ex = int(
        max_exec_feedback
        if max_exec_feedback is not None
        else getattr(settings, "brain_work_exec_feedback_batch_size", 3)
    )
    return [
        ("execution_feedback_digest", max(0, ex)),
        ("backtest_requested", max(0, bt)),
    ]


def run_brain_work_dispatch_round(
    db: Session,
    *,
    user_id: int | None = None,
    max_backtest: int | None = None,
    max_exec_feedback: int | None = None,
) -> dict[str, Any]:
    """Release stale leases, then claim+process work by handler family (bounded per type)."""
    if not brain_work_ledger_enabled():
        return {"ok": True, "skipped": True, "reason": "ledger_disabled", "processed": 0}

    release_stale_leases(db)
    db.commit()

    lease_s = int(getattr(settings, "brain_work_lease_seconds", 900))
    holder = _holder_id()

    processed = 0
    claimed_total = 0
    errors: list[str] = []
    per_type: dict[str, int] = {}

    for event_type, lim in _dispatch_limits(
        max_backtest=max_backtest, max_exec_feedback=max_exec_feedback
    ):
        if lim <= 0:
            continue
        rows = claim_work_batch(
            db, limit=lim, lease_seconds=lease_s, holder_id=holder, event_type=event_type
        )
        db.commit()
        claimed_total += len(rows)
        n_done = 0
        for ev in rows:
            try:
                if event_type == "backtest_requested":
                    _handle_backtest_requested(db, ev, user_id)
                elif event_type == "execution_feedback_digest":
                    _handle_execution_feedback_digest(db, ev, user_id)
                else:
                    raise ValueError(f"unknown work event_type={event_type}")
                mark_work_done(db, int(ev.id))
                db.commit()
                n_done += 1
                processed += 1
            except Exception as e:
                logger.warning("%s work id=%s type=%s failed: %s", LOG_PREFIX, ev.id, event_type, e, exc_info=True)
                try:
                    db.rollback()
                except Exception:
                    pass
                try:
                    mark_work_retry_or_dead(db, int(ev.id), str(e))
                    db.commit()
                except Exception as e2:
                    logger.warning("%s mark retry failed id=%s: %s", LOG_PREFIX, ev.id, e2)
                    try:
                        db.rollback()
                    except Exception:
                        pass
                errors.append(f"id={ev.id}:{event_type}:{e!s}")
        per_type[event_type] = n_done

    return {
        "ok": True,
        "processed": processed,
        "claimed": claimed_total,
        "per_type": per_type,
        "errors": errors,
    }


def run_brain_work_batch(
    db: Session,
    *,
    user_id: int | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """Backward-compatible name: multi-type dispatch (*max_items* caps backtest bucket only)."""
    return run_brain_work_dispatch_round(
        db, user_id=user_id, max_backtest=max_items, max_exec_feedback=None
    )
