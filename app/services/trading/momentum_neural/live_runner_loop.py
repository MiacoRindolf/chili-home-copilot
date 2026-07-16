"""Event-driven live momentum runner loop.

The APScheduler live batch is a fallback. Ross-style equity scalps need active
live sessions ticked by market-data events, including IQFeed L1 tape rows.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import select
import threading
import time
from typing import Any

import sqlalchemy as sa

from ....config import settings
from ....db import SessionLocal
from ....models.trading import TradingAutomationSession
from ..execution_family_registry import normalize_execution_family
from .live_fsm import (
    LIVE_RUNNER_RUNNABLE_STATES,
    LIVE_RUNNER_TERMINAL_STATES,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_PENDING_ENTRY,
)
from .persistence import append_trading_automation_event
from .session_lifecycle import is_operator_paused

_log = logging.getLogger(__name__)


class _LiveSessionTracker:
    """Thread-safe registry of active live sessions by symbol."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, dict[str, Any]] = {}

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
            new_map: dict[int, dict[str, Any]] = {}
            for sess in rows:
                if is_operator_paused(sess.risk_snapshot_json):
                    continue
                new_map[int(sess.id)] = {
                    "session_id": int(sess.id),
                    "symbol": str(sess.symbol or "").upper(),
                    "state": sess.state,
                    "execution_family": sess.execution_family,
                }
            with self._lock:
                self._sessions = new_map
        except Exception as exc:
            _log.warning("[live_runner_loop] session refresh failed: %s", exc)
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

    def get_sessions_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        sym = str(symbol or "").upper()
        with self._lock:
            return [s for s in self._sessions.values() if s.get("symbol") == sym]

    def get_all_session_ids(self) -> list[int]:
        with self._lock:
            return list(self._sessions.keys())

    def get_all_symbols(self) -> set[str]:
        with self._lock:
            return {str(s["symbol"]).upper() for s in self._sessions.values() if s.get("symbol")}

    def get_all_execution_families(self) -> set[str]:
        with self._lock:
            return {str(s["execution_family"] or "").strip().lower() for s in self._sessions.values() if s.get("execution_family")}


