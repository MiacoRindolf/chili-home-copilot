"""Claim brain_work_events and run registered handlers (bounded batch, multi-type)."""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ....config import settings
from .emitters import emit_execution_quality_updated_outcome
from .ledger import (
    brain_work_ledger_enabled,
    claim_work_batch,
    coalesce_duplicate_open_work,
    enqueue_outcome_event,
    mark_work_done,
    mark_work_retry_or_dead,
    recover_retryable_dead_work,
    release_stale_leases,
)
from .promotion_surface import emit_promotion_surface_change

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work_dispatch]"

# f-brain-phase2-producer-completion (2026-05-09): watchdog-style
# mining producer. The APScheduler brain_market_snapshots job is
# wired in trading_scheduler.py:262 but stopped firing 2026-05-05;
# the audit confirmed zero market_snapshots_batch events in 4 days.
# This in-process timestamp tracks the last dispatch-round emit so
# we can space them out at the configured interval. Module-level so
# all rounds share state within a brain-worker process. Cleared on
# container restart (intentional: the next round emits immediately
# after a restart, which is the right behaviour for catch-up).
_LAST_DISPATCH_MARKET_SNAPSHOTS_AT: float = 0.0

_TRANSIENT_DB_DISCONNECT_MARKERS = (
    "server closed the connection unexpectedly",
    "connection already closed",
    "connection not open",
    "can't reconnect until invalid transaction is rolled back",
)

_ISOLATED_SIDE_EFFECT_EVENT_TYPES = frozenset({
    "backtest_requested",
    "backtest_completed",
    "pattern_eligible_promotion",
    "live_trade_closed",
    "paper_trade_closed",
    "broker_fill_closed",
})


def _holder_id() -> str:
    try:
        host = socket.gethostname()[:40]
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def _recover_dispatch_session(db: Session, context: str) -> None:
    """Clear a failed dispatcher transaction after a swallowed handler error."""
    try:
        db.rollback()
    except Exception as exc:
        logger.debug(
            "%s rollback after swallowed %s failed: %s",
            LOG_PREFIX,
            context,
            exc,
            exc_info=True,
        )


def _looks_like_transient_db_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return True
    msg = str(exc or "").lower()
    return any(marker in msg for marker in _TRANSIENT_DB_DISCONNECT_MARKERS)


def _mark_work_done_after_handler_success(
    db: Session,
    *,
    event_id: int,
    event_type: str,
) -> dict[str, Any]:
    """Mark completed work done, recovering isolated-handler rows from DB disconnects.

    Some handler families already commit their real side effects through fresh
    ``SessionLocal`` instances. If the dispatcher connection dies only while
    writing the final done marker, retrying the whole work item duplicates heavy
    side effects. For those isolated families only, finish the done marker in a
    fresh session. Same-session handlers still retry, which is safer because
    their uncommitted writes may have been lost with the broken connection.
    """
    try:
        mark_work_done(db, event_id)
        db.commit()
        return {"ok": True, "isolated": False}
    except Exception as exc:
        if (
            event_type not in _ISOLATED_SIDE_EFFECT_EVENT_TYPES
            or not _looks_like_transient_db_disconnect(exc)
        ):
            raise
        logger.warning(
            "%s mark done lost dispatcher DB connection id=%s type=%s; "
            "retrying done marker in isolated session",
            LOG_PREFIX,
            event_id,
            event_type,
        )
        _recover_dispatch_session(db, "mark_work_done transient disconnect")
        from ....db import SessionLocal

        sess = SessionLocal()
        try:
            mark_work_done(sess, event_id)
            sess.commit()
            return {"ok": True, "isolated": True}
        except Exception:
            try:
                sess.rollback()
            except Exception:
                pass
            raise
        finally:
            sess.close()


