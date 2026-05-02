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
from .decay_miner import FastPathDecayMiner
from .executor import FastPathExecutor
from .exit_manager import FastPathExitManager
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
        self._executor: FastPathExecutor | None = None
        self._exit_manager: FastPathExitManager | None = None
        self._decay_miner: FastPathDecayMiner | None = None
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

        # F4: executor reads from fast_alerts (written by F3 scanner)
        # and decides paper/live actions. It's only useful when
        # ingestion is enabled — if enabled=False, no alerts will be
        # written, so don't spin it up.
        if self._settings.enabled:
            self._executor = FastPathExecutor(
                self._settings, self._engine, self._ws._book,  # noqa: SLF001
            )
            # F5: exit manager closes the loop on F4 entries —
            # streams top-of-book against per-position bracket
            # (stop_engine-derived stop+target) and writes fast_exits
            # rows with realized P/L. Paper-only in F5; live exit is
            # a follow-up (same three-flag belt as live entry).
            self._exit_manager = FastPathExitManager(
                self._settings, self._engine, self._ws._book,  # noqa: SLF001
            )
            # F6: signal-decay miner. Event-driven brain node that
            # LISTENs on fp_alert_inserted / fp_exit_inserted /
            # fp_book_inserted (NOTIFY triggers in migration 221) and
            # Welford-updates fast_signal_decay (migration 220) so
            # exit_manager / gates can later read calibrated values
            # instead of the current hardcoded magic numbers.
            # Independent of executor: even if F4 is paused, learning
            # from raw fast_alerts continues.
            self._decay_miner = FastPathDecayMiner(
                self._settings, self._engine,
            )

        if self._settings.enabled:
            await self._ws.start()
            if self._executor is not None:
                await self._executor.start()
            if self._exit_manager is not None:
                await self._exit_manager.start()
            if self._decay_miner is not None:
                await self._decay_miner.start()
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
        # Stop in reverse of dependency order: exit manager depends on
        # the executor's entry rows + the WS book; stop it FIRST so any
        # in-flight exit finishes against a still-live book stream.
        # decay_miner is independent of all of these; stop it first so
        # its LISTEN connection is closed before db_writer drains.
        if self._decay_miner is not None:
            await self._decay_miner.stop()
        if self._exit_manager is not None:
            await self._exit_manager.stop()
        if self._executor is not None:
            await self._executor.stop()
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
        book_stats = ws_stats.get("book") or {}
        if book_stats:
            logger.info(
                "[fast_path] book snapshots=%s updates_recv=%s updates_applied=%s "
                "malformed=%s emitted=%s skip_no_snap=%s skip_throttled=%s "
                "skip_empty=%s tickers=%s levels_held=%s "
                "writer_books_received=%s writer_books_written=%s",
                book_stats.get("snapshots_received"),
                book_stats.get("updates_received"),
                book_stats.get("updates_applied"),
                book_stats.get("malformed_updates"),
                book_stats.get("books_emitted"),
                book_stats.get("emissions_skipped_no_snapshot"),
                book_stats.get("emissions_skipped_throttled"),
                book_stats.get("emissions_skipped_empty"),
                book_stats.get("tickers_tracked"),
                book_stats.get("total_levels_held"),
                writer.get("books_received"),
                writer.get("books_written"),
            )
        decay_stats = snap.get("decay_miner") or {}
        if decay_stats:
            obs_per = decay_stats.get("obs_finalized_per_horizon") or {}
            obs_total = sum(int(v or 0) for v in obs_per.values())
            logger.info(
                "[fast_path] decay_miner alerts=%s exits=%s book_ticks=%s "
                "obs_scheduled=%s obs_finalized=%d backfilled=%s "
                "pending_heap=%s validations=%s db_errors=%s last_finalize=%s",
                decay_stats.get("alerts_received"),
                decay_stats.get("exits_received"),
                decay_stats.get("book_ticks_received"),
                decay_stats.get("obs_scheduled"),
                obs_total,
                decay_stats.get("backfilled_rows_written"),
                decay_stats.get("pending_heap_size"),
                decay_stats.get("validations_recorded"),
                decay_stats.get("db_errors"),
                decay_stats.get("last_finalize_at"),
            )
        exit_mgr_stats = snap.get("exit_manager") or {}
        if exit_mgr_stats:
            logger.info(
                "[fast_path] exit_manager polls=%s bootstrap=%s "
                "open=%s stop_hit=%s target_hit=%s time_stop=%s "
                "skipped_no_book=%s db_errors=%s last_decision=%s "
                "max_hold_s=%.0f tickers=%s",
                exit_mgr_stats.get("polls_total"),
                exit_mgr_stats.get("positions_bootstrapped"),
                exit_mgr_stats.get("open_positions_now"),
                exit_mgr_stats.get("decisions_stop_hit"),
                exit_mgr_stats.get("decisions_target_hit"),
                exit_mgr_stats.get("decisions_time_stop"),
                exit_mgr_stats.get("decisions_skipped_no_book"),
                exit_mgr_stats.get("db_errors"),
                exit_mgr_stats.get("last_decision_at"),
                float(exit_mgr_stats.get("max_hold_s") or 0.0),
                exit_mgr_stats.get("tickers_tracked"),
            )
        executor_stats = snap.get("executor") or {}
        if executor_stats:
            logger.info(
                "[fast_path] executor mode=%s live_authorized=%s "
                "polls=%s alerts_seen=%s paper_fill=%s live_placed=%s "
                "rejected=%s db_errors=%s last_alert_id=%s "
                "open_positions=%s tickers_held=%s daily_used_usd=%.2f",
                executor_stats.get("mode"),
                executor_stats.get("live_authorized"),
                executor_stats.get("polls_total"),
                executor_stats.get("alerts_seen"),
                executor_stats.get("decisions_paper_fill"),
                executor_stats.get("decisions_live_placed"),
                executor_stats.get("decisions_rejected"),
                executor_stats.get("db_errors"),
                executor_stats.get("last_alert_id_seen"),
                executor_stats.get("open_positions_total"),
                executor_stats.get("tickers_with_position"),
                float(executor_stats.get("daily_notional_used_usd") or 0.0),
            )
        scanner_stats = ws_stats.get("scanner") or {}
        if scanner_stats:
            logger.info(
                "[fast_path] scanner bars_seen=%s books_seen=%s "
                "vol_breakout=%s vol_pullback=%s pullback_heap=%s pullback_dropped=%s "
                "imb_long=%s imb_short=%s spread_squeeze=%s "
                "suppressed_cooldown=%s suppressed_warmup=%s tickers=%s "
                "writer_alerts_received=%s writer_alerts_written=%s "
                "writer_alerts_dropped=%s",
                scanner_stats.get("bars_seen"),
                scanner_stats.get("books_seen"),
                scanner_stats.get("fired_volume_breakout_long"),
                scanner_stats.get("fired_volume_breakout_pullback_long"),
                scanner_stats.get("pullback_pending_heap"),
                scanner_stats.get("pullback_deferred_dropped_overcap"),
                scanner_stats.get("fired_imbalance_long"),
                scanner_stats.get("fired_imbalance_short"),
                scanner_stats.get("fired_spread_squeeze"),
                scanner_stats.get("suppressed_cooldown"),
                scanner_stats.get("suppressed_warmup"),
                scanner_stats.get("tickers_tracked"),
                writer.get("alerts_received"),
                writer.get("alerts_written"),
                writer.get("alerts_dropped_queue_full"),
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
            "executor": self._executor.stats() if self._executor else {},
            "exit_manager": self._exit_manager.stats() if self._exit_manager else {},
            "decay_miner": self._decay_miner.stats() if self._decay_miner else {},
        }


__all__ = ["FastPathSupervisor"]
