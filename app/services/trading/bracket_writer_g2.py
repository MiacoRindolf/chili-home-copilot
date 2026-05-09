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

from sqlalchemy import text
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


# f-phase-e-revert-and-bracket-writer-crash-fix (2026-05-08):
# per-intent cooldown after ANY exception inside place_missing_stop.
# The existing _intent_reject_cooldown only fires on known broker-
# side reject codes; code bugs (IndexError inside rh.orders for
# crypto tickers, etc.) did NOT arm any cooldown and re-fired every
# 60s sweep. Active crash loop on ADA/SOL since 2026-05-09 01:57 UTC
# was the audit fingerprint. Settings-tunable via
# CHILI_BRACKET_WRITER_EXCEPTION_COOLDOWN_SECS (default 300).
_intent_exception_cooldown: dict[int, float] = {}


def _exception_cooldown_secs() -> int:
    """Read at call-time so env overrides take effect on next sweep."""
    return int(
        getattr(settings, "chili_bracket_writer_exception_cooldown_secs", 300)
    )


def _is_in_exception_cooldown(bracket_intent_id: int) -> bool:
    until = _intent_exception_cooldown.get(int(bracket_intent_id))
    if until is None:
        return False
    if time.time() >= until:
        _intent_exception_cooldown.pop(int(bracket_intent_id), None)
        return False
    return True


