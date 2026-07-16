"""Event-driven ignition scorer for the momentum lane.

The scheduled scanner can miss vertical names because a strong mover may be far
away from the EMA pullback geometry. This loop subscribes the current equity
momentum universe on the price bus and scores a symbol as soon as live ticks show
one of the Ross event axes crossing. When the event also proves the Ross
small-cap setup, it can immediately admit a guarded live watcher; the live
runner still owns any order decision.
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
    _intraday_rvol,
    _snapshot_adv_shares,
    _snapshot_price,
    _snapshot_today_shares,
    _snapshot_volume_pace,
    build_equity_universe,
)

_log = logging.getLogger(__name__)

_UNIVERSE_REFRESH_S = 20.0
_SCORE_COOLDOWN_S = 20.0


class _UniverseTracker:
    """Thread-safe equity watch set plus per-symbol day baseline evidence."""

    def __init__(self, profile: UniverseProfile = EQUITY_ROSS_SMALLCAP) -> None:
        self._profile = profile
        self._lock = threading.Lock()
        self._symbols: set[str] = set()
        self._baseline: dict[str, float] = {}
        self._rvol: dict[str, float] = {}
        self._signals: dict[str, dict[str, object]] = {}

    def refresh(self) -> set[str]:
        snapshot = []
        try:
            from ...massive_client import get_full_market_snapshot

            snapshot = get_full_market_snapshot(
                max_age_seconds=self._profile.snapshot_max_age_seconds
            ) or []
        except Exception:
            _log.debug("[momentum_ws_ignition] snapshot fetch failed", exc_info=True)

        try:
            universe = build_equity_universe(self._profile, snapshot=snapshot or None)
        except Exception:
            _log.debug("[momentum_ws_ignition] universe build failed", exc_info=True)
            universe = []
        symbols = {str(symbol).strip().upper() for symbol in universe if str(symbol or "").strip()}

        baseline: dict[str, float] = {}
        rvol: dict[str, float] = {}
        signals: dict[str, dict[str, object]] = {}
        for snap in snapshot or []:
            try:
                if not isinstance(snap, dict):
                    continue
                ticker = str(snap.get("ticker") or "").strip().upper()
                if ticker not in symbols:
                    continue
                price = _snapshot_price(snap)
                day = snap.get("day") or {}
                prev = snap.get("prevDay") or {}
                base = _f(day.get("o")) or _f(prev.get("c"))
                if base and base > 0:
                    baseline[ticker] = float(base)
                today_shares = _snapshot_today_shares(snap)
                adv_shares = _snapshot_adv_shares(snap)
                rv = _intraday_rvol(today_shares, adv_shares)
                if rv is not None and rv > 0:
                    rvol[ticker] = float(rv)
                sig: dict[str, object] = {
                    "ticker": ticker,
                    "symbol": ticker,
                    "direction": "long",
                    "source": "ws_ignition",
                    "scanner_source": "ws_ignition ross small-cap universe",
                    "signal_type": "ws_ignition",
                }
                change_pct = _f(snap.get("todaysChangePerc"))
                if price is not None:
                    sig["price"] = float(price)
                    sig["last_price"] = float(price)
                if change_pct is not None:
                    sig["daily_change_pct"] = float(change_pct)
                    sig["change_pct"] = float(change_pct)
                    sig["todays_change_perc"] = float(change_pct)
                if today_shares is not None:
                    sig["volume"] = float(today_shares)
                    sig["day_volume"] = float(today_shares)
                if adv_shares is not None:
                    sig["prev_day_volume"] = float(adv_shares)
                if price is not None and today_shares is not None:
                    sig["dollar_volume"] = float(price) * float(today_shares)
                pace = _snapshot_volume_pace(snap)
                if isinstance(pace, dict):
                    for key in (
                        "rvol_pace",
                        "rvol_source",
                        "rvol_basis",
                        "expected_cum_vol",
                        "actual_cum_vol",
                        "session_elapsed_fraction",
                        "session_bucket",
                        "fallback_reason",
                    ):
                        if pace.get(key) is not None:
                            sig[key] = pace[key]
                    if pace.get("rvol_pace") is not None:
                        sig["rvol"] = pace["rvol_pace"]
                try:
                    if (change_pct is not None and float(change_pct) >= 10.0) or (
                        sig.get("rvol_pace") is not None and float(sig["rvol_pace"]) >= 5.0
                    ):
                        sig["daily_breaking_major"] = True
                except (TypeError, ValueError):
                    pass
                signals[ticker] = sig
            except Exception:
                continue

        with self._lock:
            self._symbols = symbols
            self._baseline = baseline
            self._rvol = rvol
            self._signals = signals
        return set(symbols)

    def get_symbols(self) -> set[str]:
        with self._lock:
            return set(self._symbols)

    def baseline_for(self, symbol: str) -> float | None:
        with self._lock:
            return self._baseline.get(symbol.upper())

    def rvol_for(self, symbol: str) -> float | None:
        with self._lock:
            return self._rvol.get(symbol.upper())

    def signal_for(self, symbol: str) -> dict[str, object] | None:
        with self._lock:
            sig = self._signals.get(symbol.upper())
            return dict(sig) if isinstance(sig, dict) else None

    def count(self) -> int:
        with self._lock:
            return len(self._symbols)


class IgnitionScoringLoop:
    """Bridges price-bus ticks to direct single-symbol viability scoring."""

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
            _log.info("[momentum_ws_ignition] disabled")
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
            "[momentum_ws_ignition] started symbols=%d floor_pct=%.2f",
            self._tracker.count(),
            float(getattr(settings, "chili_momentum_ignition_min_pct", 3.0)),
        )

    def stop(self) -> None:
        self._running = False
        try:
            from ..price_bus import get_price_bus

            bus = get_price_bus()
            unregister = getattr(bus, "unregister_tick_listener", None)
            if callable(unregister):
                for symbol in list(self._subscribed):
                    for callback in (self._on_tick, self._record_universe_tick):
                        try:
                            unregister(symbol, callback)
                        except Exception:
                            pass
        except Exception:
            pass
        self._subscribed = set()
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
        _log.info("[momentum_ws_ignition] stopped")

    def _sync_subscriptions(self) -> None:
        try:
            from ..price_bus import get_price_bus

            bus = get_price_bus()
        except Exception:
            return

        current = self._tracker.get_symbols()
        new_symbols = current - self._subscribed
        gone_symbols = self._subscribed - current
        densify = bool(getattr(settings, "chili_momentum_universe_tick_record_enabled", True))

        for symbol in new_symbols:
            try:
                bus.subscribe_symbol(symbol)
                bus.register_tick_listener(symbol, self._on_tick)
                if densify:
                    bus.register_tick_listener(symbol, self._record_universe_tick)
            except Exception:
                _log.debug("[momentum_ws_ignition] subscribe failed symbol=%s", symbol, exc_info=True)

        unregister = getattr(bus, "unregister_tick_listener", None)
        if callable(unregister):
            for symbol in gone_symbols:
                for callback in (self._on_tick, self._record_universe_tick):
                    try:
                        unregister(symbol, callback)
                    except Exception:
                        pass
        self._subscribed = (self._subscribed | new_symbols) - gone_symbols

    def _refresh_loop(self) -> None:
        while self._running:
            time.sleep(_UNIVERSE_REFRESH_S)
            if not self._running:
                break
            try:
                self._tracker.refresh()
                self._sync_subscriptions()
            except Exception:
                _log.debug("[momentum_ws_ignition] refresh failed", exc_info=True)

    def _quote_price(self, quote) -> float | None:
        for attr in ("last", "mid", "price", "bid"):
            try:
                value = getattr(quote, attr, None)
                if value is not None and float(value) > 0:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _move_pct(self, symbol: str, quote) -> float | None:
        for attr in ("change_pct", "todays_change_perc", "pct_change"):
            try:
                value = getattr(quote, attr, None)
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        price = self._quote_price(quote)
        baseline = self._tracker.baseline_for(symbol)
        if price is None or baseline is None or baseline <= 0:
            return None
        return (price - baseline) / baseline * 100.0

    def _record_universe_tick(self, symbol: str, quote) -> None:
        try:
            from .tape_ws_recorder import get_tape_ws_recorder

            get_tape_ws_recorder().record_external(symbol, quote)
        except Exception:
            pass

    def _on_tick(self, symbol: str, quote) -> None:
        if not self._running:
            return
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        move_pct = self._move_pct(sym, quote)
        if move_pct is None:
            return

        if bool(getattr(settings, "chili_momentum_event_select_primary_enabled", True)):
            try:
                from .nbbo_tape import _ross_threshold_crossed

                if not _ross_threshold_crossed(
                    sym,
                    rvol=self._tracker.rvol_for(sym),
                    move_pct=move_pct,
                    gap_pct=move_pct,
                    price=self._quote_price(quote),
                ):
                    return
            except Exception:
                return
        else:
            floor = float(getattr(settings, "chili_momentum_ignition_min_pct", 3.0))
            if move_pct < floor:
                return

        now = time.monotonic()
        if now - self._last_score.get(sym, 0.0) < _SCORE_COOLDOWN_S:
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
            pool.submit(self._score_symbol, sym, move_pct, self._quote_price(quote))
        except Exception:
            with self._inflight_lock:
                self._inflight.discard(sym)

    def _score_symbol(self, symbol: str, move_pct: float, price: float | None = None) -> None:
        scored_ok = False
        admission: dict[str, object] | None = None
        db = SessionLocal()
        try:
            from .pipeline import run_momentum_neural_tick

            signal = self._tracker.signal_for(symbol) or {
                "ticker": symbol,
                "symbol": symbol,
                "direction": "long",
                "todays_change_perc": float(move_pct),
                "daily_change_pct": float(move_pct),
                "change_pct": float(move_pct),
                "signal_type": "ws_ignition",
                "source": "ws_ignition",
                "scanner_source": "ws_ignition",
            }
            if price is not None and price > 0:
                signal["price"] = float(price)
                signal["last_price"] = float(price)
            signal["todays_change_perc"] = float(move_pct)
            signal["daily_change_pct"] = float(move_pct)
            signal["change_pct"] = float(move_pct)
            if bool(getattr(settings, "chili_momentum_ross_rvol_feed_enabled", True)):
                rv = self._tracker.rvol_for(symbol)
                if rv is not None and rv > 0:
                    signal["intraday_cumulative_rvol"] = float(rv)
                    signal.setdefault("rvol_pace", float(rv))
                    signal.setdefault("rvol", float(rv))
                    signal["rvol_basis"] = "cumulative_day_over_prev_day"
            run_momentum_neural_tick(
                db,
                meta={"tickers": [symbol], "ross_signals": {symbol: signal}},
            )
            try:
                from .ross_event_admission import admit_ross_event

                admission = admit_ross_event(
                    db,
                    symbol=symbol,
                    signal=signal,
                    source="ws_ignition",
                    refresh_viability=False,
                )
            except Exception as exc:
                admission = {"ok": False, "error": str(exc)[:160]}
            db.commit()
            scored_ok = True
        except Exception as exc:
            _log.debug("[momentum_ws_ignition] score failed symbol=%s error=%s", symbol, exc)
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
        _log.info(
            "[momentum_ws_ignition] symbol=%s move_pct=%.2f scored_ok=%s admitted=%s skipped=%s",
            symbol,
            float(move_pct),
            scored_ok,
            bool(admission and admission.get("admitted")),
            admission.get("skipped") if isinstance(admission, dict) else None,
        )


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
    if not getattr(settings, "chili_autopilot_price_bus_enabled", False):
        return
    if not getattr(settings, "chili_momentum_ws_ignition_enabled", False):
        return
    get_ignition_loop().start()


def stop_ignition_loop() -> None:
    if _loop is not None:
        _loop.stop()
