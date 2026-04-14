"""Real-time microstructure features derived from L2 order book and trade tape.

Provides in-memory ring buffers for order book snapshots and trades,
plus feature computations (imbalance, depth, aggression) consumed by
scanners and alert logic for breakout confirmation.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

_DEFAULT_DEPTH_LEVELS = 10
_DEFAULT_BOOK_HISTORY = 120  # seconds of book snapshots to keep
_DEFAULT_TRADE_HISTORY = 300  # seconds of trade tape to keep


@dataclass
class BookLevel:
    price: float
    size: float
    side: str  # "bid" or "offer"


@dataclass
class BookSnapshot:
    product_id: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    ts: float = field(default_factory=time.time)


@dataclass
class TapeTrade:
    product_id: str
    price: float
    size: float
    side: str  # "BUY" or "SELL" (taker side)
    ts: float = field(default_factory=time.time)


@dataclass
class MicrostructureFeatures:
    """Snapshot of microstructure signals for a single product."""

    product_id: str
    bid_ask_imbalance: float | None = None  # >1 = bid heavy, <1 = ask heavy
    depth_bid_total: float | None = None
    depth_ask_total: float | None = None
    spread_bps: float | None = None
    trade_aggression: float | None = None  # ratio of taker-buy volume to total
    tape_vwap: float | None = None
    absorption_ratio: float | None = None  # bid absorption near best ask
    book_depth_levels: int = 0
    sample_window_secs: float = 0.0
    computed_at: float = field(default_factory=time.time)


class OrderBookBuffer:
    """Thread-safe ring buffer for per-product L2 book snapshots."""

    def __init__(self, max_age_secs: float = _DEFAULT_BOOK_HISTORY) -> None:
        self._max_age = max_age_secs
        self._lock = threading.Lock()
        self._books: dict[str, deque[BookSnapshot]] = {}

    def update(self, snap: BookSnapshot) -> None:
        with self._lock:
            buf = self._books.setdefault(snap.product_id, deque(maxlen=2000))
            buf.append(snap)
            self._prune(snap.product_id)

    def latest(self, product_id: str) -> BookSnapshot | None:
        with self._lock:
            buf = self._books.get(product_id)
            return buf[-1] if buf else None

    def recent(self, product_id: str, window_secs: float = 30.0) -> list[BookSnapshot]:
        cutoff = time.time() - window_secs
        with self._lock:
            buf = self._books.get(product_id, deque())
            return [s for s in buf if s.ts >= cutoff]

    def _prune(self, product_id: str) -> None:
        cutoff = time.time() - self._max_age
        buf = self._books.get(product_id)
        if buf:
            while buf and buf[0].ts < cutoff:
                buf.popleft()


class TradeBuffer:
    """Thread-safe ring buffer for per-product trade tape."""

    def __init__(self, max_age_secs: float = _DEFAULT_TRADE_HISTORY) -> None:
        self._max_age = max_age_secs
        self._lock = threading.Lock()
        self._trades: dict[str, deque[TapeTrade]] = {}

    def append(self, trade: TapeTrade) -> None:
        with self._lock:
            buf = self._trades.setdefault(trade.product_id, deque(maxlen=10000))
            buf.append(trade)
            self._prune(trade.product_id)

    def recent(self, product_id: str, window_secs: float = 60.0) -> list[TapeTrade]:
        cutoff = time.time() - window_secs
        with self._lock:
            buf = self._trades.get(product_id, deque())
            return [t for t in buf if t.ts >= cutoff]

    def _prune(self, product_id: str) -> None:
        cutoff = time.time() - self._max_age
        buf = self._trades.get(product_id)
        if buf:
            while buf and buf[0].ts < cutoff:
                buf.popleft()


# ── Global singletons ──────────────────────────────────────────────
_book_buffer = OrderBookBuffer()
_trade_buffer = TradeBuffer()


def get_book_buffer() -> OrderBookBuffer:
    return _book_buffer


def get_trade_buffer() -> TradeBuffer:
    return _trade_buffer


# ── Feature computation ────────────────────────────────────────────

def compute_features(
    product_id: str,
    *,
    book_buf: OrderBookBuffer | None = None,
    trade_buf: TradeBuffer | None = None,
    trade_window_secs: float = 60.0,
) -> MicrostructureFeatures:
    """Compute microstructure features for a product from buffered data."""
    bb = book_buf or _book_buffer
    tb = trade_buf or _trade_buffer

    feat = MicrostructureFeatures(product_id=product_id)

    snap = bb.latest(product_id)
    if snap and snap.bids and snap.asks:
        bid_total = sum(l.size for l in snap.bids)
        ask_total = sum(l.size for l in snap.asks)
        feat.depth_bid_total = round(bid_total, 6)
        feat.depth_ask_total = round(ask_total, 6)
        feat.book_depth_levels = min(len(snap.bids), len(snap.asks))

        if ask_total > 0:
            feat.bid_ask_imbalance = round(bid_total / ask_total, 4)

        best_bid = snap.bids[0].price
        best_ask = snap.asks[0].price
        mid = (best_bid + best_ask) / 2
        if mid > 0:
            feat.spread_bps = round((best_ask - best_bid) / mid * 10000, 2)

        # Absorption: bid size within 0.1% of best ask (liquidity absorbing sells)
        threshold = best_ask * 0.001
        absorbed = sum(l.size for l in snap.bids if best_ask - l.price <= threshold)
        if bid_total > 0:
            feat.absorption_ratio = round(absorbed / bid_total, 4)

    trades = tb.recent(product_id, window_secs=trade_window_secs)
    if trades:
        feat.sample_window_secs = trade_window_secs
        buy_vol = sum(t.size for t in trades if t.side == "BUY")
        sell_vol = sum(t.size for t in trades if t.side == "SELL")
        total_vol = buy_vol + sell_vol
        if total_vol > 0:
            feat.trade_aggression = round(buy_vol / total_vol, 4)

        # VWAP of recent tape
        notional = sum(t.price * t.size for t in trades)
        size_sum = sum(t.size for t in trades)
        if size_sum > 0:
            feat.tape_vwap = round(notional / size_sum, 8)

    return feat


def get_features(product_id: str) -> MicrostructureFeatures:
    """Convenience: compute features using global buffers."""
    return compute_features(product_id)


def get_features_dict(product_id: str) -> dict[str, Any]:
    """Return features as a plain dict for JSON serialization."""
    f = get_features(product_id)
    return {
        "product_id": f.product_id,
        "bid_ask_imbalance": f.bid_ask_imbalance,
        "depth_bid_total": f.depth_bid_total,
        "depth_ask_total": f.depth_ask_total,
        "spread_bps": f.spread_bps,
        "trade_aggression": f.trade_aggression,
        "tape_vwap": f.tape_vwap,
        "absorption_ratio": f.absorption_ratio,
        "book_depth_levels": f.book_depth_levels,
        "sample_window_secs": f.sample_window_secs,
    }
