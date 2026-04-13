"""Paper trading simulation for promoted patterns.

Auto-enters paper trades when a promoted pattern fires a signal,
auto-exits on stop/target/expiry, and tracks simulated P&L.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import PaperTrade, ScanPattern

logger = logging.getLogger(__name__)

DEFAULT_PAPER_CAPITAL = 100_000.0
MAX_OPEN_PAPER_TRADES = 20
PAPER_TRADE_EXPIRY_DAYS = 5


def open_paper_trade(
    db: Session,
    user_id: int | None,
    ticker: str,
    entry_price: float,
    *,
    scan_pattern_id: int | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    direction: str = "long",
    quantity: int = 100,
    signal_json: dict | None = None,
) -> PaperTrade | None:
    """Open a simulated paper trade."""
    open_count = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.status == "open",
    ).count()
    if open_count >= MAX_OPEN_PAPER_TRADES:
        logger.debug("[paper] Max open paper trades (%d) reached", MAX_OPEN_PAPER_TRADES)
        return None

    existing = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.ticker == ticker.upper(),
        PaperTrade.status == "open",
        PaperTrade.scan_pattern_id == scan_pattern_id,
    ).first()
    if existing:
        logger.debug("[paper] Already have open paper trade for %s pattern %s", ticker, scan_pattern_id)
        return None

    if stop_price is None:
        stop_price = entry_price * 0.97  # default 3% stop

    if target_price is None:
        risk = abs(entry_price - stop_price)
        target_price = entry_price + (risk * 2)  # 2:1 R:R default

    pt = PaperTrade(
        user_id=user_id,
        scan_pattern_id=scan_pattern_id,
        ticker=ticker.upper(),
        direction=direction,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        quantity=quantity,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json=signal_json or {},
    )
    db.add(pt)
    db.flush()
    logger.info("[paper] Opened paper trade: %s %s @ %.2f (stop=%.2f, target=%.2f)",
                direction, ticker, entry_price, stop_price, target_price)
    return pt


def check_paper_exits(db: Session, user_id: int | None = None) -> dict[str, Any]:
    """Check all open paper trades against current prices for stop/target/expiry."""
    from .market_data import fetch_quote

    open_trades = db.query(PaperTrade).filter(
        PaperTrade.status == "open",
    )
    if user_id is not None:
        open_trades = open_trades.filter(PaperTrade.user_id == user_id)
    open_trades = open_trades.all()

    if not open_trades:
        return {"checked": 0, "closed": 0}

    def _paper_close_ledger(db_sess: Session, ptx: PaperTrade) -> None:
        try:
            from .brain_work.execution_hooks import on_paper_trade_closed

            on_paper_trade_closed(db_sess, ptx)
        except Exception:
            pass

    closed = 0
    for pt in open_trades:
        try:
            quote = fetch_quote(pt.ticker)
            if not quote or not quote.get("price"):
                # Check expiry even without price
                if pt.entry_date and (datetime.utcnow() - pt.entry_date).days >= PAPER_TRADE_EXPIRY_DAYS:
                    _close_paper_trade(pt, pt.entry_price, "expired")
                    _paper_close_ledger(db, pt)
                    closed += 1
                continue

            price = float(quote["price"])
            is_long = pt.direction == "long"

            # Stop hit
            if is_long and pt.stop_price and price <= pt.stop_price:
                _close_paper_trade(pt, pt.stop_price, "stop")
                _paper_close_ledger(db, pt)
                closed += 1
            elif not is_long and pt.stop_price and price >= pt.stop_price:
                _close_paper_trade(pt, pt.stop_price, "stop")
                _paper_close_ledger(db, pt)
                closed += 1
            # Target hit
            elif is_long and pt.target_price and price >= pt.target_price:
                _close_paper_trade(pt, pt.target_price, "target")
                _paper_close_ledger(db, pt)
                closed += 1
            elif not is_long and pt.target_price and price <= pt.target_price:
                _close_paper_trade(pt, pt.target_price, "target")
                _paper_close_ledger(db, pt)
                closed += 1
            # Expiry
            elif pt.entry_date and (datetime.utcnow() - pt.entry_date).days >= PAPER_TRADE_EXPIRY_DAYS:
                _close_paper_trade(pt, price, "expired")
                _paper_close_ledger(db, pt)
                closed += 1
        except Exception as e:
            logger.debug("[paper] Error checking %s: %s", pt.ticker, e)

    if closed > 0:
        db.commit()

    return {"checked": len(open_trades), "closed": closed}


def _close_paper_trade(pt: PaperTrade, exit_price: float, reason: str) -> None:
    """Close a paper trade with P&L calculation."""
    pt.status = "closed"
    pt.exit_date = datetime.utcnow()
    pt.exit_price = exit_price
    pt.exit_reason = reason

    if pt.direction == "long":
        pt.pnl = round((exit_price - pt.entry_price) * pt.quantity, 2)
        pt.pnl_pct = round((exit_price - pt.entry_price) / pt.entry_price * 100, 2)
    else:
        pt.pnl = round((pt.entry_price - exit_price) * pt.quantity, 2)
        pt.pnl_pct = round((pt.entry_price - exit_price) / pt.entry_price * 100, 2)

    logger.info("[paper] Closed %s %s @ %.2f (%s) P&L: $%.2f (%.2f%%)",
                pt.direction, pt.ticker, exit_price, reason, pt.pnl, pt.pnl_pct)


def get_paper_dashboard(db: Session, user_id: int | None = None) -> dict[str, Any]:
    """Get paper trading performance summary."""
    open_trades = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.status == "open",
    ).all()

    closed_trades = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.status == "closed",
    ).all()

    total_pnl = sum(t.pnl or 0 for t in closed_trades)
    wins = [t for t in closed_trades if (t.pnl or 0) > 0]
    losses = [t for t in closed_trades if (t.pnl or 0) <= 0]
    win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0

    stops_hit = sum(1 for t in closed_trades if t.exit_reason == "stop")
    targets_hit = sum(1 for t in closed_trades if t.exit_reason == "target")
    expired = sum(1 for t in closed_trades if t.exit_reason == "expired")

    # Per-pattern attribution
    sp_pnl: dict[int, list[float]] = {}
    for t in closed_trades:
        if t.scan_pattern_id:
            sp_pnl.setdefault(t.scan_pattern_id, []).append(t.pnl or 0)

    sp_ids = list(sp_pnl.keys())
    sp_names = {}
    if sp_ids:
        for sp in db.query(ScanPattern).filter(ScanPattern.id.in_(sp_ids)).all():
            sp_names[sp.id] = sp.name

    pattern_stats = sorted([
        {
            "pattern_id": sp_id,
            "pattern_name": sp_names.get(sp_id, f"#{sp_id}"),
            "trades": len(pnls),
            "pnl": round(sum(pnls), 2),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
        }
        for sp_id, pnls in sp_pnl.items()
    ], key=lambda x: x["pnl"], reverse=True)

    return {
        "ok": True,
        "open_trades": len(open_trades),
        "closed_trades": len(closed_trades),
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 1),
        "wins": len(wins),
        "losses": len(losses),
        "stops_hit": stops_hit,
        "targets_hit": targets_hit,
        "expired": expired,
        "pattern_stats": pattern_stats[:10],
        "open": [
            {
                "id": t.id, "ticker": t.ticker, "direction": t.direction,
                "entry": t.entry_price, "stop": t.stop_price, "target": t.target_price,
                "pattern_id": t.scan_pattern_id,
                "entry_date": t.entry_date.isoformat() if t.entry_date else None,
            }
            for t in open_trades
        ],
    }


def auto_enter_from_signals(
    db: Session,
    user_id: int | None,
    signals: list[dict[str, Any]],
    capital: float = DEFAULT_PAPER_CAPITAL,
) -> int:
    """Automatically open paper trades from high-confidence signals.

    Each signal dict should have: ticker, entry_price, stop_price, target_price,
    scan_pattern_id, confidence.
    """
    from .portfolio_risk import size_position

    entered = 0
    for sig in signals:
        conf = sig.get("confidence", 0)
        if conf < 0.6:
            continue

        entry = sig.get("entry_price") or sig.get("price")
        stop = sig.get("stop_price") or sig.get("stop")
        target = sig.get("target_price") or sig.get("target")
        if not entry or entry <= 0:
            continue
        if not stop:
            stop = entry * 0.97

        qty = size_position(capital, entry, stop, risk_pct=0.5)
        if qty <= 0:
            qty = 10

        pt = open_paper_trade(
            db, user_id,
            ticker=sig["ticker"],
            entry_price=entry,
            scan_pattern_id=sig.get("scan_pattern_id"),
            stop_price=stop,
            target_price=target,
            quantity=qty,
            signal_json=sig,
        )
        if pt:
            entered += 1

    if entered > 0:
        db.commit()

    return entered
