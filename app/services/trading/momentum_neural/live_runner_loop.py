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
import os
import re
import select
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ....config import settings
from ....db import SessionLocal, engine
from ....models.trading import TradingAutomationSession
from ..execution_family_registry import (
    EXECUTION_FAMILY_ALPACA_SPOT,
    normalize_execution_family,
)
from .captured_paper_dispatcher import (
    dispatch_captured_paper_live_runner_tick,
    dispatch_captured_paper_post_commit,
    dispatch_live_runner_tick,
    validate_captured_paper_session_owner_inventory,
)
from .captured_paper_entry_intent import CapturedPaperPostCommitRequest
from .captured_paper_fill_capture import (
    CAPTURED_PAPER_EXIT_FILL_POST_COMMIT_KEY,
    CAPTURED_PAPER_EXIT_TRANSPORT_POST_COMMIT_KEY,
    CapturedPaperExitFillPostCommitRequest,
    CapturedPaperExitTransportPostCommitRequest,
)
from .captured_paper_iqfeed_trigger import (
    parse_captured_paper_iqfeed_q_notify,
)
from .captured_paper_pending_owner import (
    validate_captured_paper_pending_owner_inventory,
)
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
from .replay_capture_contract import CaptureContractError

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

# Frames that are never the answer to "who stopped the loop?" -- stop() calls
# itself re-entrantly under the lifecycle lock, and the module-level helper is
# just a forwarder.
_STOP_FRAME_NOISE = frozenset({"stop", "_stop_caller", "stop_live_runner_loop"})


def _stop_caller() -> str:
    """Name whoever asked the loop to retire, for the shutdown log line.

    Distinguishes the two cases that look identical in the logs today:

      * a deliberate in-process stop (scheduler shutdown, a restart, a
        supervisor) -- reported as `module.func:line`, and
      * interpreter teardown, i.e. the process itself is going away because it
        was signalled or its parent console closed -- reported as
        `interpreter-shutdown`. This one matters most here: the loop runs in the
        FOREGROUND of a Task Scheduler PowerShell, so ending the task ends the
        loop, and that path leaves no other evidence anywhere.

    Best-effort by construction: this runs on the teardown path, so it must never
    be the reason a shutdown fails.
    """
    try:
        import sys as _sys
        import traceback as _traceback

        if getattr(_sys, "is_finalizing", None) and _sys.is_finalizing():
            return "interpreter-shutdown"

        for frame in reversed(_traceback.extract_stack()[:-1]):
            if frame.name in _STOP_FRAME_NOISE:
                continue
            module = frame.filename.replace("\\", "/").rsplit("/", 1)[-1]
            return f"{module}:{frame.lineno} in {frame.name}()"
        return "unknown-caller"
    except Exception:
        return "unknown-caller"
_IQFEED_AUTHORITY_BASIS = "iqfeed_q_receive_trade_reference_fenced"
_IQFEED_AUTHORITY_MAX_AGE_S = 2.0
_IQFEED_FUTURE_TOLERANCE_S = 1.0
_IQFEED_DEDUP_RETENTION_S = 5.0
_CAPTURED_PAPER_ADMISSION_REJECTION_LOG_INTERVAL_S = 60.0
# ADMISSION OUTCOME CENSUS (2026-08-05). Ang buod ay lumalabas kada ganito
# karaming segundo, sa WARNING — sinadya ang antas: ang ilang runtime dito ay
# walang logging config kaya root WARNING ang default, at ang INFO ay tahimik na
# nawawala (napatunayan sa replay harness ngayong araw).
_CAPTURED_PAPER_ADMISSION_CENSUS_INTERVAL_S = 120.0
# Tuwing ilang kalalabasan bago bumasa ng orasan. Hindi ito tuning para sa
# isang test: ang pagbasa ng orasan kada admission ay dagdag na trabaho sa
# hot path AT nag-e-exhaust ng eksaktong monotonic budget ng mga test.
_CAPTURED_PAPER_ADMISSION_CENSUS_TICKS = 16
# Ilang MAGKAKAIBANG hilaw na dahilan ang itatabi bilang sample. Maliit at may
# hangganan — pagmamasid ito, hindi imbakan.
_CAPTURED_PAPER_ADMISSION_RAW_SAMPLE_KEYS = 8

# Runs of 2+ digits are identifiers (sequence numbers, ids), never the reason.
# Two digits is the floor deliberately: single digits carry meaning in reasons
# like "l2 depth 0", while any counter that can collide with the 8-key cap is
# already wider than that.
_DIGIT_RUN_RE = re.compile(r"\d{2,}")
_CAPTURED_PAPER_ADMISSION_RAW_SAMPLE_LEN = 120
_CAPTURED_PAPER_ADMISSION_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_CAPTURED_PAPER_ADMISSION_TYPED_PREFIX_RE = re.compile(
    r"^([A-Z][A-Z0-9_]{0,127}):(?:\s|$)"
)
_IQFEED_BUILD_RE = re.compile(
    r"^iqfeed-l1-exact-print-provenance-v3\+sha256:[0-9a-f]{16}$"
)
_IQFEED_NOTIFY_CHANNEL_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_IQFEED_DEFAULT_NOTIFY_CHANNEL = "momentum_iqfeed_l1"
# IGNITION nominations (2026-07-17): SEPARATE pg_notify channel fed by the host
# bridge's tick-based early-mover detector. Minimal payload, NOT the v3 authority
# envelope (captured_paper_iqfeed_trigger does exact key-set matching on that one).
# The consumer only NOMINATES into the same guarded admit_ross_event path
# (pg_advisory_xact_lock dedup + every admission gate stay authoritative); hard
# caps here are defense-in-depth against a chatty/compromised producer.
_IGNITION_DEFAULT_CHANNEL = "momentum_iqfeed_ignition"
_IGNITION_SCHEMA_VERSION = "chili.iqfeed-ignition-nominate.v1"
_IGNITION_SOURCE_TAG = "ignition_tick"
_IGNITION_MAX_AGE_S = 30.0
_IGNITION_FUTURE_TOLERANCE_S = 1.0


def _captured_paper_admission_reason_code(value: Any) -> str:
    """Return a bounded typed reason without persisting arbitrary exception text."""

    reason = value.strip() if isinstance(value, str) else ""
    if _CAPTURED_PAPER_ADMISSION_REASON_RE.fullmatch(reason):
        return reason
    # Selection-runtime errors carry ``UPPER_SNAKE_CODE: detail``.  Preserve only
    # that stable code and discard the free-form detail.  This keeps the census
    # useful without allowing URLs, credentials, symbols, or exception text into
    # logs/sidecars.
    typed = _CAPTURED_PAPER_ADMISSION_TYPED_PREFIX_RE.match(reason)
    if typed is not None:
        return typed.group(1).lower()
    return "captured_paper_admission_rejected"


# IGNITION ADMISSION GOVERNORS (2026-09-04). These were hard-coded module
# constants (300.0 / 6). They gate the ONLY path that can admit a symbol the
# universe poll has never seen, so they are load-bearing on admission latency —
# the largest measured class of missed winners (median first admission at
# +15.6%, p90 +44.7%). Both are now ONE documented setting each, with the
# derivation written down in app/config.py and computed by
# scripts/derive_ignition_governors.py. Day-1 defaults equal the old constants,
# so behaviour is byte-identical until an operator moves them.
_IGNITION_DEDUP_TTL_FALLBACK_S = 300.0
_IGNITION_ADMITS_PER_MINUTE_FALLBACK = 6
# Rolling window the admits/minute cap is measured over. Not a tunable: "per
# minute" IS the unit of the setting above.
_IGNITION_ADMIT_RATE_WINDOW_S = 60.0
# Bounded memory for the per-symbol dedup map (defence against a chatty producer).
_IGNITION_DEDUP_MAX_ENTRIES = 2048


def _ignition_dedup_ttl_seconds() -> float:
    """One admission ATTEMPT per symbol per this many seconds (settings-driven)."""
    try:
        value = float(
            getattr(
                settings,
                "chili_momentum_ignition_dedup_ttl_seconds",
                _IGNITION_DEDUP_TTL_FALLBACK_S,
            )
        )
    except (TypeError, ValueError):
        return _IGNITION_DEDUP_TTL_FALLBACK_S
    return value if value > 0 else _IGNITION_DEDUP_TTL_FALLBACK_S


def _ignition_admits_per_minute() -> int:
    """Hard cap on ignition admission ATTEMPTS per rolling minute (settings-driven)."""
    try:
        value = int(
            getattr(
                settings,
                "chili_momentum_ignition_admits_per_minute",
                _IGNITION_ADMITS_PER_MINUTE_FALLBACK,
            )
        )
    except (TypeError, ValueError):
        return _IGNITION_ADMITS_PER_MINUTE_FALLBACK
    return value if value > 0 else _IGNITION_ADMITS_PER_MINUTE_FALLBACK


_IQFEED_EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,15}$")
_LIVE_LOOP_OWNER_INSTANCE_ID = str(uuid.uuid4())
# Cluster-wide, database-scoped PostgreSQL advisory fence.  Positive int32 values
# spell ``CHIL`` / ``LOOP`` and intentionally use the two-key advisory namespace.
_LIVE_LOOP_FENCE_NAMESPACE = 0x4348494C
_LIVE_LOOP_FENCE_KEY = 0x4C4F4F50
_CAPTURED_PAPER_PREOWNER_STATE = "captured_paper_preowner"


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


