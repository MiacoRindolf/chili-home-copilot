"""Event-driven LIVE runner loop — Stage 2 of the websocket rail (exits first).

The 15s APScheduler batch remains the heartbeat/safety net; this loop adds
REAL-TIME reaction: on every price-bus tick for a symbol with an open LIVE
position, if the bid breaches the tracked stop (or reaches the target zone),
``tick_live_session`` fires immediately instead of waiting for the next batch.
Sessions resting in ``live_pending_entry`` also get fast ticks so the 10s
entry ack-timeout resolves at tick speed.

Safety model (why this is shippable while entries stay scheduled):
  * The breach predicate is only a DISPATCH HINT — the full runner logic
    (trail math, partial exits, broker calls) runs inside tick_live_session,
    so there is no duplicated exit math to drift.
  * tick_live_session is re-entrancy-safe (SELECT ... FOR UPDATE NOWAIT —
    overlapping ticks return ``concurrent_tick`` no-ops), so event ticks and
    the scheduled batch coexist.
  * Broker work never runs on the websocket receive thread: breaches are
    dispatched to a small bounded pool with per-session min-spacing; if the
    pool is saturated the event is DROPPED (the heartbeat batch covers it).

Scope: EXITS + pending-entry resolution only. Event-driven ENTRIES (candle
close) are Stage 3, after Replay Lab parity validation.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ....config import settings
from ....db import SessionLocal
from ....models.trading import TradingAutomationSession
from .live_fsm import (
    LIVE_RUNNER_RUNNABLE_STATES,
    STATE_LIVE_BAILOUT,
    STATE_LIVE_ENTERED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_LIVE_SCALING_OUT,
    STATE_LIVE_TRAILING,
    STATE_WATCHING_LIVE,
)

_log = logging.getLogger(__name__)

_KEY_LIVE_EXEC = "momentum_live_execution"
_POSITION_STATES = frozenset(
    {STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT}
)
# Per-session minimum spacing between EVENT ticks. The scheduled batch already
# runs every 15s; events exist to catch the breach moment, not to stream.
_EVENT_TICK_MIN_SPACING_S = 2.0
_TRACKER_REFRESH_S = 10.0


class _LiveSessionTracker:
    """Thread-safe registry of runnable LIVE sessions + their exit thresholds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, dict] = {}

    def refresh(self) -> None:
        db = SessionLocal()
        try:
            rows = (
                db.query(TradingAutomationSession)
                .filter(
                    TradingAutomationSession.mode == "live",
                    TradingAutomationSession.state.in_(LIVE_RUNNER_RUNNABLE_STATES),
                )
                .all()
            )
            new_map: dict[int, dict] = {}
            for sess in rows:
                snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
                le = snap.get(_KEY_LIVE_EXEC) if isinstance(snap.get(_KEY_LIVE_EXEC), dict) else {}
                pos = le.get("position") if isinstance(le.get("position"), dict) else None
                entry: dict = {
                    "session_id": int(sess.id),
                    "symbol": str(sess.symbol or "").upper(),
                    "state": sess.state,
                }
                if pos and sess.state in _POSITION_STATES:
                    try:
                        entry["stop_px"] = float(pos.get("stop_price") or 0)
                        entry["target_px"] = float(pos.get("target_price") or 0)
                    except (TypeError, ValueError):
                        pass
                if sess.state == STATE_WATCHING_LIVE:
                    try:
                        wl = le.get("watch_break_level")
                        if wl:
                            entry["watch_break_level"] = float(wl)
                    except (TypeError, ValueError):
                        pass
                new_map[int(sess.id)] = entry
            with self._lock:
                self._sessions = new_map
        except Exception as e:
            _log.warning("[live_loop] session refresh failed: %s", e)
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

    def get_sessions_for_symbol(self, symbol: str) -> list[dict]:
        sym = symbol.upper()
        with self._lock:
            return [s for s in self._sessions.values() if s.get("symbol") == sym]

    def get_all_symbols(self) -> set[str]:
        with self._lock:
            return {s["symbol"] for s in self._sessions.values() if s.get("symbol")}

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


