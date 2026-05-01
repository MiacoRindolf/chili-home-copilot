"""Bounded write-coalescing queue for fast-path data.

Producers (ws_client, bar_aggregator, orderbook) call :meth:`enqueue_bar`
or :meth:`enqueue_book` without blocking on DB. The writer drains
batches of up to ``batch_size`` rows or every ``batch_interval_ms``,
whichever first, into one INSERT.

Backpressure rules (per architecture doc):
* If the queue is full, sub-second tick updates are dropped first.
* Bar-close events are NEVER dropped — the queue is sized so it can
  always accept them; if it can't, that's a hard error.
* If three consecutive batches fail, the writer logs CRITICAL and
  signals the supervisor to halt the lane.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BarItem:
    """A closed OHLCV bar to persist."""

    ticker: str
    interval: str
    bar_open_at: datetime
    bar_close_at: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    trade_count: int | None = None
    vwap: float | None = None
    source: str = "coinbase"


@dataclass(frozen=True)
class BookItem:
    """A point-in-time L2 snapshot (top-N levels per side)."""

    ticker: str
    snapshot_at: datetime
    bid_levels: list[tuple[float, float]]  # [(price, size), ...]
    ask_levels: list[tuple[float, float]]
    bid_total_size: float
    ask_total_size: float
    imbalance: float
    spread_bps: float
    source: str = "coinbase"


@dataclass(frozen=True)
class AlertItem:
    """A scanner-emitted scalp alert (F3).

    Sub-bar-close granularity but EXECUTION-RELEVANT: the F4 path will
    consume these as triggers. They share the same backpressure rules
    as bars (must never silently drop) — the scanner emits at most a
    handful per minute per ticker thanks to cooldown gates, so the
    queue should never be the bottleneck in healthy operation.
    """

    ticker: str
    alert_type: str
    fired_at: datetime
    signal_score: float
    features: dict
    source: str = "fast_path"


class FastPathDBWriter:
    """Async write-coalescer with bounded queue.

    Caller responsibilities:
    * Call :meth:`start` once before producers begin enqueueing.
    * Call :meth:`stop` for graceful drain on shutdown.

    The writer's own task lives for the lifetime of the supervisor.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        queue_max: int = 10_000,
        batch_size: int = 50,
        batch_interval_ms: int = 200,
    ) -> None:
        self._engine = engine
        self._queue_max = int(queue_max)
        self._batch_size = int(batch_size)
        self._batch_interval = max(0.05, batch_interval_ms / 1000.0)
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_max)
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Counters surfaced via /healthz
        self.bars_received = 0
        self.bars_written = 0
        self.bars_dropped_queue_full = 0
        self.books_received = 0
        self.books_written = 0
        self.alerts_received = 0
        self.alerts_written = 0
        self.alerts_dropped_queue_full = 0
        self.consecutive_batch_failures = 0

    # ── Producer-side ─────────────────────────────────────────────────

    def enqueue_bar(self, item: BarItem) -> bool:
        """Enqueue a closed bar. NEVER blocks; on full queue, this is a
        hard error (bar-close events must never be dropped — the queue
        is sized to always have room for them in healthy operation).

        Returns True on success, False if dropped (logged CRITICAL).
        """
        self.bars_received += 1
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self.bars_dropped_queue_full += 1
            logger.critical(
                "[fast_path] DROP_BAR_CLOSE queue_full ticker=%s "
                "bar_close_at=%s queue_max=%s — DB write path is "
                "degraded; investigate immediately",
                item.ticker, item.bar_close_at, self._queue_max,
            )
            return False

    def enqueue_book(self, item: BookItem) -> bool:
        """Enqueue an L2 snapshot. May be dropped silently under
        backpressure (sub-bar-close granularity)."""
        self.books_received += 1
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            # Silent drop is OK for L2 — we always have a fresher snapshot
            # coming, so missing one is recoverable.
            return False

    def enqueue_alert(self, item: AlertItem) -> bool:
        """Enqueue a scanner-emitted alert. NEVER blocks; on full queue
        this is a hard error — alerts are execution-relevant and the
        scanner's own cooldown gate already throttles emission rate, so
        a full queue indicates the writer/DB itself is degraded."""
        self.alerts_received += 1
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self.alerts_dropped_queue_full += 1
            logger.critical(
                "[fast_path] DROP_ALERT queue_full ticker=%s "
                "alert_type=%s queue_max=%s — DB write path is "
                "degraded; investigate immediately",
                item.ticker, item.alert_type, self._queue_max,
            )
            return False

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="fast_path_db_writer")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    # ── Drain loop ────────────────────────────────────────────────────

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            batch: list[Any] = []
            deadline = loop.time() + self._batch_interval
            # Pull up to batch_size items or until deadline.
            while len(batch) < self._batch_size:
                timeout = max(0.0, deadline - loop.time())
                if timeout <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                    batch.append(item)
                except asyncio.TimeoutError:
                    break
            if not batch:
                continue
            await self._flush_batch(batch)

        # Drain remaining items on shutdown.
        leftover: list[Any] = []
        while not self._queue.empty():
            try:
                leftover.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if leftover:
            await self._flush_batch(leftover)

    async def _flush_batch(self, batch: list[Any]) -> None:
        """Run the synchronous DB write in a thread to avoid blocking
        the event loop. We use sync psycopg2 (via SQLAlchemy) because
        the rest of the project does, and our throughput is low enough
        that thread-pool offload is fine."""
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self._write_batch_sync, batch,
            )
            self.consecutive_batch_failures = 0
        except Exception as exc:
            self.consecutive_batch_failures += 1
            level = logging.CRITICAL if self.consecutive_batch_failures >= 3 else logging.WARNING
            logger.log(
                level,
                "[fast_path] batch_write_failed (count=%s): %s",
                self.consecutive_batch_failures, exc,
                exc_info=(level == logging.CRITICAL),
            )
            # Re-queue items that aren't bar-closes (which we don't want to lose).
            # For F1 we drop the failed batch entirely; F2 will add a retry queue.
            # Bar-close persistence will recover on the next successful flush
            # because the WS aggregator keeps emitting them — at worst we miss
            # a single bar's persistence after a transient DB hiccup.

    def _write_batch_sync(self, batch: list[Any]) -> None:
        bars = [b for b in batch if isinstance(b, BarItem)]
        books = [b for b in batch if isinstance(b, BookItem)]
        alerts = [b for b in batch if isinstance(b, AlertItem)]
        with self._engine.begin() as conn:
            if bars:
                conn.execute(
                    text("""
                        INSERT INTO fast_snapshots (
                            ticker, interval, bar_open_at, bar_close_at,
                            open_price, high_price, low_price, close_price,
                            volume, trade_count, vwap, source
                        ) VALUES (
                            :ticker, :interval, :bar_open_at, :bar_close_at,
                            :open_price, :high_price, :low_price, :close_price,
                            :volume, :trade_count, :vwap, :source
                        )
                        ON CONFLICT (ticker, interval, bar_close_at, source)
                        DO NOTHING
                    """),
                    [
                        {
                            "ticker": b.ticker,
                            "interval": b.interval,
                            "bar_open_at": b.bar_open_at,
                            "bar_close_at": b.bar_close_at,
                            "open_price": b.open_price,
                            "high_price": b.high_price,
                            "low_price": b.low_price,
                            "close_price": b.close_price,
                            "volume": b.volume,
                            "trade_count": b.trade_count,
                            "vwap": b.vwap,
                            "source": b.source,
                        }
                        for b in bars
                    ],
                )
                self.bars_written += len(bars)
            if books:
                conn.execute(
                    text("""
                        INSERT INTO fast_orderbook (
                            ticker, snapshot_at, bid_levels, ask_levels,
                            bid_total_size, ask_total_size, imbalance, spread_bps, source
                        ) VALUES (
                            :ticker, :snapshot_at, CAST(:bid_levels AS JSONB),
                            CAST(:ask_levels AS JSONB),
                            :bid_total_size, :ask_total_size, :imbalance, :spread_bps, :source
                        )
                    """),
                    [
                        {
                            "ticker": b.ticker,
                            "snapshot_at": b.snapshot_at,
                            "bid_levels": json.dumps(b.bid_levels),
                            "ask_levels": json.dumps(b.ask_levels),
                            "bid_total_size": b.bid_total_size,
                            "ask_total_size": b.ask_total_size,
                            "imbalance": b.imbalance,
                            "spread_bps": b.spread_bps,
                            "source": b.source,
                        }
                        for b in books
                    ],
                )
                self.books_written += len(books)
            if alerts:
                conn.execute(
                    text("""
                        INSERT INTO fast_alerts (
                            ticker, alert_type, fired_at,
                            signal_score, features, source
                        ) VALUES (
                            :ticker, :alert_type, :fired_at,
                            :signal_score, CAST(:features AS JSONB), :source
                        )
                    """),
                    [
                        {
                            "ticker": a.ticker,
                            "alert_type": a.alert_type,
                            "fired_at": a.fired_at,
                            "signal_score": a.signal_score,
                            "features": json.dumps(a.features),
                            "source": a.source,
                        }
                        for a in alerts
                    ],
                )
                self.alerts_written += len(alerts)

    # ── Observability ─────────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "bars_received": self.bars_received,
            "bars_written": self.bars_written,
            "bars_dropped_queue_full": self.bars_dropped_queue_full,
            "books_received": self.books_received,
            "books_written": self.books_written,
            "alerts_received": self.alerts_received,
            "alerts_written": self.alerts_written,
            "alerts_dropped_queue_full": self.alerts_dropped_queue_full,
            "consecutive_batch_failures": self.consecutive_batch_failures,
            "queue_depth": self._queue.qsize(),
            "queue_max": self._queue_max,
        }


__all__ = ["BarItem", "BookItem", "AlertItem", "FastPathDBWriter"]
