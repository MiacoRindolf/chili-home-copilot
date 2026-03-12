"""Learning: pattern mining, deep study, learning cycles, brain stats."""
from __future__ import annotations

import json
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import func

from ...models.trading import (
    LearningEvent, MarketSnapshot, ScanResult, TradingInsight, Trade,
)
from ..yf_session import get_history as _yf_history
from .market_data import (
    fetch_quote, fetch_quotes_batch, get_indicator_snapshot, get_vix,
    get_volatility_regime, DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights, save_insight, get_trade_stats_by_pattern

logger = logging.getLogger(__name__)

_shutting_down = threading.Event()

# Stale-while-revalidate cache for get_current_predictions
_pred_cache: dict[str, Any] = {"results": [], "ts": 0.0}
_PRED_CACHE_TTL = 180       # 3 min fresh
_PRED_CACHE_STALE_TTL = 600  # 10 min stale-while-revalidate
_pred_refreshing = False
_pred_refresh_lock = threading.Lock()


def signal_shutdown():
    _shutting_down.set()


# ── Learning Event Logger ─────────────────────────────────────────────

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


# ── AI Self-Learning ──────────────────────────────────────────────────

def analyze_closed_trade(db: Session, trade: Trade) -> str | None:
    """Called after a trade is closed. Asks the AI to review and extract patterns."""
    from ...prompts import load_prompt
    from ... import openai_client
    from ...logger import log_info, new_trace_id
    from .journal import add_journal_entry

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
            max_tokens=2048,
        )
        reply = result.get("reply", "")
    except Exception as e:
        log_info(trace_id, f"[trading] post-trade analysis error: {e}")
        return None

    _extract_and_store_patterns(db, trade.user_id, reply, existing_insights)

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


# ── Market Snapshots ──────────────────────────────────────────────────

def _fetch_news_sentiment(ticker: str) -> tuple[float | None, int | None]:
    """Fetch news for a ticker and return (avg_sentiment, news_count)."""
    try:
        from .sentiment import aggregate_sentiment
        from ..yf_session import get_ticker_news
        news = get_ticker_news(ticker, limit=10)
        titles = [n.get("title", "") for n in news if n.get("title")]
        if not titles:
            return None, 0
        agg = aggregate_sentiment(titles)
        return agg["avg_score"], agg["count"]
    except Exception:
        return None, None


def _fetch_fundamentals(ticker: str) -> tuple[float | None, float | None]:
    """Return (pe_ratio, market_cap_billions) for a ticker."""
    try:
        quote = fetch_quote(ticker)
        if not quote:
            return None, None
        pe = quote.get("pe") or quote.get("trailingPE")
        mcap = quote.get("marketCap") or quote.get("market_cap")
        pe_f = float(pe) if pe else None
        mcap_b = float(mcap) / 1e9 if mcap else None
        return pe_f, mcap_b
    except Exception:
        return None, None


def take_market_snapshot(db: Session, ticker: str) -> None:
    try:
        snap = get_indicator_snapshot(ticker, "1d")
        quote = fetch_quote(ticker)
        price = quote.get("price", 0) if quote else 0
        ind_data = {k: v for k, v in snap.items() if k not in ("ticker", "interval")}
        pred_score = compute_prediction(ind_data) if ind_data else None
        vix = get_vix()
        sent_score, sent_count = _fetch_news_sentiment(ticker)
        pe_ratio, mcap_b = _fetch_fundamentals(ticker)

        record = MarketSnapshot(
            ticker=ticker.upper(),
            snapshot_date=datetime.utcnow(),
            close_price=price,
            indicator_data=json.dumps(snap),
            predicted_score=pred_score,
            vix_at_snapshot=vix,
            news_sentiment=sent_score,
            news_count=sent_count,
            pe_ratio=pe_ratio,
            market_cap_b=mcap_b,
        )
        db.add(record)
        db.commit()
    except Exception:
        pass


def _snapshot_data(ticker: str) -> tuple[str, dict | None, dict | None, float | None, int | None, float | None, float | None]:
    """Fetch snapshot data in a thread (no DB access).

    Uses the prewarmed OHLCV cache for the price instead of calling
    fetch_quote (which hits get_fast_info with a 30s TTL and triggers
    individual API requests when the TTL expires during a long batch).
    Returns (ticker, snap, quote, news_sentiment, news_count, pe_ratio, market_cap_b).
    """
    try:
        snap = get_indicator_snapshot(ticker, "1d")
        from ..yf_session import get_history as _yf_hist
        df = _yf_hist(ticker, period="3mo", interval="1d")
        price = float(df.iloc[-1]["Close"]) if df is not None and not df.empty else 0
        quote = {"price": price}
        sent_score, sent_count = _fetch_news_sentiment(ticker)
        pe, mcap = _fetch_fundamentals(ticker)
        return ticker, snap, quote, sent_score, sent_count, pe, mcap
    except Exception:
        return ticker, None, None, None, None, None, None


