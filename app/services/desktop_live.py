"""Live desktop-cockpit view-model for CHILI OS.

Powers the auto-refreshing widgets on the workspace desktop home: today's P/L
and counts, trading-safety status (kill switch + drawdown breaker), and market
state. Read-only and defensive — every section is wrapped so a failure degrades
that widget to a neutral "unknown" state rather than 500-ing the poll. Reuses
existing read accessors; no new schema, no writes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _numbers(db: Session, user_id: Optional[int]) -> Dict[str, Any]:
    try:
        from .dashboard_summary import build_dashboard
        d = build_dashboard(db, user_id)
        t = d.get("trading") or {}
        return {
            "net_pnl_fmt": t.get("net_pnl_fmt") or "$0.00",
            "net_pnl_up": (t.get("net_pnl") or 0) >= 0,
            "win_rate_fmt": t.get("win_rate_fmt") or "—",
            "open_positions": len(t.get("open_positions") or []),
            "closes_today": len(t.get("closes") or []),
            "top_patterns": len(t.get("top_patterns") or []),
        }
    except Exception as e:
        logger.warning("[desktop_live] numbers failed: %s", e)
        return {"net_pnl_fmt": "$0.00", "net_pnl_up": True, "win_rate_fmt": "—",
                "open_positions": 0, "closes_today": 0, "top_patterns": 0}


def _lists(db: Session, user_id: Optional[int]) -> Dict[str, Any]:
    # Compact live lists for the glance widgets: currently-open tickers/sides and
    # the latest few closed trades (already money-formatted by dashboard_summary).
    # Each section degrades to [] on failure so the poll never 500s.
    try:
        from .dashboard_summary import build_dashboard
        d = build_dashboard(db, user_id)
        t = d.get("trading") or {}
        positions = [
            {"ticker": p.get("ticker"), "side": p.get("side") or ""}
            for p in (t.get("open_positions") or []) if isinstance(p, dict)
        ][:6]
        closes = [
            {"ticker": c.get("ticker"), "pattern": c.get("pattern") or "—",
             "pnl_fmt": c.get("pnl_fmt") or "—", "pnl_up": bool(c.get("pnl_up"))}
            for c in (t.get("closes_fmt") or []) if isinstance(c, dict)
        ][:5]
        return {"positions": positions, "closes": closes}
    except Exception as e:
        logger.warning("[desktop_live] lists failed: %s", e)
        return {"positions": [], "closes": []}


def _kill_switch() -> Dict[str, Any]:
    # active=True means trading is HALTED (the alarm state). On read failure we
    # report ok=False so the widget shows "unknown" rather than a false "clear".
    try:
        from .trading.governance import get_kill_switch_status
        s = get_kill_switch_status() or {}
        return {"ok": True, "active": bool(s.get("active")), "reason": s.get("reason")}
    except Exception as e:
        logger.warning("[desktop_live] kill switch read failed: %s", e)
        return {"ok": False, "active": False, "reason": None}


def _breaker() -> Dict[str, Any]:
    # In-process pattern-tier breaker state (cheap, restored from DB on startup).
    try:
        from .trading.portfolio_risk import get_breaker_status
        s = get_breaker_status() or {}
        return {"ok": True, "tripped": bool(s.get("tripped")), "reason": s.get("reason")}
    except Exception as e:
        logger.warning("[desktop_live] breaker read failed: %s", e)
        return {"ok": False, "tripped": False, "reason": None}


def _market() -> Dict[str, Any]:
    try:
        from .trading.momentum_neural.market_profile import market_open_now
        return {"ok": True, "equities_open": bool(market_open_now("SPY")), "crypto_open": True}
    except Exception as e:
        logger.warning("[desktop_live] market state failed: %s", e)
        return {"ok": False, "equities_open": None, "crypto_open": True}


def _last_activity(db: Session, user_id: Optional[int]) -> Optional[str]:
    # Timestamp of the trading brain's most recent action for this user, as an
    # ISO-8601 UTC string (``...Z``). "Activity" = the most recent of trade
    # open (``entry_date``) or close (``exit_date``); ``exit_date`` is NULL while
    # a position is open, so we max the GREATEST of the two. Guest → None.
    # Any failure (or no trades) degrades to None so the cockpit shows "—".
    if user_id is None:
        return None
    try:
        from datetime import timezone
        from sqlalchemy import func
        from ..models.trading import Trade
        latest = (
            db.query(func.max(func.greatest(Trade.entry_date, Trade.exit_date)))
            .filter(Trade.user_id == user_id)
            .scalar()
        )
        if latest is None:
            return None
        # Stored naive-UTC (datetime.utcnow); treat naive as UTC, normalize aware.
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        else:
            latest = latest.astimezone(timezone.utc)
        return latest.isoformat().replace("+00:00", "Z")
    except Exception as e:
        logger.warning("[desktop_live] last activity read failed: %s", e)
        return None


def _data_fresh(db: Session) -> Optional[str]:
    # Timestamp of the most recently ingested market-data snapshot, as an
    # ISO-8601 UTC string (``...Z``). This is GLOBAL (not per-user) — the
    # learning cycle mines patterns from ``trading_snapshots``, whose
    # ``snapshot_date`` column is non-null on every row and stamped at capture,
    # so its MAX is the freshest proof the data pipeline is alive. Distinct from
    # "last trade": this advances even when the brain isn't trading. Any failure
    # (or an empty table) degrades to None so the cockpit shows "—".
    try:
        from datetime import timezone
        from sqlalchemy import func
        from ..models.trading import MarketSnapshot
        latest = db.query(func.max(MarketSnapshot.snapshot_date)).scalar()
        if latest is None:
            return None
        # Stored naive-UTC; treat naive as UTC, normalize aware.
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        else:
            latest = latest.astimezone(timezone.utc)
        return latest.isoformat().replace("+00:00", "Z")
    except Exception as e:
        logger.warning("[desktop_live] data freshness read failed: %s", e)
        return None


def _merge_unrealized(out: Dict[str, Any], db: Session, user_id: Optional[int]) -> None:
    """Enrich the open-positions list with live unrealized P/L + a portfolio total
    (read-only, via cockpit_pnl). Defensive: a quote outage leaves the positions
    list intact and the total as None, so the cockpit simply omits the P/L."""
    try:
        from .cockpit_pnl import build_unrealized
        u = build_unrealized(db, user_id)
        by = u.get("by_ticker") or {}
        for p in out.get("positions") or []:
            info = by.get(p.get("ticker"))
            if info and info.get("priced"):
                p["pnl_fmt"] = info.get("pnl_fmt")
                p["pnl_up"] = info.get("pnl_up")
                p["pnl_pct_fmt"] = info.get("pnl_pct_fmt")
        out["unrealized_total_fmt"] = u.get("total_fmt")
        out["unrealized_total_up"] = bool(u.get("total_up"))
        out["unrealized_priced"] = u.get("priced")
    except Exception as e:
        logger.warning("[desktop_live] unrealized merge failed: %s", e)
        out["unrealized_total_fmt"] = None


def build_live(db: Session, user_id: Optional[int]) -> Dict[str, Any]:
    """Return the compact live cockpit view-model (safe to poll repeatedly)."""
    out: Dict[str, Any] = {"ok": True}
    out.update(_numbers(db, user_id))
    out.update(_lists(db, user_id))
    _merge_unrealized(out, db, user_id)
    out["kill_switch"] = _kill_switch()
    out["breaker"] = _breaker()
    out["market"] = _market()
    out["last_trade_iso"] = _last_activity(db, user_id)
    out["data_fresh_iso"] = _data_fresh(db)
    return out
