"""CAPTURE-G3 — event-driven IQFeed-bridge subscribe-on-first-alert.

The IQFeed trade/depth bridges (host processes) subscribe symbols by POLLING two DB tables on
a ~20s refresh (armed/live sessions + the eligible-mover viability board). A symbol that FIRST
ignites only reaches the bridge after its viability row is written AND the next refresh — a
~2.7-min blind window on a sub-2-min squeeze (VWAV 2026-06-30: the 5->9.75 leg was un-taped).

This module is the FAST PATH: the app container writes a subscription HINT the instant a symbol
first-alerts (``request_bridge_subscription``); the bridge fast-polls a recent trailing window
(its standalone SQL reader mirrors the pure, unit-tested ``select_fresh_subscribe_symbols``
contract — newest-first, fresh-window, cap keeps the freshest) and subscribes immediately,
additively to its normal refresh set — first-alert -> subscribed in seconds.

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

# The one INSERT, named once, so a caller that needs the hint to share a SAVEPOINT
# with its own evidence row (``ignition_receipts.record_snapshot_onset``) writes
# the SAME statement instead of a second, independently-rolled-back copy.
BRIDGE_SUBSCRIBE_INSERT_SQL = (
    "INSERT INTO momentum_bridge_subscribe_requests "
    "(symbol, requested_at, reason) VALUES (:s, :at, :r)"
)

# HINT REASONS THAT YIELD THE LAST SLOT ([61] review 09-11). The HINT source sits
# ABOVE ROSS and ELIGIBLE in ``iqfeed_subscription_policy._TARGET_PRIORITY_ORDER``,
# so at the IQFeed watch cap the evictions land on the Ross universe and the
# eligible-mover board — never on the hints. That ordering is right for
# ``first_alert`` (a name that JUST ignited on a tape we already hold) and wrong
# for the snapshot cross-section onset, which is speculative by construction and
# can contribute up to ``profile.max_universe`` distinct symbols per pull against
# a measured HINT baseline of mean 67.9 / max 117 distinct symbols per 180 s
# window. So onset hints are honored — but they are the FIRST thing the cap
# drops, never the Ross universe.
YIELDING_HINT_REASONS = frozenset({"snapshot_onset"})


def bridge_subscription_params(
    symbol: str, *, reason: str = "first_alert", now_utc: datetime | None = None
) -> dict | None:
    """Bind one hint row's parameters, or ``None`` when the hint must not be written.

    PURE — no I/O, no settings read beyond the kill switch, no clock unless the
    caller omits ``now_utc``. Split out of ``request_bridge_subscription`` so a
    caller can run the INSERT inside its OWN savepoint (see
    ``BRIDGE_SUBSCRIBE_INSERT_SQL``); the validation is identical, which is the
    point — two copies of the symbol hygiene would drift.
    """
    if not bool(getattr(settings, "chili_momentum_bridge_subscribe_on_alert_enabled", True)):
        return None
    sym = str(symbol or "").strip().upper()
    # These host bridges are equity-only. Reject every pair-shaped/hyphenated
    # symbol (including *-USDC), not just the historical *-USD spelling.
    if not sym or "-" in sym:
        return None
    sym = sym[:16]  # momentum_bridge_subscribe_requests.symbol is VARCHAR(16)
    _at = (now_utc or datetime.now(timezone.utc)).replace(tzinfo=None)
    return {"s": sym, "at": _at, "r": str(reason)[:32]}


def request_bridge_subscription(
    db: Any, symbol: str, *, reason: str = "first_alert", now_utc: datetime | None = None
) -> bool:
    """Write a subscribe HINT for ``symbol`` (the first-alert moment) so the IQFeed bridge
    picks it up on its fast path. Returns True on write, False when disabled / invalid / error.

    Container-side write to a NON-trading coordination table (allowed by matrix G3). Idempotent
    at the read side (the bridge de-dups against its current watch set); a repeated write within
    the fast window is harmless (just refreshes the freshness). Never raises — a failed hint must
    NEVER break the ignition/alert path that called it.

    F3 (capture-g fix): the INSERT runs inside a SAVEPOINT (``db.begin_nested()``). The hint
    shares the caller's session/transaction (the ignition score's viability/hub writes commit
    together with it); a plain failed INSERT — e.g. the table missing on a pre-mig-313 env —
    ABORTED the whole shared transaction, and swallowing the exception did NOT un-abort it, so
    the caller's ``db.commit()`` raised and EVERY ignition score for that tick rolled back. The
    savepoint confines the failure to the hint: on error we roll back TO the savepoint and the
    outer transaction stays healthy. Symbol is truncated to the column's VARCHAR(16).
    """
    params = bridge_subscription_params(symbol, reason=reason, now_utc=now_utc)
    if params is None:
        return False
    try:
        # SAVEPOINT: a failed hint INSERT must not poison the caller's transaction (the
        # ignition viability writes). begin_nested rolls back to the savepoint on error,
        # leaving the outer transaction committable.
        with db.begin_nested():
            db.execute(_sql(BRIDGE_SUBSCRIBE_INSERT_SQL), params)
        return True
    except Exception:
        # non-fatal by contract: missing table (pre-mig-313) / constraint / transient DB error
        # -> the savepoint already rolled back; the outer transaction is untouched.
        _log.debug("[bridge_subscribe] hint write failed sym=%s", params["s"], exc_info=True)
        return False


def select_fresh_subscribe_symbols(
    rows: Iterable[tuple[Any, Any]],
    *,
    now_utc: datetime,
    fresh_window_s: float = FRESH_WINDOW_S_DEFAULT,
    already_watched: set[str] | None = None,
    max_new: int | None = None,
) -> list[str]:
    """PURE fast-path trigger (no I/O — unit-testable): from ``(symbol, requested_at)`` rows
    (or ``(symbol, requested_at, reason)``), return the NEW symbols to subscribe NOW — those
    requested within ``fresh_window_s`` of ``now_utc`` and NOT already watched, de-duplicated,
    newest-first, capped at ``max_new``.

    YIELDING REASONS ([61] review 09-11). A symbol whose in-window hints are ALL yielding
    reasons (``YIELDING_HINT_REASONS``) sorts AFTER every other symbol regardless of
    freshness before this helper's max_new cap. The host readers also carry the
    classification into SourceRead.yielding_symbols for the final cross-source cap;
    sorting within HINT alone cannot protect ROSS/ELIGIBLE. One non-yielding hint
    is enough to keep ordinary HINT priority.
    HINT outranks ROSS and ELIGIBLE in the bridge's capacity priority, so without this a
    burst of speculative snapshot-onset hints (up to ``profile.max_universe`` distinct
    symbols per pull, against a measured baseline of mean 67.9 / max 117 distinct HINT
    symbols per 180 s window) evicts the tape of the names the lane is actually arming.
    Two-tuple rows carry no reason and therefore never yield — the previous behaviour.

    This is the tested SPECIFICATION of the fast-poll contract. The host bridge
    (``scripts/iqfeed_trade_bridge.py::_alert_symbols``) mirrors it in SQL (per-symbol
    ``max(requested_at)`` ordered DESC within the fresh window) because it stays standalone
    (no app-package import on the host) — F8: newest-first ordering is load-bearing there,
    since the fast poll breaks at the adaptive watch cap and the cap must keep the FRESHEST
    movers, never let a stale hint take the last slot.

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

    # PER-SYMBOL, exactly like the bridge's GROUP BY: `freshest` is the max over
    # ALL of that symbol's in-window hints (the pre-existing contract), and a
    # symbol YIELDS only when EVERY one of them is a yielding reason — one
    # first_alert is enough to keep its slot.
    freshest: dict[str, datetime] = {}
    yields: dict[str, bool] = {}
    for row in rows or []:
        try:
            sym, ts = row[0], row[1]
            reason = row[2] if len(row) > 2 else None
        except (IndexError, TypeError):
            continue
        s = str(sym or "").strip().upper()
        if not s or "-" in s:
            continue
        t = _naive(ts)
        if t is None or t < cutoff:
            continue
        if s not in freshest or t > freshest[s]:
            freshest[s] = t
        row_yields = str(reason or "") in YIELDING_HINT_REASONS
        yields[s] = row_yields if s not in yields else (yields[s] and row_yields)
    # Yielding symbols last; within each class newest-first, then symbol ASC —
    # the bridge's `ORDER BY yields ASC, freshest DESC, symbol ASC`, so the two
    # implementations of this contract agree row for row.
    ordered = sorted(freshest)                                   # symbol ASC
    ordered.sort(key=lambda s: freshest[s], reverse=True)         # freshest DESC
    ordered.sort(key=lambda s: 1 if yields.get(s) else 0)         # yields ASC
    for s in ordered:
        if s in watched or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if max_new is not None and len(out) >= int(max_new):
            break
    return out


# F8 note: the former recent_subscribe_requests(engine) reader was removed — it was dead code
# (the host bridge keeps its own standalone SQL reader, scripts/iqfeed_trade_bridge.py::
# _alert_symbols, which mirrors select_fresh_subscribe_symbols' newest-first contract).