def take_snapshots_parallel(
    db: Session,
    tickers: list[str],
    max_workers: int = 16,
) -> int:
    """Take snapshots for many tickers using a thread pool.

    Data fetching runs in parallel; DB writes happen sequentially on the
    calling thread to avoid SQLAlchemy session issues.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..yf_session import batch_download

    # Pre-warm the OHLCV cache so indicator computation hits cache.
    # batch_download also seeds the quote: cache from the last OHLCV row,
    # eliminating the need for per-ticker get_fast_info calls.
    BATCH = 50
    for i in range(0, len(tickers), BATCH):
        try:
            batch_download(tickers[i:i + BATCH], period="3mo", interval="1d")
        except Exception:
            pass

    fetched: list[tuple[str, dict | None, dict | None]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_snapshot_data, t): t for t in tickers}
        for future in as_completed(futures):
            if _shutting_down.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                fetched.append(future.result())
            except Exception:
                pass

    vix = get_vix()
    count = 0
    for row in fetched:
        ticker, snap, quote = row[0], row[1], row[2]
        sent_score = row[3] if len(row) > 3 else None
        sent_count = row[4] if len(row) > 4 else None
        pe = row[5] if len(row) > 5 else None
        mcap = row[6] if len(row) > 6 else None
        if snap is None:
            continue
        try:
            price = quote.get("price", 0) if quote else 0
            ind_data = {k: v for k, v in snap.items() if k not in ("ticker", "interval")}
            pred_score = compute_prediction(ind_data) if ind_data else None
            record = MarketSnapshot(
                ticker=ticker.upper(),
                snapshot_date=datetime.utcnow(),
                close_price=price,
                indicator_data=json.dumps(snap),
                predicted_score=pred_score,
                vix_at_snapshot=vix,
                news_sentiment=sent_score,
                news_count=sent_count,
                pe_ratio=pe,
                market_cap_b=mcap,
            )
            db.add(record)
            count += 1
        except Exception:
            pass
    if count:
        db.commit()
    return count


def take_all_snapshots(db: Session, user_id: int | None, ticker_list: list[str] | None = None) -> int:
    if ticker_list:
        tickers = list(set(ticker_list))
    else:
        tickers = list(set(DEFAULT_SCAN_TICKERS[:20] + DEFAULT_CRYPTO_TICKERS[:10]))

    watchlist = get_watchlist(db, user_id)
    for w in watchlist:
        if w.ticker not in tickers:
            tickers.append(w.ticker)

    return take_snapshots_parallel(db, tickers)


def backfill_future_returns(db: Session) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    unfilled = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.is_(None),
    ).limit(3000).all()

    if not unfilled:
        return 0

    tickers = list({s.ticker for s in unfilled})
    from ..yf_session import batch_download as _bd
    BATCH = 50
    for i in range(0, len(tickers), BATCH):
        try:
            _bd(tickers[i:i + BATCH], period="1mo", interval="1d")
        except Exception:
            pass

    def _fetch_returns(snap):
        try:
            df = _yf_history(snap.ticker, start=snap.snapshot_date, period="15d", interval="1d")
            if len(df) < 2:
                return None
            base_price = snap.close_price
            if base_price <= 0:
                return None
            def _ret(idx):
                return round((float(df["Close"].iloc[idx]) - base_price) / base_price * 100, 2)
            r1 = _ret(1) if len(df) >= 2 else None
            r3 = _ret(3) if len(df) >= 4 else None
            r5 = _ret(5) if len(df) >= 6 else None
            r10 = _ret(10) if len(df) >= 11 else None
            return (snap.id, r1, r3, r5, r10)
        except Exception:
            return None

    results = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(_fetch_returns, s): s for s in unfilled}
        for f in as_completed(futures):
            if _shutting_down.is_set():
                break
            r = f.result()
            if r:
                results.append(r)

    updated = 0
    snap_map = {s.id: s for s in unfilled}
    for snap_id, r1, r3, r5, r10 in results:
        snap = snap_map.get(snap_id)
        if not snap:
            continue
        if r1 is not None:
            snap.future_return_1d = r1
        if r3 is not None:
            snap.future_return_3d = r3
        if r5 is not None:
            snap.future_return_5d = r5
        if r10 is not None:
            snap.future_return_10d = r10
        updated += 1

    if updated:
        db.commit()
    return updated


# ── Pattern Mining ────────────────────────────────────────────────────

def _mine_from_history(ticker: str) -> list[dict]:
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange

    try:
        # Use 6mo to hit the cache populated by the scan phase
        df = _yf_history(ticker, period="6mo", interval="1d")
        if df.empty or len(df) < 60:
            return []
    except Exception:
        return []

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

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
    vol_sma = volume.rolling(20).mean()

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

        vol_ratio = (float(volume.iloc[i]) / float(vol_sma.iloc[i])
                     if pd.notna(vol_sma.iloc[i]) and float(vol_sma.iloc[i]) > 0 else 1.0)

        prev_close = float(close.iloc[i - 1]) if i > 0 else price
        gap_pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0

        # Stochastic divergence detection (last 5 bars)
        stoch_bull_div = False
        stoch_bear_div = False
        if i >= 5:
            prices_5 = [float(close.iloc[j]) for j in range(i - 4, i + 1)]
            stochs_5 = [float(stoch_k.iloc[j]) if pd.notna(stoch_k.iloc[j]) else 50 for j in range(i - 4, i + 1)]
            if prices_5[-1] < min(prices_5[:-1]) and stochs_5[-1] > min(stochs_5[:-1]):
                stoch_bull_div = True
            if prices_5[-1] > max(prices_5[:-1]) and stochs_5[-1] < max(stochs_5[:-1]):
                stoch_bear_div = True

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
            "stoch_bull_div": stoch_bull_div,
            "stoch_bear_div": stoch_bear_div,
            "above_sma20": price > float(sma20.iloc[i]) if pd.notna(sma20.iloc[i]) else False,
            "ema_stack": (e20 is not None and e50 is not None and e100 is not None
                          and price > e20 > e50 > e100),
            "is_crypto": ticker.endswith("-USD"),
            "vol_ratio": round(vol_ratio, 2),
            "gap_pct": round(gap_pct, 2),
        })
    return rows


def mine_patterns(db: Session, user_id: int | None) -> list[str]:
    """Discover patterns from historical price data + existing snapshots."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .market_data import ALL_SCAN_TICKERS as _ALL_TICKERS

    mine_tickers = list(_ALL_TICKERS)

    watchlist = get_watchlist(db, user_id)
    for w in watchlist:
        if w.ticker not in mine_tickers:
            mine_tickers.append(w.ticker)

    try:
        from .prescreener import get_trending_crypto
        for t in get_trending_crypto():
            if t not in mine_tickers:
                mine_tickers.append(t)
    except Exception:
        pass

    mine_tickers = mine_tickers[:500]

    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {}
        for ticker in mine_tickers:
            if _shutting_down.is_set():
                break
            futures[executor.submit(_mine_from_history, ticker)] = ticker

        for future in as_completed(futures):
            if _shutting_down.is_set():
                break
            try:
                rows = future.result()
                all_rows.extend(rows)
            except Exception:
                continue

    snapshots = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
    ).order_by(MarketSnapshot.snapshot_date.desc()).limit(5000).all()

    for s in snapshots:
        try:
            data = json.loads(s.indicator_data) if s.indicator_data else {}
            rsi_data = data.get("rsi", {})
            macd_data = data.get("macd", {})
            bb_data = data.get("bbands", {})
            adx_data = data.get("adx", {})
            stoch_data = data.get("stoch", {})
            sma20_data = data.get("sma_20", {})
            ema20_data = data.get("ema_20", {})
            ema50_data = data.get("ema_50", {})
            ema100_data = data.get("ema_100", {})

            bb_range = ((bb_data.get("upper", 0) or 0) - (bb_data.get("lower", 0) or 0))
            bb_pct = ((s.close_price - (bb_data.get("lower", 0) or 0)) / bb_range
                      if bb_range > 0 and s.close_price else 0.5)

            e20 = ema20_data.get("value") if ema20_data else None
            e50 = ema50_data.get("value") if ema50_data else None
            e100 = ema100_data.get("value") if ema100_data else None
            price = s.close_price or 0
            ema_stack = (e20 is not None and e50 is not None and e100 is not None
                         and price > e20 > e50 > e100)

            all_rows.append({
                "ticker": s.ticker,
                "price": price,
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
                "above_sma20": (price > (sma20_data.get("value", 0) or 0)
                                if sma20_data and price else False),
                "ema_stack": ema_stack,
                "is_crypto": s.ticker.endswith("-USD"),
                "news_sentiment": getattr(s, "news_sentiment", None),
                "news_count": getattr(s, "news_count", None) or 0,
                "pe_ratio": getattr(s, "pe_ratio", None),
                "market_cap_b": getattr(s, "market_cap_b", None),
            })
        except Exception:
            continue

    if len(all_rows) < 10:
        return []

    vol_regime = get_volatility_regime()
    regime_tag = f" [{vol_regime['label']}]" if vol_regime.get("regime") != "unknown" else ""

    discoveries: list[str] = []
    MIN_SAMPLES = 3

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
            pattern = f"{label} -> avg {ret_str} ({wr:.0f}% win, {len(filtered)} samples){regime_tag}"
            discoveries.append(pattern)
            save_insight(db, user_id, pattern, confidence=min(0.9, wr / 100))

    logger.info(f"[mine_patterns] Mining from {len(all_rows)} historical data points")

    _check([r for r in all_rows if r["rsi"] < 30], "RSI oversold (<30)")
    _check([r for r in all_rows if r["rsi"] > 70], "RSI overbought (>70) — sell signal")
    _check([r for r in all_rows if 30 <= r["rsi"] < 40], "RSI near-oversold (30-40)")
    _check([r for r in all_rows if r["macd"] > r["macd_sig"]], "MACD bullish crossover")
    _check([r for r in all_rows if r["macd_hist"] > 0 and r["macd"] < 0],
           "MACD histogram positive while MACD negative (early reversal)")
    _check([r for r in all_rows if r["bb_pct"] < 0.1],
           "Price below lower Bollinger Band (<10%)")
    _check([r for r in all_rows if r["bb_pct"] > 0.9],
           "Price above upper Bollinger Band (>90%) — sell signal")
    _check([r for r in all_rows if r["adx"] > 30 and r["rsi"] < 40],
           "Strong trend (ADX>30) + RSI<40 (trending oversold)")
    _check([r for r in all_rows if r["adx"] < 15],
           "No trend (ADX<15) — range-bound, mean reversion expected")
    _check([r for r in all_rows if r["ema_stack"]],
           "EMA stacking bullish (Price > EMA20 > EMA50 > EMA100)")
    _check([r for r in all_rows
            if r["rsi"] < 35 and r["macd"] > r["macd_sig"] and r["bb_pct"] < 0.2],
           "Triple confluence: RSI<35 + MACD bullish + near lower BB")
    _check([r for r in all_rows
            if r["rsi"] > 55 and r["adx"] > 25 and r["macd"] > r["macd_sig"]],
           "Momentum confluence: RSI>55 + ADX>25 + MACD bullish (trend continuation)")

    atr_vals = [r["atr"] for r in all_rows if r["atr"] > 0]
    if atr_vals:
        atr_median = sorted(atr_vals)[len(atr_vals) // 2]
        _check([r for r in all_rows if r["atr"] > atr_median * 1.5 and r["rsi"] < 35],
               "High volatility + oversold RSI (capitulation bounce)")
        _check([r for r in all_rows if 0 < r["atr"] < atr_median * 0.5],
               "Low volatility squeeze — breakout expected")

    crypto = [r for r in all_rows if r["is_crypto"]]
    if crypto:
        _check([r for r in crypto if r["rsi"] < 25],
               "Crypto deep oversold (RSI<25)")
        _check([r for r in crypto if r["rsi"] < 35 and r["macd_hist"] > 0],
               "Crypto RSI<35 + MACD histogram positive — reversal")

    _check([r for r in all_rows if r["above_sma20"] and r["rsi"] > 50 and r["adx"] > 20],
           "Above SMA20 + RSI>50 + ADX>20 (healthy uptrend)")
    _check([r for r in all_rows if r["stoch_k"] < 20],
           "Stochastic oversold (K<20)")
    _check([r for r in all_rows if r["bb_pct"] < 0.15 and r["macd_hist"] > 0],
           "Lower BB + MACD turning positive (bounce setup)")
    _check([r for r in all_rows if r["above_sma20"] and r["ema_stack"] and r["adx"] > 20],
           "Full alignment: EMA stack + above SMA20 + ADX>20 (strong trend)")

    # Stochastic + MACD confluence
    _check([r for r in all_rows if r["stoch_k"] < 20 and r["macd_hist"] > 0],
           "Stochastic oversold + MACD turning positive (double bottom signal)")
    _check([r for r in all_rows if r["stoch_k"] > 80 and r["macd_hist"] < 0],
           "Stochastic overbought + MACD turning negative — sell signal")

    # EMA stack with RSI confirmation
    _check([r for r in all_rows if r["ema_stack"] and 40 <= r["rsi"] <= 60],
           "EMA stack + RSI neutral zone (healthy trend, not overextended)")

    # Extreme RSI with trend
    _check([r for r in all_rows if r["rsi"] < 25 and r["adx"] > 20],
           "Deep oversold RSI<25 in trending market (sharp reversal setup)")

    # Consolidation breakout
    _check([r for r in all_rows if r["bb_pct"] > 0.5 and r["bb_pct"] < 0.7
            and r["adx"] < 20 and r["macd_hist"] > 0],
           "Mid-BB range + low ADX + MACD positive (consolidation breakout)")

    # Bearish divergence patterns
    _check([r for r in all_rows if r["rsi"] > 60 and r["macd_hist"] < 0 and r["adx"] > 25],
           "RSI>60 but MACD negative + strong trend — bearish divergence sell signal")

    # Volume spike patterns
    vol_rows = [r for r in all_rows if r.get("vol_ratio") is not None]
    if vol_rows:
        _check([r for r in vol_rows if r["vol_ratio"] > 2.0 and r["rsi"] < 40],
               "Volume spike 2x+ with RSI<40 (capitulation / accumulation)")
        _check([r for r in vol_rows if r["vol_ratio"] > 2.0 and r["ema_stack"]],
               "Volume spike 2x+ with EMA stack (breakout confirmation)")
        _check([r for r in vol_rows if r["vol_ratio"] > 1.5 and r["macd_hist"] > 0
                and r["rsi"] > 50],
               "Volume surge + MACD positive + RSI>50 (momentum ignition)")

    # Gap patterns
    gap_rows = [r for r in all_rows if r.get("gap_pct") is not None]
    if gap_rows:
        _check([r for r in gap_rows if r["gap_pct"] > 2.0 and r["rsi"] < 70],
               "Gap up >2% with RSI not overbought (momentum gap)")
        _check([r for r in gap_rows if r["gap_pct"] < -2.0 and r["rsi"] < 30],
               "Gap down >2% into oversold RSI (gap-fill reversal)")

    # ── Momentum pullback patterns (inspired by day-trade best practices) ──

    # MACD positive + high relative volume + pullback = bread-and-butter entry
    if vol_rows:
        _check([r for r in vol_rows if r["vol_ratio"] > 5.0
                and r["macd"] > r["macd_sig"] and r["macd_hist"] > 0
                and r["rsi"] < 65],
               "MACD positive + volume surge 5x+ (momentum pullback setup)")

    # Topping tail warning (upper wick dominance on high volume)
    _check([r for r in all_rows
            if r["rsi"] > 60 and r.get("vol_ratio") is not None
            and r["vol_ratio"] > 2.0 and r["macd_hist"] < 0],
           "High RSI + volume spike + MACD turning negative (topping/reversal warning)")

    # MACD flipped negative after extended run = setup invalidated
    _check([r for r in all_rows
            if r["macd"] < r["macd_sig"] and r["macd_hist"] < 0
            and r["rsi"] > 40 and r["adx"] > 20],
           "MACD flipped negative in active trend — setup invalidated (avoid entry)")

    # Low float + strong gapper + MACD confirmation
    if gap_rows and vol_rows:
        _check([r for r in gap_rows
                if r["gap_pct"] > 10.0 and r["macd_hist"] > 0
                and r.get("vol_ratio") is not None and r["vol_ratio"] > 3.0],
               "10%+ gapper + MACD positive + high volume (high-conviction momentum)")

    # First pullback with clean volume profile
    _check([r for r in all_rows
            if r["rsi"] > 45 and r["rsi"] < 65
            and r["macd"] > r["macd_sig"] and r["macd_hist"] > 0
            and r["ema_stack"] and r.get("vol_ratio") is not None
            and r["vol_ratio"] > 1.5],
           "First pullback: MACD+, EMA stack, rising volume (bread-and-butter entry)")

    # Extended pullback (7+ candles = dead setup) — captured as sell signal
    _check([r for r in all_rows
            if r["rsi"] < 35 and r["macd_hist"] < 0
            and r["adx"] > 15 and not r["ema_stack"]],
           "Extended pullback with MACD negative + broken EMA stack — setup dead")

    # ── Stochastic divergence patterns ──
    _check([r for r in all_rows if r.get("stoch_bull_div")],
           "Stochastic bullish divergence (price lower low, stoch higher low)")
    _check([r for r in all_rows if r.get("stoch_bear_div")],
           "Stochastic bearish divergence (price higher high, stoch lower high) — sell signal")
    _check([r for r in all_rows if r.get("stoch_bull_div") and r["macd_hist"] > 0],
           "Stoch bullish divergence + MACD turning positive (reversal confirmation)")
    _check([r for r in all_rows if r.get("stoch_bear_div") and r["macd_hist"] < 0],
           "Stoch bearish divergence + MACD turning negative (top confirmation)")

    # ── Multi-indicator confluence patterns ──
    _check([r for r in all_rows
            if r["rsi"] < 35 and r["stoch_k"] < 25 and r["bb_pct"] < 0.15],
           "Triple oversold confluence: RSI<35 + Stoch<25 + BB<0.15")
    _check([r for r in all_rows
            if r["adx"] > 30 and r["stoch_k"] < 20 and r["ema_stack"]],
           "Trend pullback to oversold: ADX>30 + Stoch<20 + EMA stack")
    _check([r for r in all_rows
            if r.get("stoch_bull_div") and r["rsi"] < 40 and r["bb_pct"] < 0.25],
           "Multi-signal reversal: stoch bull divergence + RSI<40 + near lower BB")

    # ── News sentiment + technical confluence patterns ──
    sent_rows = [r for r in all_rows if r.get("news_sentiment") is not None]
    if len(sent_rows) >= 5:
        _check([r for r in sent_rows if r["news_sentiment"] > 0.15 and r["rsi"] < 35],
               "Bullish news + RSI oversold (<35) — contrarian catalyst")
        _check([r for r in sent_rows if r["news_sentiment"] < -0.15 and r["rsi"] > 70],
               "Bearish news + RSI overbought (>70) — sell signal confluence")
        _check([r for r in sent_rows if r["news_sentiment"] > 0.15 and r["macd_hist"] > 0
                and r["ema_stack"]],
               "Bullish news + MACD positive + EMA stack — momentum confirmation")
        _check([r for r in sent_rows if r["news_sentiment"] < -0.15 and r["macd_hist"] < 0],
               "Bearish news + MACD negative — downtrend confirmation")
        _check([r for r in sent_rows if r.get("news_count", 0) >= 5
                and r.get("vol_ratio") is not None and r["vol_ratio"] > 2],
               "High news volume (5+) + high trading volume (2x) — event-driven breakout")
        _check([r for r in sent_rows if r["news_sentiment"] > 0.2 and r["stoch_k"] < 25],
               "Strong bullish news + stochastic oversold — high-probability bounce")
        _check([r for r in sent_rows if abs(r["news_sentiment"]) < 0.05
                and r["adx"] > 30 and r["rsi"] < 40],
               "Neutral news + strong trend (ADX>30) + RSI<40 — trend pullback, no catalyst fear")

    existing = get_insights(db, user_id, limit=50)
    now = datetime.utcnow()
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
            continue

        days_since_seen = (now - (ins.last_seen or ins.created_at)).days
        if days_since_seen > 30:
            months_inactive = days_since_seen / 30
            decay = 0.95 ** months_inactive
            old_conf = ins.confidence
            ins.confidence = round(max(0.05, ins.confidence * decay), 3)
            if ins.confidence < 0.15:
                ins.active = False
                db.commit()
                log_learning_event(
                    db, user_id, "demotion",
                    f"Pattern decayed and demoted (inactive {days_since_seen}d): {ins.pattern_description[:100]}",
                    confidence_before=old_conf, confidence_after=ins.confidence,
                    related_insight_id=ins.id,
                )
            elif abs(ins.confidence - old_conf) > 0.01:
                db.commit()

    logger.info(f"[mine_patterns] Discovered {len(discoveries)} patterns from {len(all_rows)} data points")
    return discoveries


_PATTERN_CONDITION_MAP: dict[str, dict[str, Any]] = {
    "rsi oversold": {"field": "rsi", "op": "lt", "val": 30},
    "rsi overbought": {"field": "rsi", "op": "gt", "val": 70},
    "rsi near-oversold": {"field": "rsi", "op": "lt", "val": 40},
    "macd bullish": {"field": "macd", "op": "gt_field", "val": "macd_sig"},
    "macd positive": {"field": "macd_hist", "op": "gt", "val": 0},
    "macd negative": {"field": "macd_hist", "op": "lt", "val": 0},
    "ema stack": {"field": "ema_stack", "op": "eq", "val": True},
    "bollinger": {"field": "bb_pct", "op": "lt", "val": 0.15},
    "adx>25": {"field": "adx", "op": "gt", "val": 25},
    "adx>30": {"field": "adx", "op": "gt", "val": 30},
    "stoch oversold": {"field": "stoch_k", "op": "lt", "val": 20},
    "stoch overbought": {"field": "stoch_k", "op": "gt", "val": 80},
    "volume surge": {"field": "vol_ratio", "op": "gt", "val": 2.0},
    "5x": {"field": "vol_ratio", "op": "gt", "val": 5.0},
    "gap up": {"field": "gap_pct", "op": "gt", "val": 2.0},
    "pullback": {"field": "rsi", "op": "lt", "val": 55},
}


def _row_matches_condition(row: dict, cond: dict) -> bool:
    val = row.get(cond["field"])
    if val is None:
        return False
    op = cond["op"]
    target = cond["val"]
    if op == "lt":
        return val < target
    elif op == "gt":
        return val > target
    elif op == "eq":
        return val == target
    elif op == "gt_field":
        return val > (row.get(target) or 0)
    return False


def _filter_rows_by_condition(rows: list[dict], condition_str: str) -> list[dict]:
    """Parse a simple condition string like 'rsi < 30' and filter rows."""
    import re
    parts = re.split(r'\s+and\s+', condition_str.lower().strip())
    filtered = list(rows)
    for part in parts:
        m = re.match(r'(\w+)\s*([<>=!]+)\s*([\d.]+)', part.strip())
        if not m:
            continue
        field, op_str, val_str = m.group(1), m.group(2), m.group(3)
        try:
            threshold = float(val_str)
        except ValueError:
            continue
        if op_str in ("<", "<="):
            filtered = [r for r in filtered if (r.get(field) or 999) < threshold + (0.001 if "<=" in op_str else 0)]
        elif op_str in (">", ">="):
            filtered = [r for r in filtered if (r.get(field) or -999) > threshold - (0.001 if ">=" in op_str else 0)]
        elif op_str == "==":
            filtered = [r for r in filtered if r.get(field) == threshold]
    return filtered


def seek_pattern_data(db: Session, user_id: int | None) -> dict[str, Any]:
    """Actively mine more data for under-sampled but promising patterns.

    Identifies insights with few evidence samples but decent confidence,
    then mines a broader ticker set specifically looking for bars that
    match those pattern conditions to boost evidence counts.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .prescreener import get_prescreened_candidates

    insights = get_insights(db, user_id, limit=50)
    under_sampled = [
        ins for ins in insights
        if ins.evidence_count < 20 and ins.confidence > 0.4 and ins.active
    ]
    if not under_sampled:
        return {"sought": 0, "note": "no under-sampled patterns"}

    try:
        seek_tickers = get_prescreened_candidates(include_crypto=True, max_total=600)
    except Exception:
        from .market_data import ALL_SCAN_TICKERS
        seek_tickers = list(ALL_SCAN_TICKERS)

    seek_tickers = seek_tickers[:400]
    extra_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(_mine_from_history, t): t for t in seek_tickers}
        for f in as_completed(futs):
            if _shutting_down.is_set():
                break
            try:
                extra_rows.extend(f.result())
            except Exception:
                pass

    if len(extra_rows) < 10:
        return {"sought": 0, "rows_mined": len(extra_rows)}

    boosted = 0
    for ins in under_sampled:
        desc_lower = ins.pattern_description.lower()
        conditions = [
            cond for keyword, cond in _PATTERN_CONDITION_MAP.items()
            if keyword in desc_lower
        ]
        if not conditions:
            continue

        matching = [
            r for r in extra_rows
            if all(_row_matches_condition(r, c) for c in conditions)
        ]
        if len(matching) < 3:
            continue

        avg_5d = sum(r["ret_5d"] for r in matching) / len(matching)
        wins = sum(1 for r in matching if r["ret_5d"] > 0)
        wr = wins / len(matching) * 100

        old_evidence = ins.evidence_count
        old_conf = ins.confidence
        ins.evidence_count = min(old_evidence + len(matching), 200)

        if avg_5d > 0 and wr > 50:
            ins.confidence = round(min(0.95, old_conf * 0.7 + (wr / 100) * 0.3), 3)
        elif wr < 40:
            ins.confidence = round(max(0.1, old_conf * 0.8), 3)

        ins.last_seen = datetime.utcnow()
        db.commit()
        boosted += 1

        log_learning_event(
            db, user_id, "active_seeking",
            f"Boosted '{ins.pattern_description[:60]}' with {len(matching)} new samples "
            f"(evidence {old_evidence}->{ins.evidence_count}, "
            f"conf {old_conf:.0%}->{ins.confidence:.0%}, "
            f"avg {avg_5d:+.2f}%/5d, {wr:.0f}%wr)",
            confidence_before=old_conf,
            confidence_after=ins.confidence,
            related_insight_id=ins.id,
        )

    logger.info(
        f"[learning] Active seeking: boosted {boosted}/{len(under_sampled)} "
        f"under-sampled patterns from {len(extra_rows)} extra rows"
    )
    return {
        "sought": boosted,
        "under_sampled_total": len(under_sampled),
        "extra_rows_mined": len(extra_rows),
    }


def _auto_backtest_patterns(db: Session, user_id: int | None) -> int:
    """Run backtests on a sample of tickers to validate discovered patterns.

    Maps pattern types to the most relevant backtesting strategy and runs
    backtests on the top-scored tickers. Updates pattern confidence based
    on backtest win rates.
    """
    from ..backtest_service import run_backtest, save_backtest, STRATEGIES

    PATTERN_STRATEGY_MAP = {
        "rsi": "rsi_reversal",
        "macd": "macd",
        "bollinger": "bb_bounce",
        "ema": "ema_cross",
        "sma": "sma_cross",
        "trend": "trend_follow",
        "momentum": "trend_follow",
    }

    insights = get_insights(db, user_id, limit=20)
    if not insights:
        return 0

    test_tickers = list(DEFAULT_SCAN_TICKERS[:18]) + list(DEFAULT_CRYPTO_TICKERS[:7])

    backtests_run = 0
    for ins in insights[:10]:
        desc_lower = ins.pattern_description.lower()
        strategy_id = None
        for keyword, strat in PATTERN_STRATEGY_MAP.items():
            if keyword in desc_lower:
                strategy_id = strat
                break
        if not strategy_id:
            strategy_id = "trend_follow"

        wins = 0
        total = 0
        for ticker in test_tickers[:10]:
            if _shutting_down.is_set():
                break
            try:
                result = run_backtest(ticker, strategy_id=strategy_id, period="1y")
                if result.get("ok") and result.get("trade_count", 0) > 0:
                    save_backtest(db, user_id, result)
                    total += 1
                    if result.get("return_pct", 0) > 0:
                        wins += 1
                    backtests_run += 1
            except Exception:
                continue

        if total >= 3:
            bt_win_rate = wins / total
            old_conf = ins.confidence
            new_conf = old_conf * 0.7 + bt_win_rate * 0.3
            ins.confidence = round(min(0.95, max(0.1, new_conf)), 3)
            ins.evidence_count += total
            db.commit()
            if abs(new_conf - old_conf) > 0.01:
                log_learning_event(
                    db, user_id, "backtest_validation",
                    f"Pattern backtested ({wins}/{total} profitable): "
                    f"{ins.pattern_description[:80]} | conf {old_conf:.0%}->{ins.confidence:.0%}",
                    confidence_before=old_conf,
                    confidence_after=ins.confidence,
                    related_insight_id=ins.id,
                )

    logger.info(f"[learning] Auto-backtest: {backtests_run} backtests across {len(insights[:10])} patterns")
    return backtests_run


def validate_and_evolve(db: Session, user_id: int | None) -> dict[str, Any]:
    """Test every scoring assumption against real data and evolve.

    This is where CHILI grows beyond any single strategy.  For each key
    hypothesis (e.g. "MACD negative = always bad"), we check the actual
    historical data.  If the data contradicts an assumption, CHILI weakens
    it.  If CHILI discovers something new that no strategy taught it, it
    creates a novel insight.

    Called as part of the learning cycle.
    """
    from .market_data import ALL_SCAN_TICKERS

    mine_tickers = list(ALL_SCAN_TICKERS)[:120]

    rows: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_mine_from_history, t): t for t in mine_tickers}
        for f in as_completed(futs):
            try:
                rows.extend(f.result())
            except Exception:
                pass

    if len(rows) < 30:
        return {"tested": 0, "note": "insufficient data for self-validation"}

    results: list[dict[str, Any]] = []

    def _test_hypothesis(label: str, group_a: list, group_b: list, expected: str):
        """Compare two groups (e.g. MACD+ vs MACD-) and report which actually performs better."""
        if len(group_a) < 5 or len(group_b) < 5:
            return
        avg_a = sum(r["ret_5d"] for r in group_a) / len(group_a)
        avg_b = sum(r["ret_5d"] for r in group_b) / len(group_b)
        wr_a = sum(1 for r in group_a if r["ret_5d"] > 0) / len(group_a) * 100
        wr_b = sum(1 for r in group_b if r["ret_5d"] > 0) / len(group_b) * 100

        if expected == "a_better":
            confirmed = avg_a > avg_b
        else:
            confirmed = avg_b > avg_a

        finding = {
            "hypothesis": label,
            "confirmed": confirmed,
            "group_a_avg": round(avg_a, 3),
            "group_b_avg": round(avg_b, 3),
            "group_a_wr": round(wr_a, 1),
            "group_b_wr": round(wr_b, 1),
            "group_a_n": len(group_a),
            "group_b_n": len(group_b),
        }
        results.append(finding)

        if not confirmed:
            save_insight(
                db, user_id,
                f"CHILI challenge: {label} — data says otherwise "
                f"(A: {avg_a:+.2f}%/5d {wr_a:.0f}%wr vs B: {avg_b:+.2f}%/5d {wr_b:.0f}%wr, "
                f"n={len(group_a)}+{len(group_b)})",
                confidence=0.45,
            )
            log_learning_event(
                db, user_id, "hypothesis_challenged",
                f"Data challenges: {label} | "
                f"Expected {'A' if expected == 'a_better' else 'B'} wins, "
                f"but {'B' if expected == 'a_better' else 'A'} actually performed better",
            )
        else:
            if avg_a > 0 and wr_a > 55:
                save_insight(
                    db, user_id,
                    f"CHILI validated: {label} — confirmed by data "
                    f"({avg_a:+.2f}%/5d, {wr_a:.0f}%wr, {len(group_a)} samples)",
                    confidence=min(0.85, wr_a / 100),
                )

    # ── Test: Does MACD positive actually outperform MACD negative? ──
    macd_pos = [r for r in rows if r["macd"] > r["macd_sig"] and r["macd_hist"] > 0]
    macd_neg = [r for r in rows if r["macd"] < r["macd_sig"] and r["macd_hist"] < 0]
    _test_hypothesis(
        "MACD positive entries outperform MACD negative entries",
        macd_pos, macd_neg, "a_better",
    )

    # ── Test: Do high-volume (>3x) entries outperform low-volume? ──
    vol_hi = [r for r in rows if r.get("vol_ratio") and r["vol_ratio"] > 3.0]
    vol_lo = [r for r in rows if r.get("vol_ratio") and r["vol_ratio"] < 1.5]
    _test_hypothesis(
        "High relative volume (>3x) entries outperform low volume (<1.5x)",
        vol_hi, vol_lo, "a_better",
    )

    # ── Test: Do strong ADX trends (>25) produce better returns? ──
    adx_hi = [r for r in rows if r["adx"] > 25]
    adx_lo = [r for r in rows if r["adx"] < 15]
    _test_hypothesis(
        "Strong trends (ADX>25) outperform range-bound (ADX<15)",
        adx_hi, adx_lo, "a_better",
    )

    # ── Test: Does EMA stack (bullish alignment) outperform broken EMAs? ──
    ema_stack = [r for r in rows if r.get("ema_stack")]
    ema_broken = [r for r in rows if not r.get("ema_stack")]
    _test_hypothesis(
        "Bullish EMA stack outperforms broken EMA alignment",
        ema_stack, ema_broken, "a_better",
    )

    # ── Test: Do oversold RSI entries (RSI<30) outperform neutral RSI? ──
    rsi_os = [r for r in rows if r["rsi"] < 30]
    rsi_mid = [r for r in rows if 40 <= r["rsi"] <= 60]
    _test_hypothesis(
        "Oversold RSI (<30) mean-reversion outperforms neutral RSI entries",
        rsi_os, rsi_mid, "a_better",
    )

    # ── Test: MACD histogram momentum (positive hist) vs negative hist ──
    hist_pos = [r for r in rows if r["macd_hist"] > 0 and r["rsi"] > 40 and r["rsi"] < 65]
    hist_neg = [r for r in rows if r["macd_hist"] < 0 and r["rsi"] > 40 and r["rsi"] < 65]
    _test_hypothesis(
        "MACD histogram positive (momentum) outperforms histogram negative in neutral RSI zone",
        hist_pos, hist_neg, "a_better",
    )

    # ── Exploratory: Find novel patterns CHILI discovers on its own ──
    # Check conditions that no human taught — let the data reveal surprises
    bb_low_macd_neg = [r for r in rows if r["bb_pct"] < 0.15 and r["macd_hist"] < 0]
    bb_low_macd_pos = [r for r in rows if r["bb_pct"] < 0.15 and r["macd_hist"] > 0]
    if len(bb_low_macd_neg) >= 5 and len(bb_low_macd_pos) >= 5:
        avg_neg = sum(r["ret_5d"] for r in bb_low_macd_neg) / len(bb_low_macd_neg)
        avg_pos = sum(r["ret_5d"] for r in bb_low_macd_pos) / len(bb_low_macd_pos)
        if avg_neg > avg_pos and avg_neg > 0.5:
            save_insight(
                db, user_id,
                f"CHILI discovery: BB low + MACD negative STILL profitable "
                f"({avg_neg:+.2f}%/5d vs {avg_pos:+.2f}%/5d) — "
                f"contrarian bounce pattern (n={len(bb_low_macd_neg)})",
                confidence=0.5,
            )
            results.append({
                "hypothesis": "CHILI novel: BB oversold + MACD neg = contrarian opportunity",
                "confirmed": True,
                "avg_return": round(avg_neg, 3),
            })

    stoch_ext_adx = [r for r in rows if r["stoch_k"] < 20 and r["adx"] > 30]
    if len(stoch_ext_adx) >= 5:
        avg_ret = sum(r["ret_5d"] for r in stoch_ext_adx) / len(stoch_ext_adx)
        wr = sum(1 for r in stoch_ext_adx if r["ret_5d"] > 0) / len(stoch_ext_adx) * 100
        if avg_ret > 0.3 and wr > 50:
            save_insight(
                db, user_id,
                f"CHILI discovery: Stochastic extreme (<20) + strong trend (ADX>30) "
                f"= high-probability snap-back ({avg_ret:+.2f}%/5d, {wr:.0f}%wr, "
                f"n={len(stoch_ext_adx)})",
                confidence=min(0.8, wr / 100),
            )

    # ── Dynamic hypothesis testing (from LLM-generated hypotheses) ──
    dynamic_tested = 0
    try:
        hyp_insights = db.query(TradingInsight).filter(
            TradingInsight.user_id == user_id,
            TradingInsight.active.is_(True),
            TradingInsight.pattern_description.like("hypothesis:%"),
        ).limit(10).all()

        for hyp_ins in hyp_insights:
            desc = hyp_ins.pattern_description
            parts = desc.split("|")
            if len(parts) < 3:
                continue
            cond_a_str = parts[1].strip().replace("A:", "").strip()
            cond_b_str = parts[2].strip().replace("B:", "").strip()
            expected = "a"
            if len(parts) >= 4:
                exp_part = parts[3].strip().lower()
                if "b" in exp_part:
                    expected = "b"

            group_a = _filter_rows_by_condition(rows, cond_a_str)
            group_b = _filter_rows_by_condition(rows, cond_b_str)

            if len(group_a) >= 5 and len(group_b) >= 5:
                label = desc.split("|")[0].replace("hypothesis:", "").strip()
                _test_hypothesis(label, group_a, group_b, f"{expected}_better")
                dynamic_tested += 1

                hyp_ins.active = False
                db.commit()

        if dynamic_tested > 0:
            log_learning_event(
                db, user_id, "dynamic_hypothesis_testing",
                f"Tested {dynamic_tested} LLM-generated hypotheses",
            )
    except Exception as e:
        logger.warning(f"[learning] Dynamic hypothesis testing failed: {e}")

    # ── Feed real-trade per-pattern win rates back into insight confidence ──
    real_trade_adjustments = 0
    try:
        pattern_stats = get_trade_stats_by_pattern(db, user_id, min_trades=3)
        if pattern_stats:
            all_insights = get_insights(db, user_id, limit=100)
            for ps in pattern_stats:
                tag = ps["pattern"]
                real_wr = ps["win_rate"]
                for ins in all_insights:
                    if tag.replace("_", " ") in ins.pattern_description.lower():
                        old_conf = ins.confidence
                        ins.confidence = round(
                            min(0.95, old_conf * 0.5 + (real_wr / 100) * 0.5), 3
                        )
                        db.commit()
                        real_trade_adjustments += 1
                        log_learning_event(
                            db, user_id, "real_trade_validation",
                            f"Pattern '{tag}' real-trade WR {real_wr:.0f}% "
                            f"({ps['trades']} trades) adjusted confidence "
                            f"{old_conf:.0%} -> {ins.confidence:.0%}",
                            confidence_before=old_conf,
                            confidence_after=ins.confidence,
                            related_insight_id=ins.id,
                        )
                        break
    except Exception as e:
        logger.warning(f"[learning] Per-pattern trade feedback failed: {e}")

    # ── Now evolve the scoring weights based on what we learned ──
    from .scanner import evolve_strategy_weights
    weight_result = evolve_strategy_weights(db)

    confirmed_count = sum(1 for r in results if r.get("confirmed"))
    challenged_count = sum(1 for r in results if not r.get("confirmed", True))

    log_learning_event(
        db, user_id, "self_validation",
        f"Tested {len(results)} hypotheses: {confirmed_count} confirmed, "
        f"{challenged_count} challenged by data. "
        f"Real-trade adjustments: {real_trade_adjustments}. "
        f"Evolved {weight_result.get('adjusted', 0)} scoring weights.",
    )

    logger.info(
        f"[learning] Self-validation: {len(results)} hypotheses tested, "
        f"{confirmed_count} confirmed, {challenged_count} challenged, "
        f"{real_trade_adjustments} real-trade adjustments, "
        f"{weight_result.get('adjusted', 0)} weights evolved"
    )

    return {
        "hypotheses_tested": len(results),
        "confirmed": confirmed_count,
        "challenged": challenged_count,
        "real_trade_adjustments": real_trade_adjustments,
        "weights_evolved": weight_result.get("adjusted", 0),
        "details": results,
    }


# ── Pattern Refinement Engine ──────────────────────────────────────────

REFINEMENT_RULES: dict[str, list[dict[str, Any]]] = {
    "rsi oversold": [
        {"field": "rsi", "op": "lt", "variations": [25, 28, 30, 32, 35]},
    ],
    "rsi overbought": [
        {"field": "rsi", "op": "gt", "variations": [65, 68, 70, 72, 75]},
    ],
    "rsi<35": [
        {"field": "rsi", "op": "lt", "variations": [30, 33, 35, 38, 40]},
    ],
    "rsi<40": [
        {"field": "rsi", "op": "lt", "variations": [35, 38, 40, 42, 45]},
    ],
    "adx>25": [
        {"field": "adx", "op": "gt", "variations": [20, 22, 25, 28, 30]},
    ],
    "adx>30": [
        {"field": "adx", "op": "gt", "variations": [25, 28, 30, 33, 35]},
    ],
    "stoch<20": [
        {"field": "stoch_k", "op": "lt", "variations": [15, 18, 20, 22, 25]},
    ],
    "stoch<25": [
        {"field": "stoch_k", "op": "lt", "variations": [18, 20, 25, 28, 30]},
    ],
    "bb<0.15": [
        {"field": "bb_pct", "op": "lt", "variations": [0.10, 0.12, 0.15, 0.18, 0.20]},
    ],
    "volume surge": [
        {"field": "vol_ratio", "op": "gt", "variations": [1.5, 2.0, 2.5, 3.0, 4.0]},
    ],
    "volume spike 2x": [
        {"field": "vol_ratio", "op": "gt", "variations": [1.5, 2.0, 3.0, 4.0, 5.0]},
    ],
    "macd positive": [
        {"field": "macd_hist", "op": "gt", "variations": [0, 0.01, 0.05]},
    ],
}


def refine_patterns(db: Session, user_id: int | None) -> dict[str, Any]:
    """Test parameter variations of top patterns and save improved variants.

    For each high-evidence pattern, tries different threshold values
    (e.g. RSI < 25/28/30/32/35) and saves the variant that outperforms
    the original.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .market_data import ALL_SCAN_TICKERS

    insights = get_insights(db, user_id, limit=50)
    top_patterns = sorted(insights, key=lambda i: i.evidence_count, reverse=True)[:10]

    if not top_patterns:
        return {"refined": 0, "note": "no patterns to refine"}

    mine_tickers = list(ALL_SCAN_TICKERS)[:200]
    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_mine_from_history, t): t for t in mine_tickers}
        for f in as_completed(futs):
            if _shutting_down.is_set():
                break
            try:
                all_rows.extend(f.result())
            except Exception:
                pass

    if len(all_rows) < 50:
        return {"refined": 0, "note": "insufficient data for refinement"}

    refined_count = 0
    for ins in top_patterns:
        desc_lower = ins.pattern_description.lower()

        for rule_key, rule_defs in REFINEMENT_RULES.items():
            if rule_key not in desc_lower:
                continue

            for rule_def in rule_defs:
                field = rule_def["field"]
                op = rule_def["op"]
                variations = rule_def["variations"]

                best_variant = None
                best_score = -999.0
                best_wr = 0.0
                best_n = 0

                for threshold in variations:
                    if op == "lt":
                        filtered = [r for r in all_rows if r.get(field, 999) < threshold]
                    elif op == "gt":
                        filtered = [r for r in all_rows if r.get(field, -999) > threshold]
                    else:
                        continue

                    if len(filtered) < 10:
                        continue

                    avg_5d = sum(r["ret_5d"] for r in filtered) / len(filtered)
                    wins = sum(1 for r in filtered if r["ret_5d"] > 0)
                    wr = wins / len(filtered) * 100
                    composite = avg_5d * 0.6 + (wr / 100) * 0.4

                    if composite > best_score:
                        best_score = composite
                        best_variant = threshold
                        best_wr = wr
                        best_n = len(filtered)

                if best_variant is not None and best_score > 0:
                    original_desc = ins.pattern_description[:80]
                    refined_label = (
                        f"CHILI refinement: {field} {op} {best_variant} "
                        f"(avg {best_score:.2f}, {best_wr:.0f}%wr, n={best_n}) "
                        f"— refined from '{original_desc}'"
                    )
                    save_insight(
                        db, user_id, refined_label,
                        confidence=min(0.85, best_wr / 100),
                    )
                    refined_count += 1
                    log_learning_event(
                        db, user_id, "pattern_refinement",
                        f"Refined '{original_desc}': best threshold "
                        f"{field}{op}{best_variant} "
                        f"({best_wr:.0f}%wr, n={best_n})",
                        related_insight_id=ins.id,
                    )
                    break

    logger.info(f"[learning] Pattern refinement: {refined_count} patterns improved")
    return {"refined": refined_count, "top_patterns_checked": len(top_patterns)}