@dataclass(frozen=True, slots=True)
class CapturedPaperLiveRunnerScope:
    """Exact fake-money session inventory owned by the dedicated service."""

    expected_account_id: str
    runtime_generation: str
    broker_connection_generation: str
    execution_family: str = EXECUTION_FAMILY_ALPACA_SPOT
    account_scope: str = "alpaca:paper"

    def __post_init__(self) -> None:
        try:
            account_id = str(uuid.UUID(str(self.expected_account_id or "")))
            generation = str(uuid.UUID(str(self.runtime_generation or "")))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("captured PAPER live-loop scope UUID is invalid") from exc
        connection_generation = str(
            self.broker_connection_generation or ""
        ).strip()
        if (
            account_id != str(self.expected_account_id or "").strip().lower()
            or generation != str(self.runtime_generation or "").strip().lower()
            or not connection_generation
            or len(connection_generation) > 160
            or any(ord(char) < 32 for char in connection_generation)
            or self.account_scope != "alpaca:paper"
            or normalize_execution_family(self.execution_family)
            != EXECUTION_FAMILY_ALPACA_SPOT
        ):
            raise ValueError("captured PAPER live-loop scope is not exact")
        object.__setattr__(self, "expected_account_id", account_id)
        object.__setattr__(self, "runtime_generation", generation)
        object.__setattr__(
            self,
            "broker_connection_generation",
            connection_generation,
        )
        object.__setattr__(self, "execution_family", EXECUTION_FAMILY_ALPACA_SPOT)

    def assert_session(self, sess) -> None:
        snapshot = getattr(sess, "risk_snapshot_json", None)
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        if (
            snapshot.get("captured_paper_session_owner") is None
            and snapshot.get("captured_paper_session_pending_owner") is not None
        ):
            # The only owner-less runnable row admitted to the isolated
            # inventory is the exact content-addressed PENDING_OWNER produced
            # by the atomic initial path.  It remains non-executable until the
            # runtime installs the final owner under account/claim/session
            # locks immediately before its first FSM tick.
            validate_captured_paper_pending_owner_inventory(
                sess,
                expected_account_id=self.expected_account_id,
                expected_runtime_generation=self.runtime_generation,
                expected_execution_family=self.execution_family,
            )
        else:
            validate_captured_paper_session_owner_inventory(
                sess,
                expected_account_id=self.expected_account_id,
                expected_runtime_generation=self.runtime_generation,
                expected_execution_family=self.execution_family,
            )
        generation_claims: list[str] = []
        for container in (
            snapshot,
            snapshot.get("momentum_live_execution"),
            snapshot.get("captured_paper_admission"),
        ):
            if isinstance(container, dict):
                claimed = str(
                    container.get("captured_paper_runtime_generation")
                    or container.get("runtime_generation")
                    or ""
                ).strip()
                if claimed:
                    generation_claims.append(claimed)
        if any(claim != self.runtime_generation for claim in generation_claims):
            raise RuntimeError(
                "captured_paper_foreign_runtime_generation_session"
            )


class _LiveSessionTracker:
    """Thread-safe registry of runnable LIVE sessions + their exit thresholds."""

    def __init__(
        self,
        captured_paper_scope: CapturedPaperLiveRunnerScope | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, dict] = {}
        self._owner_generation: int | None = None
        self._captured_paper_scope = captured_paper_scope
        self._scope_breach_reason: str | None = None
        # Monotonic stamp of when the inventory first became UNREADABLE via a
        # DB fault. None means readable, or breached (which never waits).
        self._inventory_unreadable_since: float | None = None

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
                if self._captured_paper_scope is not None:
                    if str(getattr(sess, "state", "") or "").strip() == (
                        _CAPTURED_PAPER_PREOWNER_STATE
                    ):
                        # The sealed admission foundation is intentionally a
                        # distinct, non-runnable state with a distinct PREOWNER
                        # marker.  It must never alias the final durable owner.
                        continue
                    self._captured_paper_scope.assert_session(sess)
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
                self._scope_breach_reason = None
                self._inventory_unreadable_since = None
            return True
        except Exception as e:
            _log.warning("[live_loop] session refresh failed: %s", e)
            if self._captured_paper_scope is not None:
                # A dedicated service must never continue from a previously
                # cached inventory after either a foreign session appears or
                # the current inventory becomes unreadable. Clearing _sessions
                # makes the loop INERT, which is the safety property -- it acts
                # on nothing until the inventory is readable again.
                #
                # But UNREADABLE and BREACHED are not the same fact, and the
                # caller could not tell them apart. A psycopg2 blip on the 5433
                # loopback and a foreign session appearing in the scope both
                # arrived here as a bare False, and the caller retired the
                # generation permanently for either. That is what killed r162:
                # `dedicated captured PAPER inventory lost; retiring
                # generation=1` at 04:59:03Z, seconds after Postgres logged
                # `FATAL: canceling authentication due to timeout`. Nothing
                # restarts the loop, so one blip cost 7.5 hours.
                #
                # A DB error is transient by nature; a scope breach
                # (assert_session raises RuntimeError) is not. Record which.
                with self._lock:
                    if self._owner_generation == int(expected_generation):
                        self._sessions = {}
                        self._scope_breach_reason = str(
                            e or "captured_paper_session_inventory_unavailable"
                        )
                        if isinstance(e, SQLAlchemyError):
                            if self._inventory_unreadable_since is None:
                                self._inventory_unreadable_since = time.monotonic()
                        else:
                            self._inventory_unreadable_since = None
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

    def inventory_is_unreadable(self) -> bool:
        """Was the last failure a DB fault (transient) rather than a breach?

        Separate from the duration on purpose. `time.monotonic()` has ~15.6ms
        resolution on Windows, so a blip detected within the same tick measures
        EXACTLY 0.0 seconds -- and a caller gating on `duration > 0` would treat
        the freshest possible fault as no fault at all and retire anyway, which
        is the entire bug this change exists to fix.
        """

        with self._lock:
            return self._inventory_unreadable_since is not None

    def inventory_unreadable_seconds(self) -> float:
        """How long the inventory has been unreadable. 0.0 if it is readable.

        Only meaningful alongside `inventory_is_unreadable()` -- see above.
        """

        with self._lock:
            since = self._inventory_unreadable_since
        return 0.0 if since is None else max(0.0, time.monotonic() - since)

    def scope_is_healthy(self) -> bool:
        with self._lock:
            return self._scope_breach_reason is None


