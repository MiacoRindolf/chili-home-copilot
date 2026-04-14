"""Event-driven paper runner loop — hybrid tick/candle model.

Replaces the 3-minute APScheduler batch with:
  - Real-time stop-loss/take-profit monitoring on every price tick
  - Entry signal evaluation on 1m candle close
  - 60s heartbeat for governance, cooldowns, and missed events
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ....config import settings
from ....db import SessionLocal
from ....models.trading import TradingAutomationSession
from .paper_fsm import (
    PAPER_RUNNER_RUNNABLE_STATES,
    STATE_BAILOUT,
    STATE_ENTERED,
    STATE_ENTRY_CANDIDATE,
    STATE_PENDING_ENTRY,
    STATE_QUEUED,
    STATE_SCALING_OUT,
    STATE_TRAILING,
    STATE_WATCHING,
)

_log = logging.getLogger(__name__)

_POSITION_STATES = frozenset({STATE_ENTERED, STATE_SCALING_OUT, STATE_TRAILING, STATE_BAILOUT})
_ENTRY_SIGNAL_STATES = frozenset({STATE_WATCHING, STATE_ENTRY_CANDIDATE, STATE_PENDING_ENTRY, STATE_QUEUED})

_KEY_PAPER_EXEC = "momentum_paper_execution"


class _SessionTracker:
    """Thread-safe registry of active paper sessions and their price thresholds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, dict[str, Any]] = {}

    def refresh(self) -> None:
        """Reload active paper sessions from the database."""
        db = SessionLocal()
        try:
            rows = (
                db.query(TradingAutomationSession)
                .filter(
                    TradingAutomationSession.mode == "paper",
                    TradingAutomationSession.state.in_(PAPER_RUNNER_RUNNABLE_STATES),
                )
                .all()
            )
            new_map: dict[int, dict[str, Any]] = {}
            for sess in rows:
                snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
                pe = snap.get(_KEY_PAPER_EXEC) if isinstance(snap.get(_KEY_PAPER_EXEC), dict) else {}
                pos = pe.get("position") if isinstance(pe.get("position"), dict) else None
                entry: dict[str, Any] = {
                    "session_id": int(sess.id),
                    "symbol": sess.symbol,
                    "state": sess.state,
                    "variant_id": int(sess.variant_id),
                }
                if pos and sess.state in _POSITION_STATES:
                    entry["stop_px"] = float(pos.get("stop_price", 0))
                    entry["target_px"] = float(pos.get("target_price", 0))
                    entry["entry_px"] = float(pos.get("entry_price", 0))
                new_map[int(sess.id)] = entry
            with self._lock:
                self._sessions = new_map
        except Exception as e:
            _log.warning("[runner_loop] session refresh failed: %s", e)
        finally:
            db.close()

    def get_sessions_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        sym = symbol.upper()
        with self._lock:
            return [s for s in self._sessions.values() if s.get("symbol") == sym]

    def get_all_symbols(self) -> set[str]:
        with self._lock:
            return {s["symbol"] for s in self._sessions.values()}

    def get_all_session_ids(self) -> list[int]:
        with self._lock:
            return list(self._sessions.keys())


