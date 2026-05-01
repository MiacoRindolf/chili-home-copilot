"""L2 order-book aggregator (F2).

Maintains a per-ticker mirror of the live Coinbase Advanced Trade
``level2`` channel: a snapshot followed by incremental updates of
(side, price_level, new_size). Sizes of 0 mean the level was removed.

Coinbase event shape (one event per message, often batched):
    {
        "channel": "l2_data",
        "events": [
            {
                "type": "snapshot" | "update",
                "product_id": "BTC-USD",
                "updates": [
                    {"side": "bid"|"offer",
                     "event_time": "...",
                     "price_level": "67000.55",
                     "new_quantity": "0.123"},
                    ...
                ]
            },
            ...
        ]
    }

We do NOT persist every L2 event — the event stream can be 1000s/sec
which would saturate the DB queue and drown out bar writes (those must
never drop). Instead we sample at most one ``BookItem`` per ticker per
``emit_interval_s`` (default 0.25s), producing top-N levels per side
plus pre-computed imbalance and spread_bps.

Memory bound: each ticker holds two ``dict[float, float]`` (bid, ask)
keyed by price level. In production Coinbase L2 routinely emits 1000+
distinct price levels per side; we cap each side at
``max_levels_per_side`` to keep this from growing unbounded if a
malformed update never deletes a level.

Top-N for emission is ``output_levels`` (default 25) — the same as the
``CHILI_FAST_PATH_BOOK_DEPTH`` setting.

NOTE: L2 sizes can be float (crypto) or string ("0", "0.000000") in
the wire format. We coerce to float and treat 0.0 as "remove this
level".
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)


# Coinbase L2 sides: "bid" and "offer" (NOT "ask"). We normalize to
# bid/ask internally for caller-facing API.
_SIDE_BID = "bid"
_SIDE_ASK = "offer"


@dataclass
class _PerTickerBook:
    """A single ticker's running price→size view."""

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    # Last time we *emitted* a BookItem for this ticker (monotonic seconds).
    last_emit_at: float = 0.0
    # Did we receive a snapshot? Until then, updates are kept but the
    # book is incomplete and we shouldn't emit (top-of-book might be wrong).
    has_snapshot: bool = False
    # Total updates applied since boot — diagnostic.
    updates_applied: int = 0


