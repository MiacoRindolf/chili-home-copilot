"""Phase G.2 - bracket writer (ACTIVE).

The Phase G sweep is read-only: it classifies drift but never repairs.
Phase G.2 closes that gap by letting the reconciler act on specific
classification outcomes - resize a stop to match a partial fill, place
a missing server-side stop, cancel an orphan.

**Default ON.** ``chili_bracket_writer_g2_enabled`` and the per-action
flags all default True. Kill switches remain available via env:

  * ``CHILI_BRACKET_WRITER_G2_ENABLED=0`` - disable the whole module.
  * ``CHILI_BRACKET_WRITER_G2_PARTIAL_FILL_RESIZE=0`` - disable just the
    partial-fill stop resize path.
  * ``CHILI_BRACKET_WRITER_G2_PLACE_MISSING_STOP=0`` - disable just the
    missing-stop placement path.

**Actions covered:**

* ``resize_stop_for_partial_fill`` - given a ``qty_drift`` decision
  with ``drift_kind=partial_fill``, cancel the current broker stop and
  place a new stop sized to ``expected_stop_qty`` (the broker-reported
  quantity).
* ``place_missing_stop`` - given a ``missing_stop`` decision on an
  open trade, submit a new STOP-LOSS ORDER at the local intent's
  ``stop_price`` (triggers as a market order on price breach).

**Stop-order primitive (Round 23):** the writer routes through
``RobinhoodSpotAdapter.place_stop_loss_sell_order`` which submits a real
broker stop with ``trigger='stop'`` + ``orderType='market'``. Earlier
versions of this scaffold routed through ``place_limit_order_gtc``,
which placed a marketable sell-limit at stop_price - that immediately
exits the position at current bid when bid > stop, instead of triggering
on a downside break. The new primitive rests at the broker until the
trigger price is touched, then converts to a market order for guaranteed
fill on a fast drop.

**Audit trail:** every writer call emits one or more
``trading_execution_events`` rows via ``record_execution_event``. Status
values:

* ``submitting`` - immediately before the broker call (so a hung broker
  call is still visible in the audit table).
* ``submitted`` - broker accepted, returned an order_id.
* ``rejected`` - broker rejected outright.
* ``unprotected`` - the dangerous race where cancel succeeded but the
  replacement place failed; the position is currently exposed.
* ``skipped`` - flag-disabled or invalid-decision early returns are NOT
  recorded (they happen many times per sweep on healthy data).

**Actions explicitly NOT covered (scope guard):**

* Target/take-profit placement - separate feature, separate flag.
* Orphan-stop cancellation - operator-confirmed only; automating
  broker cancellations on trades we no longer own needs a separate
  design review.
* Venues other than Robinhood equities. Coinbase stop-order mechanics
  differ materially; we'll add them in a follow-up once equities land.

**Authority contract:**

The writer is only called from within the reconciliation sweep's
post-classification hook; it inherits the sweep's session and does not
create its own. Every action records a ``trading_execution_events`` row
(via the existing ``record_execution_event`` surface) so the audit
trail and venue-health metrics stay accurate.

**Operational guardrails** (monitoring, not gating):

* Every writer action logs one line. ``resize`` failures after a
  successful cancel log CRITICAL - the position is unprotected for
  the window between cancel ACK and the next sweep.
* The drift escalation watchdog (``drift_escalation_watchdog.py``)
  catches the case where a writer action keeps failing against the
  same intent - consecutive same-kind classifications escalate once
  the watchdog flag is enabled.
* The execution-event lag gauge catches stale broker state that
  would make the writer act on outdated data.

FIX 52 (2026-05-01) - per-intent terminal-reject cooldown:

When Robinhood rejects a SELL_STOP with "Not enough shares to sell" (or
other terminal-class messages: instrument suspended, fractional-share
restriction, T+1 settlement violation, etc.), we mark the intent as
cooled-down for ``_TERMINAL_REJECT_COOLDOWN_SECS`` (1 hour). Subsequent
sweep ticks within that window skip the placement entirely instead of
producing another reject + Robinhood user notification. This complements
the pre-flight ``broker_qty`` check in the reconciler (which catches
the case where ``BrokerView.position_quantity`` is zero or stale-low);
the cooldown handles the leftover case where the BrokerView reports
plenty of shares but the broker still rejects. One rejection is enough
to start a storm; the cooldown stops it at one.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from ...config import settings
from .bracket_reconciler import ReconciliationDecision
from .ops_log_prefixes import BRACKET_WRITER_G2

logger = logging.getLogger(__name__)


# FIX 52 (2026-05-01) — per-intent rejection cooldown. Keyed on
# bracket_intent_id; value is the unix timestamp at which the cooldown
# expires (``time.time() + _TERMINAL_REJECT_COOLDOWN``). The set is
# in-process and intentionally not persisted: a container restart should
# allow ONE retry to confirm the rejection is still happening before
# re-arming the cooldown. That one retry is bounded — it produces at
# most one rejection per restart, not a storm.
_TERMINAL_REJECT_COOLDOWN_SECS = 3600  # 1h
_intent_reject_cooldown: dict[int, float] = {}

# Substrings of broker error messages that we treat as terminal-class
# (won't self-resolve in 1-2 minutes — retrying just spams the broker
# and triggers user-visible reject notifications).
_TERMINAL_REJECT_PATTERNS = (
    "not enough shares",   # Robinhood "Not enough shares to sell."
    "insufficient shares",
    "instrument suspended",
    "instrument is not allowed",
    "uncovered",            # uncovered short
    "fractional",           # fractional restriction (Robinhood doesn't allow stop-loss on fractionals)
    "settlement",           # T+1 settlement violation messages
    "good faith violation",
)


def _is_terminal_reject(error_text: str | None) -> bool:
    """Return True for reject reasons we should not retry within an hour."""
    if not error_text:
        return False
    needle = str(error_text).lower()
    return any(pat in needle for pat in _TERMINAL_REJECT_PATTERNS)


def _is_in_reject_cooldown(bracket_intent_id: int) -> bool:
    """Return True if this intent is within the terminal-reject cooldown."""
    until = _intent_reject_cooldown.get(int(bracket_intent_id))
    if until is None:
        return False
    if time.time() >= until:
        # Expired — drop the key so the dict doesn't grow forever.
        _intent_reject_cooldown.pop(int(bracket_intent_id), None)
        return False
    return True


def _arm_reject_cooldown(bracket_intent_id: int) -> None:
    _intent_reject_cooldown[int(bracket_intent_id)] = (
        time.time() + _TERMINAL_REJECT_COOLDOWN_SECS
    )


# FIX 53 (2026-05-01) — post-placement cooldown.
#
# When the broker accepts a stop placement (returns ok=true with an
# order_id) but the order doesn't actually persist as a resting stop
# (Robinhood may auto-cancel unconfirmed orders, or the order goes to
# 'unconfirmed' and never confirms), the next 60-second sweep classifies
# the intent as missing_stop again and places ANOTHER stop. The user
# sees a Robinhood notification per placement-then-cancel cycle. The
# 60-second sweep cadence is faster than the broker's confirm/cancel
# cycle, so without a post-placement cooldown we churn 60+ orders per
# hour against the same intent.
#
# Five-minute cooldown after a successful placement gives the broker
# time to confirm or auto-cancel before we retry. If the stop genuinely
# didn't take, the next attempt happens 5 min later, not 1 min later --
# 12x reduction in placement spam without giving up on the intent.
_POST_PLACE_COOLDOWN_SECS = 300  # 5 min
_intent_post_place_cooldown: dict[int, float] = {}


def _is_in_post_place_cooldown(bracket_intent_id: int) -> bool:
    until = _intent_post_place_cooldown.get(int(bracket_intent_id))
    if until is None:
        return False
    if time.time() >= until:
        _intent_post_place_cooldown.pop(int(bracket_intent_id), None)
        return False
    return True


def _arm_post_place_cooldown(bracket_intent_id: int) -> None:
    _intent_post_place_cooldown[int(bracket_intent_id)] = (
        time.time() + _POST_PLACE_COOLDOWN_SECS
    )


# Feature flags (all default True per the module docstring); any one
# being off disables the corresponding action even when the top-level
# flag is on.

def _top_level_enabled() -> bool:
    return bool(getattr(settings, "chili_bracket_writer_g2_enabled", False))


def _partial_fill_resize_enabled() -> bool:
    if not _top_level_enabled():
        return False
    return bool(getattr(settings, "chili_bracket_writer_g2_partial_fill_resize", False))


def _place_missing_stop_enabled() -> bool:
    if not _top_level_enabled():
        return False
    return bool(getattr(settings, "chili_bracket_writer_g2_place_missing_stop", False))


# The writer only handles Robinhood equities in this scaffold (see
# module docstring). Guarding explicitly means a change that adds
# Coinbase later can't accidentally trip on a stale Robinhood code path.

_SUPPORTED_VENUES = frozenset({"robinhood"})


@dataclass(frozen=True)
class WriterAction:
    """Result of a single writer call. Never raises; a failed action is
    reported by :attr:`ok=False` + :attr:`reason` so the sweep can log
    it and move on."""

    action: str            # 'resize_stop_for_partial_fill' | 'place_missing_stop' | 'noop'
    ok: bool
    reason: str            # 'disabled' | 'unsupported_venue' | 'invalid_decision' |
                           # 'cancel_failed' | 'place_failed' | 'unprotected' |
                           # 'ok' | 'dry_run'
    broker_source: Optional[str] = None
    ticker: Optional[str] = None
    prior_stop_order_id: Optional[str] = None
    new_stop_order_id: Optional[str] = None
    new_stop_qty: Optional[float] = None
    new_stop_price: Optional[float] = None
    raw_broker_response: dict[str, Any] = field(default_factory=dict)


# Adapter factory (injectable for tests)

AdapterFactory = Callable[[str], Any]


def _default_adapter_factory(broker_source: str) -> Any:
    """Return a live VenueAdapter instance for ``broker_source``."""
    from .venue.factory import get_adapter

    adapter = get_adapter(broker_source)
    if adapter is None:
        raise ValueError(f"unsupported broker_source for Phase G.2 writer: {broker_source!r}")
    return adapter


# Audit helper

def _g2_event(
    db: Session,
    *,
    trade_id: int,
    bracket_intent_id: int,
    ticker: str,
    broker_source: str,
    event_type: str,
    status: str,
    new_stop_order_id: Optional[str] = None,
    prior_stop_order_id: Optional[str] = None,
    qty: Optional[float] = None,
    stop_price: Optional[float] = None,
    error: Optional[str] = None,
    decision_kind: Optional[str] = None,
    decision_severity: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Write one ``trading_execution_events`` row for a writer action.

    Failures here are swallowed so the writer's broker-side outcome
    never depends on the audit insert. The execution event is best-effort
    enrichment; the source of truth for "did the broker accept this?"
    is the WriterAction return value.
    """
    try:
        from .execution_audit import record_execution_event
        from ...models.trading import Trade

        # Look up the trade only to extract user_id + scan_pattern_id for the
        # event-row enrichment. We deliberately DO NOT pass ``trade=`` to
        # ``record_execution_event`` because that triggers
        # ``apply_execution_event_to_trade``, which interprets event status
        # values like 'rejected' / 'submitted' as broker-state transitions
        # on the underlying Trade row. The writer's status reflects the
        # STOP-ORDER placement outcome, not the trade's own broker state --
        # letting that helper fire would corrupt Trade.status (it bit us
        # on 2026-04-30 when 3 trades got flipped to 'rejected' the moment
        # the writer first ran).
        user_id = None
        scan_pattern_id = None
        try:
            if trade_id is not None:
                t = db.get(Trade, int(trade_id))
                if t is not None:
                    user_id = getattr(t, "user_id", None)
                    scan_pattern_id = getattr(t, "scan_pattern_id", None)
        except Exception:
            user_id = None
            scan_pattern_id = None

        payload = {
            "bracket_intent_id": bracket_intent_id,
            "decision_kind": decision_kind,
            "decision_severity": decision_severity,
            "prior_stop_order_id": prior_stop_order_id,
            "new_stop_order_id": new_stop_order_id,
            "stop_price": stop_price,
            "qty": qty,
            "error": error,
            "trade_id": trade_id,
        }
        if extra:
            payload.update(extra)

        record_execution_event(
            db,
            user_id=user_id,
            ticker=ticker,
            trade=None,  # MUST stay None -- see comment above.
            scan_pattern_id=scan_pattern_id,
            broker_source=broker_source,
            order_id=new_stop_order_id,
            event_type=event_type,
            status=status,
            requested_quantity=float(qty) if qty is not None else None,
            reference_price=float(stop_price) if stop_price is not None else None,
            payload_json=payload,
        )
    except Exception:
        logger.warning(
            f"{BRACKET_WRITER_G2} record_execution_event failed for "
            f"intent=%s event_type=%s",
            bracket_intent_id, event_type, exc_info=True,
        )


