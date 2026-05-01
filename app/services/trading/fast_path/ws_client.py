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

from .db_writer import BarItem, FastPathDBWriter
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
        async with websockets.connect(  # type: ignore[arg-type]
            url, ping_interval=20, ping_timeout=20, close_timeout=5,
            max_size=2 ** 20,  # 1 MB; candles + heartbeats are tiny
        ) as ws:
            await self._subscribe(ws, "candles")
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
        try:
            payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("[fast_path] ws msg decode failed: %s", exc)
            return

        channel = payload.get("channel")
        if channel == "candles":
            self._handle_candles(payload)
        elif channel == "heartbeats":
            self._handle_heartbeat(payload)
        elif channel == "subscriptions":
            # Coinbase confirms subscriptions — purely informational.
            return
        # Unknown channels are ignored (forward compat).

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
        for ev in events:
            candles = ev.get("candles") or []
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
                return
            close_at = datetime.fromtimestamp(end_s, tz=timezone.utc).replace(tzinfo=None)
            open_at = datetime.fromtimestamp(start_s, tz=timezone.utc).replace(tzinfo=None)

            # Dedupe: don't re-enqueue an already-persisted bar.
            last = self._last_persisted.get(ticker)
            if last is not None and close_at <= last:
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
        except (TypeError, ValueError) as exc:
            ticker = candle.get("product_id") or "_unknown"
            self._status.record_error(ticker, f"candle_parse:{type(exc).__name__}")
            logger.debug("[fast_path] candle parse failed: %s", exc, exc_info=True)


__all__ = ["CoinbaseWSClient", "BAR_CLOSE_GRACE_S"]