def _publish_brain_work_outcome_isolated(
    *,
    outcome_type: str,
    scan_pattern_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Publish non-critical mesh observations without risking the dispatcher DB session."""
    try:
        from ....db import SessionLocal
        from ..brain_neural_mesh.publisher import publish_brain_work_outcome
    except Exception as exc:
        logger.debug("%s mesh publisher import skipped: %s", LOG_PREFIX, exc)
        return

    sess = SessionLocal()
    try:
        publish_brain_work_outcome(
            sess,
            outcome_type=outcome_type,
            scan_pattern_id=scan_pattern_id,
            extra=extra,
        )
        sess.commit()
    except Exception as exc:
        logger.debug(
            "%s isolated mesh publish skipped type=%s: %s",
            LOG_PREFIX,
            outcome_type,
            exc,
            exc_info=True,
        )
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        try:
            sess.close()
        except Exception:
            pass


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
        # FIX 46 pattern: rollback to end implicit read txn before close.
        try:
            s0.rollback()
        except Exception:
            pass
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
        # FIX 46 pattern (rollback before close).
        try:
            s1.rollback()
        except Exception:
            pass
        s1.close()

    _recover_dispatch_session(db, "backtest completion handoff")
    _publish_brain_work_outcome_isolated(
        outcome_type="backtest_completed",
        scan_pattern_id=pid,
        extra={"work_event_id": int(ev.id), "backtests_run": bt_run},
    )


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
    # f-pattern-demote-sweep-wiring-fix (2026-05-09): the
    # `run_thin_evidence_demote` sweep used to live here, but
    # this hook fires only on `live_trade_closed` events
    # (~3 per 24h in the current operating state). Wired into
    # `run_brain_work_dispatch_round` directly so it fires every
    # ~75-90s round regardless of work-ledger state.

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
    attribution_summary: dict | None = None
    try:
        from ..attribution_service import live_vs_research_by_pattern

        rep = live_vs_research_by_pattern(db, int(uid), days=90, limit=8)
        pats = rep.get("patterns") or []
        attribution_summary = {
            "window_days": rep.get("window_days"),
            "patterns_tracked": len(pats),
            "top_by_live_closed": [
                {
                    "scan_pattern_id": p.get("scan_pattern_id"),
                    "live_n": p.get("live_closed_trades"),
                    "live_wr_pct": p.get("live_win_rate_pct"),
                    "oos_wr_pct": p.get("research_oos_win_rate_pct"),
                }
                for p in pats[:5]
            ],
            "digest_trigger": payload.get("trigger"),
        }
    except Exception:
        logger.debug("%s attribution snapshot skipped", LOG_PREFIX, exc_info=True)

    # f-pattern-demote-sweep-wiring-fix (2026-05-09): the thin-
    # evidence merge moved out -- the per-cycle sweep at the end of
    # `run_brain_work_dispatch_round` is now the source of truth.
    dep_payload = dep if isinstance(dep, dict) else {"raw": dep}

    emit_execution_quality_updated_outcome(
        db,
        user_id=int(uid),
        stats_summary=stats_summary,
        spread_hint=spread_hint,
        depromotion=dep_payload,
        parent_work_event_id=int(ev.id),
        attribution_summary=attribution_summary,
    )
    _publish_brain_work_outcome_isolated(
        outcome_type="execution_quality_updated",
        scan_pattern_id=None,
        extra={"work_event_id": int(ev.id), "user_id": int(uid)},
    )


def _dispatch_limits(
    *,
    max_backtest: int | None = None,
    max_exec_feedback: int | None = None,
    max_edge_reliability: int | None = None,
    max_recert_rescue: int | None = None,
    max_exit_variant: int | None = None,
    max_provenance: int | None = None,
    max_mine: int | None = None,
    max_cpcv_gate: int | None = None,
    max_promote: int | None = None,
    max_trade_close: int | None = None,
) -> list[tuple[str, int]]:
    """Order: execution feedback, mine, backtests, cpcv_gate, promote, trade-close fanout."""
    bt = int(max_backtest if max_backtest is not None else getattr(settings, "brain_work_dispatch_batch_size", 8))
    ex = int(
        max_exec_feedback
        if max_exec_feedback is not None
        else getattr(settings, "brain_work_exec_feedback_batch_size", 3)
    )
    er = int(
        max_edge_reliability
        if max_edge_reliability is not None
        else getattr(settings, "brain_work_edge_reliability_batch_size", 4)
    )
    rr = int(
        max_recert_rescue
        if max_recert_rescue is not None
        else getattr(settings, "brain_work_recert_rescue_batch_size", 2)
    )
    xv = int(
        max_exit_variant
        if max_exit_variant is not None
        else getattr(settings, "brain_work_exit_variant_batch_size", 2)
    )
    pb = int(
        max_provenance
        if max_provenance is not None
        else getattr(settings, "brain_work_provenance_batch_size", 1)
    )
    # FIX 36 (Phase 2, 2026-04-29): cap mine to 1 per dispatch round — mining
    # is heavy and there's no value in running the same handler twice in a
    # single batch, even if multiple market_snapshots_batch events arrived.
    mn = int(
        max_mine
        if max_mine is not None
        else getattr(settings, "brain_work_mine_batch_size", 1)
    )
    # FIX 37 (Phase 2 #2, 2026-04-29): cpcv_gate is fast (DB query + numeric
    # eval); cap higher to drain the pipe quickly when many backtests complete
    # in a burst.
    cg = int(
        max_cpcv_gate
        if max_cpcv_gate is not None
        else getattr(settings, "brain_work_cpcv_gate_batch_size", 8)
    )
    # FIX 38 (Phase 2 #3, 2026-04-29): promote handler — flips lifecycle to
    # 'promoted' after second-gate (realized EV) check. Cap at 4 per round
    # since promotion is rare and we want to log each one cleanly.
    pm = int(
        max_promote
        if max_promote is not None
        else getattr(settings, "brain_work_promote_batch_size", 4)
    )
    # FIX 39 (Phase 2 #4+#5, 2026-04-29): trade-close fanout. Both demote
    # and regime_ledger handlers subscribe to the same three close events
    # (live_trade_closed, paper_trade_closed, broker_fill_closed). Each
    # event is dispatched to BOTH handlers in sequence (see handler chain
    # below). Cap higher since trade closes can burst during regime shifts.
    tc = int(
        max_trade_close
        if max_trade_close is not None
        else getattr(settings, "brain_work_trade_close_batch_size", 16)
    )
    return [
        ("execution_feedback_digest", max(0, ex)),
        ("edge_reliability_refresh", max(0, er)),
        ("recert_rescue_refresh", max(0, rr)),
        ("exit_variant_refresh", max(0, xv)),
        ("provenance_backfill", max(0, pb)),
        ("market_snapshots_batch", max(0, mn)),
        ("backtest_requested", max(0, bt)),
        ("backtest_completed", max(0, cg)),
        ("pattern_eligible_promotion", max(0, pm)),
        ("live_trade_closed", max(0, tc)),
        ("paper_trade_closed", max(0, tc)),
        ("broker_fill_closed", max(0, tc)),
    ]


def run_brain_work_dispatch_round(
    db: Session,
    *,
    user_id: int | None = None,
    max_backtest: int | None = None,
    max_exec_feedback: int | None = None,
    max_edge_reliability: int | None = None,
    max_recert_rescue: int | None = None,
    max_exit_variant: int | None = None,
    max_provenance: int | None = None,
    max_mine: int | None = None,
    max_cpcv_gate: int | None = None,
    max_promote: int | None = None,
    max_trade_close: int | None = None,
    run_thin_evidence_sweep: bool = True,
    run_market_snapshots_watchdog: bool = True,
) -> dict[str, Any]:
    """Release stale leases, then claim+process work by handler family (bounded per type)."""
    if not brain_work_ledger_enabled():
        return {"ok": True, "skipped": True, "reason": "ledger_disabled", "processed": 0}

    stale_leases_released = release_stale_leases(db)
    dead_letter_recovery = recover_retryable_dead_work(db)
    duplicate_open_work = coalesce_duplicate_open_work(db)
    db.commit()

    lease_s = int(getattr(settings, "brain_work_lease_seconds", 900))
    holder = _holder_id()

    processed = 0
    claimed_total = 0
    errors: list[str] = []
    per_type: dict[str, int] = {}

    for event_type, lim in _dispatch_limits(
        max_backtest=max_backtest,
        max_exec_feedback=max_exec_feedback,
        max_edge_reliability=max_edge_reliability,
        max_recert_rescue=max_recert_rescue,
        max_exit_variant=max_exit_variant,
        max_provenance=max_provenance,
        max_mine=max_mine,
        max_cpcv_gate=max_cpcv_gate,
        max_promote=max_promote,
        max_trade_close=max_trade_close,
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
            ev_id = int(ev.id)
            try:
                if event_type == "backtest_requested":
                    _handle_backtest_requested(db, ev, user_id)
                elif event_type == "execution_feedback_digest":
                    _handle_execution_feedback_digest(db, ev, user_id)
                elif event_type == "edge_reliability_refresh":
                    from .handlers.profitability import handle_edge_reliability_refresh
                    handle_edge_reliability_refresh(db, ev, user_id)
                elif event_type == "recert_rescue_refresh":
                    from .handlers.profitability import handle_recert_rescue_refresh
                    handle_recert_rescue_refresh(db, ev, user_id)
                elif event_type == "exit_variant_refresh":
                    from .handlers.profitability import handle_exit_variant_refresh
                    handle_exit_variant_refresh(db, ev, user_id)
                elif event_type == "provenance_backfill":
                    from .handlers.profitability import handle_provenance_backfill
                    handle_provenance_backfill(db, ev, user_id)
                elif event_type == "market_snapshots_batch":
                    # FIX 36 (Phase 2, 2026-04-29): event-driven mine handler.
                    # Replaces Step 1 of run_learning_cycle.
                    from .handlers.mine import handle_market_snapshots_batch
                    handle_market_snapshots_batch(db, ev, user_id)
                elif event_type == "backtest_completed":
                    # FIX 37 (Phase 2 #2, 2026-04-29): event-driven CPCV gate.
                    # Replaces the OOS validation step of run_learning_cycle.
                    from .handlers.cpcv_gate import handle_backtest_completed
                    handle_backtest_completed(db, ev, user_id)
                    # f-composite-quality-event-driven (Phase 3,
                    # 2026-05-11): recompute quality_composite_score for
                    # the pattern after cpcv_gate has committed the
                    # fresh CPCV / DSR / PBO numbers. Swallowed exception
                    # so a broken composite doesn't poison the CPCV
                    # gate's lifecycle write.
                    try:
                        from .handlers.quality_score import (
                            handle_backtest_completed_quality,
                        )
                        handle_backtest_completed_quality(db, ev, user_id)
                    except Exception as _qs_err:
                        logger.warning(
                            "%s quality_score (backtest_completed) handler "
                            "failed ev_id=%s: %s",
                            LOG_PREFIX, ev_id, _qs_err,
                        )
                        _recover_dispatch_session(db, "backtest quality_score")
                    try:
                        from .handlers.profitability import (
                            handle_recert_rescue_post_backtest,
                        )
                        handle_recert_rescue_post_backtest(db, ev, user_id)
                    except Exception as _rr_err:
                        logger.warning(
                            "%s recert rescue post-backtest handler "
                            "failed ev_id=%s: %s",
                            LOG_PREFIX, ev_id, _rr_err,
                        )
                        _recover_dispatch_session(db, "recert rescue post-backtest")
                elif event_type == "pattern_eligible_promotion":
                    # FIX 38 (Phase 2 #3, 2026-04-29): promote handler.
                    # Sole authority for flipping lifecycle to 'promoted'.
                    from .handlers.promote import handle_pattern_eligible_promotion
                    handle_pattern_eligible_promotion(db, ev, user_id)
                elif event_type in ("live_trade_closed", "paper_trade_closed", "broker_fill_closed"):
                    # FIX 39 (Phase 2 #4+#5, 2026-04-29): trade-close fanout.
                    # Each event is dispatched to demote + regime_ledger
                    # handlers. If demote raises, we still try regime_ledger
                    # so a broken handler doesn't poison the other.
                    #
                    # f-handler-pattern-stats (2026-05-05, Phase 2 #6): added
                    # pattern_stats as a THIRD subscriber, dispatched FIRST
                    # in the chain so demote re-evaluates the realized-EV
                    # gate against canonical-corrected evidence rather than
                    # stale pre-correction stats. pattern_stats swallows its
                    # own exceptions internally; this branch doesn't gate
                    # on it.
                    try:
                        from .handlers.pattern_stats import (
                            handle_paper_trade_closed,
                            handle_live_trade_closed,
                            handle_broker_fill_closed,
                        )
                        if event_type == "paper_trade_closed":
                            handle_paper_trade_closed(db, ev, user_id)
                        elif event_type == "live_trade_closed":
                            handle_live_trade_closed(db, ev, user_id)
                        else:
                            handle_broker_fill_closed(db, ev, user_id)
                    except Exception as _ps:
                        # pattern_stats handler is defensive; if it raises
                        # at the import boundary, log + continue so demote
                        # still runs.
                        logger.warning(
                            "%s pattern_stats handler failed ev_id=%s: %s "
                            "— proceeding to demote with stale evidence",
                            LOG_PREFIX, ev_id, _ps,
                        )
                        _recover_dispatch_session(db, "pattern_stats")
                    demote_err: Exception | None = None
                    try:
                        from .handlers.demote import handle_trade_closed
                        handle_trade_closed(db, ev, user_id)
                    except Exception as _de:
                        demote_err = _de
                        logger.warning(
                            "%s demote handler failed ev_id=%s: %s — proceeding to regime_ledger",
                            LOG_PREFIX, ev_id, _de,
                        )
                        _recover_dispatch_session(db, "demote")
                    # f-handler-live-drift + f-handler-execution-robustness
                    # (2026-05-06, Phase 6 of f-overnight-jumbo): both
                    # subscribe to trade-close events; both swallow their
                    # own exceptions. Run BOTH after demote so the EV-gate
                    # has already run; drift / robustness are independent
                    # observability, not lifecycle gates.
                    try:
                        from .handlers.live_drift import (
                            handle_paper_trade_closed as _ld_paper,
                            handle_live_trade_closed as _ld_live,
                            handle_broker_fill_closed as _ld_broker,
                        )
                        if event_type == "paper_trade_closed":
                            _ld_paper(db, ev, user_id)
                        elif event_type == "live_trade_closed":
                            _ld_live(db, ev, user_id)
                        else:
                            _ld_broker(db, ev, user_id)
                    except Exception as _ld_err:
                        logger.warning(
                            "%s live_drift handler failed ev_id=%s: %s",
                            LOG_PREFIX, ev_id, _ld_err,
                        )
                        _recover_dispatch_session(db, "live_drift")
                    try:
                        from .handlers.execution_robustness import (
                            handle_paper_trade_closed as _er_paper,
                            handle_live_trade_closed as _er_live,
                            handle_broker_fill_closed as _er_broker,
                        )
                        if event_type == "paper_trade_closed":
                            _er_paper(db, ev, user_id)
                        elif event_type == "live_trade_closed":
                            _er_live(db, ev, user_id)
                        else:
                            _er_broker(db, ev, user_id)
                    except Exception as _er_err:
                        logger.warning(
                            "%s execution_robustness handler failed ev_id=%s: %s",
                            LOG_PREFIX, ev_id, _er_err,
                        )
                        _recover_dispatch_session(db, "execution_robustness")

                    try:
                        from .handlers.regime_ledger import handle_trade_closed_for_ledger
                        handle_trade_closed_for_ledger(db, ev, user_id)
                    except Exception as _re:
                        if demote_err is None:
                            raise
                        # Both failed — re-raise the earlier one for retry.
                        logger.warning(
                            "%s regime_ledger handler also failed ev_id=%s: %s",
                            LOG_PREFIX, ev_id, _re,
                        )
                        raise demote_err
                    # f-composite-quality-event-driven (Phase 3,
                    # 2026-05-11): recompute quality_composite_score
                    # AFTER pattern_stats + regime_ledger have written
                    # fresh win_rate / avg_return / directional-WR
                    # inputs. Swallowed exception so a broken composite
                    # doesn't poison the demote / regime chain.
                    try:
                        from .handlers.quality_score import (
                            handle_trade_closed_quality,
                        )
                        handle_trade_closed_quality(db, ev, user_id)
                    except Exception as _qs_err:
                        logger.warning(
                            "%s quality_score (trade_closed) handler "
                            "failed ev_id=%s: %s",
                            LOG_PREFIX, ev_id, _qs_err,
                        )
                        _recover_dispatch_session(db, "trade-close quality_score")
                    if demote_err is not None:
                        raise demote_err
                else:
                    raise ValueError(f"unknown work event_type={event_type}")
                _mark_work_done_after_handler_success(
                    db,
                    event_id=ev_id,
                    event_type=event_type,
                )
                n_done += 1
                processed += 1
            except Exception as e:
                logger.warning("%s work id=%s type=%s failed: %s", LOG_PREFIX, ev_id, event_type, e, exc_info=True)
                try:
                    db.rollback()
                except Exception:
                    pass
                try:
                    mark_work_retry_or_dead(db, ev_id, str(e))
                    db.commit()
                except Exception as e2:
                    logger.warning("%s mark retry failed id=%s: %s", LOG_PREFIX, ev_id, e2)
                    try:
                        db.rollback()
                    except Exception:
                        pass
                errors.append(f"id={ev_id}:{event_type}:{e!s}")
        per_type[event_type] = n_done

    # f-pattern-demote-sweep-wiring-fix (2026-05-09): per-cycle
    # thin-evidence sweep. Runs once per dispatch round (~75-90s) so
    # newly-promoted thin-evidence patterns get demoted on a
    # meaningful timeline, not only when an `execution_feedback_digest`
    # event happens to fire (which depends on `live_trade_closed`
    # triggers -- ~3 events per 24h in the current operating state).
    #
    # Wrapped in try/except so a sweep failure surfaces in the result
    # dict's `thin_evidence_sweep.ok=False` rather than poisoning the
    # round (other dispatch work has already completed at this point).
    thin_evidence_sweep: dict[str, Any]
    if run_thin_evidence_sweep:
        try:
            from ..learning import run_thin_evidence_demote
            thin_evidence_sweep = run_thin_evidence_demote(db) or {}
            if thin_evidence_sweep.get("demoted_ids"):
                logger.info(
                    "%s thin_evidence sweep: demoted=%d ids=%s",
                    LOG_PREFIX,
                    int(thin_evidence_sweep.get("demoted", 0)),
                    list(thin_evidence_sweep.get("demoted_ids", []) or []),
                )
            else:
                logger.debug(
                    "%s thin_evidence sweep: demoted=0 ids=[]",
                    LOG_PREFIX,
                )
        except Exception as _tied:
            logger.warning(
                "%s thin_evidence sweep failed: %s",
                LOG_PREFIX, _tied, exc_info=True,
            )
            thin_evidence_sweep = {
                "ok": False,
                "demoted": 0,
                "demoted_ids": [],
                "error": str(_tied)[:500],
            }
    else:
        thin_evidence_sweep = {
            "ok": True,
            "skipped": True,
            "reason": "disabled_by_caller",
            "demoted": 0,
            "demoted_ids": [],
        }

    # f-brain-phase2-producer-completion (2026-05-09): watchdog-style
    # mining producer. Emits market_snapshots_batch + writes
    # trading_snapshots if no dispatch-round emit has fired in the
    # last interval_secs. The APScheduler job is unaffected; if it is
    # healthy, the per-minute dedupe bucket in
    # emit_market_snapshots_batch_outcome merges duplicates.
    market_snapshots: dict[str, Any]
    if run_market_snapshots_watchdog:
        try:
            market_snapshots = _maybe_run_dispatch_market_snapshots(
                db, user_id=user_id,
            )
        except Exception as _ms_err:
            logger.warning(
                "%s dispatch market_snapshots watchdog failed: %s",
                LOG_PREFIX, _ms_err, exc_info=True,
            )
            market_snapshots = {
                "ok": False,
                "skipped": False,
                "error": str(_ms_err)[:500],
            }
    else:
        market_snapshots = {
            "ok": True,
            "skipped": True,
            "reason": "disabled_by_caller",
        }

    return {
        "ok": True,
        "processed": processed,
        "claimed": claimed_total,
        "per_type": per_type,
        "errors": errors,
        "stale_leases_released": stale_leases_released,
        "dead_letter_recovery": dead_letter_recovery,
        "duplicate_open_work": duplicate_open_work,
        "thin_evidence_sweep": thin_evidence_sweep,
        "market_snapshots": market_snapshots,
    }


def _maybe_run_dispatch_market_snapshots(
    db: Session,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Watchdog: emit a market_snapshots_batch if the last dispatch-
    round emit was longer than ``chili_brain_dispatch_market_snapshots_interval_secs``
    seconds ago.

    Returns a result dict surfaced in the round's return payload so ops
    can grep dispatch behaviour (e.g.
    ``[brain_work_dispatch] dispatch_market_snapshots emitted daily=N
    intra=M universe_size=K``).

    Skips with ``skipped=True`` and a reason when:
      * The watchdog is disabled via
        ``chili_brain_dispatch_market_snapshots_enabled=False``.
      * The interval gate hasn't expired yet.
    """
    global _LAST_DISPATCH_MARKET_SNAPSHOTS_AT

    enabled = bool(
        getattr(settings, "chili_brain_dispatch_market_snapshots_enabled", True)
    )
    if not enabled:
        return {"ok": True, "skipped": True, "reason": "disabled_by_setting"}

    interval_secs = max(0, int(
        getattr(settings, "chili_brain_dispatch_market_snapshots_interval_secs", 900)
    ))

    import time as _time
    now = _time.time()
    if interval_secs > 0 and (now - _LAST_DISPATCH_MARKET_SNAPSHOTS_AT) < interval_secs:
        remaining = int(interval_secs - (now - _LAST_DISPATCH_MARKET_SNAPSHOTS_AT))
        return {
            "ok": True,
            "skipped": True,
            "reason": "interval_gate",
            "remaining_secs": remaining,
        }

    # Mark ATTEMPTED-at before the call so a crash doesn't hot-loop the
    # dispatch round trying to re-run a heavy snapshot job.
    _LAST_DISPATCH_MARKET_SNAPSHOTS_AT = now

    try:
        from ..learning import run_scheduled_market_snapshots
        from .emitters import emit_market_snapshots_batch_outcome
    except Exception as exc:
        return {"ok": False, "skipped": False, "error": f"import_failed: {exc!s:.200}"}

    uid = user_id
    if uid is None:
        uid = getattr(settings, "brain_default_user_id", None)

    out = run_scheduled_market_snapshots(db, uid)

    daily = int(out.get("snapshots_taken_daily") or 0)
    intra = int(out.get("intraday_snapshots_taken") or 0)
    universe_size = int(out.get("universe_size") or 0)

    try:
        emit_market_snapshots_batch_outcome(
            db,
            daily=daily,
            intraday=intra,
            universe_size=universe_size,
            job_id=None,  # dispatch path -- per-minute dedupe bucket key
            snapshot_driver=out.get("snapshot_driver"),
        )
        db.commit()
    except Exception as exc:
        logger.warning(
            "%s dispatch market_snapshots emit failed: %s",
            LOG_PREFIX, exc, exc_info=True,
        )

    logger.info(
        "%s dispatch_market_snapshots emitted daily=%d intra=%d universe_size=%d",
        LOG_PREFIX, daily, intra, universe_size,
    )

    return {
        "ok": True,
        "skipped": False,
        "snapshots_taken_daily": daily,
        "intraday_snapshots_taken": intra,
        "universe_size": universe_size,
        "snapshot_driver": out.get("snapshot_driver"),
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