def deep_study(db: Session, user_id: int | None) -> dict[str, Any]:
    """Intensive AI-powered learning: mine patterns, then ask LLM to reflect
    and generate structured testable hypotheses for the next validation cycle."""
    discoveries = mine_patterns(db, user_id)

    insights = get_insights(db, user_id, limit=30)
    insight_lines = []
    for ins in insights:
        insight_lines.append(
            f"- [{ins.confidence:.0%} conf, {ins.evidence_count} evidence] {ins.pattern_description}"
        )
    insight_text = "\n".join(insight_lines) if insight_lines else "No patterns learned yet."

    snap_count = db.query(MarketSnapshot).count()
    filled_count = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
    ).count()

    recent_scans = db.query(ScanResult).order_by(
        ScanResult.scanned_at.desc()
    ).limit(20).all()
    buy_count = sum(1 for s in recent_scans if s.signal == "buy")
    sell_count = sum(1 for s in recent_scans if s.signal == "sell")
    hold_count = sum(1 for s in recent_scans if s.signal == "hold")

    stats = get_trade_stats(db, user_id)

    # Per-pattern trade stats
    pattern_stats = get_trade_stats_by_pattern(db, user_id, min_trades=1)
    pattern_stats_text = "No per-pattern trade data yet."
    if pattern_stats:
        lines = []
        for ps in pattern_stats[:15]:
            lines.append(
                f"  - {ps['pattern']}: {ps['trades']} trades, "
                f"{ps['win_rate']:.0f}%wr, avg P&L ${ps['avg_pnl']:.2f}"
            )
        pattern_stats_text = "\n".join(lines)

    # Market regime
    regime_text = "Unknown"
    try:
        from .market_data import get_market_regime
        regime = get_market_regime()
        regime_text = (
            f"SPY {regime['spy_direction']} (5d momentum {regime['spy_momentum_5d']:+.1f}%), "
            f"VIX {regime['vix_regime']} ({regime['vix']}), "
            f"overall: {regime['regime']}"
        )
    except Exception:
        pass

    # Adaptive weights drift
    weights_text = "Not available"
    try:
        from .scanner import get_all_weights, _DEFAULT_WEIGHTS
        current_w = get_all_weights()
        drifts = []
        for k, v in current_w.items():
            default = _DEFAULT_WEIGHTS.get(k, v)
            if default != 0 and abs(v - default) / abs(default) > 0.1:
                drifts.append(f"  - {k}: {default} -> {v} ({(v-default)/abs(default):+.0%})")
        weights_text = "\n".join(drifts) if drifts else "All weights at defaults."
    except Exception:
        pass

    # Recently challenged hypotheses
    challenged_text = "None yet."
    try:
        challenged_events = db.query(LearningEvent).filter(
            LearningEvent.event_type == "hypothesis_challenged",
        ).order_by(LearningEvent.created_at.desc()).limit(5).all()
        if challenged_events:
            challenged_text = "\n".join(
                f"  - {e.description[:120]}" for e in challenged_events
            )
    except Exception:
        pass

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

