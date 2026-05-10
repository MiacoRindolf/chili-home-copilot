"""Coinbase orphan-stop adoption pass (f-coinbase-orphan-stop-adoption, 2026-05-10).

Background
----------
After ``f-coinbase-post-place-verify-routing-fix`` (commit ``c8a3ff3``) sealed
the Robinhood-routing bug that caused Coinbase stop placements to be marked
``unverified``, four trades (AERGO, 1INCH, ACX, RARE) were left in a state
where the broker had a working SELL stop-limit order at the venue but the
local ``trading_bracket_intents`` row had ``broker_stop_order_id IS NULL``.
The orphan stops reserve qty at the venue, so subsequent
``place_missing_stop`` attempts now fail with
``"Insufficient balance in source account"``.

This module is the one-shot adoption pass that closes that gap.

Contract
--------
For each (open Coinbase trade, open Coinbase SELL stop-limit order at the
venue) pair where:

* the local trade has ``broker_source = 'coinbase'`` and an existing
  ``trading_bracket_intents`` row whose ``broker_stop_order_id`` is NULL,
* there is exactly one such naked intent for the ticker,
* there is exactly one open Coinbase SELL stop-limit order for the matching
  product_id (``f"{ticker}-USD"``),
* the broker order's ``base_size`` matches the local intent's ``quantity``
  within a 1% relative tolerance,

the pass:

1. Persists ``broker_stop_order_id`` via the existing audited writer
   :func:`app.services.trading.bracket_intent_writer.sync_broker_stop_order_id_mirror`.
2. Transitions the intent to ``reconciled``:
   * ``intent`` / ``confirmed_at_broker`` / ``amending`` →
     :func:`app.services.trading.bracket_intent_writer.transition`
     (RECONCILED is a legal target from each).
   * ``terminal_reject`` → the audited bypass
     :func:`app.services.trading.bracket_intent_writer.mark_auto_reconciled_after_terminal_reject`.

Ambiguous cases (multiple naked intents for the ticker, multiple open broker
orders for the ticker, qty mismatch beyond tolerance, missing broker qty)
are LOGGED and SKIPPED — never guessed (per ``COWORK_ADVISOR_BRIEF`` §2.6
"no magic-fallback values").

Coinbase API failures raise :class:`VenueAdapterError` to the caller — we
do NOT silently swallow.

Public surface
--------------
:func:`adopt_coinbase_orphan_stops` — call site for the dispatch script and
the test suite. Returns a structured report; does NOT print.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..bracket_intent_writer import (
    IntentState,
    _coerce_state,
    mark_auto_reconciled_after_terminal_reject,
    sync_broker_stop_order_id_mirror,
    transition,
)
from .coinbase_spot import CoinbaseSpotAdapter
from .protocol import NormalizedOrder, VenueAdapterError

logger = logging.getLogger(__name__)

# Adoption candidates: states from which a transition to reconciled is
# either directly legal (per `_LEGAL_TRANSITIONS`) or supported via the
# audited terminal_reject bypass. Anything outside this set is excluded
# from the naked-intent SELECT — protects already-reconciled rows, closed
# rows, and shadow_logged rows from being touched.
_ADOPTABLE_STATES: frozenset[str] = frozenset({
    "intent",
    "confirmed_at_broker",
    "amending",
    "terminal_reject",
})

# Quantity match tolerance. Coinbase rounds base_size to the product's
# base_increment (e.g. AERGO is 0.01); the local intent.quantity comes
# from the autotrader's pre-rounded compute. 1% relative is the
# expected rounding noise — anything looser would be guessing per the
# brief's no-magic-fallback rule.
_QTY_REL_TOLERANCE: float = 0.01


@dataclass(frozen=True)
class _NakedIntent:
    """Row from the naked-intent SELECT. Frozen so accidental in-place
    mutation can't pollute the matching pass."""

    intent_id: int
    trade_id: int
    ticker: str
    quantity: float
    intent_state: str
    broker_source: str


