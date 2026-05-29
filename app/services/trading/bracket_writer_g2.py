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
    "insufficient balance",
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


# f-prefilter-bypass-and-cooldown-investigation (2026-05-08):
# substrings that indicate an upstream code bug surfaced via the
# broker layer's `except Exception` -> `{"ok": False, "error": ...}`
# packaging path. The 2026-05-09 ADA/SOL crash loop showed the
# exception cooldown DIDN'T arm for these because the IndexError
# never escaped the broker call -- it was caught and returned as a
# normal-looking ok=False reject. Matching on the error string
# instead lets the bracket_writer arm the cooldown anyway.
#
# Conservative: only patterns that are unambiguously crash signatures
# (Python exception class names + the canonical IndexError text), not
# generic words like "error" or "fail" that could match legitimate
# broker rejects.
_CODE_BUG_ERROR_PATTERNS = (
    "list index out of range",
    "indexerror",
    "typeerror",
    "attributeerror",
    "keyerror",
    "nameerror",
    "valueerror",
    "crypto_ticker_unsupported_via_equity_primitive",
)

_TRANSIENT_DATA_UNAVAILABLE_ERROR_PATTERNS = (
    "product info fetch failed",
    "product_info_unavailable",
)


def _is_code_bug_error(error_text: str | None) -> bool:
    """Return True if the broker error string looks like a swallowed
    upstream exception (IndexError caught and packaged as ok=False).
    """
    if not error_text:
        return False
    needle = str(error_text).lower()
    return any(pat in needle for pat in _CODE_BUG_ERROR_PATTERNS)


def _is_transient_data_unavailable_error(error_text: str | None) -> bool:
    """Return True for broker-adapter data dependencies worth cooldown.

    These are not terminal broker rejects and must not mark an intent
    terminal_reject, but retrying every sweep just hammers the same
    unproven placement path. Example: Coinbase product metadata unavailable,
    where placing an unquantized stop would violate no-magic-fallback policy.
    """
    if not error_text:
        return False
    needle = str(error_text).lower()
    return any(pat in needle for pat in _TRANSIENT_DATA_UNAVAILABLE_ERROR_PATTERNS)


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

_SUPPORTED_VENUES = frozenset({"robinhood", "coinbase"})


def _is_fractional_share_quantity(value: Any) -> bool:
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return False
    return qty > 0.0 and abs(qty - round(qty)) > 1e-9


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


# f-coinbase-post-place-verify-routing-fix (2026-05-10): vocabulary
# mirror of broker_service.verify_order_landed. The Robinhood path
# uses these exact state strings; the Coinbase adapter's
# get_order_status emits the same normalized vocabulary so the verify
# verdict mapping is identical between venues.
_VERIFY_RESTING_STATES = frozenset(
    {"confirmed", "queued", "partially_filled", "filled"}
)
_VERIFY_REJECTED_STATES = frozenset({"rejected", "cancelled", "failed"})


def _verify_via_coinbase(
    adapter: Any,
    order_id: str,
    *,
    max_wait_s: float = 3.0,
    poll_interval_s: float = 0.5,
) -> tuple[str, Optional[str]]:
    """Coinbase-side mirror of broker_service.verify_order_landed.

    Polls ``adapter.get_order_status(order_id)`` every ``poll_interval_s``
    for at most ``max_wait_s``; maps the normalized state vocabulary to
    ``(verdict, observed_state)`` tuples with the same contract:

      * ``("resting",  state)`` on OPEN / FILLED / partially_filled /
                                 queued / confirmed
      * ``("rejected", state)`` on CANCELLED / EXPIRED / FAILED /
                                 REJECTED
      * ``("unknown",  last_state_or_None)`` on timeout

    Pre-fix bug (2026-05-10): the writer was calling
    ``broker_service.verify_order_landed`` for Coinbase orders, which
    polled api.robinhood.com for a Coinbase UUID and 404'd every cycle.
    This helper closes that routing gap.
    """
    if not order_id:
        return ("unknown", None)
    deadline = time.time() + float(max_wait_s)
    observed: Optional[str] = None
    while time.time() < deadline:
        try:
            res = adapter.get_order_status(order_id) or {}
        except Exception:
            res = {}
        state = res.get("state") if isinstance(res, dict) else None
        if isinstance(state, str):
            observed = state.strip().lower() or None
        if observed in _VERIFY_REJECTED_STATES:
            return ("rejected", observed)
        if observed in _VERIFY_RESTING_STATES:
            return ("resting", observed)
        # state likely None (404 / not-yet-acked) or "unconfirmed" / "new"
        # — keep polling
        time.sleep(float(poll_interval_s))
    return ("unknown", observed)


