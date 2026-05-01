"""Fast-path supervisor — boots the components, monitors, shuts down.

Owns the asyncio event loop. Wires:
  ws_client → db_writer
  ws_client + db_writer → status_tracker
  ws_client + db_writer + status_tracker → healthz

Periodically (every ``metrics_log_interval_s``) emits one structured
log line per pair AND flushes the status_tracker to ``fast_path_status``.

Graceful shutdown: SIGINT/SIGTERM → set stop event → drain → exit.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from sqlalchemy.engine import Engine

from .db_writer import FastPathDBWriter
from .healthz import HealthzServer
from .settings import FastPathSettings
from .status_tracker import StatusTracker
from .ws_client import CoinbaseWSClient

logger = logging.getLogger(__name__)


class FastPathSupervisor:
    def __init__(self, settings: FastPathSettings, engine: Engine) -> None:
        self._settings = settings
        self._engine = engine
        self._stop = asyncio.Event()
        self._db_writer: FastPathDBWriter | None = None
        self._status: StatusTracker | None = None
        self._ws: CoinbaseWSClient | None = None
        self._healthz: HealthzServer | None = None
        self._metrics_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def run(self) -> None:
        # Build components (cheap; no I/O until start()).
        self._status = StatusTracker(
            self._engine, cb_threshold=self._settings.cb_threshold,
        )
        self._db_writer = FastPathDBWriter(
            self._engine,
            queue_max=self._settings.queue_max,
            batch_size=self._settings.batch_size,
            batch_interval_ms=self._settings.batch_interval_ms,
        )
        self._ws = CoinbaseWSClient(
            self._settings, self._db_writer, self._status,
        )
        self._healthz = HealthzServer(
            port=self._settings.healthz_port,
            snapshot_fn=self._snapshot,
        )

        # Pre-register tracked pairs so /healthz has them even before WS connects.
        for ticker in self._settings.pairs:
            self._status.register(ticker)
        # Mark every pair paused by default; mark_streaming flips them
        # on once WS is connected and bars arrive.
        if not self._settings.enabled:
            for ticker in self._settings.pairs:
                self._status.mark_paused(ticker, "fast_path_disabled")
            self._status.flush(force=True)

        # Start always — healthz first so compose can verify the
        # container is alive before WS connects.
        await self._healthz.start()
        await self._db_writer.start()

        if self._settings.enabled:
            await self._ws.start()
        else:
            logger.warning(
                "[fast_path] CHILI_FAST_PATH_ENABLED=0 — supervisor up "
                "but WS NOT subscribed; all pairs in state=paused"
            )

        # Install signal handlers for graceful shutdown.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                # Windows doesn't support add_signal_handler; KeyboardInterrupt
                # via Ctrl+C is enough for local dev.
                pass

        self._metrics_task = asyncio.create_task(self._metrics_loop(), name="metrics")
        logger.info(
            "[fast_path] supervisor running enabled=%s mode=%s pairs=%s",
            self._settings.enabled, self._settings.mode, self._settings.pairs,
        )
        await self._stop.wait()
        await self._shutdown()

    async def _shutdown(self) -> None:
        logger.info("[fast_path] shutting down")
        if self._metrics_task is not None:
            self._metrics_task.cancel()
        if self._ws is not None:
            await self._ws.stop()
        if self._db_writer is not None:
            await self._db_writer.stop()
        if self._healthz is not None:
            await self._healthz.stop()
        if self._status is not None:
            self._status.flush(force=True)

    # ── Periodic metrics + status flush ───────────────────────────────

    async def _metrics_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._settings.metrics_log_interval_s,
                    )
                    return  # _stop was set
                except asyncio.TimeoutError:
                    pass
                self._emit_metrics()
                if self._status is not None:
                    self._status.flush()
        except asyncio.CancelledError:
            return

    def _emit_metrics(self) -> None:
        if self._status is None or self._db_writer is None:
            return
        snap = self._snapshot()
        writer = snap.get("writer", {})
        ws_stats = snap.get("ws", {})
        # WS-level stats are global, not per-pair — log them once at the
        # top of each metrics tick so we can see whether raw traffic is
        # flowing at all (and where it's being filtered).
        logger.info(
            "[fast_path] ws raw_messages=%s candles_events=%s candles=%s "
            "filtered_unclosed=%s filtered_dedupe=%s heartbeats=%s "
            "subscriptions=%s unknown=%s last_unknown=%s",
            ws_stats.get("raw_messages_total"),
            ws_stats.get("raw_candles_events_total"),
            ws_stats.get("raw_candles_total"),
            ws_stats.get("candles_filtered_unclosed"),
            ws_stats.get("candles_filtered_dedupe"),
            ws_stats.get("heartbeats_total"),
            ws_stats.get("subscriptions_total"),
            ws_stats.get("unknown_channel_total"),
            ws_stats.get("last_unknown_channel"),
        )
        for ticker, ps in (snap.get("status") or {}).get("pairs", {}).items():
            logger.info(
                "[fast_path] pair=%s state=%s last_bar_at=%s "
                "errors_60s=%s reconnects=%s queue_depth=%s/%s "
                "writer_bars_received=%s writer_bars_written=%s "
                "writer_bars_dropped=%s",
                ticker, ps.get("state"), ps.get("last_bar_at"),
                ps.get("error_count_60s"), ps.get("reconnect_count"),
                writer.get("queue_depth"), writer.get("queue_max"),
                writer.get("bars_received"), writer.get("bars_written"),
                writer.get("bars_dropped_queue_full"),
            )

    # ── Snapshot for /healthz ─────────────────────────────────────────

    def _snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._settings.enabled,
            "mode": self._settings.mode,
            "pairs_configured": list(self._settings.pairs),
            "writer": self._db_writer.snapshot() if self._db_writer else {},
            "status": self._status.snapshot() if self._status else {},
            "ws": self._ws.stats() if self._ws else {},
        }


__all__ = ["FastPathSupervisor"]
