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
from datetime import datetime
from zoneinfo import ZoneInfo

from ....config import settings
from ....db import SessionLocal
from ..day_basis_guard import (
    DAY_BASIS_OK,
    DAY_BASIS_REJECTED,
    basis_continuity_broken,
    classify_day_basis,
)
from .live_fsm import (
    LIVE_RUNNER_RUNNABLE_STATES,
    STATE_LIVE_BAILOUT,
    STATE_LIVE_ENTERED,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_PENDING_ENTRY,
    STATE_LIVE_SCALING_OUT,
    STATE_LIVE_TRAILING,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
)
from .universe import (
    EQUITY_ROSS_SMALLCAP,
    UniverseProfile,
    _f,
    _intraday_rvol,
    _normalize_ross_common_stock_symbol,
    _snapshot_adv_shares,
    _snapshot_price,
    _snapshot_today_shares,
    build_equity_universe,
)

_log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Universe rebuild + per-symbol score cooldown share the same adaptive rhythm:
# the universe is re-screened every ~20s, and a name is re-scored at most once per
# the same window (a single ignition is enough to put it on the viability board;
# the scheduled batch + the auto-arm refresh keep it warm thereafter).
_UNIVERSE_REFRESH_S = 20.0
_SCORE_COOLDOWN_S = 20.0

# ── UNIVERSE FAIL-OPEN TO ZERO (2026-09-02) ─────────────────────────────────
# `build_equity_universe` documents a fail-open contract: "any error / empty
# snapshot -> [] so the caller falls back to its default universe (no
# regression)". `trading_scheduler` honours it (`scan_tickers = merged or None`).
# This module did NOT: it installed the empty list AS the watch set, so a
# provider outage silently converted the screen into one that watches nothing
# while still logging normally. Measured on 2026-08-28: the Massive full-market
# snapshot returned an empty body 705 times across two windows,
# 16:07:17-16:22:13 PT and 20:46:43-21:01:16 PT. Both landed after the RTH
# close, so nothing tradable was lost — but the path is live, not theoretical,
# and nothing in the log, the code, or the DB distinguished it from a quiet
# market (`trading_universe_snapshots` holds 0 rows; the two failure branches
# below logged at DEBUG under a root logger pinned to INFO in app/main.py:8-9).
#
# The retention below is deliberately BOUNDED. A stale watch set is better than
# an empty one for a few minutes and worse than one after many, so the outage
# is ridden out and then surrendered — loudly, in both directions.
_UNIVERSE_RETAIN_MAX_S = 300.0

# Refresh outcomes. Only `ok` and `screen_empty` are the screen speaking; the
# other three are the provider failing, and the two must never look alike.
_UNIVERSE_OK = "ok"
_UNIVERSE_SCREEN_EMPTY = "screen_empty"
_UNIVERSE_SNAPSHOT_EMPTY = "snapshot_empty"
_UNIVERSE_SNAPSHOT_ERROR = "snapshot_error"
_UNIVERSE_BUILD_ERROR = "build_error"
_UNIVERSE_DEGRADED = frozenset(
    {_UNIVERSE_SNAPSHOT_EMPTY, _UNIVERSE_SNAPSHOT_ERROR, _UNIVERSE_BUILD_ERROR}
)
# Ang session-threshold inventory ay mas maliit (iilang row) at mas time-critical
# kaysa sa universe screen: ang na-ratchet na trail stop o kaka-armang session ay
# hindi dapat maging bulag sa bridge nang 20s. Hiwalay na mabilis na cadence.
_SESSION_REFRESH_S = 5.0

# LANE OBSERVATION HEARTBEAT (2026-09-02). 30s: the watchdog's silence threshold
# is measured in minutes, so this only has to be small enough that a genuine
# death is unambiguous within one threshold window, and large enough that it
# costs nothing (one tiny insert per 30s, on the refresh thread, never the bus).
_OBSERVATION_HEARTBEAT_S = 30.0