class LiveRunnerLoop:
    """Bridge price-bus/IQFeed ticks to ``tick_live_session``."""

    def __init__(self) -> None:
        self._tracker = _LiveSessionTracker()
        self._running = False
        self._thread: threading.Thread | None = None
        self._notify_thread: threading.Thread | None = None
        self._subscribed_symbols: set[str] = set()
        self._last_refresh = 0.0
        self._last_tick_monotonic: dict[int, float] = {}
        self._last_iqfeed_observed_at: dict[str, Any] = {}
        self._inflight: set[int] = set()
        self._lock = threading.Lock()
        workers = int(getattr(settings, "chili_momentum_live_runner_batch_workers", 0) or 0)
        if workers <= 0:
            workers = int(getattr(settings, "chili_momentum_risk_max_concurrent_live_sessions", 5) or 5)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(workers, 20)),
            thread_name_prefix="live-runner-tick",
        )

    @property
    def _refresh_interval(self) -> float:
        return max(0.25, float(getattr(settings, "chili_momentum_live_runner_loop_refresh_seconds", 2.0) or 2.0))

    @property
    def _min_tick_interval(self) -> float:
        ms = int(getattr(settings, "chili_momentum_live_runner_loop_min_tick_interval_ms", 250) or 250)
        return max(0.05, float(ms) / 1000.0)

    @property
    def _heartbeat_interval(self) -> float:
        return max(0.25, float(getattr(settings, "chili_momentum_live_runner_loop_heartbeat_seconds", 2.0) or 2.0))

    @property
    def _iqfeed_poll_interval(self) -> float:
        return max(0.05, float(getattr(settings, "chili_momentum_live_runner_loop_iqfeed_poll_seconds", 0.25) or 0.25))

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tracker.refresh()
        self._last_refresh = time.monotonic()
        self._warm_live_execution_adapters()
        self._subscribe_active_symbols()
        self._start_iqfeed_notify_listener()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="live-runner-loop")
        self._thread.start()
        _log.info(
            "[live_runner_loop] started sessions=%d iqfeed_tape=%s iqfeed_notify=%s min_tick_ms=%s",
            len(self._tracker.get_all_session_ids()),
            bool(getattr(settings, "chili_momentum_live_runner_loop_iqfeed_tape_enabled", True)),
            bool(getattr(settings, "chili_momentum_live_runner_loop_iqfeed_notify_enabled", True)),
            int(self._min_tick_interval * 1000),
        )

    def _warm_live_execution_adapters(self) -> None:
        """Warm active live venue adapters before the first event-driven tick.

        Ross scalps can resolve in seconds. The first actionable tick should not
        spend that window discovering broker tools or verifying execution auth.
        """
        families = sorted(self._tracker.get_all_execution_families())
        for execution_family in families:
            if not execution_family:
                continue
            try:
                from ..execution_family_registry import resolve_live_spot_adapter_factory

                adapter = resolve_live_spot_adapter_factory(execution_family)()
                enabled = bool(adapter.is_enabled())
                reason = None
                if not enabled:
                    reason = str(getattr(adapter, "_execution_auth_error", "") or "") or "adapter_is_enabled_false"
                _log.info(
                    "[live_runner_loop] adapter_warmup execution_family=%s enabled=%s reason=%s",
                    execution_family,
                    enabled,
                    reason,
                )
            except Exception as exc:
                _log.warning(
                    "[live_runner_loop] adapter_warmup failed execution_family=%s error=%s",
                    execution_family,
                    exc,
                )

    def stop(self) -> None:
        self._running = False
        _log.info("[live_runner_loop] stopped")

    def _maybe_refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_refresh < self._refresh_interval:
            return
        self._tracker.refresh()
        self._subscribe_active_symbols()
        self._last_refresh = now

    def _subscribe_active_symbols(self) -> None:
        try:
            from ..price_bus import get_price_bus

            bus = get_price_bus()
        except Exception:
            return

        current_symbols = self._tracker.get_all_symbols()
        new_symbols = current_symbols - self._subscribed_symbols
        stale_symbols = self._subscribed_symbols - current_symbols

        for sym in stale_symbols:
            try:
                bus.unregister_tick_listener(sym, self._on_tick)
            except Exception:
                pass
        for sym in new_symbols:
            try:
                bus.subscribe_symbol(sym)
                bus.register_tick_listener(sym, self._on_tick)
            except Exception as exc:
                _log.debug("[live_runner_loop] price-bus subscribe failed %s: %s", sym, exc)
        self._subscribed_symbols = current_symbols

    def _on_tick(self, symbol: str, quote: Any) -> None:
        if not self._running:
            return
        self._maybe_refresh()
        source = getattr(quote, "source", "price_bus")
        for sess in self._tracker.get_sessions_for_symbol(symbol):
            self._submit_session(int(sess["session_id"]), cause=f"price_bus:{source}")

    def _loop(self) -> None:
        next_heartbeat = time.monotonic() + self._heartbeat_interval
        next_iqfeed = time.monotonic()
        while self._running:
            now = time.monotonic()
            self._maybe_refresh()
            if (
                bool(getattr(settings, "chili_momentum_live_runner_loop_iqfeed_tape_enabled", True))
                and bool(getattr(settings, "chili_momentum_live_runner_loop_iqfeed_poll_fallback_enabled", True))
                and now >= next_iqfeed
            ):
                self._tick_from_iqfeed_tape()
                next_iqfeed = now + self._iqfeed_poll_interval
            if now >= next_heartbeat:
                for sid in self._tracker.get_all_session_ids():
                    self._submit_session(int(sid), cause="heartbeat")
                next_heartbeat = now + self._heartbeat_interval
            time.sleep(min(0.10, self._iqfeed_poll_interval))

    def _start_iqfeed_notify_listener(self) -> None:
        if not bool(getattr(settings, "chili_momentum_live_runner_loop_iqfeed_notify_enabled", True)):
            return
        self._notify_thread = threading.Thread(
            target=self._iqfeed_notify_loop,
            daemon=True,
            name="live-runner-iqfeed-listen",
        )
        self._notify_thread.start()

    def _iqfeed_notify_loop(self) -> None:
        try:
            import psycopg2
        except Exception as exc:
            _log.warning("[live_runner_loop] IQFeed notify disabled; psycopg2 unavailable: %s", exc)
            return

        channel = "momentum_iqfeed_l1"
        db_url = str(getattr(settings, "database_url", "") or "")
        while self._running:
            conn = None
            try:
                conn = psycopg2.connect(db_url)
                conn.set_session(autocommit=True)
                cur = conn.cursor()
                cur.execute(f"LISTEN {channel};")
                _log.info("[live_runner_loop] listening for IQFeed events channel=%s", channel)
                while self._running:
                    ready, _, _ = select.select([conn], [], [], 1.0)
                    if not ready:
                        continue
                    conn.poll()
                    while conn.notifies:
                        notify = conn.notifies.pop(0)
                        self._handle_iqfeed_notify_payload(notify.payload)
            except Exception as exc:
                if self._running:
                    _log.warning("[live_runner_loop] IQFeed notify listener reconnecting after error: %s", exc)
                    time.sleep(1.0)
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass

    def _handle_iqfeed_notify_payload(self, payload: str) -> None:
        try:
            data = json.loads(payload or "{}")
        except Exception:
            data = {"symbol": str(payload or "").upper()}
        sym = str(data.get("symbol") or "").upper().strip()
        if not sym:
            return
        observed_at = data.get("observed_at")
        if observed_at is not None and self._last_iqfeed_observed_at.get(sym) == observed_at:
            return
        if observed_at is not None:
            self._last_iqfeed_observed_at[sym] = observed_at
        self._maybe_refresh()
        sessions = self._tracker.get_sessions_for_symbol(sym)
        if not sessions:
            admission = self._admit_iqfeed_symbol(sym, data)
            self._maybe_refresh()
            sessions = self._tracker.get_sessions_for_symbol(sym)
        for sess in sessions:
            self._submit_session(int(sess["session_id"]), cause="iqfeed_notify")

    def _admit_iqfeed_symbol(self, symbol: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        db = SessionLocal()
        try:
            from .ross_event_admission import admit_ross_event

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
            )
            db.commit()
            if result.get("admitted") or result.get("skipped") not in (
                "no_fresh_live_eligible_candidate",
                "cooldown",
                "ross_transcript_context_rejected",
            ):
                _log.info(
                    "[live_runner_loop] iqfeed admission symbol=%s admitted=%s skipped=%s",
                    symbol,
                    bool(result.get("admitted")),
                    result.get("skipped"),
                )
            if result.get("admitted"):
                self._tracker.refresh()
            return result
        except Exception as exc:
            _log.debug("[live_runner_loop] iqfeed admission failed symbol=%s: %s", symbol, exc)
            try:
                db.rollback()
            except Exception:
                pass
            return None
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

    def _tick_from_iqfeed_tape(self) -> None:
        symbols = sorted(self._tracker.get_all_symbols())
        if not symbols:
            return
        db = SessionLocal()
        try:
            stmt = sa.text(
                """
                SELECT symbol, max(observed_at) AS observed_at
                FROM momentum_nbbo_spread_tape
                WHERE source = 'iqfeed_l1'
                  AND symbol IN :symbols
                  AND observed_at >= (now() - make_interval(secs => :recent_s))
                GROUP BY symbol
                """
            ).bindparams(sa.bindparam("symbols", expanding=True))
            rows = db.execute(
                stmt,
                {
                    "symbols": symbols,
                    "recent_s": float(
                        getattr(settings, "chili_momentum_live_runner_loop_iqfeed_poll_recent_seconds", 180.0)
                        or 180.0
                    ),
                },
            ).mappings().all()
        except Exception as exc:
            _log.debug("[live_runner_loop] IQFeed tape poll failed: %s", exc)
            return
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            observed_at = row.get("observed_at")
            if not sym or observed_at is None:
                continue
            if self._last_iqfeed_observed_at.get(sym) == observed_at:
                continue
            self._last_iqfeed_observed_at[sym] = observed_at
            for sess in self._tracker.get_sessions_for_symbol(sym):
                self._submit_session(int(sess["session_id"]), cause="iqfeed_l1")

    def _submit_session(self, session_id: int, *, cause: str) -> None:
        now = time.monotonic()
        with self._lock:
            if session_id in self._inflight:
                return
            last = self._last_tick_monotonic.get(session_id, 0.0)
            if now - last < self._min_tick_interval:
                return
            self._last_tick_monotonic[session_id] = now
            self._inflight.add(session_id)
        try:
            self._executor.submit(self._tick_session, int(session_id), cause)
        except Exception:
            with self._lock:
                self._inflight.discard(session_id)

    def _tick_session(self, session_id: int, cause: str) -> None:
        db = SessionLocal()
        result: dict[str, Any] = {}
        try:
            from .live_runner import tick_live_session

            state = ""
            for handoff_pass in range(3):
                result = tick_live_session(db, int(session_id))
                db.commit()
                state = str(result.get("state") or "")
                if result.get("skipped") not in (None, "concurrent_tick"):
                    _log.debug(
                        "[live_runner_loop] tick skipped session=%s cause=%s reason=%s",
                        session_id,
                        cause,
                        result.get("skipped"),
                    )
                if state in LIVE_RUNNER_TERMINAL_STATES:
                    self._tracker.refresh()
                    break
                if not self._needs_immediate_entry_handoff_drain(db, int(session_id)):
                    break
                _log.info(
                    "[live_runner_loop] draining entry handoff session=%s cause=%s pass=%s state=%s",
                    session_id,
                    cause,
                    handoff_pass + 1,
                    state,
                )
            self._emit_event_replay_snapshot(db, int(session_id), cause=cause, result=result)
        except Exception as exc:
            _log.debug("[live_runner_loop] tick failed session=%s cause=%s: %s", session_id, cause, exc)
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
            with self._lock:
                self._inflight.discard(int(session_id))

    def _emit_event_replay_snapshot(
        self,
        db: Any,
        session_id: int,
        *,
        cause: str,
        result: dict[str, Any],
    ) -> None:
        """Best-effort Replay v3 timeline snapshot for event-driven runner ticks."""
        try:
            if not bool(getattr(settings, "chili_momentum_live_runner_replay_snapshot_enabled", True)):
                return
            tracker_ids = self._tracker.get_all_session_ids()
            if not tracker_ids:
                tracker_ids = [int(session_id)]
            rows = (
                db.query(TradingAutomationSession)
                .filter(
                    TradingAutomationSession.mode == "live",
                    TradingAutomationSession.id.in_([int(sid) for sid in tracker_ids]),
                )
                .order_by(TradingAutomationSession.updated_at.desc(), TradingAutomationSession.id.desc())
                .all()
            )
            if not rows:
                return
            by_id = {int(row.id): row for row in rows}
            anchor = by_id.get(int(session_id)) or rows[0]

            def _iso(raw: Any) -> str | None:
                try:
                    return raw.isoformat() if raw is not None else None
                except Exception:
                    return str(raw) if raw is not None else None

            session_rows = []
            families: dict[str, str] = {}
            for sess in rows:
                snapshot = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
                ef = normalize_execution_family(sess.execution_family)
                if ef:
                    families.setdefault(ef, str(sess.venue or ef))
                session_rows.append(
                    {
                        "id": int(sess.id),
                        "session_id": int(sess.id),
                        "symbol": str(sess.symbol or ""),
                        "venue": str(sess.venue or ""),
                        "execution_family": ef,
                        "mode": str(sess.mode or ""),
                        "state": str(sess.state or ""),
                        "risk_snapshot_json": snapshot,
                        "snapshot": snapshot,
                        "started_at": _iso(sess.started_at),
                        "created_at": _iso(sess.created_at),
                        "updated_at": _iso(sess.updated_at),
                        "ended_at": _iso(sess.ended_at),
                        "correlation_id": sess.correlation_id,
                        "source_node_id": sess.source_node_id,
                    }
                )
            capacity = max(1, len(session_rows))
            payload = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "rows": session_rows,
                "session_rows": session_rows,
                "selected_session_ids": [int(session_id)],
                "event_session_id": int(session_id),
                "event_cause": str(cause or ""),
                "event_result_state": str((result or {}).get("state") or ""),
                "capacity_limit": capacity,
                "order_call_budget": capacity,
                "risk_budget_slots": capacity,
                "venue_states": [
                    {
                        "venue": families.get(ef) or ef,
                        "execution_family": ef,
                        "adapter_available": True,
                        "venue_enabled": True,
                        "order_call_budget": capacity,
                        "risk_budget_slots": capacity,
                    }
                    for ef in sorted(families)
                ],
                "candidate_count": len(session_rows),
                "source": "live_runner_event_loop",
            }
            append_trading_automation_event(
                db,
                int(anchor.id),
                "live_replay_event_snapshot",
                payload,
                correlation_id=anchor.correlation_id,
                source_node_id="momentum_live_runner_loop",
            )
            db.commit()
        except Exception:
            _log.debug("[live_runner_loop] replay event snapshot skipped session=%s", session_id, exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass

    @staticmethod
    def _needs_immediate_entry_handoff_drain(db: Any, session_id: int) -> bool:
        try:
            sess = db.get(TradingAutomationSession, int(session_id))
        except Exception:
            return False
        if sess is None or sess.state not in (STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_PENDING_ENTRY):
            return False
        snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
        le = snap.get("momentum_live_execution")
        if not isinstance(le, dict):
            return False
        if le.get("entry_submitted") or le.get("entry_order_id"):
            return False
        unresolved = le.get("entry_order_ids_unresolved") or le.get("entry_order_ids_all_unresolved")
        if isinstance(unresolved, list) and unresolved:
            return False
        return True


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
    if not settings.chili_momentum_live_runner_enabled:
        return
    if not bool(getattr(settings, "chili_momentum_live_runner_loop_enabled", True)):
        return
    get_live_runner_loop().start()


def stop_live_runner_loop() -> None:
    if _loop is not None:
        _loop.stop()
