"""Trading module business logic: market data, indicators, journal, analytics."""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Optional
import threading

import pandas as pd
import yfinance as yf
from sqlalchemy.orm import Session

from ..models.trading import JournalEntry, LearningEvent, Trade, TradingInsight, WatchlistItem

logger = logging.getLogger(__name__)

_shutting_down = threading.Event()


def signal_shutdown():
    """Call during app shutdown to stop long-running background tasks gracefully."""
    _shutting_down.set()


# ── Market data (yfinance) ──────────────────────────────────────────────

_VALID_INTERVALS = {
    "1m", "2m", "5m", "15m", "30m", "60m", "90m",
    "1h", "1d", "5d", "1wk", "1mo", "3mo",
}
_VALID_PERIODS = {
    "1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max",
}


_INTERVAL_MAX_PERIOD: dict[str, list[str]] = {
    "1m": ["1d", "5d"],
    "2m": ["1d", "5d"],
    "5m": ["1d", "5d", "1mo"],
    "15m": ["1d", "5d", "1mo"],
    "30m": ["1d", "5d", "1mo"],
    "1h": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y"],
    "60m": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y"],
    "90m": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y"],
}
_INTERVAL_DEFAULT_PERIOD: dict[str, str] = {
    "1m": "1d", "2m": "5d", "5m": "5d", "15m": "1mo", "30m": "1mo",
    "1h": "3mo", "60m": "3mo", "90m": "3mo",
}


def _clamp_period(interval: str, period: str) -> str:
    """Ensure the requested period is valid for the given interval (yfinance limits)."""
    allowed = _INTERVAL_MAX_PERIOD.get(interval)
    if allowed is None:
        return period
    if period in allowed:
        return period
    return _INTERVAL_DEFAULT_PERIOD.get(interval, allowed[-1])


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
    period = _clamp_period(interval, period)

    try:
        t = yf.Ticker(ticker)
        df = t.history(period=period, interval=interval)
    except Exception as e:
        logger.warning(f"[trading] OHLCV fetch failed for {ticker}: {e}")
        return []

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
    period = _clamp_period(interval, period)

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
                "rsi", "macd", "sma_20", "sma_50",
                "ema_20", "ema_50", "ema_100", "ema_200",
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
    """Score a single ticker using multi-signal confluence (1-10).

    Computes all major indicators including EMA 20/50/100/200 for trend analysis.
    """
    try:
        from ta.momentum import RSIIndicator, StochasticOscillator
        from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator
        from ta.volatility import BollingerBands, AverageTrueRange

        t = yf.Ticker(ticker)
        df = t.history(period="6mo", interval="1d")
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
        macd_hist = macd_obj.macd_diff().iloc[-1]
        sma_20 = SMAIndicator(close=close, window=20).sma_indicator().iloc[-1]
        sma_50 = SMAIndicator(close=close, window=50).sma_indicator().iloc[-1]
        ema_20 = EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
        ema_50 = EMAIndicator(close=close, window=50).ema_indicator().iloc[-1]
        ema_100 = EMAIndicator(close=close, window=100).ema_indicator().iloc[-1] if len(close) >= 100 else None
        ema_200 = EMAIndicator(close=close, window=200).ema_indicator().iloc[-1] if len(close) >= 200 else None
        bb = BollingerBands(close=close, window=20, window_dev=2)
        bb_lower = bb.bollinger_lband().iloc[-1]
        bb_upper = bb.bollinger_hband().iloc[-1]
        adx_val = ADXIndicator(high=high, low=low, close=close).adx().iloc[-1]
        atr_val = AverageTrueRange(high=high, low=low, close=close).average_true_range().iloc[-1]
        stoch = StochasticOscillator(high=high, low=low, close=close)
        stoch_k = stoch.stoch().iloc[-1]

        price = float(close.iloc[-1])
        vol_avg = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else float(volume.mean())
        vol_latest = float(volume.iloc[-1])

        score = 5.0
        signals: list[str] = []

        # ── EMA Stacking (Price > EMA20 > EMA50 > EMA100) ──
        ema_stack_bullish = False
        ema_stack_bearish = False
        if pd.notna(ema_20) and pd.notna(ema_50):
            e20 = float(ema_20)
            e50 = float(ema_50)
            e100 = float(ema_100) if ema_100 is not None and pd.notna(ema_100) else None

            if e100 is not None and price > e20 > e50 > e100:
                ema_stack_bullish = True
                score += 1.5
                signals.append(f"EMA stacking bullish (P>{e20:.0f}>{e50:.0f}>{e100:.0f})")
            elif price > e20 > e50:
                score += 0.8
                signals.append("Partial EMA alignment (P>EMA20>EMA50)")
            elif e100 is not None and price < e20 < e50 < e100:
                ema_stack_bearish = True
                score -= 1.5
                signals.append("EMA stacking bearish")
            elif price < e20 < e50:
                score -= 0.8
                signals.append("Bearish EMA alignment")

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

        # Price vs SMA
        if pd.notna(sma_20) and pd.notna(sma_50):
            if price > sma_20 > sma_50:
                score += 0.5
                signals.append("Uptrend (price > SMA20 > SMA50)")
            elif price < sma_20 < sma_50:
                score -= 0.5
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
            "ema_stack_bullish": ema_stack_bullish,
            "ema_stack_bearish": ema_stack_bearish,
            "indicators": {
                "rsi": round(float(rsi_val), 1) if pd.notna(rsi_val) else None,
                "macd": round(float(macd_val), 4) if pd.notna(macd_val) else None,
                "macd_hist": round(float(macd_hist), 4) if pd.notna(macd_hist) else None,
                "adx": round(float(adx_val), 1) if pd.notna(adx_val) else None,
                "atr": round(atr_f, 4),
                "ema_20": round(float(ema_20), 2) if pd.notna(ema_20) else None,
                "ema_50": round(float(ema_50), 2) if pd.notna(ema_50) else None,
                "ema_100": round(float(ema_100), 2) if ema_100 is not None and pd.notna(ema_100) else None,
                "ema_200": round(float(ema_200), 2) if ema_200 is not None and pd.notna(ema_200) else None,
                "stoch_k": round(float(stoch_k), 1) if pd.notna(stoch_k) else None,
                "bb_pct": round((price - float(bb_lower)) / (float(bb_upper) - float(bb_lower)) * 100, 1)
                    if pd.notna(bb_lower) and pd.notna(bb_upper) and float(bb_upper) > float(bb_lower) else None,
                "vol_ratio": round(vol_latest / vol_avg, 2) if vol_avg > 0 else None,
            },
        }
    except Exception:
        return None


