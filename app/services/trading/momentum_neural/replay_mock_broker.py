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
from dataclasses import dataclass, field
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

# ── STEP-2 REALISTIC FILL MODEL — documented base constants ──────────────────────────
#
# These are the ONE-documented-setting bases (the reference FLOORS, per
# [[feedback_adaptive_no_magic]]): the driver may override any of them with an
# adaptive/recorded value (e.g. the per-day printed-volume series, the per-venue
# measured ack-latency percentiles). They exist so a mock constructed with NO driver
# feed still fills conservatively and never fills through an empty tape.
#
# (b) FILL-VOLUME REALISM — cumulative fill ≤ this fraction of the recorded printed
#     volume at-or-through the limit during the order's live window. 0.25 = we assume the
#     replayed order can capture at most 25 % of the shares that actually printed at or
#     through its price while it rested. A conservative participation cap; partial fills
#     result when the printed volume is thin relative to the order size.
DEFAULT_VOLUME_PARTICIPATION_FRAC = 0.25
#
# (c) ACK/LATENCY — the observed distribution of real place→fill latencies measured from
#     ``trading_automation_events`` (live_entry_submitted → live_entry_filled), 2026-07-02
#     live DB: n=218 sessions, plausible (0-60 s) window median ≈ 10.1 s, p25 ≈ 6.3 s,
#     p75 ≈ 27.9 s (full-sample median 12.7 s; a long tail to broker-timeout is excluded
#     from the ack model). The documented FALLBACK BASE ack latency when the driver
#     supplies no measured distribution is this median. The driver's
#     ``set_latency_distribution`` overrides it with the recomputed per-run percentiles.
DEFAULT_ACK_LATENCY_SECONDS = 10.0
DEFAULT_ACK_LATENCY_P25_SECONDS = 6.3
DEFAULT_ACK_LATENCY_P75_SECONDS = 27.9


class FillMode:
    """The explicit conservative/optimistic fill-realism mode (STEP-2 (e)).

    * ``CONSERVATIVE`` (DEFAULT): fills cross the ADVERSE side (buy at ask+slip, sell at
      bid−slip), are VOLUME-CAPPED against the recorded printed volume (partial fills when
      the tape is thin), and pay the full ack latency. This is the floor the operator
      trusts for a lower-bound PnL estimate — it never over-credits a fill the tape could
      not have supplied.
    * ``OPTIMISTIC``: fills cross at the FAVORABLE mid (no adverse slippage), are NOT
      volume-capped (assume full size available), and pay a shorter ack latency. This is
      the upper-bound of the PnL band. It is still bounded by the recorded quote (no
      fill-through-empty-tape), so even optimistic mode is honest about a quoteless name.
    """

    CONSERVATIVE = "conservative"
    OPTIMISTIC = "optimistic"

    @staticmethod
    def normalize(value: Any) -> str:
        v = str(value).strip().lower() if value is not None else FillMode.CONSERVATIVE
        return FillMode.OPTIMISTIC if v == FillMode.OPTIMISTIC else FillMode.CONSERVATIVE


