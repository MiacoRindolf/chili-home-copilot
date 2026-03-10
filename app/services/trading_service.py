"""Trading module business logic: market data, indicators, journal, analytics."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import pandas as pd
import yfinance as yf
from sqlalchemy.orm import Session

from ..models.trading import JournalEntry, Trade, TradingInsight, WatchlistItem


# ── Market data (yfinance) ──────────────────────────────────────────────

_VALID_INTERVALS = {
    "1m", "2m", "5m", "15m", "30m", "60m", "90m",
    "1h", "1d", "5d", "1wk", "1mo", "3mo",
}
_VALID_PERIODS = {
    "1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max",
}


def fetch_ohlcv(
    ticker: str,
    interval: str = "1d",
    period: str = "6mo",
) -> list[dict[str, Any]]:
    """Fetch OHLCV candle data from Yahoo Finance."""
    if interval not in _VALID_INTERVALS:
        interval = "1d"
    if period not in _VALID_PERIODS:
        period = "6mo"

    t = yf.Ticker(ticker)
    df = t.history(period=period, interval=interval)
    if df.empty:
        return []

    records: list[dict[str, Any]] = []
    for ts, row in df.iterrows():
        epoch = int(pd.Timestamp(ts).timestamp())
        records.append({
            "time": epoch,
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
            "volume": int(row["Volume"]),
        })
    return records


def fetch_quote(ticker: str) -> dict[str, Any] | None:
    """Current price + basic info for a ticker."""
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        return {
            "ticker": ticker.upper(),
            "price": round(float(info.last_price), 2) if info.last_price else None,
            "previous_close": round(float(info.previous_close), 2) if info.previous_close else None,
            "change": round(float(info.last_price - info.previous_close), 2)
                if info.last_price and info.previous_close else None,
            "change_pct": round(
                float((info.last_price - info.previous_close) / info.previous_close * 100), 2
            ) if info.last_price and info.previous_close else None,
            "market_cap": int(info.market_cap) if info.market_cap else None,
            "currency": info.currency if hasattr(info, "currency") else "USD",
        }
    except Exception:
        return None


def search_tickers(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Search for tickers matching a query string."""
    try:
        results = yf.search(query, max_results=limit)
        quotes = results.get("quotes", []) if isinstance(results, dict) else []
        return [
            {
                "ticker": q.get("symbol", ""),
                "name": q.get("shortname") or q.get("longname", ""),
                "exchange": q.get("exchange", ""),
                "type": q.get("quoteType", ""),
            }
            for q in quotes
            if q.get("symbol")
        ]
    except Exception:
        return []


# ── Technical indicators (pandas-ta) ────────────────────────────────────

def compute_indicators(
    ticker: str,
    interval: str = "1d",
    period: str = "6mo",
    indicators: list[str] | None = None,
) -> dict[str, Any]:
    """Compute requested technical indicators for a ticker.

    Uses the ``ta`` library (technical-analysis).  Returns a dict keyed by
    indicator name, each value a list of {time, value} or multi-key dicts.
    """
    if indicators is None:
        indicators = ["rsi", "macd", "sma_20", "ema_20", "bbands"]

    t = yf.Ticker(ticker)
    df = t.history(period=period, interval=interval)
    if df.empty:
        return {}

    df.index = pd.to_datetime(df.index)
    timestamps = [int(pd.Timestamp(ts).timestamp()) for ts in df.index]
    result: dict[str, Any] = {}

    for ind in indicators:
        ind_lower = ind.lower().strip()
        try:
            data = _compute_single_indicator(df, timestamps, ind_lower)
            if data is not None:
                result[ind_lower] = data
        except Exception:
            continue

    return result


