"""Event-driven WS IGNITION scorer — surface the day's biggest movers FAST.

The scheduled 5-min batch builder hands the screened universe to the
``scan_momentum_continuation`` enrichment, whose EMA9-pullback gate emits NOTHING
for a VERTICAL name (a +498% runner like RGNT is nowhere near its EMA9). So a
genuinely explosive mover that ``build_equity_universe`` SELECTS can still never
get a fresh ``momentum_symbol_viability`` row — the lane can't even consider it.

This loop closes that gap ADDITIVELY: it subscribes the (now-uncapped) equity
universe on the price bus and, the instant a tick shows a name igniting (intraday
move% ≥ the ignition floor), it scores THAT ONE symbol DIRECTLY into viability via
``run_momentum_neural_tick`` — the same single-symbol path ``_bridge_scanner_to_viability``
uses — BYPASSING the EMA9 continuation gate entirely. The scheduled batch + legacy
pattern lane are untouched; this is a pure additive feeder.

Mirrors the structure of ``live_runner_loop.LiveRunnerLoop``:
  * a ``_UniverseTracker`` (analogue of ``_LiveSessionTracker``) refreshes the
    watch set on a cadence and manages bus subscriptions;
  * a small bounded ``ThreadPoolExecutor`` runs the DB scoring off the WS receive
    thread (never block the bus);
  * per-symbol cooldown + an ``_inflight`` set dedup so the same name is not
    double-dispatched.

Adaptive / no-magic: ONE base FLOOR knob (``chili_momentum_ignition_min_pct``);
the refresh/cooldown cadence reuses the same ~20s rhythm as the universe rebuild.
Kill-switch: ``chili_momentum_ws_ignition_enabled=0`` ⇒ the loop is a no-op (the
scheduled-only path is byte-identical to current).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ....config import settings
from ....db import SessionLocal
from .universe import (
    EQUITY_ROSS_SMALLCAP,
    UniverseProfile,
    _f,
    build_equity_universe,
)

_log = logging.getLogger(__name__)

# Universe rebuild + per-symbol score cooldown share the same adaptive rhythm:
# the universe is re-screened every ~20s, and a name is re-scored at most once per
# the same window (a single ignition is enough to put it on the viability board;
# the scheduled batch + the auto-arm refresh keep it warm thereafter).
_UNIVERSE_REFRESH_S = 20.0
_SCORE_COOLDOWN_S = 20.0


class _UniverseTracker:
    """Thread-safe watch set: the uncapped equity universe + each name's day baseline.

    The watch set IS ``build_equity_universe`` (now uncapped). For each member it
    also captures the day baseline (today's open, else previous-day close) from the
    same full-market snapshot so ``_on_tick`` can compute the intraday move% from a
    bare ``BusQuote`` (which carries only bid/ask/mid/last, no day-open / pct).
    """

    def __init__(self, profile: UniverseProfile = EQUITY_ROSS_SMALLCAP) -> None:
        self._profile = profile
        self._lock = threading.Lock()
        self._symbols: set[str] = set()
        self._baseline: dict[str, float] = {}

    def refresh(self) -> set[str]:
        """Re-screen the universe; return the CURRENT watch set (uppercased)."""
        snapshot = None
        try:
            from ...massive_client import get_full_market_snapshot

            snapshot = get_full_market_snapshot(
                max_age_seconds=self._profile.snapshot_max_age_seconds
            ) or []
        except Exception:
            _log.debug("[momentum_ws_ignition] snapshot fetch failed", exc_info=True)
            snapshot = []

        try:
            universe = build_equity_universe(self._profile, snapshot=snapshot or None)
        except Exception:
            _log.debug("[momentum_ws_ignition] universe build failed", exc_info=True)
            universe = []
        want = {str(s).strip().upper() for s in universe if str(s or "").strip()}

        # Day baseline for each watched name: today's open, else prev-day close
        # (same base as universe._premarket_change_pct, so move% agrees with the
        # snapshot screen). Built from the SAME snapshot — no extra fetch.
        baseline: dict[str, float] = {}
        for s in snapshot or []:
            try:
                if not isinstance(s, dict):
                    continue
                t = str(s.get("ticker") or "").strip().upper()
                if t not in want:
                    continue
                day = s.get("day") or {}
                prev = s.get("prevDay") or {}
                base = _f(day.get("o")) or _f(prev.get("c"))
                if base and base > 0:
                    baseline[t] = float(base)
            except Exception:
                continue

        with self._lock:
            self._symbols = want
            self._baseline = baseline
        return set(want)

    def get_symbols(self) -> set[str]:
        with self._lock:
            return set(self._symbols)

    def baseline_for(self, symbol: str) -> float | None:
        with self._lock:
            return self._baseline.get(symbol.upper())

    def count(self) -> int:
        with self._lock:
            return len(self._symbols)


class IgnitionScoringLoop:
    """Bridges price-bus ticks to a direct single-symbol viability score."""

    def __init__(self) -> None:
        self._tracker = _UniverseTracker()
        self._running = False
        self._refresher: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._subscribed: set[str] = set()
        self._last_score: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

    def start(self) -> None:
        if not getattr(settings, "chili_momentum_ws_ignition_enabled", False):
            _log.info("[momentum_ws_ignition] disabled (chili_momentum_ws_ignition_enabled=0) — no-op")
            return
        if self._running:
            return
        self._running = True
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ws-ignition")
        self._tracker.refresh()
        self._sync_subscriptions()
        self._refresher = threading.Thread(
            target=self._refresh_loop, daemon=True, name="ws-ignition-refresh"
        )
        self._refresher.start()
        _log.info(
            "[momentum_ws_ignition] started — %d universe symbols watched (floor=%.2f%%)",
            self._tracker.count(),
            float(getattr(settings, "chili_momentum_ignition_min_pct", 3.0)),
        )

    def stop(self) -> None:
        self._running = False
        # Unsubscribe so a restarted loop re-subscribes cleanly.
        try:
            from ..price_bus import get_price_bus

            bus = get_price_bus()
            unreg = getattr(bus, "unregister_tick_listener", None)
            if callable(unreg):
                for sym in list(self._subscribed):
                    try:
                        unreg(sym, self._on_tick)
                    except Exception:
                        pass
        except Exception:
            pass
        self._subscribed = set()
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
        _log.info("[momentum_ws_ignition] stopped")

    # ── subscription management ──────────────────────────────────────────────

    def _sync_subscriptions(self) -> None:
        """Subscribe NEW universe members, unsubscribe ones that left."""
        try:
            from ..price_bus import get_price_bus

            bus = get_price_bus()
        except Exception:
            return
        current = self._tracker.get_symbols()
        new = current - self._subscribed
        gone = self._subscribed - current
        for sym in new:
            try:
                bus.subscribe_symbol(sym)
                bus.register_tick_listener(sym, self._on_tick)
            except Exception:
                _log.debug("[momentum_ws_ignition] subscribe failed for %s", sym, exc_info=True)
        unreg = getattr(bus, "unregister_tick_listener", None)
        if callable(unreg):
            for sym in gone:
                try:
                    unreg(sym, self._on_tick)
                except Exception:
                    pass
        # Keep tracking only what is currently subscribed (drop departed names even
        # if the bus has no unregister, so they are re-subscribed if they return).
        self._subscribed = (self._subscribed | new) - gone

    def _refresh_loop(self) -> None:
        while self._running:
            time.sleep(_UNIVERSE_REFRESH_S)
            if not self._running:
                break
            try:
                self._tracker.refresh()
                self._sync_subscriptions()
            except Exception:
                pass

    # ── tick handler (runs on the WS receive thread — keep it cheap) ─────────

    def _quote_price(self, quote) -> float | None:
        """Best available current price from a BusQuote (last → mid → bid)."""
        for attr in ("last", "mid", "price", "bid"):
            try:
                v = getattr(quote, attr, None)
                if v and float(v) > 0:
                    return float(v)
            except (TypeError, ValueError):
                continue
        return None

    def _move_pct(self, symbol: str, quote) -> float | None:
        """Intraday move% = (live price − day baseline) / baseline · 100.

        The day baseline is the tracker's cached today-open / prev-close. If a quote
        already carries an explicit pct/change field, prefer it (cheapest); else fall
        back to the baseline math.
        """
        for attr in ("change_pct", "todays_change_perc", "pct_change"):
            try:
                v = getattr(quote, attr, None)
                if v is not None:
                    return float(v)
            except (TypeError, ValueError):
                continue
        price = self._quote_price(quote)
        if price is None:
            return None
        base = self._tracker.baseline_for(symbol)
        if base is None or base <= 0:
            return None
        return (price - base) / base * 100.0

    def _on_tick(self, symbol: str, quote) -> None:
        if not self._running:
            return
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        move_pct = self._move_pct(sym, quote)
        if move_pct is None:
            return
        floor = float(getattr(settings, "chili_momentum_ignition_min_pct", 3.0))
        if move_pct < floor:
            return  # below the adaptive ignition floor — dead tape, ignore
        # DEDUP: one score per cooldown window + an inflight guard so two ticks
        # arriving together don't double-dispatch the same symbol.
        now = time.monotonic()
        last = self._last_score.get(sym, 0.0)
        if now - last < _SCORE_COOLDOWN_S:
            return
        with self._inflight_lock:
            if sym in self._inflight:
                return
            self._inflight.add(sym)
        self._last_score[sym] = now
        pool = self._pool
        if pool is None:
            with self._inflight_lock:
                self._inflight.discard(sym)
            return
        try:
            pool.submit(self._score_symbol, sym, move_pct)
        except Exception:
            with self._inflight_lock:
                self._inflight.discard(sym)

    # ── scoring (runs on the pool — owns its own DB session) ─────────────────

    def _score_symbol(self, symbol: str, move_pct: float) -> None:
        """Score ONE igniting symbol into momentum_symbol_viability.

        Reuses the bridge's single-symbol path: a direct ``run_momentum_neural_tick``
        with a minimal ``ross_signals`` meta — identical shape to
        ``_bridge_scanner_to_viability`` — so the vertical name (RGNT-class) gets a
        fresh viability row WITHOUT going through the EMA9 continuation gate.

        Session hygiene (the idle-in-transaction guard, #561/#610): own SessionLocal,
        commit on success, rollback on error, and rollback-in-finally before close.
        """
        scored_ok = False
        db = SessionLocal()
        try:
            from .pipeline import run_momentum_neural_tick

            ross_signals = {
                symbol: {
                    "ticker": symbol,
                    "direction": "long",
                    "todays_change_perc": float(move_pct),
                    "signal_type": "ws_ignition",
                    "source": "ws_ignition",
                }
            }
            run_momentum_neural_tick(
                db, meta={"tickers": [symbol], "ross_signals": ross_signals}
            )
            db.commit()
            scored_ok = True
        except Exception as e:
            _log.debug("[momentum_ws_ignition] score %s failed: %s", symbol, e)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            with self._inflight_lock:
                self._inflight.discard(symbol)
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
        # A/B LOG: queryable proof the ignition path put a name on the board.
        _log.info(
            "[momentum_ws_ignition] symbol=%s move_pct=%.2f scored_ok=%s",
            symbol, float(move_pct), scored_ok,
        )


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_loop: IgnitionScoringLoop | None = None
_loop_lock = threading.Lock()


def get_ignition_loop() -> IgnitionScoringLoop:
    global _loop
    if _loop is None:
        with _loop_lock:
            if _loop is None:
                _loop = IgnitionScoringLoop()
    return _loop


def start_ignition_loop() -> None:
    """Start the WS ignition scorer when the price bus + the flag are on."""
    if not getattr(settings, "chili_autopilot_price_bus_enabled", False):
        return
    if not getattr(settings, "chili_momentum_ws_ignition_enabled", False):
        return
    get_ignition_loop().start()


def stop_ignition_loop() -> None:
    if _loop is not None:
        _loop.stop()
