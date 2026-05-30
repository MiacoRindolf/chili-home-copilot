"""Tiny aiohttp /healthz for compose's health check.

Three orthogonal probes AND'd together for the 200/503 verdict:

* ``ws_connected`` — is the upstream Coinbase WS pipe alive at all?
  Passes if no pair has tripped its error-rate circuit breaker AND
  the L2 order-book aggregator emitted within the short window
  (default 60s). L2 emits are continuous on every active subscription
  — they don't go silent the way 1m candles do — so this is the right
  channel to use as a connectivity heartbeat.

* ``candle_freshness`` — is the candles channel still producing
  closed bars somewhere? Passes if AT LEAST ONE pair has a
  ``last_bar_at`` within the long window (default 300s). Coinbase
  legitimately emits no candles for an individual quiet pair for
  several minutes; we only care if ALL pairs go silent at once,
  which would indicate a real upstream outage.

* ``executor_learning_freshness`` - when fresh alerts are landing,
  is the paper/live executor still writing decisions to
  ``fast_executions`` or maker-attempt decisions? This catches the
  dangerous state where scanner learning looks alive upstream, but
  paper-trade learning has stalled.

Boot grace (30s after start) returns 200 unconditionally so the
snapshot replay has time to populate the in-memory state.

Why split probes instead of one tighter window: the prior single-probe
implementation used a 90s ``last_bar_at`` threshold and flapped
during low-volatility periods on Coinbase, even though WS, heartbeats,
and L2 books were all healthy. Splitting lets us detect a real WS
outage (books stop flowing) within seconds while tolerating expected
candle-channel quietness on individual pairs.

Returns 503 when any probe fails; the JSON body always includes
probe states plus age fields so an operator can see WHICH check
failed without log diving.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None  # type: ignore

logger = logging.getLogger(__name__)

# Boot grace — for the first N seconds after start, /healthz returns 200
# even if no bars have arrived yet (markets may be quiet).
BOOT_GRACE_S = 30.0

# Short window for the WS-connectivity probe. L2 books emit ~4/s/ticker
# under live load; 60s of silence across every ticker is a real outage.
WS_FRESHNESS_WINDOW_S = 60.0

# Long window for the candle-freshness probe. Coinbase's 1m candle
# channel can go quiet for 2-3 min on individual low-vol pairs; we
# only fail if NONE of the subscribed pairs has a recent bar.
CANDLE_FRESHNESS_WINDOW_S = 300.0

# Per-pair circuit-breaker threshold (matches StatusTracker default).
# A pair with this many errors in 60s is treated as ws-degraded for
# /healthz purposes; the tracker itself flips it to PAUSED.
WS_ERROR_CIRCUIT_BREAKER = 5
HEALTH_REASON_NO_SUBSCRIBED_PAIRS = "no_subscribed_pairs"
HEALTH_REASON_IDLE_NO_SUBSCRIBED_PAIRS = "idle_no_subscribed_pairs"
HEALTH_REASON_EXECUTOR_LEARNING_STALE = "executor_learning_stale"
HEALTH_SUBSCRIBED_PAIR_STATES = frozenset({"streaming", "degraded"})

FAST_LEARNING_FRESHNESS_KEY = "fast_learning_freshness"
LEARNING_LATEST_ALERT_AT_KEY = "latest_alert_at"
LEARNING_LATEST_EXECUTION_AT_KEY = "latest_execution_at"
LEARNING_LATEST_MAKER_ATTEMPT_AT_KEY = "latest_maker_attempt_at"
LEARNING_LATEST_MAKER_FILL_AT_KEY = "latest_maker_fill_at"
LEARNING_LATEST_MAKER_OUTCOME_AT_KEY = "latest_maker_outcome_at"
LEARNING_LATEST_MAKER_OUTCOME_KEY = "latest_maker_outcome"
LEARNING_LATEST_DECISION_AT_KEY = "latest_learning_decision_at"
LEARNING_LATEST_EXIT_AT_KEY = "latest_exit_at"
LEARNING_ALERT_TO_EXECUTION_LAG_S_KEY = "alert_to_execution_lag_s"
LEARNING_ALERT_TO_DECISION_LAG_S_KEY = "alert_to_learning_decision_lag_s"
LEARNING_MAKER_ATTEMPTS_WINDOW_KEY = "maker_attempts_window"
LEARNING_MAKER_CANCELS_WINDOW_KEY = "maker_cancels_window"
LEARNING_MAKER_FILLS_WINDOW_KEY = "maker_fills_window"
LEARNING_MAKER_OUTCOME_WINDOW_S_KEY = "maker_outcome_window_s"
LEARNING_MAKER_PENDING_WINDOW_KEY = "maker_pending_window"
LEARNING_MAKER_REJECTED_WINDOW_KEY = "maker_rejected_window"
LEARNING_MAKER_REPLACED_WINDOW_KEY = "maker_replaced_window"

# The executor polls fresh alerts once per second. A two-minute lag is
# deliberately loose: it avoids clock-skew / deploy flaps while still
# failing fast enough to preserve paper-learning continuity.
EXECUTOR_LEARNING_MAX_LAG_S = 120.0

# Only enforce executor freshness while the alert stream itself is active.
# Ten minutes distinguishes "scanner is quiet" from "scanner is producing
# rows and the executor is not turning them into learning decisions."
EXECUTOR_LEARNING_ACTIVE_ALERT_WINDOW_S = 600.0


class HealthzServer:
    def __init__(
        self,
        port: int,
        *,
        snapshot_fn,
    ) -> None:
        self._port = int(port)
        self._snapshot_fn = snapshot_fn
        self._runner: Any = None
        self._site: Any = None
        self._started_at = time.monotonic()

    async def start(self) -> None:
        if web is None:
            logger.critical(
                "[fast_path] aiohttp not installed — /healthz unavailable. "
                "Add `aiohttp>=3.9` to requirements."
            )
            return
        app = web.Application()
        app.router.add_get("/healthz", self._handle)
        app.router.add_get("/", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="0.0.0.0", port=self._port)
        await self._site.start()
        logger.info("[fast_path] /healthz listening on :%s", self._port)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request) -> Any:
        try:
            snap = self._snapshot_fn()
        except Exception as exc:
            return web.json_response(
                {
                    "ok": False,
                    "ws_connected": False,
                    "candle_freshness": False,
                    "executor_learning_freshness": False,
                    "reason": f"snapshot_failed:{exc}",
                },
                status=503,
            )
        ok, body = self._evaluate(snap)
        body["ok"] = bool(ok)
        return web.json_response(body, status=200 if ok else 503)

    # ── Probe evaluation ──────────────────────────────────────────────

    def _evaluate(self, snap: dict) -> tuple[bool, dict]:
        # Boot grace short-circuits — let the snapshot replay populate
        # in-memory state before we start asserting freshness.
        if (time.monotonic() - self._started_at) < BOOT_GRACE_S:
            return True, {
                "ws_connected": True,
                "candle_freshness": True,
                "executor_learning_freshness": True,
                "reason": "boot_grace",
            }

        if not snap.get("enabled", True):
            return True, {
                "ws_connected": True,
                "candle_freshness": True,
                "executor_learning_freshness": True,
                "reason": "disabled",
            }

        # DB-writer back-pressure is a fail regardless of probe state —
        # if we can't write, the snapshot we're reading from is lying.
        writer = snap.get("writer", {}) or {}
        queue_depth = int(writer.get("queue_depth") or 0)
        queue_max = int(writer.get("queue_max") or 1)
        if queue_max > 0 and (queue_depth / queue_max) > 0.9:
            return False, {
                "ws_connected": False,
                "candle_freshness": False,
                "executor_learning_freshness": False,
                "reason": f"queue_full:{queue_depth}/{queue_max}",
            }
        if int(writer.get("consecutive_batch_failures") or 0) >= 3:
            return False, {
                "ws_connected": False,
                "candle_freshness": False,
                "executor_learning_freshness": False,
                "reason": "db_write_failing",
            }

        tracked_pairs = (snap.get("status") or {}).get("pairs") or {}
        pairs = self._subscribed_pairs(tracked_pairs)
        if not pairs:
            if not tracked_pairs:
                return True, {
                    "ws_connected": True,
                    "candle_freshness": True,
                    "executor_learning_freshness": True,
                    "reason": HEALTH_REASON_IDLE_NO_SUBSCRIBED_PAIRS,
                    "details": {
                        "ws_window_s": WS_FRESHNESS_WINDOW_S,
                        "candle_window_s": CANDLE_FRESHNESS_WINDOW_S,
                        "tracked_pairs": 0,
                        "subscribed_pairs": 0,
                        "ignored_pair_states": {},
                    },
                }
            return False, {
                "ws_connected": False,
                "candle_freshness": False,
                "executor_learning_freshness": False,
                "reason": HEALTH_REASON_NO_SUBSCRIBED_PAIRS,
                "details": {
                    "ws_window_s": WS_FRESHNESS_WINDOW_S,
                    "candle_window_s": CANDLE_FRESHNESS_WINDOW_S,
                    "tracked_pairs": len(tracked_pairs),
                    "subscribed_pairs": 0,
                    "ignored_pair_states": self._pair_state_counts(tracked_pairs),
                },
            }

        ws_ok, ws_detail = self._probe_ws_connected(snap, pairs)
        candle_ok, candle_detail = self._probe_candle_freshness(pairs)
        learning_ok, learning_detail = self._probe_executor_learning_freshness(snap)

        failed_reasons = [
            label for label, failed in (
                ("ws_disconnected", not ws_ok),
                ("no_candle_freshness", not candle_ok),
                (HEALTH_REASON_EXECUTOR_LEARNING_STALE, not learning_ok),
            )
            if failed
        ]

        body = {
            "ws_connected": ws_ok,
            "candle_freshness": candle_ok,
            "executor_learning_freshness": learning_ok,
            "reason": "ok" if not failed_reasons else "+".join(failed_reasons),
            "details": {
                "ws_window_s": WS_FRESHNESS_WINDOW_S,
                "candle_window_s": CANDLE_FRESHNESS_WINDOW_S,
                "executor_learning_active_alert_window_s": (
                    EXECUTOR_LEARNING_ACTIVE_ALERT_WINDOW_S
                ),
                "executor_learning_max_lag_s": EXECUTOR_LEARNING_MAX_LAG_S,
                "tracked_pairs": len(tracked_pairs),
                "subscribed_pairs": len(pairs),
                "ignored_pair_states": self._ignored_pair_state_counts(tracked_pairs),
                **ws_detail,
                **candle_detail,
                **learning_detail,
            },
        }
        return (ws_ok and candle_ok and learning_ok), body

    @staticmethod
    def _subscribed_pairs(pairs: dict) -> dict:
        return {
            ticker: pair
            for ticker, pair in pairs.items()
            if str(pair.get("state") or "").strip().lower()
            in HEALTH_SUBSCRIBED_PAIR_STATES
        }

    @staticmethod
    def _pair_state_counts(pairs: dict) -> dict[str, int]:
        counts: dict[str, int] = {}
        for pair in pairs.values():
            state = str(pair.get("state") or "unknown").strip().lower()
            counts[state] = counts.get(state, 0) + 1
        return counts

    @classmethod
    def _ignored_pair_state_counts(cls, pairs: dict) -> dict[str, int]:
        counts = cls._pair_state_counts(pairs)
        return {
            state: count
            for state, count in counts.items()
            if state not in HEALTH_SUBSCRIBED_PAIR_STATES
        }

    def _probe_ws_connected(
        self, snap: dict, pairs: dict,
    ) -> tuple[bool, dict]:
        """Pass iff (a) no pair tripped its error-rate breaker and (b)
        the L2 book aggregator emitted within ``WS_FRESHNESS_WINDOW_S``.

        L2 book freshness is the strongest "WS pipe alive" signal we
        have — it ticks 4/s/ticker under live load and stops *immediately*
        on disconnect, unlike the candle channel which lags by minutes
        on quiet pairs.
        """
        # (a) error-rate breaker
        any_halted = any(p.get("state") == "halted" for p in pairs.values())
        if any_halted:
            return False, {"halted_pair": True}
        breached = [
            t for t, p in pairs.items()
            if int(p.get("error_count_60s") or 0) >= WS_ERROR_CIRCUIT_BREAKER
        ]
        if breached:
            return False, {"error_breaker_pairs": breached}

        # (b) L2 freshness
        book_stats = ((snap.get("ws") or {}).get("book") or {})
        last_emit = book_stats.get("last_emit_at_wall")
        if not last_emit:
            # No L2 emits yet at all — could be cold start beyond grace.
            # If we never had any pair in 'streaming', treat as warming
            # (warming_up == OK); else treat as outage.
            had_any_streaming = any(
                p.get("state") == "streaming" for p in pairs.values()
            )
            if not had_any_streaming:
                return True, {"newest_book_age_s": None, "ws_phase": "warming"}
            return False, {"newest_book_age_s": None, "ws_phase": "no_books"}

        age = self._age_seconds(last_emit)
        if age is None:
            return False, {"newest_book_age_s": None,
                           "ws_phase": "unparseable_emit_ts"}
        if age > WS_FRESHNESS_WINDOW_S:
            return False, {"newest_book_age_s": round(age, 2)}
        return True, {"newest_book_age_s": round(age, 2)}

    def _probe_candle_freshness(self, pairs: dict) -> tuple[bool, dict]:
        """Pass iff at least one pair has ``last_bar_at`` within
        ``CANDLE_FRESHNESS_WINDOW_S``.

        Coinbase's 1m candle channel is sparse: an individual quiet
        pair can produce no closed bars for 2-3 min while WS, L2,
        and other pairs all behave normally. Requiring "at least one"
        pair to be fresh tolerates that without masking a true outage
        (where ALL pairs go silent simultaneously).
        """
        had_any_bar_ever = False
        freshest_age: float | None = None
        freshest_pair: str | None = None

        for ticker, p in pairs.items():
            ts = p.get("last_bar_at")
            if not ts:
                continue
            had_any_bar_ever = True
            age = self._age_seconds(ts)
            if age is None:
                continue
            if freshest_age is None or age < freshest_age:
                freshest_age = age
                freshest_pair = ticker

        if not had_any_bar_ever:
            # No pair has ever produced a bar yet — boot grace already
            # ended; treat as warming (200) for the same reason the
            # original implementation did: silent markets are possible.
            return True, {
                "newest_bar_age_s": None,
                "freshest_pair_for_bars": None,
                "candle_phase": "warming",
            }

        if freshest_age is None or freshest_age > CANDLE_FRESHNESS_WINDOW_S:
            return False, {
                "newest_bar_age_s": (
                    round(freshest_age, 2) if freshest_age is not None else None
                ),
                "freshest_pair_for_bars": freshest_pair,
            }
        return True, {
            "newest_bar_age_s": round(freshest_age, 2),
            "freshest_pair_for_bars": freshest_pair,
        }

    def _probe_executor_learning_freshness(self, snap: dict) -> tuple[bool, dict]:
        """Pass iff active alert flow is still becoming execution rows.

        A quiet scanner is allowed. A fresh alert stream with no fresh
        ``fast_executions.decided_at`` is not, because that means paper
        learning stopped even though upstream alert learning appears alive.
        """
        freshness = snap.get(FAST_LEARNING_FRESHNESS_KEY)
        if not isinstance(freshness, dict):
            return True, {"executor_learning_phase": "snapshot_missing"}

        latest_alert_at = freshness.get(LEARNING_LATEST_ALERT_AT_KEY)
        latest_execution_at = freshness.get(LEARNING_LATEST_EXECUTION_AT_KEY)
        latest_maker_attempt_at = freshness.get(
            LEARNING_LATEST_MAKER_ATTEMPT_AT_KEY
        )
        latest_maker_fill_at = freshness.get(LEARNING_LATEST_MAKER_FILL_AT_KEY)
        latest_maker_outcome_at = freshness.get(
            LEARNING_LATEST_MAKER_OUTCOME_AT_KEY
        )
        latest_maker_outcome = freshness.get(LEARNING_LATEST_MAKER_OUTCOME_KEY)
        latest_decision_at = (
            freshness.get(LEARNING_LATEST_DECISION_AT_KEY)
            or latest_execution_at
        )
        latest_exit_at = freshness.get(LEARNING_LATEST_EXIT_AT_KEY)
        lag_s = self._coerce_float(
            freshness.get(LEARNING_ALERT_TO_DECISION_LAG_S_KEY)
        )
        execution_lag_s = self._coerce_float(
            freshness.get(LEARNING_ALERT_TO_EXECUTION_LAG_S_KEY)
        )
        if lag_s is None:
            lag_s = execution_lag_s

        detail = {
            LEARNING_LATEST_ALERT_AT_KEY: latest_alert_at,
            LEARNING_LATEST_EXECUTION_AT_KEY: latest_execution_at,
            LEARNING_LATEST_MAKER_ATTEMPT_AT_KEY: latest_maker_attempt_at,
            LEARNING_LATEST_MAKER_FILL_AT_KEY: latest_maker_fill_at,
            LEARNING_LATEST_MAKER_OUTCOME_AT_KEY: latest_maker_outcome_at,
            LEARNING_LATEST_MAKER_OUTCOME_KEY: latest_maker_outcome,
            LEARNING_LATEST_DECISION_AT_KEY: latest_decision_at,
            LEARNING_LATEST_EXIT_AT_KEY: latest_exit_at,
            LEARNING_ALERT_TO_EXECUTION_LAG_S_KEY: (
                round(execution_lag_s, 2) if execution_lag_s is not None else None
            ),
            LEARNING_ALERT_TO_DECISION_LAG_S_KEY: (
                round(lag_s, 2) if lag_s is not None else None
            ),
            LEARNING_MAKER_OUTCOME_WINDOW_S_KEY: freshness.get(
                LEARNING_MAKER_OUTCOME_WINDOW_S_KEY
            ),
            LEARNING_MAKER_ATTEMPTS_WINDOW_KEY: freshness.get(
                LEARNING_MAKER_ATTEMPTS_WINDOW_KEY
            ),
            LEARNING_MAKER_FILLS_WINDOW_KEY: freshness.get(
                LEARNING_MAKER_FILLS_WINDOW_KEY
            ),
            LEARNING_MAKER_CANCELS_WINDOW_KEY: freshness.get(
                LEARNING_MAKER_CANCELS_WINDOW_KEY
            ),
            LEARNING_MAKER_REPLACED_WINDOW_KEY: freshness.get(
                LEARNING_MAKER_REPLACED_WINDOW_KEY
            ),
            LEARNING_MAKER_REJECTED_WINDOW_KEY: freshness.get(
                LEARNING_MAKER_REJECTED_WINDOW_KEY
            ),
            LEARNING_MAKER_PENDING_WINDOW_KEY: freshness.get(
                LEARNING_MAKER_PENDING_WINDOW_KEY
            ),
        }
        maker_outcome_age_s = self._age_seconds(latest_maker_outcome_at)
        detail["latest_maker_outcome_age_s"] = (
            round(maker_outcome_age_s, 2)
            if maker_outcome_age_s is not None
            else None
        )

        if freshness.get("ok") is False:
            return False, {
                **detail,
                "executor_learning_phase": "snapshot_error",
                "executor_learning_error": str(
                    freshness.get("error") or "unknown"
                )[:240],
            }

        alert_age_s = self._age_seconds(latest_alert_at)
        if latest_alert_at and alert_age_s is None:
            return False, {
                **detail,
                "latest_alert_age_s": None,
                "executor_learning_phase": "unparseable_alert_timestamp",
            }
        if alert_age_s is None:
            return True, {
                **detail,
                "latest_alert_age_s": None,
                "executor_learning_phase": "no_alerts",
            }

        detail["latest_alert_age_s"] = round(alert_age_s, 2)
        if alert_age_s > EXECUTOR_LEARNING_ACTIVE_ALERT_WINDOW_S:
            return True, {
                **detail,
                "executor_learning_phase": "alert_stream_quiet",
            }

        decision_age_s = self._age_seconds(latest_decision_at)
        if lag_s is None and decision_age_s is not None:
            lag_s = max(0.0, decision_age_s - alert_age_s)
            detail[LEARNING_ALERT_TO_DECISION_LAG_S_KEY] = round(lag_s, 2)
        detail["latest_learning_decision_age_s"] = (
            round(decision_age_s, 2) if decision_age_s is not None else None
        )

        if not latest_decision_at:
            return False, {
                **detail,
                "executor_learning_phase": "no_learning_decisions",
            }
        if decision_age_s is None:
            return False, {
                **detail,
                "executor_learning_phase": "unparseable_decision_timestamp",
            }
        if lag_s is None:
            return False, {
                **detail,
                "executor_learning_phase": "unmeasurable_decision_lag",
            }
        if lag_s > EXECUTOR_LEARNING_MAX_LAG_S:
            return False, {
                **detail,
                "executor_learning_phase": "stale_learning_decision",
            }
        return True, {**detail, "executor_learning_phase": "ok"}

    @staticmethod
    def _age_seconds(ts: Any) -> float | None:
        """Convert an ISO8601 string or datetime into "seconds ago",
        or None if unparseable. Times are treated as naive UTC to match
        the producers (status_tracker / order_book write naive UTC)."""
        try:
            t = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        except (TypeError, ValueError):
            return None
        if not isinstance(t, datetime):
            return None
        if t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return (now - t).total_seconds()

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


__all__ = [
    "BOOT_GRACE_S",
    "EXECUTOR_LEARNING_ACTIVE_ALERT_WINDOW_S",
    "EXECUTOR_LEARNING_MAX_LAG_S",
    "FAST_LEARNING_FRESHNESS_KEY",
    "HEALTH_REASON_EXECUTOR_LEARNING_STALE",
    "HEALTH_REASON_NO_SUBSCRIBED_PAIRS",
    "HEALTH_SUBSCRIBED_PAIR_STATES",
    "HealthzServer",
    "LEARNING_ALERT_TO_DECISION_LAG_S_KEY",
    "LEARNING_ALERT_TO_EXECUTION_LAG_S_KEY",
    "LEARNING_LATEST_DECISION_AT_KEY",
    "LEARNING_LATEST_ALERT_AT_KEY",
    "LEARNING_LATEST_EXECUTION_AT_KEY",
    "LEARNING_LATEST_EXIT_AT_KEY",
    "LEARNING_LATEST_MAKER_ATTEMPT_AT_KEY",
    "LEARNING_LATEST_MAKER_FILL_AT_KEY",
    "LEARNING_LATEST_MAKER_OUTCOME_AT_KEY",
    "LEARNING_LATEST_MAKER_OUTCOME_KEY",
    "LEARNING_MAKER_ATTEMPTS_WINDOW_KEY",
    "LEARNING_MAKER_CANCELS_WINDOW_KEY",
    "LEARNING_MAKER_FILLS_WINDOW_KEY",
    "LEARNING_MAKER_OUTCOME_WINDOW_S_KEY",
    "LEARNING_MAKER_PENDING_WINDOW_KEY",
    "LEARNING_MAKER_REJECTED_WINDOW_KEY",
    "LEARNING_MAKER_REPLACED_WINDOW_KEY",
]
