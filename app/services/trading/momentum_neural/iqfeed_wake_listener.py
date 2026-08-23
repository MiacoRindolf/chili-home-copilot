"""Batch-mode pg-LISTEN wake consumer — tick-speed exits on a QUIET bus.

THE GAP THIS CLOSES (2026-08-23). The session tick-cross wake shipped in #1109
rides the Massive price bus: a stop/target/watch-break crossing is only noticed
when a bus tick arrives for that symbol. When the bus is quiet — thin premarket
names, a symbol the bus never subscribed, a bus outage — there is no tick, so
there is no wake, and the crossing waits for the 10s scheduler batch (measured
at 10-30s per pass). Premarket is exactly where that hurts: it is the lane's
most profitable session and its thinnest tape.

The HOST IQFeed bridge has been pg_notify'ing every L1 quote on
``momentum_iqfeed_l1`` for months — it is the lane's EARLIEST tick authority,
and in premarket it is often the ONLY one. The live event loop consumes that
channel, but the loop does not run in the deployed batch window, so nothing
consumed it there. This module is the batch-mode consumer: it LISTENs, and
routes each quote through the SAME crossing check and the SAME wake helper the
bus path already uses.

DISPATCH HINT ONLY — identical contract to every other waker. The payload is
read as a hint (symbol/bid/ask); this module never treats it as authoritative
market data, and the FSM re-reads its own quote and decides. The per-session
2s spacing and single-flight in ``_spawn_session_wake`` bound a notify storm,
and the scheduler batch remains the safety net if this listener dies.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from types import SimpleNamespace
from typing import Any

from ....config import settings

_log = logging.getLogger(__name__)

_DEFAULT_CHANNEL = "momentum_iqfeed_l1"
# Same refusal pattern the loop consumer uses: the channel is interpolated into
# a LISTEN statement, which cannot be parameterised, so it is validated first.
_CHANNEL_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SELECT_TIMEOUT_S = 1.0
_RECONNECT_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)


def _duck_quote(payload: dict) -> Any | None:
    """Hint-grade quote from one notify payload (bid/ask may be null).

    Only a bid supports an exit-side crossing check and only a real mid
    supports the entry-side one, so an ask-only payload yields nothing rather
    than a fabricated price.
    """

    def _pos(value: Any) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return 0.0
        return out if out > 0 else 0.0

    bid = _pos(payload.get("bid"))
    ask = _pos(payload.get("ask"))
    if bid <= 0 and ask <= 0:
        return None
    mid = ((bid + ask) / 2.0) if (bid > 0 and ask > 0) else 0.0
    return SimpleNamespace(bid=bid or None, ask=ask or None, mid=mid or None, last=None)


class IqfeedWakeListener:
    """Daemon LISTEN loop that wakes crossing sessions on IQFeed quotes."""

    def __init__(self) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_notify_at: float = 0.0
        self._notifies = 0
        self._wakes = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        if not bool(
            getattr(settings, "chili_momentum_iqfeed_wake_listener_enabled", True)
        ):
            _log.info("[iqfeed_wake] disabled — no-op")
            return
        channel = self._channel()
        if channel is None:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._listen_loop, args=(channel,), daemon=True,
            name="iqfeed-wake-listen",
        )
        self._thread.start()
        _log.info("[iqfeed_wake] started — LISTEN %s", channel)

    def stop(self) -> None:
        self._running = False

    def health(self) -> dict:
        return {
            "running": self._running,
            "last_notify_age_s": (
                round(time.monotonic() - self._last_notify_at, 1)
                if self._last_notify_at
                else None
            ),
            "notifies": self._notifies,
            "wakes": self._wakes,
        }

    # ── internals ────────────────────────────────────────────────────────

    def _channel(self) -> str | None:
        channel = str(
            getattr(
                settings,
                "chili_momentum_live_runner_loop_iqfeed_notify_channel",
                _DEFAULT_CHANNEL,
            )
            or ""
        ).strip()
        if _CHANNEL_RE.fullmatch(channel) is None:
            _log.critical(
                "[iqfeed_wake] refusing LISTEN with invalid channel=%r", channel
            )
            return None
        return channel

    def _listen_loop(self, channel: str) -> None:
        try:
            import select

            import psycopg2
        except Exception as exc:
            _log.warning("[iqfeed_wake] psycopg2/select unavailable: %s", exc)
            self._running = False
            return

        db_url = str(getattr(settings, "database_url", "") or "")
        attempt = 0
        while self._running:
            conn = None
            try:
                conn = psycopg2.connect(db_url)
                conn.set_session(autocommit=True)
                cur = conn.cursor()
                cur.execute(f"LISTEN {channel};")
                attempt = 0
                _log.info("[iqfeed_wake] listening on %s", channel)
                while self._running:
                    ready, _, _ = select.select([conn], [], [], _SELECT_TIMEOUT_S)
                    if not self._running:
                        break
                    if not ready:
                        continue
                    conn.poll()
                    notifications = list(conn.notifies)
                    del conn.notifies[:]
                    if notifications:
                        self._handle_batch(notifications)
            except Exception as exc:
                if not self._running:
                    break
                delay = _RECONNECT_BACKOFF_S[
                    min(attempt, len(_RECONNECT_BACKOFF_S) - 1)
                ]
                attempt += 1
                _log.warning(
                    "[iqfeed_wake] listen connection lost (%s); reconnecting in %.0fs "
                    "— the scheduler batch covers sessions meanwhile",
                    exc, delay,
                )
                time.sleep(delay)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def _handle_batch(self, notifications: list) -> None:
        """Coalesce a storm to the NEWEST payload per symbol, then wake."""
        latest: dict[str, dict] = {}
        for note in notifications:
            try:
                payload = json.loads(getattr(note, "payload", "") or "")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            symbol = str(payload.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            latest[symbol] = payload
        if not latest:
            return
        self._notifies += len(latest)
        self._last_notify_at = time.monotonic()

        from .ignition_loop import _spawn_session_wake, get_ignition_loop

        try:
            tracker = get_ignition_loop()._sessions
        except Exception:
            return
        for symbol, payload in latest.items():
            try:
                quote = _duck_quote(payload)
                if quote is None:
                    continue
                for sid in tracker.crossed(symbol, quote):
                    if _spawn_session_wake(sid):
                        self._wakes += 1
            except Exception:
                _log.debug(
                    "[iqfeed_wake] wake handling failed symbol=%s", symbol, exc_info=True
                )


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_listener: IqfeedWakeListener | None = None
_listener_lock = threading.Lock()


def get_iqfeed_wake_listener() -> IqfeedWakeListener:
    global _listener
    if _listener is None:
        with _listener_lock:
            if _listener is None:
                _listener = IqfeedWakeListener()
    return _listener


def start_iqfeed_wake_listener() -> None:
    get_iqfeed_wake_listener().start()


def stop_iqfeed_wake_listener() -> None:
    if _listener is not None:
        _listener.stop()