class _UniverseTracker:
    """Thread-safe watch set: the uncapped equity universe + each name's day baseline.

    The watch set IS ``build_equity_universe`` (now uncapped). For each member it
    also captures the day baseline (previous-day close, else today's open — #1284)
    AND the open separately (``open_baseline_for``, for the since-open move) from the
    same full-market snapshot so ``_on_tick`` can compute the intraday move% from a
    bare ``BusQuote`` (which carries only bid/ask/mid/last, no day-open / pct).
    """

    def __init__(self, profile: UniverseProfile = EQUITY_ROSS_SMALLCAP) -> None:
        self._profile = profile
        self._lock = threading.Lock()
        self._symbols: set[str] = set()
        self._baseline: dict[str, float] = {}
        # #1284: ang OPEN nang hiwalay — para sa since-open na move ng RVOL-axis
        # direction guard (_rvol_alone_may_fire), hindi para sa stamp.
        self._open_baseline: dict[str, float] = {}
        # FIX A1: per-symbol intraday RVOL (today's accumulated shares / prevDay ADV),
        # captured from the SAME full-market snapshot the universe screen uses. This is
        # a REAL relative-volume value (not a move-proxy) — the ignition feeder ignites
        # on a volume surge, so this is the magnitude it ignited on. Threaded into
        # ross_signals as ``vol_ratio`` so the explosive scorer's CORE is no longer
        # rvol-None -> 0.0 -> tilt-penalised for every ws_ignition mover.
        self._rvol: dict[str, float] = {}
        # Today's accumulated SHARES per watched name (same snapshot, same helper the
        # universe screen uses). Stamped onto the ws_ignition signal as ``volume`` so
        # the Ross universe evidence gate can derive dollar_volume = price × volume.
        # Without it, every ws_ignition-sourced viability row failed that gate closed
        # (`ross_universe_missing_price`) — proved live 2026-08-19 against the running
        # board: BMNG/CONX/MSTX/COIW/YJ all ok=False, while premarket_gap-sourced rows
        # (AZI/SKK) passed. That kept the scoped ignition→arm bridge from EVER arming a
        # WS-ignited name, and it also degraded any row the WS path overwrote.
        self._shares: dict[str, float] = {}
        # VELOCITY INTAKE (2026-08-28, ang XLAB blindspot): short-horizon na
        # pagtaas ng presyo sa pagitan ng mga snapshot refresh (~20s cadence),
        # HIWALAY sa day change. Sinukat: ang #1 play ni Ross (XLAB, SPAC day-2)
        # ay −2.4% sa araw pero +12% sa loob ng minutos — ang day-change na
        # screen (min_change_pct=5 vs prev close) ay bulag dito, kaya ZERO tape,
        # zero viability, invisible. Ang "Running Up" scanner ni Ross ay puro
        # velocity. Itinatago rito ang (monotonic_ts, {sym: px}) na kasaysayan sa
        # loob ng window at ang pinakamataas na %rise kada pangalan.
        self._price_history: list[tuple[float, dict[str, float]]] = []
        self._velocity: dict[str, float] = {}
        # UNIVERSE FAIL-OPEN TO ZERO: the last refresh's outcome + the retention
        # clock, so an empty watch set is never confused with a quiet market.
        self._last_outcome: str = _UNIVERSE_OK
        self._last_screened_size: int = 0
        self._retaining_since: float | None = None
        # BASIS INTEGRITY (2026-09-02): the accepted prev close per name for the
        # CURRENT ET session, plus once-per-session alarm memory so a corrupt
        # row logs one line rather than one per 20s refresh (HOS produced 798
        # observations in a single session). Cleared on the ET date rollover —
        # a prev close is fixed within a session and legitimately changes
        # between them. Touched only by the refresh thread.
        self._basis_held: dict[str, float] = {}
        self._basis_rejected: set[str] = set()
        self._basis_drifted: set[str] = set()
        self._basis_session_date: str | None = None

    def last_outcome(self) -> str:
        """Outcome of the most recent refresh (see the `_UNIVERSE_*` constants)."""
        with self._lock:
            return self._last_outcome

    def _roll_basis_session(self) -> None:
        """Drop the frozen-basis memory on the ET trading-date rollover."""
        today = datetime.now(_ET).date().isoformat()
        if today != self._basis_session_date:
            self._basis_session_date = today
            self._basis_held = {}
            self._basis_rejected = set()
            self._basis_drifted = set()

    def refresh(self) -> set[str]:
        """Re-screen the universe; return the CURRENT watch set (uppercased).

        A degraded provider must NOT be able to empty the watch set silently. The
        outcome is classified, an empty result from a degraded provider retains
        the previous set for up to ``_UNIVERSE_RETAIN_MAX_S``, and every state
        change is logged at WARNING/INFO with a DISTINCT line — an empty screen
        on a healthy snapshot reads differently from a provider outage.
        """
        self._roll_basis_session()
        outcome = _UNIVERSE_OK
        snapshot = None
        try:
            from ...massive_client import get_full_market_snapshot

            snapshot = get_full_market_snapshot(
                max_age_seconds=self._profile.snapshot_max_age_seconds
            ) or []
        except Exception:
            # Was `_log.debug`, which app/main.py:8-9 (root logger pinned to
            # INFO) discards outright: a full-file scan of the 3.6M-line lane log
            # found 0 occurrences of this message, ever.
            _log.warning("[momentum_ws_ignition] snapshot fetch failed", exc_info=True)
            snapshot = []
            outcome = _UNIVERSE_SNAPSHOT_ERROR
        if outcome == _UNIVERSE_OK and not snapshot:
            outcome = _UNIVERSE_SNAPSHOT_EMPTY

        try:
            universe = build_equity_universe(self._profile, snapshot=snapshot or None)
        except Exception:
            _log.warning("[momentum_ws_ignition] universe build failed", exc_info=True)
            universe = []
            outcome = _UNIVERSE_BUILD_ERROR
        want = {str(s).strip().upper() for s in universe if str(s or "").strip()}
        if not want and outcome == _UNIVERSE_OK:
            # Healthy snapshot, nothing cleared the bands. This is the screen
            # speaking, and it is legitimate (03:52 ET, before the premarket
            # session, produced exactly this on 08-18/08-19/08-20/08-22).
            outcome = _UNIVERSE_SCREEN_EMPTY

        # ── VELOCITY INTAKE (2026-08-28): day-change-independent na admission ──
        # Monotonic OR-leg: NAKAKADAGDAG lamang sa `want`, hindi nakakabawas, kaya
        # ang day-change na universe ay byte-identical kapag walang qualifier o
        # kapag naka-off ang flag. Hygiene sa admission: common-stock symbol
        # (walang warrant/unit/leveraged-ETP), presyo sa loob ng profile band, at
        # ang PAREHONG $-volume floor ng screen (day.v/min.av — ext-hours-aware).
        velocity: dict[str, float] = {}
        if snapshot and bool(getattr(
            settings, "chili_momentum_velocity_intake_enabled", True
        )):
            try:
                _vel_floor = float(getattr(
                    settings, "chili_momentum_velocity_intake_min_pct", 7.0
                ) or 7.0)
            except (TypeError, ValueError):
                _vel_floor = 7.0
            try:
                _vel_window = float(getattr(
                    settings,
                    "chili_momentum_velocity_intake_window_seconds", 180.0
                ) or 180.0)
            except (TypeError, ValueError):
                _vel_window = 180.0
            _now_mono = time.monotonic()
            _cur: dict[str, float] = {}
            _row_by_sym: dict[str, dict] = {}
            for s in snapshot:
                try:
                    if not isinstance(s, dict):
                        continue
                    _t = _normalize_ross_common_stock_symbol(s.get("ticker"))
                    if not _t:
                        continue
                    _px = _snapshot_price(s)
                    if _px is None or float(_px) <= 0:
                        continue
                    _cur[_t] = float(_px)
                    _row_by_sym[_t] = s
                except Exception:
                    continue
            for _hist_ts, _hist_px in self._price_history:
                if _now_mono - _hist_ts > _vel_window:
                    continue
                for _t, _px in _cur.items():
                    _old = _hist_px.get(_t)
                    if _old and _old > 0:
                        _rise = (_px - _old) / _old * 100.0
                        if _rise > velocity.get(_t, 0.0):
                            velocity[_t] = _rise
            _admits: set[str] = set()
            for _t, _rise in velocity.items():
                if _rise < _vel_floor or _t in want:
                    continue
                _px = _cur.get(_t)
                _row = _row_by_sym.get(_t)
                if _px is None or _row is None:
                    continue
                if (
                    self._profile.price_max is not None
                    and _px > float(self._profile.price_max)
                ):
                    continue
                # SUB-$1 PAPER LANE: kapag bukas ang paper flag, ang sub-dollar
                # velocity mover ay pumapasok sa watch set (FNGR/CHAI/DUO-class)
                # — ang LIVE arm ay nakakandado pa rin sa auto_arm.
                if (
                    self._profile.price_min is not None
                    and _px < float(self._profile.price_min)
                    and not bool(getattr(
                        settings, "chili_momentum_subdollar_paper_enabled", True
                    ))
                ):
                    continue
                if self._profile.min_dollar_volume is not None:
                    _day = _row.get("day") or {}
                    _minute = _row.get("min") or {}
                    _vol = max(
                        _f(_day.get("v")) or 0.0,
                        _f(_minute.get("av")) or 0.0,
                    )
                    if _px * _vol < float(self._profile.min_dollar_volume):
                        continue
                _admits.add(_t)
            if _admits:
                want |= _admits
                _log.info(
                    "[momentum_ws_ignition] velocity intake admitted %s "
                    "(>=%.1f%% sa loob ng %.0fs, hiwalay sa day change)",
                    sorted(_admits), _vel_floor, _vel_window,
                )
            self._price_history.append((_now_mono, _cur))
            self._price_history = [
                (ts, p) for ts, p in self._price_history
                if _now_mono - ts <= _vel_window
            ][-12:]
            # Ang velocity ay itinatabi para sa LAHAT ng nasa final watch set —
            # nagsisilbi itong pang-apat na ignite axis sa _on_tick, kaya kahit
            # ang day-change na miyembro na bumubulusok NGAYON ay makaka-ignite
            # nang hindi hinihintay ang 10% day floor.
            velocity = {t: v for t, v in velocity.items() if t in want}

        # Day baseline for each watched name: prev-day close, else today's open
        # (#1284 — same meaning as the vendor todaysChangePerc every consumer of
        # the stamped change assumes), plus the open kept separately for the
        # since-open move. Built from the SAME snapshot — no extra fetch.
        baseline: dict[str, float] = {}
        open_baseline: dict[str, float] = {}
        rvol: dict[str, float] = {}
        shares: dict[str, float] = {}
        for s in snapshot or []:
            try:
                if not isinstance(s, dict):
                    continue
                t = str(s.get("ticker") or "").strip().upper()
                if t not in want:
                    continue
                day = s.get("day") or {}
                prev = s.get("prevDay") or {}
                # BASIS INTEGRITY (2026-09-02, #1284). Dating `day.o or prev.c`:
                # premarket ay zero ang `day` kaya prev close ang base, pero sa
                # 13:30Z ay lumilipat ito sa OPENING PRINT — at ang move% na
                # itina-stamp sa :1136 bilang `todays_change_perc` (na binabasa
                # ng viability A-setup floor at ng Ross-universe check bilang
                # change-vs-PREV-CLOSE) ay bumabagsak sa ~0. BIAF 09-01: prev
                # close 4.56, open 7.63 → +67% ay nabasang +0.13% → "Below
                # A-setup quality floor" × 15 row, hinarang sa 13:31-13:37Z
                # habang bumubuo ng bagong HOD (8.585 @13:38). Sa 3 araw: BIAF,
                # FLYE, GYGY (09-01, 45/45 row ≥10% sa tamang basis) + 08-28/
                # 08-31. Ang vendor na `todaysChangePerc` ng screen at ng tape
                # ay prev-close-based; ang `day.o` ay fallback lang doon. Kaya
                # prev close MUNA dito rin — iisa ang kahulugan ng datum sa
                # buong araw. Ang `day.o` ay natitira bilang fallback para sa
                # day-1 listing na walang prev close.
                # BASIS PLAUSIBILITY (2026-09-02). The division below is only as
                # honest as `prev.c`, which nothing validated. HOS 2026-09-02
                # arrived with prev.c=0.30 against a $10.33 stock and produced
                # move_pct=3462.37 across 798 observations and 9,290 persisted
                # viability rows — the #1 ross_score on the board, for a name
                # that moved 4.75% all day. A rejected basis yields NO baseline,
                # never a substitute: an unknown change is fail-closed at the
                # arm gate, a fictional one is fail-open into the top of the
                # ranking. See ..day_basis_guard for what this does and does not
                # catch.
                _prev_c, _verdict = classify_day_basis(
                    prev.get("c"),
                    open_price=day.get("o"),
                    price=_snapshot_price(s),
                    prev_high=prev.get("h"),
                    prev_low=prev.get("l"),
                )
                if _verdict in DAY_BASIS_REJECTED:
                    if t not in self._basis_rejected:
                        self._basis_rejected.add(t)
                        _log.warning(
                            "[momentum_ws_ignition] basis REJECTED %s "
                            "(verdict=%s prev_close=%r open=%r) — no day change "
                            "will be stamped for this name",
                            t, _verdict, prev.get("c"), day.get("o"),
                        )
                    base = None
                elif _verdict == DAY_BASIS_OK:
                    base = _prev_c
                else:
                    # No prev close at all (day-1 listing). Unchanged fallback.
                    base = _f(day.get("o"))
                # CONTINUITY (2026-09-02). A previous close is fixed for the
                # session; the tracker re-reads it every 20s and silently
                # re-based on whatever arrived. Freeze the first accepted value
                # and say so once, rather than reporting several mutually
                # inconsistent day changes for one name in one session.
                _held = self._basis_held.get(t)
                if base and base > 0 and _held and basis_continuity_broken(_held, base):
                    if t not in self._basis_drifted:
                        self._basis_drifted.add(t)
                        _log.warning(
                            "[momentum_ws_ignition] basis DRIFTED mid-session %s "
                            "(held=%.4f candidate=%.4f) — FREEZING the held value",
                            t, _held, float(base),
                        )
                    base = _held
                if base and base > 0:
                    baseline[t] = float(base)
                    self._basis_held[t] = float(base)
                open_base = _f(day.get("o"))
                if open_base and open_base > 0:
                    open_baseline[t] = float(open_base)
                # FIX A1: REAL intraday relative-volume from the SAME snapshot — today's
                # accumulated shares (day.v else min.av, ext-hours-aware) / prevDay ADV.
                # Reuses the universe screen's own helpers so the value AGREES with the
                # screen. Fail-open: missing either side -> _intraday_rvol returns None ->
                # the name simply has no rvol fed (the A2 scorer guard handles that name).
                _today_shares = _snapshot_today_shares(s)
                _rv = _intraday_rvol(_today_shares, _snapshot_adv_shares(s))
                if _rv is not None and _rv > 0:
                    rvol[t] = float(_rv)
                if _today_shares is not None and float(_today_shares) > 0:
                    shares[t] = float(_today_shares)
            except Exception:
                continue

        screened_size = len(want)
        now_mono = time.monotonic()
        with self._lock:
            prev_symbols = set(self._symbols)
            retain = (
                not want
                and outcome in _UNIVERSE_DEGRADED
                and bool(prev_symbols)
                and bool(getattr(
                    settings,
                    "chili_momentum_universe_retain_on_provider_failure_enabled",
                    True,
                ))
            )
            if retain:
                if self._retaining_since is None:
                    self._retaining_since = now_mono
                held_for = now_mono - self._retaining_since
                if held_for > _UNIVERSE_RETAIN_MAX_S:
                    retain = False
            else:
                held_for = 0.0

            if retain:
                # Keep the whole cached screen — symbols AND their baselines — so
                # the retained names still score correctly while the provider is
                # down. Nothing else in this method touched `self`.
                self._last_outcome = outcome
                self._last_screened_size = screened_size
                _log.warning(
                    "[momentum_ws_ignition] universe RETAINED — provider degraded "
                    "(outcome=%s), screen returned 0; holding %d cached symbols "
                    "for %.0fs (max %.0fs). THIS IS NOT A QUIET MARKET.",
                    outcome, len(prev_symbols), held_for, _UNIVERSE_RETAIN_MAX_S,
                )
                return set(prev_symbols)

            if (
                not want
                and outcome in _UNIVERSE_DEGRADED
                and self._retaining_since is not None
            ):
                _log.warning(
                    "[momentum_ws_ignition] universe SURRENDERED to empty — "
                    "provider degraded (outcome=%s) for %.0fs, past the %.0fs "
                    "retention bound; the lane is now watching NOTHING.",
                    outcome, now_mono - self._retaining_since, _UNIVERSE_RETAIN_MAX_S,
                )
            self._retaining_since = None
            self._last_outcome = outcome
            self._last_screened_size = screened_size
            self._symbols = want
            self._baseline = baseline
            self._open_baseline = open_baseline
            self._rvol = rvol
            self._shares = shares
            self._velocity = velocity
        return set(want)

    def get_symbols(self) -> set[str]:
        with self._lock:
            return set(self._symbols)

    def baseline_for(self, symbol: str) -> float | None:
        with self._lock:
            return self._baseline.get(symbol.upper())

    def open_baseline_for(self, symbol: str) -> float | None:
        """Today's open (None premarket / day-1 listing) — para sa since-open move."""
        with self._lock:
            return self._open_baseline.get(symbol.upper())

    def rvol_for(self, symbol: str) -> float | None:
        """FIX A1: last-snapshot intraday RVOL for this name (None if unknown)."""
        with self._lock:
            return self._rvol.get(symbol.upper())

    def shares_for(self, symbol: str) -> float | None:
        """Today's accumulated shares from the last snapshot (None if unknown)."""
        with self._lock:
            return self._shares.get(symbol.upper())

    def velocity_for(self, symbol: str) -> float | None:
        """VELOCITY INTAKE: pinakamataas na %rise sa loob ng window (None kung wala)."""
        with self._lock:
            return self._velocity.get(symbol.upper())

    def count(self) -> int:
        with self._lock:
            return len(self._symbols)