def _build_coid(prefix: str, bracket_intent_id: int, qty: float) -> str:
    """Generate a non-colliding client_order_id.

    Earlier versions hashed only ``intent_id + qty`` which produced the
    same coid every sweep -> ``idempotency_store.is_duplicate=True`` ->
    placement loop on every retry. Including the current epoch second
    breaks that collision; the idempotency store still rejects same-call
    retries within the same second (which is the legitimate dedupe case).
    """
    return f"g2-{prefix}-{int(bracket_intent_id)}-{int(float(qty) * 1e6)}-{int(time.time())}"


# Public entry points


def resize_stop_for_partial_fill(
    db: Session,
    *,
    trade_id: int,
    bracket_intent_id: int,
    ticker: str,
    broker_source: str,
    decision: ReconciliationDecision,
    prior_stop_order_id: Optional[str],
    stop_price: float,
    adapter_factory: AdapterFactory = _default_adapter_factory,
) -> WriterAction:
    """Cancel the current stop and re-place it at ``expected_stop_qty``.

    Only fires when ``decision.delta_payload["drift_kind"] == "partial_fill"``
    and the per-action flag is enabled. The order of operations is:

    1. Validate input (decision kind, venue, flags).
    2. Cancel the existing stop at the broker.
    3. Place a new stop at ``stop_price`` for ``expected_stop_qty``.
    4. Record both actions via ``record_execution_event``.

    Step 2 must succeed before step 3 fires - we never leave TWO working
    stops on the same ticker, which would over-hedge and could cause
    both to fire during a drawdown.
    """
    if not _partial_fill_resize_enabled():
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False, reason="disabled",
            broker_source=broker_source, ticker=ticker,
            prior_stop_order_id=prior_stop_order_id,
        )

    if decision.kind != "qty_drift":
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False, reason="invalid_decision",
            broker_source=broker_source, ticker=ticker,
        )
    payload = decision.delta_payload or {}
    if payload.get("drift_kind") != "partial_fill":
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False, reason="invalid_decision",
            broker_source=broker_source, ticker=ticker,
        )
    expected_qty = payload.get("expected_stop_qty")
    if expected_qty is None or float(expected_qty) <= 0:
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False, reason="invalid_decision",
            broker_source=broker_source, ticker=ticker,
        )

    if (broker_source or "").lower() not in _SUPPORTED_VENUES:
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False, reason="unsupported_venue",
            broker_source=broker_source, ticker=ticker,
        )

    if not prior_stop_order_id:
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False,
            reason="invalid_decision",
            broker_source=broker_source, ticker=ticker,
        )

    adapter = adapter_factory(broker_source)

    _g2_event(
        db,
        trade_id=trade_id, bracket_intent_id=bracket_intent_id,
        ticker=ticker, broker_source=broker_source,
        event_type="g2_resize_stop_submitting", status="submitting",
        prior_stop_order_id=prior_stop_order_id,
        qty=float(expected_qty), stop_price=float(stop_price),
        decision_kind=decision.kind, decision_severity=decision.severity,
    )

    # Cancel the existing stop.
    try:
        cancel_res = adapter.cancel_order(prior_stop_order_id) or {}
    except Exception as exc:
        logger.warning(
            f"{BRACKET_WRITER_G2} cancel_order raised for intent=%s order=%s: %s",
            bracket_intent_id, prior_stop_order_id, exc, exc_info=True,
        )
        _g2_event(
            db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
            ticker=ticker, broker_source=broker_source,
            event_type="g2_resize_cancel_failed", status="rejected",
            prior_stop_order_id=prior_stop_order_id,
            qty=float(expected_qty), stop_price=float(stop_price),
            error=str(exc)[:500],
        )
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False, reason="cancel_failed",
            broker_source=broker_source, ticker=ticker,
            prior_stop_order_id=prior_stop_order_id,
        )
    if not cancel_res.get("ok"):
        logger.warning(
            f"{BRACKET_WRITER_G2} cancel failed intent=%s order=%s error=%s",
            bracket_intent_id, prior_stop_order_id, cancel_res.get("error"),
        )
        _g2_event(
            db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
            ticker=ticker, broker_source=broker_source,
            event_type="g2_resize_cancel_failed", status="rejected",
            prior_stop_order_id=prior_stop_order_id,
            qty=float(expected_qty), stop_price=float(stop_price),
            error=str(cancel_res.get("error") or "")[:500],
        )
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False, reason="cancel_failed",
            broker_source=broker_source, ticker=ticker,
            prior_stop_order_id=prior_stop_order_id,
            raw_broker_response=cancel_res,
        )

    # Place the resized stop using the real stop-loss primitive.
    client_oid = _build_coid("resize", bracket_intent_id, float(expected_qty))
    try:
        place_res = adapter.place_stop_loss_sell_order(
            product_id=ticker,
            base_size=str(float(expected_qty)),
            trigger_price=str(float(stop_price)),
            client_order_id=client_oid,
        )
    except Exception as exc:
        logger.warning(
            f"{BRACKET_WRITER_G2} place_stop_loss_sell_order raised for intent=%s: %s",
            bracket_intent_id, exc, exc_info=True,
        )
        _g2_event(
            db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
            ticker=ticker, broker_source=broker_source,
            event_type="g2_resize_unprotected_window", status="unprotected",
            prior_stop_order_id=prior_stop_order_id,
            qty=float(expected_qty), stop_price=float(stop_price),
            error=str(exc)[:500],
            extra={"phase": "place_after_cancel"},
        )
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False, reason="place_failed",
            broker_source=broker_source, ticker=ticker,
            prior_stop_order_id=prior_stop_order_id,
        )
    if not place_res.get("ok"):
        logger.critical(
            f"{BRACKET_WRITER_G2} PRIOR STOP CANCELLED BUT REPLACEMENT FAILED "
            "intent=%s order=%s error=%s - position is currently unprotected",
            bracket_intent_id, prior_stop_order_id, place_res.get("error"),
        )
        _g2_event(
            db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
            ticker=ticker, broker_source=broker_source,
            event_type="g2_resize_unprotected_window", status="unprotected",
            prior_stop_order_id=prior_stop_order_id,
            qty=float(expected_qty), stop_price=float(stop_price),
            error=str(place_res.get("error") or "")[:500],
            extra={"phase": "place_after_cancel"},
        )
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False, reason="place_failed",
            broker_source=broker_source, ticker=ticker,
            prior_stop_order_id=prior_stop_order_id,
            raw_broker_response=place_res,
        )

    new_oid = place_res.get("order_id") or ""
    logger.info(
        f"{BRACKET_WRITER_G2} resize_stop intent=%s ticker=%s qty=%s price=%s "
        "old=%s new=%s",
        bracket_intent_id, ticker, expected_qty, stop_price,
        prior_stop_order_id, new_oid,
    )
    _g2_event(
        db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
        ticker=ticker, broker_source=broker_source,
        event_type="g2_resize_stop_submitted", status="submitted",
        new_stop_order_id=new_oid, prior_stop_order_id=prior_stop_order_id,
        qty=float(expected_qty), stop_price=float(stop_price),
    )
    return WriterAction(
        action="resize_stop_for_partial_fill", ok=True, reason="ok",
        broker_source=broker_source, ticker=ticker,
        prior_stop_order_id=prior_stop_order_id,
        new_stop_order_id=new_oid,
        new_stop_qty=float(expected_qty),
        new_stop_price=float(stop_price),
        raw_broker_response=place_res,
    )


