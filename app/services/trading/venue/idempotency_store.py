"""Durable, DB-backed client_order_id idempotency guard for venue adapters.

Replaces the in-RAM OrderedDict guards that previously lived inside
``robinhood_spot.py`` and ``coinbase_spot.py``. Those guards reset on every
restart, leaving a window where a crash-and-redeploy during an in-flight order
could re-submit the same order.

This store keeps a short-TTL in-memory cache in front (fast path for
submission-burst protection) and persists each submission to
``venue_order_idempotency`` so restart-safety is real.

Contract
--------
- ``is_duplicate`` is safe to call from any venue adapter; returns ``False``
  when ``client_order_id`` is empty/None so callers don't need to pre-check.
- ``remember`` is best-effort against the DB — if the DB write fails the
  in-memory cache still records the key, and the error is logged but does
  not raise (order placement must not be blocked by a bookkeeping failure).
- ``mark_broker_id`` is called after the venue ACKs, so crash-recovery can
  look up the broker's order_id via ``resolve_broker_id``.

TTLs come from ``settings`` (see chili_venue_idempotency_ttl_hours_*).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

# ── In-memory fast path ────────────────────────────────────────────────

_MEM_MAX = 2048
_MEM_TTL_SEC = 300.0  # matches the legacy guard; DB is the durable layer

_mem_cache: "OrderedDict[str, float]" = OrderedDict()
_mem_lock = threading.Lock()


def _mem_remember(key: str) -> None:
    now = time.monotonic()
    with _mem_lock:
        _mem_cache[key] = now
        _mem_cache.move_to_end(key)
        # prune by size
        while len(_mem_cache) > _MEM_MAX:
            _mem_cache.popitem(last=False)
        # prune by TTL (walk from oldest)
        cutoff = now - _MEM_TTL_SEC
        for k in list(_mem_cache.keys()):
            if _mem_cache[k] < cutoff:
                del _mem_cache[k]
            else:
                break


def _mem_is_duplicate(key: str) -> bool:
    now = time.monotonic()
    cutoff = now - _MEM_TTL_SEC
    with _mem_lock:
        ts = _mem_cache.get(key)
        if ts is None:
            return False
        if ts < cutoff:
            _mem_cache.pop(key, None)
            return False
        return True


def reset_for_tests() -> None:
    """Clear the in-memory cache (pytest helper)."""
    with _mem_lock:
        _mem_cache.clear()


# ── TTL resolution ─────────────────────────────────────────────────────


def _venue_ttl_hours(venue: str) -> float:
    v = (venue or "").strip().lower()
    try:
        from ....config import settings

        if v in ("coinbase", "coinbase_spot", "crypto"):
            return float(getattr(settings, "chili_venue_idempotency_ttl_hours_crypto", 48.0))
        return float(getattr(settings, "chili_venue_idempotency_ttl_hours_equities", 168.0))
    except Exception:
        # Sensible fallback if settings import fails in a test harness
        return 168.0 if v not in ("coinbase", "coinbase_spot", "crypto") else 48.0


# ── DB helpers ─────────────────────────────────────────────────────────


def _session():
    # Local import — avoid pulling DB init at module load time (tests mock).
    from ....db import SessionLocal

    return SessionLocal()


def _db_is_duplicate(key: str) -> bool:
    """Return True if ``key`` exists in ``venue_order_idempotency`` and its
    TTL has not expired. On any DB error, return False (fail-open is safer
    than blocking a legitimate retry — the in-memory guard still covers the
    hot path, and the venue itself rejects duplicates server-side for CB)."""
    try:
        sess = _session()
        try:
            row = sess.execute(
                text(
                    "SELECT 1 FROM venue_order_idempotency "
                    "WHERE client_order_id = :k AND ttl_expires_at > NOW()"
                ),
                {"k": key},
            ).first()
            return row is not None
        finally:
            sess.close()
    except Exception:
        logger.debug("[idempotency_store] DB duplicate check failed", exc_info=True)
        return False


def _db_remember(
    key: str,
    *,
    venue: str,
    symbol: str,
    side: str,
    qty: float,
    broker_order_id: Optional[str],
    status: str,
) -> None:
    ttl_hours = _venue_ttl_hours(venue)
    # Store naive UTC to match the table's ``TIMESTAMP`` (without tz) column.
    expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=ttl_hours)
    try:
        sess = _session()
        try:
            sess.execute(
                text(
                    "INSERT INTO venue_order_idempotency "
                    "(client_order_id, venue, symbol, side, qty, broker_order_id, status, ttl_expires_at) "
                    "VALUES (:k, :venue, :symbol, :side, :qty, :boi, :status, :exp) "
                    "ON CONFLICT (client_order_id) DO NOTHING"
                ),
                {
                    "k": key,
                    "venue": (venue or "").lower(),
                    "symbol": symbol,
                    "side": (side or "").lower(),
                    "qty": float(qty or 0.0),
                    "boi": broker_order_id,
                    "status": status,
                    "exp": expires,
                },
            )
            sess.commit()
        finally:
            sess.close()
    except Exception:
        logger.warning(
            "[idempotency_store] Failed to persist client_order_id=%s venue=%s",
            key,
            venue,
            exc_info=True,
        )


def _db_mark_broker_id(key: str, broker_order_id: str, status: Optional[str]) -> None:
    try:
        sess = _session()
        try:
            sess.execute(
                text(
                    "UPDATE venue_order_idempotency "
                    "SET broker_order_id = :boi, "
                    "    status = COALESCE(:status, status) "
                    "WHERE client_order_id = :k"
                ),
                {"k": key, "boi": broker_order_id, "status": status},
            )
            sess.commit()
        finally:
            sess.close()
    except Exception:
        logger.debug(
            "[idempotency_store] Failed to update broker_order_id for %s", key, exc_info=True
        )


# ── Public API ─────────────────────────────────────────────────────────


def is_duplicate(client_order_id: Optional[str], *, venue: str = "") -> bool:
    """Return True if this client_order_id was recently recorded.

    Empty/None is never a duplicate — the caller (venue adapter) decides how
    to handle missing ids (Robinhood doesn't support them; Coinbase autogens).
    """
    if not client_order_id:
        return False
    if _mem_is_duplicate(client_order_id):
        return True
    if _db_is_duplicate(client_order_id):
        # Promote to memory so subsequent hot-path checks are fast.
        _mem_remember(client_order_id)
        return True
    return False


def remember(
    client_order_id: Optional[str],
    *,
    venue: str,
    symbol: str,
    side: str,
    qty: float,
    broker_order_id: Optional[str] = None,
    status: str = "submitted",
) -> None:
    """Record a submission. Safe to call with an empty id (no-op)."""
    if not client_order_id:
        return
    _mem_remember(client_order_id)
    _db_remember(
        client_order_id,
        venue=venue,
        symbol=symbol,
        side=side,
        qty=qty,
        broker_order_id=broker_order_id,
        status=status,
    )


def mark_broker_id(
    client_order_id: Optional[str],
    broker_order_id: Optional[str],
    status: Optional[str] = None,
) -> None:
    """After a venue ACKs, associate the broker's order_id with this key.

    Used by reconcilers to answer 'did this client_order_id actually result
    in a live broker order?' after a process restart.
    """
    if not client_order_id or not broker_order_id:
        return
    _db_mark_broker_id(client_order_id, broker_order_id, status)


def resolve_broker_id(client_order_id: Optional[str]) -> Optional[str]:
    """Return the broker_order_id previously associated with this key, if any.

    Returns None on DB error — caller must treat missing as 'unknown'.
    """
    if not client_order_id:
        return None
    try:
        sess = _session()
        try:
            row = sess.execute(
                text(
                    "SELECT broker_order_id FROM venue_order_idempotency "
                    "WHERE client_order_id = :k"
                ),
                {"k": client_order_id},
            ).first()
            return row[0] if row and row[0] else None
        finally:
            sess.close()
    except Exception:
        logger.debug(
            "[idempotency_store] resolve_broker_id failed for %s", client_order_id, exc_info=True
        )
        return None


def gc_expired(*, max_rows: int = 5000) -> int:
    """Best-effort GC for expired rows. Returns number deleted (0 on error).

    Optional — a nightly scheduler can call this; not required for
    correctness because lookups already filter on ``ttl_expires_at``.
    """
    try:
        sess = _session()
        try:
            result = sess.execute(
                text(
                    "DELETE FROM venue_order_idempotency "
                    "WHERE client_order_id IN ("
                    "  SELECT client_order_id FROM venue_order_idempotency "
                    "  WHERE ttl_expires_at <= NOW() LIMIT :n"
                    ")"
                ),
                {"n": int(max_rows)},
            )
            sess.commit()
            return int(result.rowcount or 0)
        finally:
            sess.close()
    except Exception:
        logger.debug("[idempotency_store] gc_expired failed", exc_info=True)
        return 0
