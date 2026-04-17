"""Phase M.2-autopilot: runtime mode override helper.

Each M.2 slice's ``_raw_mode()`` helper consults this module BEFORE
falling back to ``settings.brain_pattern_regime_*_mode``. A row in
``trading_brain_runtime_modes`` for a given slice wins over the env
default, allowing the autopilot to advance / revert the mode without
mutating ``.env`` or recreating services.

Reads use a process-local 30-second TTL cache so the hot path
(every tilt emission, every promotion request, every kill-switch
sweep cycle) stays cheap.

Absence of a row for a slice is a valid state: callers receive
``None`` and fall back to the env config.

Slice name conventions (must match the M.2 ``ACTION_TYPE`` strings):

* ``pattern_regime_tilt``
* ``pattern_regime_promotion``
* ``pattern_regime_killswitch``

Any other slice name is silently treated as "no override" to keep
this helper strictly additive.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

KNOWN_SLICE_NAMES = (
    "pattern_regime_tilt",
    "pattern_regime_promotion",
    "pattern_regime_killswitch",
)

_CACHE_TTL_SECONDS = 30.0
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Optional[str]]] = {}


def _cache_get(slice_name: str) -> tuple[bool, Optional[str]]:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(slice_name)
        if hit is None:
            return (False, None)
        expires_at, value = hit
        if expires_at <= now:
            return (False, None)
        return (True, value)


def _cache_put(slice_name: str, value: Optional[str]) -> None:
    with _cache_lock:
        _cache[slice_name] = (time.monotonic() + _CACHE_TTL_SECONDS, value)


def _cache_clear(slice_name: Optional[str] = None) -> None:
    with _cache_lock:
        if slice_name is None:
            _cache.clear()
        else:
            _cache.pop(slice_name, None)


def get_runtime_mode_override(
    slice_name: str,
    *,
    db: Optional[Session] = None,
    bypass_cache: bool = False,
) -> Optional[str]:
    """Return the active runtime mode override for ``slice_name``.

    Returns ``None`` if no row exists (fall back to env default).

    Accepts an optional ``db`` session; if not provided, opens a
    short-lived one. Exceptions are swallowed and treated as "no
    override" so a DB hiccup can never worsen the slice's behavior.
    """
    if slice_name not in KNOWN_SLICE_NAMES:
        return None

    if not bypass_cache:
        hit, value = _cache_get(slice_name)
        if hit:
            return value

    try:
        close_when_done = False
        if db is None:
            from ...db import SessionLocal

            db = SessionLocal()
            close_when_done = True
        try:
            row = db.execute(
                text(
                    """
                    SELECT mode
                    FROM trading_brain_runtime_modes
                    WHERE slice_name = :slice
                    LIMIT 1
                    """
                ),
                {"slice": slice_name},
            ).fetchone()
            value: Optional[str] = None
            if row is not None:
                raw = (row[0] or "").strip().lower()
                if raw in ("off", "shadow", "compare", "authoritative"):
                    value = raw
            _cache_put(slice_name, value)
            return value
        finally:
            if close_when_done:
                try:
                    db.close()
                except Exception:  # pragma: no cover - defensive
                    pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "[runtime_mode_override] lookup failed for %s: %s",
            slice_name,
            exc,
        )
        return None


def set_runtime_mode_override(
    db: Session,
    *,
    slice_name: str,
    mode: str,
    updated_by: str,
    reason: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Upsert the override row for ``slice_name``. Invalidates cache.

    ``mode`` must be one of ``off`` / ``shadow`` / ``compare`` /
    ``authoritative``. Invalid inputs raise ``ValueError``.
    """
    if slice_name not in KNOWN_SLICE_NAMES:
        raise ValueError(f"unknown slice_name: {slice_name!r}")
    normalised = (mode or "").strip().lower()
    if normalised not in ("off", "shadow", "compare", "authoritative"):
        raise ValueError(f"invalid mode: {mode!r}")

    payload_json = json.dumps(payload or {}, sort_keys=True)

    db.execute(
        text(
            """
            INSERT INTO trading_brain_runtime_modes
                (slice_name, mode, updated_at, updated_by, reason, payload_json)
            VALUES
                (:slice, :mode, NOW(), :updated_by, :reason, CAST(:payload AS JSONB))
            ON CONFLICT (slice_name) DO UPDATE SET
                mode = EXCLUDED.mode,
                updated_at = NOW(),
                updated_by = EXCLUDED.updated_by,
                reason = EXCLUDED.reason,
                payload_json = EXCLUDED.payload_json
            """
        ),
        {
            "slice": slice_name,
            "mode": normalised,
            "updated_by": updated_by[:64],
            "reason": (reason or None),
            "payload": payload_json,
        },
    )
    # Caller is responsible for commit; we invalidate the cache
    # eagerly so even if commit is rolled back, at worst we re-read
    # the DB once.
    _cache_clear(slice_name)


def clear_runtime_mode_override(db: Session, *, slice_name: str) -> None:
    """Remove the override row for ``slice_name`` (returns to env default)."""
    if slice_name not in KNOWN_SLICE_NAMES:
        raise ValueError(f"unknown slice_name: {slice_name!r}")
    db.execute(
        text(
            "DELETE FROM trading_brain_runtime_modes WHERE slice_name = :slice"
        ),
        {"slice": slice_name},
    )
    _cache_clear(slice_name)


def list_runtime_overrides(db: Session) -> list[dict[str, Any]]:
    """Return all override rows for diagnostics. Cache-free."""
    rows = db.execute(
        text(
            """
            SELECT slice_name, mode, updated_at, updated_by, reason
            FROM trading_brain_runtime_modes
            ORDER BY slice_name
            """
        )
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "slice_name": r[0],
                "mode": r[1],
                "updated_at": r[2].isoformat() if r[2] is not None else None,
                "updated_by": r[3],
                "reason": r[4],
            }
        )
    return out


def invalidate_cache(slice_name: Optional[str] = None) -> None:
    """Public helper: clear cache so the next read hits the DB."""
    _cache_clear(slice_name)


__all__ = [
    "KNOWN_SLICE_NAMES",
    "get_runtime_mode_override",
    "set_runtime_mode_override",
    "clear_runtime_mode_override",
    "list_runtime_overrides",
    "invalidate_cache",
]