@dataclass
class _AdoptionReport:
    """Mutable accumulator for the adoption pass result. Converted to a
    plain dict by :func:`_to_dict` at return time so the public surface
    is JSON-friendly."""

    ok: bool = True
    dry_run: bool = True
    open_stop_orders_examined: int = 0
    naked_intents_examined: int = 0
    adoptions: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────


def _ticker_to_product_id(ticker: str) -> str:
    t = (ticker or "").upper().strip()
    if not t:
        return ""
    return t if t.endswith("-USD") else f"{t}-USD"


def _product_id_to_ticker(product_id: str) -> str:
    p = (product_id or "").upper().strip()
    return p[:-4] if p.endswith("-USD") else p


def _sf(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_broker_qty(order: NormalizedOrder) -> Optional[float]:
    """Coinbase returns the ordered size in ``base_size`` on the raw payload.
    Some SDK versions surface it as ``size`` or ``original_size`` instead.
    Prefer ``base_size``; fall back through synonyms before giving up.

    Returns None when no parseable size field exists — caller logs and
    skips (no magic fallback).
    """
    raw = order.raw or {}
    for key in ("base_size", "size", "original_size", "leaves_quantity"):
        v = _sf(raw.get(key))
        if v is not None and v > 0:
            return v
    # Some SDK shapes nest the stop-limit fields one level deeper.
    config = raw.get("order_configuration")
    if isinstance(config, dict):
        for sub in ("stop_limit_stop_limit_gtc", "stop_limit_stop_limit_gtd"):
            inner = config.get(sub)
            if isinstance(inner, dict):
                v = _sf(inner.get("base_size"))
                if v is not None and v > 0:
                    return v
    return None


def _is_stop_limit_sell(order: NormalizedOrder) -> bool:
    """SELL stop-limit identifier — case-insensitive substring match on
    ``order_type``. Coinbase has emitted both ``STOP_LIMIT`` and
    ``stop_limit_stop_limit_gtc`` across SDK versions."""
    if (order.side or "").lower() != "sell":
        return False
    ot = (order.order_type or "").lower()
    if "stop_limit" in ot:
        return True
    # Fallback: detect via raw order_configuration shape.
    raw = order.raw or {}
    config = raw.get("order_configuration")
    if isinstance(config, dict):
        for key in config.keys():
            if "stop_limit" in str(key).lower():
                return True
    return False


def _qty_within_tolerance(qty_local: float, qty_broker: float) -> bool:
    """1% relative tolerance, with the local qty as denominator. A
    non-positive local qty fails closed (we never invent a match)."""
    if qty_local is None or qty_broker is None:
        return False
    if qty_local <= 0 or qty_broker <= 0:
        return False
    rel = abs(qty_local - qty_broker) / qty_local
    return rel <= _QTY_REL_TOLERANCE


def _load_naked_coinbase_intents(db: Session) -> list[_NakedIntent]:
    """SELECT bracket_intents JOIN trades for OPEN Coinbase trades whose
    intent has no persisted broker_stop_order_id and whose intent_state
    is one of the adoptable states.

    Excluded by construction:

    * paper trades (``broker_source IS NULL``),
    * already-adopted intents (``broker_stop_order_id IS NOT NULL``),
    * closed / reconciled / shadow_logged rows.
    """
    rows = db.execute(text("""
        SELECT
            bi.id           AS intent_id,
            bi.trade_id     AS trade_id,
            bi.ticker       AS ticker,
            bi.quantity     AS quantity,
            bi.intent_state AS intent_state,
            bi.broker_source AS broker_source
        FROM trading_bracket_intents bi
        JOIN trading_trades t ON t.id = bi.trade_id
        WHERE t.status = 'open'
          AND bi.broker_source = 'coinbase'
          AND bi.broker_stop_order_id IS NULL
          AND LOWER(bi.intent_state) = ANY(:states)
        ORDER BY bi.id
    """), {"states": list(_ADOPTABLE_STATES)}).fetchall()

    out: list[_NakedIntent] = []
    for r in rows:
        try:
            out.append(_NakedIntent(
                intent_id=int(r[0]),
                trade_id=int(r[1]),
                ticker=str(r[2] or "").upper(),
                quantity=float(r[3] or 0.0),
                intent_state=str(r[4] or "").lower(),
                broker_source=str(r[5] or "").lower(),
            ))
        except (TypeError, ValueError) as e:
            logger.warning(
                "[coinbase_orphan_adopt] skipping malformed naked-intent row "
                "intent_id=%s: %s", r[0], e,
            )
    return out


def _list_open_coinbase_stops(adapter: CoinbaseSpotAdapter) -> list[NormalizedOrder]:
    """Pull all open Coinbase orders, filter to SELL stop-limit. Raises
    :class:`VenueAdapterError` on adapter failure (the caller decides
    whether to surface or retry)."""
    orders, _fresh = adapter.list_open_orders(product_id=None, limit=250)
    return [o for o in orders if _is_stop_limit_sell(o)]


def _group_by_product(
    orders: Iterable[NormalizedOrder],
) -> dict[str, list[NormalizedOrder]]:
    by_pid: dict[str, list[NormalizedOrder]] = {}
    for o in orders:
        pid = (o.product_id or "").upper().strip()
        if not pid:
            continue
        by_pid.setdefault(pid, []).append(o)
    return by_pid


def _group_intents_by_ticker(
    intents: Iterable[_NakedIntent],
) -> dict[str, list[_NakedIntent]]:
    by_t: dict[str, list[_NakedIntent]] = {}
    for it in intents:
        t = (it.ticker or "").upper().strip()
        if not t:
            continue
        by_t.setdefault(t, []).append(it)
    return by_t


# ── Persistence ────────────────────────────────────────────────────────


def _persist_adoption(
    db: Session,
    *,
    intent: _NakedIntent,
    broker_order_id: str,
) -> tuple[bool, str, str]:
    """Persist broker_stop_order_id + transition state to reconciled.

    Returns ``(ok, prev_state_value, new_state_value)``. Does NOT commit;
    the caller commits the batch at end-of-pass.

    On failure (transition rejected by state machine), logs and returns
    ``(False, prev, new)``. Mirror write is best-effort: a mirror failure
    after a state transition succeeded is logged but not raised.
    """
    prev_state = (intent.intent_state or "").lower()

    # Mirror write must happen first — if the state transition succeeds
    # but the order-id mirror has not been persisted, the next sweep sees
    # a "reconciled but order-id NULL" row which the reconciler can't act
    # on. Order matters.
    try:
        sync_broker_stop_order_id_mirror(
            db, intent.intent_id, broker_value=broker_order_id,
        )
    except Exception:
        logger.exception(
            "[coinbase_orphan_adopt] mirror write failed for intent_id=%s",
            intent.intent_id,
        )
        return (False, prev_state, prev_state)

    if prev_state == "terminal_reject":
        # Audited bypass — the standard state machine forbids
        # terminal_reject → reconciled, but
        # mark_auto_reconciled_after_terminal_reject is the documented
        # writer for exactly this case (broker subsequently agrees).
        try:
            ok = mark_auto_reconciled_after_terminal_reject(db, intent.intent_id)
        except Exception:
            logger.exception(
                "[coinbase_orphan_adopt] terminal_reject auto-reconcile failed "
                "for intent_id=%s", intent.intent_id,
            )
            return (False, prev_state, prev_state)
        new_state = "reconciled" if ok else prev_state
        return (bool(ok), prev_state, new_state)

    # Standard transition — RECONCILED is a legal target from intent /
    # confirmed_at_broker / amending per `_LEGAL_TRANSITIONS`.
    try:
        result = transition(
            db,
            intent.intent_id,
            to_state=IntentState.RECONCILED,
            reason="orphan_adopt",
        )
    except Exception:
        logger.exception(
            "[coinbase_orphan_adopt] transition failed for intent_id=%s",
            intent.intent_id,
        )
        return (False, prev_state, prev_state)

    if not result.ok:
        logger.warning(
            "[coinbase_orphan_adopt] transition rejected for intent_id=%s "
            "prev=%s reason=%s", intent.intent_id, prev_state, result.reason,
        )
        return (False, prev_state, prev_state)
    return (True, prev_state, IntentState.RECONCILED.value)


# ── Public surface ─────────────────────────────────────────────────────


def adopt_coinbase_orphan_stops(
    db: Session,
    *,
    adapter: Optional[CoinbaseSpotAdapter] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Run the adoption pass and return a structured report.

    Parameters
    ----------
    db : Session
        SQLAlchemy session. Caller controls the engine; on dry_run=False
        this function commits the batch at the end.
    adapter : CoinbaseSpotAdapter, optional
        Coinbase venue adapter. Constructed via the default factory when
        omitted (the test suite injects a stub).
    dry_run : bool
        When True (default), no DB writes happen — the report shows what
        WOULD be adopted. When False, mirror writes + transitions are
        applied and committed.

    Returns
    -------
    dict
        Structured report with keys: ``ok``, ``dry_run``,
        ``open_stop_orders_examined``, ``naked_intents_examined``,
        ``adoptions`` (list), ``skipped`` (list), ``errors`` (list).

    Raises
    ------
    VenueAdapterError
        If the Coinbase API is unreachable or the adapter is misconfigured.
        Per the brief: "Coinbase API unreachable: raise, don't silently
        swallow."
    """
    report = _AdoptionReport(dry_run=bool(dry_run))

    if adapter is None:
        adapter = CoinbaseSpotAdapter()

    # Step 1: pull broker truth. Raises VenueAdapterError on adapter
    # failure — propagated to caller per brief.
    open_stops = _list_open_coinbase_stops(adapter)
    report.open_stop_orders_examined = len(open_stops)

    # Step 2: pull naked intents.
    naked_intents = _load_naked_coinbase_intents(db)
    report.naked_intents_examined = len(naked_intents)

    if not open_stops or not naked_intents:
        logger.info(
            "[coinbase_orphan_adopt] nothing to do: "
            "open_stops=%s naked_intents=%s dry_run=%s",
            len(open_stops), len(naked_intents), dry_run,
        )
        return _to_dict(report)

    # Step 3: bipartite match by ticker.
    orders_by_pid = _group_by_product(open_stops)
    intents_by_ticker = _group_intents_by_ticker(naked_intents)

    # Iterate the union of tickers/product_ids so we surface BOTH
    # "multiple intents, no orders" AND "multiple orders, no intents"
    # as visible skips in the report (operator can investigate).
    all_tickers = set(intents_by_ticker.keys()) | {
        _product_id_to_ticker(pid) for pid in orders_by_pid.keys()
    }

    for ticker in sorted(all_tickers):
        pid = _ticker_to_product_id(ticker)
        intent_candidates = intents_by_ticker.get(ticker, [])
        order_candidates = orders_by_pid.get(pid, [])

        # Ambiguity skips — log + skip, do NOT guess.
        if len(intent_candidates) > 1:
            report.skipped.append({
                "ticker": ticker,
                "reason": "multiple_intents",
                "detail": f"{len(intent_candidates)} naked intents for {ticker}",
                "intent_ids": [i.intent_id for i in intent_candidates],
            })
            logger.warning(
                "[coinbase_orphan_adopt] skip ticker=%s reason=multiple_intents "
                "intent_ids=%s", ticker, [i.intent_id for i in intent_candidates],
            )
            continue
        if len(order_candidates) > 1:
            report.skipped.append({
                "ticker": ticker,
                "reason": "multiple_orders",
                "detail": f"{len(order_candidates)} open Coinbase stops for {pid}",
                "order_ids": [o.order_id for o in order_candidates],
            })
            logger.warning(
                "[coinbase_orphan_adopt] skip ticker=%s reason=multiple_orders "
                "order_ids=%s", ticker, [o.order_id for o in order_candidates],
            )
            continue
        if not intent_candidates:
            # Open broker order, no naked intent — not actionable from
            # this pass (could be a bracket already reconciled, or a
            # manual venue-side order). Quiet skip.
            report.skipped.append({
                "ticker": ticker,
                "reason": "no_naked_intent",
                "detail": "open Coinbase stop with no matching naked intent",
                "order_ids": [o.order_id for o in order_candidates],
            })
            continue
        if not order_candidates:
            report.skipped.append({
                "ticker": ticker,
                "reason": "no_broker_order",
                "detail": "naked intent with no matching open Coinbase stop",
                "intent_ids": [i.intent_id for i in intent_candidates],
            })
            continue

        intent = intent_candidates[0]
        order = order_candidates[0]
        broker_qty = _extract_broker_qty(order)

        if broker_qty is None:
            report.skipped.append({
                "ticker": ticker,
                "reason": "broker_qty_unparseable",
                "detail": "could not extract base_size from broker order",
                "order_id": order.order_id,
                "intent_id": intent.intent_id,
            })
            logger.warning(
                "[coinbase_orphan_adopt] skip ticker=%s reason=broker_qty_unparseable "
                "order_id=%s", ticker, order.order_id,
            )
            continue

        if not _qty_within_tolerance(intent.quantity, broker_qty):
            report.skipped.append({
                "ticker": ticker,
                "reason": "qty_mismatch",
                "detail": (
                    f"local={intent.quantity} broker={broker_qty} "
                    f"rel_tolerance={_QTY_REL_TOLERANCE}"
                ),
                "order_id": order.order_id,
                "intent_id": intent.intent_id,
                "qty_local": intent.quantity,
                "qty_broker": broker_qty,
            })
            logger.warning(
                "[coinbase_orphan_adopt] skip ticker=%s reason=qty_mismatch "
                "local=%s broker=%s", ticker, intent.quantity, broker_qty,
            )
            continue

        # Match accepted. Build the adoption record (whether dry_run or not).
        adoption = {
            "intent_id": intent.intent_id,
            "trade_id": intent.trade_id,
            "ticker": ticker,
            "broker_stop_order_id": order.order_id,
            "prev_state": intent.intent_state,
            "qty_local": intent.quantity,
            "qty_broker": broker_qty,
        }

        if dry_run:
            adoption["new_state"] = "reconciled"  # planned, not applied
            adoption["applied"] = False
            report.adoptions.append(adoption)
            logger.info(
                "[coinbase_orphan_adopt] DRY-RUN would adopt ticker=%s "
                "intent_id=%s order_id=%s prev_state=%s",
                ticker, intent.intent_id, order.order_id, intent.intent_state,
            )
            continue

        ok, prev_state, new_state = _persist_adoption(
            db, intent=intent, broker_order_id=order.order_id,
        )
        adoption["prev_state"] = prev_state
        adoption["new_state"] = new_state
        adoption["applied"] = bool(ok)
        if ok:
            report.adoptions.append(adoption)
            logger.info(
                "[coinbase_orphan_adopt] APPLIED ticker=%s intent_id=%s "
                "order_id=%s prev_state=%s new_state=%s",
                ticker, intent.intent_id, order.order_id, prev_state, new_state,
            )
        else:
            report.skipped.append({
                "ticker": ticker,
                "reason": "transition_rejected",
                "detail": f"prev_state={prev_state}",
                "intent_id": intent.intent_id,
                "order_id": order.order_id,
            })

    if not dry_run and report.adoptions:
        try:
            db.commit()
            logger.info(
                "[coinbase_orphan_adopt] committed %s adoption(s)",
                len(report.adoptions),
            )
        except Exception as e:
            db.rollback()
            report.ok = False
            report.errors.append({
                "context": "commit",
                "message": str(e),
            })
            logger.exception("[coinbase_orphan_adopt] commit failed")

    return _to_dict(report)


def _to_dict(report: _AdoptionReport) -> dict[str, Any]:
    return {
        "ok": bool(report.ok),
        "dry_run": bool(report.dry_run),
        "open_stop_orders_examined": int(report.open_stop_orders_examined),
        "naked_intents_examined": int(report.naked_intents_examined),
        "adoptions": list(report.adoptions),
        "skipped": list(report.skipped),
        "errors": list(report.errors),
    }


__all__ = [
    "adopt_coinbase_orphan_stops",
]