## PER-PATTERN TRADE PERFORMANCE
{pattern_stats_text}

## CURRENT MARKET REGIME
{regime_text}

## ADAPTIVE WEIGHT DRIFT (what the brain is learning)
{weights_text}

## RECENTLY CHALLENGED HYPOTHESES
{challenged_text}

Write a LEARNING REPORT in this format:

### What I've Learned So Far
(Summarize the top 3-5 most reliable patterns, explain each in plain English)

### What's Working
(Which patterns have the highest confidence and best returns?)

### What I'm Still Figuring Out
(What areas need more data? What hypotheses are you testing?)

### My Current Market Read
(Based on the patterns + regime, what's the overall market telling you?)

### Next Study Goals
(What should I focus on studying next to get smarter?)

### Hypotheses to Test
IMPORTANT: After your report, output a JSON block with concrete testable hypotheses.
Use EXACTLY this format (valid JSON):

```json
{{
  "hypotheses": [
    {{
      "description": "human-readable description of what to test",
      "condition_a": "indicator condition for group A (e.g. 'rsi < 30')",
      "condition_b": "indicator condition for group B (e.g. 'rsi >= 30 and rsi < 50')",
      "expected_winner": "a"
    }}
  ]
}}
```

Generate 3-5 hypotheses based on gaps in your knowledge, challenged hypotheses above,
or interesting combinations you want to explore. Use indicator fields: rsi, macd_hist,
macd (vs macd_sig), adx, stoch_k, bb_pct, vol_ratio, ema_stack, gap_pct.
Conditions should use simple comparisons like "rsi < 30", "adx > 25", "stoch_k < 20".

Keep it conversational and honest. Use actual numbers from the patterns above."""

    try:
        from ...prompts import load_prompt
        from ... import openai_client
        from ...logger import new_trace_id

        system_prompt = load_prompt("trading_analyst")
        trace_id = new_trace_id()
        result = openai_client.chat(
            messages=[{"role": "user", "content": reflection_prompt}],
            system_prompt=system_prompt,
            trace_id=trace_id,
            user_message=reflection_prompt,
            max_tokens=3000,
        )
        reflection = result.get("reply", "Could not generate reflection.")
    except Exception as e:
        reflection = f"Reflection unavailable: {e}"

    # Extract structured hypotheses from the LLM response
    extracted_hypotheses = _extract_hypotheses_from_reflection(reflection)
    hypotheses_saved = 0
    for hyp in extracted_hypotheses:
        desc = hyp.get("description", "")
        cond_a = hyp.get("condition_a", "")
        cond_b = hyp.get("condition_b", "")
        expected = hyp.get("expected_winner", "a")
        if desc and cond_a and cond_b:
            save_insight(
                db, user_id,
                f"hypothesis:{desc} | A: {cond_a} | B: {cond_b} | expected: {expected}",
                confidence=0.5,
            )
            hypotheses_saved += 1

    log_learning_event(
        db, user_id, "review",
        f"Deep study: {len(discoveries)} new patterns, {len(insights)} total active. "
        f"AI reflection generated. {hypotheses_saved} new hypotheses extracted.",
    )

    return {
        "ok": True,
        "discoveries": discoveries,
        "total_patterns": len(insights),
        "reflection": reflection,
        "hypotheses_extracted": hypotheses_saved,
        "stats": {
            "snapshots": snap_count,
            "verified": filled_count,
            "new_discoveries": len(discoveries),
        },
    }


def _extract_hypotheses_from_reflection(text: str) -> list[dict]:
    """Parse structured hypotheses JSON from LLM reflection output."""
    import re
    hypotheses = []
    json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if not json_match:
        json_match = re.search(r'\{\s*"hypotheses"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1) if json_match.lastindex else json_match.group(0))
            raw = data.get("hypotheses", [])
            for h in raw:
                if isinstance(h, dict) and "description" in h:
                    hypotheses.append(h)
        except (json.JSONDecodeError, AttributeError):
            pass
    return hypotheses[:10]


# ── Multi-Signal Prediction Engine ────────────────────────────────────

def compute_prediction(indicator_data: dict) -> float:
    """Compute a directional prediction score from indicator data.

    Returns a score from -10 (strongly bearish) to +10 (strongly bullish).
    Each signal contributes a weighted vote. The final score is the sum
    clamped to [-10, +10].

    Signals used (with weights):
      RSI (2.0), MACD histogram (1.5), MACD crossover (1.0),
      EMA alignment (1.5), Bollinger Band position (1.0),
      Stochastic (1.0), ADX trend strength (0.5), Volume (0.5)
    """
    score = 0.0

    rsi_data = indicator_data.get("rsi", {})
    macd_data = indicator_data.get("macd", {})
    bb_data = indicator_data.get("bbands", {})
    stoch_data = indicator_data.get("stoch", {})
    adx_data = indicator_data.get("adx", {})
    ema20_data = indicator_data.get("ema_20", {})
    ema50_data = indicator_data.get("ema_50", {})
    ema100_data = indicator_data.get("ema_100", {})
    sma20_data = indicator_data.get("sma_20", {})
    obv_data = indicator_data.get("obv", {})
    atr_data = indicator_data.get("atr", {})

    rsi = rsi_data.get("value") if rsi_data else None
    if rsi is not None:
        if rsi < 25:
            score += 2.0
        elif rsi < 35:
            score += 1.5
        elif rsi < 45:
            score += 0.5
        elif rsi > 75:
            score -= 2.0
        elif rsi > 65:
            score -= 1.5
        elif rsi > 55:
            score -= 0.5

    macd_hist = macd_data.get("histogram") if macd_data else None
    macd_line = macd_data.get("macd") if macd_data else None
    macd_sig = macd_data.get("signal") if macd_data else None
    if macd_hist is not None:
        if macd_hist > 0:
            score += min(1.5, macd_hist * 10)
        else:
            score -= min(1.5, abs(macd_hist) * 10)
    if macd_line is not None and macd_sig is not None:
        if macd_line > macd_sig:
            score += 1.0
        elif macd_line < macd_sig:
            score -= 1.0

    e20 = ema20_data.get("value") if ema20_data else None
    e50 = ema50_data.get("value") if ema50_data else None
    e100 = ema100_data.get("value") if ema100_data else None
    sma20 = sma20_data.get("value") if sma20_data else None
    if e20 is not None and e50 is not None and e100 is not None:
        if e20 > e50 > e100:
            score += 1.5
        elif e20 < e50 < e100:
            score -= 1.5
        elif e20 > e50:
            score += 0.5
        elif e20 < e50:
            score -= 0.5

    bb_upper = bb_data.get("upper") if bb_data else None
    bb_lower = bb_data.get("lower") if bb_data else None
    if bb_upper and bb_lower and bb_upper > bb_lower:
        bb_mid = (bb_upper + bb_lower) / 2
        bb_range = bb_upper - bb_lower
        if sma20 is not None:
            bb_pos = (sma20 - bb_lower) / bb_range
        elif e20 is not None:
            bb_pos = (e20 - bb_lower) / bb_range
        else:
            bb_pos = 0.5
        if bb_pos < 0.15:
            score += 1.0
        elif bb_pos < 0.3:
            score += 0.5
        elif bb_pos > 0.85:
            score -= 1.0
        elif bb_pos > 0.7:
            score -= 0.5

    stoch_k = stoch_data.get("k") if stoch_data else None
    if stoch_k is not None:
        if stoch_k < 20:
            score += 1.0
        elif stoch_k < 30:
            score += 0.5
        elif stoch_k > 80:
            score -= 1.0
        elif stoch_k > 70:
            score -= 0.5

    adx_val = adx_data.get("adx") if adx_data else None
    if adx_val is not None and adx_val > 25:
        score *= 1.0 + min(0.5, (adx_val - 25) / 50)

    return max(-10.0, min(10.0, round(score, 2)))


def predict_direction(score: float) -> str:
    """Convert prediction score to a human-readable direction."""
    if score >= 3.0:
        return "bullish"
    elif score >= 1.0:
        return "slightly_bullish"
    elif score <= -3.0:
        return "bearish"
    elif score <= -1.0:
        return "slightly_bearish"
    return "neutral"


def predict_confidence(score: float) -> int:
    """Convert absolute prediction score to a confidence percentage (0-100)."""
    return min(100, int(abs(score) * 10))


def _build_prediction_tickers(db: Session, explicit: list[str] | None) -> list[str]:
    """Build a diverse ticker list for predictions from multiple sources."""
    if explicit:
        return explicit

    seen: set[str] = set()
    result: list[str] = []

    def _add(t: str):
        u = t.upper()
        if u not in seen:
            seen.add(u)
            result.append(u)

    recent_scans = (
        db.query(ScanResult.ticker, ScanResult.score)
        .order_by(ScanResult.scanned_at.desc())
        .limit(500)
        .all()
    )
    top_scanned = sorted(set((r.ticker, r.score) for r in recent_scans), key=lambda x: x[1], reverse=True)
    for ticker, _ in top_scanned[:60]:
        _add(ticker)

    try:
        from .prescreener import get_trending_crypto
        for t in get_trending_crypto()[:30]:
            _add(t)
    except Exception:
        pass

    try:
        from ..ticker_universe import get_all_crypto_tickers
        for t in get_all_crypto_tickers(n=100)[:40]:
            _add(t)
    except Exception:
        for t in DEFAULT_CRYPTO_TICKERS[:20]:
            _add(t)

    try:
        wl_items = get_watchlist(db, user_id=None)
        for item in wl_items[:20]:
            _add(item.ticker)
    except Exception:
        pass

    if len(result) < 40:
        for t in DEFAULT_SCAN_TICKERS[:30]:
            _add(t)
        for t in DEFAULT_CRYPTO_TICKERS[:15]:
            _add(t)

    return result


def _predict_single_ticker(
    ticker: str,
    quotes_map: dict[str, dict],
    vix: float | None,
    vol_regime: dict,
    ml_available: bool,
    predict_ml_fn,
    extract_features_fn,
    market_regime: dict | None = None,
) -> dict | None:
    """Predict a single ticker. Thread-safe; returns None on failure."""
    try:
        snapshot = get_indicator_snapshot(ticker)
        if not snapshot or len(snapshot) < 3:
            return None
        ind_data = {k: v for k, v in snapshot.items() if k not in ("ticker", "interval")}
        rule_score = compute_prediction(ind_data)

        quote = quotes_map.get(ticker)
        if not quote:
            quote = fetch_quote(ticker)
        price = quote["price"] if quote else None

        ml_prob = None
        blended_score = rule_score
        if ml_available and price:
            ind_data_with_ticker = dict(ind_data)
            ind_data_with_ticker["ticker"] = ticker
            features = extract_features_fn(
                ind_data_with_ticker, close_price=price, vix=vix,
                regime=market_regime,
            )
            ml_prob = predict_ml_fn(features)
            if ml_prob is not None:
                rule_norm = (rule_score + 10) / 20
                blended = 0.4 * rule_norm + 0.6 * ml_prob
                blended_score = round((blended * 20) - 10, 2)

        regime = vol_regime.get("regime", "normal")
        if regime == "extreme":
            blended_score *= 0.6
        elif regime == "elevated":
            if abs(blended_score) < 3:
                blended_score *= 0.8

        blended_score = max(-10.0, min(10.0, round(blended_score, 2)))
        direction = predict_direction(blended_score)
        confidence = predict_confidence(blended_score)

        atr_val = (ind_data.get("atr") or {}).get("value")
        _cr = ticker.upper().endswith("-USD")
        _rd = 8 if _cr else 6
        stop = target = rr = pos_size_pct = None
        _vol_pct = (atr_val / price * 100) if price and atr_val else 0
        _stop_mult = 2.5 if _vol_pct > 3 else 2.0
        if price and atr_val and atr_val > 0:
            if blended_score > 0:
                stop = round(price - atr_val * _stop_mult, _rd)
                target = round(price + atr_val * 3.0, _rd)
            elif blended_score < 0:
                stop = round(price + atr_val * _stop_mult, _rd)
                target = round(price - atr_val * 3.0, _rd)
            if stop is not None and target is not None:
                risk = abs(price - stop)
                reward = abs(target - price)
                rr = round(reward / risk, 2) if risk > 0 else 0
                pos_size_pct = round(min(5.0, 1.0 / (risk / price * 100)) * 100 / 100, 2) if price > 0 else None

        return {
            "ticker": ticker,
            "price": price,
            "score": blended_score,
            "rule_score": rule_score,
            "ml_probability": round(ml_prob, 4) if ml_prob is not None else None,
            "direction": direction,
            "confidence": confidence,
            "signals": _explain_prediction(ind_data, blended_score),
            "vix_regime": regime,
            "suggested_stop": stop,
            "suggested_target": target,
            "risk_reward": rr,
            "position_size_pct": pos_size_pct,
        }
    except Exception:
        return None


def get_current_predictions(db: Session, tickers: list[str] | None = None) -> list[dict]:
    """Generate live predictions for a set of tickers.

    Blends rule-based scores with ML probabilities and adjusts for
    volatility regime. Includes risk-management fields (stop, target, R:R).
    Uses ThreadPoolExecutor to process tickers in parallel for speed.

    When *tickers* is None (the common case from Top Picks), results are
    cached with stale-while-revalidate: 3 min fresh, 10 min stale.
    Explicit ticker lists bypass the cache.
    """
    global _pred_cache, _pred_refreshing

    if tickers is not None:
        return _get_current_predictions_impl(db, tickers)

    now = time.time()
    age = now - _pred_cache["ts"]

    if _pred_cache["results"] and age < _PRED_CACHE_TTL:
        return _pred_cache["results"]

    if _pred_cache["results"] and age < _PRED_CACHE_STALE_TTL:
        with _pred_refresh_lock:
            if not _pred_refreshing:
                _pred_refreshing = True

                def _bg_refresh():
                    global _pred_cache, _pred_refreshing
                    try:
                        from ...db import SessionLocal
                        s = SessionLocal()
                        try:
                            fresh = _get_current_predictions_impl(s, None)
                            _pred_cache = {"results": fresh, "ts": time.time()}
                        finally:
                            s.close()
                    except Exception:
                        logger.debug("Background prediction refresh failed", exc_info=True)
                    finally:
                        _pred_refreshing = False

                threading.Thread(target=_bg_refresh, daemon=True).start()
        return _pred_cache["results"]

    results = _get_current_predictions_impl(db, None)
    _pred_cache = {"results": results, "ts": time.time()}
    return results


def _get_current_predictions_impl(db: Session, tickers: list[str] | None) -> list[dict]:
    """Core prediction logic (no cache)."""
    from .ml_engine import predict_ml, extract_features, is_model_ready

    tickers = _build_prediction_tickers(db, tickers)
    ticker_batch = tickers[:100]

    vix = get_vix()
    vol_regime = get_volatility_regime(vix)
    ml_available = is_model_ready()

    try:
        from .market_data import get_market_regime
        _mkt_regime = get_market_regime()
    except Exception:
        _mkt_regime = None

    quotes_map = fetch_quotes_batch(ticker_batch)

    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(
                _predict_single_ticker,
                t, quotes_map, vix, vol_regime, ml_available,
                predict_ml, extract_features,
                _mkt_regime,
            ): t
            for t in ticker_batch
        }
        for fut in as_completed(futures):
            entry = fut.result()
            if entry is not None:
                results.append(entry)

    results.sort(key=lambda x: abs(x["score"]), reverse=True)
    return results


def _explain_prediction(ind_data: dict, score: float) -> list[str]:
    """Generate human-readable explanations for a prediction."""
    reasons = []
    rsi = (ind_data.get("rsi") or {}).get("value")
    macd_data = ind_data.get("macd") or {}
    stoch = (ind_data.get("stoch") or {}).get("k")
    e20 = (ind_data.get("ema_20") or {}).get("value")
    e50 = (ind_data.get("ema_50") or {}).get("value")
    e100 = (ind_data.get("ema_100") or {}).get("value")
    adx = (ind_data.get("adx") or {}).get("adx")

    if rsi is not None:
        if rsi < 30:
            reasons.append(f"RSI oversold at {rsi:.0f}")
        elif rsi < 40:
            reasons.append(f"RSI near oversold ({rsi:.0f})")
        elif rsi > 70:
            reasons.append(f"RSI overbought at {rsi:.0f}")
        elif rsi > 60:
            reasons.append(f"RSI elevated ({rsi:.0f})")

    hist = macd_data.get("histogram")
    macd_l = macd_data.get("macd")
    macd_s = macd_data.get("signal")
    if macd_l is not None and macd_s is not None:
        if macd_l > macd_s and hist and hist > 0:
            reasons.append("MACD bullish crossover")
        elif macd_l < macd_s and hist and hist < 0:
            reasons.append("MACD bearish crossover")

    if e20 is not None and e50 is not None and e100 is not None:
        if e20 > e50 > e100:
            reasons.append("EMA stacking bullish")
        elif e20 < e50 < e100:
            reasons.append("EMA stacking bearish")

    if stoch is not None:
        if stoch < 20:
            reasons.append(f"Stochastic oversold ({stoch:.0f})")
        elif stoch > 80:
            reasons.append(f"Stochastic overbought ({stoch:.0f})")

    if adx is not None and adx > 25:
        reasons.append(f"Strong trend (ADX {adx:.0f})")

    if not reasons:
        reasons.append("Mixed signals, no strong conviction")

    return reasons


# ── Brain Dashboard Stats ────────────────────────────────────────────

def backfill_predicted_scores(db: Session, limit: int = 500) -> int:
    """Batch-fill predicted_score on snapshots that have indicator_data but no score."""
    unfilled = db.query(MarketSnapshot).filter(
        MarketSnapshot.predicted_score.is_(None),
        MarketSnapshot.indicator_data.isnot(None),
    ).limit(limit).all()

    filled = 0
    for snap in unfilled:
        try:
            ind_data = json.loads(snap.indicator_data) if snap.indicator_data else {}
            if not ind_data:
                continue
            clean = {k: v for k, v in ind_data.items() if k not in ("ticker", "interval")}
            snap.predicted_score = compute_prediction(clean)
            filled += 1
        except Exception:
            continue

    if filled > 0:
        try:
            db.commit()
        except Exception:
            db.rollback()
    return filled


def get_brain_stats(db: Session, user_id: int | None) -> dict[str, Any]:
    from ..ticker_universe import get_ticker_count
    from .scanner import get_scan_status

    total_patterns = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id, TradingInsight.active.is_(True),
    ).count()

    avg_confidence_row = db.query(func.avg(TradingInsight.confidence)).filter(
        TradingInsight.user_id == user_id, TradingInsight.active.is_(True),
    ).scalar()
    avg_confidence = round(float(avg_confidence_row or 0) * 100, 1)

    week_ago = datetime.utcnow() - timedelta(days=7)
    two_weeks_ago = datetime.utcnow() - timedelta(days=14)
    patterns_this_week = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.created_at >= week_ago,
    ).count()

    recent_conf = db.query(func.avg(TradingInsight.confidence)).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
        TradingInsight.created_at >= week_ago,
    ).scalar()
    prior_conf = db.query(func.avg(TradingInsight.confidence)).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
        TradingInsight.created_at >= two_weeks_ago,
        TradingInsight.created_at < week_ago,
    ).scalar()
    if recent_conf and prior_conf:
        conf_delta = round((float(recent_conf) - float(prior_conf)) * 100, 1)
    else:
        conf_delta = 0.0

    total_snapshots = db.query(MarketSnapshot).count()

    # Backfill predicted_score on snapshots that have indicator_data but no score
    backfill_predicted_scores(db, limit=500)

    filled_snaps = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
    ).order_by(MarketSnapshot.snapshot_date.desc()).limit(2000).all()
    correct = 0
    medium_correct = medium_total = 0
    strong_correct = strong_total = 0
    total_predictions = 0
    stock_correct = stock_total = crypto_correct = crypto_total = 0
    for snap in filled_snaps:
        try:
            if snap.predicted_score is not None:
                pred_score = snap.predicted_score
            else:
                ind_data = json.loads(snap.indicator_data) if snap.indicator_data else {}
                if not ind_data:
                    continue
                pred_score = compute_prediction(ind_data)
                snap.predicted_score = pred_score

            if abs(pred_score) < 0.1:
                continue

            predicted_up = pred_score > 0
            actual_up = (snap.future_return_5d or 0) > 0
            is_hit = predicted_up == actual_up
            if is_hit:
                correct += 1
            total_predictions += 1

            is_c = snap.ticker.endswith("-USD")
            if is_c:
                crypto_total += 1
                if is_hit:
                    crypto_correct += 1
            else:
                stock_total += 1
                if is_hit:
                    stock_correct += 1

            if abs(pred_score) >= 1.0:
                medium_total += 1
                if is_hit:
                    medium_correct += 1

            if abs(pred_score) >= 3.0:
                strong_total += 1
                if is_hit:
                    strong_correct += 1
        except Exception:
            continue
    if total_predictions > 0:
        try:
            db.commit()
        except Exception:
            db.rollback()
    accuracy = round(correct / total_predictions * 100, 1) if total_predictions > 0 else 0
    medium_accuracy = round(medium_correct / medium_total * 100, 1) if medium_total > 0 else 0
    strong_accuracy = round(strong_correct / strong_total * 100, 1) if strong_total > 0 else 0
    stock_accuracy = round(stock_correct / stock_total * 100, 1) if stock_total > 0 else 0
    crypto_accuracy = round(crypto_correct / crypto_total * 100, 1) if crypto_total > 0 else 0

    # Pipeline status: pending predictions (have predicted_score, awaiting outcome)
    pending_predictions = db.query(MarketSnapshot).filter(
        MarketSnapshot.predicted_score.isnot(None),
        MarketSnapshot.future_return_5d.is_(None),
    ).count()

    evaluated_snapshots = db.query(MarketSnapshot).filter(
        MarketSnapshot.predicted_score.isnot(None),
        MarketSnapshot.future_return_5d.isnot(None),
    ).count()

    oldest_unevaluated = None
    days_until_first_result = None
    if pending_predictions > 0:
        oldest_pending = db.query(MarketSnapshot.snapshot_date).filter(
            MarketSnapshot.predicted_score.isnot(None),
            MarketSnapshot.future_return_5d.is_(None),
        ).order_by(MarketSnapshot.snapshot_date.asc()).first()
        if oldest_pending and oldest_pending[0]:
            oldest_unevaluated = oldest_pending[0].isoformat()
            days_elapsed = (datetime.utcnow() - oldest_pending[0]).days
            remaining = max(0, 7 - days_elapsed)
            days_until_first_result = remaining

    if total_snapshots == 0:
        pipeline_status = "no_data"
    elif pending_predictions > 0 and total_predictions == 0:
        pipeline_status = "pending_verification"
    elif total_predictions > 0:
        pipeline_status = "active"
    else:
        pipeline_status = "collecting"

    total_events = db.query(LearningEvent).filter(
        LearningEvent.user_id == user_id,
    ).count()

    universe_counts = get_ticker_count()
    scan_st = get_scan_status()
    learning_st = get_learning_status()
    vol_regime = get_volatility_regime()

    from .ml_engine import get_model_stats, is_model_ready
    ml_stats = get_model_stats()

    return {
        "total_patterns": total_patterns,
        "avg_confidence": avg_confidence,
        "confidence_trend": conf_delta,
        "patterns_this_week": patterns_this_week,
        "total_snapshots": total_snapshots,
        "prediction_accuracy": accuracy,
        "medium_accuracy": medium_accuracy,
        "strong_accuracy": strong_accuracy,
        "total_predictions": total_predictions,
        "medium_predictions": medium_total,
        "strong_predictions": strong_total,
        "stock_accuracy": stock_accuracy,
        "stock_predictions": stock_total,
        "crypto_accuracy": crypto_accuracy,
        "crypto_predictions": crypto_total,
        "pending_predictions": pending_predictions,
        "evaluated_snapshots": evaluated_snapshots,
        "oldest_unevaluated": oldest_unevaluated,
        "days_until_first_result": days_until_first_result,
        "pipeline_status": pipeline_status,
        "total_events": total_events,
        "universe_stocks": universe_counts["stocks"],
        "universe_crypto": universe_counts["crypto"],
        "universe_total": universe_counts["total"],
        "last_scan": scan_st.get("last_run"),
        "learning_running": learning_st.get("running", False),
        "vix": vol_regime.get("vix"),
        "vix_regime": vol_regime.get("regime"),
        "vix_label": vol_regime.get("label"),
        "ml_ready": is_model_ready(),
        "ml_accuracy": ml_stats.get("cv_accuracy", 0),
        "ml_samples": ml_stats.get("samples", 0),
        "ml_trained_at": ml_stats.get("trained_at"),
        "ml_feature_importances": ml_stats.get("feature_importances"),
    }


def dedup_existing_patterns(db: Session, user_id: int | None) -> dict[str, Any]:
    """One-time cleanup: merge duplicate active patterns that share the same label prefix."""
    from .portfolio import _pattern_label, _pattern_keywords

    active = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
    ).order_by(TradingInsight.evidence_count.desc()).all()

    groups: dict[str, list[TradingInsight]] = {}
    for ins in active:
        label = _pattern_label(ins.pattern_description).lower().strip()
        kw = frozenset(_pattern_keywords(label))
        placed = False
        for key, members in groups.items():
            existing_kw = frozenset(_pattern_keywords(key))
            if existing_kw and kw:
                overlap = len(kw & existing_kw) / max(1, len(kw | existing_kw))
                if overlap >= 0.5:
                    members.append(ins)
                    placed = True
                    break
        if not placed:
            groups[label] = [ins]

    merged = 0
    deactivated = 0
    for _label, members in groups.items():
        if len(members) <= 1:
            continue
        members.sort(key=lambda i: i.evidence_count, reverse=True)
        keeper = members[0]
        for dup in members[1:]:
            keeper.evidence_count += dup.evidence_count
            keeper.confidence = round(
                min(0.95, max(keeper.confidence, dup.confidence)), 3
            )
            if dup.last_seen and (not keeper.last_seen or dup.last_seen > keeper.last_seen):
                keeper.last_seen = dup.last_seen
            dup.active = False
            deactivated += 1
        keeper.pattern_description = members[0].pattern_description
        merged += 1

    if deactivated:
        db.commit()
        log_learning_event(
            db, user_id, "review",
            f"Pattern cleanup: merged {merged} groups, deactivated {deactivated} duplicates",
        )

    return {
        "groups_merged": merged,
        "duplicates_removed": deactivated,
        "remaining_active": len(active) - deactivated,
    }


def get_accuracy_detail(
    db: Session,
    detail_type: str = "all",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent evaluated predictions with outcomes for drill-down."""
    filled_snaps = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
        MarketSnapshot.predicted_score.isnot(None),
    ).order_by(MarketSnapshot.snapshot_date.desc()).limit(500).all()

    results: list[dict[str, Any]] = []
    for snap in filled_snaps:
        pred_score = snap.predicted_score
        if abs(pred_score) < 0.1:
            continue

        is_crypto = snap.ticker.endswith("-USD")
        if detail_type == "stock" and is_crypto:
            continue
        if detail_type == "crypto" and not is_crypto:
            continue
        if detail_type == "strong" and abs(pred_score) < 3.0:
            continue

        predicted_up = pred_score > 0
        actual_return = snap.future_return_5d or 0
        actual_up = actual_return > 0
        is_hit = predicted_up == actual_up

        results.append({
            "ticker": snap.ticker,
            "date": snap.snapshot_date.isoformat() if snap.snapshot_date else None,
            "predicted_score": round(pred_score, 2),
            "predicted_direction": "bullish" if predicted_up else "bearish",
            "actual_return_5d": round(actual_return, 2),
            "actual_direction": "up" if actual_up else "down",
            "hit": is_hit,
            "close_price": round(snap.close_price, 4) if snap.close_price else None,
        })
        if len(results) >= limit:
            break

    return results


