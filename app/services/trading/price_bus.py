"""Unified real-time price bus — aggregates quotes from Massive WS (stocks)
and Coinbase WS (crypto) into a single in-process cache + listener system.

Consumers:
  - Hybrid paper runner (tick-level stop/target exits, candle-close entries)
  - /ws/autopilot/live (streaming chart data to the UI)
  - fetch_quote fallback (read from cache before hitting REST)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Quote snapshot (unified across providers)
# ---------------------------------------------------------------------------

@dataclass
class BusQuote:
    symbol: str
    bid: float | None = None
    ask: float | None = None
    mid: float = 0.0
    last: float = 0.0
    timestamp: float = 0.0
    source: str = ""
    provider_event_at: float | None = None
    received_at: float | None = None
    available_at: float | None = None
    provider_sequence: int | None = None
    provider_run_id: str | None = None
    provider_connection_generation: int | None = None
    # Empty preserves legacy provider behavior; Massive explicitly labels
    # quote vs trade so NBBO mids cannot masquerade as prints in live candles.
    event_kind: str = ""

BUS_QUOTE_STALENESS = 5.0  # seconds

TickCallback = Callable[[str, BusQuote], None]
CandleCallback = Callable[[str, "BusCandle"], None]


@dataclass
class BusCandle:
    symbol: str
    interval_seconds: int
    bucket_start: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int
    closed: bool = False


# ---------------------------------------------------------------------------
# Price Bus singleton
# ---------------------------------------------------------------------------

class PriceBus:
    """In-process price aggregator with listener dispatch."""

    def __init__(self) -> None:
        self._quotes: dict[str, BusQuote] = {}
        self._quotes_lock = threading.Lock()

        self._tick_listeners: dict[str, list[TickCallback]] = {}
        self._tick_lock = threading.Lock()

        self._candle_bars: dict[str, BusCandle] = {}
        self._candle_lock = threading.Lock()
        self._candle_listeners: dict[str, list[CandleCallback]] = {}
        self._candle_listeners_lock = threading.Lock()

        self._candle_interval = 60  # 1m

        self._massive_bridge_active = False
        self._coinbase_bridge_active = False

    # ── quote cache ──────────────────────────────────────────────────

    def update_quote(self, symbol: str, quote: BusQuote) -> None:
        """Push a quote into the cache and fire tick listeners."""
        sym = symbol.upper()
        quote.symbol = sym
        with self._quotes_lock:
            self._quotes[sym] = quote
        self._fire_tick(sym, quote)
        if quote.last > 0 and quote.event_kind != "quote":
            event_at = (
                quote.provider_event_at
                if quote.provider_event_at is not None
                else quote.timestamp
            )
            self._on_trade_tick(sym, quote.last, event_at)

    def get_quote(self, symbol: str) -> BusQuote | None:
        """Return cached quote if fresh (within staleness window)."""
        sym = symbol.upper()
        with self._quotes_lock:
            q = self._quotes.get(sym)
        if q is None:
            return None
        now = time.time()
        received_at = q.received_at if q.received_at is not None else q.timestamp
        if now - received_at > BUS_QUOTE_STALENESS or now - received_at < -1.0:
            return None
        if q.provider_event_at is not None:
            provider_age = now - q.provider_event_at
            if provider_age > BUS_QUOTE_STALENESS or provider_age < -1.0:
                return None
        if q.available_at is not None:
            available_age = now - q.available_at
            if available_age > BUS_QUOTE_STALENESS or available_age < -1.0:
                return None
        return q

    # ── tick listeners ───────────────────────────────────────────────

    def register_tick_listener(self, symbol: str, cb: TickCallback) -> None:
        sym = symbol.upper()
        with self._tick_lock:
            self._tick_listeners.setdefault(sym, []).append(cb)

    def unregister_tick_listener(self, symbol: str, cb: TickCallback) -> None:
        sym = symbol.upper()
        with self._tick_lock:
            cbs = self._tick_listeners.get(sym)
            if cbs:
                try:
                    cbs.remove(cb)
                except ValueError:
                    pass
                if not cbs:
                    del self._tick_listeners[sym]

    def _fire_tick(self, sym: str, quote: BusQuote) -> None:
        with self._tick_lock:
            cbs = list(self._tick_listeners.get(sym, []))
        for cb in cbs:
            try:
                cb(sym, quote)
            except Exception:
                pass

    # ── 1m candle aggregation ────────────────────────────────────────

    def _on_trade_tick(self, sym: str, price: float, ts: float) -> None:
        bucket_start = (ts // self._candle_interval) * self._candle_interval
        with self._candle_lock:
            bar = self._candle_bars.get(sym)
            if bar is None or bar.bucket_start != bucket_start:
                if bar is not None:
                    bar.closed = True
                    self._fire_candle(sym, bar)
                self._candle_bars[sym] = BusCandle(
                    symbol=sym,
                    interval_seconds=self._candle_interval,
                    bucket_start=bucket_start,
                    open=price, high=price, low=price, close=price,
                    volume=0.0, trade_count=1,
                )
            else:
                bar.high = max(bar.high, price)
                bar.low = min(bar.low, price)
                bar.close = price
                bar.trade_count += 1

    def get_current_candle(self, symbol: str) -> BusCandle | None:
        sym = symbol.upper()
        with self._candle_lock:
            bar = self._candle_bars.get(sym)
        return bar

    def register_candle_listener(self, symbol: str, cb: CandleCallback) -> None:
        sym = symbol.upper()
        with self._candle_listeners_lock:
            self._candle_listeners.setdefault(sym, []).append(cb)

    def unregister_candle_listener(self, symbol: str, cb: CandleCallback) -> None:
        sym = symbol.upper()
        with self._candle_listeners_lock:
            cbs = self._candle_listeners.get(sym)
            if cbs:
                try:
                    cbs.remove(cb)
                except ValueError:
                    pass
                if not cbs:
                    del self._candle_listeners[sym]

    def _fire_candle(self, sym: str, bar: BusCandle) -> None:
        with self._candle_listeners_lock:
            cbs = list(self._candle_listeners.get(sym, []))
        for cb in cbs:
            try:
                cb(sym, bar)
            except Exception:
                pass

    # ── provider bridges ─────────────────────────────────────────────

    def bridge_massive_ws(self) -> None:
        """Wire Massive WS tick listeners into the price bus."""
        if self._massive_bridge_active:
            return
        try:
            from ..massive_client import (
                register_tick_listener as _massive_reg,
                QuoteSnapshot as _MassiveQuoteSnap,
                TradeSnapshot as _MassiveTradeSnap,
            )
        except ImportError:
            _log.debug("[price_bus] massive_client not available")
            return

        def _on_massive_tick(sym: str, snap) -> None:
            now = time.time()
            provider_event_at = getattr(snap, "provider_event_at", None)
            received_at = getattr(snap, "received_at", None)
            available_at = getattr(snap, "available_at", None)
            # Massive stock Q/T messages provide an exact SIP clock.  A frame
            # missing it, arriving delayed, or stamped in the future is raw
            # capture evidence only and must not drive the operational bus.
            if provider_event_at is None:
                return
            if now - provider_event_at > BUS_QUOTE_STALENESS or now - provider_event_at < -1.0:
                return
            if hasattr(snap, "bid"):
                q = BusQuote(
                    symbol=sym, bid=snap.bid, ask=snap.ask, mid=snap.price,
                    last=snap.price,
                    timestamp=received_at if received_at is not None else snap.timestamp,
                    source="massive_ws",
                    provider_event_at=provider_event_at,
                    received_at=received_at,
                    available_at=available_at,
                    provider_sequence=getattr(snap, "sequence", None),
                    provider_run_id=getattr(snap, "bridge_run_id", None),
                    provider_connection_generation=getattr(
                        snap, "connection_generation", None
                    ),
                    event_kind="quote",
                )
            else:
                q = BusQuote(
                    symbol=sym, mid=snap.price, last=snap.price,
                    timestamp=received_at if received_at is not None else snap.timestamp,
                    source="massive_ws_trade",
                    provider_event_at=provider_event_at,
                    received_at=received_at,
                    available_at=available_at,
                    provider_sequence=getattr(snap, "sequence", None),
                    provider_run_id=getattr(snap, "bridge_run_id", None),
                    provider_connection_generation=getattr(
                        snap, "connection_generation", None
                    ),
                    event_kind="trade",
                )
            self.update_quote(sym, q)

        self._massive_tick_cb = _on_massive_tick
        self._massive_bridge_active = True
        _log.info("[price_bus] Massive WS bridge active")

    def bridge_coinbase_ws(self) -> None:
        """Wire Coinbase WS tick listeners into the price bus."""
        if self._coinbase_bridge_active:
            return
        try:
            from .venue.coinbase_spot import get_coinbase_ws
            cb_ws = get_coinbase_ws()
            if not cb_ws.enabled or not cb_ws._running:
                _log.debug("[price_bus] Coinbase WS not running, bridge deferred")
                return
        except ImportError:
            return

        self._coinbase_bridge_active = True
        _log.info("[price_bus] Coinbase WS bridge active")

    def subscribe_massive(self, symbol: str) -> None:
        """Subscribe a symbol on Massive WS and register the bridge callback."""
        if not self._massive_bridge_active:
            self.bridge_massive_ws()
        try:
            from ..massive_client import get_ws_client, register_tick_listener
            ws = get_ws_client()
            from ..massive_client import to_massive_ticker
            m_ticker = to_massive_ticker(symbol)
            if ws.running:
                ws.subscribe([m_ticker])
                register_tick_listener(m_ticker, self._massive_tick_cb)
        except Exception as e:
            _log.debug("[price_bus] Massive subscribe failed for %s: %s", symbol, e)

    def subscribe_coinbase(self, product_id: str) -> None:
        """Subscribe a crypto product on Coinbase WS and bridge into price bus."""
        try:
            from .venue.coinbase_spot import get_coinbase_ws
            cb_ws = get_coinbase_ws()
            if not cb_ws._running:
                return

            def _on_cb_tick(pid: str, snap: dict) -> None:
                mid = snap.get("mid", 0.0)
                if mid <= 0:
                    return
                q = BusQuote(
                    symbol=pid, bid=snap.get("bid"), ask=snap.get("ask"),
                    mid=mid, last=snap.get("last", mid),
                    timestamp=snap.get("timestamp", time.time()),
                    source=snap.get("source", "coinbase_ws"),
                )
                self.update_quote(pid, q)

            cb_ws.register_tick_listener(product_id, _on_cb_tick)
            cb_ws.subscribe([product_id])
            if not self._coinbase_bridge_active:
                self._coinbase_bridge_active = True
                _log.info("[price_bus] Coinbase WS bridge active")
        except Exception as e:
            _log.debug("[price_bus] Coinbase subscribe failed for %s: %s", product_id, e)

    def subscribe_symbol(self, symbol: str) -> None:
        """Auto-route: crypto -> Coinbase WS, stocks -> Massive WS."""
        from ..massive_client import is_crypto
        sym = symbol.strip().upper()
        if is_crypto(sym):
            pid = sym.replace("-USD", "-USD")  # Coinbase uses BTC-USD format
            self.subscribe_coinbase(pid)
        else:
            self.subscribe_massive(sym)

    # ── Robinhood REST supplemental polling ────────────────────────────

    def start_robinhood_poll(self, symbols: list[str], interval_sec: float = 30.0) -> None:
        """Poll Robinhood REST quotes for stock symbols every *interval_sec*
        and feed into the bus as a supplemental accuracy reference."""
        if not symbols:
            return

        def _poll_loop():
            while True:
                time.sleep(interval_sec)
                try:
                    from .venue.robinhood_spot import RobinhoodSpotAdapter
                    rh = RobinhoodSpotAdapter()
                    for sym in symbols:
                        try:
                            ticker_data, _ = rh.get_best_bid_ask(sym)
                            if ticker_data and ticker_data.mid and ticker_data.mid > 0:
                                q = BusQuote(
                                    symbol=sym.upper(),
                                    bid=ticker_data.bid,
                                    ask=ticker_data.ask,
                                    mid=ticker_data.mid,
                                    last=ticker_data.last_price or ticker_data.mid,
                                    timestamp=time.time(),
                                    source="robinhood_rest",
                                )
                                # Only update if no fresher WS quote exists
                                existing = self.get_quote(sym)
                                if existing is None or existing.source == "robinhood_rest":
                                    self.update_quote(sym, q)
                        except Exception:
                            pass
                except Exception as e:
                    _log.debug("[price_bus] Robinhood poll cycle failed: %s", e)

        t = threading.Thread(target=_poll_loop, daemon=True, name="price-bus-rh-poll")
        t.start()
        _log.info("[price_bus] Robinhood REST poll started for %d symbols (every %.0fs)", len(symbols), interval_sec)

    # ── status ───────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        with self._quotes_lock:
            syms = sorted(self._quotes.keys())
        with self._tick_lock:
            listener_syms = sorted(self._tick_listeners.keys())
        return {
            "cached_symbols": syms,
            "listener_symbols": listener_syms,
            "massive_bridge": self._massive_bridge_active,
            "coinbase_bridge": self._coinbase_bridge_active,
        }


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_bus: PriceBus | None = None
_bus_lock = threading.Lock()


def get_price_bus() -> PriceBus:
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = PriceBus()
    return _bus


def get_live_quote(symbol: str) -> dict[str, Any] | None:
    """Convenience: read from bus cache, return dict compatible with fetch_quote."""
    bus = get_price_bus()
    q = bus.get_quote(symbol)
    if q is None:
        return None
    return {
        "price": q.mid,
        "bid": q.bid,
        "ask": q.ask,
        "last": q.last,
        "source": q.source,
        "timestamp": q.timestamp,
        "provider_event_at": q.provider_event_at,
        "received_at": q.received_at,
        "available_at": q.available_at,
        "provider_sequence": q.provider_sequence,
        "provider_run_id": q.provider_run_id,
        "provider_connection_generation": q.provider_connection_generation,
        "event_kind": q.event_kind,
    }
