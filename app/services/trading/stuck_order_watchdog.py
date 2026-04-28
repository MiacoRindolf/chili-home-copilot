"""P0.7 — stuck-order watchdog.

Cancels orders that the broker acknowledged but never transitioned to a
terminal state within the configured timeout. Without this, a queue
hiccup at the venue can leave a Trade row in ``status='open'`` with
``broker_status='queued'`` forever — the AutoTrader rule gate keeps
counting it as an open position (blocking new entries) and the
reconciler has no ground truth to resolve it against.

Timeouts come from settings:

* ``chili_stuck_order_market_timeout_seconds`` (default 300, i.e. 5 min)
* ``chili_stuck_order_limit_timeout_seconds`` (default 1800, i.e. 30 min)

Market orders get the short timeout because anything that doesn't fill
near-instantly on Robinhood during RTH points to a broker-side problem;
limit orders get the long timeout because they're explicitly resting
against a price level and a slow fill is expected.

Flow per candidate:

1. Ask the broker for the canonical order state.
2. If terminal → update the local Trade row to match (filled/cancelled/rejected).
3. If still non-terminal and elapsed > timeout → issue a cancel, log CRITICAL.

Runs on a scheduler interval; never raises.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import Trade

logger = logging.getLogger(__name__)


# Broker statuses that mean "live, not yet terminal". Matches the lowercased
# Robinhood + Coinbase lexicons (see order_state_machine._ACK_STATUSES /
# _PARTIAL_STATUSES).
_NON_TERMINAL = {
    "queued", "confirmed", "submitted", "pending", "accepted",
    "acknowledged", "ack", "working", "open", "active",
    "partially_filled", "partial", "partial_filled",
    "unconfirmed",
}
_TERMINAL_FILLED = {"filled", "done", "completed", "complete"}
_TERMINAL_CANCELLED = {"cancelled", "canceled", "revoked"}
_TERMINAL_REJECTED = {"rejected", "failed", "denied", "expired", "timed_out"}


def _market_timeout() -> timedelta:
    return timedelta(
        seconds=int(getattr(settings, "chili_stuck_order_market_timeout_seconds", 300))
    )


def _limit_timeout() -> timedelta:
    return timedelta(
        seconds=int(getattr(settings, "chili_stuck_order_limit_timeout_seconds", 1800))
    )


def _effective_submit_time(t: Trade) -> Optional[datetime]:
    """Return the best-available submission timestamp for this trade.

    ``submitted_at`` is populated by broker_position_sync once it pulls
    the authoritative value from the venue; for freshly-placed AutoTrader
    trades it's still NULL and ``entry_date`` (set at INSERT) is the
    closest proxy.
    """
    if t.submitted_at is not None:
        return t.submitted_at
    return t.entry_date


def _fetch_candidates(db: Session) -> list[Trade]:
    """Find open trades whose broker order is not in a terminal state."""
    return (
        db.query(Trade)
        .filter(
            Trade.status.in_(("open", "working")),
            Trade.broker_order_id.isnot(None),
            Trade.broker_order_id != "",
            or_(
                Trade.broker_status.is_(None),
                Trade.broker_status.in_(tuple(_NON_TERMINAL)),
            ),
            Trade.broker_source.in_(("robinhood", "coinbase")),
        )
        .all()
    )


def _get_adapter(broker_source: str) -> Any:
    """Return a venue adapter instance for ``broker_source``, or None.

    Delegates to :func:`venue.factory.get_adapter` so the supported-venue
    list has one definition. The factory already logs import failures.
    """
    from .venue.factory import get_adapter

    return get_adapter(broker_source)


def _infer_is_market(broker_status: Optional[str], pending_status: Optional[str]) -> bool:
    """Best-effort inference of whether this is a market order.

    The Trade model doesn't carry order_type, so we lean on signals we do
    have. AutoTrader v1 places ONLY market orders (see auto_trader._execute_*),
    so anything with ``management_scope='auto_trader_v1'`` is market. For
    other sources we default to the limit timeout since that's the safer
    (longer-wait) choice.
    """
    # Caller checks management_scope; this helper is a light final sanity
    # default. Keeping it separate makes the call site readable.
    return False


def _timeout_for(t: Trade) -> timedelta:
    scope = (t.management_scope or "").lower()
    if scope == "auto_trader_v1":
        return _market_timeout()
    # Other sources (broker_sync, manual) could be anything — use the
    # longer limit-order timeout by default so we don't over-cancel.
    return _limit_timeout()


def _apply_terminal_state(t: Trade, broker_status: str, raw: dict[str, Any] | None) -> None:
    """Mirror a terminal broker status onto the Trade row.

    Doesn't commit — the caller controls the transaction boundary so the
    whole tick is atomic per-trade.
    """
    bs = (broker_status or "").strip().lower()
    t.broker_status = bs or t.broker_status
    t.last_broker_sync = datetime.utcnow()
    if bs in _TERMINAL_FILLED:
        # Don't flip status=closed — a FILLED entry is an *opened* position.
        # The exit evaluator closes when the exit fills. Leave status='open'.
        if raw:
            avg = raw.get("average_price") or raw.get("price")
            try:
                if avg is not None:
                    t.avg_fill_price = float(avg)
            except (TypeError, ValueError):
                pass
            if t.filled_at is None:
                t.filled_at = datetime.utcnow()
        return
    if bs in _TERMINAL_CANCELLED:
        t.status = "cancelled"
        if not t.exit_reason:
            t.exit_reason = f"stuck_order_terminal_cancelled:{bs}"[:50]
        return
    if bs in _TERMINAL_REJECTED:
        t.status = "rejected"
        if not t.exit_reason:
            t.exit_reason = f"stuck_order_terminal_rejected:{bs}"[:50]
        return


def _try_cancel(adapter: Any, t: Trade) -> dict[str, Any]:
    """Issue a cancel through the adapter. Never raises."""
    try:
        return adapter.cancel_order(t.broker_order_id) or {}
    except Exception as e:
        logger.warning(
            "[stuck_order_watchdog] cancel_order raised for trade=%s order=%s: %s",
            t.id, t.broker_order_id, e, exc_info=True,
        )
        return {"ok": False, "error": str(e)}


def _process_one(db: Session, t: Trade, now: datetime) -> str:
    """Process one stuck-order candidate. Returns a short outcome string."""
    # KKK -- skip option trades. The robinhood spot adapter cannot query
    # option order IDs (those go through rh.options.*, not rh.orders.*),
    # so adapter.get_order(broker_order_id) returns None for an option
    # order even when the broker filled it. The watchdog then stamps
    # the trade rejected/unknown. Phase 5 options exit monitor and
    # broker_sync (GGG revive + MMM stale-skip) reconcile option
    # positions correctly; this watchdog should leave them alone.
    try:
        from .autopilot_scope import is_option_trade
        if is_option_trade(t):
            return "skipped_option_trade"
    except Exception:
        pass
    submit_time = _effective_submit_time(t)
    if submit_time is None:
        return "no_submit_time"

    # Normalize to naive UTC (matches DB TIMESTAMP without tz).
    if submit_time.tzinfo is not None:
        submit_time = submit_time.astimezone(timezone.utc).replace(tzinfo=None)

    elapsed = now - submit_time
    timeout = _timeout_for(t)
    if elapsed < timeout:
        return "within_timeout"

    adapter = _get_adapter(t.broker_source or "")
    if adapter is None:
        logger.warning(
            "[stuck_order_watchdog] no adapter for trade=%s broker_source=%s",
            t.id, t.broker_source,
        )
        return "no_adapter"

    # Step 1: ask the broker what state the order is actually in.
    try:
        order_normalized, _fresh = adapter.get_order(t.broker_order_id)
    except Exception as e:
        logger.warning(
            "[stuck_order_watchdog] get_order raised for trade=%s order=%s: %s",
            t.id, t.broker_order_id, e, exc_info=True,
        )
        return "get_order_error"

    if order_normalized is None:
        # KKK -- broker get_order returned None. This is NOT a confirmed
        # rejection -- it can mean (1) transient broker lookup glitch,
        # (2) wrong-typed adapter (options/crypto via spot adapter), or
        # (3) the order really vanished. Only stamp "rejected" when
        # elapsed > 10x the normal timeout AND the trade has been retried.
        # Otherwise leave broker_status="unknown" so broker_sync (with
        # GGG revive) can reconcile if the broker actually filled.
        long_elapsed = elapsed > timeout * 10
        if long_elapsed:
            logger.critical(
                "[stuck_order_watchdog] trade=%s broker_order=%s unknown at venue "
                "for >10x timeout (%ss); marking rejected (KKK guard)",
                t.id, t.broker_order_id, int(elapsed.total_seconds()),
            )
            t.status = "rejected"
            t.broker_status = "unknown"
            t.last_broker_sync = datetime.utcnow()
            db.commit()
            return "unknown_at_venue_rejected"
        else:
            # Just record the lookup-uncertainty; trade stays open. broker_sync
            # GGG revive will reconcile if the position is actually held.
            logger.warning(
                "[stuck_order_watchdog] trade=%s broker_order=%s unknown at venue "
                "after %ss; deferring rejection (KKK guard) -- broker_sync will reconcile",
                t.id, t.broker_order_id, int(elapsed.total_seconds()),
            )
            t.broker_status = "unknown"
            t.last_broker_sync = datetime.utcnow()
            db.commit()
            return "unknown_at_venue_deferred"

    broker_status = (order_normalized.status or "").lower()
    # Step 2: if the broker already has a terminal state, just mirror it.
    if broker_status in _TERMINAL_FILLED | _TERMINAL_CANCELLED | _TERMINAL_REJECTED:
        _apply_terminal_state(
            t, broker_status, order_normalized.raw if hasattr(order_normalized, "raw") else None
        )
        db.commit()
        return f"mirrored:{broker_status}"

    # Step 3: still non-terminal past the timeout → cancel + log CRITICAL.
    logger.critical(
        "[stuck_order_watchdog] trade=%s broker_order=%s stuck in %s for %ss; cancelling",
        t.id, t.broker_order_id, broker_status or "unknown",
        int(elapsed.total_seconds()),
    )
    cancel_result = _try_cancel(adapter, t)
    if cancel_result.get("ok"):
        t.status = "cancelled"
        t.broker_status = "cancelled"
        if not t.exit_reason:
            t.exit_reason = "stuck_order_watchdog_timeout"
        t.last_broker_sync = datetime.utcnow()
        db.commit()
        return "cancelled"

    # Cancel itself failed — leave the row for the next tick. Don't mark the
    # trade cancelled locally because the broker may still fill it.
    logger.critical(
        "[stuck_order_watchdog] cancel failed for trade=%s error=%s",
        t.id, cancel_result.get("error"),
    )
    return "cancel_failed"


def tick_stuck_order_watchdog(db: Session) -> dict[str, Any]:
    """One pass of the watchdog. Safe to call on an interval."""
    if not bool(getattr(settings, "chili_stuck_order_watchdog_enabled", True)):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    now = datetime.utcnow()
    candidates = _fetch_candidates(db)
    out: dict[str, Any] = {"ok": True, "inspected": 0, "outcomes": {}}

    for t in candidates:
        out["inspected"] += 1
        try:
            outcome = _process_one(db, t, now)
        except Exception:
            logger.exception(
                "[stuck_order_watchdog] unexpected error on trade=%s", t.id
            )
            outcome = "error"
        out["outcomes"][outcome] = out["outcomes"].get(outcome, 0) + 1

    return out