# ── Custom Screener ───────────────────────────────────────────────────

PRESET_SCREENS: dict[str, dict[str, Any]] = {
    "ema_stack_bullish": {
        "name": "EMA Stacking (Bullish)",
        "description": "Price > EMA20 > EMA50 > EMA100 — perfect bullish alignment showing strong uptrend with all timeframes agreeing",
        "conditions": [
            {"field": "ema_stack_bullish", "op": "eq", "value": True},
        ],
        "confirmations": [
            {"field": "adx", "op": "gte", "value": 20, "label": "ADX > 20 (trending)"},
            {"field": "rsi", "op": "between", "value": [40, 70], "label": "RSI 40-70 (not overbought)"},
            {"field": "macd_hist", "op": "gt", "value": 0, "label": "MACD histogram positive"},
        ],
    },
    "ema_stack_bearish": {
        "name": "EMA Stacking (Bearish)",
        "description": "Price < EMA20 < EMA50 < EMA100 — perfect bearish alignment, avoid or short",
        "conditions": [
            {"field": "ema_stack_bearish", "op": "eq", "value": True},
        ],
    },
    "oversold_bounce": {
        "name": "Oversold Bounce",
        "description": "RSI below 30 with MACD turning positive — potential reversal from oversold conditions",
        "conditions": [
            {"field": "rsi", "op": "lt", "value": 30},
            {"field": "macd_hist", "op": "gt", "value": 0},
        ],
    },
    "golden_cross": {
        "name": "Golden Cross Setup",
        "description": "EMA20 crossed above EMA50 with price above both — classic bullish trend start",
        "conditions": [
            {"field": "ema_20", "op": "gt_field", "value": "ema_50"},
            {"field": "price", "op": "gt_field", "value": "ema_20"},
            {"field": "adx", "op": "gte", "value": 20},
        ],
    },
    "vol_breakout": {
        "name": "Volume Breakout",
        "description": "Volume 2x above average with bullish EMA alignment — institutional buying signal",
        "conditions": [
            {"field": "vol_ratio", "op": "gte", "value": 2.0},
            {"field": "ema_20", "op": "gt_field", "value": "ema_50"},
            {"field": "rsi", "op": "between", "value": [45, 75]},
        ],
    },
    "bb_squeeze_bullish": {
        "name": "Bollinger Squeeze (Bullish)",
        "description": "Price near lower BB with RSI oversold — mean reversion bounce expected",
        "conditions": [
            {"field": "bb_pct", "op": "lt", "value": 15},
            {"field": "rsi", "op": "lt", "value": 35},
        ],
    },
}


def _eval_condition(cond: dict, scored: dict) -> bool:
    """Evaluate a single screening condition against a scored ticker result."""
    field = cond["field"]
    op = cond["op"]
    value = cond["value"]

    # Direct top-level fields
    if field in scored:
        actual = scored[field]
    elif field in scored.get("indicators", {}):
        actual = scored["indicators"][field]
    else:
        actual = scored.get("indicators", {}).get(field)

    if actual is None:
        return False

    if op == "eq":
        return actual == value
    elif op == "gt":
        return float(actual) > float(value)
    elif op == "gte":
        return float(actual) >= float(value)
    elif op == "lt":
        return float(actual) < float(value)
    elif op == "lte":
        return float(actual) <= float(value)
    elif op == "between":
        return float(value[0]) <= float(actual) <= float(value[1])
    elif op == "gt_field":
        ref = scored.get("indicators", {}).get(value)
        if ref is None:
            ref = scored.get(value)
        return ref is not None and float(actual) > float(ref)
    return False


