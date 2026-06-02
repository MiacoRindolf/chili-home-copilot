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


def build_live(db: Session, user_id: Optional[int]) -> Dict[str, Any]:
    """Return the compact live cockpit view-model (safe to poll repeatedly)."""
    out: Dict[str, Any] = {"ok": True}
    out.update(_numbers(db, user_id))
    out["kill_switch"] = _kill_switch()
    out["breaker"] = _breaker()
    out["market"] = _market()
    return out
