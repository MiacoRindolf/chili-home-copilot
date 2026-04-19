"""Durable runtime health state persisted in ``trading_brain_runtime_modes``."""
from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import BrainRuntimeMode

RUNTIME_SURFACE_PREFIX = "runtime_surface:"
_WRITE_THROTTLE_SECONDS = 15.0
_last_persist_lock = Lock()
_last_persist_by_surface: dict[str, float] = {}


def runtime_surface_slice(surface: str) -> str:
    return f"{RUNTIME_SURFACE_PREFIX}{str(surface or '').strip().lower()}"


def upsert_runtime_surface_state(
    db: Session,
    *,
    surface: str,
    state: str,
    source: str,
    as_of: datetime | None = None,
    details: dict[str, Any] | None = None,
    updated_by: str = "runtime_surface",
) -> BrainRuntimeMode:
    ts = as_of or datetime.utcnow()
    slice_name = runtime_surface_slice(surface)
    row = (
        db.query(BrainRuntimeMode)
        .filter(BrainRuntimeMode.slice_name == slice_name)
        .first()
    )
    payload = dict(details or {})
    payload["surface"] = str(surface or "").strip().lower()
    payload["source"] = str(source or "").strip() or "unknown"
    payload["as_of"] = ts.isoformat() + "Z"
    if row is None:
        row = BrainRuntimeMode(
            slice_name=slice_name,
            mode=str(state or "no_data"),
            updated_at=ts,
            updated_by=updated_by,
            reason=f"runtime_surface:{surface}",
            payload_json=payload,
        )
        db.add(row)
    else:
        row.mode = str(state or "no_data")
        row.updated_at = ts
        row.updated_by = updated_by
        row.reason = f"runtime_surface:{surface}"
        row.payload_json = payload
    return row


def persist_runtime_surface_now(
    *,
    surface: str,
    state: str,
    source: str,
    as_of: datetime | None = None,
    details: dict[str, Any] | None = None,
    updated_by: str = "runtime_surface",
) -> bool:
    """Best-effort cross-process runtime heartbeat writer."""
    import time

    ts = as_of or datetime.utcnow()
    key = str(surface or "").strip().lower()
    now = float(time.time())
    with _last_persist_lock:
        last = float(_last_persist_by_surface.get(key) or 0.0)
        if now - last < _WRITE_THROTTLE_SECONDS:
            return False
        _last_persist_by_surface[key] = now

    try:
        from ...db import SessionLocal

        db = SessionLocal()
        try:
            upsert_runtime_surface_state(
                db,
                surface=key,
                state=state,
                source=source,
                as_of=ts,
                details=details,
                updated_by=updated_by,
            )
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def read_runtime_surface_state(
    db: Session,
    *,
    surface: str,
) -> dict[str, Any] | None:
    row = (
        db.query(BrainRuntimeMode)
        .filter(BrainRuntimeMode.slice_name == runtime_surface_slice(surface))
        .first()
    )
    if row is None:
        return None
    payload = dict(row.payload_json or {})
    payload.setdefault("surface", str(surface or "").strip().lower())
    payload["state"] = str(row.mode or "no_data").strip().lower() or "no_data"
    payload["updated_at"] = row.updated_at.isoformat() + "Z" if row.updated_at else None
    payload["updated_by"] = row.updated_by
    return payload