def run_custom_screen(
    screen_id: str | None = None,
    conditions: list[dict] | None = None,
    tickers: list[str] | None = None,
) -> dict[str, Any]:
    """Run a preset or custom screen against the ticker universe."""
    from .ticker_universe import get_full_ticker_universe

    if screen_id and screen_id in PRESET_SCREENS:
        preset = PRESET_SCREENS[screen_id]
        conds = preset["conditions"]
        confirms = preset.get("confirmations", [])
        screen_name = preset["name"]
    elif conditions:
        conds = conditions
        confirms = []
        screen_name = "Custom Screen"
    else:
        return {"ok": False, "error": "No screen_id or conditions provided"}

    scan_list = tickers or get_full_ticker_universe()
    scored_all = batch_score_tickers(scan_list, max_workers=_MAX_SCAN_WORKERS)

    matches = []
    for scored in scored_all:
        if all(_eval_condition(c, scored) for c in conds):
            # Count how many confirmation signals are met
            conf_met = 0
            conf_details = []
            for conf in confirms:
                met = _eval_condition(conf, scored)
                if met:
                    conf_met += 1
                conf_details.append({"label": conf.get("label", ""), "met": met})

            matches.append({
                **scored,
                "confirmations_met": conf_met,
                "confirmations_total": len(confirms),
                "confirmation_details": conf_details,
            })

    matches.sort(key=lambda m: (m.get("confirmations_met", 0), m["score"]), reverse=True)

    return {
        "ok": True,
        "screen_name": screen_name,
        "total_scanned": len(scored_all),
        "matches": len(matches),
        "results": matches,
    }


def run_scan(
    db: Session, user_id: int | None,
    tickers: list[str] | None = None,
    use_full_universe: bool = False,
) -> list[dict[str, Any]]:
    """Scan a list of tickers, score them, store results, return sorted.

    If use_full_universe=True and no tickers list given, scans ALL US stocks + crypto.
    """
    from ..models.trading import ScanResult
    from .ticker_universe import get_full_ticker_universe

    if tickers:
        scan_list = tickers
    elif use_full_universe:
        scan_list = get_full_ticker_universe()
    else:
        scan_list = list(ALL_SCAN_TICKERS)

    if len(scan_list) > 100:
        results = batch_score_tickers(scan_list, max_workers=_MAX_SCAN_WORKERS)
    else:
        results = []
        for ticker in scan_list:
            scored = _score_ticker(ticker)
            if scored is not None:
                results.append(scored)
        results.sort(key=lambda r: r["score"], reverse=True)

    for scored in results:
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

    db.commit()
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


def take_all_snapshots(db: Session, user_id: int | None, ticker_list: list[str] | None = None) -> int:
    """Snapshot given tickers (or watchlist + top defaults). Returns count."""
    if ticker_list:
        tickers = set(ticker_list)
    else:
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


def _mine_from_history(ticker: str) -> list[dict]:
    """Compute indicator states at each historical bar and pair with actual 5d/10d returns.

    This lets us discover patterns immediately from past price data instead of
    waiting weeks for snapshot future returns to backfill.
    """
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange

    try:
        t = yf.Ticker(ticker)
        df = t.history(period="1y", interval="1d")
        if df.empty or len(df) < 60:
            return []
    except Exception:
        return []

    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    rsi = RSIIndicator(close=close, window=14).rsi()
    macd_obj = MACD(close=close)
    macd_line = macd_obj.macd()
    macd_signal = macd_obj.macd_signal()
    macd_hist = macd_obj.macd_diff()
    sma20 = SMAIndicator(close=close, window=20).sma_indicator()
    ema20 = EMAIndicator(close=close, window=20).ema_indicator()
    ema50 = EMAIndicator(close=close, window=50).ema_indicator()
    ema100 = EMAIndicator(close=close, window=100).ema_indicator()
    bb = BollingerBands(close=close, window=20, window_dev=2)
    bb_upper = bb.bollinger_hband()
    bb_lower = bb.bollinger_lband()
    adx = ADXIndicator(high=high, low=low, close=close).adx()
    atr = AverageTrueRange(high=high, low=low, close=close).average_true_range()
    stoch = StochasticOscillator(high=high, low=low, close=close)
    stoch_k = stoch.stoch()

    rows = []
    for i in range(50, len(df) - 10):
        price = float(close.iloc[i])
        if price <= 0:
            continue
        ret_5d = (float(close.iloc[i + 5]) - price) / price * 100
        ret_10d = (float(close.iloc[i + 10]) - price) / price * 100 if i + 10 < len(df) else None

        bb_range = float(bb_upper.iloc[i]) - float(bb_lower.iloc[i]) if pd.notna(bb_upper.iloc[i]) and pd.notna(bb_lower.iloc[i]) else 0
        bb_pct = (price - float(bb_lower.iloc[i])) / bb_range if bb_range > 0 else 0.5

        e20 = float(ema20.iloc[i]) if pd.notna(ema20.iloc[i]) else None
        e50 = float(ema50.iloc[i]) if pd.notna(ema50.iloc[i]) else None
        e100 = float(ema100.iloc[i]) if pd.notna(ema100.iloc[i]) else None

        rows.append({
            "ticker": ticker,
            "price": price,
            "ret_5d": round(ret_5d, 2),
            "ret_10d": round(ret_10d, 2) if ret_10d is not None else None,
            "rsi": float(rsi.iloc[i]) if pd.notna(rsi.iloc[i]) else 50,
            "macd": float(macd_line.iloc[i]) if pd.notna(macd_line.iloc[i]) else 0,
            "macd_sig": float(macd_signal.iloc[i]) if pd.notna(macd_signal.iloc[i]) else 0,
            "macd_hist": float(macd_hist.iloc[i]) if pd.notna(macd_hist.iloc[i]) else 0,
            "adx": float(adx.iloc[i]) if pd.notna(adx.iloc[i]) else 0,
            "bb_pct": bb_pct,
            "atr": float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0,
            "stoch_k": float(stoch_k.iloc[i]) if pd.notna(stoch_k.iloc[i]) else 50,
            "above_sma20": price > float(sma20.iloc[i]) if pd.notna(sma20.iloc[i]) else False,
            "ema_stack": (e20 is not None and e50 is not None and e100 is not None
                          and price > e20 > e50 > e100),
            "is_crypto": ticker.endswith("-USD"),
        })
    return rows


