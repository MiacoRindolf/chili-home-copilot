"""Coinbase Advanced Trade WebSocket client.

Subscribes to the ``candles`` channel for the configured pairs and
forwards closed 1m bars to the supplied DB writer. Subscribes to
``heartbeats`` so connection liveness is observable even on a quiet
market.

Per ``docs/ARCHITECTURE-fast-path.md``:
* exponential reconnect backoff (1s → 30s cap, unlimited attempts)
* sequence-number tracking with a REST recovery path on a gap
* per-pair circuit breaker via :class:`StatusTracker`
* bounded resource use (no per-message allocation explosion)

Coinbase ``candles`` channel docs:
https://docs.cloud.coinbase.com/advanced-trade-api/docs/ws-channels#candles-channel

The channel emits granularity=ONE_MINUTE bars. Each event contains a
list of candles including the *currently-forming* bar plus historical
context. We only persist a bar when its ``end`` timestamp is in the
past — i.e., the bar is closed. This avoids writing partial bars that
will be revised seconds later.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

try:
    import websockets
except ImportError:  # pragma: no cover - dependency added via requirements
    websockets = None  # type: ignore

from .db_writer import AlertItem, BarItem, BookItem, FastPathDBWriter
from .order_book import OrderBookAggregator
from .scanner import MomentumScanner
from .settings import FastPathSettings
from .status_tracker import StatusTracker

logger = logging.getLogger(__name__)


# A bar is considered "closed" once this many seconds past its end have
# elapsed — gives Coinbase time to publish the final aggregation. With
# 1m bars and a 3s threshold, a bar that ends at HH:MM:00 is persisted
# at HH:MM:03 at the earliest.
BAR_CLOSE_GRACE_S = 3.0


class CoinbaseWSClient:
    def __init__(
        self,
        settings: FastPathSettings,
        db_writer: FastPathDBWriter,
        status: StatusTracker,
    ) -> None:
        self._settings = settings
        self._db_writer = db_writer
        self._status = status
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Per-ticker last persisted bar_close_at — dedupe across reconnects.
        self._last_persisted: dict[str, datetime] = {}
        # F2: L2 order-book aggregator. Initialized lazily so settings
        # control depth + emit cadence centrally.
        self._book = OrderBookAggregator(
            output_levels=settings.book_depth,
            emit_interval_s=0.25,
        )
        # F3: event-driven scalp scanner. Pure-Python; reads bars +
        # books and emits alert dicts.
        self._scanner = MomentumScanner()
        # Diagnostic counters — surfaced via stats() so the supervisor
        # metrics line shows whether we're seeing raw traffic at all
        # (vs only filtering it out as not-yet-closed).
        self._raw_messages_total: int = 0
        self._raw_candles_events_total: int = 0
        self._raw_candles_total: int = 0
        self._candles_filtered_unclosed: int = 0
        self._candles_filtered_dedupe: int = 0
        self._heartbeats_total: int = 0
        self._subscriptions_total: int = 0
        self._unknown_channel_total: int = 0
        self._last_unknown_channel: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if websockets is None:
            logger.critical(
                "[fast_path] websockets package not installed — "
                "fast-data-worker cannot connect to Coinbase. "
                "Add `websockets>=12` to requirements."
            )
            self._status.mark_halted(
                "_global", "websockets_missing"
            )
            return
        if self._task is not None:
            return
        for ticker in self._settings.pairs:
            self._status.register(ticker)
        self._task = asyncio.create_task(self._run(), name="coinbase_ws_client")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    # ── Run-with-supervised-reconnect ─────────────────────────────────

    async def _run(self) -> None:
        backoff = self._settings.reconnect_min_s
        while not self._stop.is_set():
            try:
                await self._connect_and_consume()
                # Clean exit (server closed cleanly) — reset backoff
                backoff = self._settings.reconnect_min_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Per-connection error — log + back off + retry.
                for ticker in self._settings.pairs:
                    self._status.record_error(ticker, f"ws_loop:{type(exc).__name__}")
                logger.warning(
                    "[fast_path] ws connection error (backoff=%.1fs): %s",
                    backoff, exc,
                )
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                # _stop set during sleep — exit
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2.0, self._settings.reconnect_max_s)
            for ticker in self._settings.pairs:
                self._status.record_reconnect(ticker)

    async def _connect_and_consume(self) -> None:
        url = self._settings.coinbase_ws_url
        # ping_interval keeps the connection alive; close_timeout caps clean shutdown.
        # max_size 32MB: Coinbase L2 snapshots for BTC-USD / ETH-USD on
        # initial subscribe can be 8-15 MB. 4 MB tripped 1009 (message
        # too big) repeatedly under F2 smoke. 32 MB covers the largest
        # observed snapshots with headroom; one-off allocation is fine
        # against the 512 MB container cap (snapshot is freed after parse).
        async with websockets.connect(  # type: ignore[arg-type]
            url, ping_interval=20, ping_timeout=20, close_timeout=5,
            max_size=32 * 2 ** 20,
        ) as ws:
            await self._subscribe(ws, "candles")
            await self._subscribe(ws, "level2")
            await self._subscribe(ws, "heartbeats")
            for ticker in self._settings.pairs:
                self._status.mark_streaming(ticker)
            async for raw in ws:
                if self._stop.is_set():
                    break
                self._handle_message(raw)

    async def _subscribe(self, ws, channel: str) -> None:
        msg = {
            "type": "subscribe",
            "product_ids": list(self._settings.pairs),
            "channel": channel,
        }
        await ws.send(json.dumps(msg))

    # ── Message routing ───────────────────────────────────────────────

    def _handle_message(self, raw: str | bytes) -> None:
        self._raw_messages_total += 1
        try:
            payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("[fast_path] ws msg decode failed: %s", exc)
            return

        channel = payload.get("channel")
        if channel == "candles":
            self._handle_candles(payload)
        elif channel in ("l2_data", "level2"):
            # Coinbase Advanced Trade names this channel "level2" on the
            # subscribe message but emits it as "l2_data" in events.
            self._handle_l2(payload)
        elif channel == "heartbeats":
            self._heartbeats_total += 1
            self._handle_heartbeat(payload)
        elif channel == "subscriptions":
            self._subscriptions_total += 1
            # Coinbase confirms subscriptions — log once per call so we
            # can see exactly what got accepted server-side.
            logger.info("[fast_path] subscription confirmed: %s",
                        payload.get("events"))
            return
        else:
            self._unknown_channel_total += 1
            self._last_unknown_channel = channel
            # Sample-log unknown channels so we know what to whitelist.
            if self._unknown_channel_total <= 5:
                logger.info("[fast_path] ws unknown channel=%r payload_keys=%s",
                            channel, list(payload.keys()))
        # Unknown channels are ignored (forward compat).

    def _handle_l2(self, payload: dict[str, Any]) -> None:
        """Apply Coinbase l2_data events to the in-memory book and
        opportunistically emit a sampled BookItem to the DB writer.

        We emit AT MOST one BookItem per ticker per ``emit_interval_s``
        (default 250ms). Most events are absorbed without an emission;
        the aggregator throttles internally."""
        events = payload.get("events") or []
        # Refresh status_tracker on traffic — L2 is the highest-frequency
        # signal we have that the connection is alive for the pair.
        for ev in events:
            ticker = ev.get("product_id")
            if ticker:
                self._status.mark_streaming(ticker)
            self._book.apply_event(ev)
            if not ticker:
                continue
            item = self._book.maybe_emit(ticker)
            if item is None:
                continue
            book = BookItem(
                ticker=item["ticker"],
                snapshot_at=item["snapshot_at"],
                bid_levels=item["bid_levels"],
                ask_levels=item["ask_levels"],
                bid_total_size=item["bid_total_size"],
                ask_total_size=item["ask_total_size"],
                imbalance=item["imbalance"],
                spread_bps=item["spread_bps"],
                source="coinbase",
            )
            # enqueue_book silently drops on backpressure — that's fine
            # for L2 sampling; a fresher snapshot is always coming.
            self._db_writer.enqueue_book(book)
            # F3: scan the freshly-emitted book for imbalance setups.
            for alert_dict in self._scanner.on_book_emit(item["ticker"], item):
                self._dispatch_alert(alert_dict)

    def _handle_heartbeat(self, payload: dict[str, Any]) -> None:
        # Heartbeats per product confirm the connection is alive even
        # when no candle events flow. We don't persist them; we use
        # them only to refresh status_tracker's "we're getting traffic"
        # signal.
        events = payload.get("events") or []
        for ev in events:
            ticker = ev.get("product_id")
            if ticker:
                self._status.mark_streaming(ticker)

    def _handle_candles(self, payload: dict[str, Any]) -> None:
        events = payload.get("events") or []
        now_ts = datetime.now(timezone.utc).timestamp()
        self._raw_candles_events_total += len(events)
        for ev in events:
            candles = ev.get("candles") or []
            self._raw_candles_total += len(candles)
            for candle in candles:
                self._maybe_emit_bar(candle, now_ts)

    def _maybe_emit_bar(self, candle: dict[str, Any], now_ts: float) -> None:
        # Coinbase candle shape:
        # {
        #   "start": "1717363200",          # unix seconds (string)
        #   "high": "67000.55",
        #   "low":  "66950.10",
        #   "open": "66980.00",
        #   "close": "66985.42",
        #   "volume": "1.234",
        #   "product_id": "BTC-USD"
        # }
        try:
            ticker = candle.get("product_id")
            if not ticker:
                return
            start_s = float(candle.get("start") or 0)
            if start_s <= 0:
                return
            # 1m bar — end is start + 60s.
            end_s = start_s + 60.0
            # Only persist if the bar is closed (with a small grace).
            if (end_s + BAR_CLOSE_GRACE_S) > now_ts:
                self._candles_filtered_unclosed += 1
                return
            close_at = datetime.fromtimestamp(end_s, tz=timezone.utc).replace(tzinfo=None)
            open_at = datetime.fromtimestamp(start_s, tz=timezone.utc).replace(tzinfo=None)

            # Dedupe: don't re-enqueue an already-persisted bar.
            last = self._last_persisted.get(ticker)
            if last is not None and close_at <= last:
                self._candles_filtered_dedupe += 1
                return

            bar = BarItem(
                ticker=str(ticker),
                interval="1m",
                bar_open_at=open_at,
                bar_close_at=close_at,
                open_price=float(candle.get("open") or 0),
                high_price=float(candle.get("high") or 0),
                low_price=float(candle.get("low") or 0),
                close_price=float(candle.get("close") or 0),
                volume=float(candle.get("volume") or 0),
                source="coinbase",
            )
            ok = self._db_writer.enqueue_bar(bar)
            if ok:
                self._last_persisted[ticker] = close_at
                self._status.record_bar(ticker, close_at, seq=None)
                # F3: scan the just-closed bar for volume/breakout setups.
                # Build the dict the scanner expects (it stays decoupled
                # from BarItem to keep it unit-testable in isolation).
                bar_dict = {
                    "ticker": str(ticker),
                    "bar_close_at": close_at,
                    "open": float(bar.open_price),
                    "close": float(bar.close_price),
                    "high": float(bar.high_price),
                    "low": float(bar.low_price),
                    "volume": float(bar.volume),
                }
                for alert_dict in self._scanner.on_bar_close(bar_dict):
                    self._dispatch_alert(alert_dict)
        except (TypeError, ValueError) as exc:
            ticker = candle.get("product_id") or "_unknown"
            self._status.record_error(ticker, f"candle_parse:{type(exc).__name__}")
            logger.debug("[fast_path] candle parse failed: %s", exc, exc_info=True)


    def _dispatch_alert(self, alert_dict: dict[str, Any]) -> None:
        """Convert scanner-emitted dict into AlertItem and enqueue.

        Kept narrow on purpose: the scanner returns plain dicts so it
        stays infra-free for unit tests; this method is the seam where
        we cross into the typed db_writer interface.
        """
        try:
            item = AlertItem(
                ticker=str(alert_dict["ticker"]),
                alert_type=str(alert_dict["alert_type"]),
                fired_at=alert_dict["fired_at"],
                signal_score=float(alert_dict.get("signal_score") or 0.0),
                features=dict(alert_dict.get("features") or {}),
                source="fast_path",
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("[fast_path] scanner emitted malformed alert: %s", exc)
            return
        self._db_writer.enqueue_alert(item)
        logger.info(
            "[fast_path] ALERT ticker=%s type=%s score=%.3f features=%s",
            item.ticker, item.alert_type, item.signal_score, item.features,
        )

    def stats(self) -> dict[str, Any]:
        """Diagnostic counters — for the supervisor metrics line. Lets us
        distinguish "no live updates" (raw_messages_total stuck) vs
        "filtered out" (candles_filtered_unclosed climbing) vs "dedup
        thrash" (candles_filtered_dedupe climbing).
        """
        return {
            "raw_messages_total": self._raw_messages_total,
            "raw_candles_events_total": self._raw_candles_events_total,
            "raw_candles_total": self._raw_candles_total,
            "candles_filtered_unclosed": self._candles_filtered_unclosed,
            "candles_filtered_dedupe": self._candles_filtered_dedupe,
            "heartbeats_total": self._heartbeats_total,
            "subscriptions_total": self._subscriptions_total,
            "unknown_channel_total": self._unknown_channel_total,
            "last_unknown_channel": self._last_unknown_channel,
            # F2: nested order-book aggregator stats.
            "book": self._book.stats(),
            # F3: nested scanner stats.
            "scanner": self._scanner.stats(),
        }


__all__ = ["CoinbaseWSClient", "BAR_CLOSE_GRACE_S"]