def _float_or_none(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
    """An order the runner can poll via ``get_order``.

    Two fill modes (per the adapter's ``resting_limit_fills`` flag):

      * **immediate** (P0, default): the order fills the moment it is created at the recorded
        NBBO; ``status`` is terminal (``"filled"``) immediately. ``filled_size == base_size``.
      * **resting** (P1): a LIMIT order rests ``status="open"`` with ``filled_size == 0`` until
        a later recorded NBBO CROSSES its limit (buy: ask <= limit; sell: bid >= limit), at
        which point ``_advance`` flips it to ``"filled"`` (or a partial). An optional
        ``ack_delay_ticks`` holds the order ``open`` for N quote advances before it is even
        eligible to cross (exercises the runner's pending-entry ack-poll/timeout path)."""

    order_id: str
    client_order_id: Optional[str]
    product_id: str
    side: str
    order_type: str
    base_size: float
    limit_price: Optional[float]
    created_time: str
    # mutable fill state
    status: str = "filled"
    filled_size: float = 0.0
    fill_price: Optional[float] = None
    fee: float = 0.0
    ack_delay_remaining: int = 0
    partial_first_fill: bool = False  # fill base_size/2 first, the remainder on the next cross
    # STEP-2 volume-cap bookkeeping: the cumulative printed volume at-or-through this
    # order's limit that has been OBSERVED while the order was resting (advanced by the
    # driver via ``set_printed_volume`` between ticks). The order's cumulative fill is
    # capped at ``volume_participation_frac × observed_printed_volume``.
    observed_printed_volume: float = 0.0
    volume_participation_frac: float = 1.0  # 1.0 ⇒ uncapped (P0/P1 backward-compat)

    def to_normalized(self) -> NormalizedOrder:
        return NormalizedOrder(
            order_id=self.order_id,
            client_order_id=self.client_order_id,
            product_id=self.product_id,
            side=self.side,
            status=self.status,
            order_type=self.order_type,
            filled_size=float(self.filled_size),
            average_filled_price=(float(self.fill_price) if self.fill_price is not None else None),
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
        resting_limit_fills: bool = False,
        ack_delay_ticks: int = 0,
        partial_first_fill: bool = False,
        freshness_mode: str = "sim",
        # ── STEP-2 realistic fill model ──────────────────────────────────────────────
        fill_mode: str = FillMode.CONSERVATIVE,
        volume_cap_enabled: bool = False,
        volume_participation_frac: float = DEFAULT_VOLUME_PARTICIPATION_FRAC,
        optimistic_slippage_bps: float = 0.0,
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
        # P1 FIDELITY KNOBS (default OFF ⇒ the P0 immediate-fill model, byte-identical):
        #  * resting_limit_fills: a LIMIT order RESTS open until the recorded NBBO crosses it.
        #  * ack_delay_ticks: hold a resting order `open` for N quote advances before it can
        #    cross (exercises the runner's pending-entry ack-poll/timeout path).
        #  * partial_first_fill: the first cross fills HALF; the remainder fills on the next
        #    cross (exercises the runner's partial-entry/partial-exit bookkeeping).
        self._resting_limit_fills = bool(resting_limit_fills)
        self._ack_delay_ticks = max(0, int(ack_delay_ticks))
        self._partial_first_fill = bool(partial_first_fill)
        # "sim" (P0 contract) | "wall" (P1 driver: quote fresh vs the wall-clock stale gate).
        self._freshness_mode = "wall" if str(freshness_mode).lower() == "wall" else "sim"
        # ── STEP-2 realistic fill model state ────────────────────────────────────────
        # (e) explicit conservative/optimistic mode. Conservative = adverse-side crossing
        #     + volume cap + full ack latency (the trustworthy lower bound). Optimistic =
        #     favorable mid + no volume cap + shorter latency (the upper bound).
        self._fill_mode = FillMode.normalize(fill_mode)
        # (b) fill-VOLUME realism. When enabled, a resting order's cumulative fill is capped
        #     at ``volume_participation_frac × observed_printed_volume`` (the printed volume
        #     at-or-through its limit while it rested, fed by ``set_printed_volume``). Default
        #     OFF ⇒ P0/P1 backward-compat (uncapped). Optimistic mode also bypasses the cap.
        self._volume_cap_enabled = bool(volume_cap_enabled) and self._fill_mode == FillMode.CONSERVATIVE
        self._volume_participation_frac = max(0.0, float(volume_participation_frac))
        # OPTIMISTIC slippage: 0.0 (mid-favorable) by default. Kept separate so a run can be
        # deliberately optimistic-but-not-free.
        self._optimistic_slippage_bps = float(optimistic_slippage_bps)
        # (c) ack/latency: the driver may install a measured distribution via
        #     ``set_latency_distribution`` (percentile seconds). Absent one, the documented
        #     fallback base (module constants) is used to derive a per-order ack-delay.
        self._ack_latency_seconds: float = (
            DEFAULT_ACK_LATENCY_SECONDS
            if self._fill_mode == FillMode.CONSERVATIVE
            else DEFAULT_ACK_LATENCY_P25_SECONDS
        )
        # Per-product printed-volume observed while an order rests (advanced by the driver).
        self._printed_volume_pending: dict[str, float] = {}

    # ── driver-side injection seams (NOT part of the VenueAdapter protocol) ──────────
    def set_clock(self, t: datetime) -> None:
        """Freeze the broker's quote/fill clock at ``t`` (naive-UTC normalized).

        In resting mode this is the tick that ADVANCES resting orders: decrement their
        ack-delay and re-test the cross against the current per-product quote."""
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        self._clock = t
        if self._resting_limit_fills:
            self._advance_resting_orders()

    def set_quote(self, product_id: str, quote: RecordedQuote) -> None:
        self._quotes[str(product_id).upper()] = quote
        if self._resting_limit_fills:
            # A new quote can satisfy a resting cross immediately (the driver may set the clock
            # then the quote, or only the quote, between ticks) — re-test on quote arrival too.
            self._advance_resting_orders(product_id=str(product_id).upper())

    def clear_quote(self, product_id: str) -> None:
        """Remove a product's quote ⇒ subsequent reads return ``no_bbo`` (RVMDW path)."""
        self._quotes.pop(str(product_id).upper(), None)

    # ── STEP-2 realistic-fill driver seams (NOT part of the VenueAdapter protocol) ────
    def set_printed_volume(self, product_id: str, printed_volume: float) -> None:
        """Feed the recorded printed volume that traded AT-OR-THROUGH a resting order's
        limit during THIS advance window (from ``iqfeed_trade_ticks`` prints the driver
        selected as-of ``t`` with ``price <= ask``/``>= bid`` and inside the limit). The
        volume-cap fill model consumes it: each still-``open`` order for the product accrues
        this volume, and its cumulative fill is capped at ``frac × observed_printed_volume``.

        Additive across advances — the driver passes the INCREMENT since the last advance
        (or the full at-or-through-limit volume for a single-advance immediate order)."""
        pid = str(product_id).upper()
        inc = max(0.0, float(printed_volume))
        self._printed_volume_pending[pid] = self._printed_volume_pending.get(pid, 0.0) + inc
        # accrue onto every resting order for this product, then re-test the cross/cap
        for ro in self._orders.values():
            if ro.status == "open" and ro.product_id == pid:
                ro.observed_printed_volume += inc
        if self._resting_limit_fills:
            self._advance_resting_orders(product_id=pid)

    def set_latency_distribution(
        self,
        *,
        median_seconds: Optional[float] = None,
        p25_seconds: Optional[float] = None,
        p75_seconds: Optional[float] = None,
    ) -> None:
        """Install the MEASURED place→fill latency distribution (percentile seconds) the
        driver recomputed from ``trading_automation_events`` for this run. Conservative mode
        uses the median (the trustworthy central latency); optimistic uses p25 (fast fills).
        Absent this call the documented fallback base (module constants) applies."""
        if self._fill_mode == FillMode.CONSERVATIVE:
            base = median_seconds if median_seconds is not None else DEFAULT_ACK_LATENCY_SECONDS
        else:
            base = p25_seconds if p25_seconds is not None else DEFAULT_ACK_LATENCY_P25_SECONDS
        self._ack_latency_seconds = max(0.0, float(base))

    def ack_delay_ticks_for(self, tick_seconds: float) -> int:
        """Convert the mode's ack latency (seconds) into an integer number of quote
        advances for a grid whose median spacing is ``tick_seconds``. Deterministic (round
        half-up), never negative. Used by the driver to set ``ack_delay_ticks`` per run so
        the pending-entry ack window reflects the REAL measured broker latency rather than a
        magic constant."""
        if tick_seconds is None or float(tick_seconds) <= 0:
            return 0
        return max(0, int(self._ack_latency_seconds / float(tick_seconds) + 0.5))

    def _quote_for(self, product_id: str) -> Optional[RecordedQuote]:
        q = self._quotes.get(str(product_id).upper())
        if q is None or not q.is_valid():
            return None
        return q

    def _freshness(self) -> FreshnessMeta:
        # The freshness STAMP. Two modes (``freshness_mode``):
        #
        #   * ``"sim"`` (P0 default): stamp at the sim clock — the documented P0 contract
        #     (``test_replay_v3_p0`` pins ``retrieved_at_utc == sim clock``). Honest, but see
        #     the caveat below.
        #   * ``"wall"`` (the P1 DRIVER uses this): stamp at the REAL wall clock so the quote is
        #     fresh by construction.
        #
        # WHY ``"wall"`` exists (documented hidden real-time dep — design R2/R7): the runner's
        # stale-quote gate calls ``protocol.is_fresh_enough(meta)`` WITHOUT a ``now=`` override,
        # so freshness is measured against ``datetime.now(timezone.utc)`` — the WALL clock, NOT
        # the sim clock (``live_runner._utcnow``). It is the ONE market read the P0/P1 clock seam
        # does not reach. Stamping a past sim instant would make EVERY replayed quote look stale
        # and the runner would never leave ``queued_live``. The faithful replay semantics: the
        # injected quote IS the current quote for THIS step, so it is fresh by construction.
        # The gate still works in replay — a ``clear_quote``d name returns no_bbo (not stale),
        # exercising the quoteless decline path. A clean sim-now ``is_fresh_enough`` override
        # threaded from the runner is the P5 cleanup that makes ``"sim"`` viable end-to-end.
        if self._freshness_mode == "wall":
            stamp = datetime.now(timezone.utc)
            return FreshnessMeta(
                retrieved_at_utc=stamp,
                provider_time_utc=None,
                max_age_seconds=self._max_age_seconds,
            )
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
        # Immediate-fill (P0): nothing rests open. Resting (P1): the still-``open`` orders.
        pid = str(product_id).upper() if product_id is not None else None
        opens = [
            o.to_normalized()
            for o in self._orders.values()
            if o.status == "open" and (pid is None or o.product_id == pid)
        ]
        return list(opens[: int(limit)]), self._freshness()

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
        **kwargs: Any,
    ) -> dict[str, Any]:
        # ``**kwargs`` tolerates venue-specific extras the runner threads to the REAL adapters
        # (e.g. time_in_force / overnight / post_only) — the mock ignores them, exactly as a
        # crypto adapter ignores ``overnight``. Keeps the mock a drop-in across families.
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
        **kwargs: Any,
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
        # Always accept (mirrors protocol.cancel_order). In resting mode an ``open`` order is
        # marked ``cancelled`` so a later cross can't fill an order the runner abandoned (the
        # ack-timeout → re-watch path); a partial keeps its already-filled size.
        o = self._orders.get(str(order_id))
        if o is not None and o.status == "open":
            o.status = "cancelled"
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
        order_id = f"{_VENUE}-{next(self._order_seq):08d}"
        created = self._clock.replace(tzinfo=timezone.utc).isoformat()
        _lim = _float_or_none(limit_price)

        # RESTING MODE (P1): a marketable LIMIT does NOT necessarily cross on placement —
        # it rests ``open`` until a recorded NBBO crosses it. A MARKET order (no limit / the
        # exit) still crosses immediately. ``ack_delay_ticks`` holds even a crossable limit
        # ``open`` for N quote advances first (the pending-entry ack window).
        if self._resting_limit_fills and order_type == "limit":
            pid_u = str(product_id).upper()
            ro = _RestingOrder(
                order_id=order_id,
                client_order_id=client_order_id,
                product_id=pid_u,
                side=s,
                order_type=order_type,
                base_size=size,
                limit_price=_lim,
                created_time=created,
                status="open",
                filled_size=0.0,
                fill_price=None,
                fee=0.0,
                ack_delay_remaining=self._ack_delay_ticks,
                partial_first_fill=self._partial_first_fill,
                # STEP-2 (b): stamp the participation cap onto the order (1.0 ⇒ uncapped when
                # volume-capping is off). Seed the observed-printed-volume with whatever has
                # already been fed for this product this window.
                volume_participation_frac=(
                    self._volume_participation_frac if self._volume_cap_enabled else 1.0
                ),
                observed_printed_volume=self._printed_volume_pending.get(pid_u, 0.0),
            )
            self._orders[order_id] = ro
            # An at-or-through-market limit with no ack delay crosses on THIS placement quote.
            self._maybe_cross(ro, q)
            return {
                "ok": True,
                "venue": _VENUE,
                "order_id": order_id,
                "client_order_id": client_order_id,
                "status": ro.status,
                "raw": {
                    "filled_size": float(ro.filled_size),
                    "fill_price": (float(ro.fill_price) if ro.fill_price is not None else None),
                    "fee": float(ro.fee),
                    "order_type": order_type,
                    "limit_price": limit_price,
                },
            }

        # IMMEDIATE MODE (P0 default; and all MARKET orders even in resting mode): cross now.
        fill_price = self._cross_price(s, q)
        ro = _RestingOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            product_id=str(product_id).upper(),
            side=s,
            order_type=order_type,
            base_size=size,
            limit_price=_lim,
            created_time=created,
            status="filled",
        )
        self._orders[order_id] = ro
        # _book_fill sets filled_size/fill_price/fee + appends the NormalizedFill.
        self._book_fill(ro, qty=float(size), price=float(fill_price))
        return {
            "ok": True,
            "venue": _VENUE,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "status": "filled",
            "raw": {
                "fill_price": float(fill_price),
                "filled_size": float(size),
                "fee": float(ro.fee),
                "order_type": order_type,
                "limit_price": limit_price,
            },
        }

    # ── resting-fill mechanics (P1; zero network, deterministic) ─────────────────────
    def _cross_price(self, side: str, q: RecordedQuote) -> float:
        """Fill PRICE for a marketable order against quote ``q`` (REUSE the pure paper math).

        STEP-2 mode-aware:
          * CONSERVATIVE — cross the ADVERSE side: buy at ask + slip, sell at bid − slip.
          * OPTIMISTIC   — cross at the FAVORABLE MID (buy at mid + optimistic_slip, sell at
            mid − optimistic_slip). Still bounded by the recorded quote (mid is inside the
            spread), so optimistic is generous but NEVER prices outside the recorded book."""
        is_buy = str(side).lower() in ("buy", "bid", "long")
        if self._fill_mode == FillMode.OPTIMISTIC:
            # Favorable: fill at the recorded mid (± a small optional optimistic slip).
            if is_buy:
                return long_entry_fill_price(q.mid, q.mid, self._optimistic_slippage_bps)
            return long_exit_fill_price(q.mid, q.mid, self._optimistic_slippage_bps)
        # Conservative (default): adverse-side crossing.
        if is_buy:
            return long_entry_fill_price(q.ask, q.mid, self._slippage_bps)
        return long_exit_fill_price(q.bid, q.mid, self._slippage_bps)

    def _limit_crosses(self, ro: _RestingOrder, q: RecordedQuote) -> bool:
        """A resting LIMIT crosses when the recorded NBBO trades through it: a BUY limit
        crosses once the ask is at/below the limit; a SELL limit once the bid is at/above it.
        A limit of ``None`` (defensive) is treated as marketable (always crosses)."""
        if ro.limit_price is None:
            return True
        if ro.side in ("buy", "bid", "long"):
            return float(q.ask) <= float(ro.limit_price) + 1e-12
        return float(q.bid) >= float(ro.limit_price) - 1e-12

    def _volume_cap_available(self, ro: _RestingOrder) -> Optional[float]:
        """STEP-2 (b): the additional qty this order may fill given the printed volume it has
        observed at-or-through its limit. Returns ``None`` when volume-capping is not active
        for this order (⇒ no cap, P0/P1 behavior). When active, returns
        ``max(0, frac × observed_printed_volume − already_filled)`` — a partial results when
        the tape was thin, and 0 (no fill) when NO volume printed through the limit yet
        (the no-fill-through-empty-tape property)."""
        if not self._volume_cap_enabled or ro.volume_participation_frac >= 1.0:
            return None
        allowed_total = ro.volume_participation_frac * float(ro.observed_printed_volume)
        return max(0.0, allowed_total - float(ro.filled_size))

    def _maybe_cross(self, ro: _RestingOrder, q: Optional[RecordedQuote]) -> None:
        """Advance one resting order against the current quote: respect the ack delay, then
        fill (or partial-fill) when the limit crosses, capped by the observed printed volume.
        Idempotent on terminal orders."""
        if ro.status != "open" or q is None or not q.is_valid():
            return
        if ro.ack_delay_remaining > 0:
            ro.ack_delay_remaining -= 1
            return
        if not self._limit_crosses(ro, q):
            return
        remaining = ro.base_size - ro.filled_size
        if remaining <= 0:
            ro.status = "filled"
            return
        # STEP-2 (b) VOLUME CAP: never fill more than frac × printed-volume-through-limit.
        cap = self._volume_cap_available(ro)
        if cap is not None:
            fillable = min(remaining, cap)
            if fillable <= 1e-9:
                # No (more) printed volume available through the limit yet ⇒ stay open,
                # accrue on later advances. This is the no-fill-through-empty-tape guarantee.
                return
            px = self._cross_price(ro.side, q)
            self._book_fill(ro, qty=fillable, price=px)
            # terminal only once the FULL size is filled; else keep resting for more volume
            ro.status = "filled" if (ro.base_size - ro.filled_size) <= 1e-9 else "open"
            return
        # PARTIAL: the first cross fills half, leaving the order ``open`` for the next cross.
        if ro.partial_first_fill and ro.filled_size <= 0 and remaining > 1e-9:
            half = remaining / 2.0
            px = self._cross_price(ro.side, q)
            self._book_fill(ro, qty=half, price=px)
            ro.status = "open"  # stays resting for the remainder
            return
        px = self._cross_price(ro.side, q)
        self._book_fill(ro, qty=remaining, price=px)
        ro.status = "filled"

    def _advance_resting_orders(self, *, product_id: Optional[str] = None) -> None:
        """Re-test every still-``open`` resting order against its product's current quote."""
        for ro in self._orders.values():
            if ro.status != "open":
                continue
            if product_id is not None and ro.product_id != product_id:
                continue
            self._maybe_cross(ro, self._quote_for(ro.product_id))

    def _book_fill(self, ro: _RestingOrder, *, qty: float, price: float) -> None:
        """Record a (possibly partial) fill on ``ro`` + append a NormalizedFill. Updates the
        size-weighted average fill price + accrues the proportional fee."""
        if qty <= 0:
            return
        prev_filled = ro.filled_size
        prev_px = ro.fill_price if ro.fill_price is not None else price
        new_filled = prev_filled + qty
        # size-weighted average across partials
        ro.fill_price = (prev_px * prev_filled + price * qty) / new_filled if new_filled > 0 else price
        ro.filled_size = new_filled
        notional = abs(price * qty)
        ro.fee += roundtrip_fee_usd(
            notional, self._fee_to_target_ratio, venue_rt_bps=self._venue_rt_bps
        )
        self._fills.append(
            NormalizedFill(
                fill_id=f"{ro.order_id}-f{len([f for f in self._fills if f.order_id == ro.order_id]) + 1}",
                order_id=ro.order_id,
                product_id=ro.product_id,
                side=ro.side,
                size=float(qty),
                price=float(price),
                fee=float(roundtrip_fee_usd(notional, self._fee_to_target_ratio, venue_rt_bps=self._venue_rt_bps)),
                trade_time=self._clock.replace(tzinfo=timezone.utc).isoformat(),
                raw={"venue": _VENUE},
            )
        )


def make_mock_broker_factory(adapter: MockBrokerAdapter):
    """Return an ``adapter_factory`` callable (the shape ``tick_live_session`` accepts) that
    yields the *same* singleton mock so the driver can inject quotes/clock across ticks.

    NOTE: not wired into the live runner in P0 — provided for P1 to pass as
    ``adapter_factory=make_mock_broker_factory(mock)``."""

    def _factory() -> MockBrokerAdapter:
        return adapter

    return _factory