class OrderBookAggregator:
    """In-memory L2 mirror with sampled emission of BookItems.

    Single-threaded usage is assumed (asyncio task on the WS client).
    """

    def __init__(
        self,
        *,
        output_levels: int = 25,
        emit_interval_s: float = 0.25,
        max_levels_per_side: int = 5_000,
    ) -> None:
        self._output_levels = max(1, int(output_levels))
        self._emit_interval = max(0.0, float(emit_interval_s))
        self._max_levels = max(self._output_levels * 4, int(max_levels_per_side))
        self._books: dict[str, _PerTickerBook] = {}
        # Diagnostic counters surfaced via stats().
        self.snapshots_received = 0
        self.updates_received = 0
        self.updates_applied = 0
        self.malformed_updates = 0
        self.books_emitted = 0
        self.emissions_skipped_no_snapshot = 0
        self.emissions_skipped_throttled = 0
        self.emissions_skipped_empty = 0
        self.last_unknown_side: str | None = None

    # ── Apply incoming L2 events ──────────────────────────────────────

    def apply_event(self, event: dict) -> None:
        """Apply a single Coinbase l2_data event (snapshot or update).

        Returns nothing; book is mutated in place. Errors on individual
        rows are counted but do not abort the event.
        """
        ev_type = event.get("type")
        ticker = event.get("product_id")
        updates = event.get("updates") or []
        if not ticker or not isinstance(updates, list):
            return
        book = self._books.get(ticker)
        if book is None:
            book = _PerTickerBook()
            self._books[ticker] = book

        if ev_type == "snapshot":
            self.snapshots_received += 1
            # Snapshot replaces prior state — Coinbase doesn't guarantee
            # monotonic ordering between reconnects.
            book.bids.clear()
            book.asks.clear()
            self._apply_rows(book, updates)
            book.has_snapshot = True
        elif ev_type == "update":
            self.updates_received += 1
            self._apply_rows(book, updates)
        # Unknown event types (forward compat) are ignored.

    def _apply_rows(self, book: _PerTickerBook, rows: Iterable[dict]) -> None:
        for row in rows:
            try:
                side = row.get("side")
                price = float(row.get("price_level"))
                size = float(row.get("new_quantity"))
            except (TypeError, ValueError):
                self.malformed_updates += 1
                continue
            if side == _SIDE_BID:
                target = book.bids
            elif side == _SIDE_ASK:
                target = book.asks
            else:
                self.malformed_updates += 1
                self.last_unknown_side = side
                continue
            if size <= 0.0:
                # Coinbase sends size=0 to remove a level.
                target.pop(price, None)
            else:
                target[price] = size
            book.updates_applied += 1
            self.updates_applied += 1
        # Cap each side; if a producer bug or stale state grows it
        # past the cap, drop the worst-priced levels (they wouldn't make
        # the top-N emission anyway).
        if len(book.bids) > self._max_levels:
            self._trim(book.bids, self._max_levels, descending=True)
        if len(book.asks) > self._max_levels:
            self._trim(book.asks, self._max_levels, descending=False)

    @staticmethod
    def _trim(side: dict[float, float], keep_n: int, *, descending: bool) -> None:
        # Keep the keep_n best-priced entries (highest for bids, lowest for asks).
        if len(side) <= keep_n:
            return
        prices = sorted(side.keys(), reverse=descending)
        for p in prices[keep_n:]:
            side.pop(p, None)

    # ── Query / emit ──────────────────────────────────────────────────

    def maybe_emit(self, ticker: str, *, now_monotonic: float | None = None,
                   now_wall: datetime | None = None) -> dict | None:
        """Return a serialised BookItem-shaped dict if it's time to emit
        a snapshot for this ticker, else None.

        Caller (ws_client) is responsible for converting the dict into a
        ``BookItem`` and calling ``db_writer.enqueue_book``. Returning a
        dict instead of importing BookItem keeps this module decoupled
        from db_writer for unit testability.
        """
        book = self._books.get(ticker)
        if book is None or not book.has_snapshot:
            self.emissions_skipped_no_snapshot += 1
            return None
        now_m = now_monotonic if now_monotonic is not None else time.monotonic()
        if (now_m - book.last_emit_at) < self._emit_interval:
            self.emissions_skipped_throttled += 1
            return None
        if not book.bids or not book.asks:
            # Crossed-or-empty book; can happen mid-snapshot or after
            # full level deletion. Don't emit — wait for the next update.
            self.emissions_skipped_empty += 1
            return None

        # Top-N best per side: bids descending, asks ascending.
        bid_prices = sorted(book.bids.keys(), reverse=True)[:self._output_levels]
        ask_prices = sorted(book.asks.keys())[:self._output_levels]
        bid_levels = [(p, book.bids[p]) for p in bid_prices]
        ask_levels = [(p, book.asks[p]) for p in ask_prices]
        bid_total = sum(s for _, s in bid_levels)
        ask_total = sum(s for _, s in ask_levels)

        best_bid = bid_levels[0][0] if bid_levels else 0.0
        best_ask = ask_levels[0][0] if ask_levels else 0.0
        mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else 0.0
        spread_bps = ((best_ask - best_bid) / mid) * 10_000.0 if mid > 0 else 0.0
        # Imbalance ∈ [0,1]: 0.5 = balanced; >0.5 = bid-heavy. Skip if total is 0.
        denom = bid_total + ask_total
        imbalance = (bid_total / denom) if denom > 0 else 0.5

        book.last_emit_at = now_m
        self.books_emitted += 1
        return {
            "ticker": ticker,
            "snapshot_at": now_wall or datetime.now(timezone.utc).replace(tzinfo=None),
            "bid_levels": bid_levels,
            "ask_levels": ask_levels,
            "bid_total_size": float(bid_total),
            "ask_total_size": float(ask_total),
            "imbalance": float(imbalance),
            "spread_bps": float(spread_bps),
        }

    # ── Observability ─────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "snapshots_received": self.snapshots_received,
            "updates_received": self.updates_received,
            "updates_applied": self.updates_applied,
            "malformed_updates": self.malformed_updates,
            "books_emitted": self.books_emitted,
            "emissions_skipped_no_snapshot": self.emissions_skipped_no_snapshot,
            "emissions_skipped_throttled": self.emissions_skipped_throttled,
            "emissions_skipped_empty": self.emissions_skipped_empty,
            "tickers_tracked": len(self._books),
            "total_levels_held": sum(
                len(b.bids) + len(b.asks) for b in self._books.values()
            ),
            "last_unknown_side": self.last_unknown_side,
        }


__all__ = ["OrderBookAggregator"]
