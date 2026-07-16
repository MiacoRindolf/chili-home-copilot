"""WS tape recorder — densify the NBBO tape with real-time ticks for traded names.

The replay's fidelity ceiling is the tape's granularity. The snapshot sampler
writes ~1 row/min/symbol; with the Massive WS now live in the runner process,
this recorder writes a tape row on every QUOTE CHANGE (throttled >=1s/symbol)
for the symbols the live lane is actually working — so tomorrow's replays see
second-scale NBBO exactly where it matters, at bounded row volume.

day_volume accounting (load-bearing for the replay's liquidity caps and
participation math, which diff consecutive rows): rows carry
``baseline + ws_trades_since_baseline`` where the baseline re-anchors to the
snapshot sampler's authoritative cumulative (source='massive_snapshot') every
time a newer sampler row lands. WS TradeSnapshot sizes accumulate in between.

Write-only diagnostic side path: no trading logic reads this module; a failure
degrades to the 1-min sampler tape.  This throttled SQL tape is never itself a
full-fidelity coverage proof.  Certifying capture uses the separate append-only
runtime, whose queue overflow and continuity rules fail closed explicitly.
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

_MIN_ROW_SPACING_S = 1.0     # per-symbol floor between recorded rows
_FLUSH_INTERVAL_S = 5.0      # buffer -> DB batch cadence
_REFRESH_INTERVAL_S = 30.0   # tracked-symbol + volume-baseline re-anchor cadence
_BUFFER_CAP = 5000           # diagnostic only; never grants full-fidelity coverage


class TapeWsRecorder:
    """Buffers WS quote ticks for live-lane equity symbols into the NBBO tape."""

    def __init__(self) -> None:
        self._running = False
        self._lock = threading.Lock()
        self._buffer: list[dict] = []
        self._symbols: set[str] = set()
        self._listening: set[str] = set()
        # per-symbol: last recorded (bid, ask), last row monotonic ts,
        # volume baseline (sampler cumulative) + ws trades accumulated since
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
        _log.info("[tape_ws] recorder started — %d symbols tracked", len(self._symbols))

    def stop(self) -> None:
        self._running = False

    # ── tracking ──────────────────────────────────────────────────────────

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
            syms = {str(r[0]).upper() for r in rows if r[0]}
            with self._lock:
                self._symbols = syms
            self._anchor_volume_baselines(db, syms)
        except Exception as e:
            _log.debug("[tape_ws] symbol refresh failed: %s", e)
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
        self._attach_listeners()

    def _anchor_volume_baselines(self, db, syms: set[str]) -> None:
        """Re-anchor day_volume to the sampler's authoritative cumulative."""
        for sym in syms:
            try:
                row = db.execute(
                    text(
                        "SELECT observed_at, day_volume FROM momentum_nbbo_spread_tape "
                        "WHERE symbol = :s AND source = 'massive_snapshot' "
                        "AND observed_at >= date_trunc('day', now() at time zone 'utc') "
                        "ORDER BY observed_at DESC LIMIT 1"
                    ),
                    {"s": sym},
                ).fetchone()
            except Exception:
                row = None
            if row is None and sym.endswith("-USD"):
                # crypto has no Massive snapshot rows — anchor to the latest tape
                # row from ANY source (best effort; 0 on a fresh symbol)
                try:
                    row = db.execute(
                        text(
                            "SELECT observed_at, day_volume FROM momentum_nbbo_spread_tape "
                            "WHERE symbol = :s "
                            "AND observed_at >= date_trunc('day', now() at time zone 'utc') "
                            "ORDER BY observed_at DESC LIMIT 1"
                        ),
                        {"s": sym},
                    ).fetchone()
                except Exception:
                    row = None
            if row is None:
                self._vol_base.setdefault(sym, 0.0)
                continue
            at, dv = row[0], float(row[1] or 0.0)
            prev_at = self._vol_base_at.get(sym)
            if prev_at is None or at > prev_at:
                self._vol_base[sym] = dv
                self._vol_base_at[sym] = at
                self._vol_ws[sym] = 0.0  # trades since this anchor start over

    def _attach_listeners(self) -> None:
        with self._lock:
            new = self._symbols - self._listening
        for sym in new:
            try:
                if sym.endswith("-USD"):
                    # CRYPTO PARITY (2026-06-11): crypto was BLIND to the tape —
                    # zero rows ever — so the running-up feeder, spread-stability
                    # gate, and replay lab were equities-only. Coinbase ticks ride
                    # the price bus into the same tape.
                    from ...trading.price_bus import get_price_bus

                    _bus = get_price_bus()
                    _bus.subscribe_coinbase(sym)
                    _bus.register_tick_listener(sym, lambda t, s2=sym: self._on_bus_tick(s2, t))
                else:
                    from ...massive_client import register_tick_listener

                    register_tick_listener(sym, self._on_tick)
                self._listening.add(sym)
            except Exception as e:
                _log.debug("[tape_ws] listener attach failed %s: %s", sym, e)

    def _on_bus_tick(self, symbol: str, tick) -> None:
        """Price-bus adapter (crypto): same dedupe/throttle path as _on_tick."""
        self._on_tick(symbol, tick)

    # ── tick handler (WS receive thread — cheap only) ─────────────────────

    def _on_tick(self, symbol: str, snap) -> None:
        self._record(symbol, snap, source=None)

    def record_external(self, symbol: str, snap, source: str = "massive_ws_universe") -> None:
        """UNIVERSE DENSIFICATION (2026-06-15): persist a WS quote for ANY symbol —
        not just the armed/live-lane set — so the whole momentum universe leaves a
        sub-minute tape forward. Runs the SAME throttle (>=1s/symbol), skip-unchanged
        dedupe, and bounded-buffer body as ``_on_tick``, but tags the row with
        ``source`` (default 'massive_ws_universe', a distinct, SHORTER-retention class)
        and does NOT require the symbol to be in ``self._symbols``. Write-only side
        path: a failure degrades to the 1-min sampler. The ignition loop registers
        this as an INDEPENDENT listener so it fires regardless of the ignition floor.
        """
        self._record(symbol, snap, source=source)

    def _record(self, symbol: str, snap, *, source: str | None) -> None:
        if not self._running:
            return
        sym = symbol.upper()
        size = getattr(snap, "size", None)
        if size is not None and not hasattr(snap, "bid"):  # TradeSnapshot
            self._vol_ws[sym] = self._vol_ws.get(sym, 0.0) + float(size or 0)
            return
        bid = getattr(snap, "bid", None)
        ask = getattr(snap, "ask", None)
        if not bid or not ask or bid <= 0 or ask < bid:
            return
        now_m = time.monotonic()
        if now_m - self._last_row_t.get(sym, 0.0) < _MIN_ROW_SPACING_S:
            return
        if self._last_quote.get(sym) == (bid, ask):
            return
        self._last_quote[sym] = (bid, ask)
        self._last_row_t[sym] = now_m
        mid = (bid + ask) / 2.0
        # Source: an explicit caller-supplied class (universe densification) wins;
        # otherwise the armed-lane default by asset class (crypto vs equity).
        if source is None:
            source = "coinbase_ws" if sym.endswith("-USD") else "massive_ws"
        row = {
            "symbol": sym,
            "observed_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "provider_event_at": (
                datetime.fromtimestamp(
                    float(snap.provider_event_at), tz=timezone.utc
                )
                if getattr(snap, "provider_event_at", None) is not None
                else None
            ),
            "received_at": (
                datetime.fromtimestamp(float(snap.received_at), tz=timezone.utc)
                if getattr(snap, "received_at", None) is not None
                else None
            ),
            "available_at": (
                datetime.fromtimestamp(float(snap.available_at), tz=timezone.utc)
                if getattr(snap, "available_at", None) is not None
                else None
            ),
            "bid": float(bid), "ask": float(ask), "mid": mid,
            "spread_bps": (ask - bid) / mid * 10_000.0 if mid > 0 else None,
            "day_volume": self._vol_base.get(sym, 0.0) + self._vol_ws.get(sym, 0.0),
            "source": source,
            "timestamp_basis": (
                "massive_sip_unix_ms"
                if getattr(snap, "provider_event_at", None) is not None
                else "local_receive_only"
            ),
            "bridge_version": "massive_ws_v2_sip_clock",
            "message_type": "Q",
            "bridge_run_id": getattr(snap, "bridge_run_id", None),
            "connection_generation": getattr(snap, "connection_generation", None),
        }
        with self._lock:
            self._buffer.append(row)
            if len(self._buffer) > _BUFFER_CAP:
                del self._buffer[: len(self._buffer) - _BUFFER_CAP]

    # ── flush loop ────────────────────────────────────────────────────────

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
                        "(symbol, observed_at, provider_event_at, received_at, available_at, "
                        "bid, ask, mid, spread_bps, day_volume, source, timestamp_basis, "
                        "bridge_version, message_type, bridge_run_id, connection_generation) "
                        "VALUES (:symbol, :observed_at, :provider_event_at, :received_at, "
                        ":available_at, :bid, :ask, :mid, :spread_bps, :day_volume, :source, "
                        ":timestamp_basis, :bridge_version, :message_type, :bridge_run_id, "
                        ":connection_generation)"
                    ),
                    rows,
                )
                db.commit()
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                _log.debug("[tape_ws] flush failed (%d rows dropped): %s", len(rows), e)
            finally:
                try:
                    db.rollback()
                except Exception:
                    pass
                db.close()


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_rec: TapeWsRecorder | None = None
_rec_lock = threading.Lock()


def get_tape_ws_recorder() -> TapeWsRecorder:
    global _rec
    if _rec is None:
        with _rec_lock:
            if _rec is None:
                _rec = TapeWsRecorder()
    return _rec


def start_tape_ws_recorder() -> None:
    if not settings.chili_autopilot_price_bus_enabled:
        return
    if not getattr(settings, "chili_momentum_nbbo_tape_enabled", True):
        return
    get_tape_ws_recorder().start()
