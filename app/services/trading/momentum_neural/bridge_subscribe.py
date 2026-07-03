"""CAPTURE-G3 — event-driven IQFeed-bridge subscribe-on-first-alert.

The IQFeed trade/depth bridges (host processes) subscribe symbols by POLLING two DB tables on
a ~20s refresh (armed/live sessions + the eligible-mover viability board). A symbol that FIRST
ignites only reaches the bridge after its viability row is written AND the next refresh — a
~2.7-min blind window on a sub-2-min squeeze (VWAV 2026-06-30: the 5->9.75 leg was un-taped).

This module is the FAST PATH: the app container writes a subscription HINT the instant a symbol
first-alerts (``request_bridge_subscription``); the bridge reads a recent trailing window
(``recent_subscribe_requests`` / the pure ``select_fresh_subscribe_symbols``) and subscribes
immediately, additively to its normal refresh set — first-alert -> subscribed in seconds.

``momentum_bridge_subscribe_requests`` is NOT a trading table (no orders/positions/fills) — a
pure subscription hint — so the container-side write is safe (matrix G3). Kill-switch
``chili_momentum_bridge_subscribe_on_alert_enabled`` (default True): OFF ⇒ no write ⇒ the
bridge sees no fast-path rows ⇒ byte-identical to the poll-only cadence.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text as _sql

from ....config import settings

_log = logging.getLogger(__name__)

# The bridge's fast-path window: only requests newer than this are honored, so a stale row can
# never resurrect a long-dead name. Wider than the bridge's fast poll so no fresh row is missed.
FRESH_WINDOW_S_DEFAULT = 180.0


def request_bridge_subscription(
    db: Any, symbol: str, *, reason: str = "first_alert", now_utc: datetime | None = None
) -> bool:
    """Write a subscribe HINT for ``symbol`` (the first-alert moment) so the IQFeed bridge
    picks it up on its fast path. Returns True on write, False when disabled / invalid / error.

    Container-side write to a NON-trading coordination table (allowed by matrix G3). Idempotent
    at the read side (the bridge de-dups against its current watch set); a repeated write within
    the fast window is harmless (just refreshes the freshness). Never raises — a failed hint must
    NEVER break the ignition/alert path that called it.
    """
    if not bool(getattr(settings, "chili_momentum_bridge_subscribe_on_alert_enabled", True)):
        return False
    sym = str(symbol or "").strip().upper()
    if not sym or sym.endswith("-USD"):  # equities only (the IQFeed L1/L2 bridges are equity)
        return False
    _at = (now_utc or datetime.now(timezone.utc)).replace(tzinfo=None)
    try:
        db.execute(
            _sql(
                "INSERT INTO momentum_bridge_subscribe_requests (symbol, requested_at, reason) "
                "VALUES (:s, :at, :r)"
            ),
            {"s": sym, "at": _at, "r": str(reason)[:32]},
        )
        return True
    except Exception:
        # the caller owns the transaction; a failed hint is non-fatal — do not raise.
        _log.debug("[bridge_subscribe] hint write failed sym=%s", sym, exc_info=True)
        return False


def select_fresh_subscribe_symbols(
    rows: Iterable[tuple[Any, Any]],
    *,
    now_utc: datetime,
    fresh_window_s: float = FRESH_WINDOW_S_DEFAULT,
    already_watched: set[str] | None = None,
    max_new: int | None = None,
) -> list[str]:
    """PURE fast-path trigger (no I/O — unit-testable): from ``(symbol, requested_at)`` rows,
    return the NEW symbols to subscribe NOW — those requested within ``fresh_window_s`` of
    ``now_utc`` and NOT already watched, de-duplicated, newest-first, capped at ``max_new``.

    ``requested_at`` may be naive-UTC (the table basis) or tz-aware; both are compared in naive
    UTC. A row with an unreadable timestamp is skipped (fail-safe: never subscribe on garbage).
    """
    watched = {str(s).strip().upper() for s in (already_watched or set())}
    cutoff = now_utc.replace(tzinfo=None) - timedelta(seconds=max(0.0, float(fresh_window_s)))
    seen: set[str] = set()
    out: list[str] = []
    # newest-first so the cap keeps the freshest movers.
    def _naive(ts: Any) -> datetime | None:
        try:
            if ts is None:
                return None
            if getattr(ts, "tzinfo", None) is not None:
                return ts.astimezone(timezone.utc).replace(tzinfo=None)
            return ts
        except Exception:
            return None

    parsed: list[tuple[datetime, str]] = []
    for sym, ts in rows or []:
        s = str(sym or "").strip().upper()
        if not s or s.endswith("-USD"):
            continue
        t = _naive(ts)
        if t is None or t < cutoff:
            continue
        parsed.append((t, s))
    parsed.sort(key=lambda x: x[0], reverse=True)
    for _t, s in parsed:
        if s in watched or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if max_new is not None and len(out) >= int(max_new):
            break
    return out


def recent_subscribe_requests(
    engine: Any, *, fresh_window_s: float = FRESH_WINDOW_S_DEFAULT, now_utc: datetime | None = None
) -> list[tuple[str, datetime]]:
    """Read the fast-path hint rows (symbol, requested_at) within the fresh window. Used by the
    bridge (which owns a raw SQLAlchemy engine). Returns [] on any error / missing table so the
    bridge degrades to its normal poll cadence (never crashes on the fast path)."""
    _now = (now_utc or datetime.now(timezone.utc)).replace(tzinfo=None)
    cutoff = _now - timedelta(seconds=max(0.0, float(fresh_window_s)))
    try:
        with engine.connect() as c:
            rows = c.execute(
                _sql(
                    "SELECT symbol, requested_at FROM momentum_bridge_subscribe_requests "
                    "WHERE requested_at > :cut ORDER BY requested_at DESC"
                ),
                {"cut": cutoff},
            ).fetchall()
        return [(str(r[0]).upper(), r[1]) for r in rows]
    except Exception:
        return []
