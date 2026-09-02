"""Lane-health FROZEN alert for the momentum/trading lane.

A tripped safety breaker silently empties the lane: the auto-arm pass short-circuits
(``skipped="kill_switch"`` / a per-broker daily-loss block) and the only visible trace
is a 6ms ``phase=ok`` tick with no ``[scheduler] auto_arm:`` line. On 2026-06-15 the
global daily-loss kill switch tripped at 05:18 ET and the momentum lane sat empty all
day before the operator caught it — a frozen SAFETY state is silent.

This module makes a frozen lane LOUD:

* :func:`evaluate_lane_health` — pure, read-only assessment (no side effects). Reused by
  the cockpit P&L rollup so the autopilot band shows the freeze live.
* :func:`run_lane_health_check` — the periodic scheduler hook. When the lane has been
  frozen past the (adaptive) grace window it emits ``logger.critical("[lane_health]
  FROZEN ...")`` AND writes an audit row to ``trading_alerts`` — change-only with a
  re-remind cooldown so a long freeze keeps nagging without spamming.

Frozen conditions:
  (a) the GLOBAL kill switch has been active longer than the grace window;
  (b) a PER-BROKER daily-loss block is set (and held past grace);
  (c) the lane is enabled + expected to be trading, but the auto-arm pass / scheduler
      is not actually executing (job wedged or scheduler-worker down) — distinct from a
      legitimately quiet market, where the pass keeps firing every tick (it just finds
      no setup) and the heartbeats stay fresh.

Threshold N is ADAPTIVE: it derives from the lane's OWN watch cadence (auto_arm
max_watch + watch_extend) — no separate magic number — with a single optional override
knob. Reversible: ``CHILI_LANE_HEALTH_ALERT_ENABLED=0`` disables the alert entirely.

See [[project_per_broker_daily_loss]] (the 06-15 incident) and
[[project_autopilot_money_cockpit]] (where the operator actually looks).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from ....config import settings
from ....models.trading import BrainBatchJob
from ..batch_job_constants import (
    IQFEED_DRAIN_METRICS_HEARTBEAT_SCHEMA,
    IQFEED_DRAIN_METRICS_HEARTBEAT_SCOPE,
    IQFEED_EXACT_PRINT_HEARTBEAT_SCHEMA,
    IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
    JOB_IQFEED_DRAIN_METRICS_HEARTBEAT,
    JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
    JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
)

logger = logging.getLogger(__name__)


# Durable event-loop heartbeat contract.  This is deliberately a CONTROL-PLANE
# signal: it proves that the generation-owned tracker refresh and price-bus callback
# attachment completed.  It does not claim quote/feed freshness or that a broker
# worker finished an individual tick; those need their own data/worker watchdogs.
LIVE_LOOP_HEARTBEAT_SCHEMA_V1 = "momentum_live_loop_control_heartbeat_v1"
LIVE_LOOP_HEARTBEAT_SCHEMA_V2 = "momentum_live_loop_control_heartbeat_v2"
# Compatibility alias for generic/non-captured callers and existing persisted rows.
LIVE_LOOP_HEARTBEAT_SCHEMA = LIVE_LOOP_HEARTBEAT_SCHEMA_V1
LIVE_LOOP_HEARTBEAT_SCOPE = "tracker_refresh_and_callback_registration"
LIVE_LOOP_HEARTBEAT_INTERVAL_SECONDS = 30.0
_LIVE_LOOP_HEARTBEAT_MIN_STALE_SECONDS = 2.0 * LIVE_LOOP_HEARTBEAT_INTERVAL_SECONDS
_LIVE_LOOP_HEARTBEAT_MAX_STALE_SECONDS = 300.0
_LIVE_LOOP_HEARTBEAT_HISTORY_LIMIT = 64
_LIVE_LOOP_HANDOFF_CLOCK_TOLERANCE_SECONDS = 2.0
_LIVE_LOOP_HEARTBEAT_V1_META_KEYS = frozenset(
    {
        "schema",
        "scope",
        "owner",
        "owner_instance_id",
        "generation",
        "generation_identity",
        "generation_started_at_utc",
    }
)
_LIVE_LOOP_HEARTBEAT_V2_META_KEYS = frozenset(
    {
        *_LIVE_LOOP_HEARTBEAT_V1_META_KEYS,
        "account_scope",
        "expected_account_id",
        "runtime_generation",
        "execution_family",
        "live_cash_authorized",
        "row_started_at_utc",
        "heartbeat_at_utc",
        "content_sha256",
    }
)
_IQFEED_EXACT_PRINT_META_KEYS = frozenset(
    {
        "schema",
        "scope",
        "symbol",
        "observed_at_utc",
        "provider_event_at_utc",
        "received_at_utc",
        "available_at_utc",
        "bridge_version",
        "bridge_run_id",
        "bridge_run_started_at_utc",
        "connection_generation",
        "source_frame_sequence",
        "source_frame_sha256",
        "committed_print_count",
        "content_sha256",
    }
)
_IQFEED_DRAIN_METRICS_META_KEYS = frozenset(
    {
        "schema",
        "scope",
        "window_started_at_utc",
        "window_ended_at_utc",
        "bridge_version",
        "bridge_run_id",
        "connection_generation",
        "pending_trades",
        "pending_quotes",
        "oldest_pending_received_age_s",
        "batches",
        # A batch that exhausted the write chain is counted in ``batches`` and
        # its (slowest) wall is inside the percentiles; this says how many of
        # them there were.
        "failed_batches",
        "events_per_s",
        "batch_p50_ms",
        "batch_p90_ms",
        "batch_max_ms",
        "insert_execute_p50_ms",
        # SUM over the receipt window, not a per-batch cost -- the name says so.
        "release_total_ms",
        "notify_count",
        "write_mode",
        # Per-window DELTAS, so two consecutive receipts are diffable.
        "write_mode_fallbacks_window",
        "commit_in_doubt_window",
        "pending_bound_drops",
        "content_sha256",
    }
)
_IQFEED_EXACT_PRINT_FUTURE_TOLERANCE_SECONDS = 2.0
_IQFEED_EXACT_PRINT_CLOCK_TOLERANCE_SECONDS = 1.0
_IQFEED_EXACT_PRINT_HISTORY_LIMIT = 64

# Process-start floor: a fresh scheduler process has no in-process auto-arm heartbeat
# yet, so "stalled" must be measured from when this module loaded, not from epoch.
_MODULE_LOADED_MONOTONIC = time.monotonic()

# In-process heartbeat: the auto-arm scheduler job stamps this every time it actually
# executes a pass. A healthy-but-quiet lane keeps stamping; a wedged/dead job goes
# stale. Lives in the SAME process as the lane-health job (both are scheduler jobs).
_auto_arm_last_run_monotonic: float | None = None
_auto_arm_last_run_wall: datetime | None = None
_heartbeat_lock = threading.Lock()

# Change-only / cooldown state for the loud side effects.
_alert_lock = threading.Lock()
_last_alert_signature: str | None = None
_last_alert_at_monotonic: float | None = None


def record_auto_arm_run() -> None:
    """Stamp the auto-arm execution heartbeat. Called by the auto-arm scheduler job at
    the top of each pass so lane-health can tell a wedged job from a quiet market."""
    global _auto_arm_last_run_monotonic, _auto_arm_last_run_wall
    with _heartbeat_lock:
        _auto_arm_last_run_monotonic = time.monotonic()
        _auto_arm_last_run_wall = datetime.utcnow()


def _heartbeat_content_sha256(meta: dict[str, Any]) -> str:
    body = dict(meta)
    body.pop("content_sha256", None)
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_live_runner_loop_run(
    db,
    *,
    owner_instance_id: str,
    generation: int,
    generation_started_at: datetime,
    account_scope: str | None = None,
    expected_account_id: str | None = None,
    runtime_generation: str | None = None,
    execution_family: str | None = None,
    live_cash_authorized: bool | None = None,
) -> str:
    """Stage one completed, cross-process event-loop heartbeat.

    The live-loop owner commits this row only after its generation has successfully
    refreshed the tracker and attached the price-bus callbacks.  The caller owns the
    transaction so it can re-check generation ownership immediately before commit.
    """
    owner_instance_id = str(owner_instance_id or "").strip().lower()
    try:
        parsed_owner = uuid.UUID(owner_instance_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("live-loop heartbeat requires a canonical owner UUID") from exc
    if str(parsed_owner) != owner_instance_id:
        raise ValueError("live-loop heartbeat requires a canonical owner UUID")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ValueError("live-loop heartbeat requires a positive owner generation")
    if not isinstance(generation_started_at, datetime):
        raise ValueError("live-loop heartbeat requires a generation start timestamp")
    if (
        generation_started_at.tzinfo is None
        or generation_started_at.utcoffset() != timezone.utc.utcoffset(generation_started_at)
    ):
        raise ValueError("live-loop generation start must be timezone-aware UTC")
    generation_started_at = generation_started_at.astimezone(timezone.utc)
    generation_identity = f"{owner_instance_id}:{generation}"
    meta = {
        "schema": LIVE_LOOP_HEARTBEAT_SCHEMA_V1,
        "scope": LIVE_LOOP_HEARTBEAT_SCOPE,
        "owner": "momentum_live_runner_loop",
        "owner_instance_id": owner_instance_id,
        "generation": generation,
        "generation_identity": generation_identity,
        "generation_started_at_utc": (
            generation_started_at.isoformat().replace("+00:00", "Z")
        ),
    }
    captured_values = (
        account_scope,
        expected_account_id,
        runtime_generation,
        execution_family,
        live_cash_authorized,
    )
    if any(value is not None for value in captured_values):
        if any(value is None for value in captured_values):
            raise ValueError(
                "captured PAPER heartbeat identity must be supplied atomically"
            )
        try:
            canonical_account_id = str(uuid.UUID(str(expected_account_id or "")))
            canonical_runtime_generation = str(
                uuid.UUID(str(runtime_generation or ""))
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                "captured PAPER heartbeat identity UUID is invalid"
            ) from exc
        if (
            str(expected_account_id or "").strip().lower()
            != canonical_account_id
            or str(runtime_generation or "").strip().lower()
            != canonical_runtime_generation
            or account_scope != "alpaca:paper"
            or execution_family != "alpaca_spot"
            or live_cash_authorized is not False
        ):
            raise ValueError("captured PAPER heartbeat identity is not exact")
        meta.update(
            {
                "schema": LIVE_LOOP_HEARTBEAT_SCHEMA_V2,
                "account_scope": "alpaca:paper",
                "expected_account_id": canonical_account_id,
                "runtime_generation": canonical_runtime_generation,
                "execution_family": "alpaca_spot",
                "live_cash_authorized": False,
            }
        )
    from ..brain_batch_job_log import brain_batch_job_record_completed

    job_id = brain_batch_job_record_completed(
        db,
        JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
        ok=True,
        meta=meta,
    )
    # ``brain_batch_job_finish`` is intentionally best-effort for generic batch
    # error reporting and may swallow a retry/missing-row failure.  A live-owner
    # heartbeat cannot inherit that contract: startup must not turn green unless
    # the exact completed row is present in this transaction and can be flushed.
    db.flush()
    row = (
        db.query(BrainBatchJob)
        .populate_existing()
        .filter(
            BrainBatchJob.id == job_id,
            BrainBatchJob.job_type == JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
        )
        .one_or_none()
    )
    if row is not None and meta["schema"] == LIVE_LOOP_HEARTBEAT_SCHEMA_V2:
        row_started_at = _utc_naive_datetime(getattr(row, "started_at", None))
        heartbeat_at = _utc_naive_datetime(getattr(row, "ended_at", None))
        if row_started_at is not None and heartbeat_at is not None:
            meta = {
                **meta,
                "row_started_at_utc": (
                    row_started_at.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
                "heartbeat_at_utc": (
                    heartbeat_at.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
            }
            meta["content_sha256"] = _heartbeat_content_sha256(meta)
            row.meta_json = meta
            db.flush()
    persisted = _validated_live_loop_heartbeat_row(row) if row is not None else None
    expected_started_at = generation_started_at.replace(tzinfo=None)
    if (
        persisted is None
        or persisted.get("generation_identity") != generation_identity
        or persisted.get("generation_started_at") != expected_started_at
        or (
            meta["schema"] == LIVE_LOOP_HEARTBEAT_SCHEMA_V2
            and (
                persisted.get("captured_paper") is not True
                or persisted.get("account_scope") != meta["account_scope"]
                or persisted.get("expected_account_id")
                != meta["expected_account_id"]
                or persisted.get("runtime_generation")
                != meta["runtime_generation"]
                or persisted.get("execution_family")
                != meta["execution_family"]
                or persisted.get("live_cash_authorized") is not False
                or persisted.get("content_sha256") != meta["content_sha256"]
            )
        )
    ):
        raise RuntimeError(
            "live-loop heartbeat writer did not persist the exact completed row"
        )
    return job_id


def _auto_arm_heartbeat_age_seconds() -> float | None:
    """Seconds since the auto-arm pass last executed in THIS process, or None if it has
    never run yet (then the caller uses the module-load floor instead)."""
    with _heartbeat_lock:
        mono = _auto_arm_last_run_monotonic
    if mono is None:
        return None
    return max(0.0, time.monotonic() - mono)


def _alert_enabled() -> bool:
    return bool(getattr(settings, "chili_lane_health_alert_enabled", True))


def freeze_grace_seconds() -> float:
    """Adaptive grace before a held safety state counts as FROZEN.

    A positive ``chili_lane_health_freeze_alert_seconds`` overrides. Otherwise derive
    from the lane's own patience: one full auto-arm watch-and-extend cycle (max_watch +
    watch_extend). If a breaker has held longer than the lane would wait on a single
    candidate, the lane has skipped a complete arming cycle — it is frozen, not paused.
    No separate magic number; the threshold tracks the lane's configured cadence.
    """
    override = float(getattr(settings, "chili_lane_health_freeze_alert_seconds", 0.0) or 0.0)
    if override > 0:
        return override
    max_watch = float(getattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 300) or 300)
    extend = float(getattr(settings, "chili_momentum_auto_arm_watch_extend_seconds", 600) or 600)
    return max(60.0, max_watch + extend)


def live_loop_stale_seconds() -> float:
    """Tight timeout for the 30-second event-loop control heartbeat.

    This must stay independent from the entry watch-and-extend patience used by
    :func:`freeze_grace_seconds`: a held position cannot wait 15 minutes for a dead
    exit owner to become visible.
    """
    try:
        configured = float(
            getattr(
                settings,
                "chili_lane_health_live_loop_stale_seconds",
                75.0,
            )
            or 75.0
        )
    except (TypeError, ValueError):
        configured = 75.0
    return min(
        _LIVE_LOOP_HEARTBEAT_MAX_STALE_SECONDS,
        max(_LIVE_LOOP_HEARTBEAT_MIN_STALE_SECONDS, configured),
    )


def live_runner_driver_configuration() -> tuple[str | None, str | None]:
    """Return ``(mode, error)`` for the configured live-session owner.

    New-entry admission may be intentionally paused while the event loop still owns
    exits for held sessions.  Admission flags therefore cannot disable owner-health
    monitoring.  A master-enabled ambiguous/broker-dark configuration is an explicit
    frozen condition, never equivalent to an intentionally disabled lane.
    """
    if not bool(getattr(settings, "chili_momentum_live_runner_enabled", False)):
        return None, None
    batch_on = bool(getattr(settings, "chili_momentum_live_runner_scheduler_enabled", False))
    loop_on = bool(getattr(settings, "chili_momentum_live_runner_loop_enabled", False))
    if batch_on and loop_on:
        return None, "live_runner_batch_and_event_loop_both_enabled"
    if not batch_on and not loop_on:
        return None, "live_runner_no_driver_enabled"
    if loop_on:
        if not bool(getattr(settings, "chili_autopilot_price_bus_enabled", False)):
            return None, "live_runner_event_loop_price_bus_disabled"
        return "event_loop", None
    return "scheduled_auto_arm", None


def _lane_driver_configuration() -> tuple[str | None, str | None]:
    """Backward-compatible private alias for the shared posture contract."""
    return live_runner_driver_configuration()


def _lane_driver_mode() -> str | None:
    mode, _error = _lane_driver_configuration()
    return mode


def _lane_enabled() -> bool:
    return bool(getattr(settings, "chili_momentum_live_runner_enabled", False))


def _expected_trading_window_open() -> bool:
    """Is the lane expected to be able to arm RIGHT NOW? Crypto-inclusive lanes trade
    24/7; an equity-only lane only during tradeable equity hours. Fail-open (assume
    expected) so a profile/clock hiccup never SUPPRESSES a real freeze."""
    try:
        equity_only = bool(getattr(settings, "chili_momentum_auto_arm_equity_only", False))
        crypto_only = bool(getattr(settings, "chili_momentum_auto_arm_crypto_only", True))
        if crypto_only or not equity_only:
            return True  # crypto present -> always expected
        from .market_profile import is_tradeable_now

        return bool(is_tradeable_now("SPY"))
    except Exception:
        return True


def _auto_arm_user_id() -> int | None:
    return getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )


def _human(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m" + (f"{sec}s" if sec else "")
    h, m = divmod(m, 60)
    return f"{h}h" + (f"{m}m" if m else "")


def _parse_iso(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "").replace("+00:00", "")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _utc_naive_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _strict_aware_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
    ):
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _validated_iqfeed_drain_metrics_row(row: Any) -> dict[str, Any] | None:
    """Validate one IQFeed writer-drain telemetry receipt.

    Deliberately a SEPARATE job type and key set from the exact-print
    heartbeat: that body is matched key-set-exactly and its content_sha256
    covers the body, so adding keys there would make every exact-print receipt
    unparseable and take the tape-liveness diagnostic dark.  This receipt
    carries the one number the committed-row receipt cannot express -- the age
    of the OLDEST UNWRITTEN arrival still sitting in the bridge's deques.
    """

    if str(getattr(row, "status", "") or "").strip().lower() != "ok":
        return None
    started_at = _utc_naive_datetime(getattr(row, "started_at", None))
    ended_at = _utc_naive_datetime(getattr(row, "ended_at", None))
    meta = getattr(row, "meta_json", None)
    if (
        started_at is None
        or ended_at is None
        or ended_at < started_at
        or not isinstance(meta, dict)
        or set(meta) != _IQFEED_DRAIN_METRICS_META_KEYS
        or meta.get("schema") != IQFEED_DRAIN_METRICS_HEARTBEAT_SCHEMA
        or meta.get("scope") != IQFEED_DRAIN_METRICS_HEARTBEAT_SCOPE
    ):
        return None
    supplied_sha256 = str(meta.get("content_sha256") or "").strip()
    if (
        len(supplied_sha256) != 64
        or supplied_sha256 != supplied_sha256.lower()
        or any(char not in "0123456789abcdef" for char in supplied_sha256)
        or supplied_sha256 != _heartbeat_content_sha256(meta)
    ):
        return None
    try:
        bridge_run_id = str(uuid.UUID(str(meta.get("bridge_run_id") or "")))
    except (AttributeError, TypeError, ValueError):
        return None
    window_started_at = _strict_aware_utc(meta.get("window_started_at_utc"))
    window_ended_at = _strict_aware_utc(meta.get("window_ended_at_utc"))
    if (
        window_started_at is None
        or window_ended_at is None
        or window_ended_at < window_started_at
    ):
        return None
    numeric: dict[str, float] = {}
    for key in (
        "oldest_pending_received_age_s",
        "events_per_s",
        "batch_p50_ms",
        "batch_p90_ms",
        "batch_max_ms",
        "insert_execute_p50_ms",
        "release_total_ms",
    ):
        value = meta.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric[key] = float(value)
    counters: dict[str, int] = {}
    for key in (
        "pending_trades",
        "pending_quotes",
        "batches",
        "failed_batches",
        "notify_count",
        "write_mode_fallbacks_window",
        "commit_in_doubt_window",
        "pending_bound_drops",
        "connection_generation",
    ):
        value = meta.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        counters[key] = value
    write_mode = str(meta.get("write_mode") or "").strip()
    if write_mode not in ("copy", "execute_values", "values"):
        return None
    return {
        "bridge_run_id": bridge_run_id,
        "bridge_version": str(meta.get("bridge_version") or "").strip(),
        "window_started_at": window_started_at,
        "window_ended_at": window_ended_at,
        "write_mode": write_mode,
        **counters,
        **numeric,
    }


def latest_iqfeed_drain_metrics(db) -> dict[str, Any] | None:
    """Newest parseable writer-drain receipt, or ``None`` when there is none."""

    rows = (
        db.query(BrainBatchJob)
        .filter(BrainBatchJob.job_type == JOB_IQFEED_DRAIN_METRICS_HEARTBEAT)
        .order_by(BrainBatchJob.started_at.desc(), BrainBatchJob.id.desc())
        .limit(8)
        .all()
    )
    for row in rows:
        parsed = _validated_iqfeed_drain_metrics_row(row)
        if parsed is not None:
            return parsed
    return None


def _validated_exact_iqfeed_print_row(row: Any) -> dict[str, Any] | None:
    """Validate one append-only receipt for a committed exact IQFeed print."""

    if str(getattr(row, "status", "") or "").strip().lower() != "ok":
        return None
    started_at = _utc_naive_datetime(getattr(row, "started_at", None))
    ended_at = _utc_naive_datetime(getattr(row, "ended_at", None))
    meta = getattr(row, "meta_json", None)
    if (
        started_at is None
        or ended_at is None
        or ended_at < started_at
        or not isinstance(meta, dict)
        or set(meta) != _IQFEED_EXACT_PRINT_META_KEYS
        or meta.get("schema") != IQFEED_EXACT_PRINT_HEARTBEAT_SCHEMA
        or meta.get("scope") != IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE
    ):
        return None

    supplied_sha256 = str(meta.get("content_sha256") or "").strip()
    source_frame_sha256 = str(meta.get("source_frame_sha256") or "").strip()
    if (
        len(supplied_sha256) != 64
        or supplied_sha256 != supplied_sha256.lower()
        or any(char not in "0123456789abcdef" for char in supplied_sha256)
        or supplied_sha256 != _heartbeat_content_sha256(meta)
        or len(source_frame_sha256) != 64
        or source_frame_sha256 != source_frame_sha256.lower()
        or any(char not in "0123456789abcdef" for char in source_frame_sha256)
    ):
        return None

    symbol = str(meta.get("symbol") or "").strip().upper()
    bridge_version = str(meta.get("bridge_version") or "").strip()
    try:
        bridge_run_id = str(uuid.UUID(str(meta.get("bridge_run_id") or "")))
    except (AttributeError, TypeError, ValueError):
        return None
    connection_generation = meta.get("connection_generation")
    source_frame_sequence = meta.get("source_frame_sequence")
    committed_print_count = meta.get("committed_print_count")
    if (
        not symbol
        or len(symbol) > 16
        or not bridge_version
        or str(meta.get("bridge_run_id") or "").strip().lower() != bridge_run_id
        or isinstance(connection_generation, bool)
        or not isinstance(connection_generation, int)
        or connection_generation <= 0
        or isinstance(source_frame_sequence, bool)
        or not isinstance(source_frame_sequence, int)
        or source_frame_sequence <= 0
        or isinstance(committed_print_count, bool)
        or not isinstance(committed_print_count, int)
        or committed_print_count <= 0
    ):
        return None

    observed_at = _strict_aware_utc(meta.get("observed_at_utc"))
    provider_event_at = _strict_aware_utc(meta.get("provider_event_at_utc"))
    received_at = _strict_aware_utc(meta.get("received_at_utc"))
    available_at = _strict_aware_utc(meta.get("available_at_utc"))
    bridge_run_started_at = _strict_aware_utc(
        meta.get("bridge_run_started_at_utc")
    )
    clock_tolerance = timedelta(
        seconds=_IQFEED_EXACT_PRINT_CLOCK_TOLERANCE_SECONDS
    )
    if (
        observed_at is None
        or provider_event_at is None
        or received_at is None
        or available_at is None
        or bridge_run_started_at is None
        or observed_at != provider_event_at
        or provider_event_at > received_at + clock_tolerance
        or received_at > available_at
        or bridge_run_started_at > ended_at + clock_tolerance
    ):
        return None
    return {
        "row_started_at": started_at,
        "heartbeat_at": ended_at,
        "symbol": symbol,
        "observed_at": observed_at,
        "provider_event_at": provider_event_at,
        "received_at": received_at,
        "available_at": available_at,
        "bridge_version": bridge_version,
        "bridge_run_id": bridge_run_id,
        "bridge_run_started_at": bridge_run_started_at,
        "connection_generation": connection_generation,
        "source_frame_sequence": source_frame_sequence,
        "source_frame_sha256": source_frame_sha256,
        "committed_print_count": committed_print_count,
        "content_sha256": supplied_sha256,
        "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
    }


def _exact_iqfeed_print_receipt_has_tape_row(db, truth: dict[str, Any]) -> bool:
    """Bind the receipt to one indexed, phase-two-released exact tape row."""

    provider_event_at = truth["provider_event_at"].replace(tzinfo=timezone.utc)
    received_at = truth["received_at"].replace(tzinfo=timezone.utc)
    available_at = truth["available_at"].replace(tzinfo=timezone.utc)
    return bool(
        db.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM iqfeed_trade_ticks "
                "WHERE symbol = :symbol AND observed_at = :observed_at "
                "AND provider_event_at = :provider_event_at "
                "AND received_at = :received_at "
                "AND available_at = :available_at "
                "AND timestamp_basis = 'iqfeed_selected_trade_date_timems_exact' "
                "AND bridge_version = :bridge_version "
                "AND bridge_run_id = :bridge_run_id "
                "AND connection_generation = :connection_generation "
                "AND source_frame_sequence = :source_frame_sequence "
                "AND source_frame_sha256 = :source_frame_sha256 "
                "LIMIT 1)"
            ),
            {
                "symbol": truth["symbol"],
                "observed_at": truth["observed_at"],
                "provider_event_at": provider_event_at,
                "received_at": received_at,
                "available_at": available_at,
                "bridge_version": truth["bridge_version"],
                "bridge_run_id": truth["bridge_run_id"],
                "connection_generation": truth["connection_generation"],
                "source_frame_sequence": truth["source_frame_sequence"],
                "source_frame_sha256": truth["source_frame_sha256"],
            },
        ).scalar_one()
    )


def _latest_exact_iqfeed_print_status(db) -> dict[str, Any]:
    """Validate latest exact print, its tape row, and singleton bridge ownership."""

    rows = (
        db.query(BrainBatchJob)
        .filter(BrainBatchJob.job_type == JOB_IQFEED_EXACT_PRINT_HEARTBEAT)
        .order_by(BrainBatchJob.started_at.desc(), BrainBatchJob.id.desc())
        .limit(_IQFEED_EXACT_PRINT_HISTORY_LIMIT)
        .all()
    )
    if not rows:
        return {
            "ok": False,
            "reason": "iqfeed_exact_print_missing",
            "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
        }
    latest_row = rows[0]
    status = str(getattr(latest_row, "status", "") or "").strip().lower()
    if status == "running" or getattr(latest_row, "ended_at", None) is None:
        return {
            "ok": False,
            "reason": "iqfeed_exact_print_latest_unfinished",
            "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
        }
    if status != "ok":
        return {
            "ok": False,
            "reason": "iqfeed_exact_print_latest_error",
            "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
        }
    truth = _validated_exact_iqfeed_print_row(latest_row)
    if truth is None:
        return {
            "ok": False,
            "reason": "iqfeed_exact_print_latest_malformed",
            "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
        }
    if not _exact_iqfeed_print_receipt_has_tape_row(db, truth):
        return {
            "ok": False,
            "reason": "iqfeed_exact_print_tape_row_missing",
            "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
        }

    tolerance = timedelta(seconds=_LIVE_LOOP_HANDOFF_CLOCK_TOLERANCE_SECONDS)
    for row in rows[1:]:
        other = _validated_exact_iqfeed_print_row(row)
        if other is None or other["bridge_run_id"] == truth["bridge_run_id"]:
            continue
        overlap_start = max(
            truth["bridge_run_started_at"], other["bridge_run_started_at"]
        )
        overlap_end = min(truth["heartbeat_at"], other["heartbeat_at"])
        if overlap_end - overlap_start > tolerance:
            return {
                "ok": False,
                "reason": "iqfeed_exact_print_bridge_owner_overlap",
                "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
                "overlapping_owner_count": 2,
            }
    return {"ok": True, **truth}


def _iqfeed_data_session_open() -> bool:
    """Fail open so a clock/profile fault cannot suppress a real tape outage."""

    try:
        from .market_profile import is_data_session_now

        return bool(is_data_session_now(None))
    except Exception:
        logger.warning(
            "[lane_health] IQFeed data-session clock probe failed",
            exc_info=True,
        )
        return True


def _iqfeed_data_session_age_seconds() -> float | None:
    """Return elapsed current-session time; fail open after the normal grace."""

    if not _iqfeed_data_session_open():
        return None
    try:
        from . import market_profile

        now_utc = datetime.now(timezone.utc)
        local = now_utc.astimezone(market_profile._NY_TZ)
        open_minute = int(market_profile._data_session_open_min())
        opened = local.replace(
            hour=open_minute // 60,
            minute=open_minute % 60,
            second=0,
            microsecond=0,
        )
        return max(0.0, (local - opened).total_seconds())
    except Exception:
        logger.warning(
            "[lane_health] IQFeed data-session age probe failed",
            exc_info=True,
        )
        return iqfeed_print_stale_seconds()


def iqfeed_print_stale_seconds() -> float:
    """Reuse the entry tape-age contract, retaining a loud floor if it is disabled."""

    try:
        configured = float(
            getattr(settings, "chili_momentum_arm_tape_freshness_max_sec", 180.0)
        )
    except (TypeError, ValueError):
        configured = 180.0
    if configured <= 0:
        configured = 180.0
    return min(900.0, max(60.0, configured))


def _equity_exact_print_tape_condition(
    db,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    """Return one loud exact-print failure independently of PAPER ownership."""

    equity_only = bool(
        getattr(settings, "chili_momentum_auto_arm_equity_only", False)
    )
    crypto_only = bool(
        getattr(settings, "chili_momentum_auto_arm_crypto_only", True)
    )
    if not equity_only or crypto_only:
        return None
    stale_seconds = iqfeed_print_stale_seconds()
    session_age = _iqfeed_data_session_age_seconds()
    if session_age is None or session_age < stale_seconds:
        return None
    try:
        tape_truth = _latest_exact_iqfeed_print_status(db)
    except Exception:
        logger.exception("[lane_health] exact IQFeed print receipt read failed")
        tape_truth = {
            "ok": False,
            "reason": "iqfeed_exact_print_unreadable",
            "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
        }
    if tape_truth.get("ok") is not True:
        return {
            "kind": "equity_tape_stalled",
            "reason": str(
                tape_truth.get("reason") or "iqfeed_exact_print_unreadable"
            ),
            "scope": (
                tape_truth.get("scope") or IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE
            ),
            "since_utc": None,
            "elapsed_seconds": round(session_age, 1),
            "elapsed_human": _human(session_age),
            "stale_seconds": round(stale_seconds, 1),
            "frozen": True,
            **(
                {"overlapping_owner_count": tape_truth["overlapping_owner_count"]}
                if tape_truth.get("overlapping_owner_count") is not None
                else {}
            ),
        }

    provider_event_at = _utc_naive_datetime(tape_truth.get("provider_event_at"))
    received_at = _utc_naive_datetime(tape_truth.get("received_at"))
    available_at = _utc_naive_datetime(tape_truth.get("available_at"))
    if provider_event_at is None or received_at is None or available_at is None:
        reason = "iqfeed_exact_print_latest_malformed"
        elapsed = session_age
        since = None
    else:
        # The caller snapshots `now` at the top of a multi-probe sweep that can
        # run for >10s, while the receipt above was fetched just now. On a
        # healthy tape (commits every ~1s) the latest available_at is then
        # almost always AHEAD of the stale snapshot, tripping the future guard
        # and freezing entries precisely when the feed is at its best. Anchor
        # the age math to a clock read taken after the fetch; keep the caller's
        # `now` only as a floor so a backwards host-clock step still trips the
        # future guard.
        anchor = max(now, datetime.utcnow())
        provider_age = (anchor - provider_event_at).total_seconds()
        available_age = (anchor - available_at).total_seconds()
        if provider_age < -_IQFEED_EXACT_PRINT_FUTURE_TOLERANCE_SECONDS:
            reason = "iqfeed_exact_print_provider_future"
            elapsed = provider_age
            since = provider_event_at
        elif available_age < -_IQFEED_EXACT_PRINT_FUTURE_TOLERANCE_SECONDS:
            reason = "iqfeed_exact_print_available_future"
            elapsed = available_age
            since = available_at
        elif provider_age >= stale_seconds:
            reason = "iqfeed_exact_print_provider_stale"
            elapsed = provider_age
            since = provider_event_at
        elif available_age >= stale_seconds:
            reason = "iqfeed_exact_print_persistence_stale"
            elapsed = available_age
            since = available_at
        else:
            return None
    return {
        "kind": "equity_tape_stalled",
        "reason": reason,
        "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
        "since_utc": since.isoformat() + "Z" if since is not None else None,
        "elapsed_seconds": round(elapsed, 1),
        "elapsed_human": _human(elapsed),
        "stale_seconds": round(stale_seconds, 1),
        "provider_event_at_utc": (
            provider_event_at.isoformat() + "Z"
            if provider_event_at is not None
            else None
        ),
        "received_at_utc": (
            received_at.isoformat() + "Z" if received_at is not None else None
        ),
        "available_at_utc": (
            available_at.isoformat() + "Z" if available_at is not None else None
        ),
        "frozen": True,
    }


def _validated_live_loop_heartbeat_row(row: Any) -> dict[str, Any] | None:
    """Validate the exact durable control-heartbeat schema for one completed row."""
    if str(getattr(row, "status", "") or "").strip().lower() != "ok":
        return None
    started_at = _utc_naive_datetime(getattr(row, "started_at", None))
    ended_at = _utc_naive_datetime(getattr(row, "ended_at", None))
    meta = getattr(row, "meta_json", None)
    if started_at is None or ended_at is None or not isinstance(meta, dict):
        return None
    schema = meta.get("schema")
    if schema == LIVE_LOOP_HEARTBEAT_SCHEMA_V1:
        if set(meta) != _LIVE_LOOP_HEARTBEAT_V1_META_KEYS:
            return None
        captured_paper = False
        captured_identity: dict[str, Any] = {}
    elif schema == LIVE_LOOP_HEARTBEAT_SCHEMA_V2:
        if set(meta) != _LIVE_LOOP_HEARTBEAT_V2_META_KEYS:
            return None
        supplied_sha256 = str(meta.get("content_sha256") or "").strip()
        if (
            len(supplied_sha256) != 64
            or supplied_sha256 != supplied_sha256.lower()
            or any(char not in "0123456789abcdef" for char in supplied_sha256)
            or supplied_sha256 != _heartbeat_content_sha256(meta)
        ):
            return None
        try:
            expected_account_id = str(
                uuid.UUID(str(meta.get("expected_account_id") or ""))
            )
            runtime_generation = str(
                uuid.UUID(str(meta.get("runtime_generation") or ""))
            )
        except (AttributeError, TypeError, ValueError):
            return None
        bound_started_at = _strict_aware_utc(meta.get("row_started_at_utc"))
        bound_heartbeat_at = _strict_aware_utc(meta.get("heartbeat_at_utc"))
        if (
            str(meta.get("expected_account_id") or "").strip().lower()
            != expected_account_id
            or str(meta.get("runtime_generation") or "").strip().lower()
            != runtime_generation
            or meta.get("account_scope") != "alpaca:paper"
            or meta.get("execution_family") != "alpaca_spot"
            or meta.get("live_cash_authorized") is not False
            or bound_started_at != started_at
            or bound_heartbeat_at != ended_at
        ):
            return None
        captured_paper = True
        captured_identity = {
            "account_scope": "alpaca:paper",
            "expected_account_id": expected_account_id,
            "runtime_generation": runtime_generation,
            "execution_family": "alpaca_spot",
            "live_cash_authorized": False,
            "content_sha256": supplied_sha256,
        }
    else:
        return None
    if (
        meta.get("scope") != LIVE_LOOP_HEARTBEAT_SCOPE
        or meta.get("owner") != "momentum_live_runner_loop"
    ):
        return None
    owner_instance_id = str(meta.get("owner_instance_id") or "").strip().lower()
    try:
        parsed_owner = uuid.UUID(owner_instance_id)
    except (AttributeError, TypeError, ValueError):
        return None
    if str(parsed_owner) != owner_instance_id:
        return None
    generation = meta.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        return None
    generation_identity = str(meta.get("generation_identity") or "").strip()
    if generation_identity != f"{owner_instance_id}:{generation}":
        return None
    generation_started_at = _strict_aware_utc(
        meta.get("generation_started_at_utc")
    )
    if generation_started_at is None:
        return None
    tolerance = timedelta(seconds=_LIVE_LOOP_HANDOFF_CLOCK_TOLERANCE_SECONDS)
    if (
        ended_at + tolerance < started_at
        or started_at + tolerance < generation_started_at
    ):
        return None
    return {
        "heartbeat_at": ended_at,
        "row_started_at": started_at,
        "generation_started_at": generation_started_at,
        "owner_instance_id": owner_instance_id,
        "generation": generation,
        "generation_identity": generation_identity,
        "scope": LIVE_LOOP_HEARTBEAT_SCOPE,
        "schema": schema,
        "captured_paper": captured_paper,
        **captured_identity,
    }


def _latest_live_loop_heartbeat_status(
    db,
    *,
    stale_seconds: float,
) -> dict[str, Any]:
    """Read the latest exact heartbeat and reject ambiguous concurrent owners.

    The latest row is authoritative even when it is malformed/error/unfinished; an
    older successful row must never hide a broken current writer.  For valid rows,
    generation intervals make a clean handoff (old heartbeat before new start) distinct
    from two owners whose independently completed heartbeat intervals overlap.
    """
    rows = (
        db.query(BrainBatchJob)
        .filter(BrainBatchJob.job_type == JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT)
        .order_by(BrainBatchJob.started_at.desc(), BrainBatchJob.id.desc())
        .limit(_LIVE_LOOP_HEARTBEAT_HISTORY_LIMIT)
        .all()
    )
    if not rows:
        return {
            "ok": False,
            "reason": "live_runner_loop_heartbeat_missing",
            "scope": LIVE_LOOP_HEARTBEAT_SCOPE,
        }

    latest_row = rows[0]
    latest_status = str(getattr(latest_row, "status", "") or "").strip().lower()
    if latest_status == "running" or getattr(latest_row, "ended_at", None) is None:
        return {
            "ok": False,
            "reason": "live_runner_loop_heartbeat_latest_unfinished",
            "scope": LIVE_LOOP_HEARTBEAT_SCOPE,
        }
    if latest_status != "ok":
        return {
            "ok": False,
            "reason": "live_runner_loop_heartbeat_latest_error",
            "scope": LIVE_LOOP_HEARTBEAT_SCOPE,
        }
    latest = _validated_live_loop_heartbeat_row(latest_row)
    if latest is None:
        return {
            "ok": False,
            "reason": "live_runner_loop_heartbeat_latest_malformed",
            "scope": LIVE_LOOP_HEARTBEAT_SCOPE,
        }

    generations: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = _validated_live_loop_heartbeat_row(row)
        if record is None:
            continue
        identity = record["generation_identity"]
        group = generations.get(identity)
        if group is None:
            generations[identity] = dict(record)
            continue
        group["generation_started_at"] = min(
            group["generation_started_at"],
            record["generation_started_at"],
        )
        group["heartbeat_at"] = max(
            group["heartbeat_at"],
            record["heartbeat_at"],
        )

    latest_identity = latest["generation_identity"]
    latest_group = generations.get(latest_identity, latest)
    recency_floor = latest["heartbeat_at"] - timedelta(seconds=float(stale_seconds))
    tolerance = timedelta(seconds=_LIVE_LOOP_HANDOFF_CLOCK_TOLERANCE_SECONDS)
    overlapping: list[str] = []
    for identity, other in generations.items():
        if identity == latest_identity or other["heartbeat_at"] < recency_floor:
            continue
        overlap_start = max(
            latest_group["generation_started_at"],
            other["generation_started_at"],
        )
        overlap_end = min(
            latest_group["heartbeat_at"],
            other["heartbeat_at"],
        )
        if overlap_end - overlap_start > tolerance:
            overlapping.append(identity)
    if overlapping:
        return {
            "ok": False,
            "reason": "live_runner_loop_owner_overlap",
            "heartbeat_at": latest["heartbeat_at"],
            "scope": LIVE_LOOP_HEARTBEAT_SCOPE,
            "overlapping_owner_count": len(overlapping) + 1,
        }
    return {"ok": True, **latest}


def _latest_live_loop_heartbeat_at(db) -> datetime | None:
    """Compatibility wrapper for callers that only need the exact timestamp."""
    truth = _latest_live_loop_heartbeat_status(
        db,
        stale_seconds=live_loop_stale_seconds(),
    )
    if truth.get("reason") == "live_runner_loop_heartbeat_missing":
        return None
    if truth.get("ok") is not True:
        raise ValueError(str(truth.get("reason") or "live-loop heartbeat unreadable"))
    return truth["heartbeat_at"]


def live_runner_loop_control_health(
    db,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fail-closed durable health truth for live entry/exit ownership.

    Scheduler admission and cockpit lane health intentionally share this exact
    read.  That prevents a red/stale/overlapping control heartbeat from remaining
    merely informational while a later auto-arm pass continues adding new risk.
    """
    checked_at = _utc_naive_datetime(now) if now is not None else datetime.utcnow()
    if checked_at is None:
        checked_at = datetime.utcnow()
    stale_after = live_loop_stale_seconds()
    try:
        truth = _latest_live_loop_heartbeat_status(
            db,
            stale_seconds=stale_after,
        )
    except Exception:
        logger.debug(
            "[lane_health] durable live-loop heartbeat probe failed",
            exc_info=True,
        )
        truth = {
            "ok": False,
            "reason": "live_runner_loop_heartbeat_unreadable",
            "scope": LIVE_LOOP_HEARTBEAT_SCOPE,
        }

    heartbeat_at = _utc_naive_datetime(truth.get("heartbeat_at"))
    heartbeat_age = (
        (checked_at - heartbeat_at).total_seconds()
        if heartbeat_at is not None
        else None
    )
    reason = (
        None
        if truth.get("ok") is True
        else str(
            truth.get("reason")
            or "live_runner_loop_heartbeat_unreadable"
        )
    )
    if reason is None:
        if heartbeat_age is None:
            reason = "live_runner_loop_heartbeat_latest_malformed"
        elif heartbeat_age < -_LIVE_LOOP_HANDOFF_CLOCK_TOLERANCE_SECONDS:
            reason = "live_runner_loop_heartbeat_future"
        elif heartbeat_age >= stale_after:
            reason = "live_runner_loop_heartbeat_stale"

    return {
        **truth,
        "ok": reason is None,
        "reason": reason,
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age,
        "stale_seconds": stale_after,
        "scope": truth.get("scope") or LIVE_LOOP_HEARTBEAT_SCOPE,
    }


