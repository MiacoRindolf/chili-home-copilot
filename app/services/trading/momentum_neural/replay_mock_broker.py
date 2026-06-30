"""Replay v3 P0b — MockBrokerAdapter (standalone, provably-inert scaffolding).

A drop-in ``VenueAdapter`` (``app/services/trading/venue/protocol.py:133-183``) that the
Replay v3 FSM driver will pass to ``tick_live_session(..., adapter_factory=)`` so the REAL
live runner can be stepped over historical data with ZERO real broker / network I/O.

P0 scope (this module) is the SKELETON + interface conformance + a SIMPLE deterministic
fill model:

  * BBO comes from an *injected* recorded NBBO (one ``RecordedQuote`` per product), NOT a
    network read. ``get_best_bid_ask`` returns ``(NormalizedTicker, FreshnessMeta)`` stamped
    at the injected sim clock so the runner's stale-quote checks compare sim-to-sim.
  * Orders fill DETERMINISTICALLY at the recorded NBBO using the *pure paper-fill math*
    (``paper_execution.long_entry_fill_price`` / ``long_exit_fill_price`` /
    ``roundtrip_fee_usd``) — REUSE, not a re-derivation. A long entry (buy) crosses the ask
    + adverse slippage; an exit (sell) crosses the bid − slippage.
  * No BBO at ``t`` ⇒ ``get_best_bid_ask`` returns ``(None, …)`` and any place is REJECTED
    (``ok=False, error="no_bbo"``) — the RVMDW/warrant-class path the live runner branches on.
  * NO partials, NO ack-timeouts, NO fault injection — those are P1 (this is the skeleton).

It is intentionally NOT wired into the live runner here (that is P1). It is standalone:
importable, instantiable, and unit-testable in isolation. It places NO real orders and makes
NO network calls — every output is derived purely from the injected recorded NBBO/price.

See docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §2.1 / §4 (P0).
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ..venue.protocol import (
    FreshnessMeta,
    NormalizedFill,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
)
from .paper_execution import (
    long_entry_fill_price,
    long_exit_fill_price,
    roundtrip_fee_usd,
)

_log = logging.getLogger(__name__)

_VENUE = "replay_mock"


@dataclass(frozen=True)
class RecordedQuote:
    """A single recorded NBBO snapshot the mock fills against (as-of the sim clock).

    Mirrors the recorded ``momentum_nbbo_spread_tape`` shape (bid/ask, optional last). The
    driver reconstructs one of these per product as-of the simulated instant and injects it
    via ``set_quote`` before the tick."""

    bid: float
    ask: float
    last: Optional[float] = None

    @property
    def mid(self) -> float:
        return (float(self.bid) + float(self.ask)) / 2.0

    def is_valid(self) -> bool:
        try:
            b, a = float(self.bid), float(self.ask)
        except (TypeError, ValueError):
            return False
        return b > 0 and a > 0 and a >= b


@dataclass
class _RestingOrder:
    """A deterministically-filled order the runner can poll via ``get_order``.

    P0 fills the order IMMEDIATELY at the recorded NBBO (no resting / partials), so
    ``status`` is terminal (``"filled"``) the moment it is created."""

    order_id: str
    client_order_id: Optional[str]
    product_id: str
    side: str
    order_type: str
    base_size: float
    fill_price: float
    fee: float
    created_time: str
    status: str = "filled"

    def to_normalized(self) -> NormalizedOrder:
        return NormalizedOrder(
            order_id=self.order_id,
            client_order_id=self.client_order_id,
            product_id=self.product_id,
            side=self.side,
            status=self.status,
            order_type=self.order_type,
            filled_size=float(self.base_size),
            average_filled_price=float(self.fill_price),
            created_time=self.created_time,
            raw={"venue": _VENUE, "fee": self.fee},
        )


class MockBrokerAdapter:
    """In-memory ``VenueAdapter`` for Replay v3 — deterministic fills off recorded NBBO.

    Construct one per replay run. Before each tick the driver calls ``set_clock(t)`` and
    ``set_quote(product_id, RecordedQuote(...))`` (or ``clear_quote`` for a quoteless name);
    the unchanged ``tick_live_session`` then reads BBO + places/polls orders against this
    instance exactly as it would the real RH/Coinbase adapter.

    Determinism: identical inputs (injected quotes + clock + a fixed ``slippage_bps``) ⇒
    identical fills, with a monotonic counter for order ids (no UUID/wall-clock). No RNG.
    """

    def __init__(
        self,
        *,
        slippage_bps: float = 0.0,
        fee_to_target_ratio: float = 0.0,
        venue_rt_bps: float | None = 0.0,
        max_age_seconds: float = 15.0,
        enabled: bool = True,
    ) -> None:
        # Injected, per-product recorded NBBO (set as-of the sim clock by the driver).
        self._quotes: dict[str, RecordedQuote] = {}
        self._clock: datetime = datetime.now(timezone.utc).replace(tzinfo=None)
        self._orders: dict[str, _RestingOrder] = {}
        self._fills: list[NormalizedFill] = []
        self._order_seq = itertools.count(1)
        self._slippage_bps = float(slippage_bps)
        self._fee_to_target_ratio = float(fee_to_target_ratio)
        self._venue_rt_bps = venue_rt_bps
        self._max_age_seconds = float(max_age_seconds)
        self._enabled = bool(enabled)

    # ── driver-side injection seams (NOT part of the VenueAdapter protocol) ──────────
    def set_clock(self, t: datetime) -> None:
        """Freeze the broker's quote/fill clock at ``t`` (naive-UTC normalized)."""
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        self._clock = t

    def set_quote(self, product_id: str, quote: RecordedQuote) -> None:
        self._quotes[str(product_id).upper()] = quote

    def clear_quote(self, product_id: str) -> None:
        """Remove a product's quote ⇒ subsequent reads return ``no_bbo`` (RVMDW path)."""
        self._quotes.pop(str(product_id).upper(), None)

    def _quote_for(self, product_id: str) -> Optional[RecordedQuote]:
        q = self._quotes.get(str(product_id).upper())
        if q is None or not q.is_valid():
            return None
        return q

    def _freshness(self) -> FreshnessMeta:
        # Stamp at the sim clock so the runner's freshness checks compare sim-to-sim.
        return FreshnessMeta(
            retrieved_at_utc=self._clock.replace(tzinfo=timezone.utc),
            provider_time_utc=self._clock.replace(tzinfo=timezone.utc),
            max_age_seconds=self._max_age_seconds,
        )

    # ── VenueAdapter protocol surface ───────────────────────────────────────────────
    def is_enabled(self) -> bool:
        return self._enabled

    def get_best_bid_ask(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]:
        fresh = self._freshness()
        q = self._quote_for(product_id)
        if q is None:
            return None, fresh  # no_bbo — the runner emits live_blocked_by_risk reason=no_bbo
        mid = q.mid
        spread_abs = float(q.ask) - float(q.bid)
        spread_bps = (spread_abs / mid) * 10_000.0 if mid > 0 else None
        ticker = NormalizedTicker(
            product_id=str(product_id).upper(),
            bid=float(q.bid),
            ask=float(q.ask),
            mid=mid,
            spread_abs=spread_abs,
            spread_bps=spread_bps,
            last_price=(float(q.last) if q.last is not None else None),
            freshness=fresh,
            raw={"venue": _VENUE},
        )
        return ticker, fresh

    def get_ticker(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]:
        return self.get_best_bid_ask(product_id)

    def get_product(self, product_id: str) -> tuple[Optional[NormalizedProduct], FreshnessMeta]:
        # Minimal "online, fully tradable" product so the runner's tradability gate passes.
        prod = NormalizedProduct(
            product_id=str(product_id).upper(),
            base_currency=str(product_id).upper().split("-")[0],
            quote_currency="USD",
            status="online",
            trading_disabled=False,
            cancel_only=False,
            limit_only=False,
            post_only=False,
            auction_mode=False,
            raw={"venue": _VENUE},
        )
        return prod, self._freshness()

    def get_products(self) -> tuple[list[NormalizedProduct], FreshnessMeta]:
        prods = [self.get_product(pid)[0] for pid in list(self._quotes.keys())]
        return [p for p in prods if p is not None], self._freshness()

    def get_recent_trades(
        self, product_id: str, *, limit: int = 50
    ) -> tuple[list[dict[str, Any]], FreshnessMeta]:
        return [], self._freshness()

    def list_open_orders(
        self, *, product_id: Optional[str] = None, limit: int = 50
    ) -> tuple[list[NormalizedOrder], FreshnessMeta]:
        # P0 fills immediately ⇒ nothing rests open.
        return [], self._freshness()

    def get_order(self, order_id: str) -> tuple[Optional[NormalizedOrder], FreshnessMeta]:
        o = self._orders.get(str(order_id))
        return (o.to_normalized() if o is not None else None), self._freshness()

    def get_fills(
        self, *, product_id: Optional[str] = None, limit: int = 50
    ) -> tuple[list[NormalizedFill], FreshnessMeta]:
        fills = self._fills
        if product_id is not None:
            pid = str(product_id).upper()
            fills = [f for f in fills if f.product_id == pid]
        return list(fills[-int(limit):]), self._freshness()

    def place_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._fill_order(
            product_id=product_id,
            side=side,
            base_size=base_size,
            order_type="market",
            limit_price=None,
            client_order_id=client_order_id,
        )

    def place_limit_order_gtc(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
        extended_hours: bool = False,
    ) -> dict[str, Any]:
        return self._fill_order(
            product_id=product_id,
            side=side,
            base_size=base_size,
            order_type="limit",
            limit_price=limit_price,
            client_order_id=client_order_id,
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        # Always accept (mirrors protocol.cancel_order). P0 has nothing resting to cancel.
        return {"ok": True, "venue": _VENUE, "order_id": str(order_id), "status": "cancelled"}

    def preview_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: Optional[str] = None,
        quote_size: Optional[str] = None,
    ) -> dict[str, Any]:
        q = self._quote_for(product_id)
        if q is None:
            return {"ok": False, "venue": _VENUE, "error": "no_bbo"}
        return {"ok": True, "venue": _VENUE, "mid": q.mid, "bid": q.bid, "ask": q.ask}

    def get_account_snapshot(self) -> dict[str, Any]:
        return {"ok": True, "venue": _VENUE, "data": {}, "raw": {}}

    # ── deterministic fill model (pure paper-fill math) ─────────────────────────────
    def _fill_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        order_type: str,
        limit_price: Optional[str],
        client_order_id: Optional[str],
    ) -> dict[str, Any]:
        q = self._quote_for(product_id)
        if q is None:
            # no_bbo reject — the runner takes the place-failed / no_bbo decline branch.
            return {
                "ok": False,
                "venue": _VENUE,
                "error": "no_bbo",
                "client_order_id": client_order_id,
            }
        try:
            size = float(base_size)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "venue": _VENUE,
                "error": "bad_base_size",
                "client_order_id": client_order_id,
            }
        if not (size > 0):
            return {
                "ok": False,
                "venue": _VENUE,
                "error": "bad_base_size",
                "client_order_id": client_order_id,
            }

        s = str(side).lower()
        if s in ("buy", "bid", "long"):
            # Entry: cross the ask + adverse slippage (REUSE the pure paper math).
            fill_price = long_entry_fill_price(q.ask, q.mid, self._slippage_bps)
        else:
            # Exit: cross the bid − adverse slippage.
            fill_price = long_exit_fill_price(q.bid, q.mid, self._slippage_bps)

        notional = abs(fill_price * size)
        fee = roundtrip_fee_usd(
            notional,
            self._fee_to_target_ratio,
            venue_rt_bps=self._venue_rt_bps,
        )
        order_id = f"{_VENUE}-{next(self._order_seq):08d}"
        created = self._clock.replace(tzinfo=timezone.utc).isoformat()
        resting = _RestingOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            product_id=str(product_id).upper(),
            side=s,
            order_type=order_type,
            base_size=size,
            fill_price=float(fill_price),
            fee=float(fee),
            created_time=created,
            status="filled",
        )
        self._orders[order_id] = resting
        self._fills.append(
            NormalizedFill(
                fill_id=f"{order_id}-f",
                order_id=order_id,
                product_id=str(product_id).upper(),
                side=s,
                size=float(size),
                price=float(fill_price),
                fee=float(fee),
                trade_time=created,
                raw={"venue": _VENUE},
            )
        )
        return {
            "ok": True,
            "venue": _VENUE,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "status": "filled",
            "raw": {
                "fill_price": float(fill_price),
                "filled_size": float(size),
                "fee": float(fee),
                "order_type": order_type,
                "limit_price": limit_price,
            },
        }


def make_mock_broker_factory(adapter: MockBrokerAdapter):
    """Return an ``adapter_factory`` callable (the shape ``tick_live_session`` accepts) that
    yields the *same* singleton mock so the driver can inject quotes/clock across ticks.

    NOTE: not wired into the live runner in P0 — provided for P1 to pass as
    ``adapter_factory=make_mock_broker_factory(mock)``."""

    def _factory() -> MockBrokerAdapter:
        return adapter

    return _factory
