"""Journal: trade journaling, auto-journal, daily/weekly reviews, signal events."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import JournalEntry, Trade
from .market_data import (
    fetch_quote, get_indicator_snapshot, ticker_display_name,
    DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS,
)

logger = logging.getLogger(__name__)


def add_journal_entry(
    db: Session, user_id: int | None,
    content: str, trade_id: int | None = None,
    indicator_snapshot: dict | None = None,
) -> JournalEntry:
    entry = JournalEntry(
        user_id=user_id,
        trade_id=trade_id,
        content=content,
        indicator_snapshot=json.dumps(indicator_snapshot) if indicator_snapshot else None,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def get_journal(db: Session, user_id: int | None, limit: int = 50) -> list[JournalEntry]:
    return db.query(JournalEntry).filter(
        JournalEntry.user_id == user_id,
    ).order_by(JournalEntry.created_at.desc()).limit(limit).all()


def auto_journal_trade_open(db: Session, trade: Trade) -> None:
    """AI auto-journals why a trade was opened."""
    from .learning import log_learning_event
    try:
        snap = get_indicator_snapshot(trade.ticker)
        snap_text = json.dumps(snap, indent=2) if snap else "N/A"
    except Exception:
        snap_text = "N/A"

    content = (
        f"[AI] TRADE OPENED: {trade.direction.upper()} {trade.quantity}x {trade.ticker} @ ${trade.entry_price}\n"
        f"Indicators at entry: {snap_text[:400]}"
    )
    add_journal_entry(db, trade.user_id, content=content, trade_id=trade.id)
    log_learning_event(db, trade.user_id, "journal", f"Auto-journaled trade open: {trade.ticker} {trade.direction}")


def daily_market_journal(db: Session, user_id: int | None) -> str | None:
    """Generate a daily market observation journal entry."""
    from .learning import log_learning_event
    from .scanner import _score_ticker

    movers: list[dict] = []
    for ticker in DEFAULT_SCAN_TICKERS[:15] + DEFAULT_CRYPTO_TICKERS[:5]:
        q = fetch_quote(ticker)
        if q and q.get("change_pct") is not None:
            movers.append({"ticker": ticker, "pct": q["change_pct"], "price": q.get("price")})

    movers.sort(key=lambda m: abs(m["pct"]), reverse=True)
    top_movers = movers[:5]

    if not top_movers:
        return None

    lines = [f"[AI] DAILY MARKET OBSERVATION — {datetime.utcnow().strftime('%Y-%m-%d')}"]
    for m in top_movers:
        direction = "UP" if m["pct"] >= 0 else "DOWN"
        lines.append(f"  {ticker_display_name(m['ticker'])}: ${m['price']} ({direction} {m['pct']:+.1f}%)")

    bull = bear = 0
    for ticker in DEFAULT_SCAN_TICKERS[:10]:
        scored = _score_ticker(ticker)
        if scored:
            if scored["signal"] == "buy":
                bull += 1
            elif scored["signal"] == "sell":
                bear += 1

    lines.append(f"  Market mood: {bull} bullish, {bear} bearish out of 10 sampled")

    content = "\n".join(lines)
    add_journal_entry(db, user_id, content=content)
    log_learning_event(db, user_id, "journal", "Daily market observation recorded")
    return content


def check_signal_events(db: Session, user_id: int | None) -> list[str]:
    """Scan watchlist for significant indicator events and auto-journal them."""
    from .learning import log_learning_event
    from .scanner import _score_ticker
    from .portfolio import get_watchlist

    watchlist = get_watchlist(db, user_id)
    if not watchlist:
        return []

    events: list[str] = []
    for w in watchlist:
        scored = _score_ticker(w.ticker)
        if not scored:
            continue

        rsi = scored["indicators"].get("rsi")
        signals = scored.get("signals", [])

        notable: list[str] = []
        if rsi is not None and rsi < 30:
            notable.append(f"RSI oversold ({rsi:.0f})")
        elif rsi is not None and rsi > 70:
            notable.append(f"RSI overbought ({rsi:.0f})")

        for s in signals:
            if "macd" in s.lower() and "bullish" in s.lower():
                notable.append("MACD bullish crossover")
            if "volume surge" in s.lower():
                notable.append("Volume surge detected")
            if "bollinger" in s.lower():
                notable.append("Near Bollinger Band extreme")

        if notable:
            event_text = f"[AI] SIGNAL ALERT — {ticker_display_name(w.ticker)}: {', '.join(notable)}"
            add_journal_entry(db, user_id, content=event_text)
            events.append(event_text)
            log_learning_event(db, user_id, "journal", f"Signal event: {w.ticker} — {', '.join(notable)}")

    return events


def weekly_performance_review(db: Session, user_id: int | None) -> str | None:
    """AI writes a weekly performance summary."""
    from .learning import log_learning_event
    from .portfolio import get_trade_stats, get_insights

    stats = get_trade_stats(db, user_id)
    if not stats.get("total_trades"):
        return None

    insights = get_insights(db, user_id, limit=5)
    insight_text = "; ".join(i.pattern_description for i in insights) if insights else "None yet"

    content = (
        f"[AI] WEEKLY REVIEW — {datetime.utcnow().strftime('%Y-%m-%d')}\n"
        f"Total trades: {stats['total_trades']} | Win rate: {stats['win_rate']}%\n"
        f"Total P&L: ${stats['total_pnl']} | Best: ${stats['best_trade']} | Worst: ${stats['worst_trade']}\n"
        f"Active patterns: {len(insights)} | Top insight: {insight_text[:200]}\n"
        f"Max drawdown: ${stats['max_drawdown']}"
    )
    add_journal_entry(db, user_id, content=content)
    log_learning_event(db, user_id, "review", "Weekly performance review generated")
    return content