_KEY_LIVE_EXEC = "momentum_live_execution"
_SESSION_POSITION_STATES = frozenset(
    {STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT}
)
# ENTRY-ADVANCE WAKE (2026-09-02, #1282). Ang mga pre-entry state na umuusad
# LANG sa 10s batch cadence noon: tatlo sa kanila ay hindi magigising ng tick
# kailanman, at ang WATCHING ay sa `watch_break_level` cross lamang (na wala
# sa velocity/volume/VWAP na trigger). Ang momentum entry ay pumuputok habang
# UMAAKYAT ang presyo, kaya ang parehong bagong-high na wake ng held position
# (seed muna, saka bawat bagong high, 2s spacing kada session) ay ginagamit na
# rin dito. Dispatch hint lamang — ang FSM ang nagpapasya. Ang budget ay sa
# `_spawn_session_wake` (tingnan ang `_advance_admit`).
# Ang ARMED_PENDING_RUNNER ay SADYANG wala rito: ginigising na ito sa sandali
# ng arm (`wake_armed_sessions`), at ang tick-wake nito ay magbabayad ng buong
# quote gate para sa isang one-line na state bump.
_SESSION_PRE_ENTRY_STATES = frozenset(
    {STATE_QUEUED_LIVE, STATE_WATCHING_LIVE, STATE_LIVE_ENTRY_CANDIDATE}
)


def _entry_advance_wake_enabled() -> bool:
    return bool(getattr(settings, "chili_momentum_entry_advance_wake_enabled", True))


def _entry_advance_wakes_per_second() -> float:
    try:
        cap = float(
            getattr(settings, "chili_momentum_entry_advance_wake_max_per_second", 6.0)
            or 6.0
        )
    except (TypeError, ValueError):
        cap = 6.0
    return cap if cap > 0 else 6.0


def _entry_advance_max_inflight() -> int:
    try:
        cap = int(
            getattr(settings, "chili_momentum_entry_advance_wake_max_inflight", 8) or 8
        )
    except (TypeError, ValueError):
        cap = 8
    return cap if cap > 0 else 8


# ADVANCE-WAKE ADMISSION (#1282, pagkatapos ng adversarial review). Ang budget
# ay kinukuha LAMANG pagkatapos pumasa ang spacing at single-flight sa
# `_spawn_session_wake` — kung sa `crossed` ito kinuha, ang isang mainit na
# pangalang nagpi-print ng 10 uptick/s ay uubos ng buong budget nang walang
# isa mang spawn (spacing ang tumatanggi) at magugutom ang 39 pang session.
# Ang tracker ay nagmamarka lang ng HINT kada sid; ang spawn ang kumokonsumo.
# Token bucket (rate = cap/s, burst = max(1, cap)) + hangganan ng SABAY na
# tumatakbo (ang rate cap ay hindi humahangga sa concurrency: sa 8.9s na
# place path × 3 continuation step ay 40 thread ang aabutin ng rate lang).
_ADVANCE_HINT_TTL_S = 2.0
_ADVANCE_REFRESH_DEBOUNCE_S = 1.0
_advance_hint: dict[int, float] = {}
_adv_lock = threading.Lock()
_adv_tokens: float = -1.0          # -1 = hindi pa nasisimulan (puno sa unang gamit)
_adv_last_refill: float = 0.0
_adv_running: set[int] = set()
_adv_stats = {"admitted": 0, "rejected_budget": 0, "rejected_inflight": 0}
_adv_last_stats_log: float = 0.0
_adv_last_refresh: float = 0.0


def _mark_advance_hint(sid: int, now: float) -> None:
    with _adv_lock:
        _advance_hint[sid] = now
        if len(_advance_hint) > 512:
            stale = [k for k, v in _advance_hint.items() if now - v > _ADVANCE_HINT_TTL_S]
            for k in stale:
                _advance_hint.pop(k, None)


def _take_advance_hint(sid: int, now: float) -> bool:
    """Kinokonsumo sa BAWAT pagtatangkang mag-spawn, pumasa man o hindi."""
    with _adv_lock:
        marked = _advance_hint.pop(sid, None)
    return marked is not None and (now - marked) <= _ADVANCE_HINT_TTL_S