class LiveRunnerLoop:
    """Bridges price-bus ticks to ``tick_live_session`` for exit-speed reaction."""

    def __init__(self) -> None:
        self._tracker = _LiveSessionTracker()
        self._running = False
        self._refresher: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._subscribed: set[str] = set()
        self._last_event_tick: dict[int, float] = {}
        self._inflight: set[int] = set()
        self._inflight_lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="live-loop-tick")
        self._tracker.refresh()
        self._subscribe_active_symbols()
        self._refresher = threading.Thread(
            target=self._refresh_loop, daemon=True, name="live-runner-loop-refresh"
        )
        self._refresher.start()
        _log.info(
            "[live_loop] started — event-driven exits armed (%d live sessions tracked)",
            self._tracker.count(),
        )

    def stop(self) -> None:
        self._running = False
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
        _log.info("[live_loop] stopped")

    def _subscribe_active_symbols(self) -> None:
        try:
            from ..price_bus import get_price_bus

            bus = get_price_bus()
        except Exception:
            return
        current = self._tracker.get_all_symbols()
        for sym in current - self._subscribed:
            bus.subscribe_symbol(sym)
            bus.register_tick_listener(sym, self._on_tick)
        self._subscribed |= current

    def _refresh_loop(self) -> None:
        while self._running:
            time.sleep(_TRACKER_REFRESH_S)
            if not self._running:
                break
            try:
                self._tracker.refresh()
                self._subscribe_active_symbols()
            except Exception:
                pass

    # ── tick handler (runs on the WS receive thread — keep it cheap) ─────────

    def _on_tick(self, symbol: str, quote) -> None:
        if not self._running:
            return
        sessions = self._tracker.get_sessions_for_symbol(symbol)
        if not sessions:
            return
        bid = getattr(quote, "bid", None)
        mid = getattr(quote, "mid", None) or getattr(quote, "price", None)
        exit_ref = bid if (bid and bid > 0) else (mid if (mid and mid > 0) else None)
        if exit_ref is None:
            return
        for s in sessions:
            state = s.get("state")
            if state in _POSITION_STATES:
                stop_px = s.get("stop_px") or 0.0
                target_px = s.get("target_px") or 0.0
                # dispatch hint only — the runner re-checks everything itself
                if (stop_px > 0 and exit_ref <= stop_px) or (
                    target_px > 0 and exit_ref >= target_px * 0.995
                ):
                    self._dispatch(s["session_id"])
            elif state == STATE_LIVE_PENDING_ENTRY:
                # resolve fills / the 10s ack-timeout at tick speed
                self._dispatch(s["session_id"])
            elif state == STATE_WATCHING_LIVE:
                # Ross-speed ENTRY: the runner stashed the level it is waiting to
                # break (watch_break_level); the instant a tick trades through it,
                # re-evaluate NOW — the tick-break trigger fires within seconds
                # instead of a bar-close + batch-cadence later. The full trigger
                # still decides; this is only the dispatch hint.
                wl = s.get("watch_break_level") or 0.0
                ref = mid if (mid and mid > 0) else bid
                if wl > 0 and ref and ref > wl:
                    self._dispatch(s["session_id"])

    def _dispatch(self, session_id: int) -> None:
        now = time.monotonic()
        last = self._last_event_tick.get(session_id, 0.0)
        if now - last < _EVENT_TICK_MIN_SPACING_S:
            return
        with self._inflight_lock:
            if session_id in self._inflight:
                return
            self._inflight.add(session_id)
        self._last_event_tick[session_id] = now
        pool = self._pool
        if pool is None:
            with self._inflight_lock:
                self._inflight.discard(session_id)
            return
        try:
            pool.submit(self._tick_session, session_id)
        except Exception:
            with self._inflight_lock:
                self._inflight.discard(session_id)

    def _tick_session(self, session_id: int) -> None:
        db = SessionLocal()
        try:
            from .live_runner import tick_live_session

            tick_live_session(db, session_id)
            db.commit()
        except Exception as e:
            _log.debug("[live_loop] event tick session %d failed: %s", session_id, e)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            with self._inflight_lock:
                self._inflight.discard(session_id)
            try:
                db.rollback()
            except Exception:
                pass
            db.close()


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_loop: LiveRunnerLoop | None = None
_loop_lock = threading.Lock()


def get_live_runner_loop() -> LiveRunnerLoop:
    global _loop
    if _loop is None:
        with _loop_lock:
            if _loop is None:
                _loop = LiveRunnerLoop()
    return _loop


def start_live_runner_loop() -> None:
    """Start the event-driven live loop when the price bus + live runner are on."""
    if not settings.chili_autopilot_price_bus_enabled:
        return
    if not getattr(settings, "chili_momentum_live_runner_enabled", False):
        return
    get_live_runner_loop().start()


def stop_live_runner_loop() -> None:
    if _loop is not None:
        _loop.stop()
