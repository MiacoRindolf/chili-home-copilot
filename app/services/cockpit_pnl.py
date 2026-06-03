"""Read-only per-position unrealized P/L for the CHILI OS cockpit.

Aggregates open positions' unrealized P/L using live quotes. **Strictly
read-only**: it reads open ``Trade`` rows and fetches quotes — it never writes,
never places or modifies an order. Cached per user with a short TTL so the 20s
cockpit poll doesn't hammer quote providers, and defensive throughout so a quote
outage degrades to a neutral/empty result rather than 500-ing the poll.

Reuses existing infrastructure (no reinvention):
- ``trading_summary._open_trades`` — the canonical open-positions query.
- ``trading.market_data.fetch_quote`` — the Massive→Polygon→yfinance quote chain
  (itself price-bus cached ~5s).
- ``trading.autotrader_desk._compute_unrealized`` — the long/short-aware P/L math
  used by the trading desk, so cockpit P/L matches the desk exactly.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Per-user cache: user_id -> (timestamp, result). Hard TTL + size cap (CLAUDE.md:
# "caches must have hard max size + TTL"). TTL matches the 20s cockpit poll.
_PNL_CACHE: Dict[Optional[int], tuple] = {}
_PNL_LOCK = threading.Lock()
_PNL_TTL = 20.0   # seconds
_PNL_MAX = 500    # hard size cap (distinct users)
_POS_CAP = 16     # bound the number of quote fetches per build


def _fmt_money(v: Optional[float]) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{'-' if f < 0 else ''}${abs(f):,.2f}"


def _aggregate(trades: List[Any], quote_fn: Callable[[str], Optional[float]]) -> Dict[str, Any]:
    """Pure (no DB / network): given open trades + an injected ``quote_fn(ticker)
    -> price|None``, compute per-ticker unrealized P/L + the total. Each trade is
    duck-typed on ``ticker``/``entry_price``/``quantity``/``direction``."""
    from .trading.autotrader_desk import _compute_unrealized

    by_ticker: Dict[str, Any] = {}
    total = 0.0
    priced = 0
    count = 0
    for t in trades:
        ticker = getattr(t, "ticker", None)
        if not ticker:
            continue
        count += 1
        try:
            price = quote_fn(ticker)
        except Exception:
            price = None
        pnl_usd, pnl_pct = _compute_unrealized(
            entry_price=getattr(t, "entry_price", None),
            current_price=price,
            quantity=getattr(t, "quantity", None),
            direction=getattr(t, "direction", None),
        )
        if pnl_usd is None:
            by_ticker[ticker] = {"priced": False}
            continue
        priced += 1
        total += pnl_usd
        by_ticker[ticker] = {
            "priced": True,
            "pnl_fmt": _fmt_money(pnl_usd),
            "pnl_up": pnl_usd >= 0,
            "pnl_pct_fmt": (f"{pnl_pct:+.2f}%" if pnl_pct is not None else "—"),
        }
    return {
        "by_ticker": by_ticker,
        "count": count,
        "priced": priced,
        "total": round(total, 2),                       # numeric, for combining with realized P/L
        "total_fmt": (_fmt_money(total) if priced else None),
        "total_up": total >= 0,
    }


def _empty() -> Dict[str, Any]:
    return {"by_ticker": {}, "count": 0, "priced": 0, "total": 0.0, "total_fmt": None, "total_up": True}


def _cumulative(pnls: List[float], cap: int = 60) -> Dict[str, Any]:
    """Pure: a list of per-close realized P/L values → the running cumulative
    curve for a sparkline (capped to the most recent ``cap`` points)."""
    cum = 0.0
    pts: List[float] = []
    for v in pnls:
        try:
            cum += float(v)
        except (TypeError, ValueError):
            continue
        pts.append(round(cum, 2))
    if len(pts) > cap:
        pts = pts[-cap:]
    last = pts[-1] if pts else None
    return {
        "points": pts,
        "count": len(pts),
        "last_fmt": (_fmt_money(last) if last is not None else None),
        "up": (last >= 0) if last is not None else True,
    }


# Separate cache for the intraday curve (changes slowly — closes, not quotes).
_CURVE_CACHE: Dict[Optional[int], tuple] = {}
_CURVE_TTL = 45.0


def build_intraday_curve(db: Session, user_id: Optional[int], window_hours: int = 24) -> Dict[str, Any]:
    """Read-only cumulative realized-P/L curve over the trailing window (matches
    the "Net P/L (24h)" figure): closed trades ordered oldest→newest, running
    sum of their P/L. Cached per user; neutral empty on any failure."""
    now = time.time()
    with _PNL_LOCK:
        hit = _CURVE_CACHE.get(user_id)
        if hit and now - hit[0] < _CURVE_TTL:
            return hit[1]
    result = _cumulative([])
    try:
        if user_id:
            from datetime import datetime, timedelta
            from ..models import Trade
            since = datetime.utcnow() - timedelta(hours=window_hours)
            rows = (
                db.query(Trade.pnl)
                .filter(
                    Trade.user_id == user_id,
                    Trade.status == "closed",
                    Trade.exit_date.isnot(None),
                    Trade.exit_date >= since,
                    Trade.pnl.isnot(None),
                )
                .order_by(Trade.exit_date.asc())
                .limit(300)
                .all()
            )
            result = _cumulative([r[0] for r in rows])
    except Exception as e:
        logger.warning("[cockpit_pnl] intraday curve build failed: %s", e)
        result = _cumulative([])
    with _PNL_LOCK:
        if len(_CURVE_CACHE) >= _PNL_MAX:
            cutoff = now - _CURVE_TTL
            for k in [k for k, v in _CURVE_CACHE.items() if v[0] < cutoff]:
                _CURVE_CACHE.pop(k, None)
            if len(_CURVE_CACHE) >= _PNL_MAX:
                _CURVE_CACHE.clear()
        _CURVE_CACHE[user_id] = (now, result)
    return result


def build_unrealized(db: Session, user_id: Optional[int]) -> Dict[str, Any]:
    """Read-only per-position unrealized P/L + total for the cockpit. Cached per
    user for ``_PNL_TTL`` seconds; returns a neutral empty result on any failure."""
    now = time.time()
    with _PNL_LOCK:
        hit = _PNL_CACHE.get(user_id)
        if hit and now - hit[0] < _PNL_TTL:
            return hit[1]
    try:
        from .trading_summary import _open_trades
        from .trading.market_data import fetch_quote

        trades = _open_trades(db, user_id) if user_id else []

        def quote_fn(ticker: str) -> Optional[float]:
            q = fetch_quote(ticker)
            return (q or {}).get("price")

        result = _aggregate(list(trades)[:_POS_CAP], quote_fn)
    except Exception as e:
        logger.warning("[cockpit_pnl] unrealized build failed: %s", e)
        result = _empty()
    with _PNL_LOCK:
        if len(_PNL_CACHE) >= _PNL_MAX:
            cutoff = now - _PNL_TTL
            for k in [k for k, v in _PNL_CACHE.items() if v[0] < cutoff]:
                _PNL_CACHE.pop(k, None)
            if len(_PNL_CACHE) >= _PNL_MAX:
                _PNL_CACHE.clear()
        _PNL_CACHE[user_id] = (now, result)
    return result
