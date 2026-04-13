"""Emergency liquidation protocol.

Closes ALL open positions when catastrophic conditions are met:
- Portfolio drawdown exceeds critical threshold (distinct from pause-new-trades breaker)
- System disconnect detected (no price updates for > N minutes)
- Manual emergency trigger

Also supports partial exposure reduction (softer alternative to full close-all).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import PaperTrade, Trade

logger = logging.getLogger(__name__)

EMERGENCY_DD_THRESHOLD_PCT = 20.0
DISCONNECT_TIMEOUT_MINUTES = 10
PARTIAL_REDUCE_FRACTION = 0.5

_last_price_update: datetime | None = None


def record_price_heartbeat() -> None:
    """Call this whenever a successful price fetch occurs to track connectivity."""
    global _last_price_update
    _last_price_update = datetime.utcnow()


def is_disconnected(timeout_minutes: int = DISCONNECT_TIMEOUT_MINUTES) -> bool:
    """True if no price updates received within the timeout window."""
    if _last_price_update is None:
        return False
    return (datetime.utcnow() - _last_price_update) > timedelta(minutes=timeout_minutes)


def emergency_close_all(
    db: Session,
    user_id: int | None = None,
    reason: str = "manual_emergency",
) -> dict[str, Any]:
    """Close ALL open positions (paper and live) immediately at market price."""
    from .market_data import fetch_quote
    from .governance import activate_kill_switch

    activate_kill_switch(reason=f"emergency_liquidation: {reason}")

    closed_paper = 0
    closed_live = 0
    errors = []

    paper_q = db.query(PaperTrade).filter(PaperTrade.status == "open")
    if user_id is not None:
        paper_q = paper_q.filter(PaperTrade.user_id == user_id)
    open_paper = paper_q.all()

    for pt in open_paper:
        try:
            q = fetch_quote(pt.ticker)
            price = float(q["price"]) if q and q.get("price") else pt.entry_price
            pt.status = "closed"
            pt.exit_date = datetime.utcnow()
            pt.exit_price = price
            pt.exit_reason = f"emergency_{reason}"
            if pt.direction == "long":
                pt.pnl = round((price - pt.entry_price) * pt.quantity, 2)
                pt.pnl_pct = round((price - pt.entry_price) / pt.entry_price * 100, 2)
            else:
                pt.pnl = round((pt.entry_price - price) * pt.quantity, 2)
                pt.pnl_pct = round((pt.entry_price - price) / pt.entry_price * 100, 2)
            closed_paper += 1
        except Exception as e:
            errors.append(f"Paper {pt.ticker}: {e}")

    live_q = db.query(Trade).filter(Trade.status == "open")
    if user_id is not None:
        live_q = live_q.filter(Trade.user_id == user_id)
    open_live = live_q.all()

    for t in open_live:
        try:
            q = fetch_quote(t.ticker)
            price = float(q["price"]) if q and q.get("price") else t.entry_price
            t.status = "closed"
            t.exit_date = datetime.utcnow()
            t.exit_price = price
            t.exit_reason = f"emergency_{reason}"
            if hasattr(t, "pnl"):
                t.pnl = round((price - t.entry_price) * (t.quantity or 1), 2)
            closed_live += 1
        except Exception as e:
            errors.append(f"Live {t.ticker}: {e}")

    db.commit()

    logger.critical(
        "[EMERGENCY] Liquidated %d paper + %d live trades. Reason: %s. Errors: %d",
        closed_paper, closed_live, reason, len(errors),
    )

    return {
        "ok": True,
        "closed_paper": closed_paper,
        "closed_live": closed_live,
        "total_closed": closed_paper + closed_live,
        "reason": reason,
        "errors": errors,
        "kill_switch_activated": True,
    }


def partial_reduce_exposure(
    db: Session,
    user_id: int | None = None,
    reduce_fraction: float = PARTIAL_REDUCE_FRACTION,
    reason: str = "drawdown_warning",
) -> dict[str, Any]:
    """Close a fraction of open positions (most-losing first) as a softer alternative."""
    from .market_data import fetch_quote

    paper_q = db.query(PaperTrade).filter(PaperTrade.status == "open")
    if user_id is not None:
        paper_q = paper_q.filter(PaperTrade.user_id == user_id)
    open_positions = paper_q.all()

    if not open_positions:
        return {"ok": True, "closed": 0, "reason": "no_open_positions"}

    position_pnl = []
    for pos in open_positions:
        try:
            q = fetch_quote(pos.ticker)
            if q and q.get("price"):
                price = float(q["price"])
                unrealized = (
                    (price - pos.entry_price) * pos.quantity
                    if pos.direction == "long"
                    else (pos.entry_price - price) * pos.quantity
                )
                position_pnl.append((pos, unrealized, price))
        except Exception:
            continue

    position_pnl.sort(key=lambda x: x[1])

    n_to_close = max(1, int(len(position_pnl) * reduce_fraction))
    closed = 0

    for pos, _, price in position_pnl[:n_to_close]:
        pos.status = "closed"
        pos.exit_date = datetime.utcnow()
        pos.exit_price = price
        pos.exit_reason = f"partial_reduce_{reason}"
        if pos.direction == "long":
            pos.pnl = round((price - pos.entry_price) * pos.quantity, 2)
            pos.pnl_pct = round((price - pos.entry_price) / pos.entry_price * 100, 2)
        else:
            pos.pnl = round((pos.entry_price - price) * pos.quantity, 2)
            pos.pnl_pct = round((pos.entry_price - price) / pos.entry_price * 100, 2)
        closed += 1

    db.commit()

    logger.warning(
        "[emergency] Partial reduce: closed %d of %d positions (%.0f%%). Reason: %s",
        closed, len(open_positions), reduce_fraction * 100, reason,
    )

    return {
        "ok": True,
        "closed": closed,
        "total_open_before": len(open_positions),
        "reduce_fraction": reduce_fraction,
        "reason": reason,
    }


def check_emergency_conditions(
    db: Session,
    user_id: int | None = None,
    capital: float = 100_000.0,
    critical_dd_pct: float = EMERGENCY_DD_THRESHOLD_PCT,
) -> dict[str, Any]:
    """Evaluate whether emergency liquidation conditions are met.

    Returns action recommendation without executing it.
    """
    from .portfolio_optimizer import check_portfolio_drawdown

    dd = check_portfolio_drawdown(db, user_id, capital)
    disconnected = is_disconnected()

    action = "none"
    if dd.get("dd_pct", 0) < -critical_dd_pct:
        action = "emergency_close_all"
    elif disconnected:
        action = "emergency_close_all"
    elif dd.get("dd_pct", 0) < -(critical_dd_pct * 0.6):
        action = "partial_reduce"

    return {
        "ok": True,
        "drawdown_pct": dd.get("dd_pct", 0),
        "critical_threshold": critical_dd_pct,
        "disconnected": disconnected,
        "recommended_action": action,
        "open_positions": dd.get("open_positions", 0),
    }