def _advance_admit(sid: int, now: float) -> bool:
    """Isang token + isang slot para sa advance wake; False = hayaan sa batch."""
    global _adv_tokens, _adv_last_refill, _adv_last_stats_log
    rate = _entry_advance_wakes_per_second()
    burst = max(1.0, rate)
    snap = None
    with _adv_lock:
        if _adv_tokens < 0:
            _adv_tokens = burst
        else:
            _adv_tokens = min(burst, _adv_tokens + max(0.0, now - _adv_last_refill) * rate)
        _adv_last_refill = now
        if len(_adv_running) >= _entry_advance_max_inflight():
            _adv_stats["rejected_inflight"] += 1
            ok = False
        elif _adv_tokens < 1.0:
            _adv_stats["rejected_budget"] += 1
            ok = False
        else:
            _adv_tokens -= 1.0
            _adv_running.add(sid)
            _adv_stats["admitted"] += 1
            ok = True
        if now - _adv_last_stats_log >= 60.0:
            _adv_last_stats_log = now
            snap = (dict(_adv_stats), len(_adv_running))
    if snap is not None:
        _log.info(
            "[momentum_ws_ignition] advance-wake admitted=%d rejected_budget=%d "
            "rejected_inflight=%d running=%d",
            snap[0]["admitted"], snap[0]["rejected_budget"],
            snap[0]["rejected_inflight"], snap[1],
        )
    return ok


def _advance_done(sid: int) -> None:
    with _adv_lock:
        _adv_running.discard(sid)


def _advance_refresh_due(now: float) -> bool:
    """Debounce ng post-wake inventory reload para sa advance wakes: isa kada ~1s.

    Ang bawat wake ay nagre-reload ng BUONG runnable inventory (SQL + JSON
    snapshot kada session) + subscription sync; sa 6 wake/s iyon ay 6 na
    buong reload kada segundo para sa walang bagong impormasyon. Ang 5s
    refresher at ang batch ang sumasaklaw sa natitira.
    """
    global _adv_last_refresh
    with _adv_lock:
        if now - _adv_last_refresh < _ADVANCE_REFRESH_DEBOUNCE_S:
            return False
        _adv_last_refresh = now
        return True


