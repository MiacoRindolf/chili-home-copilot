"""Trading module business logic: market data, indicators, journal, analytics."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import pandas as pd
import yfinance as yf
from sqlalchemy.orm import Session

from ..models.trading import JournalEntry, LearningEvent, Trade, TradingInsight, WatchlistItem


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
    """Current price + enriched info for a ticker."""
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        result: dict[str, Any] = {
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
        try:
            result["day_high"] = round(float(info.day_high), 2) if info.day_high else None
            result["day_low"] = round(float(info.day_low), 2) if info.day_low else None
            result["year_high"] = round(float(info.year_high), 2) if info.year_high else None
            result["year_low"] = round(float(info.year_low), 2) if info.year_low else None
            result["volume"] = int(info.last_volume) if info.last_volume else None
            result["avg_volume"] = int(info.three_month_average_volume) if hasattr(info, "three_month_average_volume") and info.three_month_average_volume else None
        except Exception:
            pass
        return result
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
    log_learning_event(
        db, user_id, "discovery",
        f"New pattern: {pattern[:120]}",
        confidence_after=confidence,
        related_insight_id=insight.id,
    )
    return insight


def build_ai_context(
    db: Session, user_id: int | None, ticker: str, interval: str = "1d",
) -> str:
    """Assemble rich context for the trading AI: full indicators + scanner + backtest + journal."""
    parts: list[str] = []
    ticker_up = ticker.upper()

    # ── Full indicator snapshot with ALL indicators ──
    try:
        full_indicators = compute_indicators(
            ticker, interval=interval, period="6mo",
            indicators=[
                "rsi", "macd", "sma_20", "sma_50", "ema_20",
                "bbands", "stoch", "adx", "atr", "obv", "mfi",
                "vwap", "psar", "cci", "willr",
            ],
        )
        latest_vals: dict[str, Any] = {}
        for ind_name, records in full_indicators.items():
            if records:
                latest = records[-1]
                latest_vals[ind_name] = {k: v for k, v in latest.items() if k != "time"}
                # Also grab recent trend for key indicators
                if len(records) >= 5 and ind_name in ("rsi", "adx", "obv"):
                    recent_5 = [r.get("value") for r in records[-5:] if r.get("value") is not None]
                    if recent_5:
                        direction = "rising" if recent_5[-1] > recent_5[0] else "falling"
                        latest_vals[ind_name]["5d_direction"] = direction

        parts.append(f"## LIVE INDICATORS — {ticker_up} ({interval})\n{json.dumps(latest_vals, indent=2)}")
    except Exception:
        parts.append(f"## Could not fetch indicators for {ticker_up}")

    # ── Current price + context ──
    quote = fetch_quote(ticker)
    if quote:
        parts.append(
            f"## CURRENT PRICE\n"
            f"Price: ${quote.get('price')} | Day change: {quote.get('change_pct')}% (${quote.get('change')})\n"
            f"Day range: ${quote.get('day_low', 'N/A')} - ${quote.get('day_high', 'N/A')} | "
            f"52wk range: ${quote.get('year_low', 'N/A')} - ${quote.get('year_high', 'N/A')}\n"
            f"Volume: {quote.get('volume', 'N/A')} | Avg volume: {quote.get('avg_volume', 'N/A')}\n"
            f"Market cap: {quote.get('market_cap', 'N/A')}"
        )

    # ── Scanner score if available ──
    scored = _score_ticker(ticker)
    if scored:
        parts.append(
            f"## AI SCANNER SCORE\n"
            f"Score: {scored['score']}/10 | Signal: {scored['signal'].upper()}\n"
            f"Entry: ${scored['entry_price']} | Stop: ${scored['stop_loss']} | Target: ${scored['take_profit']}\n"
            f"Risk: {scored['risk_level'].upper()}\n"
            f"Signals: {', '.join(scored['signals']) if scored['signals'] else 'None strong'}"
        )

    # ── Best backtest results for this ticker ──
    from ..models.trading import BacktestResult
    backtests = db.query(BacktestResult).filter(
        BacktestResult.ticker == ticker_up,
    ).order_by(BacktestResult.return_pct.desc()).limit(3).all()
    if backtests:
        lines = ["## BACKTEST HISTORY (best strategies for this stock)"]
        for bt in backtests:
            lines.append(
                f"- {bt.strategy_name}: {bt.return_pct:+.1f}% return, "
                f"{bt.win_rate:.0f}% win rate, {bt.trade_count} trades, "
                f"Sharpe {bt.sharpe or 'N/A'}, Max DD {bt.max_drawdown:.1f}%"
            )
        parts.append("\n".join(lines))

    # ── User's trades on this ticker ──
    trades = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.ticker == ticker_up,
    ).order_by(Trade.entry_date.desc()).limit(10).all()
    if trades:
        open_trades = [t for t in trades if t.status == "open"]
        closed_trades = [t for t in trades if t.status == "closed"]

        lines = [f"## USER'S TRADES ON {ticker_up}"]
        if open_trades:
            lines.append("OPEN POSITIONS:")
            for tr in open_trades:
                lines.append(
                    f"  - {tr.direction.upper()} {tr.quantity}x @ ${tr.entry_price} (entered {tr.entry_date.strftime('%Y-%m-%d') if tr.entry_date else 'N/A'})"
                )
        if closed_trades:
            lines.append("CLOSED (recent):")
            for tr in closed_trades[:5]:
                result = "WIN" if (tr.pnl or 0) > 0 else "LOSS"
                lines.append(
                    f"  - {tr.direction.upper()} @ ${tr.entry_price} → ${tr.exit_price} | "
                    f"P&L: ${tr.pnl} ({result})"
                )
        parts.append("\n".join(lines))

    # ── Overall portfolio performance ──
    stats = get_trade_stats(db, user_id)
    if stats.get("total_trades", 0) > 0:
        parts.append(
            f"## OVERALL TRADING PERFORMANCE\n"
            f"Total trades: {stats['total_trades']} | Win rate: {stats['win_rate']}%\n"
            f"Total P&L: ${stats['total_pnl']} | Best: ${stats['best_trade']} | Worst: ${stats['worst_trade']}\n"
            f"Max drawdown: ${stats['max_drawdown']}"
        )
    else:
        parts.append("## TRADING HISTORY\nThis user has no closed trades yet. They are a beginner — guide them carefully with clear, specific first-trade advice.")

    # ── AI learned patterns (your edge) ──
    insights = get_insights(db, user_id, limit=10)
    if insights:
        lines = ["## YOUR LEARNED PATTERNS (use these as your edge)"]
        for ins in insights:
            lines.append(
                f"- [{ins.confidence:.0%} confidence, {ins.evidence_count} evidence] "
                f"{ins.pattern_description}"
            )
        parts.append("\n".join(lines))

    # ── Recent journal ──
    journal = get_journal(db, user_id, limit=5)
    if journal:
        lines = ["## RECENT JOURNAL NOTES"]
        for j in journal:
            lines.append(f"- {j.created_at.strftime('%Y-%m-%d')}: {j.content[:300]}")
        parts.append("\n".join(lines))

    # ── Market-wide context ──
    market_ctx = build_market_context(db, user_id)
    if market_ctx:
        parts.insert(0, market_ctx)

    return "\n\n".join(parts)


# ── Market-Wide Context ────────────────────────────────────────────────

def build_market_context(db: Session, user_id: int | None) -> str:
    """Build a market-wide sentiment summary for the AI to use in every response."""
    parts: list[str] = []

    # SPY as market proxy
    spy_quote = fetch_quote("SPY")
    if spy_quote:
        spy_dir = "UP" if (spy_quote.get("change_pct") or 0) >= 0 else "DOWN"
        parts.append(
            f"S&P 500 (SPY): ${spy_quote.get('price')} ({spy_dir} {spy_quote.get('change_pct')}% today)"
        )

    # Quick scan of a sample for sentiment
    sample_tickers = DEFAULT_SCAN_TICKERS[:15] + DEFAULT_CRYPTO_TICKERS[:5]
    bullish = 0
    bearish = 0
    neutral = 0
    rsi_vals: list[float] = []

    for ticker in sample_tickers:
        scored = _score_ticker(ticker)
        if scored is None:
            continue
        if scored["signal"] == "buy":
            bullish += 1
        elif scored["signal"] == "sell":
            bearish += 1
        else:
            neutral += 1
        rsi_v = scored["indicators"].get("rsi")
        if rsi_v is not None:
            rsi_vals.append(rsi_v)

    total = bullish + bearish + neutral
    if total:
        avg_rsi = sum(rsi_vals) / len(rsi_vals) if rsi_vals else 50
        if bullish > bearish * 1.5:
            sentiment = "RISK-ON (bullish majority)"
        elif bearish > bullish * 1.5:
            sentiment = "RISK-OFF (bearish majority)"
        else:
            sentiment = "MIXED / CHOPPY"

        parts.append(
            f"Market sentiment: {sentiment} — {bullish} bullish, {bearish} bearish, {neutral} neutral out of {total} sampled"
        )
        parts.append(f"Average RSI across sample: {avg_rsi:.0f}")

    # Crypto snapshot
    btc_quote = fetch_quote("BTC-USD")
    eth_quote = fetch_quote("ETH-USD")
    if btc_quote:
        parts.append(f"BTC: ${btc_quote.get('price')} ({btc_quote.get('change_pct')}%)")
    if eth_quote:
        parts.append(f"ETH: ${eth_quote.get('price')} ({eth_quote.get('change_pct')}%)")

    if not parts:
        return ""
    return "## MARKET PULSE (live)\n" + "\n".join(parts)


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
            old_conf = matched_existing.confidence
            matched_existing.evidence_count += 1
            matched_existing.confidence = min(0.95, old_conf + 0.05)
            matched_existing.last_seen = datetime.utcnow()
            db.commit()
            log_learning_event(
                db, user_id, "update",
                f"Pattern reinforced: {matched_existing.pattern_description[:100]} "
                f"({old_conf:.0%} -> {matched_existing.confidence:.0%}, {matched_existing.evidence_count} evidence)",
                confidence_before=old_conf,
                confidence_after=matched_existing.confidence,
                related_insight_id=matched_existing.id,
            )
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

DEFAULT_CRYPTO_TICKERS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD",
    "ADA-USD", "DOGE-USD", "AVAX-USD", "DOT-USD", "LINK-USD",
    "MATIC-USD", "ATOM-USD", "UNI-USD", "LTC-USD", "NEAR-USD",
]

ALL_SCAN_TICKERS = DEFAULT_SCAN_TICKERS + DEFAULT_CRYPTO_TICKERS


def ticker_display_name(ticker: str) -> str:
    """Strip -USD suffix for crypto display."""
    return ticker.replace("-USD", "") if ticker.endswith("-USD") else ticker


def is_crypto(ticker: str) -> bool:
    return ticker.upper().endswith("-USD")


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

    scan_list = tickers or ALL_SCAN_TICKERS
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
    tickers = set(DEFAULT_SCAN_TICKERS[:20] + DEFAULT_CRYPTO_TICKERS[:10])

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


# ── Smart Pick: full scan + deep AI analysis ───────────────────────────

def smart_pick(
    db: Session, user_id: int | None,
    message: str | None = None,
    budget: float | None = None,
    risk_tolerance: str = "medium",
) -> dict[str, Any]:
    """Scan the market, score all candidates, deep-analyze the top picks,
    and return one consolidated AI recommendation with exact trade plans."""

    # 1. Score all default tickers (stocks + crypto)
    scored_results: list[dict[str, Any]] = []
    for ticker in ALL_SCAN_TICKERS:
        scored = _score_ticker(ticker)
        if scored and scored["signal"] == "buy" and scored["score"] >= 5.5:
            scored_results.append(scored)

    # Also score watchlist tickers
    watchlist = get_watchlist(db, user_id)
    wl_tickers = {w.ticker for w in watchlist}
    for ticker in wl_tickers:
        if ticker not in {s["ticker"] for s in scored_results}:
            scored = _score_ticker(ticker)
            if scored and scored["score"] >= 4.0:
                scored_results.append(scored)

    scored_results.sort(key=lambda r: r["score"], reverse=True)

    # Filter by risk tolerance
    if risk_tolerance == "low":
        scored_results = [s for s in scored_results if s["risk_level"] in ("low", "medium")]
    elif risk_tolerance == "high":
        pass  # allow all

    top_picks = scored_results[:8]

    if not top_picks:
        return {
            "ok": True,
            "reply": "I scanned 50+ stocks and none have a strong enough setup right now. "
                     "The best trade is sometimes no trade. I'll keep watching and flag opportunities as they appear.",
            "picks": [],
        }

    # 2. Build rich context for the top picks
    pick_details: list[str] = []
    for p in top_picks:
        detail = (
            f"**{p['ticker']}** — Score: {p['score']}/10, Signal: {p['signal'].upper()}\n"
            f"  Price: ${p['price']} | Entry: ${p['entry_price']} | Stop: ${p['stop_loss']} | Target: ${p['take_profit']}\n"
            f"  Risk: {p['risk_level'].upper()} | Signals: {', '.join(p['signals'])}\n"
            f"  Indicators: RSI={p['indicators'].get('rsi', 'N/A')}, "
            f"MACD={p['indicators'].get('macd', 'N/A')}, "
            f"ADX={p['indicators'].get('adx', 'N/A')}"
        )

        # Add backtest data if available
        from ..models.trading import BacktestResult
        best_bt = db.query(BacktestResult).filter(
            BacktestResult.ticker == p["ticker"],
        ).order_by(BacktestResult.return_pct.desc()).first()
        if best_bt:
            detail += (
                f"\n  Best backtest: {best_bt.strategy_name} → "
                f"{best_bt.return_pct:+.1f}% return, {best_bt.win_rate:.0f}% win rate"
            )

        pick_details.append(detail)

    # 3. Build the AI context
    context_parts = [
        f"## MARKET SCAN RESULTS — Top {len(top_picks)} candidates from 50+ stocks scanned",
        "\n\n".join(pick_details),
    ]

    # User's performance context
    stats = get_trade_stats(db, user_id)
    if stats.get("total_trades", 0) > 0:
        context_parts.append(
            f"## USER PROFILE\n"
            f"Experience: {stats['total_trades']} trades, {stats['win_rate']}% win rate, "
            f"Total P&L: ${stats['total_pnl']}"
        )
    else:
        context_parts.append(
            "## USER PROFILE\nBeginner trader with no closed trades yet. "
            "Recommend safer, high-confidence setups with clear instructions."
        )

    # Learned patterns
    insights = get_insights(db, user_id, limit=10)
    if insights:
        lines = ["## LEARNED PATTERNS (your edge)"]
        for ins in insights:
            lines.append(f"- [{ins.confidence:.0%}] {ins.pattern_description}")
        context_parts.append("\n".join(lines))

    if budget:
        context_parts.append(f"## BUDGET\nUser has ${budget:,.2f} available to invest.")

    context_parts.append(f"## RISK TOLERANCE: {risk_tolerance.upper()}")

    full_context = "\n\n".join(context_parts)

    # 4. Ask the AI for the final recommendation
    user_msg = message or "Based on this scan, what are your top 3 stock picks I should buy RIGHT NOW? For each one, give me the exact buy-in price, sell target, stop-loss, expected hold duration, position size, and your confidence level. Rank them by conviction."

    from ..prompts import load_prompt
    system_prompt = load_prompt("trading_analyst")

    smart_pick_addendum = """