class PaperRunnerLoop:
    """Event-driven paper runner that bridges the price bus to tick_paper_session."""

    def __init__(self) -> None:
        self._tracker = _SessionTracker()
        self._running = False
        self._heartbeat_thread: threading.Thread | None = None
        self._subscribed_symbols: set[str] = set()
        self._last_refresh = 0.0
        self._refresh_interval = 15.0  # re-scan DB for new/removed sessions

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tracker.refresh()
        self._subscribe_active_symbols()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="paper-runner-heartbeat",
        )
        self._heartbeat_thread.start()
        _log.info("[runner_loop] started — %d sessions tracked", len(self._tracker.get_all_session_ids()))

    def stop(self) -> None:
        self._running = False
        _log.info("[runner_loop] stopped")

    def _subscribe_active_symbols(self) -> None:
        """Subscribe to price bus for all active session symbols."""
        try:
            from ..price_bus import get_price_bus
            bus = get_price_bus()
        except Exception:
            return

        current_symbols = self._tracker.get_all_symbols()
        new_symbols = current_symbols - self._subscribed_symbols

        for sym in new_symbols:
            bus.subscribe_symbol(sym)
            bus.register_tick_listener(sym, self._on_tick)
            bus.register_candle_listener(sym, self._on_candle_close)
        self._subscribed_symbols = current_symbols

    def _maybe_refresh(self) -> None:
        now = time.time()
        if now - self._last_refresh >= self._refresh_interval:
            self._tracker.refresh()
            self._subscribe_active_symbols()
            self._last_refresh = now

    # ── tick handler (real-time stop/target exits) ───────────────────

    def _on_tick(self, symbol: str, quote) -> None:
        """Called on every price tick from the price bus. Check exit triggers."""
        if not self._running:
            return
        sessions = self._tracker.get_sessions_for_symbol(symbol)
        if not sessions:
            return

        mid = quote.mid if hasattr(quote, "mid") else 0.0
        bid = quote.bid if hasattr(quote, "bid") else None

        for s in sessions:
            if s.get("state") not in _POSITION_STATES:
                continue
            stop_px = s.get("stop_px", 0)
            target_px = s.get("target_px", 0)
            exit_ref = bid if bid and bid > 0 else mid
            if exit_ref <= 0:
                continue

            triggered = False
            if stop_px > 0 and exit_ref <= stop_px:
                triggered = True
            elif target_px > 0 and exit_ref >= target_px * 0.995:
                triggered = True

            if triggered:
                self._tick_session(s["session_id"], quote)

    # ── candle close handler (entry signal evaluation) ───────────────

    def _on_candle_close(self, symbol: str, candle) -> None:
        """Called on 1m candle close — evaluate entry signals."""
        if not self._running:
            return
        sessions = self._tracker.get_sessions_for_symbol(symbol)
        for s in sessions:
            if s.get("state") in _ENTRY_SIGNAL_STATES:
                self._tick_session(s["session_id"])

    # ── heartbeat (safety net) ───────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        """60s heartbeat: tick all runnable sessions as a safety net."""
        while self._running:
            time.sleep(60.0)
            if not self._running:
                break
            self._maybe_refresh()
            session_ids = self._tracker.get_all_session_ids()
            if not session_ids:
                continue
            _log.debug("[runner_loop] heartbeat: ticking %d sessions", len(session_ids))
            for sid in session_ids:
                if not self._running:
                    break
                self._tick_session(sid)

    # ── tick execution ───────────────────────────────────────────────

    def _tick_session(self, session_id: int, quote=None) -> None:
        """Run tick_paper_session for one session with optional pre-fetched quote."""
        db = SessionLocal()
        try:
            from .paper_runner import tick_paper_session

            quote_fn = None
            if quote is not None:
                mid = quote.mid if hasattr(quote, "mid") else 0.0
                bid = quote.bid if hasattr(quote, "bid") else None
                ask = quote.ask if hasattr(quote, "ask") else None
                source = quote.source if hasattr(quote, "source") else "price_bus"
                if mid > 0:
                    def quote_fn(symbol: str) -> dict[str, Any]:
                        return {"mid": mid, "bid": bid, "ask": ask, "source": source}

            result = tick_paper_session(db, session_id, quote_fn=quote_fn)
            db.commit()

            if result.get("ok") and result.get("state") in ("exited", "error", "cooldown", "finished"):
                self._tracker.refresh()

        except Exception as e:
            _log.debug("[runner_loop] tick session %d failed: %s", session_id, e)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_loop: PaperRunnerLoop | None = None
_loop_lock = threading.Lock()


def get_runner_loop() -> PaperRunnerLoop:
    global _loop
    if _loop is None:
        with _loop_lock:
            if _loop is None:
                _loop = PaperRunnerLoop()
    return _loop


def start_runner_loop() -> None:
    """Start the event-driven paper runner if the price bus is enabled."""
    if not settings.chili_autopilot_price_bus_enabled:
        return
    if not settings.chili_momentum_paper_runner_enabled:
        return
    loop = get_runner_loop()
    loop.start()


def stop_runner_loop() -> None:
    if _loop is not None:
        _loop.stop()