class _SessionCrossTracker:
    """Thread-safe: symbol -> runnable LIVE-session thresholds for tick-cross wakes.

    TICK-SPEED OPEN/CLOSE (2026-08-23). Sa batch/scheduler window ang bawat
    entry/exit state ay umuusad LANG sa scheduler cadence (nominal 10s, sukat na
    10–30s kada pass) — ang loop-mode tick bridge na nagdi-dispatch sa sandaling
    tumawid ang presyo sa stop/target/watch-break ay RETIRED noong 08-17 cutover.
    Ang tracker na ito ang batch-mode mirror ng ``live_runner_loop`` inventory
    read: kapag may tick na tumawid sa naka-imbak na threshold, gumigising ito ng
    AGARANG runner tick via ``dispatch_live_runner_tick`` (FOR UPDATE NOWAIT sa
    dispatcher kaya benign ang karera vs batch). DISPATCH HINT LAMANG ang bawat
    wake — ang FSM pa rin ang nagbabasa ng sariwang quote at nagpapasya.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_symbol: dict[str, list[dict]] = {}
        # Running HIGH kada held session (TRAIL/LADDER GAP 2026-08-23): ang trail
        # ratchet, breakeven ratchet, at 1R partial arm ay lahat nag-e-evaluate
        # LOOB ng FSM tick — ang stop/target crossing lang ang ginigising noon,
        # kaya ang mga ito ay sumasakay pa rin sa 10s cadence. Ang bagong-high na
        # wake ang tumatapos doon: habang UMAAKYAT ang presyo (eksaktong sandali
        # ng sell-into-strength at ng ratchet), bawat bagong high ay gumigising
        # (spacing-bound sa ~2s) kaya ang ladder/trail ay tumatakbo sa tick-speed
        # nang HINDI kinokopya ang ladder math dito. Ang unang obserbasyon ay
        # SEED lamang (walang wake) para hindi sumabog ang bridge start.
        self._hi: dict[int, float] = {}

    def refresh(self) -> None:
        from ....models.trading import TradingAutomationSession

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
            new_map: dict[str, list[dict]] = {}
            for sess in rows:
                sym = str(sess.symbol or "").strip().upper()
                if not sym:
                    continue
                snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
                le = snap.get(_KEY_LIVE_EXEC) if isinstance(snap.get(_KEY_LIVE_EXEC), dict) else {}
                pos = le.get("position") if isinstance(le.get("position"), dict) else None
                entry: dict = {"session_id": int(sess.id), "state": str(sess.state or "")}
                # PENDING-EXIT wake (2026-08-23): matapos ang exit POST, ang fill
                # resolution at ang limit repeg ay isang poll KADA PULSE — at ang
                # tipikal na hugis ng stop-out ay flush-tapos-bounce, kaya ang
                # presyo ay bumabalik sa IBABAW ng stop: walang cross, walang
                # bagong high, kaya WALANG wake sa ilalim ng threshold-only na
                # modelo. Ang isang exit na may nakabinbing broker order (o isang
                # deferred deadman handoff phase) ay ginigising kada tick tulad ng
                # PENDING_ENTRY — mas maagang flat = mas maagang re-entry slot.
                if isinstance(le, dict) and (
                    le.get("pending_exit_reason")
                    or isinstance(le.get("deadman_released_for_close"), dict)
                ):
                    entry["pending_exit"] = True
                if pos and sess.state in _SESSION_POSITION_STATES:
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
                new_map.setdefault(sym, []).append(entry)
            with self._lock:
                self._by_symbol = new_map
                tracked = {
                    int(e["session_id"]) for lst in new_map.values() for e in lst
                }
                for sid in [k for k in self._hi if k not in tracked]:
                    self._hi.pop(sid, None)
        except Exception:
            _log.debug("[momentum_ws_ignition] session-cross refresh failed", exc_info=True)
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

    def symbols(self) -> set[str]:
        with self._lock:
            return set(self._by_symbol)

    def watching(self, symbol: str) -> list[int]:
        """Session ids sa STATE_WATCHING_LIVE para sa symbol na ito — ang mga
        matang dapat gisingin kapag umiignite ang pangalan (hindi lamang kapag
        may level cross). Mura: isang dict lookup sa ilalim ng lock."""
        sym = str(symbol or "").strip().upper()
        with self._lock:
            entries = self._by_symbol.get(sym)
            entries = list(entries) if entries else []
        return [
            int(e["session_id"]) for e in entries
            if e.get("state") == STATE_WATCHING_LIVE
        ]

    def crossed(self, symbol: str, quote) -> list[int]:
        """Session ids whose stored threshold this tick crossed (hint lamang).

        Parehong semantics ng loop-mode ``_on_tick``: exit ref = bid (fallback
        mid) vs stop/target; entry ref = mid (fallback bid) vs watch_break_level;
        ang PENDING_ENTRY ay ginigising kada tick (spacing-bound) para sa
        tick-speed fill resolution.
        """
        sym = str(symbol or "").strip().upper()
        with self._lock:
            entries = self._by_symbol.get(sym)
            entries = list(entries) if entries else []
        if not entries:
            return []
        try:
            bid_raw = getattr(quote, "bid", None)
            mid_raw = (
                getattr(quote, "mid", None)
                or getattr(quote, "price", None)
                or getattr(quote, "last", None)
            )
            bid = float(bid_raw) if bid_raw else 0.0
            mid = float(mid_raw) if mid_raw else 0.0
        except (TypeError, ValueError):
            return []
        exit_ref = bid if bid > 0 else mid
        hits: list[int] = []
        for s in entries:
            state = s.get("state")
            if s.get("pending_exit"):
                # in-flight exit: resolve the fill / repeg at tick speed
                hits.append(int(s["session_id"]))
                continue
            if state in _SESSION_POSITION_STATES:
                sid = int(s["session_id"])
                stop_px = float(s.get("stop_px") or 0.0)
                target_px = float(s.get("target_px") or 0.0)
                if exit_ref > 0 and (
                    (stop_px > 0 and exit_ref <= stop_px)
                    or (target_px > 0 and exit_ref >= target_px * 0.995)
                ):
                    hits.append(sid)
                    continue
                # BAGONG-HIGH wake (trail/ladder gap): ang pag-akyat ang
                # nagpapagalaw ng ratchet at nagpapa-arm ng partial — patakbuhin
                # ang FSM habang nangyayari ito, hindi 10s pagkatapos. Ang unang
                # tick pagkatapos ma-track ay seed lamang.
                px = mid if mid > 0 else bid
                if px > 0:
                    with self._lock:
                        prior = self._hi.get(sid)
                        if prior is None:
                            self._hi[sid] = px
                        elif px > prior:
                            self._hi[sid] = px
                            hits.append(sid)
            elif state == STATE_LIVE_PENDING_ENTRY:
                hits.append(int(s["session_id"]))
            elif state in _SESSION_PRE_ENTRY_STATES:
                sid = int(s["session_id"])
                # Ang running high ay nagra-ratchet PALAGI — kahit level-cross
                # ang gumising — para ang held branch pagkatapos ng fill ay
                # magmana ng TUNAY na high, hindi ng lumang seed (kung hindi,
                # bawat uptick sa ILALIM ng entry ay "bagong high" hanggang
                # umabot ito).
                px = mid if mid > 0 else bid
                is_new_high = False
                if px > 0:
                    with self._lock:
                        prior = self._hi.get(sid)
                        if prior is None:
                            self._hi[sid] = px
                        elif px > prior:
                            self._hi[sid] = px
                            is_new_high = True
                if state == STATE_WATCHING_LIVE:
                    wl = float(s.get("watch_break_level") or 0.0)
                    ref = mid if mid > 0 else bid
                    if wl > 0 and ref > wl:
                        hits.append(sid)
                        continue
                # ENTRY-ADVANCE wake (#1282): ang parehong bagong-high na wake ng
                # held position, para sa pre-entry. Seed ang unang obserbasyon;
                # bawat bagong high pagkatapos ay gumigising. HINT lang dito —
                # ang budget at concurrency cap ay sa _spawn_session_wake,
                # PAGKATAPOS ng spacing at single-flight. Ang pullback at pantay
                # na tick ay tahimik; ang level reclaim sa ilalim ng high ay ang
                # level-cross sa itaas.
                if is_new_high and _entry_advance_wake_enabled():
                    _mark_advance_hint(sid, time.monotonic())
                    hits.append(sid)
        return hits


# Per-session minimum spacing between tick-cross wakes: a collapsing tape prints
# many ticks below the stop; one wake ticks the FSM, the spacing bounds the rest.
# The scheduler batch stays the safety net for anything the spacing skips.
_session_wake_last: dict[int, float] = {}
_session_wake_lock = threading.Lock()


def _spawn_session_wake(session_id) -> bool:
    """Single-flight, spacing-bound wake for one tracked session (tick cross)."""
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return False
    from ....config import settings as _st

    now = time.monotonic()
    # Ang advance hint (#1282) ay kinokonsumo sa BAWAT pagtatangka, pumasa man
    # o hindi ang spacing — hindi ito dapat dumikit sa ibang wake ng parehong
    # session mamaya. Ang token ay kinukuha LAMANG kapag tunay na magsi-spawn.
    advance = _take_advance_hint(sid, now)
    if not bool(getattr(_st, "chili_momentum_session_tick_wake_enabled", True)):
        return False
    spacing = float(
        getattr(_st, "chili_momentum_session_tick_wake_min_spacing_s", 2.0) or 2.0
    )
    with _session_wake_lock:
        if now - _session_wake_last.get(sid, 0.0) < spacing:
            return False
        _session_wake_last[sid] = now
        if len(_session_wake_last) > 512:
            stale = [k for k, v in _session_wake_last.items() if now - v > 600.0]
            for k in stale:
                _session_wake_last.pop(k, None)
    with _wake_inflight_lock:
        if sid in _wake_inflight:
            return False
        _wake_inflight.add(sid)
    if advance and not _advance_admit(sid, now):
        # Walang token o puno ang slots: bawiin ang inflight AT ang spacing
        # stamp — hindi dapat maparusahan ang session ng 2s dahil sa budget;
        # ang batch ang safety net at ang susunod na bagong high ay susubok ulit.
        with _wake_inflight_lock:
            _wake_inflight.discard(sid)
        with _session_wake_lock:
            if _session_wake_last.get(sid) == now:
                _session_wake_last.pop(sid, None)
        return False
    threading.Thread(
        target=_wake_runner_tick, args=(sid,), kwargs={"advance": advance},
        name=f"session-wake-{sid}", daemon=True,
    ).start()
    return True


# ---------------------------------------------------------------------------
# Arm -> runner WAKE (2026-08-21). Ang armado ay dating naghihintay sa susunod
# na scheduler batch cycle — sinukat na ~23s arm->unang-tick kahit may FSM
# continuation. Ang benchmark (Ross 08-20 HUIZ): <10s mula signal hanggang
# posisyon. Ang wake ay nagpapatakbo ng AGARANG runner tick para sa kaka-armang
# session sa sariling daemon thread: (a) hindi hinaharangan ang scoring worker,
# (b) ligtas sa sabayan — ang session row ay FOR UPDATE NOWAIT sa dispatcher
# kaya ang karera laban sa scheduler batch ay nagiging benign na
# concurrent_tick skip, (c) single-flight kada session para hindi magtambak.
# Kill switch: chili_momentum_arm_wake_runner_enabled=false.
_wake_inflight: set[int] = set()
_wake_inflight_lock = threading.Lock()

# POST-WAKE FRESHNESS (2026-08-23): pagkatapos ng wake, ang FSM ay maaaring
# nag-ratchet ng stop, nag-transition ng state, o kaka-arm lang ng bagong
# session — ang bridge tracker ay dapat makakita agad, hindi sa susunod na 5s
# refresh. Ang loop instance ang nagre-rehistro ng refresh hook nito sa start().
_post_wake_session_refresh = None


def _wake_runner_tick(session_id: int, advance: bool = False) -> None:
    try:
        from .live_runner import consume_entry_fsm_continuation as _consume
    except Exception:
        _consume = None
    try:
        from .captured_paper_dispatcher import run_live_runner_tick_two_phase
        from ....db import SessionLocal as _SL
        from ....config import settings as _st

        max_steps = int(getattr(_st, "chili_momentum_entry_fsm_continuation_max_steps", 3) or 3)
        for _step in range(max(1, max_steps)):
            try:
                # Two-phase: a staged sealed-lane POST is dispatched after the
                # phase-one commit instead of being dropped on the floor.
                run_live_runner_tick_two_phase(_SL, int(session_id))
            except Exception:
                break
            if _consume is None or not _consume(int(session_id)):
                break
    except Exception:
        _log.debug("[momentum_ws_ignition] arm wake tick failed for session=%s", session_id, exc_info=True)
    finally:
        with _wake_inflight_lock:
            _wake_inflight.discard(int(session_id))
        if advance:
            _advance_done(int(session_id))
        _refresh = _post_wake_session_refresh
        # Ang advance wakes ay maaaring 6/s: ang buong inventory reload kada
        # wake ay puro aksaya — debounce sa ~1s. Ang ibang wake ay hindi ginalaw.
        if _refresh is not None and (
            not advance or _advance_refresh_due(time.monotonic())
        ):
            try:
                _refresh()
            except Exception:
                pass


def wake_armed_sessions(session_ids: Any) -> int:
    """Public: run an IMMEDIATE runner tick for each freshly armed session.

    ARM-WAKE COVERAGE (2026-08-23). The ignition→arm bridge has woken its own
    arms since 08-21, but the FULL scheduler auto-arm pass and the tape-delta
    ignition path never did — an arm from either waited for the next live-runner
    batch (10s nominal, 10–30s measured) before its first WATCHING tick, which
    is exactly the window a Ross-speed break lives in. Same single-flight,
    spacing and kill switch as every other wake; the FSM still decides.
    Returns how many wakes were actually spawned.
    """

    if not session_ids:
        return 0
    if isinstance(session_ids, (int, float)) or not hasattr(session_ids, "__iter__"):
        session_ids = [session_ids]
    woken = 0
    for sid in session_ids:
        try:
            if _spawn_arm_wake(sid):
                woken += 1
        except Exception:
            _log.debug("[momentum_ws_ignition] arm wake spawn failed sid=%s", sid, exc_info=True)
    return woken


def _spawn_arm_wake(session_id: Any) -> bool:
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return False
    from ....config import settings as _st

    if not bool(getattr(_st, "chili_momentum_arm_wake_runner_enabled", True)):
        return False
    # ROLE GATE (2026-08-24). Ang job na nagdadala rito ay naka-register sa
    # ilalim ng `include_heavy`, na KASAMA ang `rnd_only` -- ang role ng R&D
    # scheduler container. Kung walang tsek na ito, ang container na iyon ay
    # magpapatakbo ng buong live FSM tick sa loob ng sarili nitong proseso,
    # kahit ang mismong layunin ng `rnd_only` ay para hindi kailanman i-restart
    # ng R&D deploy ang prosesong may hawak na buhay na posisyon. Tingnan ang
    # `wake_ownership` para sa buong pangangatwiran.
    from .wake_ownership import process_owns_momentum_execution

    if not process_owns_momentum_execution():
        return False
    with _wake_inflight_lock:
        if sid in _wake_inflight:
            return False
        _wake_inflight.add(sid)
    threading.Thread(
        target=_wake_runner_tick, args=(sid,),
        name=f"arm-wake-{sid}", daemon=True,
    ).start()
    return True


class IgnitionScoringLoop:
    """Bridges price-bus ticks to a direct single-symbol viability score."""

    def __init__(self) -> None:
        self._tracker = _UniverseTracker()
        self._sessions = _SessionCrossTracker()
        self._running = False
        self._refresher: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._subscribed: set[str] = set()
        self._last_score: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()
        # LANE OBSERVATION HEARTBEAT (2026-09-02): observations since the last
        # heartbeat write, plus the write clock. See `_write_observation_heartbeat`.
        self._observations = 0
        self._obs_lock = threading.Lock()
        self._last_heartbeat_mono = 0.0

    def _write_observation_heartbeat(self) -> None:
        """Publish 'the lane is still observing' where ANOTHER PROCESS can read it.

        WHY A DB ROW AND NOT A LOG LINE OR AN IN-PROCESS CHECK. On 2026-09-01 the
        host uvicorn app (pid 22376, launched inside a Job Object with
        KILL_ON_JOB_CLOSE) died at 08:03:17 PT. `run_lane_health_check` had run
        normally 5 seconds earlier ("job_id=lane_health_check phase=ok
        duration_ms=265" at 08:03:12) and then died with it — a dead process
        cannot report its own death. Every other detector was Docker-scoped,
        disabled since 2026-07-27, missing from disk, or permanently saturated.
        296 of the day's 390 RTH minutes produced no observation and nothing
        alarmed. The record has to outlive the process that writes it.

        Fail-silent by construction: a heartbeat write must never be able to
        disturb the refresh loop or the price bus.
        """
        with self._obs_lock:
            observations = self._observations
            self._observations = 0
        db = SessionLocal()
        try:
            from ..batch_job_constants import (
                IGNITION_OBSERVATION_HEARTBEAT_SCHEMA,
                JOB_MOMENTUM_IGNITION_OBSERVATION_HEARTBEAT,
            )
            from ..brain_batch_job_log import brain_batch_job_record_completed

            brain_batch_job_record_completed(
                db,
                JOB_MOMENTUM_IGNITION_OBSERVATION_HEARTBEAT,
                ok=True,
                meta={
                    "schema": IGNITION_OBSERVATION_HEARTBEAT_SCHEMA,
                    "observations": int(observations),
                    "universe_size": int(self._tracker.count()),
                    "universe_outcome": str(self._tracker.last_outcome()),
                    "subscribed": int(len(self._subscribed)),
                },
            )
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            _log.warning(
                "[momentum_ws_ignition] observation heartbeat write failed",
                exc_info=True,
            )
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

    def start(self) -> None:
        if not getattr(settings, "chili_momentum_ws_ignition_enabled", False):
            _log.info("[momentum_ws_ignition] disabled (chili_momentum_ws_ignition_enabled=0) — no-op")
            return
        if self._running:
            return
        self._running = True
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ws-ignition")
        self._tracker.refresh()
        self._sessions.refresh()
        global _post_wake_session_refresh
        # Refresh the threshold inventory AND the bus subscriptions: a session
        # armed on a name that is not in the screened universe would otherwise
        # receive no ticks (and therefore no wakes) until the next 5s pass.
        _post_wake_session_refresh = self._refresh_sessions_and_subscriptions
        self._sync_subscriptions()
        self._refresher = threading.Thread(
            target=self._refresh_loop, daemon=True, name="ws-ignition-refresh"
        )
        self._refresher.start()
        _log.info(
            "[momentum_ws_ignition] started — %d universe symbols watched "
            "(floor=%.2f%%, outcome=%s)",
            self._tracker.count(),
            float(getattr(settings, "chili_momentum_ignition_min_pct", 3.0)),
            self._tracker.last_outcome(),
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
                    try:
                        unreg(sym, self._record_universe_tick)
                    except Exception:
                        pass
        except Exception:
            pass
        self._subscribed = set()
        global _post_wake_session_refresh
        _post_wake_session_refresh = None
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
        # Session symbols ride the same subscription: a held/watching name must
        # keep its tick feed even after it falls off the screened universe.
        current = self._tracker.get_symbols() | self._sessions.symbols()
        new = current - self._subscribed
        gone = self._subscribed - current
        _densify = bool(
            getattr(settings, "chili_momentum_universe_tick_record_enabled", True)
        )
        for sym in new:
            try:
                bus.subscribe_symbol(sym)
                bus.register_tick_listener(sym, self._on_tick)
                # UNIVERSE DENSIFICATION (2026-06-15): a SECOND, INDEPENDENT listener
                # persists EVERY universe tick into the NBBO tape so the names the
                # lane never armed (JRSH/CUPR-class) still leave a sub-minute tape
                # for tomorrow's replay. It must be independent of ``_on_tick``,
                # which early-returns below the ignition floor — the recorder has to
                # capture quiet/sub-floor ticks too. Write-only; never blocks scoring.
                if _densify:
                    bus.register_tick_listener(sym, self._record_universe_tick)
            except Exception:
                _log.debug("[momentum_ws_ignition] subscribe failed for %s", sym, exc_info=True)
        unreg = getattr(bus, "unregister_tick_listener", None)
        if callable(unreg):
            for sym in gone:
                try:
                    unreg(sym, self._on_tick)
                except Exception:
                    pass
                try:
                    unreg(sym, self._record_universe_tick)
                except Exception:
                    pass
        # Keep tracking only what is currently subscribed (drop departed names even
        # if the bus has no unregister, so they are re-subscribed if they return).
        self._subscribed = (self._subscribed | new) - gone

    def _refresh_sessions_and_subscriptions(self) -> None:
        """Post-wake freshness: new thresholds AND a tick feed for new names."""
        self._sessions.refresh()
        try:
            self._sync_subscriptions()
        except Exception:
            _log.debug("[momentum_ws_ignition] post-wake subscribe sync failed", exc_info=True)

    def _refresh_loop(self) -> None:
        # Dalawang cadence sa iisang thread: sessions kada ~5s (maliit na query,
        # time-critical ang freshness ng ratcheted stop / bagong armado), universe
        # kada ~20s (mabigat na full-market snapshot — di binago ang ritmo).
        _last_universe = time.monotonic()
        # UNIVERSE TELEMETRY (2026-09-02). The only universe-size line this
        # module ever emitted was the one-shot at start(): over the 11 sessions
        # 2026-08-19..09-02 the lane performed ~23,990 rebuilds and logged 74 of
        # them (0.31%), all at process start. Nothing sampled the watch set
        # mid-session and nothing persisted it, so an empty universe was not
        # distinguishable from a quiet market ANYWHERE. Log on CHANGE only (size
        # delta or a non-ok outcome) so a steady state stays silent instead of
        # adding 4,320 lines/day.
        _last_logged_size: int | None = None
        _last_logged_outcome: str | None = None
        _consecutive_failures = 0
        while self._running:
            time.sleep(_SESSION_REFRESH_S)
            if not self._running:
                break
            try:
                self._sessions.refresh()
                if time.monotonic() - _last_universe >= _UNIVERSE_REFRESH_S:
                    _last_universe = time.monotonic()
                    self._tracker.refresh()
                    _size = self._tracker.count()
                    _outcome = self._tracker.last_outcome()
                    if _size != _last_logged_size or _outcome != _last_logged_outcome:
                        _log.log(
                            logging.INFO if _outcome == _UNIVERSE_OK else logging.WARNING,
                            "[momentum_ws_ignition] universe %d symbols "
                            "(was %s, outcome=%s)",
                            _size,
                            "n/a" if _last_logged_size is None else _last_logged_size,
                            _outcome,
                        )
                        _last_logged_size = _size
                        _last_logged_outcome = _outcome
                self._sync_subscriptions()
                if (
                    time.monotonic() - self._last_heartbeat_mono
                    >= _OBSERVATION_HEARTBEAT_S
                ):
                    self._last_heartbeat_mono = time.monotonic()
                    self._write_observation_heartbeat()
                _consecutive_failures = 0
            except Exception:
                # Was a bare `except Exception: pass`. That is the THIRD silent
                # state: when refresh() raises, the watch set FREEZES at its last
                # value and the lane watches a stale universe forever with no
                # trace at any log level. Distinct from empty, equally unreported.
                _consecutive_failures += 1
                _log.warning(
                    "[momentum_ws_ignition] refresh loop iteration failed "
                    "(consecutive=%d) — watch set is FROZEN at %d symbols",
                    _consecutive_failures,
                    self._tracker.count(),
                    exc_info=True,
                )

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

    def _open_move_pct(self, symbol: str, quote) -> float | None:
        """Since-OPEN move% = (live price − today's open) / open · 100 (#1284).

        Ito ang direksyong hinihingi ng RVOL-axis guard (`_rvol_alone_may_fire`):
        ang gapper na bumabagsak mula sa open ay hindi dapat mag-ignite nang long
        kahit positibo pa ang change vs prev close. Premarket / day-1 listing
        (walang open) ⇒ bumabalik sa day baseline (prev close), gaya ng dati.
        """
        price = self._quote_price(quote)
        if price is None:
            return None
        base = self._tracker.open_baseline_for(symbol)
        if base is None or base <= 0:
            base = self._tracker.baseline_for(symbol)
        if base is None or base <= 0:
            return None
        return (price - base) / base * 100.0

    def _move_pct(self, symbol: str, quote) -> float | None:
        """Day change% = (live price − day baseline) / baseline · 100.

        The day baseline is the tracker's cached PREV CLOSE (else today's open —
        #1284), i.e. the same meaning as the vendor todaysChangePerc that every
        consumer of the stamped change assumes. If a quote already carries an
        explicit pct/change field, prefer it (cheapest); else fall back to the
        baseline math. For the since-open move see ``_open_move_pct``.
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

    def _record_universe_tick(self, symbol: str, quote) -> None:
        """INDEPENDENT densification listener (write-only side path). Persists this
        universe tick to the NBBO tape via the tape recorder's bounded buffer — runs
        REGARDLESS of the ignition floor (so quiet names leave a tape too). Cheap +
        fail-silent: a recorder error must never affect scoring or the bus."""
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
        # TICK-SPEED OPEN/CLOSE: session-threshold crossings wake the runner
        # BEFORE any ignition filtering — a held name below the ignition floor
        # (a collapsing stop) must still dispatch. Cheap for untracked symbols
        # (one dict lookup); any wake runs off-thread.
        try:
            for _sid in self._sessions.crossed(sym, quote):
                _spawn_session_wake(_sid)
        except Exception:
            pass
        move_pct = self._move_pct(sym, quote)
        _vel = self._tracker.velocity_for(sym)
        # VELOCITY INTAKE (2026-08-28): ang day-1 na listing ay maaaring WALANG
        # day baseline (walang day.o/prev.c sa snapshot) — dati ay tahimik na
        # nalalaglag dito kahit velocity-admitted. Kapag may sukat na velocity,
        # TULOY nang move_pct=None — HINDI pineke bilang 0.0 (napatunayan ng
        # review: ang pekeng 0.0 ay nagiging "totoong" ebidensya downstream at
        # nagbe-bench sa below_explosive_floor na fail-open sana sa None). Ang
        # legacy branch sa ibaba ay nananatiling byte-identical (None ⇒ balik).
        if move_pct is None and _vel is None:
            return
        # S1 EVENT FEEDER (docs/DESIGN/MOMENTUM_ENGINE.md §1/§5): when the master flag is
        # ON, use the BASIS-COMPLETE Ross predicate (RVOL OR gap OR move% crosses a Ross
        # floor, within the price band) so a flat-day VOLUME spike (the SKYQ case) ignites
        # too — not just names already up X%. The RVOL axis comes from the tracker's
        # snapshot RVOL (the SAME value fed to the scorer's pillar). Flag OFF ⇒ the
        # original move%-only floor gate (BYTE-IDENTICAL to the deployed path).
        if bool(getattr(settings, "chili_momentum_event_select_primary_enabled", True)):
            from .nbbo_tape import _ross_threshold_crossed

            _rv = self._tracker.rvol_for(sym)
            _px = self._quote_price(quote)
            # #1284: gap = change vs PREV CLOSE (ang stamped datum); move = since
            # OPEN (ang direksyon ng RVOL guard: ang bumabagsak mula sa open ay
            # hindi nag-i-ignite nang long). Dati iisang numero ang dalawa.
            if not _ross_threshold_crossed(
                sym, rvol=_rv, move_pct=self._open_move_pct(sym, quote),
                gap_pct=move_pct, price=_px, velocity_pct=_vel,
            ):
                return  # no Ross axis crossed — dead tape, ignore
            _tick_price = _px
        else:
            if move_pct is None:
                return  # legacy gate: walang baseline ⇒ dating gawi, balik
            floor = float(getattr(settings, "chili_momentum_ignition_min_pct", 3.0))
            if move_pct < floor:
                return  # below the adaptive ignition floor — dead tape, ignore
            _tick_price = self._quote_price(quote)
        # IGNITION WAKE NG NAGBABANTAY (2026-08-30). SINUKAT: ang WATCHING na
        # session ay tumitibok sa p50 11.2s (scheduler batch) — ang ignition ay
        # 3-SEGUNDONG spike, kaya ang mata ay 1-2 tick na huli bago pa man ang
        # gates (candidate→submit p50 ~64s, si Ross ~4s). Ang level-cross wake
        # sa itaas ay para lamang sa may watch_break_level; ang velocity/volume/
        # vwap na trigger classes ay walang gumigising. DITO — pagkatapos
        # pumasa ang MISMONG Ross ignition floor (walang bagong threshold) —
        # gisingin ang bawat WATCHING session ng symbol na ito. Ang
        # _spawn_session_wake ay spacing-bound (2s) + single-flight kada
        # session, kaya ang mainit na tape ay ≤1 wake/2s/session lamang.
        if bool(getattr(
            settings, "chili_momentum_ignition_wake_watching_enabled", True
        )):
            try:
                for _wsid in self._sessions.watching(sym):
                    _spawn_session_wake(_wsid)
            except Exception:
                pass
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
            pool.submit(self._score_symbol, sym, move_pct, _tick_price, _vel)
        except Exception:
            with self._inflight_lock:
                self._inflight.discard(sym)

    # ── scoring (runs on the pool — owns its own DB session) ─────────────────

    def _tracker_open_move(self, symbol: str, price: float | None) -> float | None:
        """Since-OPEN move% from the cached open baseline (None when unknown)."""
        if price is None or float(price) <= 0:
            return None
        base = self._tracker.open_baseline_for(symbol)
        if base is None or float(base) <= 0:
            return None
        return (float(price) - float(base)) / float(base) * 100.0

    def _score_symbol(
        self,
        symbol: str,
        move_pct: float | None,
        price: float | None = None,
        velocity_pct: float | None = None,
    ) -> None:
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
                    "signal_type": "ws_ignition",
                    "source": "ws_ignition",
                }
            }
            # TAPAT NA STAMPING (2026-08-28, review finding): ang HINDI ALAM na
            # day move ay HINDI itina-stamp — dating pineke bilang 0.0 para sa
            # baseline-less na day-1 listing, na nagbe-bench sa
            # below_explosive_floor (0.0 < 10) samantalang ang None ay fail-open
            # ayon sa sariling kontrata ng floor. Kapareho ng price/volume/rvol
            # discipline sa ibaba: ang nawawala ay nananatiling nawawala.
            if move_pct is not None:
                ross_signals[symbol]["todays_change_perc"] = float(move_pct)
            # RUN-UP AXIS, RECORDED ONLY (2026-09-02). `todays_change_perc` is
            # gap + run-up vs the previous close, which is not the axis the lane
            # is trying to buy: over 1,032 equity mover symbol-days it agrees
            # with the reachable low-to-high run within +/-25% only 43.7% of the
            # time (understates by >2.5x on 27.0%). SGLD 2026-09-02 logged a
            # peak move_pct of 293.70 against an 89.6% reachable intraday run
            # off a 525.6% gap. Ranking on the run-up instead would be a TRADING
            # OPINION and is deliberately not made here; this stamps the second
            # axis so the divergence is measurable from the record for the first
            # time. Nothing reads it yet, by design.
            _open_move = self._tracker_open_move(symbol, price)
            if _open_move is not None:
                ross_signals[symbol]["since_open_change_perc"] = float(_open_move)
            # VELOCITY INTAKE: dalhin ang sinukat na short-horizon velocity sa
            # persisted signal — ito ang binabasa ng ross_smallcap_profile_evidence
            # bilang alternatibong "already-moving" na patunay sa arm gate (ang
            # eksaktong 2026-08-19 na pattern: i-stamp ang kailangan ng gate).
            if velocity_pct is not None and float(velocity_pct) > 0:
                ross_signals[symbol]["velocity_pct"] = float(velocity_pct)
            # INSTRUMENT-CLASS AXES (2026-08-19): stamp the tick PRICE and today's
            # SHARES so the persisted row carries what the Ross universe evidence gate
            # needs (it derives dollar_volume = price × volume). Proved live against
            # the running board: every ws_ignition-sourced row failed that gate closed
            # with `ross_universe_missing_price`, so the scoped ignition→arm bridge
            # could never arm a WS-ignited name — and a WS re-score DEGRADED a name
            # whose row had richer snapshot provenance. Both axes fail-open: a missing
            # value is simply not stamped (byte-identical to the old signal shape).
            if price is not None and float(price) > 0:
                ross_signals[symbol]["price"] = float(price)
            _shares = self._tracker.shares_for(symbol)
            if _shares is not None and float(_shares) > 0:
                ross_signals[symbol]["volume"] = float(_shares)
            # FIX A1 (kill-switch chili_momentum_ross_rvol_feed_enabled, default ON):
            # feed the REAL intraday RVOL the tracker captured from the screen snapshot
            # into the scorer's rvol pillar (``vol_ratio`` is the first key _extract_pillars
            # reads). Without this, ws_ignition movers reach the explosive CORE with rvol=None
            # -> core 0.0 -> ross_score 0.0 -> the viability tilt PENALISES every igniting
            # mover toward the floor. OFF => the key is omitted => byte-identical (old
            # None->0.0 path). SELECTION-ONLY: this only shapes the viability rank, never an
            # entry decision.
            if bool(getattr(settings, "chili_momentum_ross_rvol_feed_enabled", True)):
                _rv = self._tracker.rvol_for(symbol)
                if _rv is not None and _rv > 0:
                    ross_signals[symbol]["vol_ratio"] = float(_rv)
            # FIX A2 (2026-08-27, kill-switch chili_momentum_ignition_float_feed_enabled,
            # default ON): feed the REAL share count into the scorer's float pillar.
            # Without this, the ws_ignition signal reaches the A-setup quality floor
            # with float_shares=None -> leg-4 is fail-closed on missing float ->
            # live_eligible=false -- and the ONLY writer that could fill the datum is
            # the 300s equity_viability_refresh batch. Measured 2026-08-27 afternoon:
            # arm lag was BIMODAL (5/17 armed <=7s with a pre-existing full-pillar
            # row; 12/17 at 64s-2.85h, median 399s = the batch wait), at 37 min of
            # ZERO arms post-restart while the float cache was empty. The lookup is
            # process-cached on success and 300s-TTL on None (massive_client #1215),
            # so a new symbol pays ONE ~200ms reference call at first ignition --
            # inside the same envelope as the board work this path already does.
            # Fail-open: on None the key is simply not stamped (byte-identical old
            # shape) and the batch refresh remains the backfill. The leg-4 gate
            # itself is untouched -- this FILLS the datum, never bypasses the check.
            if bool(getattr(settings, "chili_momentum_ignition_float_feed_enabled", True)):
                try:
                    from ...massive_client import get_ticker_float

                    _fl = get_ticker_float(symbol)
                    if _fl is not None and float(_fl) > 0:
                        ross_signals[symbol]["float_shares"] = float(_fl)
                except Exception:
                    _log.debug(
                        "[momentum_ws_ignition] float feed failed for %s",
                        symbol, exc_info=True,
                    )
            # FIX A3 (2026-08-27, kill-switch chili_momentum_accel_ignition_
            # override_enabled): i-stamp ang dollar-volume ACCELERATION sa
            # signal. Sinukat sa 927 labelled ignitions (4 OOS days): ang
            # static rvol>=5.0 floor ay nag-bench ng 319/328 panalo (97.3%) --
            # ang mga panalo ay nag-i-ignite mula sa IBABA ng sariling
            # baseline; ang ACCEL20>=3.0 ay 5.4x ang panalo sa mas mataas na
            # malinis na precision (39.2% vs ~25%). Ross mismo (PPCB video
            # 2026-08-27): "low relative volume RISING FAST" ang unang alert.
            # Isang bounded na 40s query sa parehong bukas na session;
            # fail-open sa anumang kakulangan.
            if bool(getattr(
                settings, "chili_momentum_accel_ignition_override_enabled", True
            )):
                try:
                    from sqlalchemy import text as _a3_text

                    _a3 = db.execute(_a3_text(
                        "SELECT "
                        " COALESCE(SUM(CASE WHEN observed_at >= now() at time zone 'utc' - interval '20 seconds' THEN price*size END),0) AS dv_recent,"
                        " COALESCE(SUM(CASE WHEN observed_at < now() at time zone 'utc' - interval '20 seconds' THEN price*size END),0) AS dv_prev "
                        "FROM iqfeed_trade_ticks "
                        "WHERE symbol = :s "
                        " AND observed_at >= now() at time zone 'utc' - interval '40 seconds'"
                    ), {"s": symbol}).one()
                    _dv_recent = float(_a3.dv_recent or 0.0)
                    _dv_prev = float(_a3.dv_prev or 0.0)
                    ross_signals[symbol]["prev_20s_dv_usd"] = _dv_prev
                    ross_signals[symbol]["accel_20s_dv"] = (
                        _dv_recent / _dv_prev if _dv_prev > 0 else None
                    )
                except Exception:
                    _log.debug(
                        "[momentum_ws_ignition] accel feed failed for %s",
                        symbol, exc_info=True,
                    )
            run_momentum_neural_tick(
                db, meta={"tickers": [symbol], "ross_signals": ross_signals}
            )
            # CAPTURE-G3: this is the FIRST-ALERT moment (the name just crossed a Ross axis and
            # is being scored onto the viability board). PUSH a subscribe hint so the host IQFeed
            # bridge fast-polls it and subscribes NOW, instead of waiting for the viability write
            # + the bridge's ~20s poll (the ~2.7-min Gate-0 blind window on sub-2-min squeezes).
            # Same transaction as the score (committed together); non-fatal on any error.
            try:
                from .bridge_subscribe import request_bridge_subscription

                request_bridge_subscription(db, symbol, reason="ws_ignition")
            except Exception:
                _log.debug("[momentum_ws_ignition] bridge subscribe hint failed for %s", symbol, exc_info=True)
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
        # IGNITION→ARM BRIDGE (2026-08-19 YJ miss): give this just-ignited name a SCOPED
        # arm attempt inside the ignition cadence instead of waiting out a full
        # 300-1200s auto-arm pass. Deliberately runs AFTER the score session is closed
        # and AFTER _inflight is released, on its OWN short-lived session:
        #   - the scorer's session is never handed to the arm pass (which flushes,
        #     commits, expunges and rolls back on its own schedule);
        #   - the symbol is re-scorable while the arm attempt runs, so the name most
        #     likely to be moving is not the one whose scoring is frozen.
        bridge_state = "off"
        if scored_ok:
            bridge_state = self._bridge_arm(symbol)
        # OBSERVATION COUNTER (2026-09-02). This is the exact event the operator
        # depends on and the exact event that stopped at 08:03:17 PT on
        # 2026-09-01 without a single alarm. Counted here, next to the A/B log
        # line, so the counter and the log line can never disagree. Read and
        # zeroed by `_refresh_loop`'s heartbeat write.
        with self._obs_lock:
            self._observations += 1
        # A/B LOG: queryable proof the ignition path put a name on the board — and what
        # the bridge then did with it (armed / no-arm / skipped-busy / off / error), so
        # a silently starved bridge cannot look identical to a healthy one.
        _log.info(
            "[momentum_ws_ignition] symbol=%s move_pct=%s scored_ok=%s bridge=%s",
            symbol,
            ("%.2f" % float(move_pct)) if move_pct is not None else "unknown",
            scored_ok, bridge_state,
        )

    def _bridge_arm(self, symbol: str) -> str:
        """Scoped ignition→arm attempt for a just-scored symbol. Own session,
        rollback-in-finally. Returns a short outcome tag for the A/B log line;
        NEVER raises (a bridge failure must not touch the scoring path)."""
        db = SessionLocal()
        try:
            from .auto_arm import run_scoped_ignition_arm

            out = run_scoped_ignition_arm(db, [symbol])
            db.commit()
            if out is None:
                return "skipped"  # flag off, debounced, or lost the single-flight
            if int(out.get("armed") or 0) > 0:
                # WAKE: agarang runner tick para sa kaka-armang session sa
                # halip na hintayin ang susunod na scheduler batch (~23s ang
                # sinukat na gap). Fire-and-forget; benign ang anumang karera.
                # Ang armed_session_ids ang ginagamit, HINDI ang session_id:
                # ang huli ay last-writer-wins (naa-overwrite ng deduped na
                # kandidato) at hindi kasama ang Alpaca twin — samantalang ang
                # twin mismo ang naglalagay ng order sa paper endpoint.
                _woke = wake_armed_sessions(out.get("armed_session_ids"))
                return "armed+wake" if _woke else "armed"
            return "no_arm:" + str(out.get("skipped") or "unknown")
        except Exception:
            _log.warning(
                "[momentum_ws_ignition] ignition→arm bridge failed for %s",
                symbol, exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass
            return "error"
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()


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
