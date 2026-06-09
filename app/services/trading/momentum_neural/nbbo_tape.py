"""NBBO spread tape — persist the CLEAN consolidated bid/ask (Massive snapshot
``lastQuote``) for the Ross momentum universe each cycle, so the spread-sensitive
replay can use REAL spreads instead of a proxy.

Why this exists (project_momentum_zero_fills_root_cause): OHLCV bars carry no
bid/ask, so the day-replay proxied spread from dollar-volume — which was 6-17x too
tight for explosive low-float names (PAVS proxy 53bps vs real 317bps), making the
replay PnL meaningless. Reconstructing the NBBO from raw per-exchange ``/v3/quotes``
proved unreliable too (crossed/locked/stale quotes -> a PAVS rebuild read 60bps vs
the 317bps the live lane actually saw). BUT the live lane already receives the clean
consolidated NBBO from Massive's snapshot ``lastQuote`` (P=ask, p=bid) every cycle —
the exact 317bps it recorded. So we simply PERSIST what we already see: every future
day then has a real intraday spread per name, and the replay reads it back.

Design notes:
  * Source = Massive snapshot ``lastQuote`` (already consolidated by the vendor),
    NOT raw quotes (no fragile NBBO reconstruction).
  * Sampled only for the Ross universe (price 1-20, $vol>=1M, |change|>=5%) — the
    names a replay actually considers — to bound row volume.
  * RTH-gated (quotes are stale/wide outside regular hours).
  * Retention-bounded (the exit_parity_log bloat lesson): prune older than the
    window; both indexes keep the read and the prune cheap.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings

logger = logging.getLogger(__name__)

# Ross small-cap profile bounds (mirror universe.EQUITY_ROSS_SMALLCAP defaults; kept
# local so the sampler is self-contained and cannot break the trade path on import).
_PRICE_MIN = 1.0
_PRICE_MAX = 20.0
_MIN_DOLLAR_VOLUME = 1_000_000.0
_MIN_ABS_CHANGE_PCT = 5.0
# Regular US trading hours in UTC (13:30-20:00). Quotes are live only here; outside
# it the snapshot lastQuote is a stale overnight quote (absurd spreads) — skip.
_RTH_OPEN = time(13, 30)
_RTH_CLOSE = time(20, 0)
_MAX_SANE_SPREAD_BPS = 5_000.0  # >50% round-trip = stale/garbage quote, not a real market


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _is_rth(now_utc: datetime) -> bool:
    # Weekday + within RTH window (UTC). Holidays aren't excluded — a holiday simply
    # yields stale quotes that the per-row sanity filter drops, so no harm.
    if now_utc.weekday() >= 5:
        return False
    t = now_utc.timetz().replace(tzinfo=None)
    return _RTH_OPEN <= t <= _RTH_CLOSE


def _ross_row(s: dict[str, Any]) -> Optional[dict[str, Any]]:
    """If a snapshot entry is a Ross-universe mover with a usable clean NBBO, return
    its tape row dict; else None. Pure + side-effect-free."""
    if not isinstance(s, dict):
        return None
    sym = str(s.get("ticker") or "").strip().upper()
    if not sym or sym.endswith("-USD"):
        return None
    day = s.get("day") if isinstance(s.get("day"), dict) else {}
    if not day.get("c"):
        day = s.get("prevDay") if isinstance(s.get("prevDay"), dict) else {}
    px = _f(day.get("c")) or _f(day.get("vw"))
    vol = _f(day.get("v")) or 0.0
    op = _f(day.get("o"))
    if not px or px <= 0:
        return None
    if not (_PRICE_MIN <= px <= _PRICE_MAX):
        return None
    if px * vol < _MIN_DOLLAR_VOLUME:
        return None
    chg = ((px - op) / op * 100.0) if (op and op > 0) else 0.0
    if abs(chg) < _MIN_ABS_CHANGE_PCT:
        return None
    lq = s.get("lastQuote") if isinstance(s.get("lastQuote"), dict) else (s.get("last_quote") or {})
    bid = _f(lq.get("p")) or _f(lq.get("bid_price"))
    ask = _f(lq.get("P")) or _f(lq.get("ask_price"))
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10_000.0 if mid > 0 else None
    if spread_bps is None or spread_bps > _MAX_SANE_SPREAD_BPS:
        return None
    return {
        "symbol": sym, "bid": bid, "ask": ask, "mid": mid,
        "spread_bps": spread_bps, "day_volume": vol,
    }


def sample_universe_nbbo_spreads(db: Session, *, now_utc: Optional[datetime] = None) -> dict[str, Any]:
    """Read the current Massive snapshot, keep the Ross-universe movers with a clean
    NBBO, and batch-insert their spreads into the tape. RTH-gated; best-effort (never
    raises — a sampler failure must not affect anything else). Returns a small summary."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if not _is_rth(now_utc):
        return {"ok": True, "skipped": "outside_rth", "inserted": 0}
    try:
        from ...massive_client import get_full_market_snapshot
        snap = get_full_market_snapshot(max_age_seconds=120) or []
    except Exception as exc:  # pragma: no cover - network/vendor
        logger.debug("[nbbo_tape] snapshot fetch failed: %s", exc)
        return {"ok": False, "error": "snapshot_unavailable", "inserted": 0}

    rows = []
    for s in snap:
        r = _ross_row(s)
        if r is not None:
            rows.append(r)
    if not rows:
        return {"ok": True, "inserted": 0, "universe": 0}

    stamped = now_utc.replace(tzinfo=None)
    for r in rows:
        r["observed_at"] = stamped
    try:
        db.execute(
            text(
                "INSERT INTO momentum_nbbo_spread_tape "
                "(symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source) "
                "VALUES (:symbol, :observed_at, :bid, :ask, :mid, :spread_bps, :day_volume, 'massive_snapshot')"
            ),
            rows,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("[nbbo_tape] insert failed: %s", exc)
        return {"ok": False, "error": "insert_failed", "inserted": 0}
    logger.info("[nbbo_tape] sampled %d Ross-universe NBBO spreads", len(rows))
    return {"ok": True, "inserted": len(rows)}


def prune_nbbo_tape(db: Session, *, retention_days: Optional[int] = None) -> dict[str, Any]:
    """Trim rows older than the retention window (the exit_parity_log bloat lesson).
    The observed_at index makes this cheap. Best-effort."""
    days = int(retention_days if retention_days is not None
               else getattr(settings, "chili_momentum_nbbo_tape_retention_days", 30) or 30)
    try:
        res = db.execute(
            text("DELETE FROM momentum_nbbo_spread_tape WHERE observed_at < (now() - make_interval(days => :d))"),
            {"d": days},
        )
        db.commit()
        n = int(getattr(res, "rowcount", 0) or 0)
        if n:
            logger.info("[nbbo_tape] pruned %d rows older than %dd", n, days)
        return {"ok": True, "pruned": n, "retention_days": days}
    except Exception as exc:
        db.rollback()
        logger.warning("[nbbo_tape] prune failed: %s", exc)
        return {"ok": False, "error": str(exc)[:120]}


def read_spread_profile(
    db: Session, symbol: str, *, day: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Replay reader: a name's recorded intraday spread samples (optionally for one
    UTC date, 'YYYY-MM-DD'), oldest first. Empty when the tape has no data yet."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return []
    sql = ("SELECT observed_at, bid, ask, mid, spread_bps, day_volume "
           "FROM momentum_nbbo_spread_tape WHERE symbol = :s")
    params: dict[str, Any] = {"s": sym}
    if day:
        sql += " AND observed_at::date = :d"
        params["d"] = day
    sql += " ORDER BY observed_at ASC"
    try:
        rows = db.execute(text(sql), params).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("[nbbo_tape] read_spread_profile failed: %s", exc)
        return []
