"""Portfolio: watchlist CRUD, trade CRUD, P&L analytics, portfolio summary."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import JournalEntry, Trade, TradingInsight, WatchlistItem
from .market_data import fetch_quote, get_indicator_snapshot

logger = logging.getLogger(__name__)


# ── Watchlist CRUD ────────────────────────────────────────────────────

def get_watchlist(db: Session, user_id: int | None) -> list[WatchlistItem]:
    return db.query(WatchlistItem).filter(
        WatchlistItem.user_id == user_id
    ).order_by(WatchlistItem.added_at.desc()).all()


def add_to_watchlist(db: Session, user_id: int | None, ticker: str) -> WatchlistItem:
    existing = db.query(WatchlistItem).filter(
        WatchlistItem.user_id == user_id,
        WatchlistItem.ticker == ticker.upper(),
    ).first()
    if existing:
        return existing
    item = WatchlistItem(user_id=user_id, ticker=ticker.upper())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def remove_from_watchlist(db: Session, user_id: int | None, ticker: str) -> bool:
    item = db.query(WatchlistItem).filter(
        WatchlistItem.user_id == user_id,
        WatchlistItem.ticker == ticker.upper(),
    ).first()
    if not item:
        return False
    db.delete(item)
    db.commit()
    return True


# ── Trade CRUD ────────────────────────────────────────────────────────

def create_trade(db: Session, user_id: int | None, **kwargs) -> Trade:
    trade = Trade(user_id=user_id, **kwargs)
    if trade.entry_date is None:
        trade.entry_date = datetime.utcnow()
    if not trade.indicator_snapshot and trade.ticker:
        try:
            from .scanner import _score_ticker
            result = _score_ticker(trade.ticker)
            if result and result.get("stop_loss") and result.get("take_profit"):
                trade.indicator_snapshot = json.dumps({
                    "stop_loss": result["stop_loss"],
                    "take_profit": result["take_profit"],
                    "score": result.get("score"),
                    "signal": result.get("signal"),
                })
        except Exception:
            pass
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


def close_trade(
    db: Session, trade_id: int, user_id: int | None,
    exit_price: float, exit_date: datetime | None = None, notes: str | None = None,
) -> Trade | None:
    trade = db.query(Trade).filter(
        Trade.id == trade_id, Trade.user_id == user_id,
    ).first()
    if not trade or trade.status != "open":
        return None

    trade.exit_price = exit_price
    trade.exit_date = exit_date or datetime.utcnow()
    trade.status = "closed"
    trade.pnl = _calc_pnl(trade)
    if notes:
        trade.notes = (trade.notes or "") + f"\n{notes}"

    try:
        snap = get_indicator_snapshot(trade.ticker)
        trade.indicator_snapshot = json.dumps(snap)
    except Exception:
        pass

    db.commit()
    db.refresh(trade)
    return trade


def get_trades(
    db: Session, user_id: int | None,
    status: str | None = None, limit: int = 50,
) -> list[Trade]:
    q = db.query(Trade).filter(Trade.user_id == user_id)
    if status:
        q = q.filter(Trade.status == status)
    return q.order_by(Trade.entry_date.desc()).limit(limit).all()


def _calc_pnl(trade: Trade) -> float:
    if trade.exit_price is None:
        return 0.0
    diff = trade.exit_price - trade.entry_price
    if trade.direction == "short":
        diff = -diff
    return round(diff * trade.quantity, 2)


# ── P&L Analytics ─────────────────────────────────────────────────────

def get_trade_stats(db: Session, user_id: int | None) -> dict[str, Any]:
    """Aggregate performance stats from closed trades."""
    closed = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
    ).all()

    if not closed:
        return {"total_trades": 0}

    pnls = [t.pnl or 0.0 for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    total_pnl = sum(pnls)
    cumulative = []
    running = 0.0
    for p in pnls:
        running += p
        cumulative.append(round(running, 2))

    max_dd = 0.0
    peak = 0.0
    for c in cumulative:
        if c > peak:
            peak = c
        dd = peak - c
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(closed), 2),
        "best_trade": round(max(pnls), 2) if pnls else 0,
        "worst_trade": round(min(pnls), 2) if pnls else 0,
        "max_drawdown": round(max_dd, 2),
        "equity_curve": cumulative,
    }


def get_trade_stats_by_pattern(
    db: Session, user_id: int | None, min_trades: int = 1,
) -> list[dict[str, Any]]:
    """Per-pattern performance stats from closed trades that have pattern_tags."""
    closed = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.pattern_tags.isnot(None),
    ).all()

    tag_trades: dict[str, list[float]] = {}
    for t in closed:
        pnl = t.pnl or 0.0
        for tag in (t.pattern_tags or "").split(","):
            tag = tag.strip()
            if tag:
                tag_trades.setdefault(tag, []).append(pnl)

    results = []
    for pattern, pnls in tag_trades.items():
        if len(pnls) < min_trades:
            continue
        wins = sum(1 for p in pnls if p > 0)
        results.append({
            "pattern": pattern,
            "trades": len(pnls),
            "wins": wins,
            "losses": len(pnls) - wins,
            "win_rate": round(wins / len(pnls) * 100, 1),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "total_pnl": round(sum(pnls), 2),
        })

    results.sort(key=lambda x: x["trades"], reverse=True)
    return results


# ── AI Insights CRUD ──────────────────────────────────────────────────

def get_insights(db: Session, user_id: int | None, limit: int = 20) -> list[TradingInsight]:
    return db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
    ).order_by(TradingInsight.confidence.desc()).limit(limit).all()


def _pattern_label(desc: str) -> str:
    """Extract the stable label prefix before dynamic stats (e.g. ' -> avg')."""
    idx = desc.find(" -> ")
    return (desc[:idx] if idx != -1 else desc).strip()


def _pattern_keywords(desc: str) -> set[str]:
    """Extract meaningful keywords from a pattern description for dedup matching."""
    import re
    stop = {"the", "and", "for", "with", "avg", "win", "samples", "signal", "->"}
    words = set(re.findall(r"[a-z_]+(?:\d+)?", desc.lower()))
    return words - stop


def save_insight(
    db: Session, user_id: int | None,
    pattern: str, confidence: float = 0.5,
) -> TradingInsight:
    from .learning import log_learning_event
    from datetime import datetime

    new_label = _pattern_label(pattern)
    new_kw = _pattern_keywords(new_label)
    existing = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
    ).all()
    for ins in existing:
        old_label = _pattern_label(ins.pattern_description)
        old_kw = _pattern_keywords(old_label)
        if not old_kw or not new_kw:
            continue
        overlap = len(new_kw & old_kw) / max(1, len(new_kw | old_kw))
        if overlap >= 0.5:
            old_conf = ins.confidence
            ins.confidence = round(min(0.95, ins.confidence * 0.7 + confidence * 0.3), 3)
            ins.evidence_count += 1
            ins.last_seen = datetime.utcnow()
            ins.pattern_description = pattern
            db.commit()
            if abs(ins.confidence - old_conf) > 0.005:
                log_learning_event(
                    db, user_id, "update",
                    f"Pattern reinforced ({old_conf:.0%}->{ins.confidence:.0%}): {ins.pattern_description[:100]}",
                    confidence_before=old_conf,
                    confidence_after=ins.confidence,
                    related_insight_id=ins.id,
                )
            return ins

    insight = TradingInsight(
        user_id=user_id,
        pattern_description=pattern,
        confidence=confidence,
    )
    db.add(insight)
    db.commit()
    db.refresh(insight)
    log_learning_event(
        db, user_id, "discovery",
        f"New pattern: {pattern[:120]}",
        confidence_after=confidence,
        related_insight_id=insight.id,
    )
    return insight


# ── Portfolio Summary ─────────────────────────────────────────────────

def get_portfolio_summary(db: Session, user_id: int | None) -> dict[str, Any]:
    """Full portfolio overview: open positions, equity curve, benchmark."""
    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "open",
    ).all()
    closed_trades = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
    ).order_by(Trade.exit_date.asc()).all()

    positions = []
    total_invested = 0.0
    total_current = 0.0
    allocation: dict[str, float] = {}

    for t in open_trades:
        quote = fetch_quote(t.ticker)
        current_price = quote.get("price", t.entry_price) if quote else t.entry_price
        unrealized = (current_price - t.entry_price) * t.quantity
        if t.direction == "short":
            unrealized = -unrealized
        position_value = current_price * t.quantity

        positions.append({
            "id": t.id, "ticker": t.ticker, "direction": t.direction,
            "entry_price": t.entry_price, "current_price": current_price,
            "quantity": t.quantity, "unrealized_pnl": round(unrealized, 2),
            "unrealized_pct": round(unrealized / (t.entry_price * t.quantity) * 100, 2) if t.entry_price > 0 else 0,
        })
        total_invested += t.entry_price * t.quantity
        total_current += position_value
        allocation[t.ticker] = allocation.get(t.ticker, 0) + position_value

    realized_pnl = 0.0
    equity_curve = []
    for t in closed_trades:
        realized_pnl += t.pnl or 0
        if t.exit_date:
            equity_curve.append({
                "time": int(t.exit_date.timestamp()),
                "value": round(realized_pnl, 2),
            })

    total_unrealized = round(total_current - total_invested, 2) if total_invested > 0 else 0

    alloc_pct = {}
    if total_current > 0:
        for ticker, val in allocation.items():
            alloc_pct[ticker] = round(val / total_current * 100, 1)

    stats = get_trade_stats(db, user_id)

    return {
        "positions": positions,
        "position_count": len(positions),
        "total_invested": round(total_invested, 2),
        "total_current": round(total_current, 2),
        "unrealized_pnl": total_unrealized,
        "realized_pnl": round(realized_pnl, 2),
        "total_pnl": round(realized_pnl + total_unrealized, 2),
        "allocation": alloc_pct,
        "equity_curve": equity_curve,
        "stats": stats,
    }