class LiveRunnerLoop:
    """Bridges price-bus ticks to ``tick_live_session`` for exit-speed reaction."""

    def __init__(
        self,
        *,
        captured_paper_scope: CapturedPaperLiveRunnerScope | None = None,
        captured_paper_symbol_admitter: (
            Callable[..., Mapping[str, Any]] | None
        ) = None,
        captured_paper_exit_completion_handler: (
            Callable[[CapturedPaperExitFillPostCommitRequest], Any] | None
        ) = None,
        captured_paper_exit_transport_handler: (
            Callable[[CapturedPaperExitTransportPostCommitRequest], Any] | None
        ) = None,
    ) -> None:
        if (
            captured_paper_scope is not None
            and type(captured_paper_scope) is not CapturedPaperLiveRunnerScope
        ):
            raise ValueError("captured PAPER live-loop scope type is invalid")
        if captured_paper_scope is None and captured_paper_symbol_admitter is not None:
            raise ValueError(
                "ordinary live loop cannot install captured PAPER admission"
            )
        if (
            captured_paper_scope is None
            and captured_paper_exit_completion_handler is not None
        ):
            raise ValueError(
                "ordinary live loop cannot install captured PAPER exit completion"
            )
        if (
            captured_paper_exit_completion_handler is not None
            and not callable(captured_paper_exit_completion_handler)
        ):
            raise ValueError("captured PAPER exit completion handler is invalid")
        if (
            captured_paper_scope is None
            and captured_paper_exit_transport_handler is not None
        ):
            raise ValueError(
                "ordinary live loop cannot install captured PAPER exit transport"
            )
        if (
            captured_paper_exit_transport_handler is not None
            and not callable(captured_paper_exit_transport_handler)
        ):
            raise ValueError("captured PAPER exit transport handler is invalid")
        self._captured_paper_scope = captured_paper_scope
        self._captured_paper_symbol_admitter = captured_paper_symbol_admitter
        self._captured_paper_exit_completion_handler = (
            captured_paper_exit_completion_handler
        )
        self._captured_paper_exit_transport_handler = (
            captured_paper_exit_transport_handler
        )
        self._tracker = _LiveSessionTracker(captured_paper_scope)
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
        self._notify_thread_generation: int | None = None
        self._notify_ready_generation: int | None = None
        self._notify_failed_generation: int | None = None
        self._notify_startup_event: threading.Event | None = None
        self._iqfeed_provenance_lock = threading.Lock()
        self._iqfeed_inflight_certified: set[tuple] = set()
        self._iqfeed_certified_watermarks: dict[tuple, float] = {}
        self._iqfeed_generation_watermarks: dict[str, int] = {}
        self._iqfeed_admission_lock = threading.Lock()
        self._iqfeed_admission_inflight: dict[object, tuple[int, str]] = {}
        self._captured_paper_admission_rejection_log_monotonic: float | None = None
        # ADMISSION OUTCOME CENSUS (2026-08-05). Ang bawat kalalabasan ng sealed
        # captured-paper admitter ay binibilang dito ayon sa dahilan, KASAMA ang
        # mga dating tahimik. Tingnan ang paliwanag sa _admit_iqfeed_symbol.
        self._captured_paper_admission_outcomes: dict[str, int] = {}
        self._captured_paper_admission_census_monotonic: float | None = None
        self._captured_paper_admission_census_ticks: int = 0
        self._captured_paper_admission_raw_samples: dict[str, int] = {}
        # IGNITION nomination governors (monotonic clocks; consumer-side caps).
        self._ignition_lock = threading.Lock()
        self._ignition_dedup: dict[str, float] = {}
        self._ignition_admit_monotonic: list[float] = []
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

    @property
    def captured_paper_scope(self) -> CapturedPaperLiveRunnerScope | None:
        return self._captured_paper_scope

    @property
    def captured_paper_symbol_admitter(self) -> Callable[..., Mapping[str, Any]] | None:
        return self._captured_paper_symbol_admitter

    @property
    def captured_paper_exit_completion_handler(
        self,
    ) -> Callable[[CapturedPaperExitFillPostCommitRequest], Any] | None:
        return self._captured_paper_exit_completion_handler

    @property
    def captured_paper_exit_transport_handler(
        self,
    ) -> Callable[[CapturedPaperExitTransportPostCommitRequest], Any] | None:
        return self._captured_paper_exit_transport_handler

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
                and self._tracker.scope_is_healthy()
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
            if (
                self._captured_paper_scope is not None
                and not self._iqfeed_notify_listener_alive_for_generation(
                    self._generation,
                    self._stop_event,
                )
            ):
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
                    heartbeat_identity = {}
                    if self._captured_paper_scope is not None:
                        heartbeat_identity = {
                            "account_scope": (
                                self._captured_paper_scope.account_scope
                            ),
                            "expected_account_id": (
                                self._captured_paper_scope.expected_account_id
                            ),
                            "runtime_generation": (
                                self._captured_paper_scope.runtime_generation
                            ),
                            "execution_family": (
                                self._captured_paper_scope.execution_family
                            ),
                            "live_cash_authorized": False,
                        }
                    record_live_runner_loop_run(
                        db,
                        owner_instance_id=self._owner_instance_id,
                        generation=generation,
                        generation_started_at=generation_started_at,
                        **heartbeat_identity,
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
            if (
                self._captured_paper_scope is not None
                and not self._captured_paper_iqfeed_notify_configuration_ready()
            ):
                _log.critical(
                    "[live_loop] refusing captured PAPER start: IQFeed notify "
                    "admission contract is unavailable"
                )
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
                notify_started = self._start_iqfeed_notify_listener(
                    generation,
                    stop_event,
                )
                if self._captured_paper_scope is not None and (
                    not notify_started
                    or not self._await_iqfeed_notify_listener_ready(
                        generation,
                        stop_event,
                    )
                ):
                    raise RuntimeError(
                        "captured PAPER IQFeed notify listener failed startup"
                    )
                if not self._generation_active(generation, stop_event):
                    raise RuntimeError("live-loop generation retired during startup")
                if not self._owner_fence_is_held():
                    raise RuntimeError("live-loop owner fence lost during startup")
                if (
                    self._captured_paper_scope is not None
                    and not self._iqfeed_notify_listener_alive_for_generation(
                        generation,
                        stop_event,
                    )
                ):
                    raise RuntimeError(
                        "captured PAPER IQFeed notify listener retired during startup"
                    )
                if not self._record_lane_health_heartbeat(
                    generation=generation,
                    force=True,
                ):
                    raise RuntimeError(
                        "initial durable lane-health heartbeat failed"
                    )
                if (
                    self._captured_paper_scope is not None
                    and not self._iqfeed_notify_listener_alive_for_generation(
                        generation,
                        stop_event,
                    )
                ):
                    raise RuntimeError(
                        "captured PAPER IQFeed notify listener retired during startup"
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
        # See the WARNING at the end of this method for why the caller is named.
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
            self._notify_thread_generation = None
            self._notify_ready_generation = None
            self._notify_failed_generation = None
            self._notify_startup_event = None
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
                # WHY THIS IS A WARNING WITH A CALLER, NOT A BARE INFO
                # ---------------------------------------------------
                # Over the 14 days to 2026-08-11 this loop was dead 50.9% of the
                # time (57 outages, 170.0h, worst 15.58h) and not one death could
                # be explained. The forensics kept coming back empty in the same
                # way every time: every heartbeat row ends `ok` with no
                # error_message, RAM/handles/threads/sockets are FLAT right up to
                # the end, and the deaths spread evenly over all 24 ET hours. So
                # the loop is not crashing, not leaking and not being reaped on a
                # schedule -- it is being asked to stop, and the only trace it
                # left was this line, which named no one.
                #
                # An owner that manages live exits must say who retired it.
                _log.warning("[live_loop] stopped by %s", _stop_caller())
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
                    if self._captured_paper_scope is not None:
                        # A DB blip is not a lost inventory. `refresh` already
                        # cleared _sessions, so the loop is INERT either way --
                        # it acts on nothing until the read succeeds. Retiring on
                        # top of that buys no safety and costs the whole session:
                        # nothing restarts this loop, and on 2026-08-10 one
                        # `FATAL: canceling authentication due to timeout` on the
                        # 5433 loopback cost 7.5 hours of lane downtime.
                        #
                        # Bounded by live_loop_stale_seconds() -- the same
                        # authoritative threshold that decides this loop is stale
                        # elsewhere, so there is no new number to tune. Past it,
                        # retire exactly as before: an inventory that has been
                        # unreadable that long is no longer a blip.
                        if self._tracker.inventory_is_unreadable():
                            unreadable = (
                                self._tracker.inventory_unreadable_seconds()
                            )
                            try:
                                from .lane_health import live_loop_stale_seconds

                                budget = float(live_loop_stale_seconds())
                            except Exception:
                                budget = 75.0
                            if unreadable < budget:
                                _log.warning(
                                    "[live_loop] captured PAPER inventory "
                                    "unreadable for %.1fs (budget %.0fs); holding "
                                    "inert, generation=%d",
                                    unreadable,
                                    budget,
                                    generation,
                                )
                                continue
                        _log.critical(
                            "[live_loop] dedicated captured PAPER inventory lost; "
                            "retiring generation=%d",
                            generation,
                        )
                        self.stop()
                        break
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
                    return True
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

    def schedule_entry_continuation(self, session_id: int) -> bool:
        """Continue a mechanical entry-state transition after this DB tick commits.

        Candidate detection, candidate revalidation, and broker placement remain
        separate idempotent FSM states, but they must not each wait for a slow
        scheduler pulse.  When called from the current event worker, the session
        is still marked in-flight, so ``guarantee_after_inflight`` records one
        deduplicated follow-up. ``_complete_dispatch`` submits it only after
        ``_tick_session`` has committed and closed its DB session.  The next state
        therefore re-reads eligibility, BBO/tape freshness, risk, ownership, and
        all pre-submit gates exactly as before.
        """

        generation = getattr(self._worker_context, "generation", None)
        # Outside this loop's worker there is no guarantee the caller's DB
        # transaction has committed. Refuse instead of dispatching a parallel
        # reader against uncommitted candidate/pending state.
        if generation is None:
            return False
        if not self._generation_active(generation):
            return False
        return self._dispatch(
            int(session_id),
            guarantee_after_inflight=True,
            expected_generation=generation,
        )

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

    def _captured_paper_iqfeed_notify_configuration_ready(self) -> bool:
        """Return the exact bridge/listener contract for dedicated PAPER only."""

        if self._captured_paper_scope is None:
            return True
        if not bool(
            getattr(
                settings,
                "chili_momentum_live_runner_loop_iqfeed_notify_enabled",
                False,
            )
        ):
            return False
        channel = str(
            getattr(
                settings,
                "chili_momentum_live_runner_loop_iqfeed_notify_channel",
                "",
            )
            or ""
        ).strip()
        bridge_channel = str(
            os.environ.get("IQFEED_NOTIFY_CHANNEL", "")
            or ""
        ).strip()
        bridge_notify_enabled = str(
            os.environ.get("IQFEED_NOTIFY_ENABLED", "") or ""
        ).strip().lower()
        expected_build = str(
            getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "")
            or ""
        ).strip()
        return bool(
            bridge_notify_enabled in {"1", "true", "yes", "on"}
            and _IQFEED_NOTIFY_CHANNEL_RE.fullmatch(channel) is not None
            and channel == bridge_channel
            and _IQFEED_BUILD_RE.fullmatch(expected_build) is not None
        )

    def _mark_iqfeed_notify_listener_ready(self, generation: int) -> None:
        generation = int(generation)
        if (
            self._notify_thread_generation == generation
            and self._generation_active(generation, self._stop_event)
        ):
            self._notify_failed_generation = None
            self._notify_ready_generation = generation
            event = self._notify_startup_event
            if event is not None:
                event.set()

    def _mark_iqfeed_notify_listener_failed(self, generation: int) -> None:
        generation = int(generation)
        if self._notify_thread_generation == generation:
            self._notify_ready_generation = None
            self._notify_failed_generation = generation
            event = self._notify_startup_event
            if event is not None:
                event.set()

    def _iqfeed_notify_listener_alive_for_generation(
        self,
        generation: int,
        stop_event: threading.Event | None,
    ) -> bool:
        thread = self._notify_thread
        return bool(
            self._captured_paper_iqfeed_notify_configuration_ready()
            and self._generation_active(int(generation), stop_event)
            and self._notify_thread_generation == int(generation)
            and self._notify_ready_generation == int(generation)
            and self._notify_failed_generation != int(generation)
            and thread is not None
            and thread.is_alive()
        )

    def _await_iqfeed_notify_listener_ready(
        self,
        generation: int,
        stop_event: threading.Event,
    ) -> bool:
        if self._notify_thread_generation != int(generation):
            return False
        event = self._notify_startup_event
        if event is None or not event.wait(timeout=_THREAD_JOIN_TIMEOUT_S):
            return False
        return self._iqfeed_notify_listener_alive_for_generation(
            int(generation),
            stop_event,
        )

    def _start_iqfeed_notify_listener(
        self,
        generation: int,
        stop_event: threading.Event,
    ) -> bool:
        if not bool(getattr(settings, "chili_momentum_live_runner_loop_iqfeed_notify_enabled", True)):
            return False
        expected_build = str(
            getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "")
            or ""
        ).strip()
        if _IQFEED_BUILD_RE.fullmatch(expected_build) is None:
            _log.critical(
                "[live_loop] IQFeed notify admission disabled: no exact reviewed v3 "
                "bridge build is pinned"
            )
            return False
        self._notify_thread_generation = int(generation)
        self._notify_ready_generation = None
        self._notify_failed_generation = None
        self._notify_startup_event = threading.Event()
        self._notify_thread = threading.Thread(
            target=self._iqfeed_notify_loop,
            args=(generation, stop_event),
            daemon=True,
            name=f"live-runner-iqfeed-listen-g{generation}",
        )
        try:
            self._notify_thread.start()
        except Exception:
            self._mark_iqfeed_notify_listener_failed(generation)
            self._notify_thread = None
            return False
        return True

    def _coalesce_captured_paper_iqfeed_notifications(
        self,
        notifications: list[Any],
    ) -> list[Any]:
        """Keep one authoritative PAPER wake-up hint per exact symbol.

        PostgreSQL ``NOTIFY`` payloads are wake-up hints, never market evidence;
        the captured admission path revalidates the complete signed envelope and
        reads the exact retained stream before it can create a session.  A burst
        of historical hints for one symbol is therefore redundant and, if
        handled serially, can keep the LISTEN socket backpressured long enough
        that every later envelope exceeds the two-second authority window.

        Ordinary live execution retains its existing per-notification behavior.
        Captured PAPER drains the socket first, then evaluates the highest
        generation/newest fully validated hint per symbol.  The first pending
        position for each symbol is preserved so sustained ingress cannot starve
        another symbol; a just-handled symbol is requeued behind existing work.
        """

        if self._captured_paper_scope is None or not notifications:
            return notifications

        expected_build = str(
            getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "")
            or ""
        ).strip()
        retained_by_run: dict[
            tuple[str, str],
            tuple[int, Any, tuple, tuple, tuple],
        ] = {}
        for index, notification in enumerate(notifications):
            payload = getattr(notification, "payload", None)
            try:
                exact_notify = parse_captured_paper_iqfeed_q_notify(
                    payload,
                    expected_bridge_version=expected_build,
                )
            except CaptureContractError:
                continue
            available_age = (
                _utcnow() - exact_notify.available_at
            ).total_seconds()
            if (
                available_age < -_IQFEED_FUTURE_TOLERANCE_S
                or available_age > _IQFEED_AUTHORITY_MAX_AGE_S
            ):
                continue
            normalized = self._normalized_iqfeed_notify_authority(
                payload
            )
            if normalized is None:
                continue
            _data, certified_tuple = normalized
            run_key = (exact_notify.symbol, exact_notify.bridge_run_id)
            candidate_rank = (
                exact_notify.connection_generation,
                exact_notify.source_frame_sequence,
                exact_notify.provider_trade_reference_at,
                exact_notify.received_at,
                exact_notify.available_at,
                index,
            )
            cross_run_rank = (
                exact_notify.provider_trade_reference_at,
                exact_notify.received_at,
                exact_notify.available_at,
                exact_notify.connection_generation,
                exact_notify.source_frame_sequence,
                exact_notify.bridge_run_id,
                index,
            )
            prior = retained_by_run.get(run_key)
            if prior is None:
                retained_by_run[run_key] = (
                    index,
                    notification,
                    certified_tuple,
                    candidate_rank,
                    cross_run_rank,
                )
                continue
            (
                first_index,
                _prior_notification,
                _prior_tuple,
                prior_rank,
                _prior_cross_run_rank,
            ) = prior
            if candidate_rank > prior_rank:
                retained_by_run[run_key] = (
                    first_index,
                    notification,
                    certified_tuple,
                    candidate_rank,
                    cross_run_rank,
                )
        retained_by_symbol: dict[
            str,
            tuple[int, Any, tuple],
        ] = {}
        for (
            first_index,
            notification,
            certified_tuple,
            _run_rank,
            cross_run_rank,
        ) in retained_by_run.values():
            if not self._iqfeed_notify_candidate_is_reservable(
                certified_tuple
            ):
                continue
            symbol = certified_tuple[0]
            prior = retained_by_symbol.get(symbol)
            if prior is None:
                retained_by_symbol[symbol] = (
                    first_index,
                    notification,
                    cross_run_rank,
                )
                continue
            prior_first_index, _prior_notification, prior_rank = prior
            if cross_run_rank > prior_rank:
                retained_by_symbol[symbol] = (
                    min(prior_first_index, first_index),
                    notification,
                    cross_run_rank,
                )
        retained = [
            notification
            for _index, notification, _rank in sorted(
                retained_by_symbol.values(),
                key=lambda item: item[0],
            )
        ]
        if len(retained) != len(notifications):
            _log.debug(
                "[live_loop] coalesced captured PAPER IQFeed wake hints "
                "received=%d retained=%d",
                len(notifications),
                len(retained),
            )
        return retained

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
            self._mark_iqfeed_notify_listener_failed(generation)
            return
        try:
            import psycopg2
        except Exception as exc:
            _log.warning("[live_loop] IQFeed notify disabled; psycopg2 unavailable: %s", exc)
            self._mark_iqfeed_notify_listener_failed(generation)
            return

        channel = str(
            getattr(
                settings,
                "chili_momentum_live_runner_loop_iqfeed_notify_channel",
                _IQFEED_DEFAULT_NOTIFY_CHANNEL,
            )
            or ""
        ).strip()
        if _IQFEED_NOTIFY_CHANNEL_RE.fullmatch(channel) is None:
            _log.critical(
                "[live_loop] refusing IQFeed LISTEN with invalid channel=%r",
                channel,
            )
            self._mark_iqfeed_notify_listener_failed(generation)
            return
        db_url = str(getattr(settings, "database_url", "") or "")
        startup_ready = False
        try:
            while self._generation_active(generation, stop_event):
                conn = None
                try:
                    conn = psycopg2.connect(db_url)
                    conn.set_session(autocommit=True)
                    cur = conn.cursor()
                    cur.execute(f"LISTEN {channel};")
                    ignition_channel = self._ignition_listen_channel(channel)
                    if ignition_channel is not None:
                        cur.execute(f"LISTEN {ignition_channel};")
                    if not self._generation_active(generation, stop_event):
                        break
                    startup_ready = True
                    self._mark_iqfeed_notify_listener_ready(generation)
                    _log.info(
                        "[live_loop] listening for IQFeed events channel=%s ignition=%s",
                        channel,
                        ignition_channel or "-",
                    )
                    while self._generation_active(generation, stop_event):
                        ready, _, _ = select.select([conn], [], [], 1.0)
                        if not self._generation_active(generation, stop_event):
                            break
                        if not ready:
                            continue
                        conn.poll()
                        notifications = list(conn.notifies)
                        del conn.notifies[:]
                        notifications = (
                            self._coalesce_captured_paper_iqfeed_notifications(
                                notifications
                            )
                        )
                        if self._captured_paper_scope is not None:
                            while notifications:
                                notify = notifications.pop(0)
                                if not self._generation_active(
                                    generation,
                                    stop_event,
                                ):
                                    break
                                self._handle_iqfeed_notify_payload(
                                    notify.payload,
                                    generation=generation,
                                )
                                if not self._generation_active(
                                    generation,
                                    stop_event,
                                ):
                                    break
                                # Admission can perform bounded capture/DB work.
                                # Drain the socket again after every hint so the
                                # PostgreSQL backend never waits for an entire
                                # multi-symbol admission batch to finish.  Any
                                # newer hint replaces the still-pending hint for
                                # that symbol before the next evaluation.
                                immediate_ready, _, _ = select.select(
                                    [conn],
                                    [],
                                    [],
                                    0.0,
                                )
                                if immediate_ready:
                                    conn.poll()
                                if conn.notifies:
                                    incoming = list(conn.notifies)
                                    del conn.notifies[:]
                                    notifications = (
                                        self._coalesce_captured_paper_iqfeed_notifications(
                                            notifications + incoming
                                        )
                                    )
                            continue
                        for notify in notifications:
                            if not self._generation_active(generation, stop_event):
                                break
                            if (
                                ignition_channel is not None
                                and getattr(notify, "channel", None)
                                == ignition_channel
                            ):
                                self._handle_iqfeed_ignition_payload(
                                    notify.payload,
                                    generation=generation,
                                )
                                continue
                            self._handle_iqfeed_notify_payload(
                                notify.payload,
                                generation=generation,
                            )
                except Exception as exc:
                    if self._generation_active(generation, stop_event):
                        _log.warning("[live_loop] IQFeed notify listener reconnecting after error: %s", exc)
                        # A live listener thread is not proof that PostgreSQL is
                        # still delivering this generation's channel.  Clear
                        # readiness for the entire reconnect gap and restore it
                        # only after the replacement connection executes LISTEN.
                        self._mark_iqfeed_notify_listener_failed(generation)
                        if self._captured_paper_scope is not None and not startup_ready:
                            return
                        stop_event.wait(1.0)
                finally:
                    try:
                        if conn is not None:
                            conn.close()
                    except Exception:
                        pass
        finally:
            if self._notify_thread_generation == generation:
                self._mark_iqfeed_notify_listener_failed(generation)

    # ── IGNITION nominations (tick-based early-mover; 2026-07-17) ────────────
    # The host bridge's detector NOMINATES a symbol the moment its own tape shows
    # rolling %change + $-volume + print-rate ignition (PIT-measured: PLSM/ERNA/
    # VIVS fired +1-36s from data visibility vs the 3-8 min Massive-snapshot
    # funnel). Admission remains fully guarded: this handler only feeds the same
    # admit_ross_event path used by the certified L1 flow, tagged ignition_tick.

    def _ignition_listen_channel(self, main_channel: str) -> str | None:
        """The validated ignition channel, or None when ignition is disabled.

        Captured-paper loops NEVER listen to ignition: that lane's admission
        contract is sealed to the exact v3 envelope and must stay blind to
        nomination traffic.
        """
        if self._captured_paper_scope is not None:
            return None
        if not bool(
            getattr(
                settings,
                "chili_momentum_live_runner_loop_iqfeed_ignition_enabled",
                True,
            )
        ):
            return None
        channel = str(
            getattr(
                settings,
                "chili_momentum_live_runner_loop_iqfeed_ignition_channel",
                _IGNITION_DEFAULT_CHANNEL,
            )
            or ""
        ).strip()
        if (
            _IQFEED_NOTIFY_CHANNEL_RE.fullmatch(channel) is None
            or channel == str(main_channel or "").strip()
        ):
            _log.warning(
                "[live_loop] refusing IQFeed ignition LISTEN with channel=%r",
                channel,
            )
            return None
        return channel

    def _validated_ignition_notify(self, payload: str) -> dict | None:
        """Strict shape/freshness validation for the minimal nomination payload."""
        try:
            data = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("schema") != _IGNITION_SCHEMA_VERSION:
            return None
        if data.get("source") != _IGNITION_SOURCE_TAG:
            return None
        symbol = _strict_equity_symbol(data.get("symbol"))
        if symbol is None:
            return None
        fired_at = _parse_aware_utc(data.get("fired_at"))
        if fired_at is None:
            return None
        age = (_utcnow() - fired_at).total_seconds()
        if age < -_IGNITION_FUTURE_TOLERANCE_S or age > _IGNITION_MAX_AGE_S:
            return None
        last_price = data.get("last_price")
        if (
            isinstance(last_price, bool)
            or not isinstance(last_price, (int, float))
            or not math.isfinite(float(last_price))
            or float(last_price) <= 0
        ):
            return None
        return {
            "symbol": symbol,
            "source": _IGNITION_SOURCE_TAG,
            "schema": _IGNITION_SCHEMA_VERSION,
            "fired_at": fired_at.isoformat(),
            "last_price": float(last_price),
            "pct_change_60s": data.get("pct_change_60s"),
            "dollar_vol_60s": data.get("dollar_vol_60s"),
            "prints_10s": data.get("prints_10s"),
            "bridge_run_id": data.get("bridge_run_id"),
            "connection_generation": data.get("connection_generation"),
        }

    def _ignition_admission_verdict(self, symbol: str) -> str | None:
        """Consumer-side dedup TTL + admits/minute hard cap (monotonic clocks).

        Returns ``None`` when the nomination may proceed, else the governor that
        suppressed it — the reason is persisted with the nomination so the
        governors can be derived from the traffic they themselves shape.
        """
        ttl = _ignition_dedup_ttl_seconds()
        cap = _ignition_admits_per_minute()
        now_mono = time.monotonic()
        with self._ignition_lock:
            last = self._ignition_dedup.get(symbol)
            if last is not None and now_mono - last < ttl:
                return "governor_dedup_ttl"
            self._ignition_admit_monotonic = [
                at
                for at in self._ignition_admit_monotonic
                if now_mono - at < _IGNITION_ADMIT_RATE_WINDOW_S
            ]
            if len(self._ignition_admit_monotonic) >= cap:
                return "governor_admits_per_minute"
            # Bounded memory: drop expired dedup entries opportunistically.
            if len(self._ignition_dedup) > _IGNITION_DEDUP_MAX_ENTRIES:
                self._ignition_dedup = {
                    sym: at
                    for sym, at in self._ignition_dedup.items()
                    if now_mono - at < ttl
                }
            self._ignition_dedup[symbol] = now_mono
            self._ignition_admit_monotonic.append(now_mono)
            return None

    # ── Nomination record (2026-09-04) ───────────────────────────────────────
    # 0 ross_event_admitted events across 3,379 live sessions in 14 days left NO
    # durable trace of why: a nomination was a log line in a process with no
    # logging config. Every validated nomination now lands in
    # momentum_ignition_nominations (migration 376), INCLUDING the ones this
    # loop's own governors suppressed — otherwise the governors censor exactly
    # the distribution needed to derive them.

    @staticmethod
    def _ignition_nomination_params(
        data: dict,
        *,
        received_at: datetime,
        outcome: str,
        result: dict | None = None,
    ) -> dict:
        """Bind one nomination row's parameters. Pure — no I/O, no clock."""

        def _num(value):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            value = float(value)
            return value if math.isfinite(value) else None

        def _text(value, limit: int):
            if value is None:
                return None
            return str(value)[:limit]

        prints_10s = data.get("prints_10s")
        outcome_result = result if isinstance(result, dict) else {}
        return {
            "symbol": str(data.get("symbol") or "")[:16],
            "fired_at": _parse_aware_utc(data.get("fired_at")),
            "received_at": received_at,
            "last_price": _num(data.get("last_price")),
            # FRACTION, exactly as the producer reports it (0.05 == +5%).
            "pct_change_60s": _num(data.get("pct_change_60s")),
            # SIXTY-SECOND turnover; never the day-scale tradability number.
            "dollar_vol_60s": _num(data.get("dollar_vol_60s")),
            "prints_10s": (
                int(prints_10s)
                if isinstance(prints_10s, int) and not isinstance(prints_10s, bool)
                else None
            ),
            "outcome": _text(outcome, 48) or "unknown",
            "skipped": _text(outcome_result.get("skipped"), 64),
            "ross_universe_reason": _text(
                outcome_result.get("ross_universe_reason"), 64
            ),
        }

    @staticmethod
    def _write_ignition_nomination(db, params: dict) -> bool:
        """INSERT one nomination row on the CALLER's session/transaction.

        Runs inside a SAVEPOINT so a failed observation (missing table on a
        pre-migration-376 environment, a constraint, a transient error) can never
        abort the admission transaction it shares — the same contract
        ``bridge_subscribe.request_bridge_subscription`` uses, and for the same
        reason: an observation must never break the path it observes.
        """
        try:
            with db.begin_nested():
                db.execute(
                    text(
                        "INSERT INTO momentum_ignition_nominations ("
                        "symbol, fired_at, received_at, last_price, "
                        "pct_change_60s, dollar_vol_60s, prints_10s, outcome, "
                        "skipped, ross_universe_reason) VALUES ("
                        ":symbol, :fired_at, :received_at, :last_price, "
                        ":pct_change_60s, :dollar_vol_60s, :prints_10s, "
                        ":outcome, :skipped, :ross_universe_reason)"
                    ),
                    params,
                )
            return True
        except Exception:
            _log.debug(
                "[live_loop] ignition nomination record failed symbol=%s",
                params.get("symbol"),
                exc_info=True,
            )
            return False

    def _record_ignition_nomination(
        self,
        data: dict,
        *,
        received_at: datetime,
        outcome: str,
    ) -> bool:
        """Record a nomination that never reached the admission transaction.

        The governor-suppressed and already-tracked cases have no admission
        session to ride, so they open a short-lived one of their own. Bounded by
        the producer's own 6 fires/minute cap. Never raises.
        """
        db = None
        try:
            db = SessionLocal()
            written = self._write_ignition_nomination(
                db,
                self._ignition_nomination_params(
                    data, received_at=received_at, outcome=outcome
                ),
            )
            db.commit()
            return written
        except Exception:
            _log.debug(
                "[live_loop] ignition nomination session failed symbol=%s",
                data.get("symbol"),
                exc_info=True,
            )
            try:
                if db is not None:
                    db.rollback()
            except Exception:
                pass
            return False
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def _request_ignition_bridge_subscription(self, symbol: str) -> bool:
        """Promote a newly-igniting symbol to the protected subscribe cause NOW.

        Called BEFORE the admission attempt on purpose. The host IQFeed bridge
        subscribes by polling armed sessions + the eligible-mover board on a ~20s
        refresh, so a symbol that has just ignited is not on the tape until it
        already has a viability row — the same closed loop that makes admission
        late. The hint is a NON-trading coordination row, kill-switched and
        SAVEPOINT-safe inside ``request_bridge_subscription``, and it is worth
        writing even when the admission that follows is refused: the tape is what
        lets the NEXT nomination prove itself. Never raises.
        """
        db = None
        try:
            from .bridge_subscribe import request_bridge_subscription

            db = SessionLocal()
            requested = bool(
                request_bridge_subscription(db, symbol, reason="ignition_tick")
            )
            db.commit()
            return requested
        except Exception:
            _log.debug(
                "[live_loop] ignition bridge subscribe hint failed symbol=%s",
                symbol,
                exc_info=True,
            )
            try:
                if db is not None:
                    db.rollback()
            except Exception:
                pass
            return False
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def _handle_iqfeed_ignition_payload(
        self,
        payload: str,
        *,
        generation: int | None = None,
    ) -> bool:
        owner_generation = (
            self._generation if generation is None else int(generation)
        )
        if self._captured_paper_scope is not None:
            return False
        if not self._generation_active(owner_generation):
            return False
        data = self._validated_ignition_notify(payload)
        if data is None:
            return False
        sym = data["symbol"]
        received_at = _utcnow()
        handled = False
        try:
            sessions = self._tracker.get_sessions_for_symbol(sym)
            if not sessions:
                verdict = self._ignition_admission_verdict(sym)
                if verdict is not None:
                    self._record_ignition_nomination(
                        data, received_at=received_at, outcome=verdict
                    )
                    return False
                _log.info(
                    "[live_loop] ignition nomination symbol=%s px=%s pct60=%s",
                    sym,
                    data.get("last_price"),
                    data.get("pct_change_60s"),
                )
                # SUBSCRIBE FIRST, ADMIT SECOND. The bridge only tapes symbols it
                # is subscribed to, and it discovers them by polling armed
                # sessions — so a nomination that is refused today leaves the
                # symbol untaped for the next one. The hint is cheap, non-trading
                # and kill-switched; the admission decision below is unchanged.
                self._request_ignition_bridge_subscription(sym)
                admission = self._admit_iqfeed_symbol(
                    sym,
                    data,
                    expected_generation=owner_generation,
                    nomination_received_at=received_at,
                )
                handled = bool(admission and admission.get("admitted"))
                sessions = self._tracker.get_sessions_for_symbol(sym)
            else:
                self._record_ignition_nomination(
                    data, received_at=received_at, outcome="already_tracked"
                )
            for sess in sessions:
                handled = self._dispatch(
                    int(sess["session_id"]),
                    expected_generation=owner_generation,
                ) or handled
        except Exception:
            _log.debug(
                "[live_loop] ignition notify handling failed symbol=%s",
                sym,
                exc_info=True,
            )
            handled = False
        return handled

    def _normalized_iqfeed_notify_authority(
        self,
        payload: str,
    ) -> tuple[dict, tuple] | None:
        """Purely validate and normalize the complete v3 authority tuple."""

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

    def _iqfeed_notify_candidate_is_reservable_locked(
        self,
        certified_tuple: tuple,
    ) -> bool:
        bridge_run_id = certified_tuple[5]
        generation = certified_tuple[6]
        committed_generation = self._iqfeed_generation_watermarks.get(
            bridge_run_id
        )
        if (
            committed_generation is not None
            and generation < committed_generation
        ):
            return False
        inflight_generations = (
            key[6]
            for key in self._iqfeed_inflight_certified
            if key[5] == bridge_run_id
        )
        highest_inflight_generation = max(inflight_generations, default=None)
        return bool(
            (
                highest_inflight_generation is None
                or generation >= highest_inflight_generation
            )
            and certified_tuple not in self._iqfeed_certified_watermarks
            and certified_tuple not in self._iqfeed_inflight_certified
        )

    def _iqfeed_notify_candidate_is_reservable(
        self,
        certified_tuple: tuple,
    ) -> bool:
        now_monotonic = time.monotonic()
        with self._iqfeed_provenance_lock:
            self._iqfeed_certified_watermarks = {
                key: expires_at
                for key, expires_at in self._iqfeed_certified_watermarks.items()
                if expires_at > now_monotonic
            }
            return self._iqfeed_notify_candidate_is_reservable_locked(
                certified_tuple
            )

    def _validated_iqfeed_notify(self, payload: str) -> tuple[dict, tuple] | None:
        """Validate and reserve the complete v3 tuple before admission work."""

        normalized_authority = self._normalized_iqfeed_notify_authority(payload)
        if normalized_authority is None:
            return None
        normalized, certified_tuple = normalized_authority
        now_monotonic = time.monotonic()
        with self._iqfeed_provenance_lock:
            self._iqfeed_certified_watermarks = {
                key: expires_at
                for key, expires_at in self._iqfeed_certified_watermarks.items()
                if expires_at > now_monotonic
            }
            if not self._iqfeed_notify_candidate_is_reservable_locked(
                certified_tuple
            ):
                return None
            # A short-lived concurrency reservation is not an accepted-event
            # watermark. It is removed on every failed admission/dispatch path.
            self._iqfeed_inflight_certified.add(certified_tuple)
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

    def _publish_runtime_snapshot_after_commit(self, session_id: int) -> bool:
        """Best-effort observer projection, isolated from core PAPER state."""

        scope = self._captured_paper_scope
        if scope is None:
            return False
        db = None
        try:
            from .persistence import (
                build_runtime_snapshot_values,
                upsert_trading_automation_runtime_snapshot,
            )

            db = SessionLocal()
            sess = (
                db.query(TradingAutomationSession)
                .filter(TradingAutomationSession.id == int(session_id))
                .one_or_none()
            )
            if sess is None:
                db.rollback()
                return False
            scope.assert_session(sess)
            values = build_runtime_snapshot_values(
                sess,
                last_action=str(sess.state or "captured_paper_runtime_tick"),
            )
            upsert_trading_automation_runtime_snapshot(
                db,
                session_id=int(sess.id),
                values=values,
            )
            db.commit()
            return True
        except Exception:
            try:
                if db is not None:
                    db.rollback()
            except Exception:
                pass
            _log.warning(
                "[live_loop] captured PAPER runtime observer projection failed "
                "session=%d core_state_unchanged=true",
                int(session_id),
                exc_info=True,
            )
            return False
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass

    def _sample_admission_raw_reason(self, raw: str) -> None:
        """Itabi ang HILAW na rejection reason bago ito ma-normalize.

        BAKIT (2026-08-07, unang tunay na census): 17,008 na rejection sa isang
        generation, LAHAT lumabas bilang `rejected_captured_paper_admission_rejected`
        — ang fallback ng normalizer kapag hindi tumugma ang raw string sa
        `^[a-z][a-z0-9_]{0,127}$`. Ibig sabihin, ang TUNAY na dahilan ay may
        uppercase/espasyo/simbolo (halimbawa ang mga viability blocked_reason ay
        ganito ang hugis), at ang sarili kong normalizer ang nagtago nito. Kaya
        bago mag-normalize, itinatabi ang bounded na sample ng mga hilaw na
        anyo — top-N distinct, pinutol, nilinis ang control chars — at kasama
        sila sa census emit. Pagmamasid lamang; walang desisyong nababago.
        """
        try:
            clean = "".join(
                ch if ch.isprintable() else " " for ch in str(raw)
            )
            clean = " ".join(clean.split())[:_CAPTURED_PAPER_ADMISSION_RAW_SAMPLE_LEN]
            # Collapse digit runs BEFORE the top-N bookkeeping.
            #
            # The 2026-08-07 fix above made the raw reason visible; this one keeps
            # it from crowding itself out. `ingress_rejected_sequence_{N}` embeds a
            # monotonically increasing sequence number, so every rejection mints a
            # DISTINCT key. In the 2026-08-11 r165 census that saturated the 8-key
            # cap with eight one-hit variants (…_107, _163, _218, _302, _402,
            # _1119, …) summing to 10, against a fallback bucket of 16 -- six
            # rejections vanished unseen, and the eight that survived said the same
            # thing eight times.
            #
            # A census whose cardinality is driven by an event counter degrades to
            # noise exactly when volume is highest, which is when it matters. The
            # sequence number is never the diagnosis; the reason is.
            clean = _DIGIT_RUN_RE.sub("<n>", clean)
            if not clean:
                return
            with self._iqfeed_admission_lock:
                samples = self._captured_paper_admission_raw_samples
                if clean in samples:
                    samples[clean] += 1
                elif len(samples) < _CAPTURED_PAPER_ADMISSION_RAW_SAMPLE_KEYS:
                    samples[clean] = 1
                # puno na at bago ang susi -> hindi na idinadagdag; ang bilang ng
                # fallback outcome ang nagsasabi ng kabuuan
        except Exception:  # pragma: no cover — huwag makaapekto sa admission
            _log.debug("[live_loop] raw-reason sampling failed", exc_info=True)

    def _record_captured_paper_admission_outcome(self, outcome: str) -> None:
        """Bilangin ang isang kalalabasan ng admitter at pana-panahong ilabas.

        BAKIT ITO UMIIRAL (2026-08-05). Tumakbo ang lane nang 51 minuto ng RTH,
        gumawa ng 550 selection event sa 18 symbol, at may mga pangalang umabot
        sa live_eligible NANG WALANG veto — pero ZERO ang naarm, at halos walang
        naiwang ebidensya kung bakit. Apat na landas ang tahimik na nawawala:

          1. tinatanggihan NANG WALANG reason string  -> walang log (ang tseke ay
             `... and rejection_reason`, kaya ang blangkong dahilan ay lumalampas)
          2. rate-limited: isang linya kada 60s at WALANG BILANG, kaya ang
             "tinanggihan nang 3,000 beses" at "isang beses" ay magkamukha
          3. hindi-Mapping na resulta -> `return None`, tahimik
          4. exception sa admitter  -> `_log.debug`, at sa runtime na walang
             logging config ay hindi ito lumalabas

        Kaya binibilang ang BAWAT kalalabasan at inilalabas nang may bilang. Ang
        counter ay pagmamasid lamang — walang desisyong nababago nito.
        """
        try:
            emit: dict[str, int] | None = None
            raw_samples: dict[str, int] = {}
            with self._iqfeed_admission_lock:
                self._captured_paper_admission_outcomes[outcome] = (
                    self._captured_paper_admission_outcomes.get(outcome, 0) + 1
                )
                # ANG PAGBIBILANG AY WALANG ORASAN. Tanging tuwing ika-N na
                # kalalabasan tayo bumabasa ng `time.monotonic()`. Dalawang
                # dahilan: (a) ito ay hot path — isang clock read kada admission
                # ay puro dagdag; (b) ang admission ay may mga testong nagbibigay
                # ng EKSAKTONG badyet ng monotonic na halaga, kaya ang dagdag na
                # pagbasa kada kalalabasan ay nag-e-exhaust ng kanilang iterator
                # at nagpapabago ng ugali — napatunayan: dinoble nito ang clock
                # reads at pinalitan ng StopIteration ang isang tunay na resulta.
                self._captured_paper_admission_census_ticks += 1
                due = (
                    self._captured_paper_admission_census_ticks
                    % _CAPTURED_PAPER_ADMISSION_CENSUS_TICKS
                ) == 0
                if due:
                    now = time.monotonic()
                    last = self._captured_paper_admission_census_monotonic
                    if last is None:
                        # Huwag ilabas agad; simulan ang orasan.
                        self._captured_paper_admission_census_monotonic = now
                    elif now - last >= _CAPTURED_PAPER_ADMISSION_CENSUS_INTERVAL_S:
                        self._captured_paper_admission_census_monotonic = now
                        emit = dict(self._captured_paper_admission_outcomes)
                        raw_samples = dict(self._captured_paper_admission_raw_samples)
            if emit:
                total = sum(emit.values())
                admitted = emit.get("admitted", 0)
                detail = ", ".join(
                    f"{k}={v}" for k, v in sorted(emit.items(), key=lambda kv: -kv[1])
                )
                _log.warning(
                    "[live_loop] captured PAPER admission census: %d attempt, "
                    "%d admitted | %s",
                    total,
                    admitted,
                    detail,
                )
                if raw_samples:
                    _log.warning(
                        "[live_loop] admission raw-reason samples: %s",
                        " | ".join(
                            f"({v}x) {k}" for k, v in sorted(
                                raw_samples.items(), key=lambda kv: -kv[1]
                            )
                        ),
                    )
                self._append_admission_census_sidecar(
                    emit, total, admitted, raw_samples=raw_samples
                )
        except Exception:  # pragma: no cover — hindi dapat makaapekto sa admission
            _log.debug("[live_loop] admission census failed", exc_info=True)

    def _append_admission_census_sidecar(
        self,
        outcomes: dict[str, int],
        total: int,
        admitted: int,
        raw_samples: dict[str, int] | None = None,
    ) -> None:
        """Isulat ang census sa isang DURABLE na JSONL, hindi lang sa logger.

        BAKIT MAY SIDECAR (2026-08-06). Ang log line lang ay hindi sapat: nang
        subukan kong basahin ang census sa unang RTH na may instrumentasyon,
        WALA akong mabasa. Ang tatlong service log ng captured-paper host ay may
        ZERO `[live_loop]` line kahit kailan — hindi nila nahuhuli ang logger na
        ito — at ang tanging may Python WARNING output ay tumigil isang oras
        BAGO magbukas ang market. Naglagay ako ng instrumento nang hindi
        tinitiyak na may mababasang daan palabas.

        BAKIT HINDI ANG CAPTURE STORE. Iyon sana ang unang balak, pero sarado
        ang pinto at tama lang: ang `CaptureStream` ay closed enum, at ang
        `submit_capture_health` ay nangangailangan ng EKSAKTONG key allowlist +
        tugmang identity/resource hashes + `durable_sequence_next` — isang
        sadyang non-spoofable envelope na hindi kayang magdala ng census
        counters. Ang pagpapasok nito roon ay mangangailangan ng bagong stream
        sa sealed contract at ng capture-runtime handle sa loop na ito, para sa
        isang DIAGNOSTIC. Sabi mismo ng contract: "Keep this registry explicit."

        Kaya: plain JSONL sa temp — ang parehong direktoryo na pinatunayan nang
        nasusulatan ng prosesong ito (naroon ang service-*.log nito). Cumulative
        ang counters, kaya sapat ang HULING linya kahit mamatay ang proseso.
        Binabasa ito ng `scripts/paper_lane_chain_probe.py`.
        """
        try:
            import tempfile

            path = os.path.join(tempfile.gettempdir(), "chili_admission_census.jsonl")
            record = {
                "at": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
                "total": total,
                "admitted": admitted,
                "outcomes": outcomes,
                "raw_samples": raw_samples or {},
            }
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except Exception:  # pragma: no cover — kailanman huwag sirain ang admission
            _log.debug("[live_loop] admission census sidecar failed", exc_info=True)

    def _admit_iqfeed_symbol(
        self,
        symbol: str,
        payload: dict,
        *,
        expected_generation: int,
        nomination_received_at: datetime | None = None,
    ) -> dict | None:
        if self._captured_paper_scope is not None:
            admitter = self._captured_paper_symbol_admitter
            if not callable(admitter):
                return {
                    "ok": False,
                    "admitted": False,
                    "skipped": (
                        "captured_paper_sealed_symbol_admission_unavailable"
                    ),
                    "symbol": str(symbol or "").strip().upper(),
                    "opportunity_consumed": False,
                    "risk_reserved": False,
                    "order_posted": False,
                    "broker_order_post_calls": 0,
                }
            admission_token = self._begin_iqfeed_admission(
                expected_generation,
                symbol,
            )
            if admission_token is None:
                self._record_captured_paper_admission_outcome(
                    "admission_token_unavailable"
                )
                return None
            try:
                result = admitter(symbol=symbol, payload=payload)
                if not isinstance(result, Mapping):
                    self._record_captured_paper_admission_outcome(
                        "non_mapping_result"
                    )
                    return None
                result = dict(result)
                raw_rejection_reason = result.get("reason")
                rejection_reason = (
                    raw_rejection_reason.strip()
                    if isinstance(raw_rejection_reason, str)
                    else ""
                )
                if result.get("admitted") is True:
                    self._record_captured_paper_admission_outcome("admitted")
                elif not rejection_reason:
                    # Dating LUBOS na tahimik: tinanggihan nang walang dahilan.
                    self._record_captured_paper_admission_outcome(
                        "rejected_unspecified"
                    )
                if result.get("admitted") is not True and rejection_reason:
                    raw_reason = rejection_reason
                    rejection_reason = _captured_paper_admission_reason_code(
                        raw_reason
                    )
                    if rejection_reason == "captured_paper_admission_rejected":
                        # Hindi tumama ang typed-prefix extraction — ito ang
                        # ganap na bulag na klase. Itabi ang bounded sample ng
                        # hilaw na anyo bago ito tuluyang mawala sa census.
                        self._sample_admission_raw_reason(raw_reason)
                    self._record_captured_paper_admission_outcome(
                        f"rejected_{rejection_reason}"[:96]
                    )
                    now_monotonic = time.monotonic()
                    with self._iqfeed_admission_lock:
                        last_log = (
                            self._captured_paper_admission_rejection_log_monotonic
                        )
                        emit_rejection_log = bool(
                            last_log is None
                            or now_monotonic - last_log
                            >= _CAPTURED_PAPER_ADMISSION_REJECTION_LOG_INTERVAL_S
                        )
                        if emit_rejection_log:
                            self._captured_paper_admission_rejection_log_monotonic = (
                                now_monotonic
                            )
                    if emit_rejection_log:
                        _log.info(
                            "[live_loop] captured PAPER admission rejected "
                            "symbol=%s reason=%s",
                            symbol,
                            rejection_reason,
                        )
                publish_session = bool(
                    result.get("admitted")
                    or (
                        result.get("skipped") == "already_active"
                        and int(result.get("session_id") or 0) > 0
                    )
                )
                if publish_session:
                    with self._lifecycle_lock:
                        if not self._generation_active(expected_generation):
                            return None
                        self._tracker.refresh(
                            expected_generation=expected_generation,
                        )
                    self._publish_runtime_snapshot_after_commit(
                        int(result["session_id"])
                    )
                return result
            except Exception:
                # DATING `debug`: sa runtime na walang logging config ay root
                # WARNING ang default, kaya ang pumuputok na admitter ay lubos na
                # hindi nakikita. Ito ang eksaktong hugis ng bug na nag-skip ng
                # buong scale-out step nang walang traceback ngayong araw.
                self._record_captured_paper_admission_outcome("admitter_exception")
                _log.warning(
                    "[live_loop] captured PAPER IQFeed admission FAILED symbol=%s",
                    symbol,
                    exc_info=True,
                )
                return None
            finally:
                self._finish_iqfeed_admission(admission_token)
        admission_token = self._begin_iqfeed_admission(
            expected_generation,
            symbol,
        )
        if admission_token is None:
            return None
        db = None
        try:
            from .ross_event_admission import (
                admit_ross_event,
                ignition_admission_signal,
            )

            # THE IGNITION PAYLOAD IS EVIDENCE, NOT NOISE (2026-09-04). This call
            # passed ``signal=None``, discarding the nomination's own last print
            # price, 60-second move and 60-second turnover — so the universe proof
            # had nothing but the Massive snapshot row and failed closed with
            # ross_universe_missing_price / ross_universe_missing_dollar_volume on
            # exactly the newly-igniting names the snapshot has not reached yet.
            # Measured: 0 ross_event_admitted events across 3,379 live sessions in
            # 14 days. STILL FAIL-CLOSED — a symbol that cannot prove price AND
            # dollar volume is refused exactly as before; the builder simply lets
            # the proof come from the payload plus the trade tape instead of
            # requiring a snapshot row. The v3 authority payload carries no last
            # price, so that path keeps signal=None and is byte-identical.
            ignition_signal = (
                ignition_admission_signal(payload)
                if payload.get("source") == _IGNITION_SOURCE_TAG
                else None
            )
            db = SessionLocal()
            result = admit_ross_event(
                db,
                symbol=symbol,
                signal=ignition_signal,
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
            # One row per HANDLED ignition, in the SAME transaction as the
            # admission attempt it describes: the record and the decision commit
            # or roll back together, so the table can never claim an admission
            # the DB does not hold.
            if nomination_received_at is not None:
                self._write_ignition_nomination(
                    db,
                    self._ignition_nomination_params(
                        payload,
                        received_at=nomination_received_at,
                        outcome=(
                            "admitted"
                            if result.get("admitted")
                            else str(result.get("skipped") or "refused")[:48]
                        ),
                        result=result,
                    ),
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
        db_closed = False
        phase_one_committed = False
        completion_request: CapturedPaperPostCommitRequest | None = None
        exit_completion_request: CapturedPaperExitFillPostCommitRequest | None = None
        exit_transport_request: (
            CapturedPaperExitTransportPostCommitRequest | None
        ) = None
        exit_completion_handler: (
            Callable[[CapturedPaperExitFillPostCommitRequest], Any] | None
        ) = None
        exit_transport_handler: (
            Callable[[CapturedPaperExitTransportPostCommitRequest], Any] | None
        ) = None
        refresh_session_inventory = False
        try:
            if self._captured_paper_scope is None:
                result = dispatch_live_runner_tick(db, session_id)
            else:
                from .live_runner import (
                    captured_paper_exit_runtime_authority,
                    take_captured_paper_exit_post_commit_request,
                    take_captured_paper_exit_transport_post_commit_request,
                )

                owner_generation = getattr(
                    self._worker_context,
                    "generation",
                    None,
                )
                if (
                    isinstance(owner_generation, bool)
                    or not isinstance(owner_generation, int)
                    or owner_generation <= 0
                ):
                    raise ValueError(
                        "captured PAPER loop owner generation is unavailable"
                    )
                with captured_paper_exit_runtime_authority(
                    owner_generation=owner_generation,
                    expected_account_id=(
                        self._captured_paper_scope.expected_account_id
                    ),
                    runtime_generation=(
                        self._captured_paper_scope.runtime_generation
                    ),
                    broker_connection_generation=(
                        self._captured_paper_scope.broker_connection_generation
                    ),
                ):
                    result = dispatch_captured_paper_live_runner_tick(
                        db,
                        session_id,
                        expected_account_id=(
                            self._captured_paper_scope.expected_account_id
                        ),
                        expected_runtime_generation=(
                            self._captured_paper_scope.runtime_generation
                        ),
                        expected_execution_family=(
                            self._captured_paper_scope.execution_family
                        ),
                    )
                    staged_exit_request = (
                        take_captured_paper_exit_post_commit_request(
                            int(session_id)
                        )
                    )
                    staged_exit_transport_request = (
                        take_captured_paper_exit_transport_post_commit_request(
                            int(session_id)
                        )
                    )
                    if staged_exit_request is not None:
                        if not isinstance(result, Mapping):
                            raise ValueError(
                                "captured PAPER tick staged conflicting "
                                "post-commit requests"
                            )
                        prior = result.get(
                            CAPTURED_PAPER_EXIT_FILL_POST_COMMIT_KEY
                        )
                        if prior is not None and prior != staged_exit_request:
                            raise ValueError(
                                "captured PAPER tick returned a different "
                                "exit completion request"
                            )
                        result = {
                            **dict(result),
                            CAPTURED_PAPER_EXIT_FILL_POST_COMMIT_KEY: (
                                staged_exit_request
                            ),
                        }
                    if staged_exit_transport_request is not None:
                        if not isinstance(result, Mapping):
                            raise ValueError(
                                "captured PAPER tick staged conflicting "
                                "transport requests"
                            )
                        prior = result.get(
                            CAPTURED_PAPER_EXIT_TRANSPORT_POST_COMMIT_KEY
                        )
                        if (
                            prior is not None
                            and prior != staged_exit_transport_request
                        ):
                            raise ValueError(
                                "captured PAPER tick returned a different "
                                "exit transport request"
                            )
                        result = {
                            **dict(result),
                            CAPTURED_PAPER_EXIT_TRANSPORT_POST_COMMIT_KEY: (
                                staged_exit_transport_request
                            ),
                        }
            if type(result) is CapturedPaperPostCommitRequest:
                completion_request = result
            elif isinstance(result, Mapping):
                if CAPTURED_PAPER_EXIT_FILL_POST_COMMIT_KEY in result:
                    candidate = result[CAPTURED_PAPER_EXIT_FILL_POST_COMMIT_KEY]
                    if type(candidate) is not CapturedPaperExitFillPostCommitRequest:
                        raise ValueError(
                            "captured PAPER exit completion request type is invalid"
                        )
                    exit_completion_request = candidate.verify()
                if CAPTURED_PAPER_EXIT_TRANSPORT_POST_COMMIT_KEY in result:
                    candidate = result[
                        CAPTURED_PAPER_EXIT_TRANSPORT_POST_COMMIT_KEY
                    ]
                    if (
                        type(candidate)
                        is not CapturedPaperExitTransportPostCommitRequest
                    ):
                        raise ValueError(
                            "captured PAPER exit transport request type is invalid"
                        )
                    exit_transport_request = candidate.verify()
                refresh_session_inventory = (
                    result.get("refresh_session_inventory") is True
                )
            if exit_completion_request is not None:
                scope = self._captured_paper_scope
                exit_completion_handler = (
                    self._captured_paper_exit_completion_handler
                )
                if not (
                    scope is not None
                    and callable(exit_completion_handler)
                    and exit_completion_request.session_id == int(session_id)
                    and exit_completion_request.account_scope == "alpaca:paper"
                    and exit_completion_request.expected_account_id
                    == scope.expected_account_id
                    and exit_completion_request.runtime_generation
                    == scope.runtime_generation
                    and exit_completion_request.broker_connection_generation
                    == scope.broker_connection_generation
                    and exit_completion_request.execution_family
                    == scope.execution_family
                ):
                    raise ValueError(
                        "captured PAPER exit completion authority mismatch"
                    )
            if exit_transport_request is not None:
                scope = self._captured_paper_scope
                exit_transport_handler = (
                    self._captured_paper_exit_transport_handler
                )
                if not (
                    scope is not None
                    and callable(exit_transport_handler)
                    and exit_transport_request.session_id == int(session_id)
                    and exit_transport_request.account_scope == "alpaca:paper"
                    and exit_transport_request.expected_account_id
                    == scope.expected_account_id
                    and exit_transport_request.runtime_generation
                    == scope.runtime_generation
                    and exit_transport_request.broker_connection_generation
                    == scope.broker_connection_generation
                    and exit_transport_request.execution_family
                    == scope.execution_family
                ):
                    raise ValueError(
                        "captured PAPER exit transport authority mismatch"
                    )
            db.commit()
            phase_one_committed = True
            db.close()
            db_closed = True
        except Exception as e:
            _log.debug("[live_loop] event tick session %d failed: %s", session_id, e)
            try:
                db.rollback()
            except Exception:
                pass
        else:
            if refresh_session_inventory and self._captured_paper_scope is not None:
                # The recovery transaction atomically ended an expired initial
                # generation.  Publish that terminal state before the next
                # exact Q so the symbol can be admitted again without waiting
                # for the periodic refresh loop.
                with self._lifecycle_lock:
                    generation = self._generation
                    if self._generation_active(generation, self._stop_event):
                        self._tracker.refresh(expected_generation=generation)
            if completion_request is not None:
                try:
                    dispatch_captured_paper_post_commit(completion_request)
                except Exception:
                    # Phase one is durable.  Never describe or attempt a DB
                    # rollback here; the same content-addressed request must be
                    # retried/reconciled by its completion owner.
                    _log.exception(
                        "[live_loop] captured PAPER post-commit completion failed "
                        "session=%d phase_one_committed=true retry_required=true",
                        session_id,
                    )
            if exit_transport_request is not None:
                try:
                    if not callable(exit_transport_handler):
                        raise RuntimeError(
                            "captured PAPER exit transport handler unavailable"
                        )
                    exit_transport_handler(exit_transport_request)
                except Exception:
                    _log.exception(
                        "[live_loop] captured PAPER exit transport failed "
                        "session=%d phase_one_committed=true retry_required=true",
                        session_id,
                    )
            if exit_completion_request is not None:
                try:
                    if not callable(exit_completion_handler):
                        raise RuntimeError(
                            "captured PAPER exit completion handler unavailable"
                        )
                    exit_completion_handler(exit_completion_request)
                except Exception:
                    _log.exception(
                        "[live_loop] captured PAPER exit completion failed "
                        "session=%d phase_one_committed=true retry_required=true",
                        session_id,
                    )
            if self._captured_paper_scope is not None:
                self._publish_runtime_snapshot_after_commit(session_id)
        finally:
            # Preserve the ordinary cleanup path while making it impossible to
            # imply that a committed captured phase-one write was rolled back.
            if not phase_one_committed:
                try:
                    db.rollback()
                except Exception:
                    pass
            if not db_closed:
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
    loop = get_live_runner_loop()
    if getattr(loop, "captured_paper_scope", None) is not None:
        _log.critical(
            "[live_loop] refusing ordinary start through captured PAPER owner"
        )
        return False
    return loop.start()


def start_captured_paper_live_runner_loop(
    *,
    expected_account_id: str,
    runtime_generation: str,
    broker_connection_generation: str,
    execution_family: str = EXECUTION_FAMILY_ALPACA_SPOT,
    captured_paper_symbol_admitter: Callable[..., Mapping[str, Any]] | None = None,
    captured_paper_exit_completion_handler: (
        Callable[[CapturedPaperExitFillPostCommitRequest], Any] | None
    ) = None,
    captured_paper_exit_transport_handler: (
        Callable[[CapturedPaperExitTransportPostCommitRequest], Any] | None
    ) = None,
) -> bool:
    """Start one strict account/generation/family PAPER-only loop.

    The process singleton is intentionally shared with the ordinary loop: a
    process cannot own both dispatch modes, and an already-created ordinary
    loop cannot be silently repurposed into broker-capable PAPER execution.
    """

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
            "[live_loop] refusing captured PAPER start: legacy batch driver enabled"
        )
        return False
    if not callable(captured_paper_symbol_admitter):
        _log.critical(
            "[live_loop] captured PAPER sealed symbol admission is unavailable"
        )
        return False
    if not callable(captured_paper_exit_completion_handler):
        _log.critical(
            "[live_loop] captured PAPER exit completion is unavailable"
        )
        return False
    if not callable(captured_paper_exit_transport_handler):
        _log.critical(
            "[live_loop] captured PAPER exit transport is unavailable"
        )
        return False
    try:
        scope = CapturedPaperLiveRunnerScope(
            expected_account_id=expected_account_id,
            runtime_generation=runtime_generation,
            broker_connection_generation=broker_connection_generation,
            execution_family=execution_family,
        )
    except ValueError:
        _log.critical("[live_loop] captured PAPER scope is invalid", exc_info=True)
        return False

    global _loop
    with _loop_lock:
        if _loop is None:
            try:
                _loop = LiveRunnerLoop(
                    captured_paper_scope=scope,
                    captured_paper_symbol_admitter=(
                        captured_paper_symbol_admitter
                    ),
                    captured_paper_exit_completion_handler=(
                        captured_paper_exit_completion_handler
                    ),
                    captured_paper_exit_transport_handler=(
                        captured_paper_exit_transport_handler
                    ),
                )
            except ValueError:
                _log.critical(
                    "[live_loop] captured PAPER symbol admission is invalid",
                    exc_info=True,
                )
                return False
        elif (
            _loop.captured_paper_scope != scope
            or _loop.captured_paper_symbol_admitter
            is not captured_paper_symbol_admitter
            or _loop.captured_paper_exit_completion_handler
            is not captured_paper_exit_completion_handler
            or _loop.captured_paper_exit_transport_handler
            is not captured_paper_exit_transport_handler
        ):
            _log.critical(
                "[live_loop] refusing captured PAPER start through foreign scope/admission"
            )
            return False
        selected = _loop
    return selected.start()


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


def is_captured_paper_live_runner_loop_admission_ready(
    *,
    expected_account_id: str,
    runtime_generation: str,
    broker_connection_generation: str,
    execution_family: str = EXECUTION_FAMILY_ALPACA_SPOT,
    captured_paper_exit_completion_handler: (
        Callable[[CapturedPaperExitFillPostCommitRequest], Any] | None
    ) = None,
    captured_paper_exit_transport_handler: (
        Callable[[CapturedPaperExitTransportPostCommitRequest], Any] | None
    ) = None,
) -> bool:
    """Return health only for the exact dedicated fake-money owner."""

    try:
        expected = CapturedPaperLiveRunnerScope(
            expected_account_id=expected_account_id,
            runtime_generation=runtime_generation,
            broker_connection_generation=broker_connection_generation,
            execution_family=execution_family,
        )
    except ValueError:
        return False
    return bool(
        _loop is not None
        and _loop.captured_paper_scope == expected
        and callable(captured_paper_exit_completion_handler)
        and _loop.captured_paper_exit_completion_handler
        is captured_paper_exit_completion_handler
        and callable(captured_paper_exit_transport_handler)
        and _loop.captured_paper_exit_transport_handler
        is captured_paper_exit_transport_handler
        and _loop.admission_owner_ready()
    )


def schedule_live_runner_stop_confirmation(session_id: int) -> bool:
    """Schedule a bounded stop-confirm dispatch only when the live loop is running."""
    if _loop is None:
        return False
    return _loop.schedule_stop_confirmation(int(session_id))


def schedule_live_runner_entry_continuation(session_id: int) -> bool:
    """Queue one post-commit entry FSM continuation when this loop owns live ticks."""

    if _loop is None:
        return False
    return _loop.schedule_entry_continuation(int(session_id))
