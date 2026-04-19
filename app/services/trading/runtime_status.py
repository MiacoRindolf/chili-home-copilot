"""Operator runtime status backed by durable DB state."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...models.core import BrainWorkerControl, BrokerSession
from ...models.trading import BrainBatchJob
from .batch_job_constants import (
    JOB_CRYPTO_BREAKOUT_SCANNER,
    JOB_MOMENTUM_SCANNER,
    JOB_PATTERN_IMMINENT_SCANNER,
    JOB_STOCK_BREAKOUT_SCANNER,
)
from .runtime_surface_state import read_runtime_surface_state

# Freshness thresholds (seconds)
_STALE_SCANNER = 600
_STALE_PREDICTIONS = 1800
_STALE_BROKER_SYNC = 300
_STALE_LEARNING = 3600
_STALE_MARKET_DATA = 120
_STALE_REGIME = 900

_SCANNER_JOB_TYPES = (
    JOB_PATTERN_IMMINENT_SCANNER,
    JOB_MOMENTUM_SCANNER,
    JOB_STOCK_BREAKOUT_SCANNER,
    JOB_CRYPTO_BREAKOUT_SCANNER,
)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _age_seconds(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return round((datetime.now(UTC) - dt.astimezone(UTC)).total_seconds(), 1)


def _freshness_state(
    *,
    dt: datetime | None,
    stale_threshold: float,
    explicit_state: str | None = None,
) -> tuple[str, bool]:
    state = (explicit_state or "").strip().lower()
    if dt is None:
        return "no_data", True
    if state == "error":
        return "error", True
    age = _age_seconds(dt)
    if age is None:
        return "no_data", True
    if age > stale_threshold:
        return "stale", True
    if state in {"ok", "stale", "no_data"}:
        return state if state == "ok" else "ok", False
    return "ok", False


def _surface(
    surface: str,
    *,
    stale_threshold: float | None = None,
    as_of: datetime | None = None,
    state: str | None = None,
    extra: dict[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if stale_threshold is not None:
        final_state, is_stale = _freshness_state(
            dt=as_of,
            stale_threshold=stale_threshold,
            explicit_state=state,
        )
    else:
        final_state = (state or "ok").strip().lower() or "ok"
        is_stale = final_state == "stale"
    out: dict[str, Any] = {
        "surface": surface,
        "state": final_state,
        "ok": final_state == "ok",
        "as_of": _iso(as_of),
    }
    if stale_threshold is not None:
        out["age_seconds"] = _age_seconds(as_of)
        out["is_stale"] = is_stale
        out["stale_threshold_seconds"] = stale_threshold
    if note:
        out["note"] = note
    if extra:
        out.update(extra)
    return out


def _surface_from_runtime_row(
    db: Session,
    *,
    surface: str,
    stale_threshold: float,
) -> dict[str, Any]:
    row = read_runtime_surface_state(db, surface=surface)
    if row is None:
        return _surface(surface, stale_threshold=stale_threshold, state="no_data")
    as_of = _parse_dt(row.get("as_of") or row.get("updated_at"))
    extra = {k: v for k, v in row.items() if k not in {"state", "as_of", "updated_at", "surface"}}
    return _surface(
        surface,
        stale_threshold=stale_threshold,
        as_of=as_of,
        state=row.get("state"),
        extra=extra,
    )


def scanner_status(db: Session) -> dict[str, Any]:
    latest_ok = (
        db.query(BrainBatchJob)
        .filter(
            BrainBatchJob.job_type.in_(_SCANNER_JOB_TYPES),
            BrainBatchJob.status == "ok",
        )
        .order_by(BrainBatchJob.ended_at.desc().nullslast(), BrainBatchJob.started_at.desc())
        .first()
    )
    stale_running_since = datetime.utcnow() - timedelta(seconds=_STALE_SCANNER)
    stale_running = (
        db.query(BrainBatchJob)
        .filter(
            BrainBatchJob.job_type.in_(_SCANNER_JOB_TYPES),
            BrainBatchJob.status == "running",
            BrainBatchJob.started_at < stale_running_since,
        )
        .count()
    )
    if stale_running:
        latest_ts = latest_ok.ended_at if latest_ok is not None else None
        return _surface(
            "scanner",
            stale_threshold=_STALE_SCANNER,
            as_of=_parse_dt(latest_ts),
            state="error",
            extra={
                "latest_job_type": latest_ok.job_type if latest_ok is not None else None,
                "stale_running_jobs": int(stale_running),
            },
            note="stale running scanner job(s) detected",
        )
    if latest_ok is None:
        return _surface("scanner", stale_threshold=_STALE_SCANNER, state="no_data")
    payload = dict(latest_ok.payload_json or {})
    extra = {
        "latest_job_type": latest_ok.job_type,
        "latest_job_id": latest_ok.id,
        "source": "brain_batch_jobs",
        "payload": payload,
    }
    return _surface(
        "scanner",
        stale_threshold=_STALE_SCANNER,
        as_of=_parse_dt(latest_ok.ended_at or latest_ok.started_at),
        state="ok",
        extra=extra,
    )


def predictions_status(db: Session) -> dict[str, Any]:
    return _surface_from_runtime_row(
        db,
        surface="predictions",
        stale_threshold=_STALE_PREDICTIONS,
    )


def broker_status(db: Session) -> dict[str, Any]:
    row = read_runtime_surface_state(db, surface="broker")
    session_row = (
        db.query(BrokerSession)
        .filter(BrokerSession.broker == "robinhood")
        .order_by(BrokerSession.updated_at.desc())
        .first()
    )
    session_as_of = _parse_dt(session_row.updated_at if session_row is not None else None)
    if row is None:
        return _surface(
            "broker",
            stale_threshold=_STALE_BROKER_SYNC,
            state="no_data",
            extra={"session_as_of": _iso(session_as_of)},
        )
    as_of = _parse_dt(row.get("as_of") or row.get("updated_at"))
    extra = {k: v for k, v in row.items() if k not in {"state", "as_of", "updated_at", "surface"}}
    extra["session_as_of"] = _iso(session_as_of)
    return _surface(
        "broker",
        stale_threshold=_STALE_BROKER_SYNC,
        as_of=as_of,
        state=row.get("state"),
        extra=extra,
    )


def learning_status(db: Session) -> dict[str, Any]:
    ctrl = db.query(BrainWorkerControl).filter(BrainWorkerControl.id == 1).first()
    if ctrl is None or ctrl.last_heartbeat_at is None:
        return _surface("learning", stale_threshold=_STALE_LEARNING, state="no_data")
    phase = None
    try:
        payload = json.loads(ctrl.learning_live_json) if ctrl.learning_live_json else {}
        phase = payload.get("phase") or payload.get("state")
    except Exception:
        phase = None
    return _surface(
        "learning",
        stale_threshold=_STALE_LEARNING,
        as_of=_parse_dt(ctrl.last_heartbeat_at),
        state="ok",
        extra={"phase": phase, "source": "brain_worker_control"},
    )


def market_data_status(db: Session) -> dict[str, Any]:
    return _surface_from_runtime_row(
        db,
        surface="market_data",
        stale_threshold=_STALE_MARKET_DATA,
    )


def regime_status(db: Session) -> dict[str, Any]:
    return _surface_from_runtime_row(
        db,
        surface="regime",
        stale_threshold=_STALE_REGIME,
    )


def circuit_breaker_status() -> dict[str, Any]:
    try:
        from .portfolio_risk import get_breaker_status

        status = get_breaker_status()
        return _surface(
            "circuit_breaker",
            state="ok" if not status.get("tripped") else "error",
            extra={
                "tripped": bool(status.get("tripped")),
                "reason": status.get("reason"),
            },
        )
    except Exception as exc:
        return _surface("circuit_breaker", state="error", note=str(exc))


def kill_switch_status() -> dict[str, Any]:
    try:
        from .governance import get_kill_switch_reason, is_kill_switch_active

        active = bool(is_kill_switch_active())
        return _surface(
            "kill_switch",
            state="ok" if not active else "error",
            extra={"active": active, "reason": get_kill_switch_reason() if active else None},
        )
    except Exception as exc:
        return _surface("kill_switch", state="error", note=str(exc))


def get_runtime_overview(db: Session) -> dict[str, Any]:
    surfaces = [
        scanner_status(db),
        predictions_status(db),
        broker_status(db),
        learning_status(db),
        market_data_status(db),
        regime_status(db),
        circuit_breaker_status(),
        kill_switch_status(),
    ]
    degraded = [s["surface"] for s in surfaces if not s.get("ok", False)]
    return {
        "as_of": _iso(datetime.now(UTC)),
        "healthy": len(degraded) == 0,
        "degraded_surfaces": degraded,
        "surfaces": {s["surface"]: s for s in surfaces},
    }


def get_freshness_summary(db: Session) -> dict[str, Any]:
    items = []
    for entry in (
        scanner_status(db),
        predictions_status(db),
        market_data_status(db),
        regime_status(db),
        learning_status(db),
        broker_status(db),
    ):
        items.append(
            {
                "surface": entry["surface"],
                "state": entry.get("state"),
                "as_of": entry.get("as_of"),
                "age_seconds": entry.get("age_seconds"),
                "is_stale": entry.get("is_stale", entry.get("state") == "stale"),
            }
        )
    return {
        "as_of": _iso(datetime.now(UTC)),
        "items": items,
    }
