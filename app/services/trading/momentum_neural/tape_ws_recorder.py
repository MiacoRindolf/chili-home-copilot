"""Websocket tape recorder for live momentum symbols.

This is a write-only side path: it densifies ``momentum_nbbo_spread_tape`` with
sub-minute quote rows so replay can see the same tape the live runner saw. A
failure here degrades to the existing snapshot tape; trading logic does not read
from this module in-process.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import text

from ....config import settings
from ....db import SessionLocal
from ....models.trading import TradingAutomationSession
from .live_fsm import LIVE_RUNNER_RUNNABLE_STATES

_log = logging.getLogger(__name__)

_MIN_ROW_SPACING_S = 1.0
_FLUSH_INTERVAL_S = 5.0
_REFRESH_INTERVAL_S = 30.0
_BUFFER_CAP = 5000


class TapeWsRecorder:
    """Buffers WS quote ticks for live-lane symbols into the NBBO tape."""

    def __init__(self) -> None:
        self._running = False
        self._lock = threading.Lock()
        self._buffer: list[dict] = []
        self._symbols: set[str] = set()
        self._listening: set[str] = set()
        self._last_quote: dict[str, tuple[float, float]] = {}
        self._last_row_t: dict[str, float] = {}
        self._vol_base: dict[str, float] = {}
        self._vol_base_at: dict[str, datetime] = {}
        self._vol_ws: dict[str, float] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._refresh_symbols()
        self._thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="tape-ws-recorder"
        )
        self._thread.start()
        _log.info("[tape_ws] recorder started symbols=%d", len(self._symbols))

    def stop(self) -> None:
        self._running = False

    def _refresh_symbols(self) -> None:
        db = SessionLocal()
        try:
            rows = (
                db.query(TradingAutomationSession.symbol)
                .filter(
                    TradingAutomationSession.mode == "live",
                    TradingAutomationSession.state.in_(LIVE_RUNNER_RUNNABLE_STATES),
                )
                .distinct()
                .all()
            )
            symbols = {str(row[0]).upper() for row in rows if row[0]}
            with self._lock:
                self._symbols = symbols
            self._anchor_volume_baselines(db, symbols)
        except Exception as exc:
            _log.debug("[tape_ws] symbol refresh failed: %s", exc)
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
        self._attach_listeners()

    def _anchor_volume_baselines(self, db, symbols: set[str]) -> None:
        for symbol in symbols:
            row = None
            try:
                row = db.execute(
                    text(
                        "SELECT observed_at, day_volume FROM momentum_nbbo_spread_tape "
                        "WHERE symbol = :symbol AND source = 'massive_snapshot' "
                        "AND observed_at >= date_trunc('day', now() at time zone 'utc') "
                        "ORDER BY observed_at DESC LIMIT 1"
                    ),
                    {"symbol": symbol},
                ).fetchone()
            except Exception:
                row = None
            if row is None and symbol.endswith("-USD"):
                try:
                    row = db.execute(
                        text(
                            "SELECT observed_at, day_volume FROM momentum_nbbo_spread_tape "
                            "WHERE symbol = :symbol "
                            "AND observed_at >= date_trunc('day', now() at time zone 'utc') "
                            "ORDER BY observed_at DESC LIMIT 1"
                        ),
                        {"symbol": symbol},
                    ).fetchone()
                except Exception:
                    row = None
            if row is None:
                self._vol_base.setdefault(symbol, 0.0)
                continue
            observed_at, day_volume = row[0], float(row[1] or 0.0)
            previous_at = self._vol_base_at.get(symbol)
            if previous_at is None or observed_at > previous_at:
                self._vol_base[symbol] = day_volume
                self._vol_base_at[symbol] = observed_at
                self._vol_ws[symbol] = 0.0

    def _attach_listeners(self) -> None:
        with self._lock:
            new_symbols = self._symbols - self._listening
        for symbol in new_symbols:
            try:
                if symbol.endswith("-USD"):
                    from ...trading.price_bus import get_price_bus

                    bus = get_price_bus()
                    bus.subscribe_coinbase(symbol)
                    bus.register_tick_listener(
                        symbol, lambda tick, sym=symbol: self._on_bus_tick(sym, tick)
                    )
                else:
                    from ...massive_client import register_tick_listener

                    register_tick_listener(symbol, self._on_tick)
                self._listening.add(symbol)
            except Exception as exc:
                _log.debug("[tape_ws] listener attach failed symbol=%s error=%s", symbol, exc)

    def _on_bus_tick(self, symbol: str, tick) -> None:
        self._on_tick(symbol, tick)

    def _on_tick(self, symbol: str, snap) -> None:
        self._record(symbol, snap, source=None)

    def record_external(self, symbol: str, snap, source: str = "massive_ws_universe") -> None:
        """Persist a quote for any universe symbol, not only active sessions."""

        self._record(symbol, snap, source=source)

    def _record(self, symbol: str, snap, *, source: str | None) -> None:
        if not self._running:
            return
        sym = str(symbol or "").upper()
        if not sym:
            return
        size = getattr(snap, "size", None)
        if size is not None and not hasattr(snap, "bid"):
            try:
                self._vol_ws[sym] = self._vol_ws.get(sym, 0.0) + float(size or 0.0)
            except (TypeError, ValueError):
                pass
            return
        bid = getattr(snap, "bid", None)
        ask = getattr(snap, "ask", None)
        try:
            bid_f = float(bid)
            ask_f = float(ask)
        except (TypeError, ValueError):
            return
        if bid_f <= 0 or ask_f < bid_f:
            return
        now_m = time.monotonic()
        if now_m - self._last_row_t.get(sym, 0.0) < _MIN_ROW_SPACING_S:
            return
        if self._last_quote.get(sym) == (bid_f, ask_f):
            return
        self._last_quote[sym] = (bid_f, ask_f)
        self._last_row_t[sym] = now_m
        mid = (bid_f + ask_f) / 2.0
        row = {
            "symbol": sym,
            "observed_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "bid": bid_f,
            "ask": ask_f,
            "mid": mid,
            "spread_bps": (ask_f - bid_f) / mid * 10_000.0 if mid > 0 else None,
            "day_volume": self._vol_base.get(sym, 0.0) + self._vol_ws.get(sym, 0.0),
            "source": source or ("coinbase_ws" if sym.endswith("-USD") else "massive_ws"),
        }
        with self._lock:
            self._buffer.append(row)
            if len(self._buffer) > _BUFFER_CAP:
                del self._buffer[: len(self._buffer) - _BUFFER_CAP]

    def _flush_loop(self) -> None:
        last_refresh = 0.0
        while self._running:
            time.sleep(_FLUSH_INTERVAL_S)
            if not self._running:
                break
            if time.monotonic() - last_refresh >= _REFRESH_INTERVAL_S:
                self._refresh_symbols()
                last_refresh = time.monotonic()
            with self._lock:
                rows, self._buffer = self._buffer, []
            if not rows:
                continue
            db = SessionLocal()
            try:
                db.execute(
                    text(
                        "INSERT INTO momentum_nbbo_spread_tape "
                        "(symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source) "
                        "VALUES (:symbol, :observed_at, :bid, :ask, :mid, :spread_bps, :day_volume, :source)"
                    ),
                    rows,
                )
                db.commit()
            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                _log.debug("[tape_ws] flush failed rows=%d error=%s", len(rows), exc)
            finally:
                try:
                    db.rollback()
                except Exception:
                    pass
                db.close()


_recorder: TapeWsRecorder | None = None
_recorder_lock = threading.Lock()


def get_tape_ws_recorder() -> TapeWsRecorder:
    global _recorder
    if _recorder is None:
        with _recorder_lock:
            if _recorder is None:
                _recorder = TapeWsRecorder()
    return _recorder


def start_tape_ws_recorder() -> None:
    if not getattr(settings, "chili_autopilot_price_bus_enabled", False):
        return
    if not getattr(settings, "chili_momentum_nbbo_tape_enabled", True):
        return
    get_tape_ws_recorder().start()