def _arm_exception_cooldown(bracket_intent_id: int) -> None:
    _intent_exception_cooldown[int(bracket_intent_id)] = (
        time.time() + _exception_cooldown_secs()
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


# FIX 56 (2026-05-01) — auto-cancel-pattern detection.
#
# When the broker accepts a SELL_STOP placement (returns ok=true with an
# order_id) but Robinhood auto-cancels it within seconds — no
# cancel_reason exposed via the API — the next post-place cooldown
# expiry sees the same intent classified missing_stop again, places
# another stop, gets cancelled again. ELTX exhibits this pattern:
# 25 free shares, no covering sell order, yet 20 consecutive SELL_STOPs
# placed in 30 minutes were all cancelled within ~1 second of placement.
# Robinhood likely has an instrument-specific risk rule (small-cap
# biotech, low-priced, etc.) that auto-cancels stop-loss orders.
#
# Without intervention, FIX 53's 5-min post-place cooldown limits the
# spam to 12 placements/hour per intent, but the user still gets 12
# cancellation notifications per hour. FIX 56 stops it at 3 placements
# per intent then arms the 1h terminal-reject cooldown — 3
# notifications, then silence.
_PLACEMENT_FAILURE_THRESHOLD = 3
_intent_placement_count: dict[int, int] = {}


def _record_placement(bracket_intent_id: int) -> int:
    """Increment per-intent placement counter; return the new count.

    Counter is in-process; restart resets it (intentional — gives the
    operator three retries per restart to confirm the broker is still
    auto-cancelling before silencing the intent).
    """
    new = _intent_placement_count.get(int(bracket_intent_id), 0) + 1
    _intent_placement_count[int(bracket_intent_id)] = new
    return new


def _reset_placement_count(bracket_intent_id: int) -> None:
    """Clear the placement counter — used when we detect the position is
    healthy (e.g., classification flips away from missing_stop)."""
    _intent_placement_count.pop(int(bracket_intent_id), None)


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


# bracket-writer-cover-policy-clarify (2026-05-03) — startup-time
# warning emitted when the silent-exposure flag combination is in
# effect. Called from every process that may exercise the writer:
# the FastAPI app boot path and the broker-sync-worker entrypoint.
# Emits exactly one WARNING line per process; safe to call multiple
# times (idempotent — separate calls just re-emit).

def warn_if_silent_exposure(*, log: "logging.Logger | None" = None) -> bool:
    """Emit a WARNING when emergency-repair is ON and cancel-covering-sell
    is OFF — the combination that produces "rejection storm avoided,
    downside still uncovered" without any operator-visible signal at
    decision time.

    Returns True when the warning was emitted, False when the combo is
    not active (so callers can also use this as a probe).

    The warning is informational, not a misconfiguration: both flag
    values are operator choices. The function does NOT escalate to
    ERROR or fail startup. Override via the corresponding env vars.
    """
    target_log = log or logger
    repair_on = bool(getattr(settings, "chili_bracket_missing_stop_repair_enabled", False))
    cancel_on = bool(getattr(settings, "chili_bracket_writer_cancel_covering_sell", False))
    if not (repair_on and not cancel_on):
        return False
    target_log.warning(
        f"{BRACKET_WRITER_G2} SILENT-EXPOSURE COMBO ACTIVE: "
        "CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=1 AND "
        "CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=0. Positions where "
        "held_for_sells == broker_qty (covered by an existing limit-sell "
        "only) will be skipped by the emergency-repair path and remain "
        "WITHOUT downside protection. Set "
        "CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1 to enable cancel-"
        "and-place-stop behavior, or accept the upside-lock default."
    )
    return True


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


# bracket-writer-respect-upside-targets (2026-05-04) — pending-decision
# helpers. The covered-by-existing-sell branch of place_missing_stop now
# routes here instead of unilaterally cancelling the operator's covering
# limit-sells. Operator decides via POST /api/admin/bracket-decisions/<id>.
#
# No magic numbers: every threshold is brain output (target_price/
# stop_price from compute_bracket_intent) or broker observation
# (held_for_sells, broker_qty, current price via fetch_quote). The options
# list is constructed dynamically -- the trailing-stop choice is omitted
# entirely when no broker-side helper exists (currently the case).


def _has_trailing_stop_placement_helper() -> bool:
    """Returns True iff a venue-side helper exists to place a Robinhood
    trailing-stop sell. The pending-decision options list omits
    'convert_to_trailing_stop' when this returns False."""
    # Probe pattern: look for a callable named ``place_trailing_stop_*``
    # in broker_service. Discovery on 2026-05-04 found none; surface any
    # future addition automatically.
    try:
        from .. import broker_service as _bs
        for name in dir(_bs):
            n = name.lower()
            if n.startswith("place_trailing") and callable(getattr(_bs, name, None)):
                return True
    except Exception:
        pass
    return False


def record_pending_bracket_decision(
    db: Session,
    *,
    bracket_intent_id: int,
    trade_id: int,
    ticker: str,
    broker_source: str,
    broker_qty: float,
    held_for_sells: float,
    covering_orders: list[dict[str, Any]],
    brain_target_price: float | None,
    brain_stop_price: float | None,
    current_price: float | None,
    regime: str | None,
    kind: str = "existing_sell_holds_all_shares",
) -> dict[str, Any]:
    """Write a structured pending_decision row into
    ``trading_bracket_intents.payload_json``. The reconciler reads
    ``payload_json.pending_decision.operator_choice`` on each sweep and
    routes to the appropriate resolution path (keep_target /
    replace_with_stop / convert_to_trailing_stop) once non-null.

    Returns the pending_decision dict that was persisted (for caller
    audit-trail use). The dict contains an ``options`` list naming the
    resolutions available given current broker capabilities; the
    trailing-stop option is included only if a placement helper exists
    in the broker service.

    Caller controls the surrounding transaction; this helper commits.
    """
    from datetime import datetime, timezone

    options: list[dict[str, str]] = [
        {
            "choice": "keep_target",
            "consequence": "no_downside_stop",
        },
        {
            "choice": "replace_with_stop",
            "consequence": "cancels_existing_limit_sell_and_places_stop_at_brain_price",
        },
    ]
    if _has_trailing_stop_placement_helper():
        options.append({
            "choice": "convert_to_trailing_stop",
            "consequence": "cancels_existing_limit_sell_and_places_trailing_stop_per_brain_atr",
        })

    pending = {
        "kind": kind,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "broker_state": {
            "qty": float(broker_qty),
            "held_for_sells": float(held_for_sells),
            "covering_orders": covering_orders,
        },
        "brain_state": {
            "target_price": brain_target_price,
            "stop_price": brain_stop_price,
            "current_price": current_price,
            "regime": regime,
        },
        "options": options,
        "operator_choice": None,
    }

    # Merge into existing payload_json (preserve any other keys).
    db.execute(
        text(
            "UPDATE trading_bracket_intents "
            "SET payload_json = COALESCE(payload_json, '{}'::jsonb) "
            "                || jsonb_build_object('pending_decision', "
            "                    CAST(:pending AS JSONB)), "
            "    last_diff_reason = 'existing_target_present_no_stop', "
            "    updated_at = NOW() "
            "WHERE id = :iid"
        ),
        {"iid": int(bracket_intent_id), "pending": _json_dumps(pending)},
    )
    db.commit()

    logger.warning(
        f"{BRACKET_WRITER_G2} place_missing_stop PENDING-DECISION intent=%s "
        "ticker=%s kind=%s covering_orders=%d (operator decision required; "
        "writer will not cancel covering sell unilaterally)",
        bracket_intent_id, ticker, kind, len(covering_orders),
    )
    return pending


def evaluate_target_replacement(
    db: Session,
    *,
    bracket_intent_id: int,
    trade_id: int,
    ticker: str,
    broker_source: str,
    entry_price: float,
    quantity: float,
    direction: str = "long",
    stop_model: str | None = None,
) -> dict[str, Any] | None:
    """Step 5 of bracket-writer-respect-upside-targets (2026-05-04).

    For a position whose covering limit-sell was cancelled (typically
    by the prior 19:14 deploy that this task retired), evaluate whether
    the brain still considers the original profit-target viable.

    Returns ``None`` when:
      * brain target is at-or-below current price (target already
        realized OR no longer ahead -- not viable; do not surface)
      * brain target is at-or-below entry price (would be a downward
        move, not a profit target)
      * ``fetch_quote`` returns None (no signal -> defer; no signal is
        not negative signal)
      * brain bracket computation fails

    Returns a pending-decision dict (already persisted to payload_json)
    with ``kind='cancelled_limit_replacement_candidate'`` when the brain
    says the target is still ahead. The operator chooses via the admin
    endpoint.

    No magic numbers: brain output is the source of the threshold;
    current price comes from fetch_quote; entry_price from the Trade
    row. The "viable" decision is exact-equality / strict-inequality
    against those values, no tolerance literal.
    """
    # Brain bracket compute
    try:
        from .bracket_intent import BracketIntentInput, compute_bracket_intent
        bi_in = BracketIntentInput(
            ticker=ticker,
            direction=(direction or "long").lower(),
            entry_price=float(entry_price or 0.0),
            quantity=float(quantity or 0.0),
            atr=None,
            stop_model=stop_model,
            pattern_id=None,
            lifecycle_stage=None,
            regime="cautious",
            pattern_win_rate=None,
            pattern_name=None,
        )
        bi_res = compute_bracket_intent(bi_in)
    except Exception:
        logger.debug(
            f"{BRACKET_WRITER_G2} evaluate_target_replacement brain compute "
            "failed intent=%s ticker=%s",
            bracket_intent_id, ticker, exc_info=True,
        )
        return None
    brain_target = bi_res.target_price
    brain_stop = bi_res.stop_price
    if brain_target is None or float(brain_target) <= 0:
        return None

    # Current price (defer on no-signal)
    try:
        from .market_data import fetch_quote
        q = fetch_quote(ticker)
    except Exception:
        q = None
    if q is None:
        return None
    current_price = q.get("last_price")
    if current_price is None:
        return None
    try:
        cp = float(current_price)
    except (TypeError, ValueError):
        return None
    if cp <= 0:
        return None

    # Viability checks: target must be above current AND above entry.
    if float(brain_target) <= cp:
        return None
    if float(brain_target) <= float(entry_price or 0.0):
        return None

    # Surface the candidate.
    return record_pending_bracket_decision(
        db,
        bracket_intent_id=int(bracket_intent_id),
        trade_id=int(trade_id),
        ticker=ticker,
        broker_source=broker_source,
        broker_qty=float(quantity),
        held_for_sells=0.0,  # the original limit was already cancelled
        covering_orders=[],
        brain_target_price=float(brain_target),
        brain_stop_price=float(brain_stop) if brain_stop else None,
        current_price=cp,
        regime="cautious",
        kind="cancelled_limit_replacement_candidate",
    )


def _json_dumps(value: Any) -> str:
    """Local JSON helper that handles datetime + Decimal cleanly."""
    import json
    from datetime import datetime, date
    from decimal import Decimal

    def _default(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        raise TypeError(f"not serialisable: {type(o)}")

    return json.dumps(value, default=_default, separators=(",", ":"))


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

    # f-phase-e-revert-and-bracket-writer-crash-fix (2026-05-08):
    # short-circuit if this intent had ANY exception in the last
    # _exception_cooldown_secs() seconds. Sits before the FIX 52
    # reject-cooldown so a code-bug crash doesn't even attempt to
    # re-evaluate the prior reject state.
    if _is_in_exception_cooldown(bracket_intent_id):
        logger.info(
            f"{BRACKET_WRITER_G2} place_missing_stop SKIPPED intent=%s "
            "ticker=%s reason=in_exception_cooldown",
            bracket_intent_id, ticker,
        )
        return WriterAction(
            action="place_missing_stop", ok=False,
            reason="in_exception_cooldown",
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

    # audit-unsupported-crypto-prefilter (2026-05-04) — venue-capability
    # gate before any broker call. ZEC-USD was the original canonical
    # case: an open Trade row with broker_source='robinhood' but
    # ticker='ZEC-USD' (a crypto pair Robinhood doesn't list). Without
    # this check, the writer routes to
    # ``broker_service.place_sell_stop_loss_order``, which calls
    # ``rh.orders.order(...)`` -> ``get_instruments_by_symbols("ZEC")[0]``
    # -> ``IndexError: list index out of range`` (no equity instrument).
    #
    # f-phase-e-revert-and-bracket-writer-crash-fix (2026-05-08):
    # extended to refuse ALL crypto tickers, not just bases off the
    # supported-trading whitelist. The 2026-05-09 01:57 UTC ADA/SOL
    # crash loop showed that EVEN listed crypto bases (ADA / SOL are
    # in ROBINHOOD_SUPPORTED_CRYPTO_BASES) blow up the same way: the
    # equity-stop-loss path (`rh.orders.order(symbol='ADA', ...)`)
    # asks for an EQUITY instrument record and Robinhood crypto bases
    # have none, so `get_instruments_by_symbols('ADA')` returns [] and
    # the [0] crashes. Listed-vs-unlisted is irrelevant; the equity
    # API is the wrong primitive for ALL crypto. A future brief can
    # wire the actual crypto stop-loss primitive
    # (`rh.crypto.order_*`) and remove this guard, but until then the
    # safe behaviour is to SKIPPED-audit and let an operator-side
    # follow-up address the missing crypto stop coverage.
    _t_upper = (ticker or "").upper()
    if _t_upper.endswith("-USD"):
        logger.warning(
            f"{BRACKET_WRITER_G2} place_missing_stop SKIPPED intent=%s "
            "ticker=%s reason=venue_unsupported_crypto_path "
            "(Robinhood crypto stop-loss is not supported via the equity "
            "rh.orders.order primitive; the equity instrument lookup "
            "returns [] for crypto bases and the SDK crashes on [0]). "
            "Skipping placement; operator-side follow-up to wire a "
            "crypto-native stop primitive when needed.",
            bracket_intent_id, ticker,
        )
        return WriterAction(
            action="place_missing_stop", ok=False,
            reason="venue_unsupported_crypto_path",
            broker_source=broker_source, ticker=ticker,
        )

    # FIX 55 (2026-05-01) — covered-by-existing-sell pre-flight.
    #
    # ROOT CAUSE of the AIDX/CCCC/CRDL/TLS/VFS/EKSO/PED rejection storm:
    # every share of those positions was already committed to an existing
    # limit-sell order placed weeks ago (target take-profit). Robinhood
    # reports holdings as 150 AIDX, 150 CCCC, etc., but
    # ``shares_held_for_sells == quantity``, so there are zero free shares
    # to put under a SELL_STOP. Result: every sweep produced a "Not enough
    # shares to sell" reject + Robinhood notification.
    #
    # FIX 51 (BrokerView.position_quantity) didn't catch this because it
    # only checks total qty, not ``available_to_sell = qty - held_for_sells``.
    # FIX 52 (terminal-reject cooldown) absorbed the storm but still let
    # one placement through per restart. FIX 55 catches the case at the
    # source: if all shares are already committed to an existing limit-
    # sell (typically a take-profit), placing a SELL_STOP on top would
    # require canceling the limit (since the broker rejects placements
    # when ``held_for_sells == quantity``). The default policy preserves
    # the limit and skips the stop.
    #
    # **THIS IS NOT DOWNSIDE PROTECTION.** A take-profit limit at a
    # higher price than current does nothing if price falls. The
    # trade-off is deliberate: upside lock-in vs downside protection.
    # Operators who want downside protection set
    # ``CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1`` to flip the policy:
    # cancel the limit, place the stop. See operator runbook in
    # docs/STRATEGY/CC_REPORTS/2026-05-03_bracket-writer-cover-policy-clarify.md.
    adapter = adapter_factory(broker_source)
    try:
        # Direct broker_service call (cleaner than adapter.get_positions
        # which has a different shape). 60s cache built into the helper.
        from .. import broker_service as _bs

        held_for_sells = _bs.get_position_held_for_sells(ticker)
    except Exception:
        held_for_sells = None

    # We need total qty too, to compute the available bucket. Use the
    # adapter's get_products (same data source as BrokerView but reachable
    # from here without plumbing the BrokerView through).
    broker_qty: float | None = None
    try:
        products, _fresh = adapter.get_products()
        for p in products or []:
            raw = getattr(p, "raw", None) or {}
            sym = raw.get("ticker") or getattr(p, "product_id", None)
            if (sym or "").upper().strip() == str(ticker).upper().strip():
                qty_raw = raw.get("quantity")
                if qty_raw is not None:
                    broker_qty = float(qty_raw)
                break
    except Exception:
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

    # FIX 57 (2026-05-01) + 2026-05-03 + 2026-05-04 revision -- covered-
    # by-existing-sell handling.
    #
    # When every share is already committed to an existing sell order
    # (held_for_sells >= broker_qty), a SELL_STOP placement is rejected
    # by Robinhood retail with "Not enough shares to sell" because of
    # the one-sell-per-share constraint at the venue.
    #
    # bracket-writer-respect-upside-targets (2026-05-04): the writer
    # NO LONGER decides this unilaterally. The 2026-05-04 19:14 deploy
    # demonstrated the cost of auto-cancelling: 5 operator-authored
    # covering limit-sells (profit targets at +17% to +200% above entry
    # on AIDX/CCCC/CRDL/TLS/VFS) were cancelled to free shares for
    # SELL_STOPs, a strategy shift the operator did not authorize.
    #
    # New policy: the writer SURFACES the conflict via a structured
    # pending_decision row in trading_bracket_intents.payload_json and
    # parks the intent. The operator chooses keep_target /
    # replace_with_stop / convert_to_trailing_stop via the admin
    # endpoint POST /api/admin/bracket-decisions/<id>. The reconciler
    # reads operator_choice on each subsequent sweep and routes to the
    # corresponding resolution path. Until a choice is recorded, no
    # broker action is taken.
    #
    # The CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL env var is now
    # forced to 0 in compose; the auto-cancel branch is gone. Going
    # forward, this is the single decision point.
    if (
        broker_qty is not None
        and held_for_sells is not None
        and held_for_sells >= broker_qty - 1e-9  # tolerance for float compare
    ):
        # Gather broker-side covering orders for the pending_decision JSON.
        try:
            from .. import broker_service as _bs
            covering_orders = _bs.list_open_sell_orders_for_ticker(ticker)
        except Exception:
            logger.debug(
                f"{BRACKET_WRITER_G2} list_open_sell_orders_for_ticker failed "
                "intent=%s ticker=%s", bracket_intent_id, ticker, exc_info=True,
            )
            covering_orders = []

        # Brain target/stop for the JSON. The viability evaluator (Step 5)
        # uses the same brain output; any decision the operator makes
        # consumes these values.
        brain_target = None
        brain_stop = None
        regime = None
        try:
            from .bracket_intent import BracketIntentInput, compute_bracket_intent
            from ...models.trading import Trade as _Trade
            t = db.get(_Trade, int(trade_id))
            if t is not None:
                bi_in = BracketIntentInput(
                    ticker=ticker,
                    direction=(t.direction or "long").lower(),
                    entry_price=float(t.entry_price or 0.0),
                    quantity=float(local_quantity or 0.0),
                    atr=None,
                    stop_model=t.stop_model,
                    pattern_id=getattr(t, "scan_pattern_id", None),
                    lifecycle_stage=None,
                    regime="cautious",
                    pattern_win_rate=None,
                    pattern_name=None,
                )
                bi_res = compute_bracket_intent(bi_in)
                brain_target = bi_res.target_price
                brain_stop = bi_res.stop_price
                regime = "cautious"
        except Exception:
            logger.debug(
                f"{BRACKET_WRITER_G2} brain bracket compute failed intent=%s "
                "ticker=%s", bracket_intent_id, ticker, exc_info=True,
            )

        # Current price from existing fetch_quote path. None is acceptable
        # (the JSON carries None and the reconciler can defer evaluation).
        current_price = None
        try:
            from .market_data import fetch_quote
            q = fetch_quote(ticker)
            if q is not None:
                current_price = q.get("last_price")
        except Exception:
            current_price = None

        record_pending_bracket_decision(
            db,
            bracket_intent_id=int(bracket_intent_id),
            trade_id=int(trade_id),
            ticker=ticker,
            broker_source=broker_source,
            broker_qty=float(broker_qty),
            held_for_sells=float(held_for_sells),
            covering_orders=covering_orders,
            brain_target_price=brain_target,
            brain_stop_price=brain_stop,
            current_price=current_price,
            regime=regime,
            kind="existing_sell_holds_all_shares",
        )
        return WriterAction(
            action="place_missing_stop", ok=False,
            reason="existing_target_present_no_stop",
            broker_source=broker_source, ticker=ticker,
        )

    # Cap at the available bucket if it's known and below local_quantity.
    if broker_qty is not None and held_for_sells is not None:
        available = max(0.0, broker_qty - held_for_sells)
        if available < float(local_quantity):
            logger.warning(
                f"{BRACKET_WRITER_G2} place_missing_stop capping qty intent=%s "
                "ticker=%s local_qty=%s broker_qty=%s held_for_sells=%s "
                "available=%s",
                bracket_intent_id, ticker, local_quantity, broker_qty,
                held_for_sells, available,
            )
            local_quantity = available

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
        # f-phase-e-revert-and-bracket-writer-crash-fix (2026-05-08):
        # arm a 5-min cooldown (settings-tunable) so a code-side crash
        # like the ADA/SOL IndexError doesn't loop at sweep cadence.
        # The existing FIX 52 reject-cooldown only arms on known
        # terminal-class broker error strings; pure exceptions never
        # made it into that path.
        _arm_exception_cooldown(bracket_intent_id)
        logger.warning(
            f"{BRACKET_WRITER_G2} place_missing_stop raised for intent=%s: %s "
            "(arming %ss exception cooldown)",
            bracket_intent_id, exc, _exception_cooldown_secs(), exc_info=True,
        )
        _g2_event(
            db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
            ticker=ticker, broker_source=broker_source,
            event_type="g2_place_missing_stop_rejected", status="rejected",
            qty=float(local_quantity), stop_price=float(stop_price),
            error=str(exc)[:500],
            extra={"exception_cooldown_armed_secs": _exception_cooldown_secs()},
        )
        return WriterAction(
            action="place_missing_stop", ok=False, reason="place_failed",
            broker_source=broker_source, ticker=ticker,
        )
    if not place_res.get("ok"):
        err_text = str(place_res.get("error") or "")
        terminal = _is_terminal_reject(err_text)
        if terminal:
            # Phase 3.3 (2026-05-01): persist the terminal reject in the
            # state machine, not just the in-process dict. The reconciler
            # gates on intent_state and won't invoke the writer again on a
            # terminal_reject row until an operator transitions it back
            # to intent. Keep the in-process arm too as belt-and-
            # suspenders during the migration window.
            _arm_reject_cooldown(bracket_intent_id)
            try:
                from .bracket_intent_writer import mark_terminal_reject as _mtr
                _mtr(db, int(bracket_intent_id), reason=f"terminal_reject:{err_text[:100]}")
            except Exception:
                logger.debug(
                    f"{BRACKET_WRITER_G2} mark_terminal_reject persist failed",
                    exc_info=True,
                )
            logger.warning(
                f"{BRACKET_WRITER_G2} place_missing_stop terminal reject; "
                "arming %ss cooldown + state_machine.terminal_reject "
                "intent=%s ticker=%s err=%s",
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

    # Phase 4 (2026-05-01) — post-placement verification.
    #
    # The API call returning ok=true with an order_id is NOT proof the
    # order persisted at the broker. ELTX exhibited this on 2026-05-01:
    # place_stop_loss_sell_order returned order_id 69f4e7df with state
    # "unconfirmed", chili logged "successful", and Robinhood cancelled
    # the order within 250ms (user saw the rejection in their app, the
    # API didn't surface a reject_reason, and chili treated it as a win).
    #
    # verify_order_landed polls the broker for up to 3 seconds (six 0.5s
    # samples) waiting for the state to move out of "unconfirmed". One of
    # three outcomes:
    #   * resting   → real success; transition to CONFIRMED_AT_BROKER
    #   * rejected  → broker post-cancelled; treat as terminal-class
    #                 reject (mark_terminal_reject), DON'T transition to
    #                 confirmed_at_broker, increment placement_count for
    #                 the FIX 56 threshold tracker.
    #   * unknown   → verify window timed out; conservative — log a
    #                 WARNING, arm post-place cooldown, leave state alone.
    try:
        from .. import broker_service as _bs
        verdict, obs_state = _bs.verify_order_landed(new_oid)
    except Exception:
        verdict, obs_state = ("unknown", None)
        logger.debug(
            f"{BRACKET_WRITER_G2} verify_order_landed raised",
            exc_info=True,
        )

    if verdict == "rejected":
        logger.warning(
            f"{BRACKET_WRITER_G2} place_missing_stop POST-ACCEPT REJECTED "
            "intent=%s ticker=%s order=%s observed_state=%s — broker "
            "cancelled within verify window. Treating as terminal-class "
            "failure, not success.",
            bracket_intent_id, ticker, new_oid[:8], obs_state,
        )
        # Arm reject cooldown (FIX 52 fast-path) AND persist terminal_reject.
        _arm_reject_cooldown(bracket_intent_id)
        try:
            from .bracket_intent_writer import mark_terminal_reject as _mtr
            _mtr(
                db, int(bracket_intent_id),
                reason=f"post_accept_{obs_state}:order_{new_oid[:8]}",
            )
        except Exception:
            logger.debug(
                f"{BRACKET_WRITER_G2} mark_terminal_reject persist failed",
                exc_info=True,
            )
        # Record this as a placement (for the FIX 56 threshold) so a
        # subsequent restart that loses the in-memory cooldown still
        # tracks consecutive failures.
        _record_placement(bracket_intent_id)
        _g2_event(
            db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
            ticker=ticker, broker_source=broker_source,
            event_type="g2_place_missing_stop_post_accept_rejected",
            status="rejected",
            new_stop_order_id=new_oid,
            qty=float(local_quantity), stop_price=float(stop_price),
            error=f"post_accept_state:{obs_state}",
            extra={"verify_verdict": verdict, "observed_state": obs_state},
        )
        return WriterAction(
            action="place_missing_stop", ok=False,
            reason="post_accept_rejected",
            broker_source=broker_source, ticker=ticker,
            new_stop_order_id=new_oid,
            raw_broker_response=place_res,
        )

    if verdict == "unknown":
        logger.warning(
            f"{BRACKET_WRITER_G2} place_missing_stop UNVERIFIED "
            "intent=%s ticker=%s order=%s last_observed_state=%s — verify "
            "window expired without the order leaving 'unconfirmed'. "
            "Treating conservatively: arming post-place cooldown, NOT "
            "transitioning state. Next sweep will re-check broker truth.",
            bracket_intent_id, ticker, new_oid[:8], obs_state,
        )
        _arm_post_place_cooldown(bracket_intent_id)
        _g2_event(
            db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
            ticker=ticker, broker_source=broker_source,
            event_type="g2_place_missing_stop_unverified",
            status="unverified",
            new_stop_order_id=new_oid,
            qty=float(local_quantity), stop_price=float(stop_price),
            extra={"verify_verdict": verdict, "observed_state": obs_state},
        )
        return WriterAction(
            action="place_missing_stop", ok=False,
            reason="unverified",
            broker_source=broker_source, ticker=ticker,
            new_stop_order_id=new_oid,
            raw_broker_response=place_res,
        )

    # verdict == "resting" → real success.
    logger.info(
        f"{BRACKET_WRITER_G2} place_missing_stop intent=%s ticker=%s qty=%s "
        "price=%s new_order=%s verified_state=%s",
        bracket_intent_id, ticker, local_quantity, stop_price, new_oid, obs_state,
    )
    # FIX 53 — arm post-placement cooldown so the next sweep doesn't
    # immediately re-classify and try again.
    _arm_post_place_cooldown(bracket_intent_id)

    # Phase 3.3 + Phase 4 (2026-05-01): only transition to
    # confirmed_at_broker AFTER the broker has verified the order is
    # resting. Best-effort — failure to transition doesn't undo the
    # placement (the order is real at the broker either way).
    try:
        from .bracket_intent_writer import (
            IntentState as _IS,
            transition as _tr,
        )
        _tr(
            db, int(bracket_intent_id),
            to_state=_IS.CONFIRMED_AT_BROKER,
            reason=f"placed_stop_verified:{new_oid[:8]}",
        )
    except Exception:
        logger.debug(
            f"{BRACKET_WRITER_G2} state transition to confirmed_at_broker failed",
            exc_info=True,
        )

    # FIX 56 — detect auto-cancel pattern. Count consecutive placements
    # for this intent; if we hit the threshold the broker is likely
    # auto-cancelling every stop (ELTX-style instrument restriction).
    # Arm the 1h terminal-reject cooldown so the user sees 3
    # notifications then silence, instead of 12/hour forever.
    #
    # Phase 3.3 (2026-05-01): also persist via the state machine — the
    # reconciler will gate on intent_state and skip subsequent sweeps.
    placement_count = _record_placement(bracket_intent_id)
    if placement_count >= _PLACEMENT_FAILURE_THRESHOLD:
        logger.warning(
            f"{BRACKET_WRITER_G2} place_missing_stop hit failure threshold "
            "intent=%s ticker=%s placements=%s — arming %ss terminal-reject "
            "cooldown + state_machine.terminal_reject (broker is auto-"
            "cancelling each placed stop)",
            bracket_intent_id, ticker, placement_count,
            _TERMINAL_REJECT_COOLDOWN_SECS,
        )
        _arm_reject_cooldown(bracket_intent_id)
        try:
            from .bracket_intent_writer import mark_terminal_reject as _mtr
            _mtr(
                db, int(bracket_intent_id),
                reason=f"placement_threshold_{placement_count}_consecutive_failed_stops",
            )
        except Exception:
            logger.debug(
                f"{BRACKET_WRITER_G2} mark_terminal_reject persist failed",
                exc_info=True,
            )
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
