"""P1.1 — formal order state machine.

Canonical order states + an allowed-transition table for venue orders.
The state machine is **additive to execution_audit**: the existing event
stream keeps writing ``TradingExecutionEvent`` rows with broker-native
statuses; this module projects those statuses onto a single canonical
state per order and writes one transition row to
``trading_order_state_log`` per state change.

States (exhaustive)::

    DRAFT → SUBMITTING → ACK → PARTIAL → FILLED
                     ↘      ↘      ↓     ↑    ↘
                       REJECTED  ┐ │  ┌──┘     CANCELLED
                                 ▼ ▼  │
                           CANCELLED  └─► EXPIRED

Rules:
    * ``DRAFT`` is the state a locally-decided order sits in before we
      call the venue — used for shadow / phase-G.2 bracket writers that
      *may* submit later.
    * ``SUBMITTING`` is the brief window between our ``place_*`` call and
      the broker's first ACK. Reached from ``DRAFT`` or (for backwards
      compatibility with loops that don't emit DRAFT) from ``None``.
    * ``ACK`` means the broker accepted the order and it's working at
      the book but we've seen no fills yet.
    * ``PARTIAL`` is a sticky state: multiple ``PARTIAL → PARTIAL``
      transitions are allowed as fills accumulate.
    * ``FILLED`` / ``CANCELLED`` / ``REJECTED`` / ``EXPIRED`` are
      terminal — any further transition attempt is a no-op.

Why it matters:
    * Before this module, each loop inferred state from whichever broker
      API call it happened to make and wrote an execution event — with
      no clean "order is currently in ACK" answer and no latency series
      keyed on transitions.
    * P1.2 (venue health circuit breaker) needs rolling ack-to-fill P95
      per venue — which requires canonical transitions, not
      broker-native statuses that each venue spells differently.

Opt-in: controlled by ``settings.chili_order_state_machine_enabled``
(default False during P1.1 rollout). When disabled, ``record_transition``
becomes a no-op that returns an informational payload but writes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


class OrderState(str, Enum):
    """Canonical venue order state."""

    DRAFT = "draft"
    SUBMITTING = "submitting"
    ACK = "ack"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ── Allowed transitions table ─────────────────────────────────────────
#
# The key is the current state (or None for the very first observation
# of an order); the value is the set of states we're willing to move to.
# Anything outside this set is rejected by ``record_transition``.
#
# The ``None`` key lets the first observation land on any non-terminal
# state — useful when a loop learns about an order that's already in
# ACK without having first written DRAFT/SUBMITTING locally.

ALLOWED_TRANSITIONS: dict[Optional[OrderState], frozenset[OrderState]] = {
    None: frozenset({
        OrderState.DRAFT,
        OrderState.SUBMITTING,
        OrderState.ACK,
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }),
    OrderState.DRAFT: frozenset({
        OrderState.SUBMITTING,
        OrderState.CANCELLED,  # user cancels before submit
        OrderState.REJECTED,   # pre-submit validation failed
    }),
    OrderState.SUBMITTING: frozenset({
        OrderState.ACK,
        OrderState.REJECTED,   # broker refused at submit
        OrderState.CANCELLED,
    }),
    OrderState.ACK: frozenset({
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,   # rare: broker revokes after ACK
    }),
    OrderState.PARTIAL: frozenset({
        OrderState.PARTIAL,    # sticky — more fills arriving
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
    }),
    # Terminal states — nothing further.
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}

TERMINAL_STATES: frozenset[OrderState] = frozenset({
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.EXPIRED,
})


# ── Broker status → canonical state mapping ──────────────────────────
#
# Robinhood and Coinbase each spell their order lifecycle slightly
# differently. Centralise the mapping here so every caller gets the
# same normalization and the state machine stays venue-agnostic.


_ACK_STATUSES = {
    "ack", "acknowledged", "open", "active", "working", "queued",
    "confirmed", "pending", "submitted", "accepted", "unconfirmed",
}
_PARTIAL_STATUSES = {
    "partial", "partial_filled", "partially_filled", "partialfill",
}
_FILLED_STATUSES = {"filled", "done", "completed", "complete"}
_CANCELLED_STATUSES = {"cancelled", "canceled", "revoked"}
_REJECTED_STATUSES = {"rejected", "failed", "denied"}
_EXPIRED_STATUSES = {"expired", "timed_out"}
_SUBMITTING_STATUSES = {"submitting", "in_flight", "sending"}
_DRAFT_STATUSES = {"draft", "prepared", "pending_submit"}


def map_broker_status(broker_status: str | None) -> Optional[OrderState]:
    """Map a broker-native order status to a canonical :class:`OrderState`.

    Returns ``None`` for unknown / empty inputs. Callers should skip
    emitting a transition when this returns ``None`` — the alternative
    (writing a made-up state) would pollute the transition log.
    """
    if not broker_status:
        return None
    s = str(broker_status).strip().lower()
    if s in _FILLED_STATUSES:
        return OrderState.FILLED
    if s in _PARTIAL_STATUSES:
        return OrderState.PARTIAL
    if s in _CANCELLED_STATUSES:
        return OrderState.CANCELLED
    if s in _REJECTED_STATUSES:
        return OrderState.REJECTED
    if s in _EXPIRED_STATUSES:
        return OrderState.EXPIRED
    if s in _ACK_STATUSES:
        return OrderState.ACK
    if s in _SUBMITTING_STATUSES:
        return OrderState.SUBMITTING
    if s in _DRAFT_STATUSES:
        return OrderState.DRAFT
    return None


def is_terminal(state: OrderState | str | None) -> bool:
    if state is None:
        return False
    if isinstance(state, str):
        try:
            state = OrderState(state)
        except ValueError:
            return False
    return state in TERMINAL_STATES


def is_transition_allowed(
    from_state: OrderState | None,
    to_state: OrderState,
) -> bool:
    """True when ``from_state → to_state`` is in the allowed table.

    ``from_state=None`` (first observation) allows any non-None
    destination per the ``None`` entry in ``ALLOWED_TRANSITIONS``.
    """
    allowed = ALLOWED_TRANSITIONS.get(from_state, frozenset())
    return to_state in allowed


# ── Settings wrapper ──────────────────────────────────────────────────


def _is_enabled() -> bool:
    """Feature flag — read live so tests can flip via monkeypatch.

    Default False during P1.1 rollout; flip on per-environment once the
    wiring has been watched in shadow for a week.
    """
    try:
        from app.config import settings
        return bool(getattr(settings, "chili_order_state_machine_enabled", False))
    except Exception:
        return False


# ── DB readers ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TransitionResult:
    """Return value of ``record_transition``.

    ``wrote`` is False when either the state machine is disabled, the
    transition wasn't allowed, or the proposed state equals the current
    state (no-op). The other fields describe what the writer saw.
    """

    wrote: bool
    from_state: Optional[OrderState]
    to_state: OrderState
    reason: str              # 'ok' | 'disabled' | 'no_key' | 'illegal_transition' |
                             # 'same_state' | 'terminal_locked'
    order_id: Optional[str]
    client_order_id: Optional[str]


def get_current_state(
    db: Session,
    *,
    order_id: str | None = None,
    client_order_id: str | None = None,
) -> Optional[OrderState]:
    """Return the most recent ``to_state`` for an order.

    The table is queried by whichever key was supplied — callers commonly
    only know ``client_order_id`` right after a submit and need to resolve
    to the broker ``order_id`` later. When both are provided, ``order_id``
    wins (broker id is the authoritative identifier once known).
    """
    if not order_id and not client_order_id:
        return None

    if order_id:
        row = db.execute(text("""
            SELECT to_state
            FROM trading_order_state_log
            WHERE order_id = :oid
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1
        """), {"oid": order_id}).fetchone()
    else:
        row = db.execute(text("""
            SELECT to_state
            FROM trading_order_state_log
            WHERE client_order_id = :cid
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1
        """), {"cid": client_order_id}).fetchone()

    if not row or not row[0]:
        return None
    try:
        return OrderState(row[0])
    except ValueError:
        return None


def get_state_history(
    db: Session,
    *,
    order_id: str | None = None,
    client_order_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return transitions for one order in chronological order.

    Primarily a diagnostics / test helper. Callers that need to check
    "is order X currently in state Y" should use :func:`get_current_state`.
    """
    if not order_id and not client_order_id:
        return []

    key_clause = "order_id = :k" if order_id else "client_order_id = :k"
    key_val = order_id or client_order_id
    rows = db.execute(text(f"""
        SELECT id, from_state, to_state, source, venue,
               broker_status, recorded_at
        FROM trading_order_state_log
        WHERE {key_clause}
        ORDER BY recorded_at ASC, id ASC
        LIMIT :lim
    """), {"k": key_val, "lim": int(limit)}).fetchall()
    return [
        {
            "id": int(r[0]),
            "from_state": r[1],
            "to_state": r[2],
            "source": r[3],
            "venue": r[4],
            "broker_status": r[5],
            "recorded_at": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]


# ── Writer — the main entry point ────────────────────────────────────


def record_transition(
    db: Session,
    *,
    to_state: OrderState,
    venue: str,
    source: str,
    order_id: str | None = None,
    client_order_id: str | None = None,
    broker_status: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    enabled_override: bool | None = None,
    commit: bool = False,
) -> TransitionResult:
    """Record a state transition for one order.

    Reads the current state (via the most recent row with the same
    ``order_id`` or ``client_order_id``) and compares to ``to_state``
    through the allowed-transition table. Writes one row when the
    transition is legal and the state actually changes.

    Arguments:
        to_state: proposed new canonical state. Required.
        venue: "robinhood" | "coinbase" | etc. Required.
        source: where this observation came from — "submit", "poll_loop",
            "reconciler", "webhook", "manual", "test". Required.
        order_id: broker order id (preferred key once known).
        client_order_id: our client id (used before the broker responds).
        broker_status: the raw venue-native status for this observation
            (kept alongside the canonical state for debugging).
        raw_payload: optional JSON blob with the full venue response.
        enabled_override: force the state machine on / off for this call.
            When ``None`` the global setting is consulted.
        commit: when True, commits after the insert. Defaults to False so
            the caller's transaction scope wins (matches how
            execution_audit flushes and lets the outer commit fire).

    Returns:
        :class:`TransitionResult` describing whether a row was written
        and why (reason field). Does not raise; callers can always fall
        through to their existing logic.
    """
    enabled = enabled_override if enabled_override is not None else _is_enabled()
    if not enabled:
        return TransitionResult(
            wrote=False, from_state=None, to_state=to_state,
            reason="disabled",
            order_id=order_id, client_order_id=client_order_id,
        )

    if not order_id and not client_order_id:
        return TransitionResult(
            wrote=False, from_state=None, to_state=to_state,
            reason="no_key",
            order_id=None, client_order_id=None,
        )

    current = get_current_state(
        db, order_id=order_id, client_order_id=client_order_id,
    )

    # Terminal states are sticky — never move off them even if the loop
    # re-observes the same order. Protects the invariant that a FILLED
    # order never "goes back" to ACK because a stale poll arrived.
    if current in TERMINAL_STATES:
        return TransitionResult(
            wrote=False, from_state=current, to_state=to_state,
            reason="terminal_locked",
            order_id=order_id, client_order_id=client_order_id,
        )

    # No-op: same state. We don't want the log to double-count idle ACKs.
    # (PARTIAL → PARTIAL stays — that's a real additional-fill event.)
    if current == to_state and to_state != OrderState.PARTIAL:
        return TransitionResult(
            wrote=False, from_state=current, to_state=to_state,
            reason="same_state",
            order_id=order_id, client_order_id=client_order_id,
        )

    if not is_transition_allowed(current, to_state):
        return TransitionResult(
            wrote=False, from_state=current, to_state=to_state,
            reason="illegal_transition",
            order_id=order_id, client_order_id=client_order_id,
        )

    db.execute(text("""
        INSERT INTO trading_order_state_log (
            order_id, client_order_id, venue,
            from_state, to_state, source,
            broker_status, raw_payload, recorded_at
        ) VALUES (
            :order_id, :client_order_id, :venue,
            :from_state, :to_state, :source,
            :broker_status, CAST(:raw_payload AS JSONB), NOW()
        )
    """), {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "venue": venue,
        "from_state": current.value if current is not None else None,
        "to_state": to_state.value,
        "source": source[:32],
        "broker_status": (broker_status or "")[:32] or None,
        "raw_payload": _json_dumps(raw_payload or {}),
    })
    if commit:
        db.commit()

    return TransitionResult(
        wrote=True, from_state=current, to_state=to_state,
        reason="ok",
        order_id=order_id, client_order_id=client_order_id,
    )


def record_from_broker_status(
    db: Session,
    *,
    broker_status: str | None,
    venue: str,
    source: str,
    order_id: str | None = None,
    client_order_id: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    enabled_override: bool | None = None,
    commit: bool = False,
) -> TransitionResult:
    """Convenience wrapper — map a broker-native status then record.

    Returns ``reason='unknown_broker_status'`` when the mapping fails,
    so callers can tell "we didn't write because we don't speak this
    status yet" apart from "we didn't write because no transition."
    """
    canonical = map_broker_status(broker_status)
    if canonical is None:
        return TransitionResult(
            wrote=False, from_state=None,
            to_state=OrderState.ACK,  # placeholder; wrote=False
            reason="unknown_broker_status",
            order_id=order_id, client_order_id=client_order_id,
        )
    return record_transition(
        db,
        to_state=canonical,
        venue=venue,
        source=source,
        order_id=order_id,
        client_order_id=client_order_id,
        broker_status=broker_status,
        raw_payload=raw_payload,
        enabled_override=enabled_override,
        commit=commit,
    )


# ── Diagnostics summary ──────────────────────────────────────────────


def record_transition_standalone(
    *,
    to_state: OrderState,
    venue: str,
    source: str,
    order_id: str | None = None,
    client_order_id: str | None = None,
    broker_status: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> TransitionResult:
    """Session-opening wrapper around :func:`record_transition`.

    Venue adapters don't carry a SQLAlchemy ``Session`` — they just want
    to emit a transition alongside each ``place_*`` / ``cancel_order``
    call. This helper opens its own session (via ``SessionLocal``),
    records the transition, commits, and never raises. If the state
    machine is disabled or DB is unavailable, we return the usual
    no-op :class:`TransitionResult` so callers can keep going.
    """
    # Fast short-circuit when disabled so we don't even open a session.
    if not _is_enabled():
        return TransitionResult(
            wrote=False, from_state=None, to_state=to_state,
            reason="disabled",
            order_id=order_id, client_order_id=client_order_id,
        )
    try:
        from ....db import SessionLocal
    except Exception:
        return TransitionResult(
            wrote=False, from_state=None, to_state=to_state,
            reason="db_unavailable",
            order_id=order_id, client_order_id=client_order_id,
        )
    try:
        db = SessionLocal()
    except Exception:
        return TransitionResult(
            wrote=False, from_state=None, to_state=to_state,
            reason="db_unavailable",
            order_id=order_id, client_order_id=client_order_id,
        )
    try:
        result = record_transition(
            db,
            to_state=to_state,
            venue=venue,
            source=source,
            order_id=order_id,
            client_order_id=client_order_id,
            broker_status=broker_status,
            raw_payload=raw_payload,
            commit=True,
        )
        return result
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return TransitionResult(
            wrote=False, from_state=None, to_state=to_state,
            reason="db_error",
            order_id=order_id, client_order_id=client_order_id,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def record_from_broker_status_standalone(
    *,
    broker_status: str | None,
    venue: str,
    source: str,
    order_id: str | None = None,
    client_order_id: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> TransitionResult:
    """Session-opening variant of :func:`record_from_broker_status`."""
    canonical = map_broker_status(broker_status)
    if canonical is None:
        return TransitionResult(
            wrote=False, from_state=None,
            to_state=OrderState.ACK,
            reason="unknown_broker_status",
            order_id=order_id, client_order_id=client_order_id,
        )
    return record_transition_standalone(
        to_state=canonical,
        venue=venue,
        source=source,
        order_id=order_id,
        client_order_id=client_order_id,
        broker_status=broker_status,
        raw_payload=raw_payload,
    )


def state_distribution(
    db: Session,
    *,
    venue: str | None = None,
    lookback_hours: int = 24,
) -> dict[str, int]:
    """Return a ``{state: count}`` dict for the most recent transitions.

    Counts distinct orders keyed by the most recent ``to_state``. Used
    by the upcoming P1.2 venue-health module to answer "how many orders
    are stuck in ACK right now."
    """
    params: dict[str, Any] = {"lh": int(lookback_hours)}
    venue_filter = ""
    if venue:
        venue_filter = " AND venue = :v"
        params["v"] = venue

    # ``make_interval(hours => :lh)`` is more robust than ``(:lh || ' hours')::INTERVAL``:
    # the concat form relies on implicit int→text coercion which can behave
    # differently under parameter binding, and produced an empty result set
    # at least once under load in the full suite. ``make_interval`` takes
    # an integer directly and is unambiguous.
    rows = db.execute(text(f"""
        WITH latest AS (
            SELECT DISTINCT ON (COALESCE(order_id, client_order_id))
                COALESCE(order_id, client_order_id) AS k,
                to_state, venue, recorded_at
            FROM trading_order_state_log
            WHERE recorded_at >= (NOW() - make_interval(hours => :lh))
              {venue_filter}
            ORDER BY COALESCE(order_id, client_order_id),
                     recorded_at DESC, id DESC
        )
        SELECT to_state, COUNT(*) FROM latest GROUP BY to_state
    """), params).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def _json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, default=str, separators=(",", ":"))


__all__ = [
    "ALLOWED_TRANSITIONS",
    "OrderState",
    "TERMINAL_STATES",
    "TransitionResult",
    "get_current_state",
    "get_state_history",
    "is_terminal",
    "is_transition_allowed",
    "map_broker_status",
    "record_from_broker_status",
    "record_from_broker_status_standalone",
    "record_transition",
    "record_transition_standalone",
    "state_distribution",
]