def place_missing_stop(
    db: Session,
    *,
    trade_id: int,
    bracket_intent_id: int,
    ticker: str,
    broker_source: str,
    decision: ReconciliationDecision,
    local_quantity: float,
    stop_price: float,
    adapter_factory: AdapterFactory = _default_adapter_factory,
) -> WriterAction:
    """Place a server-side stop for an open trade that has no broker
    stop (missing_stop classification).

    Guard rails:

    * Flag must be on.
    * Decision must be ``missing_stop``.
    * Venue must be supported.
    * ``local_quantity`` and ``stop_price`` must be positive.

    Uses the real stop-loss primitive
    (``place_stop_loss_sell_order``), NOT a marketable sell-limit. The
    order rests at the broker and triggers on a downside break of
    ``stop_price``.
    """
    if not _place_missing_stop_enabled():
        return WriterAction(
            action="place_missing_stop", ok=False, reason="disabled",
            broker_source=broker_source, ticker=ticker,
        )

    if decision.kind != "missing_stop":
        return WriterAction(
            action="place_missing_stop", ok=False, reason="invalid_decision",
            broker_source=broker_source, ticker=ticker,
        )

    # FIX 52 (2026-05-01) — short-circuit if this intent had a
    # terminal-class rejection recently. See module docstring.
    if _is_in_reject_cooldown(bracket_intent_id):
        # Skipped early-returns aren't audited (per the docstring's "skipped"
        # rule); just log so the operator can see the cooldown in effect.
        logger.info(
            f"{BRACKET_WRITER_G2} place_missing_stop SKIPPED intent=%s "
            "ticker=%s reason=in_reject_cooldown",
            bracket_intent_id, ticker,
        )
        return WriterAction(
            action="place_missing_stop", ok=False, reason="in_reject_cooldown",
            broker_source=broker_source, ticker=ticker,
        )

    # FIX 53 (2026-05-01) — short-circuit if we placed an order recently
    # for this same intent. Lets the broker confirm or auto-cancel before
    # we retry; otherwise the 60s sweep cadence outruns the broker's
    # confirm cycle and we churn orders that all get cancelled.
    if _is_in_post_place_cooldown(bracket_intent_id):
        logger.info(
            f"{BRACKET_WRITER_G2} place_missing_stop SKIPPED intent=%s "
            "ticker=%s reason=in_post_place_cooldown",
            bracket_intent_id, ticker,
        )
        return WriterAction(
            action="place_missing_stop", ok=False,
            reason="in_post_place_cooldown",
            broker_source=broker_source, ticker=ticker,
        )

    if (broker_source or "").lower() not in _SUPPORTED_VENUES:
        return WriterAction(
            action="place_missing_stop", ok=False, reason="unsupported_venue",
            broker_source=broker_source, ticker=ticker,
        )

    if local_quantity is None or float(local_quantity) <= 0:
        return WriterAction(
            action="place_missing_stop", ok=False, reason="invalid_decision",
            broker_source=broker_source, ticker=ticker,
        )
    if stop_price is None or float(stop_price) <= 0:
        return WriterAction(
            action="place_missing_stop", ok=False, reason="invalid_decision",
            broker_source=broker_source, ticker=ticker,
        )

    # FIX 51 (2026-05-01) — last-mile broker-quantity guard.
    #
    # The reconciler's _invoke_writer_for_decision already pre-flights the
    # broker share count (skip if zero, cap if low). This second-line check
    # protects callers that bypass the reconciler — tests, ad-hoc scripts,
    # any future caller that forgets the pre-flight. We can't reach the
    # BrokerView from here so we re-query positions through the adapter.
    # If the broker actually has zero shares, fail fast with a structured
    # reason so the caller doesn't paper over the rejection as a generic
    # ``place_failed``.
    adapter = adapter_factory(broker_source)
    try:
        positions = adapter.get_positions() or {}
        # Adapter shape: {ticker: {"quantity": float, ...}, ...}.
        pos_entry = positions.get(ticker) or positions.get(str(ticker).upper()) or {}
        broker_qty_raw = (
            pos_entry.get("quantity") if isinstance(pos_entry, dict) else None
        )
        broker_qty = float(broker_qty_raw) if broker_qty_raw is not None else None
    except Exception:
        # If we can't read positions, defer to the broker — old behavior.
        # The broker will reject "Not enough shares" if applicable; we won't
        # block a legitimate placement just because the position-read failed.
        broker_qty = None

    if broker_qty is not None and broker_qty <= 0:
        # Per module docstring, skipped early-returns do not record an audit
        # row (they would fire every sweep on the same orphan intent and
        # drown the audit log). The structured log line below is the
        # operator-visible signal.
        logger.warning(
            f"{BRACKET_WRITER_G2} place_missing_stop SKIPPED intent=%s "
            "ticker=%s reason=broker_qty_zero local_qty=%s",
            bracket_intent_id, ticker, local_quantity,
        )
        return WriterAction(
            action="place_missing_stop", ok=False, reason="broker_qty_zero",
            broker_source=broker_source, ticker=ticker,
        )
    if broker_qty is not None and broker_qty < float(local_quantity):
        logger.warning(
            f"{BRACKET_WRITER_G2} place_missing_stop capping qty intent=%s "
            "ticker=%s local_qty=%s broker_qty=%s",
            bracket_intent_id, ticker, local_quantity, broker_qty,
        )
        local_quantity = broker_qty

    client_oid = _build_coid("miss", bracket_intent_id, float(local_quantity))

    _g2_event(
        db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
        ticker=ticker, broker_source=broker_source,
        event_type="g2_place_missing_stop_submitting", status="submitting",
        qty=float(local_quantity), stop_price=float(stop_price),
        decision_kind=decision.kind, decision_severity=decision.severity,
    )
    try:
        place_res = adapter.place_stop_loss_sell_order(
            product_id=ticker,
            base_size=str(float(local_quantity)),
            trigger_price=str(float(stop_price)),
            client_order_id=client_oid,
        )
    except Exception as exc:
        logger.warning(
            f"{BRACKET_WRITER_G2} place_missing_stop raised for intent=%s: %s",
            bracket_intent_id, exc, exc_info=True,
        )
        _g2_event(
            db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
            ticker=ticker, broker_source=broker_source,
            event_type="g2_place_missing_stop_rejected", status="rejected",
            qty=float(local_quantity), stop_price=float(stop_price),
            error=str(exc)[:500],
        )
        return WriterAction(
            action="place_missing_stop", ok=False, reason="place_failed",
            broker_source=broker_source, ticker=ticker,
        )
    if not place_res.get("ok"):
        err_text = str(place_res.get("error") or "")
        terminal = _is_terminal_reject(err_text)
        if terminal:
            # FIX 52 — arm the cooldown so the next 1h of sweeps skip
            # this intent instead of producing another reject + Robinhood
            # notification.
            _arm_reject_cooldown(bracket_intent_id)
            logger.warning(
                f"{BRACKET_WRITER_G2} place_missing_stop terminal reject; "
                "arming %ss cooldown intent=%s ticker=%s err=%s",
                _TERMINAL_REJECT_COOLDOWN_SECS, bracket_intent_id, ticker,
                err_text[:200],
            )
        else:
            logger.warning(
                f"{BRACKET_WRITER_G2} place_missing_stop broker error intent=%s: %s",
                bracket_intent_id, err_text[:200],
            )
        _g2_event(
            db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
            ticker=ticker, broker_source=broker_source,
            event_type="g2_place_missing_stop_rejected", status="rejected",
            qty=float(local_quantity), stop_price=float(stop_price),
            error=err_text[:500],
            extra={"terminal_reject": terminal},
        )
        return WriterAction(
            action="place_missing_stop", ok=False,
            reason="terminal_reject" if terminal else "place_failed",
            broker_source=broker_source, ticker=ticker,
            raw_broker_response=place_res,
        )

    new_oid = place_res.get("order_id") or ""
    logger.info(
        f"{BRACKET_WRITER_G2} place_missing_stop intent=%s ticker=%s qty=%s "
        "price=%s new_order=%s",
        bracket_intent_id, ticker, local_quantity, stop_price, new_oid,
    )
    # FIX 53 — arm post-placement cooldown so we don't bombard the
    # broker with duplicate stops faster than it can confirm them.
    _arm_post_place_cooldown(bracket_intent_id)
    _g2_event(
        db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
        ticker=ticker, broker_source=broker_source,
        event_type="g2_place_missing_stop_submitted", status="submitted",
        new_stop_order_id=new_oid,
        qty=float(local_quantity), stop_price=float(stop_price),
    )
    return WriterAction(
        action="place_missing_stop", ok=True, reason="ok",
        broker_source=broker_source, ticker=ticker,
        new_stop_order_id=new_oid,
        new_stop_qty=float(local_quantity),
        new_stop_price=float(stop_price),
        raw_broker_response=place_res,
    )


__all__ = [
    "WriterAction",
    "place_missing_stop",
    "resize_stop_for_partial_fill",
]
