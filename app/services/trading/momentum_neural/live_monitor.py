"""Read-only live observer for the replay research surface.

The observer never calls a broker or market-data provider. It reads bounded runtime,
audit, outcome, fill, and quote-tape records that the trading processes already persist.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import (
    BrainBatchJob,
    MomentumAutomationOutcome,
    Trade,
    TradingAutomationEvent,
    TradingAutomationRuntimeSnapshot,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
)
from ..batch_job_constants import JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT

logger = logging.getLogger(__name__)


LIVE_MONITOR_STATE_TTL_SECONDS = 4.5
LIVE_MONITOR_CHART_TTL_SECONDS = 15.0
LIVE_MONITOR_SESSION_SCAN_LIMIT = 96
LIVE_MONITOR_SYMBOL_LIMIT = 24
LIVE_MONITOR_EVENT_LIMIT = 400
LIVE_MONITOR_EVENTS_PER_SYMBOL = 10
LIVE_MONITOR_QUOTE_ROWS_PER_SYMBOL = 120
LIVE_MONITOR_CAPTURED_ROUTE_ROW_LIMIT = 1024
LIVE_MONITOR_CAPTURED_WATCH_STALE_SECONDS = 600.0

_CAPTURED_HEARTBEAT_SCHEMA = "momentum_live_loop_control_heartbeat_v2"
_LEGACY_HEARTBEAT_SCHEMA = "momentum_live_loop_control_heartbeat_v1"
_CAPTURED_HEARTBEAT_SCOPE = "tracker_refresh_and_callback_registration"
_CAPTURED_HEARTBEAT_META_KEYS = frozenset(
    {
        "schema",
        "scope",
        "owner",
        "owner_instance_id",
        "generation",
        "generation_identity",
        "generation_started_at_utc",
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
_CAPTURED_PROVENANCE_KEY = "captured_paper_selection_producer"
_CAPTURED_PROVENANCE_KEYS = frozenset(
    {
        "schema_version",
        "account_scope",
        "expected_account_id",
        "activation_generation",
        "authority_sha256",
        "policy_sha256",
        "settings_projection_sha256",
        "code_build_sha256",
        "variant_set_sha256",
        "variant_id",
        "batch_sha256",
        "observation_sha256",
        "source_name",
        "source_generation",
        "source_sequence",
        "queue_receipt_sha256",
        "coverage_receipt_sha256",
        "paper_only_strategy_override",
        "live_cash_authorized",
    }
)

_ACTIVE_CAPTURED_GENERATION_SQL = text("""
    SELECT DISTINCT
           refinement_meta_json -> 'captured_paper_variant_binding'
               ->> 'activation_generation' AS activation_generation
    FROM momentum_strategy_variants
    WHERE is_active IS TRUE
      AND execution_family = 'alpaca_spot'
      AND refinement_meta_json -> 'captured_paper_variant_binding'
          ->> 'schema_version' = 'chili.captured-paper-variant-binding-meta.v1'
      AND refinement_meta_json -> 'captured_paper_variant_binding'
          ->> 'account_scope' = 'alpaca:paper'
      AND refinement_meta_json -> 'captured_paper_variant_binding'
          ->> 'execution_family' = 'alpaca_spot'
      AND refinement_meta_json -> 'captured_paper_variant_binding'
          ->> 'expected_account_id' = :expected_account_id
      AND refinement_meta_json -> 'captured_paper_variant_binding'
          ->> 'paper_order_submission_authorized' = 'false'
      AND refinement_meta_json -> 'captured_paper_variant_binding'
          ->> 'live_cash_authorized' = 'false'
      AND refinement_meta_json -> 'captured_paper_variant_binding'
          ->> 'real_money_authorized' = 'false'
    ORDER BY activation_generation
    LIMIT 2