def get_confidence_history(db: Session, user_id: int | None) -> list[dict[str, Any]]:
    insights = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
    ).order_by(TradingInsight.created_at.asc()).all()

    if not insights:
        return []

    points: list[dict[str, Any]] = []
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


# ── Learning Cycle Orchestrator ───────────────────────────────────────

_learning_status: dict[str, Any] = {
    "running": False,
    "last_run": None,
    "last_duration_s": None,
    "phase": "idle",
    "current_step": "",
    "steps_completed": 0,
    "total_steps": 9,
    "patterns_found": 0,
    "tickers_processed": 0,
    "step_timings": {},
}


def get_learning_status() -> dict[str, Any]:
    status = dict(_learning_status)
    if status.get("running") and status.get("started_at"):
        try:
            started = datetime.fromisoformat(status["started_at"])
            status["elapsed_s"] = round((datetime.utcnow() - started).total_seconds(), 1)
        except Exception:
            pass
    return status


def run_learning_cycle(
    db: Session,
    user_id: int | None,
    full_universe: bool = True,
) -> dict[str, Any]:
    """Complete learning cycle: pre-filter -> scan -> snapshot -> backfill -> mine -> backtest -> journal -> signals.

    Uses the prescreener to narrow thousands of tickers to ~200-400
    interesting candidates before deep-scoring, making the cycle 10-30x
    faster than scanning the raw universe.
    """
    from .scanner import run_full_market_scan, _scan_status
    from .journal import daily_market_journal, check_signal_events

    if _learning_status["running"]:
        return {"ok": False, "reason": "Learning cycle already in progress"}
    if _shutting_down.is_set():
        return {"ok": False, "reason": "Server is shutting down"}

    _learning_status["running"] = True
    _learning_status["phase"] = "starting"
    _learning_status["steps_completed"] = 0
    _learning_status["total_steps"] = 14
    _learning_status["patterns_found"] = 0
    _learning_status["tickers_processed"] = 0
    _learning_status["started_at"] = datetime.utcnow().isoformat()
    _learning_status["step_timings"] = {}
    start = time.time()
    report: dict[str, Any] = {}

    def _step_time(name: str, t0: float) -> None:
        elapsed = round(time.time() - t0, 1)
        _learning_status["step_timings"][name] = elapsed
        logger.info(f"[trading] Step '{name}' took {elapsed}s")

    try:
        # Step 1: Pre-filter with FinViz + yfinance screener
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Pre-filtering market"
        _learning_status["phase"] = "pre-filtering"
        from .prescreener import get_prescreened_candidates, get_prescreen_status
        candidates = get_prescreened_candidates()
        ps = get_prescreen_status()
        report["prescreen_candidates"] = len(candidates)
        report["prescreen_sources"] = ps.get("sources", {})
        _learning_status["tickers_processed"] = len(candidates)
        _learning_status["steps_completed"] = 1
        _step_time("pre-filter", step_start)

        # Step 2: Deep-score candidates
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Scanning market"
        _learning_status["phase"] = "scanning"
        scan_results = run_full_market_scan(db, user_id, use_full_universe=full_universe)
        report["tickers_scanned"] = _scan_status["tickers_total"]
        report["tickers_scored"] = len(scan_results)
        _learning_status["tickers_processed"] = len(scan_results)
        _learning_status["steps_completed"] = 2
        _step_time("scan", step_start)

        # Step 3: Snapshots (parallel, top 500 + watchlist)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Taking market snapshots"
        _learning_status["phase"] = "snapshots"
        top_tickers = [r["ticker"] for r in scan_results[:500]]
        watchlist = get_watchlist(db, user_id)
        for w in watchlist:
            if w.ticker not in top_tickers:
                top_tickers.append(w.ticker)
        snap_count = take_snapshots_parallel(db, top_tickers)
        report["snapshots_taken"] = snap_count
        _learning_status["steps_completed"] = 3
        _step_time("snapshots", step_start)

        # Step 4: Backfill future returns + predicted scores
        step_start = time.time()
        _learning_status["current_step"] = "Backfilling future returns"
        _learning_status["phase"] = "backfilling"
        filled = backfill_future_returns(db)
        scores_filled = backfill_predicted_scores(db, limit=1000)
        report["returns_backfilled"] = filled
        report["scores_backfilled"] = scores_filled
        _learning_status["steps_completed"] = 4
        _step_time("backfill", step_start)

        # Step 5: Mine patterns
        _learning_status["current_step"] = "Mining patterns"
        _learning_status["phase"] = "mining"
        step_start = time.time()
        discoveries = mine_patterns(db, user_id)
        report["patterns_discovered"] = len(discoveries)
        _learning_status["patterns_found"] = len(discoveries)
        _learning_status["steps_completed"] = 5
        _step_time("mine", step_start)

        # Step 5b: Active pattern seeking (boost under-sampled patterns)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Active pattern seeking"
        _learning_status["phase"] = "active_seeking"
        seek_result = seek_pattern_data(db, user_id)
        report["patterns_boosted"] = seek_result.get("sought", 0)
        _learning_status["steps_completed"] = 6
        _step_time("active_seek", step_start)

        # Step 6: Backtest discovered patterns (expanded)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Backtesting patterns"
        _learning_status["phase"] = "backtesting"
        bt_count = _auto_backtest_patterns(db, user_id)
        report["backtests_run"] = bt_count
        _learning_status["steps_completed"] = 7
        _step_time("backtest", step_start)

        # Step 8: Self-validation & weight evolution (with dynamic hypotheses)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Testing hypotheses & evolving strategy"
        _learning_status["phase"] = "evolving"
        evolve_result = validate_and_evolve(db, user_id)
        report["hypotheses_tested"] = evolve_result.get("hypotheses_tested", 0)
        report["hypotheses_challenged"] = evolve_result.get("challenged", 0)
        report["real_trade_adjustments"] = evolve_result.get("real_trade_adjustments", 0)
        report["weights_evolved"] = evolve_result.get("weights_evolved", 0)
        _learning_status["steps_completed"] = 8
        _step_time("evolve", step_start)

        # Step 8b: Refine patterns (parameter sweeping)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Refining patterns"
        _learning_status["phase"] = "refining"
        refine_result = refine_patterns(db, user_id)
        report["patterns_refined"] = refine_result.get("refined", 0)
        _learning_status["steps_completed"] = 9
        _step_time("refine", step_start)

        # Step 9: Market journal
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Writing market journal"
        _learning_status["phase"] = "journaling"
        journal = daily_market_journal(db, user_id)
        report["journal_written"] = journal is not None
        _learning_status["steps_completed"] = 10
        _step_time("journal", step_start)

        # Step 10: Signal events
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Checking signal events"
        _learning_status["phase"] = "signals"
        events = check_signal_events(db, user_id)
        report["signal_events"] = len(events)
        _learning_status["steps_completed"] = 11
        _step_time("signals", step_start)

        # Step 11: Train ML model (with regime features)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Training ML model"
        _learning_status["phase"] = "ml_training"
        from .ml_engine import train_model as _train_ml
        ml_result = _train_ml(db)
        report["ml_trained"] = ml_result.get("ok", False)
        report["ml_accuracy"] = ml_result.get("cv_accuracy", 0)
        _learning_status["steps_completed"] = 12
        _step_time("ml_train", step_start)

        # Step 12: Generate strategy proposals
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Generating strategy proposals"
        _learning_status["phase"] = "proposals"
        try:
            from .alerts import generate_strategy_proposals
            proposals = generate_strategy_proposals(db, user_id)
            report["proposals_generated"] = len(proposals)
        except Exception as e:
            logger.warning(f"[trading] Strategy proposal generation failed: {e}")
            report["proposals_generated"] = 0
        _learning_status["steps_completed"] = 13
        _step_time("proposals", step_start)

        # Step 13: Finalize + log
        _learning_status["current_step"] = "Finalizing"
        _learning_status["phase"] = "finalizing"
        elapsed = time.time() - start
        log_learning_event(
            db, user_id, "scan",
            f"Learning cycle: {report.get('prescreen_candidates', 0)} pre-screened, "
            f"scored {report['tickers_scored']}, {report['snapshots_taken']} snapshots, "
            f"{report['patterns_discovered']} patterns, "
            f"{report.get('patterns_boosted', 0)} boosted, "
            f"{report.get('backtests_run', 0)} backtests, "
            f"{report.get('hypotheses_tested', 0)} hypotheses tested "
            f"({report.get('hypotheses_challenged', 0)} challenged), "
            f"{report.get('real_trade_adjustments', 0)} real-trade adjustments, "
            f"{report.get('patterns_refined', 0)} refined, "
            f"{report.get('weights_evolved', 0)} weights evolved, "
            f"{report['signal_events']} signals, "
            f"ML={'trained' if report.get('ml_trained') else 'skipped'}, "
            f"{report.get('proposals_generated', 0)} proposals — {elapsed:.0f}s",
        )
        _learning_status["steps_completed"] = 14

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
        report["step_timings"] = dict(_learning_status.get("step_timings", {}))

    logger.info(f"[trading] Learning cycle finished in {elapsed:.0f}s: {report}")
    return {"ok": True, **report}


def should_run_learning() -> bool:
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