def captured_paper_live_runner_control_health(
    db,
    *,
    expected_account_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return exact, fresh captured-PAPER heartbeat truth for read-only observers.

    Generic v1 rows remain valid for legacy lane-health monitoring, but cannot prove
    that the isolated Alpaca PAPER runtime is active.
    """
    try:
        canonical_account_id = str(uuid.UUID(str(expected_account_id or "")))
    except (AttributeError, TypeError, ValueError):
        canonical_account_id = ""
    if canonical_account_id != str(expected_account_id or "").strip().lower():
        return {
            "ok": False,
            "reason": "captured_paper_heartbeat_expected_account_invalid",
            "scope": LIVE_LOOP_HEARTBEAT_SCOPE,
        }
    truth = live_runner_loop_control_health(db, now=now)
    if truth.get("ok") is not True:
        return truth
    if (
        truth.get("schema") != LIVE_LOOP_HEARTBEAT_SCHEMA_V2
        or truth.get("captured_paper") is not True
    ):
        return {
            **truth,
            "ok": False,
            "reason": "captured_paper_heartbeat_generic",
        }
    if truth.get("expected_account_id") != canonical_account_id:
        return {
            **truth,
            "ok": False,
            "reason": "captured_paper_heartbeat_account_mismatch",
        }
    return truth


def evaluate_lane_health(db, *, user_id: int | None = None) -> dict[str, Any]:
    """Read-only assessment of whether the momentum lane is frozen.

    Returns a structured dict (always — never raises): ``frozen``, ``severity``,
    ``headline``, ``detail``, ``conditions`` (each with kind/since/elapsed), plus the
    grace window. Conditions active-but-within-grace are reported with ``frozen=False``
    so the cockpit can show "armed to alert" without crying wolf.
    """
    now = datetime.utcnow()
    grace = freeze_grace_seconds()
    enabled = _alert_enabled()
    out: dict[str, Any] = {
        "enabled": enabled,
        "frozen": False,
        "severity": "ok",
        "headline": None,
        "detail": None,
        "conditions": [],
        "grace_seconds": round(grace, 1),
        "live_loop_stale_seconds": round(live_loop_stale_seconds(), 1),
        "iqfeed_print_stale_seconds": round(iqfeed_print_stale_seconds(), 1),
        "as_of_utc": now.isoformat() + "Z",
    }
    if not enabled:
        return out

    conditions: list[dict[str, Any]] = []

    # (a) Global kill switch.
    try:
        from ..governance import get_kill_switch_status

        ks = get_kill_switch_status()
        if ks.get("active"):
            since = _parse_iso(ks.get("set_at"))
            elapsed = (now - since).total_seconds() if since else None
            conditions.append({
                "kind": "kill_switch",
                "reason": ks.get("reason") or "kill_switch_active",
                "since_utc": (since.isoformat() + "Z") if since else None,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
                "elapsed_human": _human(elapsed),
                # No set_at (e.g. manual switch before restore) -> treat as frozen now,
                # erring loud: a held global halt is exactly the silent-freeze case.
                "frozen": (elapsed is None) or (elapsed >= grace),
            })
    except Exception:
        logger.debug("[lane_health] kill-switch probe failed", exc_info=True)

    # (b) Per-broker daily-loss blocks (the 06-15 Coinbase-sized cap incident).
    try:
        from ..governance import (
            BROKER_DAILY_LOSS_FAMILIES,
            get_broker_daily_loss_block,
            is_broker_daily_loss_blocked,
        )

        for fam in BROKER_DAILY_LOSS_FAMILIES:
            if not is_broker_daily_loss_blocked(fam):
                continue
            blk = get_broker_daily_loss_block(fam) or {}
            since = _parse_iso(blk.get("set_at"))
            elapsed = (now - since).total_seconds() if since else None
            conditions.append({
                "kind": "broker_block",
                "family": fam,
                "reason": blk.get("reason") or f"broker_daily_loss_block_{fam}",
                "realized": blk.get("realized"),
                "limit": blk.get("limit"),
                "since_utc": (since.isoformat() + "Z") if since else None,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
                "elapsed_human": _human(elapsed),
                "frozen": (elapsed is None) or (elapsed >= grace),
            })
    except Exception:
        logger.debug("[lane_health] per-broker block probe failed", exc_info=True)

    # (c) The configured entry/exit owner is absent, ambiguous, or stale.  A safety
    # breaker pauses NEW risk; it does not make the process that owns held-position
    # exits optional.  Owner health is therefore additive to breaker conditions.
    driver_mode, driver_error = _lane_driver_configuration()
    if driver_error is not None:
        conditions.append({
            "kind": "driver_misconfigured",
            "reason": driver_error,
            "since_utc": None,
            "elapsed_seconds": None,
            "elapsed_human": _human(None),
            "frozen": True,
        })
    elif driver_mode == "event_loop":
        # The loop is a continuous exit owner, including overnight and while entry
        # admission is paused.  Do not gate this on an ARM trading-hours predicate.
        heartbeat_truth = live_runner_loop_control_health(db, now=now)
        heartbeat_at = _utc_naive_datetime(heartbeat_truth.get("heartbeat_at"))
        heartbeat_age = heartbeat_truth.get("heartbeat_age_seconds")
        reason = (
            None
            if heartbeat_truth.get("ok") is True
            else str(
                heartbeat_truth.get("reason")
                or "live_runner_loop_heartbeat_unreadable"
                )
            )
        if reason is not None:
            conditions.append({
                "kind": "live_loop_stalled",
                "reason": reason,
                "scope": heartbeat_truth.get("scope") or LIVE_LOOP_HEARTBEAT_SCOPE,
                "since_utc": (
                    heartbeat_at.isoformat() + "Z"
                    if heartbeat_at is not None
                    else None
                ),
                "elapsed_seconds": (
                    round(heartbeat_age, 1)
                    if heartbeat_age is not None
                    else None
                ),
                "elapsed_human": _human(heartbeat_age),
                "frozen": True,
                **(
                    {
                        "overlapping_owner_count": heartbeat_truth[
                            "overlapping_owner_count"
                        ]
                    }
                    if heartbeat_truth.get("overlapping_owner_count") is not None
                    else {}
                ),
            })

    elif driver_mode == "scheduled_auto_arm" and _expected_trading_window_open():
        # Legacy batch ownership remains trading-window scoped.  Breakers do not
        # suppress its scheduler health signal.
        scheduler_fault = False
        try:
            from .automation_query import _latest_scheduler_heartbeat_at

            hb = _latest_scheduler_heartbeat_at(db)
            hb_age = (now - hb).total_seconds() if hb else None
            if hb is None or (hb_age is not None and hb_age >= grace):
                scheduler_fault = True
                conditions.append({
                    "kind": "scheduler_down",
                    "reason": (
                        "scheduler_worker_heartbeat_missing"
                        if hb is None
                        else "scheduler_worker_stale"
                    ),
                    "since_utc": (hb.isoformat() + "Z") if hb else None,
                    "elapsed_seconds": round(hb_age, 1) if hb_age is not None else None,
                    "elapsed_human": _human(hb_age),
                    "frozen": True,
                })
        except Exception:
            logger.debug("[lane_health] scheduler-heartbeat probe failed", exc_info=True)

        auto_arm_expected = bool(
            getattr(settings, "chili_momentum_auto_arm_live_enabled", True)
            and getattr(
                settings,
                "chili_momentum_auto_arm_live_scheduler_enabled",
                True,
            )
        )
        if not scheduler_fault and auto_arm_expected:
            aa_age = _auto_arm_heartbeat_age_seconds()
            if aa_age is None:
                uptime = time.monotonic() - _MODULE_LOADED_MONOTONIC
                if uptime >= grace:
                    conditions.append({
                        "kind": "auto_arm_stalled",
                        "reason": "auto_arm_never_ran",
                        "since_utc": None,
                        "elapsed_seconds": round(uptime, 1),
                        "elapsed_human": _human(uptime),
                        "frozen": True,
                    })
            elif aa_age >= grace:
                conditions.append({
                    "kind": "auto_arm_stalled",
                    "reason": "auto_arm_pass_not_executing",
                    "since_utc": None,
                    "elapsed_seconds": round(aa_age, 1),
                    "elapsed_human": _human(aa_age),
                    "frozen": True,
                })

    # Tape health is operationally independent of the PAPER process.  A dead or
    # disabled live runner must never make an equity print outage silent.
    tape_condition = _equity_exact_print_tape_condition(db, now=now)
    if tape_condition is not None:
        conditions.append(tape_condition)

    out["conditions"] = conditions
    frozen = [c for c in conditions if c.get("frozen")]
    if frozen:
        out["frozen"] = True
        out["severity"] = "critical"
        out["headline"] = _headline_for(frozen)
        out["detail"] = _detail_for(frozen)
    return out


def _headline_for(frozen: list[dict[str, Any]]) -> str:
    c = frozen[0]
    kind = c.get("kind")
    extra = f" (+{len(frozen) - 1} more)" if len(frozen) > 1 else ""
    if kind == "kill_switch":
        return f"MOMENTUM LANE FROZEN — global kill switch active {c.get('elapsed_human')}{extra}"
    if kind == "broker_block":
        return f"MOMENTUM LANE FROZEN — {c.get('family')} daily-loss block {c.get('elapsed_human')}{extra}"
    if kind == "scheduler_down":
        return f"MOMENTUM LANE FROZEN — scheduler worker not running ({c.get('elapsed_human')}){extra}"
    if kind == "auto_arm_stalled":
        return f"MOMENTUM LANE FROZEN — auto-arm pass not executing ({c.get('elapsed_human')}){extra}"
    if kind == "live_loop_stalled":
        return f"MOMENTUM LANE FROZEN — live event-loop control plane unhealthy ({c.get('elapsed_human')}){extra}"
    if kind == "equity_tape_stalled":
        return f"MOMENTUM LANE FROZEN - committed IQFeed prints stale ({c.get('elapsed_human')}){extra}"
    if kind == "driver_misconfigured":
        return f"MOMENTUM LANE FROZEN — live driver misconfigured{extra}"
    return f"MOMENTUM LANE FROZEN ({len(frozen)} condition(s))"


def _detail_for(frozen: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for c in frozen:
        kind = c.get("kind")
        if kind == "kill_switch":
            parts.append(f"kill_switch[{c.get('reason')}] {c.get('elapsed_human')}")
        elif kind == "broker_block":
            r, lim = c.get("realized"), c.get("limit")
            money = (
                f" realized=${float(r):.2f} vs -${float(lim):.0f}"
                if isinstance(r, (int, float)) and isinstance(lim, (int, float))
                else ""
            )
            parts.append(f"{c.get('family')} block[{c.get('reason')}]{money} {c.get('elapsed_human')}")
        elif kind == "scheduler_down":
            parts.append(f"scheduler {c.get('reason')} {c.get('elapsed_human')}")
        elif kind == "auto_arm_stalled":
            parts.append(f"auto_arm {c.get('reason')} {c.get('elapsed_human')}")
        elif kind == "live_loop_stalled":
            parts.append(
                f"live_loop_control[{c.get('reason')}] {c.get('elapsed_human')}"
            )
        elif kind == "equity_tape_stalled":
            parts.append(
                f"equity_tape[{c.get('reason')}] {c.get('elapsed_human')}"
            )
        elif kind == "driver_misconfigured":
            parts.append(f"driver[{c.get('reason')}]")
    tail = (
        " — new entries halted (exits stay live); reset via the kill-switch runbook "
        "or wait for the ET-day roll if it is a daily-loss cap."
    )
    return "; ".join(parts) + tail


def _signature(frozen: list[dict[str, Any]]) -> str:
    """Identity of the frozen state (kind+reason+family), stable across the elapsed
    growing — so we re-emit on a STATE change, not on every tick."""
    return "|".join(
        sorted(f"{c.get('kind')}:{c.get('family', '')}:{c.get('reason')}" for c in frozen)
    )


def _write_alert_row(db, *, headline: str, detail: str, signature: str) -> None:
    """Append a durable audit row to trading_alerts (the notification log). A bare
    insert — does NOT itself dispatch SMS/email (that is a separate delivery path)."""
    try:
        from ....models.trading import AlertHistory
        from ..alerts import _existing_user_id_for_alert

        row = AlertHistory(
            # A stale/default operator id must not make the durable alert vanish
            # behind the broad notification guard. ``user_id`` is nullable, so
            # preserve the audit row even when the configured principal is not
            # present in this database generation.
            user_id=_existing_user_id_for_alert(db, _auto_arm_user_id()),
            alert_type="lane_health_frozen",
            ticker=None,
            message=(headline + " — " + detail)[:4000],
            sent_via="cockpit",
            success=True,
            content_signature=("lane_health:" + signature)[:512],
        )
        db.add(row)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("[lane_health] could not write audit row", exc_info=True)


def run_lane_health_check(db) -> dict[str, Any]:
    """Periodic hook: evaluate + emit the LOUD signal when frozen.

    Change-only with a re-remind cooldown (reuses the grace window): emit when the
    frozen state first appears, when its identity changes, or when the cooldown elapses
    while still frozen — so an 8h freeze keeps nagging instead of going silent after
    one line. Logs a recovery line (and resets) when the lane un-freezes.
    """
    global _last_alert_signature, _last_alert_at_monotonic

    result = evaluate_lane_health(db)
    if not result.get("enabled"):
        return result

    frozen = [c for c in result.get("conditions", []) if c.get("frozen")]
    if not frozen:
        with _alert_lock:
            was = _last_alert_signature
            _last_alert_signature = None
            _last_alert_at_monotonic = None
        if was is not None:
            logger.warning("[lane_health] RECOVERED — momentum lane no longer frozen (was: %s)", was)
        return result

    sig = _signature(frozen)
    cooldown = freeze_grace_seconds()
    now_mono = time.monotonic()
    with _alert_lock:
        changed = sig != _last_alert_signature
        cooled = (
            _last_alert_at_monotonic is None
            or (now_mono - _last_alert_at_monotonic) >= cooldown
        )
        should_emit = changed or cooled
        if should_emit:
            _last_alert_signature = sig
            _last_alert_at_monotonic = now_mono

    if should_emit:
        headline = result.get("headline") or "MOMENTUM LANE FROZEN"
        detail = result.get("detail") or ""
        logger.critical("[lane_health] FROZEN %s | %s", headline, detail)
        _write_alert_row(db, headline=headline, detail=detail, signature=sig)
        result["emitted"] = True
    else:
        result["emitted"] = False
    return result