""")

ACTIVE_STATES = frozenset(
    {
        "queued",
        "watching",
        "entry_candidate",
        "pending_entry",
        "entered",
        "scaling_out",
        "trailing",
        "bailout",
        "exited",
        "cooldown",
        "live_arm_pending",
        "armed_pending_runner",
        "queued_live",
        "watching_live",
        "live_entry_candidate",
        "live_pending_entry",
        "live_entered",
        "live_scaling_out",
        "live_trailing",
        "live_bailout",
        "live_exited",
        "live_cooldown",
    }
)

POSITION_STATES = frozenset(
    {
        "entered",
        "scaling_out",
        "trailing",
        "bailout",
        "live_entered",
        "live_scaling_out",
        "live_trailing",
        "live_bailout",
    }
)

_cache_lock = threading.RLock()
_build_locks: dict[int, threading.Lock] = {}
_state_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_chart_cache: dict[int, tuple[float, tuple[str, ...], dict[str, list[list[Any]]], str]] = {}


def clear_live_monitor_caches() -> None:
    """Test/deploy helper; the observer owns no durable state."""

    with _cache_lock:
        _state_cache.clear()
        _chart_cache.clear()
        _build_locks.clear()


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _utc_naive(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _strict_utc_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _sha256_json(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _captured_heartbeat_content_sha256(meta: dict[str, Any]) -> str:
    body = dict(meta)
    body.pop("content_sha256", None)
    return _sha256_json(body)


def _legacy_captured_runtime_identity(
    db: Session,
    *,
    expected_account_id: str,
    now_utc: datetime,
) -> tuple[dict[str, Any] | None, str]:
    """Bind a valid v1 loop heartbeat to exactly one active PAPER generation.

    Main still emits the v1 control heartbeat.  It intentionally contains no
    broker identity, so the observer may use it only when the durable active
    variant bindings resolve to one exact PAPER-only activation generation.
    """

    try:
        from .lane_health import live_runner_loop_control_health

        health = live_runner_loop_control_health(db, now=now_utc)
    except Exception:
        logger.warning(
            "[live_monitor] legacy captured PAPER heartbeat validation failed",
            exc_info=True,
        )
        return None, "captured_watch_heartbeat_unreadable"
    reason = health.get("reason")
    if reason not in (None, "live_runner_loop_heartbeat_stale"):
        return None, str(reason or "captured_watch_heartbeat_invalid")
    heartbeat_at = _utc_naive(health.get("heartbeat_at"))
    checked_at = _utc_naive(now_utc) or datetime.utcnow()
    if heartbeat_at is None:
        return None, "captured_watch_heartbeat_invalid"
    day_start, day_end = _et_day_bounds(checked_at)
    if not (day_start <= heartbeat_at < day_end):
        return None, "captured_watch_heartbeat_not_today"
    try:
        generations = [
            str(value or "").strip().lower()
            for value in db.execute(
                _ACTIVE_CAPTURED_GENERATION_SQL,
                {"expected_account_id": expected_account_id},
            ).scalars()
            if str(value or "").strip()
        ]
    except Exception:
        logger.warning(
            "[live_monitor] active captured PAPER generation read failed",
            exc_info=True,
        )
        return None, "captured_watch_generation_unreadable"
    if len(generations) != 1:
        return None, (
            "captured_watch_generation_missing"
            if not generations
            else "captured_watch_generation_ambiguous"
        )
    try:
        runtime_generation = str(uuid.UUID(generations[0]))
    except (AttributeError, TypeError, ValueError):
        return None, "captured_watch_generation_invalid"
    age_seconds = (checked_at - heartbeat_at).total_seconds()
    if age_seconds < -2.0:
        return None, "captured_watch_heartbeat_future"
    stale_after = float(health.get("stale_seconds") or 75.0)
    runtime_stale = age_seconds >= stale_after
    return (
        {
            "expected_account_id": expected_account_id,
            "runtime_generation": runtime_generation,
            "heartbeat_at": heartbeat_at,
            "heartbeat_age_seconds": max(0.0, age_seconds),
            "runtime_stale": runtime_stale,
            "live_cash_authorized": False,
        },
        "runtime_stale" if runtime_stale else "ok",
    )


def _captured_runtime_identity(
    db: Session,
    *,
    user_id: int,
    now_utc: datetime,
) -> tuple[dict[str, Any] | None, str]:
    """Read the exact signed captured-PAPER heartbeat identity.

    The selection ledgers retain prior activation generations, so the observer must
    never guess the current one from a recent row or UUID.  This validates the same
    content-addressed v2 heartbeat emitted by the running captured PAPER service.
    """

    configured_user_id = getattr(settings, "chili_autotrader_user_id", None)
    if configured_user_id is None or isinstance(configured_user_id, bool):
        return None, "captured_watch_user_scope_unconfigured"
    try:
        configured_user_id = int(configured_user_id)
    except (TypeError, ValueError):
        return None, "captured_watch_user_scope_unconfigured"
    if configured_user_id != int(user_id):
        return None, "captured_watch_user_scope_mismatch"
    expected_account_id = str(
        getattr(settings, "chili_alpaca_expected_account_id", "") or ""
    ).strip().lower()
    try:
        expected_account_id = str(uuid.UUID(expected_account_id))
    except (AttributeError, TypeError, ValueError):
        return None, "captured_watch_account_unconfigured"
    try:
        row = (
            db.query(BrainBatchJob)
            .filter(
                BrainBatchJob.job_type == JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT
            )
            .order_by(BrainBatchJob.started_at.desc(), BrainBatchJob.id.desc())
            .limit(1)
            .one_or_none()
        )
    except Exception:
        logger.warning(
            "[live_monitor] captured PAPER heartbeat read failed",
            exc_info=True,
        )
        return None, "captured_watch_heartbeat_unreadable"
    if row is None:
        return None, "captured_watch_heartbeat_missing"
    started_at = _utc_naive(getattr(row, "started_at", None))
    heartbeat_at = _utc_naive(getattr(row, "ended_at", None))
    meta = getattr(row, "meta_json", None)
    if isinstance(meta, dict) and meta.get("schema") == _LEGACY_HEARTBEAT_SCHEMA:
        return _legacy_captured_runtime_identity(
            db,
            expected_account_id=expected_account_id,
            now_utc=now_utc,
        )
    if (
        str(getattr(row, "status", "") or "").strip().lower() != "ok"
        or started_at is None
        or heartbeat_at is None
        or not isinstance(meta, dict)
        or set(meta) != _CAPTURED_HEARTBEAT_META_KEYS
        or meta.get("schema") != _CAPTURED_HEARTBEAT_SCHEMA
        or meta.get("scope") != _CAPTURED_HEARTBEAT_SCOPE
        or meta.get("owner") != "momentum_live_runner_loop"
        or meta.get("account_scope") != "alpaca:paper"
        or meta.get("execution_family") != "alpaca_spot"
        or meta.get("live_cash_authorized") is not False
        or meta.get("expected_account_id") != expected_account_id
    ):
        return None, "captured_watch_heartbeat_invalid"
    supplied_hash = str(meta.get("content_sha256") or "")
    if (
        len(supplied_hash) != 64
        or supplied_hash != supplied_hash.lower()
        or any(char not in "0123456789abcdef" for char in supplied_hash)
        or supplied_hash != _captured_heartbeat_content_sha256(meta)
    ):
        return None, "captured_watch_heartbeat_hash_mismatch"
    owner_id = str(meta.get("owner_instance_id") or "").strip().lower()
    runtime_generation = str(meta.get("runtime_generation") or "").strip().lower()
    try:
        owner_id = str(uuid.UUID(owner_id))
        runtime_generation = str(uuid.UUID(runtime_generation))
    except (AttributeError, TypeError, ValueError):
        return None, "captured_watch_heartbeat_identity_invalid"
    generation = meta.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or meta.get("generation_identity") != f"{owner_id}:{generation}"
        or _strict_utc_iso(meta.get("row_started_at_utc")) != started_at
        or _strict_utc_iso(meta.get("heartbeat_at_utc")) != heartbeat_at
    ):
        return None, "captured_watch_heartbeat_identity_invalid"
    generation_started_at = _strict_utc_iso(
        meta.get("generation_started_at_utc")
    )
    if (
        generation_started_at is None
        or heartbeat_at + timedelta(seconds=2) < started_at
        or started_at + timedelta(seconds=2) < generation_started_at
    ):
        return None, "captured_watch_heartbeat_clock_invalid"
    checked_at = _utc_naive(now_utc) or datetime.utcnow()
    age_seconds = (checked_at - heartbeat_at).total_seconds()
    if age_seconds < -2.0:
        return None, "captured_watch_heartbeat_future"
    day_start, day_end = _et_day_bounds(checked_at)
    if not (day_start <= heartbeat_at < day_end):
        return None, "captured_watch_heartbeat_not_today"
    try:
        from .lane_health import live_loop_stale_seconds

        stale_after = float(live_loop_stale_seconds())
    except Exception:
        stale_after = 75.0
    return (
        {
            "expected_account_id": expected_account_id,
            "runtime_generation": runtime_generation,
            "heartbeat_at": heartbeat_at,
            "heartbeat_age_seconds": max(0.0, age_seconds),
            "runtime_stale": age_seconds >= stale_after,
            "live_cash_authorized": False,
        },
        "ok" if age_seconds < stale_after else "runtime_stale",
    )


def _et_day_bounds(now_utc: datetime) -> tuple[datetime, datetime]:
    et = ZoneInfo("America/New_York")
    aware = now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc)
    now_et = aware.astimezone(et)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end_et = start_et + timedelta(days=1)
    return (
        start_et.astimezone(timezone.utc).replace(tzinfo=None),
        end_et.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _tape_freshness(db: Session, *, since_utc: datetime) -> tuple[datetime | None, str | None]:
    """Newest quote/tick row across the WHOLE tape today, unscoped by symbol.

    This is deliberately not derived from ``_load_chart_series``: that short-circuits
    on an empty symbol list and joins ``WHERE t.symbol = s.symbol``, so it returns
    nothing in exactly the blind case we need to detect. Both reads are covered by
    the ``(observed_at)`` index created in migration 303.
    """
    for table, source in (
        ("momentum_nbbo_spread_tape", "momentum_nbbo_spread_tape"),
        ("iqfeed_trade_ticks", "iqfeed_trade_ticks"),
    ):
        try:
            with db.begin_nested():
                row = db.execute(
                    text(
                        f"SELECT max(observed_at) AS newest FROM {table} "
                        "WHERE observed_at >= :since_utc"
                    ),
                    {"since_utc": since_utc},
                ).first()
        except Exception:
            logger.warning(
                "[live_monitor] tape freshness read failed for %s", table, exc_info=True
            )
            continue
        newest = row[0] if row else None
        if isinstance(newest, datetime):
            return _utc_naive(newest), source
    return None, None


def _lane_bucket(mode: str | None, execution_family: str | None) -> str:
    if str(mode or "").lower() != "live":
        return "paper"
    if str(execution_family or "").lower() == "alpaca_spot":
        return "paper_twin"
    return "live"


def _execution_state(session: Any) -> dict[str, Any]:
    snapshot = _mapping(session.risk_snapshot_json)
    key = "momentum_live_execution" if str(session.mode).lower() == "live" else "momentum_paper_execution"
    return _mapping(snapshot.get(key))


def _position_state(session: Any) -> dict[str, Any]:
    execution = _execution_state(session)
    raw = _mapping(execution.get("position"))
    quantity = _float_or_none(raw.get("quantity"))
    entry = _float_or_none(raw.get("avg_entry_price"))
    if entry is None:
        entry = _float_or_none(raw.get("entry_price"))
    mark = _float_or_none(execution.get("last_mid"))
    stop = _float_or_none(raw.get("stop_price"))
    target = _float_or_none(raw.get("target_price"))
    is_open = bool(raw) and bool(quantity and quantity > 0) and str(session.state) in POSITION_STATES
    unrealized = None
    if is_open and entry is not None and mark is not None:
        unrealized = (mark - entry) * float(quantity or 0.0)
    return {
        "is_open": is_open,
        "quantity": quantity,
        "entry": entry,
        "mark": mark,
        "stop": stop,
        "target": target,
        "unrealized_usd": unrealized,
        "realized_runtime_usd": _float_or_none(execution.get("realized_pnl_usd")),
        "last_tick_utc": execution.get("last_tick_utc"),
    }


def _session_priority(row: dict[str, Any]) -> tuple[int, int, float]:
    state = str(row.get("state") or "")
    state_rank = 4 if row.get("position", {}).get("is_open") else 0
    if "pending_entry" in state:
        state_rank = max(state_rank, 3)
    elif "entry_candidate" in state:
        state_rank = max(state_rank, 2)
    elif "watching" in state or "armed" in state or "queued" in state:
        state_rank = max(state_rank, 1)
    lane_rank = {"live": 3, "paper_twin": 2, "paper": 1}.get(str(row.get("lane")), 0)
    updated = row.get("updated_at")
    stamp = updated.timestamp() if isinstance(updated, datetime) else 0.0
    return state_rank, lane_rank, stamp


def _preferred_outcome_pnl(row: Any) -> float | None:
    broker = _float_or_none(row.broker_realized_pnl_usd)
    if broker is not None and str(row.broker_recon_status or "").lower() == "reconciled":
        return broker
    realized = _float_or_none(row.realized_pnl_usd)
    return realized if realized is not None else broker


def _event_detail(payload: dict[str, Any]) -> str | None:
    for key in (
        "reason",
        "wait_reason",
        "trigger_reason",
        "setup_reason",
        "exit_reason",
        "outcome_class",
        "window",
        "state",
        "status",
    ):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:120]
    # ⚠️ ANG RISK EVALUATION AY GUMAGAMIT NG `errors`, HINDI `reason` (2026-08-25).
    # Ang live-side na block ay nag-e-emit ng {"reason": "wide bbo spread"} at
    # lumalabas iyon; ang boundary-risk na block ay nag-e-emit ng
    # {"severity": "block", "errors": ["Not paper-eligible per neural viability."]}
    # at WALANG lumalabas -- walang susi rito ang tumutugma sa listahan sa itaas.
    #
    # Ang bunga ay nakita mismo ng operator: sa action history ay may walong linyang
    # "paper blocked by risk" na walang anumang dahilan, katabi ng "live blocked by
    # risk · wide bbo spread" na may dahilan. Mukhang misteryo ang isang PERPEKTONG
    # LEGITIMONG block, at kinailangan ng paghukay sa DB para lang makita ang
    # dahilang nandoon na pala sa buong panahon.
    #
    # ⚠️ ANG DATOS AY HINDI NAGBABAGO. Ito ay purong DISPLAY -- ang payload ay
    # naglalaman na ng `errors`; hindi lang ito binabasa dito.
    errors = payload.get("errors")
    if isinstance(errors, (list, tuple)) and errors:
        joined = "; ".join(str(e).strip() for e in errors if str(e or "").strip())
        if joined:
            return joined[:120]
    elif isinstance(errors, str) and errors.strip():
        return errors.strip()[:120]
    rejects = payload.get("detector_rejects")
    if isinstance(rejects, dict) and rejects:
        key, value = next(iter(rejects.items()))
        return f"{key}: {value}"[:120]
    return None


def _event_kind(event_type: str | None) -> str:
    value = str(event_type or "").lower()
    if "entry" in value and any(token in value for token in ("fill", "entered", "confirmed")):
        return "entry"
    if any(token in value for token in ("exit", "exited", "flatten")) and any(
        token in value for token in ("fill", "exited", "flatten", "closed")
    ):
        return "exit"
    return "decision"


def _event_number(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(payload.get(key))
        if value is not None:
            return value
    return None


def _state_label(value: str | None) -> str:
    clean = str(value or "unknown")
    return clean.removeprefix("live_").replace("_", " ")


def _active_session_rows(db: Session, *, user_id: int) -> list[dict[str, Any]]:
    runtime_rows = (
        db.query(
            TradingAutomationRuntimeSnapshot.session_id,
            TradingAutomationRuntimeSnapshot.symbol,
            TradingAutomationRuntimeSnapshot.mode,
            TradingAutomationRuntimeSnapshot.lane,
            TradingAutomationRuntimeSnapshot.state,
            TradingAutomationRuntimeSnapshot.strategy_family,
            TradingAutomationRuntimeSnapshot.strategy_label,
            TradingAutomationRuntimeSnapshot.confidence,
            TradingAutomationRuntimeSnapshot.current_position_state,
            TradingAutomationRuntimeSnapshot.last_action,
            TradingAutomationRuntimeSnapshot.last_price,
            TradingAutomationRuntimeSnapshot.latest_levels_json,
            TradingAutomationRuntimeSnapshot.updated_at,
        )
        .filter(
            TradingAutomationRuntimeSnapshot.user_id == int(user_id),
            TradingAutomationRuntimeSnapshot.state.in_(tuple(ACTIVE_STATES)),
        )
        .order_by(TradingAutomationRuntimeSnapshot.updated_at.desc())
        .limit(LIVE_MONITOR_SESSION_SCAN_LIMIT)
        .all()
    )
    if not runtime_rows:
        return []
    session_ids = [int(row.session_id) for row in runtime_rows]
    session_rows = (
        db.query(
            TradingAutomationSession.id,
            TradingAutomationSession.user_id,
            TradingAutomationSession.execution_family,
            TradingAutomationSession.mode,
            TradingAutomationSession.symbol,
            TradingAutomationSession.state,
            TradingAutomationSession.started_at,
            TradingAutomationSession.created_at,
            TradingAutomationSession.updated_at,
        )
        .filter(
            TradingAutomationSession.id.in_(session_ids),
            TradingAutomationSession.user_id == int(user_id),
        )
        .all()
    )
    session_map = {int(row.id): row for row in session_rows}
    execution_ids = [
        int(row.id)
        for row in session_rows
        if str(row.state) in ACTIVE_STATES
        and (str(row.mode).lower() == "live" or str(row.state) in POSITION_STATES)
    ]
    risk_snapshots: dict[int, dict[str, Any]] = {}
    if execution_ids:
        risk_snapshots = {
            int(session_id): _mapping(snapshot)
            for session_id, snapshot in db.query(
                TradingAutomationSession.id,
                TradingAutomationSession.risk_snapshot_json,
            )
            .filter(TradingAutomationSession.id.in_(execution_ids))
            .all()
        }
    out: list[dict[str, Any]] = []
    for runtime in runtime_rows:
        raw_session = session_map.get(int(runtime.session_id))
        if raw_session is None or str(raw_session.state) not in ACTIVE_STATES:
            continue
        session = SimpleNamespace(
            id=int(raw_session.id),
            user_id=raw_session.user_id,
            execution_family=raw_session.execution_family,
            mode=raw_session.mode,
            symbol=raw_session.symbol,
            state=raw_session.state,
            started_at=raw_session.started_at,
            created_at=raw_session.created_at,
            updated_at=raw_session.updated_at,
            risk_snapshot_json=risk_snapshots.get(int(raw_session.id), {}),
        )
        position = _position_state(session)
        out.append(
            {
                "session": session,
                "session_id": int(session.id),
                "symbol": str(session.symbol or "").upper(),
                "mode": str(session.mode or "paper"),
                "lane": _lane_bucket(session.mode, session.execution_family),
                "state": str(session.state or "unknown"),
                "state_label": _state_label(session.state),
                "position": position,
                "last_action": runtime.last_action or session.state,
                "strategy": runtime.strategy_label or runtime.strategy_family,
                "confidence": _float_or_none(runtime.confidence),
                "updated_at": session.updated_at or runtime.updated_at,
                "runtime_updated_at": runtime.updated_at,
                "levels": _mapping(runtime.latest_levels_json),
            }
        )
    return out


def _broker_position_truth(
    db: Session,
    *,
    user_id: int,
    symbols: Iterable[str],
    now_utc: datetime,
) -> tuple[set[str], set[str]]:
    names = sorted({str(symbol).upper() for symbol in symbols if symbol})
    if not names:
        return set(), set()
    rows = (
        db.query(Trade.ticker, Trade.status)
        .filter(
            Trade.user_id == int(user_id),
            Trade.ticker.in_(names),
            or_(
                Trade.status == "open",
                (
                    Trade.status.in_(("closed", "cancelled"))
                    & Trade.exit_date.isnot(None)
                    & (Trade.exit_date >= now_utc - timedelta(days=2))
                ),
            ),
        )
        .all()
    )
    open_symbols = {str(ticker).upper() for ticker, status in rows if status == "open" and ticker}
    exited_symbols = {
        str(ticker).upper() for ticker, status in rows if status in ("closed", "cancelled") and ticker
    }
    return open_symbols, exited_symbols


def _pnl_rows(
    db: Session,
    *,
    user_id: int,
    active_rows: list[dict[str, Any]],
    now_utc: datetime,
) -> tuple[dict[str, dict[str, Any]], set[int]]:
    day_start, day_end = _et_day_bounds(now_utc)
    by_symbol: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "realized_usd": 0.0,
            "unrealized_usd": 0.0,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "by_lane": defaultdict(lambda: {"realized_usd": 0.0, "unrealized_usd": 0.0, "trades": 0}),
            "broker_unconfirmed": False,
        }
    )
    evidence_session_ids: set[int] = set()

    outcomes = (
        db.query(
            MomentumAutomationOutcome.session_id,
            MomentumAutomationOutcome.symbol,
            MomentumAutomationOutcome.mode,
            MomentumAutomationOutcome.realized_pnl_usd,
            MomentumAutomationOutcome.broker_realized_pnl_usd,
            MomentumAutomationOutcome.broker_recon_status,
            TradingAutomationSession.execution_family,
        )
        .join(TradingAutomationSession, TradingAutomationSession.id == MomentumAutomationOutcome.session_id)
        .filter(
            MomentumAutomationOutcome.user_id == int(user_id),
            MomentumAutomationOutcome.terminal_at >= day_start,
            MomentumAutomationOutcome.terminal_at < day_end,
            or_(
                MomentumAutomationOutcome.realized_pnl_usd.isnot(None),
                MomentumAutomationOutcome.broker_realized_pnl_usd.isnot(None),
            ),
        )
        .all()
    )
    for outcome in outcomes:
        pnl = _preferred_outcome_pnl(outcome)
        if pnl is None:
            continue
        symbol = str(outcome.symbol or "").upper()
        lane = _lane_bucket(outcome.mode, outcome.execution_family)
        cell = by_symbol[symbol]
        cell["realized_usd"] += pnl
        cell["trades"] += 1
        cell["wins"] += int(pnl > 0)
        cell["losses"] += int(pnl < 0)
        cell["by_lane"][lane]["realized_usd"] += pnl
        cell["by_lane"][lane]["trades"] += 1
        evidence_session_ids.add(int(outcome.session_id))

    fills = (
        db.query(
            TradingAutomationSimulatedFill.session_id,
            TradingAutomationSimulatedFill.symbol,
            TradingAutomationSimulatedFill.pnl_usd,
            TradingAutomationSession.mode,
            TradingAutomationSession.execution_family,
        )
        .join(TradingAutomationSession, TradingAutomationSession.id == TradingAutomationSimulatedFill.session_id)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSimulatedFill.ts >= day_start,
            TradingAutomationSimulatedFill.ts < day_end,
            TradingAutomationSimulatedFill.pnl_usd.isnot(None),
        )
        .all()
    )
    for fill in fills:
        pnl = float(fill.pnl_usd or 0.0)
        symbol = str(fill.symbol or "").upper()
        lane = _lane_bucket(fill.mode, fill.execution_family)
        cell = by_symbol[symbol]
        cell["realized_usd"] += pnl
        cell["trades"] += 1
        cell["wins"] += int(pnl > 0)
        cell["losses"] += int(pnl < 0)
        cell["by_lane"][lane]["realized_usd"] += pnl
        cell["by_lane"][lane]["trades"] += 1
        evidence_session_ids.add(int(fill.session_id))

    live_symbols = [row["symbol"] for row in active_rows if row["lane"] == "live"]
    open_broker_symbols, exited_broker_symbols = _broker_position_truth(
        db,
        user_id=user_id,
        symbols=live_symbols,
        now_utc=now_utc,
    )
    seen_runtime_realized: set[int] = set()
    for row in active_rows:
        session = row["session"]
        position = row["position"]
        symbol = row["symbol"]
        lane = row["lane"]
        cell = by_symbol[symbol]
        evidence_session_ids.add(int(session.id))
        broker_unconfirmed = (
            lane == "live"
            and symbol in exited_broker_symbols
            and symbol not in open_broker_symbols
        )
        if broker_unconfirmed:
            cell["broker_unconfirmed"] = True
        unrealized = position.get("unrealized_usd")
        if unrealized is not None and not broker_unconfirmed:
            cell["unrealized_usd"] += float(unrealized)
            cell["by_lane"][lane]["unrealized_usd"] += float(unrealized)
        if str(session.mode).lower() == "live" and int(session.id) not in seen_runtime_realized:
            started = session.started_at or session.created_at
            runtime_realized = position.get("realized_runtime_usd")
            if runtime_realized is not None and (started is None or started >= day_start):
                value = float(runtime_realized)
                cell["realized_usd"] += value
                cell["by_lane"][lane]["realized_usd"] += value
            seen_runtime_realized.add(int(session.id))

    normalized: dict[str, dict[str, Any]] = {}
    for symbol, cell in by_symbol.items():
        lanes = {}
        for lane, lane_cell in cell["by_lane"].items():
            lanes[lane] = {
                "realized_usd": round(float(lane_cell["realized_usd"]), 2),
                "unrealized_usd": round(float(lane_cell["unrealized_usd"]), 2),
                "trades": int(lane_cell["trades"]),
            }
        realized = round(float(cell["realized_usd"]), 2)
        unrealized = round(float(cell["unrealized_usd"]), 2)
        normalized[symbol] = {
            "realized_usd": realized,
            "unrealized_usd": unrealized,
            "total_usd": round(realized + unrealized, 2),
            "trades": int(cell["trades"]),
            "wins": int(cell["wins"]),
            "losses": int(cell["losses"]),
            "by_lane": lanes,
            "broker_unconfirmed": bool(cell["broker_unconfirmed"]),
        }
    return normalized, evidence_session_ids


def _recent_events(
    db: Session,
    *,
    session_ids: Iterable[int],
    session_symbols: dict[int, str],
    since_utc: datetime,
) -> dict[str, list[dict[str, Any]]]:
    ids = sorted({int(value) for value in session_ids if value})
    if not ids:
        return {}
    event_columns = (
        TradingAutomationEvent.id,
        TradingAutomationEvent.session_id,
        TradingAutomationEvent.ts,
        TradingAutomationEvent.event_type,
        TradingAutomationEvent.payload_json,
    )
    recent_rows = (
        db.query(*event_columns)
        .filter(
            TradingAutomationEvent.session_id.in_(ids),
            TradingAutomationEvent.ts >= since_utc,
        )
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(LIVE_MONITOR_EVENT_LIMIT)
        .all()
    )
    lifecycle_types = ("live_entry_filled", "live_exit_filled", "live_partial_exit_filled")
    lifecycle_rows = (
        db.query(*event_columns)
        .filter(
            TradingAutomationEvent.session_id.in_(ids),
            TradingAutomationEvent.ts >= since_utc,
            TradingAutomationEvent.event_type.in_(lifecycle_types),
        )
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(200)
        .all()
    )
    rows = sorted(
        {int(row.id): row for row in [*recent_rows, *lifecycle_rows]}.values(),
        key=lambda row: row.ts or datetime.min,
        reverse=True,
    )
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: dict[str, set[tuple[str, str | None]]] = defaultdict(set)
    for event in rows:
        symbol = session_symbols.get(int(event.session_id))
        if not symbol:
            continue
        payload = _mapping(event.payload_json)
        detail = _event_detail(payload)
        lifecycle = str(event.event_type) in lifecycle_types
        key = (str(event.event_type), str(event.id) if lifecycle else detail)
        if key in seen[symbol]:
            continue
        if len(by_symbol[symbol]) >= LIVE_MONITOR_EVENTS_PER_SYMBOL:
            if not lifecycle:
                continue
            replace_at = next(
                (
                    index
                    for index in range(len(by_symbol[symbol]) - 1, -1, -1)
                    if by_symbol[symbol][index].get("kind") == "decision"
                ),
                None,
            )
            if replace_at is None:
                continue
            by_symbol[symbol].pop(replace_at)
        seen[symbol].add(key)
        by_symbol[symbol].append(
            {
                "id": int(event.id),
                "session_id": int(event.session_id),
                "ts": _iso_utc(event.ts),
                "t": event.ts.strftime("%H:%M") if event.ts else None,
                "stage": str(event.event_type or "event"),
                "detail": detail,
                "kind": _event_kind(event.event_type),
                "price": _event_number(
                    payload,
                    "fill_price",
                    "avg",
                    "average_fill_price",
                    "entry_price",
                    "exit_price",
                    "price",
                    "mid",
                ),
                "quantity": _event_number(payload, "filled_size", "quantity", "qty"),
                "pnl_usd": _event_number(payload, "pnl_usd", "realized_pnl_usd"),
            }
        )
    for values in by_symbol.values():
        values.reverse()
    return dict(by_symbol)


_CAPTURED_WATCH_SQL = text("""
    WITH current_frontier AS MATERIALIZED (
        SELECT f.*
        FROM captured_paper_selection_frontiers f
        WHERE f.account_scope = 'alpaca:paper'
          AND f.execution_family = 'alpaca_spot'
          AND f.expected_account_id = :expected_account_id
          AND f.activation_generation = :activation_generation
          AND f.status = 'ready'
          AND f.gap_count = 0
        LIMIT 1
    ), current_routes AS MATERIALIZED (
        SELECT r.*, f.policy_sha256, f.settings_projection_sha256,
               f.code_build_sha256, f.variant_set_sha256
        FROM current_frontier f
        JOIN captured_paper_selection_route_states r
          ON r.account_scope = f.account_scope
         AND r.expected_account_id = f.expected_account_id
         AND r.activation_generation = f.activation_generation
         AND r.execution_family = f.execution_family
         AND r.authority_sha256 = f.authority_sha256
        WHERE r.state = 'eligible'
        ORDER BY r.symbol, r.variant_id
        LIMIT :row_limit
    ), ranked AS MATERIALIZED (
        SELECT r.symbol, r.variant_id, r.latest_source_sequence,
               r.evidence_sha256, r.batch_sha256, r.source_event_at,
               r.source_available_at, r.authority_sha256, r.policy_sha256,
               r.settings_projection_sha256, r.code_build_sha256,
               r.variant_set_sha256, v.viability_score, v.paper_eligible,
               v.live_eligible, v.freshness_ts, v.regime_snapshot_json,
               v.execution_readiness_json, v.explain_json,
               v.evidence_window_json, v.correlation_id, mv.family, mv.label,
               count(*) OVER (PARTITION BY r.symbol) AS route_count,
               count(*) FILTER (
                   WHERE v.paper_eligible IS TRUE AND v.live_eligible IS TRUE
               ) OVER (PARTITION BY r.symbol) AS policy_eligible_route_count,
               row_number() OVER (
                   PARTITION BY r.symbol
                   ORDER BY CASE WHEN v.paper_eligible AND v.live_eligible
                                 THEN 0 ELSE 1 END,
                            v.viability_score DESC,
                            r.source_available_at DESC,
                            r.variant_id
               ) AS symbol_rank
        FROM current_routes r
        JOIN momentum_strategy_variants mv
          ON mv.id = r.variant_id
         AND mv.is_active IS TRUE
         AND mv.execution_family = 'alpaca_spot'
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'schema_version' = 'chili.captured-paper-variant-binding-meta.v1'
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'account_scope' = r.account_scope
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'execution_family' = r.execution_family
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'expected_account_id' = r.expected_account_id
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'activation_generation' = r.activation_generation
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'policy_sha256' = r.policy_sha256
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'settings_projection_sha256' = r.settings_projection_sha256
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'code_build_sha256' = r.code_build_sha256
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'strategy_params_overridden' = 'false'
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'live_cash_authorized' = 'false'
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'paper_order_submission_authorized' = 'false'
         AND mv.refinement_meta_json -> 'captured_paper_variant_binding'
             ->> 'real_money_authorized' = 'false'
        JOIN momentum_symbol_viability v
          ON v.symbol = r.symbol
         AND v.variant_id = r.variant_id
         AND v.scope = 'symbol'
         AND v.source_node_id = 'captured_paper_selection_producer'
         AND (v.freshness_ts AT TIME ZONE 'UTC') = r.source_available_at
    )
    SELECT * FROM ranked
    WHERE symbol_rank = 1
    ORDER BY CASE WHEN policy_eligible_route_count > 0 THEN 0 ELSE 1 END,
             viability_score DESC, source_available_at DESC, symbol
    LIMIT :symbol_limit