def mine_patterns(db: Session, user_id: int | None) -> list[str]:
    """Discover patterns from historical price data + any existing snapshots.

    Uses up to 1 year of historical OHLCV data from popular tickers to
    compute what indicator states historically led to profits, so patterns
    can be discovered immediately without waiting for snapshot backfills.
    """
    from ..models.trading import MarketSnapshot

    # Gather historical indicator+return data from a diverse set of tickers
    mine_tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "META",
                    "BTC-USD", "ETH-USD", "SOL-USD", "SPY", "QQQ", "AMD",
                    "JPM", "V", "NFLX", "COST"]

    watchlist = get_watchlist(db, user_id)
    for w in watchlist:
        if w.ticker not in mine_tickers:
            mine_tickers.append(w.ticker)

    all_rows: list[dict] = []
    for ticker in mine_tickers[:25]:
        if _shutting_down.is_set():
            break
        rows = _mine_from_history(ticker)
        all_rows.extend(rows)

    # Also include any existing snapshots with backfilled returns
    snapshots = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
    ).order_by(MarketSnapshot.snapshot_date.desc()).limit(500).all()

    for s in snapshots:
        try:
            data = json.loads(s.indicator_data) if s.indicator_data else {}
            rsi_data = data.get("rsi", {})
            macd_data = data.get("macd", {})
            bb_data = data.get("bbands", {})
            adx_data = data.get("adx", {})
            stoch_data = data.get("stoch", {})
            sma20_data = data.get("sma_20", {})

            bb_range = ((bb_data.get("upper", 0) or 0) - (bb_data.get("lower", 0) or 0))
            bb_pct = ((s.close_price - (bb_data.get("lower", 0) or 0)) / bb_range
                      if bb_range > 0 and s.close_price else 0.5)

            all_rows.append({
                "ticker": s.ticker,
                "price": s.close_price or 0,
                "ret_5d": s.future_return_5d or 0,
                "ret_10d": s.future_return_10d,
                "rsi": rsi_data.get("value", 50) or 50,
                "macd": macd_data.get("macd", 0) or 0,
                "macd_sig": macd_data.get("signal", 0) or 0,
                "macd_hist": macd_data.get("histogram", 0) or 0,
                "adx": adx_data.get("adx", 0) or 0,
                "bb_pct": bb_pct,
                "atr": data.get("atr", {}).get("value", 0) or 0,
                "stoch_k": stoch_data.get("k", 50) or 50,
                "above_sma20": (s.close_price > (sma20_data.get("value", 0) or 0)
                                if sma20_data and s.close_price else False),
                "ema_stack": False,
                "is_crypto": s.ticker.endswith("-USD"),
            })
        except Exception:
            continue

    if len(all_rows) < 20:
        return []

    discoveries: list[str] = []
    MIN_SAMPLES = 5

    def _check(filtered, label):
        if len(filtered) < MIN_SAMPLES:
            return
        avg_5d = sum(r["ret_5d"] for r in filtered) / len(filtered)
        avg_10d_vals = [r["ret_10d"] for r in filtered if r.get("ret_10d") is not None]
        avg_10d = (sum(avg_10d_vals) / len(avg_10d_vals)) if avg_10d_vals else None
        wins = sum(1 for r in filtered if r["ret_5d"] > 0)
        wr = wins / len(filtered) * 100
        if avg_5d > 0.2 or (avg_5d > 0 and wr >= 55):
            ret_str = f"{avg_5d:+.1f}%/5d"
            if avg_10d is not None:
                ret_str += f", {avg_10d:+.1f}%/10d"
            pattern = f"{label} -> avg {ret_str} ({wr:.0f}% win, {len(filtered)} samples)"
            discoveries.append(pattern)
            save_insight(db, user_id, pattern, confidence=min(0.9, wr / 100))

    logger.info(f"[mine_patterns] Mining from {len(all_rows)} historical data points")

    # ── 1. RSI patterns ──
    _check([r for r in all_rows if r["rsi"] < 30], "RSI oversold (<30)")
    _check([r for r in all_rows if r["rsi"] > 70], "RSI overbought (>70) — sell signal")
    _check([r for r in all_rows if 30 <= r["rsi"] < 40], "RSI near-oversold (30-40)")

    # ── 2. MACD patterns ──
    _check([r for r in all_rows if r["macd"] > r["macd_sig"]], "MACD bullish crossover")
    _check([r for r in all_rows if r["macd_hist"] > 0 and r["macd"] < 0],
           "MACD histogram positive while MACD negative (early reversal)")

    # ── 3. Bollinger Band patterns ──
    _check([r for r in all_rows if r["bb_pct"] < 0.1],
           "Price below lower Bollinger Band (<10%)")
    _check([r for r in all_rows if r["bb_pct"] > 0.9],
           "Price above upper Bollinger Band (>90%) — sell signal")

    # ── 4. ADX trend strength ──
    _check([r for r in all_rows if r["adx"] > 30 and r["rsi"] < 40],
           "Strong trend (ADX>30) + RSI<40 (trending oversold)")
    _check([r for r in all_rows if r["adx"] < 15],
           "No trend (ADX<15) — range-bound, mean reversion expected")

    # ── 5. EMA stacking ──
    _check([r for r in all_rows if r["ema_stack"]],
           "EMA stacking bullish (Price > EMA20 > EMA50 > EMA100)")

    # ── 6. Multi-indicator confluence ──
    _check([r for r in all_rows
            if r["rsi"] < 35 and r["macd"] > r["macd_sig"] and r["bb_pct"] < 0.2],
           "Triple confluence: RSI<35 + MACD bullish + near lower BB")
    _check([r for r in all_rows
            if r["rsi"] > 55 and r["adx"] > 25 and r["macd"] > r["macd_sig"]],
           "Momentum confluence: RSI>55 + ADX>25 + MACD bullish (trend continuation)")

    # ── 7. ATR volatility patterns ──
    atr_vals = [r["atr"] for r in all_rows if r["atr"] > 0]
    if atr_vals:
        atr_median = sorted(atr_vals)[len(atr_vals) // 2]
        _check([r for r in all_rows if r["atr"] > atr_median * 1.5 and r["rsi"] < 35],
               "High volatility + oversold RSI (capitulation bounce)")
        _check([r for r in all_rows if 0 < r["atr"] < atr_median * 0.5],
               "Low volatility squeeze — breakout expected")

    # ── 8. Crypto-specific patterns ──
    crypto = [r for r in all_rows if r["is_crypto"]]
    if crypto:
        _check([r for r in crypto if r["rsi"] < 25],
               "Crypto deep oversold (RSI<25)")
        _check([r for r in crypto if r["rsi"] < 35 and r["macd_hist"] > 0],
               "Crypto RSI<35 + MACD histogram positive — reversal")

    # ── 9. SMA/EMA trend patterns ──
    _check([r for r in all_rows if r["above_sma20"] and r["rsi"] > 50 and r["adx"] > 20],
           "Above SMA20 + RSI>50 + ADX>20 (healthy uptrend)")

    # ── 10. Stochastic patterns ──
    _check([r for r in all_rows if r["stoch_k"] < 20],
           "Stochastic oversold (K<20)")

    # ── 11. BB squeeze + trend ──
    _check([r for r in all_rows if r["bb_pct"] < 0.15 and r["macd_hist"] > 0],
           "Lower BB + MACD turning positive (bounce setup)")
    _check([r for r in all_rows if r["above_sma20"] and r["ema_stack"] and r["adx"] > 20],
           "Full alignment: EMA stack + above SMA20 + ADX>20 (strong trend)")

    # ── 12. Demote patterns that stopped working ──
    existing = get_insights(db, user_id, limit=50)
    for ins in existing:
        if ins.evidence_count >= 5 and ins.confidence < 0.35:
            old_conf = ins.confidence
            ins.active = False
            db.commit()
            log_learning_event(
                db, user_id, "demotion",
                f"Pattern demoted (low confidence {old_conf:.0%}): {ins.pattern_description[:100]}",
                confidence_before=old_conf, confidence_after=0,
                related_insight_id=ins.id,
            )

    logger.info(f"[mine_patterns] Discovered {len(discoveries)} patterns from {len(all_rows)} data points")
    return discoveries


def deep_study(db: Session, user_id: int | None) -> dict[str, Any]:
    """Intensive AI-powered learning: mine patterns then ask LLM to reflect on findings."""
    # Run pattern mining first
    discoveries = mine_patterns(db, user_id)

    # Gather all current insights
    insights = get_insights(db, user_id, limit=30)
    insight_lines = []
    for ins in insights:
        insight_lines.append(
            f"- [{ins.confidence:.0%} conf, {ins.evidence_count} evidence] {ins.pattern_description}"
        )
    insight_text = "\n".join(insight_lines) if insight_lines else "No patterns learned yet."

    # Gather recent scan stats
    from ..models.trading import MarketSnapshot, ScanResult
    snap_count = db.query(MarketSnapshot).count()
    filled_count = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
    ).count()

    recent_scans = db.query(ScanResult).order_by(
        ScanResult.scanned_at.desc()
    ).limit(20).all()
    scan_summary = []
    buy_count = sum(1 for s in recent_scans if s.signal == "buy")
    sell_count = sum(1 for s in recent_scans if s.signal == "sell")
    hold_count = sum(1 for s in recent_scans if s.signal == "hold")

    stats = get_trade_stats(db, user_id)

    # Ask the LLM to reflect on what it has learned
    reflection_prompt = f"""You are an AI trading brain doing a self-reflection on what you've learned.

## YOUR LEARNED PATTERNS
{insight_text}

## DATA STATS
- Total market snapshots: {snap_count}
- Snapshots with verified outcomes: {filled_count}
- New patterns discovered this session: {len(discoveries)}
- Recent scan: {buy_count} buys, {sell_count} sells, {hold_count} holds

## USER'S TRADING PERFORMANCE
- Total trades: {stats.get('total_trades', 0)}
- Win rate: {stats.get('win_rate', 0)}%
- Total P&L: ${stats.get('total_pnl', 0)}

Write a LEARNING REPORT in this format:

### What I've Learned So Far
(Summarize the top 3-5 most reliable patterns you've discovered, explain each in plain English)

### What's Working
(Which patterns have the highest confidence and best returns?)

### What I'm Still Figuring Out
(What areas need more data? What hypotheses are you testing?)

### My Current Market Read
(Based on the patterns, what's the overall market telling you right now?)

### Next Study Goals
(What should I focus on studying next to get smarter?)

Keep it conversational and honest. Use actual numbers from the patterns above. If there isn't enough data yet, be transparent about that."""

    try:
        from ..prompts import load_prompt
        from .. import openai_client
        from ..logger import new_trace_id

        system_prompt = load_prompt("trading_analyst")
        trace_id = new_trace_id()
        result = openai_client.chat(
            messages=[{"role": "user", "content": reflection_prompt}],
            system_prompt=system_prompt,
            trace_id=trace_id,
            user_message=reflection_prompt,
        )
        reflection = result.get("reply", "Could not generate reflection.")
    except Exception as e:
        reflection = f"Reflection unavailable: {e}"

    log_learning_event(
        db, user_id, "review",
        f"Deep study: {len(discoveries)} new patterns, {len(insights)} total active. AI reflection generated.",
    )

    return {
        "ok": True,
        "discoveries": discoveries,
        "total_patterns": len(insights),
        "reflection": reflection,
        "stats": {
            "snapshots": snap_count,
            "verified": filled_count,
            "new_discoveries": len(discoveries),
        },
    }


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

    # 1. Use latest scan results if available (<2 hours old), otherwise do a quick scan
    from ..models.trading import ScanResult
    from sqlalchemy import or_
    from .ticker_universe import get_full_ticker_universe, get_ticker_count

    recent_cutoff = datetime.utcnow() - timedelta(hours=2)
    # Include results from both this user AND the scheduler (user_id=None)
    user_filter = or_(ScanResult.user_id == user_id, ScanResult.user_id.is_(None))

    recent_results = db.query(ScanResult).filter(
        user_filter,
        ScanResult.scanned_at >= recent_cutoff,
        ScanResult.score >= 5.5,
        ScanResult.signal == "buy",
    ).order_by(ScanResult.score.desc()).limit(50).all()

    universe_counts = get_ticker_count()
    total_scanned = universe_counts["total"]

    if recent_results:
        scored_results = [
            {
                "ticker": r.ticker, "score": r.score, "signal": r.signal,
                "price": r.entry_price, "entry_price": r.entry_price,
                "stop_loss": r.stop_loss, "take_profit": r.take_profit,
                "risk_level": r.risk_level,
                "signals": r.rationale.split("; ") if r.rationale else [],
                "indicators": json.loads(r.indicator_data) if r.indicator_data else {},
            }
            for r in recent_results
        ]
    else:
        # No recent results — do a quick scan of the full universe
        universe = get_full_ticker_universe()
        total_scanned = len(universe)
        all_scored = batch_score_tickers(universe, max_workers=_MAX_SCAN_WORKERS)
        scored_results = [s for s in all_scored if s["signal"] == "buy" and s["score"] >= 5.5]

    # Also check watchlist tickers
    watchlist = get_watchlist(db, user_id)
    existing_tickers = {s["ticker"] for s in scored_results}
    for w in watchlist:
        if w.ticker not in existing_tickers:
            scored = _score_ticker(w.ticker)
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
            "reply": f"I scanned {total_scanned:,} stocks and crypto and none have a strong enough setup right now. "
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
        f"## MARKET SCAN RESULTS — Top {len(top_picks)} candidates from {total_scanned:,} stocks & crypto scanned",
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

    # Build a concise list of the actual ticker names from top picks
    ticker_names = ", ".join(p["ticker"] for p in top_picks)

    smart_pick_addendum = f"""

SPECIAL INSTRUCTION — SMART PICK MODE:
You scanned {total_scanned:,} stocks and crypto. The TOP candidates are: {ticker_names}
Their full indicator data and scores are in the MARKET SCAN RESULTS section below.

CRITICAL RULES:
- You MUST reference tickers BY NAME (e.g. "AAPL", "BTC-USD", "NVDA") — NEVER give a generic recommendation without naming specific tickers.
- Use the ACTUAL prices and indicator values from the data provided — do NOT make up numbers.
- If the user asked about crypto specifically, prioritize crypto tickers from the scan.
- If the user asked about stocks specifically, prioritize stock tickers.

Your job: Pick the BEST 1-3 trades from this scan and present them as a clear, specific action plan.

For EACH recommended trade, format it EXACTLY like this:

## 1. TICKER — Company/Coin Name
- **Verdict**: STRONG BUY / BUY
- **Confidence**: X%
- **Current Price**: $X.XX (from the data)
- **Buy-in Price**: $X.XX (entry level)
- **Stop-Loss**: $X.XX (reason)
- **Target 1**: $X.XX (conservative)
- **Target 2**: $X.XX (optimistic)
- **Risk/Reward**: X:1
- **Hold Duration**: X days/weeks
- **Position Size**: X% of portfolio
- **Why NOW**: 2-3 bullet points using the ACTUAL indicator values
- **Exit Signal**: what would invalidate this trade

If NONE have a strong enough setup, say so clearly. End with portfolio allocation advice.
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
        "picks_scanned": total_scanned,
        "picks_qualified": len(scored_results),
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
    from .ticker_universe import get_ticker_count

    total_patterns = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id, TradingInsight.active.is_(True),
    ).count()

    avg_confidence_row = db.query(func.avg(TradingInsight.confidence)).filter(
        TradingInsight.user_id == user_id, TradingInsight.active.is_(True),
    ).scalar()
    avg_confidence = round(float(avg_confidence_row or 0) * 100, 1)

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

    universe_counts = get_ticker_count()
    scan_st = get_scan_status()
    learning_st = get_learning_status()

    return {
        "total_patterns": total_patterns,
        "avg_confidence": avg_confidence,
        "patterns_this_week": patterns_this_week,
        "total_snapshots": total_snapshots,
        "prediction_accuracy": accuracy,
        "total_predictions": total_predictions,
        "total_events": total_events,
        "universe_stocks": universe_counts["stocks"],
        "universe_crypto": universe_counts["crypto"],
        "universe_total": universe_counts["total"],
        "last_scan": scan_st.get("last_run"),
        "learning_running": learning_st.get("running", False),
    }


def get_confidence_history(db: Session, user_id: int | None) -> list[dict[str, Any]]:
    """Weekly average confidence over time for the Brain chart."""
    insights = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
    ).order_by(TradingInsight.created_at.asc()).all()

    if not insights:
        return []

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


# ── Batch Concurrent Scanner ──────────────────────────────────────────

_MAX_SCAN_WORKERS = 20

_scan_status: dict[str, Any] = {
    "running": False,
    "last_run": None,
    "last_run_duration_s": None,
    "tickers_scanned": 0,
    "tickers_scored": 0,
    "tickers_total": 0,
    "phase": "idle",
    "progress_pct": 0,
    "errors": 0,
}


def get_scan_status() -> dict[str, Any]:
    """Current state of the background scanner for UI display."""
    return dict(_scan_status)


def batch_score_tickers(
    tickers: list[str],
    max_workers: int = _MAX_SCAN_WORKERS,
    progress_callback: Any = None,
) -> list[dict[str, Any]]:
    """Score many tickers concurrently using a thread pool."""
    results: list[dict[str, Any]] = []
    total = len(tickers)
    completed = 0
    errors = 0

    def _score_one(ticker: str) -> dict[str, Any] | None:
        try:
            return _score_ticker(ticker)
        except Exception:
            return None

    if _shutting_down.is_set():
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticker = {}
        for t in tickers:
            if _shutting_down.is_set():
                break
            future_to_ticker[executor.submit(_score_one, t)] = t

        for future in as_completed(future_to_ticker):
            if _shutting_down.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            completed += 1
            ticker = future_to_ticker[future]
            try:
                scored = future.result()
                if scored is not None:
                    results.append(scored)
            except Exception:
                errors += 1

            if progress_callback and completed % 50 == 0:
                progress_callback(completed, total, errors)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def run_full_market_scan(
    db: Session,
    user_id: int | None,
    use_full_universe: bool = True,
) -> list[dict[str, Any]]:
    """Scan the entire ticker universe concurrently, store results, return sorted."""
    from ..models.trading import ScanResult
    from .ticker_universe import get_full_ticker_universe

    _scan_status["running"] = True
    _scan_status["phase"] = "scanning"
    _scan_status["errors"] = 0

    if use_full_universe:
        scan_list = get_full_ticker_universe()
    else:
        scan_list = list(ALL_SCAN_TICKERS)

    # Also include watchlist tickers
    watchlist = get_watchlist(db, user_id)
    wl_tickers = {w.ticker for w in watchlist}
    for t in wl_tickers:
        if t not in scan_list:
            scan_list.append(t)

    _scan_status["tickers_total"] = len(scan_list)
    _scan_status["tickers_scanned"] = 0
    _scan_status["tickers_scored"] = 0

    start = time.time()

    def _progress(done: int, total: int, errs: int):
        _scan_status["tickers_scanned"] = done
        _scan_status["errors"] = errs
        _scan_status["progress_pct"] = round(done / total * 100) if total else 0

    logger.info(f"[trading] Full market scan starting: {len(scan_list)} tickers")

    results = batch_score_tickers(scan_list, progress_callback=_progress)

    _scan_status["tickers_scanned"] = len(scan_list)
    _scan_status["tickers_scored"] = len(results)
    _scan_status["progress_pct"] = 100
    _scan_status["phase"] = "storing"

    # Store results in DB (clear old scan results first to save space)
    old_cutoff = datetime.utcnow() - timedelta(days=7)
    db.query(ScanResult).filter(
        ScanResult.user_id == user_id,
        ScanResult.scanned_at < old_cutoff,
    ).delete(synchronize_session=False)

    for scored in results:
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

    db.commit()

    elapsed = time.time() - start
    _scan_status["phase"] = "idle"
    _scan_status["running"] = False
    _scan_status["last_run"] = datetime.utcnow().isoformat()
    _scan_status["last_run_duration_s"] = round(elapsed, 1)

    logger.info(
        f"[trading] Full scan complete: {len(results)}/{len(scan_list)} scored "
        f"in {elapsed:.0f}s"
    )
    return results


# ── Learning Cycle Orchestrator ────────────────────────────────────────

_learning_status: dict[str, Any] = {
    "running": False,
    "last_run": None,
    "last_duration_s": None,
    "phase": "idle",
    "current_step": "",
    "steps_completed": 0,
    "total_steps": 7,
}


def get_learning_status() -> dict[str, Any]:
    return dict(_learning_status)


def run_learning_cycle(
    db: Session,
    user_id: int | None,
    full_universe: bool = True,
) -> dict[str, Any]:
    """Complete learning cycle: scan → snapshot → backfill → mine → journal → signals.

    This is meant to be called from a background task or scheduler.
    """
    if _learning_status["running"]:
        return {"ok": False, "reason": "Learning cycle already in progress"}
    if _shutting_down.is_set():
        return {"ok": False, "reason": "Server is shutting down"}

    _learning_status["running"] = True
    _learning_status["phase"] = "starting"
    _learning_status["steps_completed"] = 0
    start = time.time()
    report: dict[str, Any] = {}

    try:
        # Step 1: Full market scan
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        _learning_status["current_step"] = "Full market scan"
        _learning_status["phase"] = "scanning"
        scan_results = run_full_market_scan(db, user_id, use_full_universe=full_universe)
        report["tickers_scanned"] = _scan_status["tickers_total"]
        report["tickers_scored"] = len(scan_results)
        _learning_status["steps_completed"] = 1

        # Step 2: Take market snapshots for top scorers + watchlist
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        _learning_status["current_step"] = "Taking market snapshots"
        _learning_status["phase"] = "snapshots"
        top_tickers = [r["ticker"] for r in scan_results[:200]]
        watchlist = get_watchlist(db, user_id)
        for w in watchlist:
            if w.ticker not in top_tickers:
                top_tickers.append(w.ticker)
        snap_count = 0
        for ticker in top_tickers:
            if _shutting_down.is_set():
                break
            take_market_snapshot(db, ticker)
            snap_count += 1
        report["snapshots_taken"] = snap_count
        _learning_status["steps_completed"] = 2

        # Step 3: Backfill future returns
        _learning_status["current_step"] = "Backfilling future returns"
        _learning_status["phase"] = "backfilling"
        filled = backfill_future_returns(db)
        report["returns_backfilled"] = filled
        _learning_status["steps_completed"] = 3

        # Step 4: Mine patterns from historical data
        _learning_status["current_step"] = "Mining patterns"
        _learning_status["phase"] = "mining"
        discoveries = mine_patterns(db, user_id)
        report["patterns_discovered"] = len(discoveries)
        _learning_status["steps_completed"] = 4

        # Step 5: Daily market journal
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        _learning_status["current_step"] = "Writing market journal"
        _learning_status["phase"] = "journaling"
        journal = daily_market_journal(db, user_id)
        report["journal_written"] = journal is not None
        _learning_status["steps_completed"] = 5

        # Step 6: Check signal events on watchlist
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        _learning_status["current_step"] = "Checking signal events"
        _learning_status["phase"] = "signals"
        events = check_signal_events(db, user_id)
        report["signal_events"] = len(events)
        _learning_status["steps_completed"] = 6

        # Step 7: Log learning cycle completion
        _learning_status["current_step"] = "Finalizing"
        _learning_status["phase"] = "finalizing"
        elapsed = time.time() - start
        log_learning_event(
            db, user_id, "scan",
            f"Full learning cycle: scanned {report['tickers_scanned']} tickers, "
            f"scored {report['tickers_scored']}, {report['snapshots_taken']} snapshots, "
            f"{report['patterns_discovered']} patterns discovered, "
            f"{report['signal_events']} signal events — {elapsed:.0f}s",
        )
        _learning_status["steps_completed"] = 7

    except InterruptedError:
        logger.info("[trading] Learning cycle interrupted by shutdown")
        report["interrupted"] = True
    except Exception as e:
        logger.error(f"[trading] Learning cycle error: {e}")
        report["error"] = str(e)
        try:
            log_learning_event(db, user_id, "error", f"Learning cycle failed: {e}")
        except Exception:
            pass
    finally:
        elapsed = time.time() - start
        _learning_status["running"] = False
        _learning_status["phase"] = "idle"
        _learning_status["current_step"] = ""
        _learning_status["last_run"] = datetime.utcnow().isoformat()
        _learning_status["last_duration_s"] = round(elapsed, 1)
        report["elapsed_s"] = round(elapsed, 1)

    logger.info(f"[trading] Learning cycle finished in {elapsed:.0f}s: {report}")
    return {"ok": True, **report}


def should_run_learning() -> bool:
    """Check if enough time has passed since last learning cycle to justify a new one."""
    if _learning_status["running"]:
        return False
    last = _learning_status.get("last_run")
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return datetime.utcnow() - last_dt > timedelta(hours=1)
    except Exception:
        return True