SPECIAL INSTRUCTION — SMART PICK MODE:
You have just scanned 50+ stocks. The top candidates are provided below with their indicator data and scores.

Your job: Pick the BEST 1-3 trades from this scan and present them as a clear action plan.

For EACH recommended trade, you MUST provide ALL of these in a clean format:
1. **TICKER** and company name
2. **VERDICT**: STRONG BUY / BUY (no holds or sells in smart pick)
3. **Confidence**: X% (be honest)
4. **Buy-in price**: exact $ amount (use current price or a limit order level)
5. **Stop-loss**: exact $ amount + reason
6. **Target 1**: exact $ (conservative exit)
7. **Target 2**: exact $ (optimistic exit)
8. **Risk/reward ratio**: X:1
9. **Hold duration**: X days/weeks (be specific)
10. **Position size**: X% of portfolio
11. **Why this stock NOW**: 2-3 bullet points of the key confluence signals
12. **What would make you exit early**: the invalidation signal

If NONE of the scanned stocks have a strong enough setup, say so clearly. "No trade" IS a valid recommendation.

End with a brief portfolio allocation suggestion if recommending multiple stocks.
"""

    try:
        from .. import openai_client
        from ..logger import new_trace_id
        trace_id = new_trace_id()

        result = openai_client.chat(
            messages=[{"role": "user", "content": user_msg}],
            system_prompt=f"{system_prompt}\n{smart_pick_addendum}\n\n---\n\n{full_context}",
            trace_id=trace_id,
            user_message=user_msg,
        )
        reply = result.get("reply", "Could not generate recommendation.")
    except Exception as e:
        reply = f"Analysis unavailable: {e}"

    return {
        "ok": True,
        "reply": reply,
        "picks_scanned": len(scored_results),
        "top_picks": [
            {"ticker": p["ticker"], "score": p["score"], "signal": p["signal"], "price": p["price"]}
            for p in top_picks
        ],
    }


# ── Learning Event Logger ──────────────────────────────────────────────

def log_learning_event(
    db: Session, user_id: int | None,
    event_type: str, description: str,
    confidence_before: float | None = None,
    confidence_after: float | None = None,
    related_insight_id: int | None = None,
) -> LearningEvent:
    ev = LearningEvent(
        user_id=user_id,
        event_type=event_type,
        description=description,
        confidence_before=confidence_before,
        confidence_after=confidence_after,
        related_insight_id=related_insight_id,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def get_learning_events(db: Session, user_id: int | None, limit: int = 50) -> list[LearningEvent]:
    return db.query(LearningEvent).filter(
        LearningEvent.user_id == user_id,
    ).order_by(LearningEvent.created_at.desc()).limit(limit).all()


# ── Auto-Journaling ───────────────────────────────────────────────────

def auto_journal_trade_open(db: Session, trade: Trade) -> None:
    """AI auto-journals why a trade was opened."""
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
    # Scan top movers
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

    # Count bullish/bearish from scored
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
    log_learning_event(db, user_id, "journal", f"Daily market observation recorded")
    return content


def check_signal_events(db: Session, user_id: int | None) -> list[str]:
    """Scan watchlist for significant indicator events and auto-journal them."""
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


# ── Brain Dashboard Stats ──────────────────────────────────────────────

def get_brain_stats(db: Session, user_id: int | None) -> dict[str, Any]:
    """Aggregate stats for the AI Brain dashboard."""
    from ..models.trading import MarketSnapshot
    from sqlalchemy import func

    total_patterns = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id, TradingInsight.active.is_(True),
    ).count()

    avg_confidence_row = db.query(func.avg(TradingInsight.confidence)).filter(
        TradingInsight.user_id == user_id, TradingInsight.active.is_(True),
    ).scalar()
    avg_confidence = round(float(avg_confidence_row or 0) * 100, 1)

    from datetime import timedelta
    week_ago = datetime.utcnow() - timedelta(days=7)
    patterns_this_week = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.created_at >= week_ago,
    ).count()

    total_snapshots = db.query(MarketSnapshot).count()

    # Prediction accuracy: snapshots where we can compare signal vs outcome
    filled_snaps = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
    ).limit(200).all()
    correct = 0
    total_predictions = 0
    for snap in filled_snaps:
        try:
            ind_data = json.loads(snap.indicator_data) if snap.indicator_data else {}
            rsi_val = ind_data.get("rsi", {}).get("value")
            if rsi_val is not None:
                predicted_up = rsi_val < 50
                actual_up = (snap.future_return_5d or 0) > 0
                if predicted_up == actual_up:
                    correct += 1
                total_predictions += 1
        except Exception:
            continue
    accuracy = round(correct / total_predictions * 100, 1) if total_predictions > 0 else 0

    total_events = db.query(LearningEvent).filter(
        LearningEvent.user_id == user_id,
    ).count()

    return {
        "total_patterns": total_patterns,
        "avg_confidence": avg_confidence,
        "patterns_this_week": patterns_this_week,
        "total_snapshots": total_snapshots,
        "prediction_accuracy": accuracy,
        "total_predictions": total_predictions,
        "total_events": total_events,
    }


def get_confidence_history(db: Session, user_id: int | None) -> list[dict[str, Any]]:
    """Weekly average confidence over time for the Brain chart."""
    insights = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
    ).order_by(TradingInsight.created_at.asc()).all()

    if not insights:
        return []

    from datetime import timedelta
    points: list[dict[str, Any]] = []
    if insights:
        start = insights[0].created_at
        end = datetime.utcnow()
        current = start
        while current <= end:
            week_end = current + timedelta(days=7)
            week_insights = [i for i in insights if current <= i.created_at < week_end and i.active]
            if week_insights:
                avg_conf = sum(i.confidence for i in week_insights) / len(week_insights)
                points.append({
                    "time": int(current.timestamp()),
                    "value": round(avg_conf * 100, 1),
                    "count": len(week_insights),
                })
            current = week_end

    return points
