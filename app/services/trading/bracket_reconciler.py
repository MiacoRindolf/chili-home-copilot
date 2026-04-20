"""Phase G - pure reconciliation classifier (no DB, no broker).

Given a local trade/bracket-intent view and a broker-reported view of
the same position + child orders, return a single
``(kind, severity, delta_payload)`` tuple. The reconciliation service
writes one ``BracketReconciliationLog`` row per call.

Kinds (exhaustive):

* ``agree``        - local and broker match within tolerances.
* ``orphan_stop``  - broker has a working stop/limit child order but
  local has no open trade (or trade is closed).
* ``missing_stop`` - local has an open live trade with a
  ``BracketIntent`` but the broker reports no protective order.
* ``qty_drift``    - quantities differ by more than
  ``qty_drift_abs`` between local and broker.
* ``price_drift``  - stop or target price differs by more than
  ``price_drift_bps`` bps between local intent and broker child order.
* ``state_drift``  - local ``intent_state`` disagrees with broker
  order state (e.g. local ``authoritative_submitted`` but broker
  order is ``cancelled``).
* ``broker_down``  - broker snapshot unavailable or marked stale.
* ``unreconciled`` - unknown or malformed input we refuse to classify.

The classifier is pure; the service layer converts input SQLAlchemy /
broker-adapter objects into the normalized ``LocalView`` / ``BrokerView``
dataclasses before calling this.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Kind = Literal[
    "agree",
    "orphan_stop",
    "missing_stop",
    "qty_drift",
    "state_drift",
    "price_drift",
    "broker_down",
    "unreconciled",
]

Severity = Literal["info", "warn", "error"]


@dataclass(frozen=True)
class LocalView:
    """Local (app-side) view of a single position + its bracket intent."""

    trade_id: Optional[int]
    bracket_intent_id: Optional[int]
    ticker: Optional[str]
    direction: Optional[str]
    quantity: Optional[float]
    intent_state: Optional[str]
    stop_price: Optional[float]
    target_price: Optional[float]
    broker_source: Optional[str]
    trade_status: Optional[str]


@dataclass(frozen=True)
class BrokerView:
    """Broker-reported view of position + child orders for one ticker.

    ``available`` is False when the sweep failed to reach the broker;
    classification must short-circuit to ``broker_down`` in that case.
    """

    available: bool
    ticker: Optional[str]
    broker_source: Optional[str]
    position_quantity: Optional[float] = None
    stop_order_id: Optional[str] = None
    stop_order_state: Optional[str] = None
    stop_order_price: Optional[float] = None
    target_order_id: Optional[str] = None
    target_order_state: Optional[str] = None
    target_order_price: Optional[float] = None


@dataclass(frozen=True)
class ReconciliationDecision:
    kind: Kind
    severity: Severity
    delta_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Tolerances:
    price_drift_bps: float = 25.0
    qty_drift_abs: float = 1e-6


def _bps_diff(expected: float, observed: float) -> float:
    if expected is None or observed is None:
        return 0.0
    if expected == 0:
        return 0.0
    return abs(observed - expected) / abs(expected) * 10_000.0


def _is_working_state(state: Optional[str]) -> bool:
    """Return True when a broker order is still capable of firing."""
    if not state:
        return False
    s = state.lower()
    return s in (
        "open",
        "active",
        "working",
        "queued",
        "confirmed",
        "pending",
        "partially_filled",
        "submitted",
        "accepted",
    )


def classify_discrepancy(
    local: LocalView,
    broker: BrokerView,
    *,
    tolerances: Tolerances = Tolerances(),
) -> ReconciliationDecision:
    """Return the single reconciliation decision for one local+broker pair.

    Exhaustive match across kinds. Tolerances are configurable because
    production Robinhood / Coinbase venues use different rounding and
    the price-drift threshold must be venue-aware in the future.
    """
    # ── broker unavailable ───────────────────────────────────────
    if not broker.available:
        return ReconciliationDecision(
            kind="broker_down",
            severity="warn",
            delta_payload={"reason": "broker snapshot unavailable"},
        )

    trade_open = (local.trade_status or "").lower() == "open"
    has_local_intent = local.bracket_intent_id is not None
    broker_has_stop = _is_working_state(broker.stop_order_state)
    broker_has_target = _is_working_state(broker.target_order_state)

    # ── orphan stop: broker has working child but local not open ─
    if (broker_has_stop or broker_has_target) and not trade_open:
        return ReconciliationDecision(
            kind="orphan_stop",
            severity="error",
            delta_payload={
                "broker_stop_order_id": broker.stop_order_id,
                "broker_target_order_id": broker.target_order_id,
                "local_trade_status": local.trade_status,
            },
        )

    # ── state drift: authoritative local intent but broker killed the order ──
    # Must run before ``missing_stop`` so we label an authoritative order
    # that was cancelled/rejected at the broker specifically, rather than
    # collapsing it into the generic missing-stop bucket.
    if (
        (local.intent_state or "").startswith("authoritative")
        and broker.stop_order_state
        and broker.stop_order_state.lower() in ("cancelled", "canceled", "rejected", "expired")
    ):
        return ReconciliationDecision(
            kind="state_drift",
            severity="error",
            delta_payload={
                "intent_state": local.intent_state,
                "broker_stop_order_state": broker.stop_order_state,
            },
        )

    # ── missing stop: open trade with intent, no broker child ────
    if trade_open and has_local_intent and not broker_has_stop:
        return ReconciliationDecision(
            kind="missing_stop",
            severity="warn" if (local.intent_state or "") == "intent" else "error",
            delta_payload={
                "intent_state": local.intent_state,
                "local_stop_price": local.stop_price,
                "broker_stop_order_state": broker.stop_order_state,
            },
        )

    # ── qty drift ────────────────────────────────────────────────
    # P0.5: Enrich the delta payload with a partial-fill hint so a Phase G.2
    # writer can resize the stop to the *actually filled* quantity rather than
    # the intended quantity. The stop must protect what is *actually on the
    # book* — sizing it to the intended qty over-hedges when a partial fill
    # leaves us short, and under-hedges if the broker ends up with extra shares
    # (shouldn't happen, but the payload captures it either way).
    if trade_open and local.quantity is not None and broker.position_quantity is not None:
        local_q = float(local.quantity)
        broker_q = float(broker.position_quantity)
        diff = abs(local_q - broker_q)
        if diff > tolerances.qty_drift_abs:
            # fill_ratio is broker/local; 0 when local is 0 to avoid div-by-zero.
            fill_ratio: Optional[float]
            if local_q > 0:
                fill_ratio = broker_q / local_q
            else:
                fill_ratio = None
            # is_partial_fill: broker holds *some* but less than intended. This is
            # the actionable case — rewrite the stop to broker_q. An over-fill
            # (broker > local) or total miss (broker == 0) are separate signals.
            is_partial_fill = (
                broker_q > 0.0
                and local_q > 0.0
                and broker_q + tolerances.qty_drift_abs < local_q
            )
            # expected_stop_qty tells a future authoritative writer exactly how
            # many shares/units to place the stop on.
            expected_stop_qty = broker_q if broker_q > 0.0 else None
            # Over-fill is highly anomalous (error); partial fill is warn
            # because the position is still protected at the broker-qty level
            # once the writer resizes. Full miss (broker==0 while local>0) is
            # error — our local state thinks we're long but the broker isn't.
            if broker_q > local_q + tolerances.qty_drift_abs:
                sev: Severity = "error"
                drift_kind = "over_fill"
            elif broker_q == 0.0 and local_q > 0.0:
                sev = "error"
                drift_kind = "broker_flat"
            else:
                sev = "warn"
                drift_kind = "partial_fill"
            return ReconciliationDecision(
                kind="qty_drift",
                severity=sev,
                delta_payload={
                    "local_qty": local_q,
                    "broker_qty": broker_q,
                    "abs_diff": diff,
                    "fill_ratio": fill_ratio,
                    "is_partial_fill": is_partial_fill,
                    "expected_stop_qty": expected_stop_qty,
                    "drift_kind": drift_kind,
                },
            )

    # ── price drift on stop / target ─────────────────────────────
    if (
        trade_open
        and broker_has_stop
        and local.stop_price is not None
        and broker.stop_order_price is not None
    ):
        bps = _bps_diff(float(local.stop_price), float(broker.stop_order_price))
        if bps > tolerances.price_drift_bps:
            return ReconciliationDecision(
                kind="price_drift",
                severity="warn",
                delta_payload={
                    "leg": "stop",
                    "local_price": float(local.stop_price),
                    "broker_price": float(broker.stop_order_price),
                    "drift_bps": round(bps, 2),
                },
            )

    if (
        trade_open
        and broker_has_target
        and local.target_price is not None
        and broker.target_order_price is not None
    ):
        bps = _bps_diff(float(local.target_price), float(broker.target_order_price))
        if bps > tolerances.price_drift_bps:
            return ReconciliationDecision(
                kind="price_drift",
                severity="warn",
                delta_payload={
                    "leg": "target",
                    "local_price": float(local.target_price),
                    "broker_price": float(broker.target_order_price),
                    "drift_bps": round(bps, 2),
                },
            )

    # ── agree ────────────────────────────────────────────────────
    return ReconciliationDecision(
        kind="agree",
        severity="info",
        delta_payload={},
    )