""")


def _hash_is_exact(value: Any) -> bool:
    clean = str(value or "")
    return (
        len(clean) == 64
        and clean == clean.lower()
        and all(char in "0123456789abcdef" for char in clean)
    )


def _candidate_last_price(raw: dict[str, Any], symbol: str) -> float | None:
    features = _mapping(raw.get("features"))
    meta = _mapping(features.get("meta"))
    ross = _mapping(meta.get("ross_signals"))
    signal = _mapping(ross.get(symbol))
    for key in ("price", "last", "close", "last_price"):
        value = _float_or_none(signal.get(key))
        if value is not None and math.isfinite(value) and value > 0:
            return value
    return None


def _validated_captured_watch_route(
    raw: Any,
    *,
    identity: dict[str, Any],
) -> dict[str, Any] | None:
    row = dict(raw)
    symbol = str(row.get("symbol") or "").strip().upper()
    variant_id = row.get("variant_id")
    source_sequence = row.get("latest_source_sequence")
    score = _float_or_none(row.get("viability_score"))
    route_count = row.get("route_count")
    policy_eligible_route_count = row.get("policy_eligible_route_count")
    if (
        not symbol
        or type(variant_id) is not int
        or variant_id <= 0
        or type(source_sequence) is not int
        or source_sequence <= 0
        or type(route_count) is not int
        or route_count <= 0
        or type(policy_eligible_route_count) is not int
        or not 0 <= policy_eligible_route_count <= route_count
        or score is None
        or not math.isfinite(score)
        or not 0.0 <= score <= 1.0
        or type(row.get("paper_eligible")) is not bool
        or type(row.get("live_eligible")) is not bool
        or row.get("paper_eligible") != row.get("live_eligible")
        or bool(policy_eligible_route_count)
        != bool(row.get("paper_eligible") and row.get("live_eligible"))
    ):
        return None
    containers: list[dict[str, Any]] = []
    payloads: list[dict[str, Any]] = []
    for key in (
        "execution_readiness_json",
        "explain_json",
        "evidence_window_json",
    ):
        payload = _mapping(row.get(key))
        provenance = payload.get(_CAPTURED_PROVENANCE_KEY)
        if not isinstance(provenance, dict):
            return None
        containers.append(dict(provenance))
        payload = dict(payload)
        payload.pop(_CAPTURED_PROVENANCE_KEY, None)
        payloads.append(payload)
    provenance = containers[0]
    if (
        set(provenance) != _CAPTURED_PROVENANCE_KEYS
        or any(item != provenance for item in containers[1:])
    ):
        return None
    expected = {
        "schema_version": (
            "chili.captured-paper-selection-viability-provenance.v1"
        ),
        "account_scope": "alpaca:paper",
        "expected_account_id": identity["expected_account_id"],
        "activation_generation": identity["runtime_generation"],
        "authority_sha256": row.get("authority_sha256"),
        "policy_sha256": row.get("policy_sha256"),
        "settings_projection_sha256": row.get("settings_projection_sha256"),
        "code_build_sha256": row.get("code_build_sha256"),
        "variant_set_sha256": row.get("variant_set_sha256"),
        "variant_id": variant_id,
        "batch_sha256": row.get("batch_sha256"),
        "observation_sha256": row.get("evidence_sha256"),
        "source_sequence": source_sequence,
        "paper_only_strategy_override": False,
        "live_cash_authorized": False,
    }
    if any(provenance.get(key) != value for key, value in expected.items()):
        return None
    try:
        source_generation = str(
            uuid.UUID(str(provenance.get("source_generation") or ""))
        )
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        source_generation != provenance.get("source_generation")
        or not str(provenance.get("source_name") or "").strip()
    ):
        return None
    for key in (
        "authority_sha256",
        "policy_sha256",
        "settings_projection_sha256",
        "code_build_sha256",
        "variant_set_sha256",
        "batch_sha256",
        "observation_sha256",
        "queue_receipt_sha256",
        "coverage_receipt_sha256",
    ):
        value = provenance.get(key) if key in provenance else row.get(key)
        if not _hash_is_exact(value):
            return None
    source_event_at = _utc_naive(row.get("source_event_at"))
    source_available_at = _utc_naive(row.get("source_available_at"))
    freshness = _utc_naive(row.get("freshness_ts"))
    if (
        source_event_at is None
        or source_available_at is None
        or freshness is None
        or source_available_at < source_event_at
        or freshness != source_available_at
    ):
        return None
    observation_body = {
        "schema_version": "chili.captured-paper-selection-observation.v1",
        "source_sequence": source_sequence,
        "source_event_at": _iso_utc(source_event_at),
        "source_available_at": _iso_utc(source_available_at),
        "symbol": symbol,
        "variant_id": variant_id,
        "viability_score": float(score),
        "paper_eligible": row["paper_eligible"],
        "live_eligible": row["live_eligible"],
        "regime_snapshot_json": _mapping(row.get("regime_snapshot_json")),
        "execution_readiness_json": payloads[0],
        "explain_json": payloads[1],
        "evidence_window_json": payloads[2],
        "correlation_id": row.get("correlation_id"),
    }
    try:
        observation_hash = _sha256_json(observation_body)
    except (TypeError, ValueError):
        return None
    if observation_hash != row.get("evidence_sha256"):
        return None
    return {
        "symbol": symbol,
        "variant_id": variant_id,
        "updated_at": source_available_at,
        "confidence": float(score),
        "strategy": str(row.get("label") or row.get("family") or "Captured PAPER"),
        "route_count": route_count,
        "policy_eligible_route_count": policy_eligible_route_count,
        "policy_eligible": bool(
            row.get("paper_eligible") and row.get("live_eligible")
        ),
        "last_price": _candidate_last_price(
            _mapping(row.get("execution_readiness_json")),
            symbol,
        ),
    }


def _captured_watch_inventory(
    db: Session,
    *,
    user_id: int,
    now_utc: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    identity, identity_status = _captured_runtime_identity(
        db,
        user_id=user_id,
        now_utc=now_utc,
    )
    if identity is None:
        return [], {"status": identity_status, "heartbeat_at": None}
    try:
        with db.begin_nested():
            raw_rows = db.execute(
                _CAPTURED_WATCH_SQL,
                {
                    "expected_account_id": identity["expected_account_id"],
                    "activation_generation": identity["runtime_generation"],
                    "row_limit": LIVE_MONITOR_CAPTURED_ROUTE_ROW_LIMIT,
                    "symbol_limit": LIVE_MONITOR_SYMBOL_LIMIT,
                },
            ).mappings().all()
    except Exception:
        logger.warning(
            "[live_monitor] captured PAPER watch inventory read failed",
            exc_info=True,
        )
        return [], {
            "status": "captured_watch_inventory_unreadable",
            "heartbeat_at": identity["heartbeat_at"],
            "runtime_stale": identity["runtime_stale"],
            "activation_generation": identity["runtime_generation"],
        }
    routes = [
        candidate
        for raw in raw_rows
        if (
            candidate := _validated_captured_watch_route(
                raw,
                identity=identity,
            )
        )
        is not None
    ]
    # `_validated_captured_watch_route` has ten silent `return None` exits, six of
    # which are provenance sha comparisons. A post-deploy sha drift zeroes the watch
    # list while the lane is actively selecting, and nothing anywhere surfaced it.
    dropped_route_count = max(0, len(raw_rows) - len(routes))
    if dropped_route_count:
        logger.info(
            "[live_monitor] captured watch dropped %s of %s routes on validation",
            dropped_route_count,
            len(raw_rows),
        )
    rows = [
        {
            **route,
            "activation_generation": identity["runtime_generation"],
            "runtime_heartbeat_at": identity["heartbeat_at"],
            "runtime_stale": identity["runtime_stale"],
        }
        for route in routes[:LIVE_MONITOR_SYMBOL_LIMIT]
    ]
    return rows, {
        "status": identity_status,
        "heartbeat_at": identity["heartbeat_at"],
        "runtime_stale": identity["runtime_stale"],
        "activation_generation": identity["runtime_generation"],
        # Counted BEFORE the [:LIVE_MONITOR_SYMBOL_LIMIT] slice above, so the funnel
        # reports the true selection width rather than the capped view.
        # NOTE: these are SYMBOL-level. _CAPTURED_WATCH_SQL already collapses to one
        # row per symbol, so len(raw_rows) is symbols scored, not routes scored.
        "symbols_scored": len(raw_rows),
        "symbols_validated": len(routes),
        "policy_eligible_symbols": sum(
            1 for route in routes if route.get("policy_eligible")
        ),
        # The genuinely route-level (variant) numbers, for the sub-line only.
        "variant_routes_scored": sum(int(r.get("route_count") or 0) for r in routes),
        "variant_routes_eligible": sum(
            int(r.get("policy_eligible_route_count") or 0) for r in routes
        ),
        "dropped_route_count": dropped_route_count,
        # len(raw_rows) is itself capped by :symbol_limit in the SQL, so a full page
        # means "at least this many", not "exactly this many".
        "truncated": len(raw_rows) >= LIVE_MONITOR_SYMBOL_LIMIT,
    }


def _session_card_status(
    primary: dict[str, Any] | None,
    positions: list[dict[str, Any]],
    watch: dict[str, Any] | None,
) -> str:
    if positions:
        return "IN POSITION"
    if primary is None:
        return "SETUP" if (watch or {}).get("policy_eligible") else "WATCHING"
    state_text = " ".join(
        str(primary.get(key) or "")
        for key in ("state", "state_label", "last_action")
    ).lower()
    if any(
        token in state_text
        for token in (
            "entry_candidate",
            "entry candidate",
            "pending_entry",
            "pending entry",
            "queued_live",
            "queued live",
            "awaiting fill",
            "submitted",
        )
    ):
        return "SETUP"
    return "WATCHING"


def _build_state_snapshot(db: Session, *, user_id: int, now_utc: datetime) -> dict[str, Any]:
    active_rows = _active_session_rows(db, user_id=user_id)
    captured_rows, captured_status = _captured_watch_inventory(
        db,
        user_id=user_id,
        now_utc=now_utc,
    )
    captured_by_symbol = {
        str(row.get("symbol") or "").upper(): row
        for row in captured_rows
        if row.get("symbol")
    }
    pnl_by_symbol, evidence_session_ids = _pnl_rows(
        db,
        user_id=user_id,
        active_rows=active_rows,
        now_utc=now_utc,
    )
    session_symbols = {int(row["session_id"]): row["symbol"] for row in active_rows}
    for session in (
        db.query(TradingAutomationSession.id, TradingAutomationSession.symbol)
        .filter(TradingAutomationSession.id.in_(tuple(evidence_session_ids) or (-1,)))
        .all()
    ):
        session_symbols.setdefault(int(session.id), str(session.symbol or "").upper())
    events = _recent_events(
        db,
        session_ids=evidence_session_ids,
        session_symbols=session_symbols,
        since_utc=_et_day_bounds(now_utc)[0],
    )

    sessions_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in active_rows:
        sessions_by_symbol[row["symbol"]].append(row)
    all_symbols = (
        set(sessions_by_symbol)
        | set(pnl_by_symbol)
        | set(captured_by_symbol)
    )

    def _symbol_rank(symbol: str) -> tuple[Any, ...]:
        session_rows = sessions_by_symbol.get(symbol, [])
        session_rank = max(
            (_session_priority(row) for row in session_rows),
            default=(0, 0, 0.0),
        )
        candidate_rank = (
            0
            if not session_rows
            and captured_by_symbol.get(symbol, {}).get("policy_eligible")
            else 1
        )
        return (
            0 if session_rows else 1,
            -session_rank[0],
            -session_rank[1],
            -session_rank[2],
            candidate_rank,
            -float(captured_by_symbol.get(symbol, {}).get("confidence") or 0.0),
            -abs(float(pnl_by_symbol.get(symbol, {}).get("total_usd") or 0.0)),
            symbol,
        )

    ranked_symbols = sorted(
        all_symbols,
        key=_symbol_rank,
    )[:LIVE_MONITOR_SYMBOL_LIMIT]

    symbols_out: list[dict[str, Any]] = []
    for symbol in ranked_symbols:
        session_rows = sorted(sessions_by_symbol.get(symbol, []), key=_session_priority, reverse=True)
        primary = session_rows[0] if session_rows else None
        watch = captured_by_symbol.get(symbol)
        lanes: dict[str, dict[str, Any]] = {}
        positions: list[dict[str, Any]] = []
        for row in session_rows:
            lane = row["lane"]
            lane_row = lanes.setdefault(
                lane,
                {
                    "lane": lane,
                    "state": row["state"],
                    "state_label": row["state_label"],
                    "session_count": 0,
                    "updated_at": _iso_utc(row["updated_at"]),
                },
            )
            lane_row["session_count"] += 1
            if row["position"].get("is_open"):
                positions.append(
                    {
                        "lane": lane,
                        "session_id": row["session_id"],
                        **{key: row["position"].get(key) for key in ("quantity", "entry", "mark", "stop", "target")},
                    }
                )
        pnl = pnl_by_symbol.get(
            symbol,
            {
                "realized_usd": 0.0,
                "unrealized_usd": 0.0,
                "total_usd": 0.0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "by_lane": {},
                "broker_unconfirmed": False,
            },
        )
        updated = (
            primary.get("updated_at")
            if primary
            else (watch or {}).get("updated_at")
        )
        age_seconds = max(0.0, (now_utc - updated).total_seconds()) if isinstance(updated, datetime) else None
        candidate_only = primary is None and watch is not None
        card_status = _session_card_status(primary, positions, watch)
        candidate_stale = bool(
            candidate_only
            and (
                (watch or {}).get("runtime_stale")
                or (
                    age_seconds is not None
                    and age_seconds > LIVE_MONITOR_CAPTURED_WATCH_STALE_SECONDS
                )
            )
        )
        symbols_out.append(
            {
                "symbol": symbol,
                "active": bool(session_rows),
                "armed": bool(session_rows) and not positions,
                "card_status": card_status,
                "state": (
                    primary.get("state")
                    if primary
                    else (
                        (
                            "captured_setup"
                            if (watch or {}).get("policy_eligible")
                            else "captured_watching"
                        )
                        if watch is not None
                        else "completed_today"
                    )
                ),
                "state_label": (
                    primary.get("state_label")
                    if primary
                    else (
                        (
                            "setup"
                            if (watch or {}).get("policy_eligible")
                            else "watching"
                        )
                        if watch is not None
                        else "completed today"
                    )
                ),
                "primary_lane": primary.get("lane") if primary else None,
                "last_action": (
                    primary.get("last_action")
                    if primary
                    else (
                        "captured policy eligible"
                        if (watch or {}).get("policy_eligible")
                        else (
                            "captured selection scored"
                            if watch is not None
                            else "completed today"
                        )
                    )
                ),
                "strategy": (
                    primary.get("strategy")
                    if primary
                    else (watch or {}).get("strategy")
                ),
                "confidence": (
                    primary.get("confidence")
                    if primary
                    else (watch or {}).get("confidence")
                ),
                "last_price": (
                    primary.get("position", {}).get("mark")
                    if primary
                    else (watch or {}).get("last_price")
                ),
                "updated_at": _iso_utc(updated),
                "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
                "stale": (
                    candidate_stale
                    if candidate_only
                    else bool(age_seconds is not None and age_seconds > 30.0)
                ),
                "lanes": list(lanes.values()),
                "positions": positions,
                "pnl": pnl,
                "events": events.get(symbol, []),
                "observer_source": (
                    "captured_paper_selection"
                    if candidate_only
                    else "trading_automation_session"
                ),
                "watch": (
                    {
                        "policy_eligible": bool(watch.get("policy_eligible")),
                        "route_count": int(watch.get("route_count") or 0),
                        "policy_eligible_route_count": int(
                            watch.get("policy_eligible_route_count") or 0
                        ),
                        "activation_generation": watch.get(
                            "activation_generation"
                        ),
                    }
                    if watch is not None
                    else None
                ),
            }
        )

    lane_summary = {
        lane: {
            "sessions": sum(1 for row in active_rows if row["lane"] == lane),
            "symbols": len({row["symbol"] for row in active_rows if row["lane"] == lane}),
            "open_positions": sum(1 for row in active_rows if row["lane"] == lane and row["position"].get("is_open")),
        }
        for lane in ("live", "paper_twin", "paper")
    }
    totals = {
        "realized_usd": round(sum(float(row.get("realized_usd") or 0.0) for row in pnl_by_symbol.values()), 2),
        "unrealized_usd": round(sum(float(row.get("unrealized_usd") or 0.0) for row in pnl_by_symbol.values()), 2),
        "trades": sum(int(row.get("trades") or 0) for row in pnl_by_symbol.values()),
    }
    totals["total_usd"] = round(totals["realized_usd"] + totals["unrealized_usd"], 2)
    latest_candidates = [
        row.get("updated_at")
        for row in active_rows
        if isinstance(row.get("updated_at"), datetime)
    ]
    heartbeat_at = captured_status.get("heartbeat_at")
    if isinstance(heartbeat_at, datetime):
        latest_candidates.append(heartbeat_at)
    if not latest_candidates:
        from .lane_health import captured_paper_live_runner_control_health

        heartbeat = captured_paper_live_runner_control_health(
            db,
            expected_account_id=str(
                getattr(settings, "chili_alpaca_expected_account_id", "") or ""
            ),
            now=now_utc,
        )
        if heartbeat.get("ok") is True:
            heartbeat_at = heartbeat.get("heartbeat_at")
            if isinstance(heartbeat_at, datetime):
                latest_candidates.append(heartbeat_at)
    latest_update = max(latest_candidates, default=None)

    # ---- Operator status -------------------------------------------------------
    # `latest_update` above is loop-iteration recency on BOTH paths and always has
    # been. Keep it on the wire for compatibility, but derive the page's verdict from
    # the control heartbeat and the tape as two independent facts.
    from .lane_health import (
        LIVE_LOOP_HEARTBEAT_INTERVAL_SECONDS,
        evaluate_lane_health,
        live_loop_stale_seconds,
    )
    from .live_status import market_session_snapshot, resolve_lane_verdict

    market = market_session_snapshot(now_utc)
    day_start = _et_day_bounds(now_utc)[0]
    tape_at, tape_source = _tape_freshness(db, since_utc=day_start)

    def _age(value: datetime | None) -> float | None:
        if not isinstance(value, datetime):
            return None
        return round(max(0.0, (now_utc - value).total_seconds()), 1)

    control_at = captured_status.get("heartbeat_at")
    control_at = control_at if isinstance(control_at, datetime) else None
    stale_after = live_loop_stale_seconds()

    try:
        lane_health = evaluate_lane_health(db, user_id=user_id)
    except Exception:  # documented never to raise, but this is a read-only surface
        logger.warning("[live_monitor] lane health probe failed", exc_info=True)
        lane_health = {"enabled": False, "frozen": False, "severity": "ok", "conditions": []}

    verdict = resolve_lane_verdict(
        watch_status=captured_status.get("status"),
        market_open=bool(market["is_open"]),
        market_label=market["label"],
        control_age_s=_age(control_at),
        tape_age_s=_age(tape_at),
        stale_after_s=stale_after,
        lane_health=lane_health,
        symbol_count=len(symbols_out),
    )

    watching_symbols = set(captured_by_symbol).difference(sessions_by_symbol)
    # Distinct SYMBOLS, matching the grain of every other funnel stage. This was
    # session-granular, so one symbol with two open sessions counted twice and could
    # make `in_position` exceed `armed`. The session-granular number is still
    # reported per lane in `lanes[*].open_positions` and in `open_position_count`.
    position_symbols = {
        row["symbol"] for row in active_rows if row["position"].get("is_open")
    }
    in_position_symbols = len(position_symbols)
    # `open_position_count` stays SESSION-granular: it is the number of open
    # positions being managed, which is what the accounting row and lanes[] mean.
    # Only the funnel stage needed to be symbol-granular, to match its neighbours.
    in_position = sum(1 for row in active_rows if row["position"].get("is_open"))
    setup_symbols = {
        str(row.get("symbol") or "")
        for row in symbols_out
        if str(row.get("card_status") or "").upper() == "SETUP"
    }
    # `armed` was `bool(session_rows) and not positions`, which counts ANY
    # session-backed symbol including plain WATCHING ones -- so armed could exceed
    # setup, which is impossible in a funnel. Nest it under setup explicitly.
    armed_symbols = {
        str(row.get("symbol") or "")
        for row in symbols_out
        if row.get("armed") and str(row.get("symbol") or "") in setup_symbols
    } | (setup_symbols & position_symbols)

    funnel = {
        # Every stage is symbol-level and a strict subset of the one before it.
        "candidates": int(captured_status.get("symbols_scored") or 0),
        "on_board": int(captured_status.get("symbols_validated") or 0),
        "eligible": int(captured_status.get("policy_eligible_symbols") or 0),
        "setup": len(setup_symbols),
        "armed": len(armed_symbols),
        "in_position": in_position_symbols,
        "closed_today": {
            "trades": totals["trades"],
            "wins": sum(int(row.get("wins") or 0) for row in pnl_by_symbol.values()),
            "losses": sum(int(row.get("losses") or 0) for row in pnl_by_symbol.values()),
        },
        # Route-level (variant) detail belongs in a sub-line, never as a stage.
        "variant_routes_scored": int(captured_status.get("variant_routes_scored") or 0),
        "variant_routes_eligible": int(
            captured_status.get("variant_routes_eligible") or 0
        ),
        "dropped_routes": int(captured_status.get("dropped_route_count") or 0),
        "watching": len(watching_symbols),
        "truncated": bool(captured_status.get("truncated")),
        "truncated_at": LIVE_MONITOR_SYMBOL_LIMIT,
    }

    # Cadence is DERIVED from the cache TTL, not guessed. Polling faster than the TTL
    # cannot deliver fresher data -- it delivers STALER data, because the TTL gets
    # quantized up to the next multiple of the cadence. At 2000ms the effective
    # refresh was 7.06s with the payload up to 4.06s old on arrival; at TTL+500ms it
    # is 5.94s and always 0s old, using ~60% fewer requests. There is no tradeoff.
    open_ms = int(LIVE_MONITOR_STATE_TTL_SECONDS * 1000) + 500
    refresh_after_ms = (
        open_ms if market["phase"] in ("regular", "premarket", "afterhours") else 30000
    )

    return {
        "ok": True,
        "read_only": True,
        "as_of_utc": _iso_utc(now_utc),
        "latest_runtime_utc": _iso_utc(latest_update),
        "refresh_after_ms": refresh_after_ms,
        "active_symbol_count": len({row["symbol"] for row in active_rows}),
        "watching_symbol_count": len(watching_symbols),
        "open_position_count": in_position,
        "totals": totals,
        "lanes": lane_summary,
        "symbols": symbols_out,
        "market_session": market,
        "funnel": funnel,
        "lane": {
            **verdict,
            "captured_watch_status": captured_status.get("status"),
            "control_heartbeat_utc": _iso_utc(control_at),
            "control_heartbeat_age_s": _age(control_at),
            "tape_utc": _iso_utc(tape_at),
            "tape_age_s": _age(tape_at),
            "tape_source": tape_source,
            "stale_after_s": round(stale_after, 1),
            "heartbeat_interval_s": LIVE_LOOP_HEARTBEAT_INTERVAL_SECONDS,
            "dropped_route_count": funnel["dropped_routes"],
        },
        "lane_health": lane_health,
        "observer": {
            "source": "persisted_runtime_events_outcomes_and_quote_tape",
            "broker_calls": 0,
            "provider_calls": 0,
            "writes": 0,
            "state_cache_seconds": LIVE_MONITOR_STATE_TTL_SECONDS,
            "chart_cache_seconds": LIVE_MONITOR_CHART_TTL_SECONDS,
            "symbol_limit": LIVE_MONITOR_SYMBOL_LIMIT,
            "quote_row_cap_per_symbol": LIVE_MONITOR_QUOTE_ROWS_PER_SYMBOL,
            "captured_watch_status": captured_status.get("status"),
            "captured_watch_source": "exact_generation_persisted_selection",
        },
    }


def _minute_bar_series(rows: Iterable[Any]) -> dict[str, list[list[Any]]]:
    buckets: dict[str, dict[datetime, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        symbol = str(row[0] or "").upper()
        observed_at = row[1]
        if not symbol or not isinstance(observed_at, datetime):
            continue
        bid = _float_or_none(row[2])
        ask = _float_or_none(row[3])
        price = _float_or_none(row[4])
        if price is None and bid is not None and ask is not None:
            price = (bid + ask) / 2.0
        if price is None or price <= 0:
            continue
        if observed_at.tzinfo is not None:
            observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
        bucket = observed_at.replace(second=0, microsecond=0)
        day_volume = _float_or_none(row[5])
        candle = buckets[symbol].get(bucket)
        if candle is None:
            buckets[symbol][bucket] = {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume_min": day_volume,
                "volume_max": day_volume,
            }
            continue
        candle["high"] = max(float(candle["high"]), price)
        candle["low"] = min(float(candle["low"]), price)
        candle["close"] = price
        if day_volume is not None:
            candle["volume_min"] = day_volume if candle["volume_min"] is None else min(candle["volume_min"], day_volume)
            candle["volume_max"] = day_volume if candle["volume_max"] is None else max(candle["volume_max"], day_volume)

    out: dict[str, list[list[Any]]] = {}
    for symbol, values in buckets.items():
        bars: list[list[Any]] = []
        for bucket, candle in sorted(values.items()):
            volume_min = candle["volume_min"]
            volume_max = candle["volume_max"]
            volume = max(0.0, float(volume_max) - float(volume_min)) if volume_min is not None and volume_max is not None else 0.0
            bars.append(
                [
                    bucket.strftime("%H:%M"),
                    round(float(candle["open"]), 6),
                    round(float(candle["high"]), 6),
                    round(float(candle["low"]), 6),
                    round(float(candle["close"]), 6),
                    round(volume, 2),
                ]
            )
        out[symbol] = bars
    return out


def _load_chart_series(
    db: Session,
    *,
    symbols: tuple[str, ...],
    now_utc: datetime,
) -> dict[str, list[list[Any]]]:
    if not symbols:
        return {}
    # Candidate-only watch cards can precede a session by hours.  Keep the read
    # bounded by both the current ET day and the per-source row cap so a quiet
    # candidate still has its retained chart without materializing the full tape.
    since_utc = _et_day_bounds(now_utc)[0]
    rows = db.execute(
        text(
            "WITH syms(symbol) AS (SELECT unnest(CAST(:symbols AS text[]))) "
            "SELECT s.symbol, q.observed_at, q.bid, q.ask, q.mid, q.day_volume "
            "FROM syms s "
            "CROSS JOIN LATERAL ("
            " SELECT observed_at, bid, ask, mid, day_volume "
            " FROM momentum_nbbo_spread_tape t "
            " WHERE t.symbol = s.symbol AND t.observed_at >= :since_utc "
            " ORDER BY observed_at DESC LIMIT :row_limit"
            ") q ORDER BY s.symbol, q.observed_at"
        ),
        {
            "symbols": list(symbols),
            "since_utc": since_utc,
            "row_limit": LIVE_MONITOR_QUOTE_ROWS_PER_SYMBOL,
        },
    ).fetchall()
    series = _minute_bar_series(rows)
    missing = tuple(symbol for symbol in symbols if not series.get(symbol))
    if not missing:
        return series
    # Trade ticks are a persisted fallback only when a symbol has no retained NBBO;
    # avoiding the second index walk keeps the normal 24-card observer bounded.
    tick_rows = db.execute(
        text(
            "WITH syms(symbol) AS (SELECT unnest(CAST(:symbols AS text[]))) "
            "SELECT s.symbol, q.observed_at, q.bid, q.ask, q.price, "
            "NULL::double precision AS day_volume FROM syms s "
            "CROSS JOIN LATERAL ("
            " SELECT observed_at, bid, ask, price FROM iqfeed_trade_ticks t "
            " WHERE t.symbol = s.symbol AND t.observed_at >= :since_utc "
            " ORDER BY observed_at DESC LIMIT :row_limit"
            ") q ORDER BY s.symbol, q.observed_at"
        ),
        {
            "symbols": list(missing),
            "since_utc": since_utc,
            "row_limit": LIVE_MONITOR_QUOTE_ROWS_PER_SYMBOL,
        },
    ).fetchall()
    series.update(_minute_bar_series(tick_rows))
    return series


def _cached_chart_series(
    db: Session,
    *,
    user_id: int,
    symbols: tuple[str, ...],
    now_utc: datetime,
    now_mono: float,
) -> tuple[dict[str, list[list[Any]]], str]:
    # Compare the symbol SET, not the ordered tuple. `_symbol_rank` sorts by live P/L
    # and confidence, so during an active session the order churns while the set is
    # unchanged -- and an ordered compare invalidated this 15s cache on nearly every
    # build, forcing the expensive per-symbol LATERAL tape scan exactly when the
    # market is busiest and the scan costs the most.
    symbol_key = tuple(sorted(symbols))
    with _cache_lock:
        cached = _chart_cache.get(int(user_id))
        if cached and now_mono - cached[0] < LIVE_MONITOR_CHART_TTL_SECONDS and cached[1] == symbol_key:
            return cached[2], cached[3]
    try:
        series = _load_chart_series(db, symbols=symbols, now_utc=now_utc)
    except Exception:
        logger.warning("[live_monitor] bounded quote-tape read failed", exc_info=True)
        db.rollback()
        series = {}
    chart_as_of = _iso_utc(now_utc) or ""
    with _cache_lock:
        _chart_cache[int(user_id)] = (now_mono, symbol_key, series, chart_as_of)
    return series, chart_as_of


def live_monitor_snapshot(db: Session, *, user_id: int) -> dict[str, Any]:
    """Return one batched observer snapshot; single-flight and TTL bounded per user."""

    now_mono = time.monotonic()
    with _cache_lock:
        cached = _state_cache.get(int(user_id))
        if cached and now_mono - cached[0] < LIVE_MONITOR_STATE_TTL_SECONDS:
            return cached[1]
        build_lock = _build_locks.setdefault(int(user_id), threading.Lock())

    with build_lock:
        now_mono = time.monotonic()
        with _cache_lock:
            cached = _state_cache.get(int(user_id))
            if cached and now_mono - cached[0] < LIVE_MONITOR_STATE_TTL_SECONDS:
                return cached[1]
        started = time.perf_counter()
        now_utc = datetime.utcnow()
        payload = _build_state_snapshot(db, user_id=int(user_id), now_utc=now_utc)
        symbols = tuple(str(row.get("symbol") or "").upper() for row in payload.get("symbols", []) if row.get("symbol"))
        series, chart_as_of = _cached_chart_series(
            db,
            user_id=int(user_id),
            symbols=symbols,
            now_utc=now_utc,
            now_mono=now_mono,
        )
        payload["series"] = series
        payload["chart_as_of_utc"] = chart_as_of
        payload["observer"]["build_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        with _cache_lock:
            _state_cache[int(user_id)] = (time.monotonic(), payload)
        return payload
