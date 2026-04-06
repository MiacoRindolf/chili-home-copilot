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
    reference_exit_price: float | None = None,
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
        from .tca_service import apply_tca_on_trade_close, resolve_exit_reference_price

        trade.tca_reference_exit_price = resolve_exit_reference_price(
            trade.ticker,
            explicit=reference_exit_price,
            fill_fallback=float(exit_price),
        )
        apply_tca_on_trade_close(trade)
    except Exception:
        pass

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


def delete_trade(db: Session, trade_id: int, user_id: int | None) -> str | None:
    """Delete a trade and clear journal references. Returns None on success, or error code: 'not_found' / 'forbidden'."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        return "not_found"
    if trade.user_id != user_id:
        return "forbidden"
    db.query(JournalEntry).filter(JournalEntry.trade_id == trade_id).update(
        {JournalEntry.trade_id: None}
    )
    db.delete(trade)
    db.commit()
    return None


def _calc_pnl(trade: Trade) -> float:
    if trade.exit_price is None:
        return 0.0
    diff = trade.exit_price - trade.entry_price
    if trade.direction == "short":
        diff = -diff
    return round(diff * trade.quantity, 2)


# ── P&L Analytics ─────────────────────────────────────────────────────

def _compute_stats_from_trades(closed: list[Trade]) -> dict[str, Any]:
    """Compute aggregate stats from a list of closed trades."""
    if not closed:
        return {"total_trades": 0}
    pnls = [t.pnl or 0.0 for t in closed]
    wins = [p for p in pnls if p > 0]
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
        "losses": len(pnls) - len(wins),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(closed), 2) if closed else 0,
        "best_trade": round(max(pnls), 2) if pnls else 0,
        "worst_trade": round(min(pnls), 2) if pnls else 0,
        "max_drawdown": round(max_dd, 2),
        "equity_curve": cumulative,
    }


def get_trade_stats(db: Session, user_id: int | None) -> dict[str, Any]:
    """Aggregate performance stats from closed trades."""
    closed = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
    ).all()
    return _compute_stats_from_trades(closed)


def get_trade_stats_by_source(db: Session, user_id: int | None) -> dict[str, Any]:
    """Stats split by broker source: all, real (Robinhood), paper (manual/null)."""
    closed = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
    ).all()
    real = [t for t in closed if t.broker_source == "robinhood"]
    paper = [t for t in closed if t.broker_source != "robinhood"]
    return {
        "all": _compute_stats_from_trades(closed),
        "real": _compute_stats_from_trades(real),
        "paper": _compute_stats_from_trades(paper),
    }


def get_daily_pnl(
    db: Session, user_id: int | None,
    start_date: datetime, end_date: datetime,
    *,
    include_day_trades: bool = True,
) -> list[dict[str, Any]]:
    """Daily P&L and trade count for calendar. Groups by exit_date (UTC date).

    *include_day_trades*: when False, omit per-day ``trades`` lists (smaller JSON for Brain
    performance widget, which only charts date + pnl).
    """
    from collections import defaultdict

    closed = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.exit_date.isnot(None),
        Trade.exit_date >= start_date,
        Trade.exit_date < end_date,
    ).order_by(Trade.exit_date.asc()).all()

    by_date: dict[str, list[Trade]] = defaultdict(list)
    for t in closed:
        day = t.exit_date.date().isoformat() if t.exit_date else None
        if day:
            by_date[day].append(t)

    result = []
    for day in sorted(by_date.keys()):
        trades = by_date[day]
        pnl = sum(t.pnl or 0.0 for t in trades)
        row: dict[str, Any] = {
            "date": day,
            "trade_count": len(trades),
            "pnl": round(pnl, 2),
        }
        if include_day_trades:
            row["trades"] = [
                {"id": t.id, "ticker": t.ticker, "direction": t.direction, "pnl": round(t.pnl or 0, 2)}
                for t in trades
            ]
        result.append(row)
    return result


def get_performance_dashboard(
    db: Session, user_id: int | None,
) -> dict[str, Any]:
    """Comprehensive performance dashboard data for the Brain UI."""
    from ...models.trading import ScanPattern
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    stats = get_trade_stats(db, user_id)
    by_source = get_trade_stats_by_source(db, user_id)

    # 30-day daily P&L (omit per-day trade rows — Brain UI only needs date + pnl for the sparkline)
    daily = get_daily_pnl(
        db, user_id, now - timedelta(days=30), now, include_day_trades=False
    )

    # Per-pattern attribution via scan_pattern_id
    closed_with_sp = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.scan_pattern_id.isnot(None),
    ).all()
    sp_pnl: dict[int, list[float]] = {}
    for t in closed_with_sp:
        sp_pnl.setdefault(t.scan_pattern_id, []).append(t.pnl or 0)
    sp_ids = list(sp_pnl.keys())
    sp_names = {}
    if sp_ids:
        for sp in db.query(ScanPattern).filter(ScanPattern.id.in_(sp_ids)).all():
            sp_names[sp.id] = sp.name
    attribution = sorted([
        {
            "pattern_id": sp_id,
            "pattern_name": sp_names.get(sp_id, f"Pattern #{sp_id}"),
            "trades": len(pnls),
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0,
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1) if pnls else 0,
        }
        for sp_id, pnls in sp_pnl.items()
    ], key=lambda x: x["total_pnl"], reverse=True)

    # Period comparisons
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    recent_week = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
        Trade.exit_date >= week_ago,
    ).all()
    recent_month = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
        Trade.exit_date >= month_ago,
    ).all()

    return {
        "ok": True,
        "overall": stats,
        "by_source": by_source,
        "daily_pnl": daily,
        "attribution": attribution[:20],
        "week": {
            "trades": len(recent_week),
            "pnl": round(sum(t.pnl or 0 for t in recent_week), 2),
            "win_rate": round(sum(1 for t in recent_week if (t.pnl or 0) > 0) / max(1, len(recent_week)) * 100, 1),
        },
        "month": {
            "trades": len(recent_month),
            "pnl": round(sum(t.pnl or 0 for t in recent_month), 2),
            "win_rate": round(sum(1 for t in recent_month if (t.pnl or 0) > 0) / max(1, len(recent_month)) * 100, 1),
        },
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


def _insight_text_with_family(description: str, family: str | None) -> str:
    if not family:
        return description
    prefix = f"[family:{family}] "
    s = (description or "").strip()
    if s.startswith("[family:"):
        return description
    return prefix + description


def preload_active_insights(db: Session, user_id: int | None) -> list["TradingInsight"]:
    """Pre-fetch active insights once so callers can reuse across multiple save_insight calls."""
    return db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
    ).all()


def save_insight(
    db: Session, user_id: int | None,
    pattern: str, confidence: float = 0.5,
    wins: int = 0, losses: int = 0,
    scan_pattern_id: int | None = None,
    hypothesis_family: str | None = None,
    _existing_cache: list["TradingInsight"] | None = None,
) -> TradingInsight:
    from .learning import log_learning_event
    from .pattern_resolution import get_legacy_unlinked_scan_pattern_id
    from datetime import datetime

    explicit_scan_pattern_id = scan_pattern_id
    if scan_pattern_id is None:
        scan_pattern_id = get_legacy_unlinked_scan_pattern_id(db)

    pattern = _insight_text_with_family(pattern, hypothesis_family)

    new_label = _pattern_label(pattern)
    new_kw = _pattern_keywords(new_label)
    existing = _existing_cache if _existing_cache is not None else preload_active_insights(db, user_id)
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
            ins.win_count = (ins.win_count or 0) + wins
            ins.loss_count = (ins.loss_count or 0) + losses
            ins.last_seen = datetime.utcnow()
            ins.pattern_description = pattern
            if hypothesis_family:
                ins.hypothesis_family = hypothesis_family
            if explicit_scan_pattern_id is not None:
                ins.scan_pattern_id = explicit_scan_pattern_id
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
        scan_pattern_id=scan_pattern_id,
        pattern_description=pattern,
        hypothesis_family=hypothesis_family,
        confidence=confidence,
        win_count=wins,
        loss_count=losses,
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