def _compute_single_indicator(
    df: pd.DataFrame, timestamps: list[int], name: str,
) -> list[dict] | None:
    """Compute one indicator using the ``ta`` library."""
    from ta.momentum import RSIIndicator, StochRSIIndicator, StochasticOscillator, WilliamsRIndicator
    from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator, PSARIndicator, CCIIndicator
    from ta.volatility import BollingerBands, AverageTrueRange
    from ta.volume import OnBalanceVolumeIndicator, MFIIndicator, VolumeWeightedAveragePrice

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    # ── RSI ──
    if name == "rsi" or name.startswith("rsi_"):
        period = int(name.split("_")[1]) if "_" in name else 14
        s = RSIIndicator(close=close, window=period).rsi()
        return _series_to_records(timestamps, s, "value")

    # ── MACD ──
    if name == "macd":
        m = MACD(close=close)
        macd_line = m.macd()
        signal_line = m.macd_signal()
        histogram = m.macd_diff()
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(macd_line.iloc[i]):
                rec["macd"] = round(float(macd_line.iloc[i]), 4)
                has = True
            if pd.notna(signal_line.iloc[i]):
                rec["signal"] = round(float(signal_line.iloc[i]), 4)
                has = True
            if pd.notna(histogram.iloc[i]):
                rec["histogram"] = round(float(histogram.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    # ── SMA ──
    if name.startswith("sma"):
        period = int(name.split("_")[1]) if "_" in name else 20
        s = SMAIndicator(close=close, window=period).sma_indicator()
        return _series_to_records(timestamps, s, "value")

    # ── EMA ──
    if name.startswith("ema"):
        period = int(name.split("_")[1]) if "_" in name else 20
        s = EMAIndicator(close=close, window=period).ema_indicator()
        return _series_to_records(timestamps, s, "value")

    # ── Bollinger Bands ──
    if name in ("bbands", "bb", "bollinger"):
        bb = BollingerBands(close=close, window=20, window_dev=2)
        upper = bb.bollinger_hband()
        middle = bb.bollinger_mavg()
        lower = bb.bollinger_lband()
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(upper.iloc[i]):
                rec["upper"] = round(float(upper.iloc[i]), 4)
                has = True
            if pd.notna(middle.iloc[i]):
                rec["middle"] = round(float(middle.iloc[i]), 4)
                has = True
            if pd.notna(lower.iloc[i]):
                rec["lower"] = round(float(lower.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    # ── Stochastic ──
    if name in ("stoch", "stochastic"):
        st = StochasticOscillator(high=high, low=low, close=close)
        k = st.stoch()
        d = st.stoch_signal()
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(k.iloc[i]):
                rec["k"] = round(float(k.iloc[i]), 4)
                has = True
            if pd.notna(d.iloc[i]):
                rec["d"] = round(float(d.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    # ── ADX ──
    if name == "adx":
        a = ADXIndicator(high=high, low=low, close=close)
        adx_val = a.adx()
        dmp = a.adx_pos()
        dmn = a.adx_neg()
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(adx_val.iloc[i]):
                rec["adx"] = round(float(adx_val.iloc[i]), 4)
                has = True
            if pd.notna(dmp.iloc[i]):
                rec["dmp"] = round(float(dmp.iloc[i]), 4)
                has = True
            if pd.notna(dmn.iloc[i]):
                rec["dmn"] = round(float(dmn.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    # ── ATR ──
    if name == "atr":
        s = AverageTrueRange(high=high, low=low, close=close).average_true_range()
        return _series_to_records(timestamps, s, "value")

    # ── CCI ──
    if name == "cci":
        s = CCIIndicator(high=high, low=low, close=close).cci()
        return _series_to_records(timestamps, s, "value")

    # ── Williams %R ──
    if name in ("willr", "williams"):
        s = WilliamsRIndicator(high=high, low=low, close=close).williams_r()
        return _series_to_records(timestamps, s, "value")

    # ── OBV ──
    if name == "obv":
        s = OnBalanceVolumeIndicator(close=close, volume=volume).on_balance_volume()
        return _series_to_records(timestamps, s, "value")

    # ── MFI ──
    if name == "mfi":
        s = MFIIndicator(high=high, low=low, close=close, volume=volume).money_flow_index()
        return _series_to_records(timestamps, s, "value")

    # ── VWAP ──
    if name == "vwap":
        s = VolumeWeightedAveragePrice(high=high, low=low, close=close, volume=volume).volume_weighted_average_price()
        return _series_to_records(timestamps, s, "value")

    # ── Parabolic SAR ──
    if name in ("psar", "sar"):
        p = PSARIndicator(high=high, low=low, close=close)
        psar_up = p.psar_up()
        psar_down = p.psar_down()
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(psar_up.iloc[i]):
                rec["long"] = round(float(psar_up.iloc[i]), 4)
                has = True
            if pd.notna(psar_down.iloc[i]):
                rec["short"] = round(float(psar_down.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    return None


def _series_to_records(timestamps: list[int], s: pd.Series, key: str) -> list[dict]:
    out = []
    for ts, val in zip(timestamps, s):
        if pd.notna(val):
            out.append({"time": ts, key: round(float(val), 4)})
    return out


def get_indicator_snapshot(ticker: str, interval: str = "1d") -> dict[str, Any]:
    """Get latest indicator values (used for journal snapshots and AI context)."""
    result = compute_indicators(
        ticker, interval=interval, period="3mo",
        indicators=["rsi", "macd", "sma_20", "ema_20", "bbands", "adx", "atr", "obv"],
    )
    snapshot: dict[str, Any] = {"ticker": ticker, "interval": interval}
    for ind_name, records in result.items():
        if records:
            latest = records[-1]
            snapshot[ind_name] = {k: v for k, v in latest.items() if k != "time"}
    return snapshot


# ── Watchlist CRUD ──────────────────────────────────────────────────────

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


# ── Trade CRUD ──────────────────────────────────────────────────────────

def create_trade(db: Session, user_id: int | None, **kwargs) -> Trade:
    trade = Trade(user_id=user_id, **kwargs)
    if trade.entry_date is None:
        trade.entry_date = datetime.utcnow()
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

    # Snapshot indicators at exit for AI learning.
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


# ── Journal entries ─────────────────────────────────────────────────────

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


# ── P&L Analytics ───────────────────────────────────────────────────────

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


# ── AI Insights CRUD ───────────────────────────────────────────────────

def get_insights(db: Session, user_id: int | None, limit: int = 20) -> list[TradingInsight]:
    return db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
    ).order_by(TradingInsight.confidence.desc()).limit(limit).all()


def save_insight(
    db: Session, user_id: int | None,
    pattern: str, confidence: float = 0.5,
) -> TradingInsight:
    insight = TradingInsight(
        user_id=user_id,
        pattern_description=pattern,
        confidence=confidence,
    )
    db.add(insight)
    db.commit()
    db.refresh(insight)
    return insight


def build_ai_context(
    db: Session, user_id: int | None, ticker: str, interval: str = "1d",
) -> str:
    """Assemble context for the trading AI: current indicators + journal + insights."""
    parts: list[str] = []

    # Current indicator snapshot
    try:
        snap = get_indicator_snapshot(ticker, interval)
        parts.append(f"## Current Indicators for {ticker} ({interval})\n{json.dumps(snap, indent=2)}")
    except Exception:
        parts.append(f"## Could not fetch indicators for {ticker}")

    # Current price
    quote = fetch_quote(ticker)
    if quote:
        parts.append(f"## Current Quote\nPrice: ${quote.get('price')}, Change: {quote.get('change_pct')}%")

    # Recent trades for this ticker
    trades = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.ticker == ticker.upper(),
    ).order_by(Trade.entry_date.desc()).limit(5).all()
    if trades:
        lines = ["## Recent Trades"]
        for tr in trades:
            lines.append(
                f"- {tr.direction.upper()} {tr.quantity}x @ ${tr.entry_price} "
                f"{'→ $' + str(tr.exit_price) if tr.exit_price else '(open)'} "
                f"P&L: ${tr.pnl or 'n/a'}"
            )
        parts.append("\n".join(lines))

    # Trade stats
    stats = get_trade_stats(db, user_id)
    if stats.get("total_trades", 0) > 0:
        parts.append(
            f"## Performance\n"
            f"Trades: {stats['total_trades']}, Win rate: {stats['win_rate']}%, "
            f"Total P&L: ${stats['total_pnl']}, Max DD: ${stats['max_drawdown']}"
        )

    # AI insights
    insights = get_insights(db, user_id, limit=5)
    if insights:
        lines = ["## Learned Patterns"]
        for ins in insights:
            lines.append(f"- [{ins.confidence:.0%}] {ins.pattern_description}")
        parts.append("\n".join(lines))

    # Recent journal
    journal = get_journal(db, user_id, limit=3)
    if journal:
        lines = ["## Recent Journal"]
        for j in journal:
            lines.append(f"- {j.created_at.strftime('%Y-%m-%d')}: {j.content[:200]}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


# ── AI Self-Learning Loop ──────────────────────────────────────────────

def analyze_closed_trade(db: Session, trade: Trade) -> str | None:
    """Called after a trade is closed.  Asks the AI to review the trade and
    extract reusable patterns, then stores any insights it discovers."""
    from ..prompts import load_prompt
    from .. import openai_client
    from ..logger import log_info, new_trace_id

    trace_id = new_trace_id()

    snap_data = ""
    if trade.indicator_snapshot:
        try:
            snap_data = json.dumps(json.loads(trade.indicator_snapshot), indent=2)
        except Exception:
            snap_data = trade.indicator_snapshot

    pnl_label = "PROFIT" if (trade.pnl or 0) > 0 else "LOSS"
    trade_summary = (
        f"Ticker: {trade.ticker}\n"
        f"Direction: {trade.direction}\n"
        f"Entry: ${trade.entry_price} on {trade.entry_date}\n"
        f"Exit: ${trade.exit_price} on {trade.exit_date}\n"
        f"P&L: ${trade.pnl} ({pnl_label})\n"
        f"Indicator snapshot at exit:\n{snap_data}"
    )

    existing_insights = get_insights(db, trade.user_id, limit=10)
    insight_text = ""
    if existing_insights:
        insight_text = "\n".join(
            f"- [{ins.confidence:.0%}] {ins.pattern_description}"
            for ins in existing_insights
        )

    user_msg = (
        f"A trade was just closed. Analyze it and extract trading patterns.\n\n"
        f"## Trade Details\n{trade_summary}\n\n"
        f"## Existing Learned Patterns\n{insight_text or 'None yet.'}\n\n"
        f"Instructions:\n"
        f"1. Explain why this trade was a {pnl_label} based on the indicator state.\n"
        f"2. Extract 1-3 reusable patterns as JSON array:\n"
        f'   [{{"pattern": "description", "confidence": 0.0-1.0}}]\n'
        f"3. If an existing pattern is confirmed, note its description so we can boost its confidence.\n"
        f"4. Put the JSON array on a line starting with PATTERNS:"
    )

    try:
        system_prompt = load_prompt("trading_analyst")
        result = openai_client.chat(
            messages=[{"role": "user", "content": user_msg}],
            system_prompt=system_prompt,
            trace_id=trace_id,
            user_message=user_msg,
        )
        reply = result.get("reply", "")
    except Exception as e:
        log_info(trace_id, f"[trading] post-trade analysis error: {e}")
        return None

    # Parse patterns from the reply.
    _extract_and_store_patterns(db, trade.user_id, reply, existing_insights)

    # Auto-journal the AI analysis.
    add_journal_entry(
        db, trade.user_id,
        content=f"[AI] Trade #{trade.id} ({trade.ticker} {pnl_label} ${trade.pnl}): {reply[:500]}",
        trade_id=trade.id,
    )

    return reply


def _extract_and_store_patterns(
    db: Session, user_id: int | None,
    ai_reply: str, existing_insights: list[TradingInsight],
) -> None:
    """Parse PATTERNS: JSON from the AI reply and upsert insights."""
    import re

    match = re.search(r"PATTERNS:\s*(\[.*?\])", ai_reply, re.DOTALL)
    if not match:
        return

    try:
        patterns = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return

    if not isinstance(patterns, list):
        return

    existing_map = {
        ins.pattern_description.lower().strip(): ins
        for ins in existing_insights
    }

    for p in patterns:
        if not isinstance(p, dict):
            continue
        desc = str(p.get("pattern", "")).strip()
        conf = float(p.get("confidence", 0.5))
        if not desc or len(desc) < 10:
            continue

        # Check if this pattern matches an existing one (fuzzy by substring).
        matched_existing = None
        desc_lower = desc.lower()
        for key, ins in existing_map.items():
            if key in desc_lower or desc_lower in key:
                matched_existing = ins
                break

        if matched_existing:
            matched_existing.evidence_count += 1
            matched_existing.confidence = min(
                0.95,
                matched_existing.confidence + 0.05,
            )
            matched_existing.last_seen = datetime.utcnow()
            db.commit()
        else:
            save_insight(db, user_id, desc, confidence=max(0.1, min(0.9, conf)))


# ── Stock Scanner ──────────────────────────────────────────────────────

DEFAULT_SCAN_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
    "JPM", "V", "UNH", "MA", "HD", "PG", "JNJ", "COST", "ABBV", "CRM",
    "MRK", "PEP", "AVGO", "KO", "TMO", "WMT", "CSCO", "ACN", "MCD",
    "ABT", "LIN", "DHR", "TXN", "NEE", "AMD", "PM", "INTC", "QCOM",
    "NFLX", "DIS", "AMGN", "HON", "LOW", "UPS", "CAT", "BA", "GS",
    "SBUX", "PYPL", "SQ", "SNAP", "PLTR",
]


def _score_ticker(ticker: str) -> dict[str, Any] | None:
    """Score a single ticker using multi-signal confluence (1-10)."""
    try:
        from ta.momentum import RSIIndicator, StochasticOscillator
        from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator
        from ta.volatility import BollingerBands, AverageTrueRange

        t = yf.Ticker(ticker)
        df = t.history(period="3mo", interval="1d")
        if df.empty or len(df) < 30:
            return None

        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]

        rsi_val = RSIIndicator(close=close, window=14).rsi().iloc[-1]
        macd_obj = MACD(close=close)
        macd_val = macd_obj.macd().iloc[-1]
        macd_sig = macd_obj.macd_signal().iloc[-1]
        sma_20 = SMAIndicator(close=close, window=20).sma_indicator().iloc[-1]
        sma_50 = SMAIndicator(close=close, window=50).sma_indicator().iloc[-1]
        ema_12 = EMAIndicator(close=close, window=12).ema_indicator().iloc[-1]
        ema_26 = EMAIndicator(close=close, window=26).ema_indicator().iloc[-1]
        bb = BollingerBands(close=close, window=20, window_dev=2)
        bb_lower = bb.bollinger_lband().iloc[-1]
        bb_upper = bb.bollinger_hband().iloc[-1]
        adx_val = ADXIndicator(high=high, low=low, close=close).adx().iloc[-1]
        atr_val = AverageTrueRange(high=high, low=low, close=close).average_true_range().iloc[-1]

        price = float(close.iloc[-1])
        vol_avg = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else float(volume.mean())
        vol_latest = float(volume.iloc[-1])

        score = 5.0  # neutral baseline
        signals: list[str] = []

        # RSI signal
        if pd.notna(rsi_val):
            if rsi_val < 30:
                score += 1.5
                signals.append(f"RSI oversold ({rsi_val:.0f})")
            elif rsi_val < 40:
                score += 0.5
                signals.append(f"RSI near oversold ({rsi_val:.0f})")
            elif rsi_val > 70:
                score -= 1.5
                signals.append(f"RSI overbought ({rsi_val:.0f})")

        # MACD crossover
        if pd.notna(macd_val) and pd.notna(macd_sig):
            if macd_val > macd_sig:
                score += 1.0
                signals.append("MACD bullish crossover")
            else:
                score -= 0.5

        # Price vs moving averages
        if pd.notna(sma_20) and pd.notna(sma_50):
            if price > sma_20 > sma_50:
                score += 1.0
                signals.append("Uptrend (price > SMA20 > SMA50)")
            elif price < sma_20 < sma_50:
                score -= 1.0
                signals.append("Downtrend")

        # Bollinger Band position
        if pd.notna(bb_lower) and pd.notna(bb_upper):
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                bb_pct = (price - bb_lower) / bb_range
                if bb_pct < 0.15:
                    score += 1.0
                    signals.append("Near lower Bollinger Band")
                elif bb_pct > 0.85:
                    score -= 0.5

        # ADX trend strength
        if pd.notna(adx_val) and adx_val > 25:
            score += 0.5
            signals.append(f"Strong trend (ADX {adx_val:.0f})")

        # Volume surge
        if vol_avg > 0 and vol_latest > vol_avg * 1.5:
            score += 0.5
            signals.append("Volume surge")

        score = max(1.0, min(10.0, score))

        if score >= 7:
            signal = "buy"
        elif score <= 3.5:
            signal = "sell"
        else:
            signal = "hold"

        # Calculate levels
        atr_f = float(atr_val) if pd.notna(atr_val) else price * 0.02
        stop_loss = round(price - 2 * atr_f, 2)
        take_profit = round(price + 3 * atr_f, 2)

        # Risk level
        volatility_pct = (atr_f / price * 100) if price > 0 else 5
        if volatility_pct > 3:
            risk = "high"
        elif volatility_pct > 1.5:
            risk = "medium"
        else:
            risk = "low"

        return {
            "ticker": ticker.upper(),
            "score": round(score, 1),
            "signal": signal,
            "price": round(price, 2),
            "entry_price": round(price, 2),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_level": risk,
            "signals": signals,
            "indicators": {
                "rsi": round(float(rsi_val), 1) if pd.notna(rsi_val) else None,
                "macd": round(float(macd_val), 4) if pd.notna(macd_val) else None,
                "adx": round(float(adx_val), 1) if pd.notna(adx_val) else None,
                "atr": round(atr_f, 4),
            },
        }
    except Exception:
        return None


def run_scan(
    db: Session, user_id: int | None,
    tickers: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Scan a list of tickers, score them, store results, return sorted."""
    from ..models.trading import ScanResult

    scan_list = tickers or DEFAULT_SCAN_TICKERS
    results: list[dict[str, Any]] = []

    for ticker in scan_list:
        scored = _score_ticker(ticker)
        if scored is None:
            continue

        rationale = "; ".join(scored["signals"]) if scored["signals"] else "No strong signals"

        record = ScanResult(
            user_id=user_id,
            ticker=scored["ticker"],
            score=scored["score"],
            signal=scored["signal"],
            entry_price=scored["entry_price"],
            stop_loss=scored["stop_loss"],
            take_profit=scored["take_profit"],
            risk_level=scored["risk_level"],
            rationale=rationale,
            indicator_data=json.dumps(scored["indicators"]),
        )
        db.add(record)
        results.append(scored)

    db.commit()
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def get_latest_scan(db: Session, user_id: int | None, limit: int = 20) -> list[dict]:
    """Get the most recent scan results."""
    from ..models.trading import ScanResult

    rows = db.query(ScanResult).filter(
        ScanResult.user_id == user_id,
    ).order_by(ScanResult.scanned_at.desc(), ScanResult.score.desc()).limit(limit).all()

    return [
        {
            "id": r.id, "ticker": r.ticker, "score": r.score, "signal": r.signal,
            "entry_price": r.entry_price, "stop_loss": r.stop_loss,
            "take_profit": r.take_profit, "risk_level": r.risk_level,
            "rationale": r.rationale,
            "indicators": json.loads(r.indicator_data) if r.indicator_data else {},
            "scanned_at": r.scanned_at.isoformat(),
        }
        for r in rows
    ]


# ── Portfolio Tracker ──────────────────────────────────────────────────

def get_portfolio_summary(db: Session, user_id: int | None) -> dict[str, Any]:
    """Full portfolio overview: open positions, equity curve, benchmark."""
    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "open",
    ).all()
    closed_trades = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
    ).order_by(Trade.exit_date.asc()).all()

    # Open positions with live P&L
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

    # Equity curve from closed trades
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

    # Allocation percentages
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


# ── Continuous Learning: Market Snapshots + Pattern Mining ─────────────

def take_market_snapshot(db: Session, ticker: str) -> None:
    """Record today's indicator state for a ticker (called by background task)."""
    from ..models.trading import MarketSnapshot

    try:
        snap = get_indicator_snapshot(ticker, "1d")
        quote = fetch_quote(ticker)
        price = quote.get("price", 0) if quote else 0

        record = MarketSnapshot(
            ticker=ticker.upper(),
            snapshot_date=datetime.utcnow(),
            close_price=price,
            indicator_data=json.dumps(snap),
        )
        db.add(record)
        db.commit()
    except Exception:
        pass


def take_all_snapshots(db: Session, user_id: int | None) -> int:
    """Snapshot all watchlist tickers + defaults. Returns count."""
    tickers = set(DEFAULT_SCAN_TICKERS[:20])

    watchlist = get_watchlist(db, user_id)
    for w in watchlist:
        tickers.add(w.ticker)

    count = 0
    for ticker in tickers:
        take_market_snapshot(db, ticker)
        count += 1
    return count


def backfill_future_returns(db: Session) -> int:
    """Fill in 5d/10d future returns for past snapshots that now have enough data."""
    from ..models.trading import MarketSnapshot

    unfilled = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.is_(None),
    ).limit(100).all()

    updated = 0
    for snap in unfilled:
        try:
            t = yf.Ticker(snap.ticker)
            df = t.history(start=snap.snapshot_date, period="15d", interval="1d")
            if len(df) < 6:
                continue
            base_price = snap.close_price
            if base_price <= 0:
                continue

            if len(df) >= 6:
                snap.future_return_5d = round(
                    (float(df["Close"].iloc[5]) - base_price) / base_price * 100, 2
                )
            if len(df) >= 11:
                snap.future_return_10d = round(
                    (float(df["Close"].iloc[10]) - base_price) / base_price * 100, 2
                )
            updated += 1
        except Exception:
            continue

    if updated:
        db.commit()
    return updated


def mine_patterns(db: Session, user_id: int | None) -> list[str]:
    """Analyze snapshots to discover profitable indicator patterns."""
    from ..models.trading import MarketSnapshot

    snapshots = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
    ).order_by(MarketSnapshot.snapshot_date.desc()).limit(500).all()

    if len(snapshots) < 20:
        return []

    discoveries: list[str] = []

    # Analyze: RSI oversold → 5d return
    rsi_low = [s for s in snapshots if _snap_indicator(s, "rsi", "value", 0) < 30]
    if len(rsi_low) >= 5:
        avg_ret = sum(s.future_return_5d or 0 for s in rsi_low) / len(rsi_low)
        win_count = sum(1 for s in rsi_low if (s.future_return_5d or 0) > 0)
        win_rate = win_count / len(rsi_low) * 100
        if avg_ret > 0.5:
            pattern = f"RSI < 30 → avg +{avg_ret:.1f}% in 5 days ({win_rate:.0f}% win rate, {len(rsi_low)} samples)"
            discoveries.append(pattern)
            save_insight(db, user_id, pattern, confidence=min(0.9, win_rate / 100))

    # MACD bullish crossover → 5d return
    macd_bull = [s for s in snapshots if _snap_indicator(s, "macd", "macd", 0) > _snap_indicator(s, "macd", "signal", 0)]
    if len(macd_bull) >= 5:
        avg_ret = sum(s.future_return_5d or 0 for s in macd_bull) / len(macd_bull)
        win_count = sum(1 for s in macd_bull if (s.future_return_5d or 0) > 0)
        win_rate = win_count / len(macd_bull) * 100
        if avg_ret > 0.3:
            pattern = f"MACD bullish → avg +{avg_ret:.1f}% in 5 days ({win_rate:.0f}% win rate, {len(macd_bull)} samples)"
            discoveries.append(pattern)
            save_insight(db, user_id, pattern, confidence=min(0.9, win_rate / 100))

    return discoveries


def _snap_indicator(snapshot, ind_name: str, key: str, default: float) -> float:
    """Extract a single value from a MarketSnapshot's indicator_data JSON."""
    try:
        data = json.loads(snapshot.indicator_data) if snapshot.indicator_data else {}
        ind = data.get(ind_name, {})
        if isinstance(ind, dict):
            return float(ind.get(key, default))
        return float(ind) if ind is not None else default
    except Exception:
        return default


# ── Signal Generation (beginner-friendly) ──────────────────────────────

def generate_signals(
    db: Session, user_id: int | None,
) -> list[dict[str, Any]]:
    """Generate buy/hold/sell signals for all watchlist tickers."""
    watchlist = get_watchlist(db, user_id)
    if not watchlist:
        return []

    signals = []
    insights = get_insights(db, user_id, limit=10)
    insight_text = "; ".join(i.pattern_description for i in insights) if insights else ""

    for w in watchlist:
        scored = _score_ticker(w.ticker)
        if not scored:
            continue

        # Confidence from backtest history
        from ..models.trading import BacktestResult
        best_bt = db.query(BacktestResult).filter(
            BacktestResult.ticker == w.ticker,
        ).order_by(BacktestResult.return_pct.desc()).first()

        bt_confidence = 0
        if best_bt and best_bt.win_rate > 50:
            bt_confidence = min(30, best_bt.win_rate - 50)

        base_confidence = (scored["score"] / 10) * 70
        confidence = min(95, base_confidence + bt_confidence)

        # Plain English explanation
        explanation = _make_plain_english(scored, insight_text)

        signals.append({
            **scored,
            "confidence": round(confidence, 0),
            "explanation": explanation,
            "best_strategy": best_bt.strategy_name if best_bt else None,
        })

    signals.sort(key=lambda s: s["score"], reverse=True)
    return signals


def _make_plain_english(scored: dict, insights: str) -> str:
    """Convert technical signals into beginner-friendly language."""
    parts = []
    signal = scored["signal"]

    if signal == "buy":
        parts.append("This stock looks like a good buying opportunity right now.")
    elif signal == "sell":
        parts.append("This stock might be overpriced. Consider taking profits.")
    else:
        parts.append("No strong signal either way. Best to wait for a clearer setup.")

    for s in scored.get("signals", [])[:3]:
        if "oversold" in s.lower():
            parts.append("The price has dropped a lot and may be due for a bounce.")
        elif "overbought" in s.lower():
            parts.append("The price has risen sharply and may pull back soon.")
        elif "uptrend" in s.lower():
            parts.append("The overall direction has been up, which is a good sign.")
        elif "downtrend" in s.lower():
            parts.append("The overall direction has been down, so be cautious.")
        elif "volume surge" in s.lower():
            parts.append("Trading activity just spiked, which often signals a big move.")
        elif "macd" in s.lower():
            parts.append("Momentum indicators suggest buyers are stepping in.")
        elif "bollinger" in s.lower():
            parts.append("The price is near a statistical low point and often bounces from here.")

    risk = scored.get("risk_level", "medium")
    if risk == "high":
        parts.append("Risk is HIGH -- only use money you're comfortable losing.")
    elif risk == "low":
        parts.append("This is a relatively stable stock with lower risk.")

    return " ".join(parts)