def _try_adopt_unverified_coinbase_order(
    db: Session,
    *,
    bracket_intent_id: int,
    adapter: Any,
    lookback_seconds: int = 24 * 3600,
) -> Optional[str]:
    """Coinbase-only orphan recovery.

    Pre-fix sweeps marked Coinbase intents 'unverified' because the
    verify call hit api.robinhood.com for a Coinbase UUID and 404'd —
    the order may STILL be resting at Coinbase. Look up the most
    recent ``g2_place_missing_stop_unverified`` event for this intent
    within ``lookback_seconds``; if the recorded broker order id is
    still in a resting state at Coinbase, return the order id so the
    caller can adopt it instead of placing a duplicate stop.

    Returns ``None`` when there's no prior unverified attempt, the DB
    lookup fails, the adapter is unreachable, or the previous order is
    in any non-resting state. Best-effort: any failure falls through
    to normal placement (safer than blocking a fresh attempt).

    Coinbase-only. Robinhood does NOT have this bug (verify routing
    was always correct for RH) and callers should gate accordingly.
    """
    # f-orphan-recovery-column-fix (2026-05-19):
    # Original SQL referenced ``payload`` (JSONB column is named
    # ``payload_json``) and ``created_at`` (timestamp columns are
    # ``event_at`` and ``recorded_at``). Both column references raised
    # ``UndefinedColumn`` at Postgres, which silently aborted the
    # broker-sync-worker's session. The bracket-reconciliation sweep's
    # subsequent INSERTs (via ``record_execution_event``) then failed
    # with ``InFailedSqlTransaction`` for the rest of the sweep. Fix:
    # use the actual column names from
    # ``app/models/trading.py:TradingExecutionEvent`` (mig 248-era).
    # Defensive rollback in except: on any failure here we re-open a
    # clean transaction so downstream writes (e.g. _g2_event) don't
    # inherit an aborted state. Without the rollback, even a typo
    # caught by ``try`` would poison the entire sweep.
    sql = text(
        "SELECT payload_json->>'new_stop_order_id' AS oid "
        "FROM trading_execution_events "
        "WHERE event_type = 'g2_place_missing_stop_unverified' "
        "  AND payload_json->>'bracket_intent_id' = :bid "
        "  AND recorded_at >= NOW() - (:lb || ' seconds')::interval "
        "ORDER BY recorded_at DESC LIMIT 1"
    )
    try:
        row = db.execute(
            sql,
            {"bid": str(int(bracket_intent_id)), "lb": str(int(lookback_seconds))},
        ).fetchone()
    except Exception:
        logger.debug(
            f"{BRACKET_WRITER_G2} orphan-recovery lookup raised intent=%s",
            bracket_intent_id, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None
    if row is None or not row[0]:
        return None
    prev_oid = str(row[0])
    try:
        res = adapter.get_order_status(prev_oid) or {}
    except Exception:
        logger.debug(
            f"{BRACKET_WRITER_G2} orphan-recovery get_order_status raised "
            "intent=%s prev_oid=%s",
            bracket_intent_id, prev_oid[:8], exc_info=True,
        )
        return None
    state = (
        res.get("state") if isinstance(res, dict) and isinstance(res.get("state"), str)
        else None
    )
    if isinstance(state, str) and state.strip().lower() in _VERIFY_RESTING_STATES:
        return prev_oid
    return None


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

    if _is_option_trade_for_bracket_writer(db, trade_id):
        logger.info(
            f"{BRACKET_WRITER_G2} resize_stop_for_partial_fill SKIPPED intent=%s "
            "trade=%s ticker=%s reason=option_exit_monitor_owns_contract_protection",
            bracket_intent_id, trade_id, ticker,
        )
        return WriterAction(
            action="resize_stop_for_partial_fill", ok=False,
            reason="option_exit_monitor_owns_contract_protection",
            broker_source=broker_source, ticker=ticker,
            prior_stop_order_id=prior_stop_order_id,
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


def _coinbase_stop_order_base_size(order: Any) -> float:
    raw = getattr(order, "raw", None) or {}
    if isinstance(raw, dict):
        cfg = raw.get("order_configuration")
        if isinstance(cfg, dict):
            for inner in cfg.values():
                if isinstance(inner, dict):
                    base_size = inner.get("base_size")
                    if base_size not in (None, ""):
                        try:
                            return float(base_size)
                        except Exception:
                            pass
        for key in ("outstanding_hold_amount", "base_size", "size"):
            value = raw.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except Exception:
                    pass
    return 0.0


def _coinbase_stop_coverage_dust_notional_usd() -> float:
    try:
        from .. import coinbase_service

        return float(getattr(coinbase_service, "_MIN_AUTO_CREATE_NOTIONAL_USD", 1.0))
    except Exception:
        return 1.0


def _coinbase_stop_coverage_full_enough(
    *,
    target_qty: float,
    stop_qty: float,
    stop_price: float | None,
) -> bool:
    """Return True when any uncovered Coinbase stop remainder is dust.

    Coinbase rejects below-min-notional stop-limit orders. If split resting
    stops already cover the actionable quantity except for an unplaceable
    remainder, adopt that broker truth instead of hammering the venue with
    doomed residual orders.
    """
    try:
        target = float(target_qty or 0.0)
        covered = float(stop_qty or 0.0)
    except (TypeError, ValueError):
        return False
    if target <= 0:
        return True
    uncovered = max(0.0, target - covered)
    if uncovered <= 1e-9:
        return True
    try:
        rel_gap = uncovered / target
    except ZeroDivisionError:
        rel_gap = 0.0
    if rel_gap <= 1e-4:
        return True
    try:
        px = float(stop_price or 0.0)
    except (TypeError, ValueError):
        px = 0.0
    if px > 0:
        threshold = _coinbase_stop_coverage_dust_notional_usd()
        if threshold > 0 and uncovered * px < threshold:
            return True
    return False


def _coinbase_open_stop_orders_for_ticker(adapter: Any, ticker: str) -> list[Any]:
    """Return working Coinbase SELL stop orders for one product.

    Coinbase permits multiple resting sell stops against one spot holding.
    The local bracket schema can store only one ``broker_stop_order_id``, so
    the writer must sum broker-held stop quantities before placing another
    order. Otherwise a split-order coverage state looks like "missing stop"
    forever and the writer keeps trying to over-cover the same inventory.
    """
    try:
        orders, _fresh = adapter.list_open_orders(product_id=ticker, limit=100)
    except Exception:
        return []
    out: list[Any] = []
    for order in orders or []:
        side = str(getattr(order, "side", "") or "").lower()
        order_type = str(getattr(order, "order_type", "") or "").upper()
        status = str(getattr(order, "status", "") or "").lower()
        product_id = str(getattr(order, "product_id", "") or "").upper()
        if product_id != str(ticker or "").upper():
            continue
        if side != "sell" or "STOP" not in order_type:
            continue
        if status not in (
            "open", "active", "working", "queued", "confirmed", "pending",
            "submitted", "accepted", "partially_filled", "unconfirmed",
        ):
            continue
        out.append(order)
    return out


def _is_option_trade_for_bracket_writer(db: Session, trade_id: int | None) -> bool:
    if trade_id is None:
        return False
    try:
        from .autopilot_scope import is_option_trade
        from ...models.trading import Trade

        trade = db.get(Trade, int(trade_id))
        return bool(trade is not None and is_option_trade(trade))
    except Exception:
        return False


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

    _t_upper = (ticker or "").upper()
    _bs_lower = (broker_source or "").strip().lower()
    if (
        _bs_lower == "robinhood"
        and not _t_upper.endswith("-USD")
        and _is_fractional_share_quantity(local_quantity)
    ):
        logger.info(
            f"{BRACKET_WRITER_G2} place_missing_stop SKIPPED intent=%s "
            "ticker=%s reason=software_stop_managed_robinhood_fractional_equity "
            "qty=%s",
            bracket_intent_id, ticker, local_quantity,
        )
        return WriterAction(
            action="place_missing_stop",
            ok=False,
            reason="software_stop_managed_robinhood_fractional_equity",
            broker_source=broker_source,
            ticker=ticker,
        )

    if _is_option_trade_for_bracket_writer(db, trade_id):
        logger.info(
            f"{BRACKET_WRITER_G2} place_missing_stop SKIPPED intent=%s "
            "trade=%s ticker=%s reason=option_exit_monitor_owns_contract_protection",
            bracket_intent_id, trade_id, ticker,
        )
        return WriterAction(
            action="place_missing_stop", ok=False,
            reason="option_exit_monitor_owns_contract_protection",
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
    # f-coinbase-autotrader-enablement-phase-4-bracket-writer-path
    # (2026-05-09): the crypto refusal narrows to RH only. Coinbase
    # has a native stop-limit primitive (place_stop_limit_order_gtc
    # in venue/coinbase_spot.py) so crypto-via-Coinbase reaches the
    # placement code below. The RH equity-API path (rh.orders.order)
    # still crashes on crypto bases via the SDK's
    # get_instruments_by_symbols([])[0] failure, so RH crypto still
    # SKIPPED-audits with the original reason string.
    if _t_upper.endswith("-USD") and _bs_lower == "robinhood":
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

    # f-coinbase-post-place-verify-routing-fix (2026-05-10): Coinbase
    # orphan recovery. Pre-fix sweeps marked intents 'unverified'
    # because the verify call hit api.robinhood.com for a Coinbase
    # UUID and 404'd — but the order may still be resting at Coinbase.
    # Before placing a fresh stop, check the most recent unverified
    # event row; if the prior order is still OPEN at Coinbase, adopt
    # it instead of duplicating. Coinbase-only — RH verify routing was
    # always correct so RH doesn't have stranded orders to recover.
    if _bs_lower == "coinbase":
        adopted_oid = _try_adopt_unverified_coinbase_order(
            db,
            bracket_intent_id=bracket_intent_id,
            adapter=adapter,
        )
        if adopted_oid:
            logger.info(
                f"{BRACKET_WRITER_G2} place_missing_stop ORPHAN-RECOVERED "
                "intent=%s ticker=%s order=%s — prior unverified order "
                "is still resting at Coinbase; adopting instead of placing "
                "a duplicate stop.",
                bracket_intent_id, ticker, adopted_oid[:8],
            )
            # Arm the post-place cooldown so the next sweep doesn't
            # immediately re-classify (mirrors the post-success path).
            _arm_post_place_cooldown(bracket_intent_id)
            # Transition intent_state -> CONFIRMED_AT_BROKER. Best-effort:
            # failure to transition doesn't undo the recovery (the order
            # is real at the broker either way).
            try:
                from .bracket_intent_writer import (
                    IntentState as _IS,
                    transition as _tr,
                )
                _tr(
                    db, int(bracket_intent_id),
                    to_state=_IS.CONFIRMED_AT_BROKER,
                    reason=f"orphan_recovered:{adopted_oid[:8]}",
                )
            except Exception:
                logger.debug(
                    f"{BRACKET_WRITER_G2} orphan-recovered transition to "
                    "confirmed_at_broker failed",
                    exc_info=True,
                )
            _g2_event(
                db, trade_id=trade_id, bracket_intent_id=bracket_intent_id,
                ticker=ticker, broker_source=broker_source,
                event_type="g2_place_missing_stop_orphan_recovered",
                status="orphan_recovered",
                new_stop_order_id=adopted_oid,
                qty=float(local_quantity), stop_price=float(stop_price),
                decision_kind=decision.kind,
                decision_severity=decision.severity,
            )
            return WriterAction(
                action="place_missing_stop", ok=True,
                reason="orphan_recovered",
                broker_source=broker_source, ticker=ticker,
                new_stop_order_id=adopted_oid,
            )

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

    if _bs_lower == "coinbase":
        existing_stop_orders = _coinbase_open_stop_orders_for_ticker(
            adapter, ticker,
        )
        existing_stop_qty = sum(
            _coinbase_stop_order_base_size(o) for o in existing_stop_orders
        )
        target_qty = float(local_quantity)
        if broker_qty is not None:
            target_qty = min(target_qty, float(broker_qty))
        uncovered_qty = max(0.0, target_qty - existing_stop_qty)
        if existing_stop_qty > 0:
            order_ids = [
                getattr(o, "order_id", None)
                for o in existing_stop_orders
                if getattr(o, "order_id", None)
            ]
            if _coinbase_stop_coverage_full_enough(
                target_qty=target_qty,
                stop_qty=existing_stop_qty,
                stop_price=stop_price,
            ):
                adopted_oid = order_ids[-1] if order_ids else None
                logger.info(
                    f"{BRACKET_WRITER_G2} place_missing_stop COINBASE-COVERED "
                    "intent=%s ticker=%s target_qty=%s existing_stop_qty=%s "
                    "uncovered_qty=%s orders=%s",
                    bracket_intent_id, ticker, target_qty, existing_stop_qty,
                    uncovered_qty, len(order_ids),
                )
                try:
                    from .bracket_intent_writer import (
                        IntentState as _IS,
                        transition as _tr,
                    )
                    _tr(
                        db, int(bracket_intent_id),
                        to_state=_IS.CONFIRMED_AT_BROKER,
                        reason="coinbase_existing_stop_coverage",
                    )
                except Exception:
                    logger.debug(
                        f"{BRACKET_WRITER_G2} coinbase coverage transition "
                        "failed",
                        exc_info=True,
                    )
                _g2_event(
                    db,
                    trade_id=trade_id,
                    bracket_intent_id=bracket_intent_id,
                    ticker=ticker,
                    broker_source=broker_source,
                    event_type="g2_place_missing_stop_existing_coinbase_coverage",
                    status="covered",
                    new_stop_order_id=adopted_oid,
                    qty=float(existing_stop_qty),
                    stop_price=float(stop_price),
                    decision_kind=decision.kind,
                    decision_severity=decision.severity,
                    extra={
                        "target_qty": target_qty,
                        "existing_stop_qty": existing_stop_qty,
                        "uncovered_qty": uncovered_qty,
                        "order_ids": order_ids,
                    },
                )
                return WriterAction(
                    action="place_missing_stop",
                    ok=True,
                    reason="existing_coinbase_stop_coverage",
                    broker_source=broker_source,
                    ticker=ticker,
                    new_stop_order_id=adopted_oid,
                    new_stop_qty=float(existing_stop_qty),
                    new_stop_price=float(stop_price),
                )
            if uncovered_qty < float(local_quantity):
                logger.warning(
                    f"{BRACKET_WRITER_G2} place_missing_stop COINBASE-CAP "
                    "intent=%s ticker=%s local_qty=%s broker_qty=%s "
                    "existing_stop_qty=%s placing_uncovered=%s",
                    bracket_intent_id, ticker, local_quantity, broker_qty,
                    existing_stop_qty, uncovered_qty,
                )
                local_quantity = uncovered_qty

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
    # f-coinbase-autotrader-enablement-phase-4-bracket-writer-path
    # (2026-05-09): venue-routed stop placement. RH path is
    # BYTE-IDENTICAL — the place_stop_loss_sell_order call args are
    # exactly the same as before this brief. Coinbase path uses the
    # new place_stop_limit_order_gtc primitive with limit_price set
    # to stop_price * (1 - chili_coinbase_stop_limit_buffer_pct).
    try:
        if _bs_lower == "coinbase":
            from ...config import settings as _cfg_p4
            _buffer_pct = float(
                getattr(_cfg_p4, "chili_coinbase_stop_limit_buffer_pct", 0.005)
            )
            _stop_px = float(stop_price)
            _limit_px = _stop_px * (1.0 - max(_buffer_pct, 0.0))
            place_res = adapter.place_stop_limit_order_gtc(
                product_id=ticker,
                side="sell",
                base_size=str(float(local_quantity)),
                stop_price=str(_stop_px),
                limit_price=str(_limit_px),
                client_order_id=client_oid,
            )
        else:
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
        # f-prefilter-bypass-and-cooldown-investigation (2026-05-08):
        # detect code-bug-class errors that the broker layer caught
        # and packaged as `ok=False` instead of letting the exception
        # escape (the exact bypass that prevented the prior
        # exception cooldown from arming on the ADA/SOL crash). Any
        # such match arms the same exception cooldown so the next
        # 60s sweep skips for `_exception_cooldown_secs()` instead of
        # re-firing.
        code_bug = _is_code_bug_error(err_text)
        transient_data_unavailable = _is_transient_data_unavailable_error(err_text)
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
        elif code_bug or transient_data_unavailable:
            _arm_exception_cooldown(bracket_intent_id)
            cooldown_class = (
                "code-bug class"
                if code_bug
                else "transient data-unavailable class"
            )
            logger.warning(
                f"{BRACKET_WRITER_G2} place_missing_stop broker error matches "
                "%s; arming %ss exception cooldown "
                "intent=%s ticker=%s err=%s",
                cooldown_class, _exception_cooldown_secs(), bracket_intent_id, ticker,
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
            extra={
                "terminal_reject": terminal,
                "code_bug_cooldown_armed": code_bug,
                "transient_data_cooldown_armed": transient_data_unavailable,
                "exception_cooldown_armed": code_bug or transient_data_unavailable,
            },
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
    # f-coinbase-post-place-verify-routing-fix (2026-05-10): venue-route
    # the verify step. The Robinhood path (broker_service.
    # verify_order_landed) hardcoded api.robinhood.com, which 404'd for
    # Coinbase UUIDs and caused 9 Coinbase positions to go DB-naked
    # while their stops actually rested at Coinbase. Coinbase orders
    # now poll via the adapter's get_order_status; RH path is byte-
    # identical to the prior behaviour.
    try:
        if _bs_lower == "coinbase":
            verdict, obs_state = _verify_via_coinbase(adapter, new_oid)
        else:
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
