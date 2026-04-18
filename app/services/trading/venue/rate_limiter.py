"""Token-bucket rate limiter for venue write calls (order placement + cancel).

Why
---
Robinhood REST caps us at ~60 req/min, Coinbase Advanced Trade private REST
is ~30 req/s. A reconciler retry storm (or a bug-induced tight loop around
``place_market_order``) could burn that budget and 429-lock the account for
the rest of the session. We need a cheap in-process guard that fires BEFORE
the HTTP call so we stay polite under our own misbehavior as well as under
honest concurrent workloads.

Design
------
- Token-bucket per venue. Tokens regenerate at ``rate_per_sec``; bucket
  capped at ``burst`` tokens.
- ``try_acquire(venue)`` is non-blocking: returns ``(allowed, retry_after)``.
  On exhaustion the caller should return a structured "rate_limited" dict
  rather than raise — reconcilers back off instead of retrying.
- Thread-safe via a single module lock. Budgets are small, contention is low.
- Settings-driven: capacities/rates read from ``settings`` at each call so
  test monkeypatches take effect immediately without needing a reload.

What this is NOT
----------------
- Not a distributed limiter — per-process only. Two worker processes calling
  the same venue concurrently share the venue's server-side budget, not this
  one. That's acceptable for now (we run a single scheduler process); when
  we scale out, move the bucket into Redis and keep this interface.
- Not a retry scheduler — we do NOT sleep or block. Exhaustion is a signal
  to the caller to defer / return / back off.
- Not an outbound HTTP connection limiter — it guards semantic order ops
  (place/cancel), not arbitrary REST traffic.

Contract
--------
    allowed, retry_after = try_acquire("coinbase")
    if not allowed:
        return {"ok": False, "error": "rate_limited", "retry_after_s": retry_after}
    # ... actually place the order ...
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ── Bucket state ───────────────────────────────────────────────────────


@dataclass
class _Bucket:
    capacity: float
    rate_per_sec: float  # tokens added per second
    tokens: float
    last_refill: float  # monotonic seconds

    def refill(self, now: float) -> None:
        if now <= self.last_refill:
            return
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
        self.last_refill = now

    def retry_after(self) -> float:
        """Seconds until at least 1 token is available (0 if already have one)."""
        if self.tokens >= 1.0:
            return 0.0
        deficit = 1.0 - self.tokens
        if self.rate_per_sec <= 0:
            return float("inf")
        return deficit / self.rate_per_sec


# ── Module-level registry ──────────────────────────────────────────────

_VENUES = ("robinhood", "coinbase")

_buckets: dict[str, _Bucket] = {}
_lock = threading.Lock()


def _resolve_config(venue: str) -> tuple[float, float]:
    """Return ``(capacity, rate_per_sec)`` for this venue from settings.

    Read at call time so tests that monkeypatch settings are honored without
    needing to reset the registry. Returns sensible defaults on any error.
    """
    v = (venue or "").strip().lower()
    try:
        from ...trading.venue import _settings_proxy  # type: ignore  # noqa
    except Exception:
        pass
    try:
        from ....config import settings

        if v in ("coinbase", "coinbase_spot", "crypto"):
            rate = float(getattr(settings, "chili_venue_rate_limit_cb_orders_per_sec", 3.0))
            burst = int(getattr(settings, "chili_venue_rate_limit_cb_burst", 5))
        else:
            # Robinhood configured per-minute; convert to per-second.
            rpm = float(getattr(settings, "chili_venue_rate_limit_rh_orders_per_min", 20.0))
            rate = max(0.01, rpm / 60.0)
            burst = int(getattr(settings, "chili_venue_rate_limit_rh_burst", 5))
        return (float(max(1, burst)), float(max(0.01, rate)))
    except Exception:
        # Fallbacks mirror defaults — never hard-block on a missing setting.
        return (5.0, 1.0) if v not in ("coinbase", "coinbase_spot", "crypto") else (5.0, 3.0)


def _is_enabled() -> bool:
    try:
        from ....config import settings

        return bool(getattr(settings, "chili_venue_rate_limit_enabled", True))
    except Exception:
        return True


def _get_or_create_bucket(venue: str) -> _Bucket:
    v = (venue or "").strip().lower()
    if v in ("coinbase_spot", "crypto"):
        v = "coinbase"
    capacity, rate = _resolve_config(v)
    b = _buckets.get(v)
    now = time.monotonic()
    if b is None:
        b = _Bucket(capacity=capacity, rate_per_sec=rate, tokens=capacity, last_refill=now)
        _buckets[v] = b
        return b
    # Reconcile live-config changes (e.g. test monkeypatches) cheaply.
    if b.capacity != capacity or b.rate_per_sec != rate:
        # Don't award free tokens on a rate bump; keep current tokens but clamp.
        b.capacity = capacity
        b.rate_per_sec = rate
        b.tokens = min(b.tokens, capacity)
    b.refill(now)
    return b


# ── Public API ─────────────────────────────────────────────────────────


def try_acquire(venue: str, *, cost: float = 1.0) -> tuple[bool, float]:
    """Try to consume ``cost`` tokens for ``venue``. Non-blocking.

    Returns ``(allowed, retry_after_seconds)``. ``retry_after_seconds`` is
    0.0 when allowed; otherwise the minimum wait for enough tokens.

    When the limiter is globally disabled via settings, always allows.
    """
    if not _is_enabled():
        return (True, 0.0)
    with _lock:
        b = _get_or_create_bucket(venue)
        if b.tokens >= cost:
            b.tokens -= cost
            return (True, 0.0)
        return (False, b.retry_after())


def peek(venue: str) -> dict[str, float]:
    """Snapshot of the current bucket (for logs / metrics / tests)."""
    with _lock:
        b = _get_or_create_bucket(venue)
        return {
            "capacity": b.capacity,
            "rate_per_sec": b.rate_per_sec,
            "tokens": b.tokens,
            "retry_after_s": b.retry_after(),
        }


def rate_limited_response(
    venue: str, retry_after_s: float, *, client_order_id: Optional[str] = None
) -> dict[str, object]:
    """Build the canonical structured response for an exhausted bucket.

    Callers return this directly; the reconciler / scheduler interprets
    ``error == "rate_limited"`` as a backoff signal, not a hard failure.
    """
    payload: dict[str, object] = {
        "ok": False,
        "error": "rate_limited",
        "venue": (venue or "").lower(),
        "retry_after_s": round(float(retry_after_s), 3),
    }
    if client_order_id:
        payload["client_order_id"] = client_order_id
    return payload


def reset_for_tests() -> None:
    """Clear all buckets so each test starts with a full bucket."""
    with _lock:
        _buckets.clear()
