"""Canonical event-driven LIVE runner loop.

This loop is the sole live-session entry/exit owner when enabled; the legacy
APScheduler batch must be disabled. On every price-bus/IQFeed tick for a tracked
session, ``tick_live_session`` runs at event speed. Sessions resting in
``live_pending_entry`` also get fast ticks so the entry ack-timeout resolves
without a second driver.

Safety model:
  * The breach predicate is only a DISPATCH HINT — the full runner logic
    (trail math, partial exits, broker calls) runs inside tick_live_session,
    so there is no duplicated exit math to drift.
  * tick_live_session remains re-entrancy-safe (SELECT ... FOR UPDATE NOWAIT),
    while startup additionally refuses a batch+event dual-driver configuration.
  * Broker work never runs on the websocket receive thread: breaches are
    dispatched to a small bounded pool with per-session in-flight dedupe and
    minimum spacing. Stop-confirm timers survive an in-flight first read.
"""

from __future__ import annotations

import json
import logging
import math
import re
import select
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import text

from ....config import settings
from ....db import SessionLocal, engine
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
from .lane_health import LIVE_LOOP_HEARTBEAT_INTERVAL_SECONDS

_log = logging.getLogger(__name__)

_KEY_LIVE_EXEC = "momentum_live_execution"
_POSITION_STATES = frozenset(
    {STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT}
)
# Per-session minimum spacing between event ticks. This bounds quote/broker work
# without relying on a second scheduled-session driver.
_EVENT_TICK_MIN_SPACING_S = 2.0
# The stop FSM requires a second observation at least one second after the
# first breach.  This backstop is intentionally independent of ordinary event
# debounce: applying the two-second cadence here can turn a confirmed stop
# into a materially worse fill in a fast small-cap move.
_STOP_CONFIRM_DELAY_S = 1.05
_TRACKER_REFRESH_S = 10.0
# Persist liveness much less often than quote/tick traffic while staying comfortably
# inside the lane-health module's minimum adaptive 60-second grace window.
_LANE_HEALTH_HEARTBEAT_MIN_INTERVAL_S = LIVE_LOOP_HEARTBEAT_INTERVAL_SECONDS
_THREAD_JOIN_TIMEOUT_S = 2.0
_IQFEED_AUTHORITY_BASIS = "iqfeed_q_receive_trade_reference_fenced"
_IQFEED_AUTHORITY_MAX_AGE_S = 2.0
_IQFEED_FUTURE_TOLERANCE_S = 1.0
_IQFEED_DEDUP_RETENTION_S = 5.0
_IQFEED_BUILD_RE = re.compile(
    r"^iqfeed-l1-quote-provenance-v2\+sha256:[0-9a-f]{16}$"
)
_IQFEED_EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,15}$")
_LIVE_LOOP_OWNER_INSTANCE_ID = str(uuid.uuid4())
# Cluster-wide, database-scoped PostgreSQL advisory fence.  Positive int32 values
# spell ``CHIL`` / ``LOOP`` and intentionally use the two-key advisory namespace.
_LIVE_LOOP_FENCE_NAMESPACE = 0x4348494C
_LIVE_LOOP_FENCE_KEY = 0x4C4F4F50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_aware_utc(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed.astimezone(timezone.utc)


def _strict_equity_symbol(value) -> str | None:
    if not isinstance(value, str):
        return None
    symbol = value.strip()
    if (
        symbol != symbol.upper()
        or _IQFEED_EQUITY_SYMBOL_RE.fullmatch(symbol) is None
        or symbol.endswith(".")
        or ".." in symbol
    ):
        return None
    return symbol


class _LiveSessionTracker:
    """Thread-safe registry of runnable LIVE sessions + their exit thresholds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, dict] = {}
        self._owner_generation: int | None = None

    def set_owner_generation(
        self,
        generation: int | None,
        *,
        clear: bool = False,
    ) -> None:
        with self._lock:
            self._owner_generation = (
                None if generation is None else int(generation)
            )
            if clear:
                self._sessions = {}

    def refresh(self, *, expected_generation: int) -> bool:
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
                if self._owner_generation != int(expected_generation):
                    return False
                self._sessions = new_map
            return True
        except Exception as e:
            _log.warning("[live_loop] session refresh failed: %s", e)
            return False
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
        self._generation = 0
        self._lifecycle_lock = threading.RLock()
        self._stop_event: threading.Event | None = None
        self._refresher: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        # Callback ownership is per generation; provider subscriptions persist
        # because PriceBus has no symmetric unsubscribe_symbol API.
        self._subscribed: set[str] = set()
        self._provider_subscribed: set[str] = set()
        self._subscription_lock = threading.Lock()
        self._price_bus = None
        self._last_event_tick: dict[int, float] = {}
        self._last_event_exit_log: dict[int, float] = {}
        self._inflight: set[int] = set()
        # A stop-confirm timer must not disappear merely because the first stop
        # read is still finishing.  Keep one deduplicated follow-up per session;
        # the completing worker drains it after releasing the in-flight slot.
        self._stop_confirm_redispatch: dict[int, int] = {}
        self._inflight_lock = threading.Lock()
        self._worker_context = threading.local()
        # EVENT-DRIVEN ADMISSION (2026-07-09 P1 port): the pg LISTEN consumer thread
        # + fail-closed v2 provenance/generation/dedup watermarks.
        self._notify_thread: threading.Thread | None = None
        self._iqfeed_provenance_lock = threading.Lock()
        self._iqfeed_inflight_certified: set[tuple] = set()
        self._iqfeed_certified_watermarks: dict[tuple, float] = {}
        self._iqfeed_generation_watermarks: dict[str, int] = {}
        self._iqfeed_admission_lock = threading.Lock()
        self._iqfeed_admission_inflight: dict[object, tuple[int, str]] = {}
        # This monotonic state only throttles this owner's durable DB writes. It is
        # never consumed as health truth by the cockpit/API process.
        self._lane_health_heartbeat_lock = threading.Lock()
        self._lane_health_heartbeat_generation: int | None = None
        self._lane_health_heartbeat_written_monotonic: float | None = None
        self._owner_instance_id = _LIVE_LOOP_OWNER_INSTANCE_ID
        self._generation_started_at_utc: datetime | None = None
        # Session-level PostgreSQL advisory locks are released automatically if
        # this dedicated connection dies.  The refresher verifies the lock every
        # cycle, and admission independently rechecks it before adding new risk.
        self._owner_fence_lock = threading.RLock()
        self._owner_fence_connection = None
        self._owner_fence_generation: int | None = None

    def _generation_active(
        self,
        generation: int,
        stop_event: threading.Event | None = None,
    ) -> bool:
        return bool(
            self._running
            and self._generation == int(generation)
            and (stop_event is None or not stop_event.is_set())
        )

    def is_running_owner(self) -> bool:
        """True only while this process has one active, initialized generation."""
        with self._lifecycle_lock:
            refresher = self._refresher
            return bool(
                self._generation_started_at_utc is not None
                and self._generation_active(self._generation, self._stop_event)
                and self._owner_fence_connection is not None
                and self._owner_fence_generation == self._generation
                and refresher is not None
                and refresher.is_alive()
            )

    def _acquire_owner_fence(self) -> bool:
        """Acquire the one cross-process live-loop owner lease."""
        with self._owner_fence_lock:
            if self._owner_fence_connection is not None:
                return self._owner_fence_is_held()
            conn = None
            try:
                conn = engine.connect()
                acquired = bool(
                    conn.execute(
                        text(
                            "SELECT pg_try_advisory_lock(:namespace, :owner_key)"
                        ),
                        {
                            "namespace": _LIVE_LOOP_FENCE_NAMESPACE,
                            "owner_key": _LIVE_LOOP_FENCE_KEY,
                        },
                    ).scalar()
                )
                # The lock is session-level, not transaction-level. End the
                # implicit transaction so this connection is never IIT.
                conn.commit()
                if not acquired:
                    conn.close()
                    return False
                self._owner_fence_connection = conn
                return True
            except Exception:
                _log.critical(
                    "[live_loop] owner advisory-fence acquisition failed",
                    exc_info=True,
                )
                # close() normally returns a backend to SQLAlchemy's pool. If the
                # SELECT acquired the session lock before a later failure, only
                # invalidation guarantees that backend (and ghost lock) dies.
                try:
                    if conn is not None:
                        conn.invalidate()
                except Exception:
                    pass
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                return False

    def _owner_fence_is_held(self) -> bool:
        """Verify that this exact dedicated backend still owns the fence."""
        with self._owner_fence_lock:
            conn = self._owner_fence_connection
            if conn is None:
                return False
            try:
                held = bool(
                    conn.execute(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                  FROM pg_locks
                                 WHERE locktype = 'advisory'
                                   AND pid = pg_backend_pid()
                                   AND database = (SELECT oid FROM pg_database
                                                    WHERE datname = current_database())
                                   AND classid = :namespace
                                   AND objid = :owner_key
                                   AND objsubid = 2
                                   AND granted
                            )
                            """
                        ),
                        {
                            "namespace": _LIVE_LOOP_FENCE_NAMESPACE,
                            "owner_key": _LIVE_LOOP_FENCE_KEY,
                        },
                    ).scalar()
                )
                conn.commit()
                return held
            except Exception:
                _log.critical(
                    "[live_loop] owner advisory-fence verification failed",
                    exc_info=True,
                )
                self._owner_fence_connection = None
                self._owner_fence_generation = None
                try:
                    conn.invalidate()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                return False

    def _release_owner_fence(self) -> None:
        """Release/close the dedicated owner-fence backend exactly once."""
        with self._owner_fence_lock:
            conn = self._owner_fence_connection
            self._owner_fence_connection = None
            self._owner_fence_generation = None
            if conn is None:
                return
            try:
                unlocked = bool(
                    conn.execute(
                        text("SELECT pg_advisory_unlock(:namespace, :owner_key)"),
                        {
                            "namespace": _LIVE_LOOP_FENCE_NAMESPACE,
                            "owner_key": _LIVE_LOOP_FENCE_KEY,
                        },
                    ).scalar()
                )
                conn.commit()
                if not unlocked:
                    _log.warning(
                        "[live_loop] owner advisory fence was already absent on stop"
                    )
            except Exception:
                _log.warning(
                    "[live_loop] owner advisory-fence release failed; invalidating backend",
                    exc_info=True,
                )
                try:
                    conn.invalidate()
                except Exception:
                    pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def admission_owner_ready(self) -> bool:
        """Runtime + cross-process fence truth used before every new-risk pass."""
        with self._lifecycle_lock:
            if not self.is_running_owner():
                return False
            return self._owner_fence_is_held()

    def _record_lane_health_heartbeat(
        self,
        *,
        generation: int,
        force: bool = False,
    ) -> bool:
        """Commit one throttled, generation-owned durable health heartbeat."""
        generation = int(generation)
        # Lock order is lifecycle -> heartbeat everywhere. stop() therefore cannot
        # retire the generation while its completed row is being committed, and it
        # never waits on a refresher that holds these locks in the reverse order.
        with self._lifecycle_lock:
            if not self._generation_active(generation):
                return False
            generation_started_at = self._generation_started_at_utc
            if generation_started_at is None:
                return False
            with self._lane_health_heartbeat_lock:
                now_mono = time.monotonic()
                if (
                    not force
                    and self._lane_health_heartbeat_generation == generation
                    and self._lane_health_heartbeat_written_monotonic is not None
                    and now_mono - self._lane_health_heartbeat_written_monotonic
                    < _LANE_HEALTH_HEARTBEAT_MIN_INTERVAL_S
                ):
                    return True

                db = None
                try:
                    from .lane_health import record_live_runner_loop_run

                    db = SessionLocal()
                    record_live_runner_loop_run(
                        db,
                        owner_instance_id=self._owner_instance_id,
                        generation=generation,
                        generation_started_at=generation_started_at,
                    )
                    db.commit()
                    self._lane_health_heartbeat_generation = generation
                    self._lane_health_heartbeat_written_monotonic = time.monotonic()
                    return True
                except Exception:
                    try:
                        if db is not None:
                            db.rollback()
                    except Exception:
                        pass
                    _log.warning(
                        "[live_loop] durable lane-health heartbeat failed",
                        exc_info=True,
                    )
                    return False
                finally:
                    try:
                        if db is not None:
                            db.rollback()
                    except Exception:
                        pass
                    try:
                        if db is not None:
                            db.close()
                    except Exception:
                        pass

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._running:
                return False
            # ``ThreadPoolExecutor.shutdown(wait=False)`` cannot cancel a worker
            # already inside a broker/DB tick.  Keep restart fail-closed until that
            # old generation has quiesced; otherwise two generations could own live
            # work during a fast in-process scheduler restart.
            with self._inflight_lock:
                if self._inflight:
                    _log.critical(
                        "[live_loop] refusing restart while prior ticks are still "
                        "in flight: %s",
                        sorted(self._inflight),
                    )
                    return False
            with self._iqfeed_admission_lock:
                if self._iqfeed_admission_inflight:
                    _log.critical(
                        "[live_loop] refusing restart while prior IQFeed admissions "
                        "are still in flight: %s",
                        sorted(self._iqfeed_admission_inflight.values()),
                    )
                    return False
            if not self._acquire_owner_fence():
                _log.critical(
                    "[live_loop] refusing start: another process owns the live-loop fence"
                )
                return False
            self._generation += 1
            generation = self._generation
            self._owner_fence_generation = generation
            self._generation_started_at_utc = _utcnow()
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._running = True
            self._pool = ThreadPoolExecutor(
                max_workers=3, thread_name_prefix="live-loop-tick"
            )
            try:
                self._tracker.set_owner_generation(generation, clear=True)
                if self._tracker.refresh(expected_generation=generation) is not True:
                    raise RuntimeError("initial tracker refresh lost owner generation")
                if self._subscribe_active_symbols(generation=generation) is not True:
                    raise RuntimeError("initial price-bus subscription failed")
                if not self._generation_active(generation, stop_event):
                    raise RuntimeError("live-loop generation retired during startup")
                self._refresher = threading.Thread(
                    target=self._refresh_loop,
                    args=(generation, stop_event),
                    daemon=True,
                    name=f"live-runner-loop-refresh-g{generation}",
                )
                self._refresher.start()
                self._start_iqfeed_notify_listener(generation, stop_event)
                if not self._generation_active(generation, stop_event):
                    raise RuntimeError("live-loop generation retired during startup")
                if not self._owner_fence_is_held():
                    raise RuntimeError("live-loop owner fence lost during startup")
                if not self._record_lane_health_heartbeat(
                    generation=generation,
                    force=True,
                ):
                    raise RuntimeError(
                        "initial durable lane-health heartbeat failed"
                    )
            except Exception:
                # Leave no half-started owner. `stop` is re-entrant under the
                # lifecycle RLock and performs callback/pool/thread cleanup.
                self.stop()
                raise
            _log.info(
                "[live_loop] started generation=%d — event-driven exits armed "
                "(%d live sessions tracked) iqfeed_notify=%s",
                generation,
                self._tracker.count(),
                bool(
                    getattr(
                        settings,
                        "chili_momentum_live_runner_loop_iqfeed_notify_enabled",
                        True,
                    )
                ),
            )
            return True

    def stop(self) -> bool:
        # Serialize the entire teardown against a quick restart. Otherwise the old
        # stop could unregister the callback just installed by the new generation.
        with self._lifecycle_lock:
            had_owner = bool(
                self._running
                or self._refresher is not None
                or self._notify_thread is not None
                or self._pool is not None
                or self._subscribed
                or self._owner_fence_connection is not None
            )
            self._running = False
            self._generation += 1
            self._generation_started_at_utc = None
            self._tracker.set_owner_generation(None, clear=True)
            stop_event = self._stop_event
            if stop_event is not None:
                stop_event.set()
            refresher = self._refresher
            notify_thread = self._notify_thread
            pool = self._pool
            self._refresher = None
            self._notify_thread = None
            self._pool = None
            self._stop_event = None
            with self._inflight_lock:
                self._stop_confirm_redispatch.clear()

            self._unsubscribe_all_symbols()
            if pool is not None:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:  # pragma: no cover - older Python compatibility
                    pool.shutdown(wait=False)

            current = threading.current_thread()
            for thread in (refresher, notify_thread):
                if thread is None or thread is current:
                    continue
                thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
                if thread.is_alive():
                    _log.warning(
                        "[live_loop] bounded stop left stale thread to self-retire: %s",
                        thread.name,
                    )
            self._release_owner_fence()
            if had_owner:
                _log.info("[live_loop] stopped")
            return had_owner

    def _subscribe_active_symbols(self, *, generation: int | None = None) -> bool:
        try:
            from ..price_bus import get_price_bus

            bus = get_price_bus()
        except Exception:
            _log.warning("[live_loop] price bus unavailable during refresh", exc_info=True)
            return False
        with self._subscription_lock:
            if generation is not None and not self._generation_active(generation):
                return False
            if self._price_bus is not None and self._price_bus is not bus:
                old_unreg = getattr(self._price_bus, "unregister_tick_listener", None)
                if callable(old_unreg):
                    for sym in tuple(self._subscribed):
                        old_unreg(sym, self._on_tick)
                self._subscribed.clear()
                self._provider_subscribed.clear()
            self._price_bus = bus
            current = self._tracker.get_all_symbols()
            unreg = getattr(bus, "unregister_tick_listener", None)
            if callable(unreg):
                for sym in self._subscribed - current:
                    unreg(sym, self._on_tick)
            self._subscribed.intersection_update(current)
            for sym in current - self._subscribed:
                if sym not in self._provider_subscribed:
                    bus.subscribe_symbol(sym)
                    self._provider_subscribed.add(sym)
                bus.register_tick_listener(sym, self._on_tick)
                self._subscribed.add(sym)
            return bool(
                generation is None or self._generation_active(generation)
            )

    def _unsubscribe_all_symbols(self) -> None:
        with self._subscription_lock:
            bus = self._price_bus
            unreg = (
                getattr(bus, "unregister_tick_listener", None)
                if bus is not None
                else None
            )
            if callable(unreg):
                for sym in tuple(self._subscribed):
                    try:
                        unreg(sym, self._on_tick)
                    except Exception:
                        _log.debug(
                            "[live_loop] price-bus callback cleanup failed symbol=%s",
                            sym,
                            exc_info=True,
                        )
            self._subscribed.clear()

    def _refresh_loop(
        self,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        while self._generation_active(generation, stop_event):
            if stop_event.wait(_TRACKER_REFRESH_S):
                break
            if not self._generation_active(generation, stop_event):
                break
            if (
                self._generation_started_at_utc is not None
                and (
                    self._owner_fence_generation != generation
                    or not self._owner_fence_is_held()
                )
            ):
                _log.critical(
                    "[live_loop] owner fence lost; retiring generation=%d",
                    generation,
                )
                self.stop()
                break
            try:
                if self._tracker.refresh(expected_generation=generation) is not True:
                    continue
                if self._subscribe_active_symbols(generation=generation) is not True:
                    continue
                if self._generation_active(generation, stop_event):
                    self._record_lane_health_heartbeat(generation=generation)
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
                # EVENT-DRIVEN EXHAUSTION-EXIT HINT (2026-06-16, Ross "eject the moment
                # the ask thickens"): a held CRYPTO trailing position whose order flow
                # rolls over to the sell side wakes the runner NOW (up to 15s sooner than
                # the poll). DISPATCH HINT ONLY — tick_live_session re-checks the full
                # INVARIANT-A-safe confluence and is the sole decider of any sell.
                elif state == STATE_LIVE_TRAILING and symbol.endswith("-USD"):
                    self._maybe_event_exit_hint(s, symbol)
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

    def _maybe_event_exit_hint(self, s: dict, symbol: str) -> None:
        """Tick-level OFI-rollover EXIT dispatch hint for a held crypto position. A
        cheap PURE in-process ring read (``db=None`` → ring-only, NO DB/broker on the
        websocket receive thread). When the order flow rolls over to the sell side
        (exhaustion of the up-move — Ross's "buying paused, a seller on the ask"), wake
        the runner NOW so its FULL INVARIANT-A-safe exit confluence (peak_r>=arm_r,
        micro rollover, OFI flip, giveback, continuation-veto in tick_live_session)
        evaluates up to 15s sooner than the poll. DISPATCH HINT ONLY — never a sell, no
        exit math here. Default OFF + observe-first: when not enabled, LOG the
        would-dispatch counterfactual (rate-limited) so the operator can validate that
        the hint fires sanely before flipping it live. Fail-open."""
        enabled = bool(getattr(settings, "chili_momentum_exit_event_driven_enabled", False))
        observe = bool(getattr(settings, "chili_momentum_exit_event_driven_observe", True))
        if not (enabled or observe):
            return
        try:
            from .pipeline import _live_ofi_microprice

            ofi, _micro = _live_ofi_microprice(symbol, db=None)  # ring-only, no DB
        except Exception:
            return
        if ofi is None:
            return
        thr = float(getattr(settings, "chili_momentum_exit_event_ofi_rollover_thr", -0.25) or -0.25)
        if ofi >= thr:
            return  # no sell-side exhaustion rollover
        if enabled:
            self._dispatch(s["session_id"])  # wake the runner; IT decides the sell
            return
        # observe-first: record the counterfactual without acting (rate-limited).
        now = time.monotonic()
        sid = s["session_id"]
        if now - self._last_event_exit_log.get(sid, 0.0) >= _EVENT_TICK_MIN_SPACING_S:
            self._last_event_exit_log[sid] = now
            _log.info(
                "[live_runner_loop] event_exit_hint observe-only (NOT dispatched): %s "
                "ofi=%.3f < thr=%.3f sess=%s — would wake the exit runner",
                symbol, ofi, thr, sid,
            )

    def _dispatch(
        self,
        session_id: int,
        *,
        guarantee_after_inflight: bool = False,
        expected_generation: int | None = None,
    ) -> bool:
        generation = (
            self._generation
            if expected_generation is None
            else int(expected_generation)
        )
        if not self._generation_active(generation):
            return False
        now = time.monotonic()
        with self._inflight_lock:
            if not self._generation_active(generation):
                return False
            if session_id in self._inflight:
                if guarantee_after_inflight:
                    self._stop_confirm_redispatch[session_id] = generation
                return False
            last = self._last_event_tick.get(session_id, 0.0)
            if (
                not guarantee_after_inflight
                and now - last < _EVENT_TICK_MIN_SPACING_S
            ):
                return False
            self._inflight.add(session_id)
            self._last_event_tick[session_id] = now
        pool = self._pool
        if pool is None:
            self._complete_dispatch(session_id, generation=generation)
            return False
        try:
            future = pool.submit(self._run_tick_task, session_id, generation)
            add_done_callback = getattr(future, "add_done_callback", None)
            if callable(add_done_callback):
                add_done_callback(
                    lambda done, sid=session_id, gen=generation: (
                        self._complete_dispatch(sid, generation=gen)
                        if done.cancelled()
                        else None
                    )
                )
            return True
        except Exception:
            self._complete_dispatch(session_id, generation=generation)
            return False

    def _run_tick_task(self, session_id: int, generation: int) -> None:
        self._worker_context.generation = generation
        try:
            if self._generation_active(generation):
                self._tick_session(session_id)
        finally:
            try:
                del self._worker_context.generation
            except AttributeError:
                pass
            self._complete_dispatch(session_id, generation=generation)

    def _complete_dispatch(
        self,
        session_id: int,
        *,
        generation: int | None = None,
    ) -> None:
        """Release one worker slot and drain a timer-guaranteed follow-up.

        The follow-up is submitted only after the current DB session has been
        finalized.  Passing ``guarantee_after_inflight`` again preserves the
        request if another event wins the tiny release-to-submit race.
        """
        redispatch_generation: int | None = None
        with self._inflight_lock:
            self._inflight.discard(session_id)
            pending_generation = self._stop_confirm_redispatch.pop(
                session_id, None
            )
            if pending_generation is not None and self._generation_active(
                pending_generation
            ):
                redispatch_generation = pending_generation
        if redispatch_generation is not None:
            self._dispatch(
                session_id,
                guarantee_after_inflight=True,
                expected_generation=redispatch_generation,
            )

    def schedule_stop_confirmation(self, session_id: int) -> bool:
        """Guarantee the second stop read even if no new websocket tick arrives.

        The stop FSM still owns the decision and re-reads the quote; this timer is
        only a dispatch backstop for the one-second flicker guard.
        """
        generation = getattr(
            self._worker_context,
            "generation",
            self._generation,
        )
        if not self._generation_active(generation):
            return False
        timer = threading.Timer(
            _STOP_CONFIRM_DELAY_S,
            lambda: self._dispatch(
                int(session_id),
                guarantee_after_inflight=True,
                expected_generation=generation,
            ),
        )
        timer.daemon = True
        timer.name = f"live-stop-confirm-{int(session_id)}"
        timer.start()
        return True

    # ── EVENT-DRIVEN ADMISSION (2026-07-09 P1 port from the concurrency WIP) ─────
    # The <1s tick->admit->arm path: the HOST IQFeed bridge pg_notify's every L1 tick
    # on channel momentum_iqfeed_l1 (producer live for days); this consumer LISTENs,
    # dispatches ticks to EXISTING sessions instantly, and — when a symbol has NO
    # session — runs the guarded event admission (ross_event_admission.admit_ross_event
    # -> the SAME begin_live_arm/confirm_live_arm flow as auto-arm, double-arm-proofed
    # by the pg_advisory_xact_lock in operator_actions). The 10s scheduler auto-arm
    # may remain an admission-only backstop (pg_notify is fire-and-forget); it never
    # advances sessions beside this loop. Quantified misses this
    # class fixes: JEM +$46k (hours late), CETX +$8.9k (~20min late), SILO (46s move,
    # 95s late). Reconnect-on-error loop; fail-open everywhere.

    def _start_iqfeed_notify_listener(
        self,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        if not bool(getattr(settings, "chili_momentum_live_runner_loop_iqfeed_notify_enabled", True)):
            return
        expected_build = str(
            getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "")
            or ""
        ).strip()
        if _IQFEED_BUILD_RE.fullmatch(expected_build) is None:
            _log.critical(
                "[live_loop] IQFeed notify admission disabled: no exact reviewed v2 "
                "bridge build is pinned"
            )
            return
        self._notify_thread = threading.Thread(
            target=self._iqfeed_notify_loop,
            args=(generation, stop_event),
            daemon=True,
            name=f"live-runner-iqfeed-listen-g{generation}",
        )
        self._notify_thread.start()

    def _iqfeed_notify_loop(
        self,
        generation: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        generation = self._generation if generation is None else int(generation)
        stop_event = stop_event or threading.Event()
        expected_build = str(
            getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "")
            or ""
        ).strip()
        if _IQFEED_BUILD_RE.fullmatch(expected_build) is None:
            return
        try:
            import psycopg2
        except Exception as exc:
            _log.warning("[live_loop] IQFeed notify disabled; psycopg2 unavailable: %s", exc)
            return

        channel = str(
            getattr(
                settings,
                "chili_momentum_live_runner_loop_iqfeed_notify_channel",
                "momentum_iqfeed_l1",
            )
            or ""
        ).strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", channel) is None:
            _log.critical(
                "[live_loop] refusing IQFeed LISTEN with invalid channel=%r",
                channel,
            )
            return
        db_url = str(getattr(settings, "database_url", "") or "")
        while self._generation_active(generation, stop_event):
            conn = None
            try:
                conn = psycopg2.connect(db_url)
                conn.set_session(autocommit=True)
                cur = conn.cursor()
                cur.execute(f"LISTEN {channel};")
                if not self._generation_active(generation, stop_event):
                    break
                _log.info("[live_loop] listening for IQFeed events channel=%s", channel)
                while self._generation_active(generation, stop_event):
                    ready, _, _ = select.select([conn], [], [], 1.0)
                    if not self._generation_active(generation, stop_event):
                        break
                    if not ready:
                        continue
                    conn.poll()
                    while conn.notifies:
                        notify = conn.notifies.pop(0)
                        if not self._generation_active(generation, stop_event):
                            break
                        self._handle_iqfeed_notify_payload(
                            notify.payload,
                            generation=generation,
                        )
            except Exception as exc:
                if self._generation_active(generation, stop_event):
                    _log.warning("[live_loop] IQFeed notify listener reconnecting after error: %s", exc)
                    stop_event.wait(1.0)
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass

    def _validated_iqfeed_notify(self, payload: str) -> tuple[dict, tuple] | None:
        """Validate the complete v2 authority tuple before any admission work."""

        def _object_without_duplicate_keys(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError(f"duplicate JSON key: {key}")
                obj[key] = value
            return obj

        try:
            data = json.loads(
                payload,
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None

        symbol = _strict_equity_symbol(data.get("symbol"))
        expected_build = str(
            getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "")
            or ""
        ).strip()
        bridge_build = data.get("bridge_version")
        bridge_run_id = data.get("bridge_run_id")
        generation = data.get("connection_generation")
        if (
            symbol is None
            or data.get("source") != "iqfeed_l1"
            or data.get("message_type") != "Q"
            or data.get("timestamp_basis") != _IQFEED_AUTHORITY_BASIS
            or _IQFEED_BUILD_RE.fullmatch(expected_build) is None
            or bridge_build != expected_build
            or data.get("provider_event_at", object()) is not None
            or not isinstance(bridge_run_id, str)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            return None
        try:
            if str(uuid.UUID(bridge_run_id)) != bridge_run_id:
                return None
        except ValueError:
            return None

        received_at = _parse_aware_utc(data.get("received_at"))
        reference_at = _parse_aware_utc(
            data.get("provider_trade_reference_at")
        )
        observed_at = _parse_aware_utc(data.get("observed_at"))
        if received_at is None or reference_at is None or observed_at is None:
            return None
        if observed_at != reference_at:
            return None
        receive_reference_delta = (received_at - reference_at).total_seconds()
        if not (
            -_IQFEED_FUTURE_TOLERANCE_S
            <= receive_reference_delta
            <= _IQFEED_AUTHORITY_MAX_AGE_S
        ):
            return None
        now_utc = _utcnow()
        received_age = (now_utc - received_at).total_seconds()
        reference_age = (now_utc - reference_at).total_seconds()
        if (
            received_age < -_IQFEED_FUTURE_TOLERANCE_S
            or reference_age < -_IQFEED_FUTURE_TOLERANCE_S
            or received_age > _IQFEED_AUTHORITY_MAX_AGE_S
            or reference_age > _IQFEED_AUTHORITY_MAX_AGE_S
        ):
            return None

        bid_value = data.get("bid")
        ask_value = data.get("ask")
        if (
            isinstance(bid_value, bool)
            or isinstance(ask_value, bool)
            or not isinstance(bid_value, (int, float))
            or not isinstance(ask_value, (int, float))
        ):
            return None
        bid = float(bid_value)
        ask = float(ask_value)
        if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
            return None

        certified_tuple = (
            symbol,
            "iqfeed_l1",
            "Q",
            _IQFEED_AUTHORITY_BASIS,
            bridge_build,
            bridge_run_id,
            generation,
            reference_at.isoformat(),
            received_at.isoformat(),
            bid,
            ask,
        )
        now_monotonic = time.monotonic()
        with self._iqfeed_provenance_lock:
            self._iqfeed_certified_watermarks = {
                key: expires_at
                for key, expires_at in self._iqfeed_certified_watermarks.items()
                if expires_at > now_monotonic
            }
            committed_generation = self._iqfeed_generation_watermarks.get(
                bridge_run_id
            )
            if (
                committed_generation is not None
                and generation < committed_generation
            ):
                return None
            inflight_generations = (
                key[6]
                for key in self._iqfeed_inflight_certified
                if key[5] == bridge_run_id
            )
            highest_inflight_generation = max(inflight_generations, default=None)
            if (
                highest_inflight_generation is not None
                and generation < highest_inflight_generation
            ):
                return None
            if (
                certified_tuple in self._iqfeed_certified_watermarks
                or certified_tuple in self._iqfeed_inflight_certified
            ):
                return None
            # A short-lived concurrency reservation is not an accepted-event
            # watermark. It is removed on every failed admission/dispatch path.
            self._iqfeed_inflight_certified.add(certified_tuple)
        normalized = dict(data)
        normalized.update(
            {
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "received_at": received_at.isoformat(),
                "observed_at": reference_at.isoformat(),
                "provider_trade_reference_at": reference_at.isoformat(),
            }
        )
        return normalized, certified_tuple

    def _handle_iqfeed_notify_payload(
        self,
        payload: str,
        *,
        generation: int | None = None,
    ) -> bool:
        owner_generation = (
            self._generation if generation is None else int(generation)
        )
        if not self._generation_active(owner_generation):
            return False
        validated = self._validated_iqfeed_notify(payload)
        if validated is None:
            return False
        data, certified_tuple = validated
        sym = data["symbol"]
        handled = False
        try:
            sessions = self._tracker.get_sessions_for_symbol(sym)
            if not sessions:
                admission = self._admit_iqfeed_symbol(
                    sym,
                    data,
                    expected_generation=owner_generation,
                )
                handled = bool(admission and admission.get("admitted"))
                sessions = self._tracker.get_sessions_for_symbol(sym)
            for sess in sessions:
                handled = self._dispatch(
                    int(sess["session_id"]),
                    expected_generation=owner_generation,
                ) or handled
        except Exception:
            _log.debug(
                "[live_loop] certified IQFeed notify handling failed symbol=%s",
                sym,
                exc_info=True,
            )
            handled = False
        finally:
            if handled and not self._generation_active(owner_generation):
                handled = False
            bridge_run_id = certified_tuple[5]
            connection_generation = certified_tuple[6]
            with self._iqfeed_provenance_lock:
                self._iqfeed_inflight_certified.discard(certified_tuple)
                if handled:
                    prior_generation = self._iqfeed_generation_watermarks.get(
                        bridge_run_id, 0
                    )
                    self._iqfeed_generation_watermarks[bridge_run_id] = max(
                        prior_generation,
                        connection_generation,
                    )
                    self._iqfeed_certified_watermarks[certified_tuple] = (
                        time.monotonic() + _IQFEED_DEDUP_RETENTION_S
                    )
        return handled

    def _begin_iqfeed_admission(
        self,
        generation: int,
        symbol: str,
    ) -> object | None:
        with self._lifecycle_lock:
            if not self._generation_active(generation):
                return None
            token = object()
            with self._iqfeed_admission_lock:
                self._iqfeed_admission_inflight[token] = (
                    int(generation),
                    str(symbol),
                )
            return token

    def _finish_iqfeed_admission(self, token: object) -> None:
        with self._iqfeed_admission_lock:
            self._iqfeed_admission_inflight.pop(token, None)

    def _admit_iqfeed_symbol(
        self,
        symbol: str,
        payload: dict,
        *,
        expected_generation: int,
    ) -> dict | None:
        admission_token = self._begin_iqfeed_admission(
            expected_generation,
            symbol,
        )
        if admission_token is None:
            return None
        db = None
        try:
            from .ross_event_admission import admit_ross_event

            db = SessionLocal()
            result = admit_ross_event(
                db,
                symbol=symbol,
                signal=None,
                source=str(payload.get("source") or "iqfeed_notify"),
                # IQFeed is the event source that should CREATE the fresh Ross
                # candidate when no viability row exists yet. Leaving this false
                # made the event path circular: watchlist -> tick -> "no candidate"
                # -> never arm.
                refresh_viability=True,
                # Admission may create/dedupe a session, but it must not run the
                # session synchronously inside this uncommitted generation.  Once
                # the lifecycle-fenced commit below succeeds, the notify handler's
                # existing _dispatch path owns the one runner tick.
                defer_live_ticks_until_commit=True,
            )
            # Serialize the final generation check and commit against stop(). A
            # stop that won the lifecycle lock forces rollback; a commit that won
            # first is owned by the still-active generation.
            with self._lifecycle_lock:
                if not self._generation_active(expected_generation):
                    db.rollback()
                    return None
                db.commit()
            if result.get("admitted") or result.get("skipped") not in (
                "no_fresh_live_eligible_candidate",
                "cooldown",
                "ross_transcript_context_rejected",
            ):
                _log.info(
                    "[live_loop] iqfeed admission symbol=%s admitted=%s skipped=%s",
                    symbol,
                    bool(result.get("admitted")),
                    result.get("skipped"),
                )
            publish_session = bool(
                result.get("admitted")
                or (
                    result.get("skipped") == "already_active"
                    and int(result.get("session_id") or 0) > 0
                )
            )
            if publish_session:
                # Either stop() wins this lock and the old generation skips the
                # refresh entirely, or the active admission owns publication to
                # completion before stop can invalidate it.
                with self._lifecycle_lock:
                    if self._generation_active(expected_generation):
                        self._tracker.refresh(
                            expected_generation=expected_generation,
                        )
            return result
        except Exception as exc:
            _log.debug("[live_loop] iqfeed admission failed symbol=%s: %s", symbol, exc)
            try:
                if db is not None:
                    db.rollback()
            except Exception:
                pass
            return None
        finally:
            try:
                if db is not None:
                    db.rollback()
            except Exception:
                pass
            if db is not None:
                db.close()
            self._finish_iqfeed_admission(admission_token)

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


def start_live_runner_loop() -> bool:
    """Start only when this loop is the one configured live-session owner."""
    if not settings.chili_autopilot_price_bus_enabled:
        return False
    if not getattr(settings, "chili_momentum_live_runner_enabled", False):
        return False
    if not bool(
        getattr(settings, "chili_momentum_live_runner_loop_enabled", False)
    ):
        return False
    if bool(
        getattr(settings, "chili_momentum_live_runner_scheduler_enabled", False)
    ):
        _log.critical(
            "[live_loop] refusing start: legacy batch and event-loop drivers "
            "are both enabled"
        )
        return False
    return get_live_runner_loop().start()


def stop_live_runner_loop() -> bool:
    if _loop is None:
        return False
    return _loop.stop()


def is_live_runner_loop_running() -> bool:
    """Process-local ownership truth used only to gate same-process admission."""
    return bool(_loop is not None and _loop.is_running_owner())


def is_live_runner_loop_admission_ready() -> bool:
    """True only when this process still owns the cluster-wide live-loop fence."""
    return bool(_loop is not None and _loop.admission_owner_ready())


def schedule_live_runner_stop_confirmation(session_id: int) -> bool:
    """Schedule a bounded stop-confirm dispatch only when the live loop is running."""
    if _loop is None:
        return False
    return _loop.schedule_stop_confirmation(int(session_id))
