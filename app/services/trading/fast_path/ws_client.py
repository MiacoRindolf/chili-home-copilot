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
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.engine import Engine

try:
    import websockets
except ImportError:  # pragma: no cover - dependency added via requirements
    websockets = None  # type: ignore

from .db_writer import AlertItem, BarItem, BookItem, FastPathDBWriter
from .gates import ALERT_RECENCY_MAX_AGE_S
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
BPS_PER_UNIT = 10000.0


class CoinbaseWSClient:
    def __init__(
        self,
        settings: FastPathSettings,
        db_writer: FastPathDBWriter,
        status: StatusTracker,
        engine: Engine | None = None,
    ) -> None:
        self._settings = settings
        self._db_writer = db_writer
        self._status = status
        self._engine = engine
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
        # 2026-05-17: pass emit_short_alerts so the scanner skips
        # imbalance_short on long-only venues (Coinbase spot). Default
        # is False at the settings layer; operators on a perp venue
        # flip CHILI_FAST_PATH_EMIT_SHORT_ALERTS=true.
        self._scanner = MomentumScanner(
            emit_short_alerts=getattr(settings, "emit_short_alerts", False),
            vol_breakout_lookback=getattr(
                settings, "scanner_vol_breakout_lookback", 20,
            ),
            vol_breakout_mult=getattr(
                settings, "scanner_vol_breakout_mult", 2.0,
            ),
            imbalance_long_threshold=getattr(
                settings, "scanner_imbalance_long_threshold", 0.65,
            ),
            imbalance_short_threshold=getattr(
                settings, "scanner_imbalance_short_threshold", 0.35,
            ),
            imbalance_cooldown_s=getattr(
                settings, "scanner_imbalance_cooldown_s", 30.0,
            ),
            spread_squeeze_bps=getattr(
                settings, "scanner_spread_squeeze_bps", 1.5,
            ),
            spread_squeeze_vol_mult=getattr(
                settings, "scanner_spread_squeeze_vol_mult", 1.2,
            ),
            spread_squeeze_cooldown_s=getattr(
                settings, "scanner_spread_squeeze_cooldown_s", 60.0,
            ),
            book_pressure_enabled=getattr(
                settings, "scanner_book_pressure_enabled", True,
            ),
            book_pressure_window=getattr(
                settings, "scanner_book_pressure_window", 5,
            ),
            book_pressure_min_avg_imbalance=getattr(
                settings, "scanner_book_pressure_min_avg_imbalance", 0.65,
            ),
            book_pressure_min_microprice_bps=getattr(
                settings, "scanner_book_pressure_min_microprice_bps", 0.25,
            ),
            book_pressure_max_spread_bps=getattr(
                settings, "scanner_book_pressure_max_spread_bps", 3.0,
            ),
            book_pressure_min_mid_move_bps=getattr(
                settings, "scanner_book_pressure_min_mid_move_bps", 0.25,
            ),
            book_pressure_cooldown_s=getattr(
                settings, "scanner_book_pressure_cooldown_s", 30.0,
            ),
            book_pressure_min_touch_notional_usd=getattr(
                settings,
                "scanner_book_pressure_min_touch_notional_usd",
                25.0,
            ),
        )
        # Diagnostic counters — surfaced via stats() so the supervisor
        # metrics line shows whether we're seeing raw traffic at all
        # (vs only filtering it out as not-yet-closed).
        self._raw_messages_total: int = 0
        self._raw_candles_events_total: int = 0
        self._raw_candles_total: int = 0
        self._candles_filtered_unclosed: int = 0
        self._candles_filtered_dedupe: int = 0
        self._candles_scanned_warmup_only: int = 0
        self._alerts_suppressed_negative_edge: int = 0
        self._alerts_suppressed_cost_barrier: int = 0
        self._alerts_suppressed_maker_attempt_adverse: int = 0
        self._heartbeats_total: int = 0
        self._subscriptions_total: int = 0
        self._unknown_channel_total: int = 0
        self._last_unknown_channel: str | None = None
        self._negative_edge_cache: dict[tuple[str, str, str, str], tuple[bool, float]] = {}
        self._cost_barrier_cache: dict[
            tuple[str, str, str, str, float, float], tuple[bool, float]
        ] = {}
        self._maker_attempt_adverse_cache: dict[
            tuple[str, str, str, int], tuple[bool, float]
        ] = {}
        # f-fastpath-universe-rotation (2026-05-07): cached active-pair
        # set. Resolved at start() and refreshed on each reconnect via
        # ``_resolve_active_pairs``. When ``universe_rotation_enabled``
        # is False (default), this is identical to ``settings.pairs``.
        # When True, it is read from ``fast_path_universe``; an empty
        # universe pauses subscription unless the operator explicitly
        # enables the legacy fallback flag.
        self._active_pairs: list[str] = list(settings.pairs)

    def _resolve_active_pairs(self) -> list[str]:
        """Return the current subscription set.

        - If ``universe_rotation_enabled`` is False -> ``settings.pairs``.
        - Else -> ``fast_path_universe.status IN ('active','shadow')``
          from the most-recent rotation. If the table is empty, return
          an empty set by default so the data worker exposes the rotator
          failure instead of quietly trading stale configured pairs.
        """
        if not getattr(self._settings, "universe_rotation_enabled", False):
            return list(self._settings.pairs)
        try:
            from ....db import SessionLocal
            from .universe_rotator import get_subscribed_pairs

            db = SessionLocal()
            try:
                tickers = get_subscribed_pairs(db)
            finally:
                db.close()
            if tickers:
                return tickers
            if getattr(self._settings, "universe_empty_fallback_enabled", False):
                logger.warning(
                    "[fast_path] universe rotation returned no pairs; "
                    "using configured pairs because empty fallback is enabled"
                )
                return list(self._settings.pairs)
            logger.warning(
                "[fast_path] universe rotation returned no pairs; "
                "WS subscription paused until rotator selects a universe"
            )
            return []
        except Exception as exc:
            if getattr(self._settings, "universe_empty_fallback_enabled", False):
                logger.warning(
                    "[fast_path] universe-rotation read failed; "
                    "using configured pairs because empty fallback is enabled: %s",
                    exc,
                )
                return list(self._settings.pairs)
            logger.warning(
                "[fast_path] universe-rotation read failed; "
                "WS subscription paused until rotator state is readable: %s",
                exc,
            )
            return []

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
        # f-fastpath-universe-rotation (2026-05-07): resolve the active
        # subscription set at start() time so the WS lifecycle uses the
        # rotator's selection rather than the configured pair fallback
        # when the rotation flag is on.
        self._active_pairs = self._resolve_active_pairs()
        if self._active_pairs:
            for ticker in self._active_pairs:
                self._status.register(ticker)
        else:
            self._pause_configured_pairs("universe_rotation_empty")
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
            if not self._active_pairs:
                self._pause_configured_pairs("universe_rotation_empty")
                logger.warning(
                    "[fast_path] ws subscription paused: no active/shadow "
                    "universe pairs (retry in %.1fs)",
                    backoff,
                )
            else:
                try:
                    await self._connect_and_consume()
                    # Clean exit (server closed cleanly) — reset backoff
                    backoff = self._settings.reconnect_min_s
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Per-connection error — log + back off + retry.
                    for ticker in self._active_pairs:
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
            # f-fastpath-universe-rotation: refresh the active set on
            # reconnect so the universe rotator's hourly updates land
            # without a fast-data-worker restart.
            self._active_pairs = self._resolve_active_pairs()
            for ticker in self._active_pairs:
                self._status.record_reconnect(ticker)

    async def _connect_and_consume(self) -> None:
        if not self._active_pairs:
            return
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
            for ticker in self._active_pairs:
                self._status.mark_streaming(ticker)
            async for raw in ws:
                if self._stop.is_set():
                    break
                self._handle_message(raw)

    async def _subscribe(self, ws, channel: str) -> None:
        if not self._active_pairs:
            logger.warning(
                "[fast_path] skipped %s subscription because active pair set is empty",
                channel,
            )
            return
        msg = {
            "type": "subscribe",
            "product_ids": list(self._active_pairs),
            "channel": channel,
        }
        await ws.send(json.dumps(msg))

    def _pause_configured_pairs(self, reason: str) -> None:
        for ticker in self._settings.pairs:
            self._status.mark_paused(ticker, reason)

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
                emit_alerts = self._bar_fresh_enough_for_alerts(
                    bar_close_ts=end_s,
                    now_ts=now_ts,
                )
                if not emit_alerts:
                    self._candles_scanned_warmup_only += 1
                for alert_dict in self._scanner.on_bar_close(
                    bar_dict,
                    emit_alerts=emit_alerts,
                ):
                    self._dispatch_alert(alert_dict)
        except (TypeError, ValueError) as exc:
            ticker = candle.get("product_id") or "_unknown"
            self._status.record_error(ticker, f"candle_parse:{type(exc).__name__}")
            logger.debug("[fast_path] candle parse failed: %s", exc, exc_info=True)

    @staticmethod
    def _bar_fresh_enough_for_alerts(*, bar_close_ts: float, now_ts: float) -> bool:
        return (now_ts - bar_close_ts) <= ALERT_RECENCY_MAX_AGE_S

    def _dispatch_alert(self, alert_dict: dict[str, Any]) -> None:
        """Convert scanner-emitted dict into AlertItem and enqueue.

        Kept narrow on purpose: the scanner returns plain dicts so it
        stays infra-free for unit tests; this method is the seam where
        we cross into the typed db_writer interface.
        """
        if self._negative_edge_suppressed(alert_dict):
            return
        if self._cost_barrier_suppressed(alert_dict):
            return
        if self._maker_attempt_adverse_suppressed(alert_dict):
            return
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

    def _negative_edge_suppressed(self, alert_dict: dict[str, Any]) -> bool:
        """Suppress alerts the learned negative-edge gate would reject.

        Executor gates remain authoritative. This is an upstream write-
        saver for mature bad buckets, using the same calibration helper
        and cache TTL from settings.
        """
        if self._engine is None:
            return False
        try:
            from .calibration import (
                decay_table_for_execution_mode,
                is_negative_edge_excluded,
            )
            from .decay_miner import score_bucket

            ticker = str(alert_dict.get("ticker") or "")
            alert_type = str(alert_dict.get("alert_type") or "")
            signal_score = float(alert_dict.get("signal_score") or 0.0)
            bucket = score_bucket(signal_score)
            decay_table = decay_table_for_execution_mode(
                getattr(self._settings, "execution_mode", "taker"),
            )
            key = (ticker, alert_type, bucket, decay_table)
            now = time.monotonic()
            cached = self._negative_edge_cache.get(key)
            if cached is not None:
                suppressed, expires_at = cached
                if now < expires_at:
                    if suppressed:
                        self._alerts_suppressed_negative_edge += 1
                    return suppressed
            excluded, _evidence = is_negative_edge_excluded(
                self._engine,
                ticker=ticker,
                alert_type=alert_type,
                signal_score=signal_score,
                table=decay_table,
                allow_pooled=True,
            )
            ttl_s = max(0.0, float(
                getattr(self._settings, "negative_edge_filter_ttl_s", 30) or 0,
            ))
            self._negative_edge_cache[key] = (bool(excluded), now + ttl_s)
            if excluded:
                self._alerts_suppressed_negative_edge += 1
                logger.debug(
                    "[fast_path] alert suppressed by learned negative edge "
                    "ticker=%s type=%s bucket=%s table=%s",
                    ticker, alert_type, bucket, decay_table,
                )
                return True
        except Exception as exc:
            logger.debug(
                "[fast_path] negative-edge alert prefilter skipped: %s",
                exc,
                exc_info=True,
            )
        return False

    @staticmethod
    def _alert_spread_bps(alert_dict: dict[str, Any]) -> float:
        features = alert_dict.get("features")
        if not isinstance(features, dict):
            return 0.0
        try:
            spread_bps = float(features.get("spread_bps") or 0.0)
        except (TypeError, ValueError):
            spread_bps = 0.0
        if spread_bps > 0.0:
            return spread_bps
        try:
            best_bid = float(features.get("best_bid") or 0.0)
            best_ask = float(features.get("best_ask") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if best_bid <= 0.0 or best_ask <= best_bid:
            return 0.0
        mid = (best_bid + best_ask) / 2.0
        return ((best_ask - best_bid) / mid) * BPS_PER_UNIT

    def _cost_barrier_suppressed(self, alert_dict: dict[str, Any]) -> bool:
        """Suppress alerts whose lane cannot clear live round-trip cost."""
        if self._engine is None:
            return False
        if not getattr(self._settings, "cost_aware_admission_enabled", False):
            return False
        try:
            from .calibration import (
                decay_table_for_execution_mode,
                is_cost_barrier_excluded,
            )
            from .decay_miner import score_bucket
            from .fees import fee_bps_for_execution_mode

            ticker = str(alert_dict.get("ticker") or "")
            alert_type = str(alert_dict.get("alert_type") or "")
            signal_score = float(alert_dict.get("signal_score") or 0.0)
            bucket = score_bucket(signal_score)
            exec_mode = str(
                getattr(self._settings, "execution_mode", "taker") or "taker"
            ).strip().lower()
            decay_table = decay_table_for_execution_mode(exec_mode)
            fee_bps, _fee_detail = fee_bps_for_execution_mode(
                self._settings,
                exec_mode,
            )
            spread_bps = self._alert_spread_bps(alert_dict)
            cost_bps = 2.0 * (float(fee_bps) + float(spread_bps))
            min_net_bps = float(
                getattr(self._settings, "live_alpha_min_net_bps", 0.0) or 0.0
            )
            key = (
                ticker,
                alert_type,
                bucket,
                decay_table,
                round(cost_bps, 4),
                round(min_net_bps, 4),
            )
            now = time.monotonic()
            cached = self._cost_barrier_cache.get(key)
            if cached is not None:
                suppressed, expires_at = cached
                if now < expires_at:
                    if suppressed:
                        self._alerts_suppressed_cost_barrier += 1
                    return suppressed
            excluded, evidence = is_cost_barrier_excluded(
                self._engine,
                ticker=ticker,
                alert_type=alert_type,
                signal_score=signal_score,
                cost_bps=cost_bps,
                min_net_bps=min_net_bps,
                table=decay_table,
                allow_pooled=True,
            )
            ttl_s = max(0.0, float(
                getattr(self._settings, "negative_edge_filter_ttl_s", 30) or 0,
            ))
            self._cost_barrier_cache[key] = (bool(excluded), now + ttl_s)
            if excluded:
                self._alerts_suppressed_cost_barrier += 1
                logger.debug(
                    "[fast_path] alert suppressed by cost-barrier evidence "
                    "ticker=%s type=%s bucket=%s table=%s verdict=%s "
                    "cost_bps=%.4f",
                    ticker,
                    alert_type,
                    bucket,
                    decay_table,
                    (evidence or {}).get("verdict"),
                    cost_bps,
                )
                return True
        except Exception as exc:
            logger.debug(
                "[fast_path] cost-barrier alert prefilter skipped: %s",
                exc,
                exc_info=True,
            )
        return False

    def _maker_attempt_adverse_suppressed(self, alert_dict: dict[str, Any]) -> bool:
        """Suppress alerts whose maker attempts prove adverse selection.

        Executor gates remain authoritative. This mirrors
        ``gate_maker_attempt_adverse_selection`` upstream so mature
        passive-execution failures do not keep burning alert writes.
        """
        if self._engine is None:
            return False
        exec_mode = str(
            getattr(self._settings, "execution_mode", "taker") or "taker"
        ).strip().lower()
        if exec_mode not in ("maker_only", "maker_first_then_taker"):
            return False
        if not getattr(self._settings, "maker_attempt_adverse_filter_enabled", True):
            return False
        try:
            from .calibration import maker_attempt_adverse_selection_excluded
            from .decay_miner import score_bucket

            ticker = str(alert_dict.get("ticker") or "")
            alert_type = str(alert_dict.get("alert_type") or "")
            signal_score = float(alert_dict.get("signal_score") or 0.0)
            bucket = score_bucket(signal_score)
            window_hours = max(1, int(
                getattr(
                    self._settings,
                    "maker_attempt_adverse_filter_window_h",
                    24,
                ) or 24,
            ))
            key = (ticker, alert_type, bucket, window_hours)
            now = time.monotonic()
            cached = self._maker_attempt_adverse_cache.get(key)
            if cached is not None:
                suppressed, expires_at = cached
                if now < expires_at:
                    if suppressed:
                        self._alerts_suppressed_maker_attempt_adverse += 1
                    return suppressed
            excluded, evidence = maker_attempt_adverse_selection_excluded(
                self._engine,
                ticker=ticker,
                alert_type=alert_type,
                signal_score=signal_score,
                window_hours=window_hours,
            )
            ttl_s = max(0.0, float(
                getattr(self._settings, "negative_edge_filter_ttl_s", 30) or 0,
            ))
            self._maker_attempt_adverse_cache[key] = (bool(excluded), now + ttl_s)
            if excluded:
                self._alerts_suppressed_maker_attempt_adverse += 1
                logger.debug(
                    "[fast_path] alert suppressed by maker-attempt "
                    "adverse-selection evidence ticker=%s type=%s "
                    "bucket=%s window_h=%s reasons=%s",
                    ticker,
                    alert_type,
                    bucket,
                    window_hours,
                    (evidence or {}).get("blocked_reasons"),
                )
                return True
        except Exception as exc:
            logger.debug(
                "[fast_path] maker-attempt alert prefilter skipped: %s",
                exc,
                exc_info=True,
            )
        return False

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
            "candles_scanned_warmup_only": self._candles_scanned_warmup_only,
            "alerts_suppressed_negative_edge": self._alerts_suppressed_negative_edge,
            "alerts_suppressed_cost_barrier": self._alerts_suppressed_cost_barrier,
            "alerts_suppressed_maker_attempt_adverse":
                self._alerts_suppressed_maker_attempt_adverse,
            "negative_edge_cache_size": len(self._negative_edge_cache),
            "cost_barrier_cache_size": len(self._cost_barrier_cache),
            "maker_attempt_adverse_cache_size": len(
                self._maker_attempt_adverse_cache
            ),
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
