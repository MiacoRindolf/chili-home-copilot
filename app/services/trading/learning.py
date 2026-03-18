"""Learning: pattern mining, deep study, learning cycles, brain stats."""
from __future__ import annotations

import json
import logging
import os
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
from .market_data import (
    fetch_quote, fetch_quotes_batch, fetch_ohlcv_df, get_indicator_snapshot,
    get_vix, get_volatility_regime, DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS,
    _use_massive, _use_polygon,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights, save_insight, get_trade_stats_by_pattern

logger = logging.getLogger(__name__)

_CPU_COUNT = os.cpu_count() or 4
_IO_WORKERS_HIGH = min(80, max(24, _CPU_COUNT * 3))  # IO-heavy data fetching (64 GB / 32 cores)
_IO_WORKERS_MED = min(48, max(16, _CPU_COUNT * 2))   # mixed IO/CPU work
_IO_WORKERS_LOW = min(32, max(10, _CPU_COUNT))        # lighter parallel tasks

_shutting_down = threading.Event()

# Stale-while-revalidate cache for get_current_predictions
_pred_cache: dict[str, Any] = {"results": [], "ts": 0.0}
_PRED_CACHE_TTL = 180       # 3 min fresh
_PRED_CACHE_STALE_TTL = 600  # 10 min stale-while-revalidate
_pred_refreshing = False
_pred_refresh_lock = threading.Lock()


def signal_shutdown():
    _shutting_down.set()


# ── Learning Event Logger (extracted to learning_events.py) ───────────
from .learning_events import log_learning_event, get_learning_events  # noqa: F401 — re-export for backward compat


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

    _extract_and_store_patterns(db, trade.user_id, reply, existing_insights,
                                trade_won=trade.pnl is not None and trade.pnl > 0)

    add_journal_entry(
        db, trade.user_id,
        content=f"[AI] Trade #{trade.id} ({trade.ticker} {pnl_label} ${trade.pnl}): {reply[:500]}",
        trade_id=trade.id,
    )

    return reply


def _extract_and_store_patterns(
    db: Session, user_id: int | None,
    ai_reply: str, existing_insights: list[TradingInsight],
    trade_won: bool = False,
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
            matched_existing.win_count = (matched_existing.win_count or 0) + (1 if trade_won else 0)
            matched_existing.loss_count = (matched_existing.loss_count or 0) + (0 if trade_won else 1)
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
            save_insight(db, user_id, desc, confidence=max(0.1, min(0.9, conf)),
                         wins=1 if trade_won else 0, losses=0 if trade_won else 1)


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
        df = fetch_ohlcv_df(ticker, period="3mo", interval="1d")
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
    max_workers: int = _IO_WORKERS_HIGH,
) -> int:
    """Take snapshots for many tickers using a thread pool.

    Data fetching runs in parallel; DB writes happen sequentially on the
    calling thread to avoid SQLAlchemy session issues.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Pre-warm the OHLCV cache so indicator computation hits cache.
    # When Massive or Polygon is active the per-ticker cache inside the
    # respective client handles this automatically.
    if not (_use_massive() or _use_polygon()):
        from ..yf_session import batch_download
        BATCH = 100
        for i in range(0, len(tickers), BATCH):
            try:
                batch_download(tickers[i:i + BATCH], period="3mo", interval="1d")
            except Exception:
                pass

    _t0 = time.time()
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

    _fetch_elapsed = round(time.time() - _t0, 1)
    logger.info(
        f"[learning] Snapshot data fetch: {len(fetched)}/{len(tickers)} tickers "
        f"in {_fetch_elapsed}s ({max_workers} workers)"
    )
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
    if not (_use_massive() or _use_polygon()):
        from ..yf_session import batch_download as _bd
        BATCH = 100
        for i in range(0, len(tickers), BATCH):
            try:
                _bd(tickers[i:i + BATCH], period="1mo", interval="1d")
            except Exception:
                pass

    def _fetch_returns(snap):
        try:
            df = fetch_ohlcv_df(
                snap.ticker, interval="1d", period="15d",
                start=str(snap.snapshot_date)[:10],
            )
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

    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    _t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=_workers) as executor:
        futures = {executor.submit(_fetch_returns, s): s for s in unfilled}
        for f in as_completed(futures):
            if _shutting_down.is_set():
                break
            r = f.result()
            if r:
                results.append(r)

    logger.info(
        f"[learning] Backfill returns fetch: {len(results)}/{len(unfilled)} snapshots "
        f"in {time.time() - _t0:.1f}s ({_workers} workers, "
        f"{len(tickers)} unique tickers)"
    )
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

_spy_regime_cache: dict[str, Any] = {"data": {}, "ts": 0.0}
_SPY_REGIME_CACHE_TTL = 600


def _get_historical_regime_map() -> dict[str, dict]:
    """Build a date->regime map from SPY daily data (cached).

    Returns {date_str: {"spy_chg": ..., "spy_mom_5d": ..., "regime": ...}}
    """
    import time as _t

    now = _t.time()
    if _spy_regime_cache["data"] and now - _spy_regime_cache["ts"] < _SPY_REGIME_CACHE_TTL:
        return _spy_regime_cache["data"]

    try:
        spy_df = fetch_ohlcv_df("SPY", period="6mo", interval="1d")
        if spy_df.empty or len(spy_df) < 10:
            return {}
    except Exception:
        return {}

    spy_close = spy_df["Close"]
    regime_map: dict[str, dict] = {}
    for i in range(5, len(spy_df)):
        dt_str = str(spy_df.index[i].date()) if hasattr(spy_df.index[i], "date") else str(spy_df.index[i])[:10]
        chg = (float(spy_close.iloc[i]) - float(spy_close.iloc[i - 1])) / float(spy_close.iloc[i - 1]) * 100
        mom_5d = (float(spy_close.iloc[i]) - float(spy_close.iloc[i - 5])) / float(spy_close.iloc[i - 5]) * 100

        if chg > 0.3 and mom_5d > 0:
            regime = "risk_on"
        elif chg < -0.3 or mom_5d < -2:
            regime = "risk_off"
        else:
            regime = "cautious"

        regime_map[dt_str] = {
            "spy_chg": round(chg, 2),
            "spy_mom_5d": round(mom_5d, 2),
            "regime": regime,
        }

    _spy_regime_cache["data"] = regime_map
    _spy_regime_cache["ts"] = now
    return regime_map


def _mine_from_history(ticker: str) -> list[dict]:
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange
    from .scanner import _detect_resistance_retests, _detect_narrow_range, _detect_vcp

    try:
        df = fetch_ohlcv_df(ticker, period="6mo", interval="1d")
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
    bb_width = bb.bollinger_wband()
    adx = ADXIndicator(high=high, low=low, close=close).adx()
    atr = AverageTrueRange(high=high, low=low, close=close).average_true_range()
    stoch = StochasticOscillator(high=high, low=low, close=close)
    stoch_k = stoch.stoch()
    vol_sma = volume.rolling(20).mean()

    regime_map = _get_historical_regime_map()

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

        # Breakout-specific enrichment
        lookback = min(20, i)
        h_slice = high.iloc[i - lookback:i + 1]
        c_slice = close.iloc[i - lookback:i + 1]
        l_slice = low.iloc[i - lookback:i + 1]
        v_slice = volume.iloc[i - lookback:i + 1]
        resistance = float(h_slice.max()) if len(h_slice) > 0 else price

        try:
            retest_info = _detect_resistance_retests(h_slice, c_slice, resistance, tolerance_pct=1.5, lookback=lookback)
            retest_count = retest_info.get("retest_count", 0)
        except Exception:
            retest_count = 0

        try:
            nr = _detect_narrow_range(h_slice, l_slice)
        except Exception:
            nr = None

        try:
            vcp = _detect_vcp(h_slice, l_slice, v_slice, lookback=lookback)
        except Exception:
            vcp = 0

        bw = float(bb_width.iloc[i]) if pd.notna(bb_width.iloc[i]) else 0.1
        bb_squeeze = bw < 0.04

        dt_str = str(df.index[i].date()) if hasattr(df.index[i], "date") else str(df.index[i])[:10]
        regime_info = regime_map.get(dt_str, {})

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
            "bb_squeeze": bb_squeeze,
            "atr": float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0,
            "stoch_k": float(stoch_k.iloc[i]) if pd.notna(stoch_k.iloc[i]) else 50,
            "stoch_bull_div": stoch_bull_div,
            "stoch_bear_div": stoch_bear_div,
            "above_sma20": price > float(sma20.iloc[i]) if pd.notna(sma20.iloc[i]) else False,
            "ema_20": e20,
            "ema_50": e50,
            "ema_100": e100,
            "ema_stack": (e20 is not None and e50 is not None and e100 is not None
                          and price > e20 > e50 > e100),
            "resistance": round(resistance, 4),
            "resistance_retests": retest_count,
            "narrow_range": nr or "",
            "vcp_count": vcp,
            "is_crypto": ticker.endswith("-USD"),
            "vol_ratio": round(vol_ratio, 2),
            "gap_pct": round(gap_pct, 2),
            "regime": regime_info.get("regime", "unknown"),
            "spy_mom_5d": regime_info.get("spy_mom_5d", 0),
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

    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    _t0 = time.time()
    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=_workers) as executor:
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

    logger.info(
        f"[learning] Pattern mining OHLCV fetch: {len(mine_tickers)} tickers → "
        f"{len(all_rows)} data rows in {time.time() - _t0:.1f}s ({_workers} workers)"
    )

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
            save_insight(db, user_id, pattern, confidence=min(0.9, wr / 100),
                         wins=wins, losses=len(filtered) - wins)

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
    _PROTECTED_ORIGINS = {"user_seeded", "seed", "user", "exit_variant", "entry_variant", "combo_variant"}
    for ins in existing:
        sp = None
        if getattr(ins, "scan_pattern_id", None):
            from ...models.trading import ScanPattern as _SP
            sp = db.query(_SP).get(ins.scan_pattern_id)

        origin = getattr(sp, "origin", None) if sp else None
        if origin in _PROTECTED_ORIGINS:
            continue

        if ins.evidence_count >= 5 and ins.confidence < 0.35:
            if (ins.win_count or 0) > 0 and (ins.win_count or 0) / max(1, (ins.win_count or 0) + (ins.loss_count or 0)) >= 0.4:
                continue
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
                if origin in _PROTECTED_ORIGINS:
                    ins.confidence = max(ins.confidence, 0.15)
                    db.commit()
                    continue
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
    """Parse condition expressions and filter rows.

    Supports:
      - Numeric comparisons: ``rsi > 65``, ``adx <= 20``, ``vol_ratio >= 3``
      - Boolean fields: ``ema_stack == true``, ``bb_squeeze == 1``, ``bb_squeeze == false``
      - String equality: ``narrow_range == NR7``, ``regime == risk_on``
      - Compound AND: ``rsi > 65 and ema_stack == true and resistance_retests >= 3``
      - Field-vs-field: ``macd > macd_sig``
    """
    import re
    parts = re.split(r'\s+and\s+', condition_str.strip())
    filtered = list(rows)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        m = re.match(r'(\w+)\s*([<>=!]+)\s*(.+)', part)
        if not m:
            continue
        field = m.group(1)
        op_str = m.group(2)
        raw_val = m.group(3).strip()

        bool_map = {"true": True, "false": False, "1": True, "0": False}
        if raw_val.lower() in bool_map:
            target_val = bool_map[raw_val.lower()]
            if op_str == "==" or op_str == "=":
                filtered = [r for r in filtered if r.get(field) == target_val]
            elif op_str == "!=" or op_str == "<>":
                filtered = [r for r in filtered if r.get(field) != target_val]
            continue

        try:
            threshold = float(raw_val)
            is_numeric = True
        except ValueError:
            is_numeric = False

        if is_numeric:
            if op_str == "<":
                filtered = [r for r in filtered if _num(r, field) < threshold]
            elif op_str == "<=":
                filtered = [r for r in filtered if _num(r, field) <= threshold]
            elif op_str == ">":
                filtered = [r for r in filtered if _num(r, field) > threshold]
            elif op_str == ">=":
                filtered = [r for r in filtered if _num(r, field) >= threshold]
            elif op_str == "==" or op_str == "=":
                filtered = [r for r in filtered if _num(r, field) == threshold]
            elif op_str == "!=" or op_str == "<>":
                filtered = [r for r in filtered if _num(r, field) != threshold]
        else:
            other_field = raw_val
            if any(r.get(other_field) is not None for r in filtered[:20]):
                if op_str == ">":
                    filtered = [r for r in filtered
                                if _num(r, field) > _num(r, other_field)]
                elif op_str == ">=":
                    filtered = [r for r in filtered
                                if _num(r, field) >= _num(r, other_field)]
                elif op_str == "<":
                    filtered = [r for r in filtered
                                if _num(r, field) < _num(r, other_field)]
                elif op_str == "<=":
                    filtered = [r for r in filtered
                                if _num(r, field) <= _num(r, other_field)]
            else:
                if op_str == "==" or op_str == "=":
                    filtered = [r for r in filtered if str(r.get(field, "")).lower() == raw_val.lower()]
                elif op_str == "!=" or op_str == "<>":
                    filtered = [r for r in filtered if str(r.get(field, "")).lower() != raw_val.lower()]

    return filtered


def _num(row: dict, field: str, default: float = 0.0) -> float:
    """Extract a numeric value from a row, coercing booleans and Nones."""
    v = row.get(field)
    if v is None:
        return default
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


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
    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    extra_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=_workers) as pool:
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
        ins.win_count = (ins.win_count or 0) + wins
        ins.loss_count = (ins.loss_count or 0) + (len(matching) - wins)

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
    """Run smart, diversified backtests to validate discovered patterns.

    Uses the centralized ``smart_backtest_insight`` engine which picks
    contextually relevant tickers across sectors, maps multiple strategies,
    and runs them in parallel.
    """
    from .backtest_engine import smart_backtest_insight

    insights = get_insights(db, user_id, limit=20)
    if not insights:
        return 0

    backtests_run = 0
    for ins in insights[:20]:
        if _shutting_down.is_set():
            break
        old_conf = ins.confidence
        try:
            result = smart_backtest_insight(
                db, ins, target_tickers=25, update_confidence=True,
            )
        except Exception:
            continue

        total = result["total"]
        wins = result["wins"]
        backtests_run += result["backtests_run"]

        if total >= 3 and abs(ins.confidence - old_conf) > 0.01:
            log_learning_event(
                db, user_id, "backtest_validation",
                f"Pattern backtested ({wins}/{total} profitable): "
                f"{ins.pattern_description[:80]} | conf {old_conf:.0%}->{ins.confidence:.0%}",
                confidence_before=old_conf,
                confidence_after=ins.confidence,
                related_insight_id=ins.id,
            )

    logger.info(f"[learning] Auto-backtest: {backtests_run} backtests across {len(insights[:20])} patterns")
    return backtests_run


def validate_and_evolve(db: Session, user_id: int | None) -> dict[str, Any]:
    """Dynamically test hypotheses from the TradingHypothesis table against real data.

    On first run, seeds the table with the original hardcoded hypotheses.
    Every subsequent run loads all pending/testing hypotheses, evaluates them
    against mined historical rows, and updates their lifecycle state.
    The brain's perspective grows as the data grows — no hardcoded ceiling.
    """
    from .market_data import ALL_SCAN_TICKERS
    from ...models.trading import TradingHypothesis

    mine_tickers = list(ALL_SCAN_TICKERS)[:200]

    rows: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futs = {pool.submit(_mine_from_history, t): t for t in mine_tickers}
        for f in as_completed(futs):
            try:
                rows.extend(f.result())
            except Exception:
                pass

    if len(rows) < 30:
        return {"tested": 0, "note": "insufficient data for self-validation"}

    # ── Seed built-in hypotheses on first run ──
    existing_count = db.query(TradingHypothesis).count()
    if existing_count == 0:
        _seed_builtin_hypotheses(db)

    # ── Auto-derive hypotheses from active ScanPatterns ──
    try:
        _derive_hypotheses_from_patterns(db)
    except Exception as e:
        logger.warning(f"[learning] Pattern-derived hypothesis generation failed: {e}")

    # ── Migrate any old-format TradingInsight hypotheses ──
    try:
        _migrate_legacy_hypotheses(db, user_id)
    except Exception:
        pass

    # ── Load all testable hypotheses ──
    hypotheses = (
        db.query(TradingHypothesis)
        .filter(TradingHypothesis.status.in_(["pending", "testing", "confirmed", "rejected"]))
        .order_by(TradingHypothesis.times_tested.asc())
        .limit(50)
        .all()
    )

    results: list[dict[str, Any]] = []

    for hyp in hypotheses:
        group_a = _filter_rows_by_condition(rows, hyp.condition_a)
        group_b = _filter_rows_by_condition(rows, hyp.condition_b)

        if len(group_a) < 5 or len(group_b) < 5:
            continue

        avg_a = sum(r["ret_5d"] for r in group_a) / len(group_a)
        avg_b = sum(r["ret_5d"] for r in group_b) / len(group_b)
        wr_a = sum(1 for r in group_a if r["ret_5d"] > 0) / len(group_a) * 100
        wr_b = sum(1 for r in group_b if r["ret_5d"] > 0) / len(group_b) * 100

        if hyp.expected_winner == "a":
            confirmed = avg_a > avg_b
        else:
            confirmed = avg_b > avg_a

        finding = {
            "hypothesis": hyp.description,
            "hypothesis_id": hyp.id,
            "confirmed": confirmed,
            "group_a_avg": round(avg_a, 3),
            "group_b_avg": round(avg_b, 3),
            "group_a_wr": round(wr_a, 1),
            "group_b_wr": round(wr_b, 1),
            "group_a_n": len(group_a),
            "group_b_n": len(group_b),
        }
        results.append(finding)

        hyp.times_tested = (hyp.times_tested or 0) + 1
        if confirmed:
            hyp.times_confirmed = (hyp.times_confirmed or 0) + 1
        else:
            hyp.times_rejected = (hyp.times_rejected or 0) + 1
        hyp.status = "testing"
        hyp.last_tested_at = datetime.utcnow()
        hyp.last_result_json = json.dumps(finding)

        confirm_rate = (hyp.times_confirmed or 0) / max(1, hyp.times_tested)
        if hyp.times_tested >= 5:
            if confirm_rate >= 0.7:
                hyp.status = "confirmed"
            elif confirm_rate < 0.3:
                hyp.status = "rejected"
        if hyp.times_tested >= 10 and confirm_rate < 0.4:
            hyp.status = "retired"

        if not confirmed:
            wins_a = sum(1 for r in group_a if r["ret_5d"] > 0)
            save_insight(
                db, user_id,
                f"CHILI challenge: {hyp.description} — data says otherwise "
                f"(A: {avg_a:+.2f}%/5d {wr_a:.0f}%wr vs B: {avg_b:+.2f}%/5d {wr_b:.0f}%wr, "
                f"n={len(group_a)}+{len(group_b)})",
                confidence=0.45,
                wins=wins_a, losses=len(group_a) - wins_a,
            )
            log_learning_event(
                db, user_id, "hypothesis_challenged",
                f"Data challenges: {hyp.description} | "
                f"Expected {'A' if hyp.expected_winner == 'a' else 'B'} wins, "
                f"but {'B' if hyp.expected_winner == 'a' else 'A'} actually performed better "
                f"(tested {hyp.times_tested}x, confirm rate {confirm_rate:.0%})",
            )
        else:
            if avg_a > 0 and wr_a > 55:
                wins_a = sum(1 for r in group_a if r["ret_5d"] > 0)
                save_insight(
                    db, user_id,
                    f"CHILI validated: {hyp.description} — confirmed by data "
                    f"({avg_a:+.2f}%/5d, {wr_a:.0f}%wr, {len(group_a)} samples, "
                    f"tested {hyp.times_tested}x, {confirm_rate:.0%} confirm rate)",
                    confidence=min(0.85, wr_a / 100),
                    wins=wins_a, losses=len(group_a) - wins_a,
                )

    db.commit()

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
                        ins.win_count = ps.get("wins", 0) or 0
                        ins.loss_count = (ps.get("trades", 0) or 0) - (ps.get("wins", 0) or 0)
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

    # ── Evolve scoring weights ──
    from .scanner import evolve_strategy_weights
    weight_result = evolve_strategy_weights(db)

    confirmed_count = sum(1 for r in results if r.get("confirmed"))
    challenged_count = sum(1 for r in results if not r.get("confirmed", True))

    total_hyp = db.query(TradingHypothesis).count()
    active_hyp = db.query(TradingHypothesis).filter(
        TradingHypothesis.status.in_(["pending", "testing", "confirmed"])
    ).count()

    log_learning_event(
        db, user_id, "self_validation",
        f"Tested {len(results)} hypotheses (of {total_hyp} total, {active_hyp} active): "
        f"{confirmed_count} confirmed, {challenged_count} challenged by data. "
        f"Real-trade adjustments: {real_trade_adjustments}. "
        f"Evolved {weight_result.get('adjusted', 0)} scoring weights.",
    )

    logger.info(
        f"[learning] Dynamic self-validation: {len(results)} hypotheses tested "
        f"({total_hyp} total in pool, {active_hyp} active), "
        f"{confirmed_count} confirmed, {challenged_count} challenged, "
        f"{real_trade_adjustments} real-trade adjustments, "
        f"{weight_result.get('adjusted', 0)} weights evolved"
    )

    return {
        "hypotheses_tested": len(results),
        "hypotheses_in_pool": total_hyp,
        "hypotheses_active": active_hyp,
        "confirmed": confirmed_count,
        "challenged": challenged_count,
        "real_trade_adjustments": real_trade_adjustments,
        "weights_evolved": weight_result.get("adjusted", 0),
        "details": results,
    }


_BUILTIN_SEED_HYPOTHESES: list[dict[str, str]] = [
    {"description": "MACD positive entries outperform MACD negative entries",
     "condition_a": "macd > macd_sig and macd_hist > 0",
     "condition_b": "macd < macd_sig and macd_hist < 0",
     "expected_winner": "a", "related_weight": "macd_positive_bonus"},
    {"description": "High relative volume (>3x) entries outperform low volume (<1.5x)",
     "condition_a": "vol_ratio > 3.0",
     "condition_b": "vol_ratio < 1.5",
     "expected_winner": "a", "related_weight": "vol_surge_3x"},
    {"description": "Strong trends (ADX>25) outperform range-bound (ADX<15)",
     "condition_a": "adx > 25",
     "condition_b": "adx < 15",
     "expected_winner": "a", "related_weight": "bo_adx_trending"},
    {"description": "Bullish EMA stack outperforms broken EMA alignment",
     "condition_a": "ema_stack == true",
     "condition_b": "ema_stack == false",
     "expected_winner": "a", "related_weight": "bo_ema_support"},
    {"description": "Oversold RSI (<30) mean-reversion outperforms neutral RSI entries",
     "condition_a": "rsi < 30",
     "condition_b": "rsi >= 40 and rsi <= 60",
     "expected_winner": "a"},
    {"description": "MACD histogram positive outperforms negative in neutral RSI zone",
     "condition_a": "macd_hist > 0 and rsi > 40 and rsi < 65",
     "condition_b": "macd_hist < 0 and rsi > 40 and rsi < 65",
     "expected_winner": "a"},
    {"description": "BB squeeze + declining volume outperforms BB squeeze + rising volume",
     "condition_a": "bb_pct < 0.20 and vol_ratio < 0.8",
     "condition_b": "bb_pct < 0.20 and vol_ratio > 1.5",
     "expected_winner": "a"},
    {"description": "Consolidating (ADX<20) squeeze outperforms trending (ADX>25) squeeze",
     "condition_a": "bb_pct < 0.20 and adx < 20",
     "condition_b": "bb_pct < 0.20 and adx > 25",
     "expected_winner": "a", "related_weight": "bo_adx_consolidating"},
    {"description": "BB squeeze + bullish EMA stack outperforms squeeze + broken EMAs",
     "condition_a": "bb_pct < 0.20 and ema_stack == true",
     "condition_b": "bb_pct < 0.20 and ema_stack == false",
     "expected_winner": "a"},
    {"description": "MACD positive in risk_on outperforms MACD positive in risk_off",
     "condition_a": "macd > macd_sig and macd_hist > 0 and regime == risk_on",
     "condition_b": "macd > macd_sig and macd_hist > 0 and regime == risk_off",
     "expected_winner": "a"},
    {"description": "EMA stack in risk_on outperforms EMA stack in risk_off",
     "condition_a": "ema_stack == true and regime == risk_on",
     "condition_b": "ema_stack == true and regime == risk_off",
     "expected_winner": "a"},
    {"description": "BB squeeze in bullish SPY momentum outperforms squeeze in bearish SPY momentum",
     "condition_a": "bb_pct < 0.20 and spy_mom_5d > 1",
     "condition_b": "bb_pct < 0.20 and spy_mom_5d < -1",
     "expected_winner": "a"},
    {"description": "High volume in risk_off outperforms low volume in risk_off (flight-to-quality)",
     "condition_a": "vol_ratio > 2.0 and regime == risk_off",
     "condition_b": "vol_ratio < 1.0 and regime == risk_off",
     "expected_winner": "a"},
    {"description": "RSI momentum + EMA stack + resistance retests outperforms RSI momentum + EMA stack alone",
     "condition_a": "rsi > 65 and ema_stack == true and resistance_retests >= 3",
     "condition_b": "rsi > 65 and ema_stack == true and resistance_retests < 3",
     "expected_winner": "a", "related_weight": "bo_retest_pressure"},
    {"description": "VCP (2+ contractions) near resistance outperforms no VCP near resistance",
     "condition_a": "vcp_count >= 2 and bb_pct < 0.30",
     "condition_b": "vcp_count < 1 and bb_pct < 0.30",
     "expected_winner": "a"},
]


def _seed_builtin_hypotheses(db: Session) -> int:
    """Seed the TradingHypothesis table with initial hypotheses on first run."""
    from ...models.trading import TradingHypothesis
    added = 0
    for h in _BUILTIN_SEED_HYPOTHESES:
        hyp = TradingHypothesis(
            description=h["description"],
            condition_a=h["condition_a"],
            condition_b=h["condition_b"],
            expected_winner=h.get("expected_winner", "a"),
            origin="builtin_seed",
            status="pending",
            related_weight=h.get("related_weight"),
        )
        db.add(hyp)
        added += 1
    db.commit()
    logger.info(f"[learning] Seeded {added} builtin hypotheses")
    return added


def _migrate_legacy_hypotheses(db: Session, user_id: int | None) -> int:
    """Migrate old TradingInsight hypothesis:... entries into TradingHypothesis."""
    from ...models.trading import TradingHypothesis

    legacy = db.query(TradingInsight).filter(
        TradingInsight.active.is_(True),
        TradingInsight.pattern_description.like("hypothesis:%"),
    ).limit(20).all()

    migrated = 0
    for ins in legacy:
        desc = ins.pattern_description
        parts = desc.split("|")
        if len(parts) < 3:
            ins.active = False
            continue

        label = parts[0].replace("hypothesis:", "").strip()
        cond_a = parts[1].strip().replace("A:", "").strip()
        cond_b = parts[2].strip().replace("B:", "").strip()
        expected = "a"
        if len(parts) >= 4 and "b" in parts[3].strip().lower():
            expected = "b"

        existing = db.query(TradingHypothesis).filter_by(description=label).first()
        if not existing:
            hyp = TradingHypothesis(
                description=label,
                condition_a=cond_a,
                condition_b=cond_b,
                expected_winner=expected,
                origin="llm_generated",
                status="pending",
            )
            db.add(hyp)
            migrated += 1

        ins.active = False

    if migrated:
        db.commit()
        logger.info(f"[learning] Migrated {migrated} legacy hypothesis insights")
    return migrated


def _derive_hypotheses_from_patterns(db: Session) -> int:
    """Auto-generate A/B hypotheses from active ScanPatterns.

    For each ScanPattern with conditions, we create a hypothesis that tests
    whether the FULL pattern outperforms a version with one key condition removed.
    This makes every user/brain/web-discovered pattern automatically testable.
    """
    from ...models.trading import ScanPattern, TradingHypothesis

    patterns = db.query(ScanPattern).filter_by(active=True).all()
    existing_pattern_ids = {
        h.related_pattern_id
        for h in db.query(TradingHypothesis).filter(
            TradingHypothesis.related_pattern_id.isnot(None)
        ).all()
    }

    _OP_MAP = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "==", "!=": "!="}
    _OP_NEGATE = {">": "<=", ">=": "<", "<": ">=", "<=": ">", "==": "!="}

    added = 0
    for p in patterns:
        if p.id in existing_pattern_ids:
            continue

        try:
            rules = json.loads(p.rules_json or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        conditions = rules.get("conditions", [])
        if len(conditions) < 2:
            continue

        parts_a: list[str] = []
        for cond in conditions:
            ind = cond.get("indicator", "")
            op = _OP_MAP.get(cond.get("op", ""), "==")
            ref = cond.get("ref")
            val = cond.get("value")

            if ind == "price" and ref:
                parts_a.append(f"ema_stack == true")
                continue
            if isinstance(val, bool):
                parts_a.append(f"{ind} == {'true' if val else 'false'}")
            elif isinstance(val, list) and len(val) == 2:
                parts_a.append(f"{ind} >= {val[0]} and {ind} <= {val[1]}")
            elif isinstance(val, list):
                parts_a.append(f"{ind} == {val[0]}")
            else:
                parts_a.append(f"{ind} {op} {val}")

        if not parts_a:
            continue

        condition_a = " and ".join(parts_a)

        drop_idx = len(conditions) - 1
        last_cond = conditions[drop_idx]
        last_ind = last_cond.get("indicator", "")
        last_op = last_cond.get("op", ">")
        last_val = last_cond.get("value")
        last_ref = last_cond.get("ref")

        parts_b = list(parts_a[:-1])
        if last_ind == "price" and last_ref:
            parts_b.append("ema_stack == false")
        elif last_op in _OP_NEGATE:
            neg_op = _OP_NEGATE[last_op]
            if isinstance(last_val, bool):
                parts_b.append(f"{last_ind} == {'false' if last_val else 'true'}")
            elif isinstance(last_val, list):
                pass
            else:
                parts_b.append(f"{last_ind} {neg_op} {last_val}")
        else:
            parts_b.append(f"{last_ind} == 0")

        if not parts_b:
            continue

        condition_b = " and ".join(parts_b)
        description = f"{p.name}: full pattern outperforms partial (without {last_ind} condition)"

        hyp = TradingHypothesis(
            description=description,
            condition_a=condition_a,
            condition_b=condition_b,
            expected_winner="a",
            origin="pattern_derived",
            status="pending",
            related_pattern_id=p.id,
        )
        db.add(hyp)
        added += 1

    if added:
        db.commit()
        logger.info(f"[learning] Derived {added} hypotheses from ScanPatterns")
    return added


# ── Intraday Breakout Pattern Mining (15m data) ──────────────────────

def _mine_intraday_breakout_patterns(ticker: str) -> list[dict]:
    """Mine 15m OHLCV for short-term breakout patterns (minutes to hours).

    Returns rows of indicator + pattern states with 4h and 8h forward returns.
    """
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange

    try:
        df = fetch_ohlcv_df(ticker, period="5d", interval="15m")
        if df.empty or len(df) < 80:
            return []
    except Exception:
        return []

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    rsi = RSIIndicator(close=close, window=14).rsi()
    macd_obj = MACD(close=close)
    macd_hist = macd_obj.macd_diff()
    ema9 = EMAIndicator(close=close, window=9).ema_indicator()
    ema21 = EMAIndicator(close=close, window=21).ema_indicator()
    bb = BollingerBands(close=close, window=20, window_dev=2)
    bb_width = bb.bollinger_wband()
    adx = ADXIndicator(high=high, low=low, close=close).adx()
    atr = AverageTrueRange(high=high, low=low, close=close).average_true_range()
    stoch = StochasticOscillator(high=high, low=low, close=close)
    stoch_k = stoch.stoch()
    vol_sma = volume.rolling(20).mean()

    rows = []
    bars_4h = 16   # 4h / 15m = 16 bars
    bars_8h = 32

    for i in range(50, len(df) - bars_8h):
        price = float(close.iloc[i])
        if price <= 0:
            continue

        ret_4h = (float(close.iloc[i + bars_4h]) - price) / price * 100
        ret_8h = (float(close.iloc[i + bars_8h]) - price) / price * 100

        bw = float(bb_width.iloc[i]) if pd.notna(bb_width.iloc[i]) else 0
        bw_pct = 0.5
        if i >= 50:
            bw_window = bb_width.iloc[i - 49:i + 1].dropna()
            if len(bw_window) > 10 and bw > 0:
                bw_pct = float((bw_window < bw).sum() / len(bw_window))

        bb_squeeze = bw_pct < 0.20

        vol_ratio = 1.0
        if pd.notna(vol_sma.iloc[i]) and float(vol_sma.iloc[i]) > 0:
            vol_ratio = float(volume.iloc[i]) / float(vol_sma.iloc[i])

        e9 = float(ema9.iloc[i]) if pd.notna(ema9.iloc[i]) else None
        e21 = float(ema21.iloc[i]) if pd.notna(ema21.iloc[i]) else None
        ema_bullish = e9 is not None and e21 is not None and price > e9 > e21

        current_range = float(high.iloc[i]) - float(low.iloc[i])
        nr7 = False
        if i >= 7 and current_range > 0:
            prev_ranges = [float(high.iloc[i - j]) - float(low.iloc[i - j]) for j in range(1, 7)]
            nr7 = current_range <= min(prev_ranges) if prev_ranges else False

        atr_val = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0
        atr_compressed = False
        if i >= 50 and atr_val > 0:
            atr_window = atr.iloc[i - 49:i + 1].dropna()
            if len(atr_window) > 10:
                atr_compressed = atr_val <= float(atr_window.quantile(0.25))

        rows.append({
            "ticker": ticker,
            "price": price,
            "ret_4h": round(ret_4h, 3),
            "ret_8h": round(ret_8h, 3),
            "rsi": float(rsi.iloc[i]) if pd.notna(rsi.iloc[i]) else 50,
            "macd_hist": float(macd_hist.iloc[i]) if pd.notna(macd_hist.iloc[i]) else 0,
            "adx": float(adx.iloc[i]) if pd.notna(adx.iloc[i]) else 0,
            "stoch_k": float(stoch_k.iloc[i]) if pd.notna(stoch_k.iloc[i]) else 50,
            "bb_squeeze": bb_squeeze,
            "vol_ratio": round(vol_ratio, 2),
            "ema_bullish": ema_bullish,
            "nr7": nr7,
            "atr_compressed": atr_compressed,
            "is_crypto": ticker.endswith("-USD"),
        })
    return rows


def mine_intraday_patterns(db: Session, user_id: int | None) -> dict[str, Any]:
    """Run intraday pattern mining on 15m data for breakout-specific learning."""
    from .market_data import DEFAULT_CRYPTO_TICKERS, DEFAULT_SCAN_TICKERS
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tickers = list(DEFAULT_CRYPTO_TICKERS)[:30] + list(DEFAULT_SCAN_TICKERS)[:30]

    rows: list[dict] = []
    _workers = _IO_WORKERS_LOW
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futs = {pool.submit(_mine_intraday_breakout_patterns, t): t for t in tickers}
        for f in as_completed(futs):
            try:
                rows.extend(f.result())
            except Exception:
                pass

    if len(rows) < 30:
        return {"tested": 0, "note": "insufficient intraday data"}

    discoveries = 0

    # Hypothesis: BB squeeze -> 4h positive returns
    sq = [r for r in rows if r["bb_squeeze"]]
    no_sq = [r for r in rows if not r["bb_squeeze"]]
    if len(sq) >= 10 and len(no_sq) >= 10:
        avg_sq = sum(r["ret_4h"] for r in sq) / len(sq)
        avg_no = sum(r["ret_4h"] for r in no_sq) / len(no_sq)
        wr_sq = sum(1 for r in sq if r["ret_4h"] > 0) / len(sq) * 100
        if avg_sq > avg_no and avg_sq > 0.1:
            w = sum(1 for r in sq if r["ret_4h"] > 0)
            save_insight(
                db, user_id,
                f"Intraday: BB squeeze -> {avg_sq:+.2f}% avg 4h return, "
                f"{wr_sq:.0f}%wr (n={len(sq)}) vs non-squeeze {avg_no:+.2f}%",
                confidence=min(0.80, wr_sq / 100),
                wins=w, losses=len(sq) - w,
            )
            discoveries += 1

    # Hypothesis: BB squeeze + volume declining -> better breakouts
    sq_vol_low = [r for r in rows if r["bb_squeeze"] and r["vol_ratio"] < 0.8]
    sq_vol_high = [r for r in rows if r["bb_squeeze"] and r["vol_ratio"] > 1.5]
    if len(sq_vol_low) >= 5 and len(sq_vol_high) >= 5:
        avg_low = sum(r["ret_4h"] for r in sq_vol_low) / len(sq_vol_low)
        avg_high = sum(r["ret_4h"] for r in sq_vol_high) / len(sq_vol_high)
        w_low = sum(1 for r in sq_vol_low if r["ret_4h"] > 0)
        save_insight(
            db, user_id,
            f"Intraday: squeeze + low vol {avg_low:+.2f}%/4h "
            f"vs squeeze + high vol {avg_high:+.2f}%/4h "
            f"(n={len(sq_vol_low)}+{len(sq_vol_high)})",
            confidence=0.5,
            wins=w_low, losses=len(sq_vol_low) - w_low,
        )
        discoveries += 1

    # Hypothesis: NR7 -> expansion profitable within 8h
    nr7s = [r for r in rows if r["nr7"]]
    if len(nr7s) >= 10:
        avg_nr7 = sum(r["ret_8h"] for r in nr7s) / len(nr7s)
        wr_nr7 = sum(1 for r in nr7s if r["ret_8h"] > 0) / len(nr7s) * 100
        w_nr7 = sum(1 for r in nr7s if r["ret_8h"] > 0)
        save_insight(
            db, user_id,
            f"Intraday: NR7 (narrow range 7) -> {avg_nr7:+.2f}% avg 8h return, "
            f"{wr_nr7:.0f}%wr (n={len(nr7s)})",
            confidence=min(0.75, wr_nr7 / 100),
            wins=w_nr7, losses=len(nr7s) - w_nr7,
        )
        discoveries += 1

    # Hypothesis: ATR compressed + EMA bullish -> breakout outperforms
    coiled = [r for r in rows if r["atr_compressed"] and r["ema_bullish"]]
    if len(coiled) >= 5:
        avg_coil = sum(r["ret_4h"] for r in coiled) / len(coiled)
        wr_coil = sum(1 for r in coiled if r["ret_4h"] > 0) / len(coiled) * 100
        w_coil = sum(1 for r in coiled if r["ret_4h"] > 0)
        save_insight(
            db, user_id,
            f"Intraday: ATR compressed + EMA bullish = coiled spring, "
            f"{avg_coil:+.2f}%/4h, {wr_coil:.0f}%wr (n={len(coiled)})",
            confidence=min(0.80, wr_coil / 100),
            wins=w_coil, losses=len(coiled) - w_coil,
        )
        discoveries += 1

    # Hypothesis: RSI 40-65 zone outperforms extremes in squeeze context
    sq_rsi_mid = [r for r in rows if r["bb_squeeze"] and 40 <= r["rsi"] <= 65]
    sq_rsi_ext = [r for r in rows if r["bb_squeeze"] and (r["rsi"] < 30 or r["rsi"] > 70)]
    if len(sq_rsi_mid) >= 5 and len(sq_rsi_ext) >= 5:
        avg_mid = sum(r["ret_4h"] for r in sq_rsi_mid) / len(sq_rsi_mid)
        avg_ext = sum(r["ret_4h"] for r in sq_rsi_ext) / len(sq_rsi_ext)
        w_mid = sum(1 for r in sq_rsi_mid if r["ret_4h"] > 0)
        save_insight(
            db, user_id,
            f"Intraday: squeeze + RSI 40-65 {avg_mid:+.2f}%/4h vs "
            f"squeeze + extreme RSI {avg_ext:+.2f}%/4h",
            confidence=0.55,
            wins=w_mid, losses=len(sq_rsi_mid) - w_mid,
        )
        discoveries += 1

    # Crypto vs Stock breakout comparison
    crypto_rows = [r for r in rows if r["is_crypto"] and r["bb_squeeze"]]
    stock_rows = [r for r in rows if not r["is_crypto"] and r["bb_squeeze"]]
    if len(crypto_rows) >= 10 and len(stock_rows) >= 10:
        avg_crypto = sum(r["ret_4h"] for r in crypto_rows) / len(crypto_rows)
        avg_stock = sum(r["ret_4h"] for r in stock_rows) / len(stock_rows)
        save_insight(
            db, user_id,
            f"Intraday squeeze: crypto {avg_crypto:+.2f}%/4h vs "
            f"stocks {avg_stock:+.2f}%/4h (n={len(crypto_rows)}+{len(stock_rows)})",
            confidence=0.5,
        )
        discoveries += 1

    log_learning_event(
        db, user_id, "intraday_pattern_mining",
        f"Mined {len(rows)} intraday bars from {len(tickers)} tickers, "
        f"{discoveries} breakout pattern discoveries",
    )

    return {
        "rows_mined": len(rows),
        "tickers": len(tickers),
        "discoveries": discoveries,
    }


# ── Breakout Outcome Learning ──────────────────────────────────────────

def learn_from_breakout_outcomes(db: Session, user_id: int | None) -> dict[str, Any]:
    """Compute per-pattern win rates from resolved BreakoutAlert outcomes
    and feed them back into TradingInsight records for weight evolution.
    """
    from ...models.trading import BreakoutAlert

    try:
        cutoff = datetime.utcnow() - timedelta(days=180)
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.resolved_at >= cutoff,
        ).order_by(BreakoutAlert.resolved_at.desc()).limit(500).all()
    except Exception:
        return {"patterns_learned": 0}

    if len(resolved) < 3:
        return {"patterns_learned": 0, "note": "insufficient resolved alerts"}

    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for alert in resolved:
        key = f"{alert.asset_type}|{alert.alert_tier}"
        groups[key].append(alert)

    patterns_created = 0
    for key, alerts in groups.items():
        if len(alerts) < 3:
            continue
        asset_type, tier = key.split("|", 1)
        winners = sum(1 for a in alerts if a.outcome == "winner")
        fakeouts = sum(1 for a in alerts if a.outcome == "fakeout")
        total = len(alerts)
        win_rate = winners / total * 100
        avg_gain = sum(
            (a.max_gain_pct or 0) for a in alerts
        ) / total
        avg_dd = sum(
            (a.max_drawdown_pct or 0) for a in alerts
        ) / total

        desc = (
            f"Breakout outcome: {asset_type} {tier} — "
            f"{win_rate:.0f}% win rate ({winners}/{total}), "
            f"avg peak gain {avg_gain:+.1f}%, avg max DD {avg_dd:+.1f}%, "
            f"fakeout rate {fakeouts/total*100:.0f}%"
        )
        confidence = min(0.90, win_rate / 100)

        existing = db.query(TradingInsight).filter(
            TradingInsight.user_id == user_id,
            TradingInsight.pattern_description.like(f"%{asset_type} {tier}%"),
            TradingInsight.pattern_description.like("Breakout outcome:%"),
            TradingInsight.active.is_(True),
        ).first()

        if existing:
            existing.confidence = round(
                existing.confidence * 0.4 + confidence * 0.6, 3
            )
            existing.evidence_count = total
            existing.win_count = winners
            existing.loss_count = total - winners
            existing.pattern_description = desc
            existing.last_seen = datetime.utcnow()
        else:
            save_insight(db, user_id, desc, confidence=confidence,
                         wins=winners, losses=total - winners)

        patterns_created += 1

        log_learning_event(
            db, user_id, "breakout_outcome_learning",
            f"{asset_type} {tier}: {win_rate:.0f}%wr ({total} alerts), "
            f"avg gain {avg_gain:+.1f}%, fakeout {fakeouts/total*100:.0f}%",
        )

    return {
        "patterns_learned": patterns_created,
        "total_resolved": len(resolved),
    }


# ── Exit Optimization Learning ────────────────────────────────────────

def learn_exit_optimization(db: Session, user_id: int | None) -> dict[str, Any]:
    """Analyze time-to-peak, time-to-stop, and trailing stop data to
    recommend ATR multiplier adjustments for stops and targets."""
    from ...models.trading import BreakoutAlert

    try:
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.time_to_peak_hours.isnot(None),
        ).all()
    except Exception:
        return {"adjustments": 0}

    if len(resolved) < 5:
        return {"adjustments": 0, "note": "insufficient data"}

    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for a in resolved:
        groups[f"{a.asset_type}|{a.alert_tier}"].append(a)

    adjustments = 0
    for key, alerts in groups.items():
        if len(alerts) < 5:
            continue
        asset_type, tier = key.split("|", 1)

        peaks = [a.time_to_peak_hours for a in alerts if a.time_to_peak_hours is not None]
        stops = [a.time_to_stop_hours for a in alerts if a.time_to_stop_hours is not None]
        winners = [a for a in alerts if a.outcome == "winner"]
        losers = [a for a in alerts if a.outcome == "loser"]

        if peaks:
            median_peak = sorted(peaks)[len(peaks) // 2]

            if median_peak < 2 and asset_type == "crypto":
                desc = (
                    f"Exit optimization: {asset_type} {tier} — median time-to-peak "
                    f"is {median_peak:.1f}h. Consider tighter crypto_bo_target_atr_mult "
                    f"targets for faster profit-taking."
                )
                save_insight(db, user_id, desc, confidence=0.6,
                             wins=len(winners), losses=len(losers))
                adjustments += 1

        if stops and losers:
            fast_stops = sum(1 for s in stops if s < 1)
            if fast_stops / len(stops) > 0.5:
                prefix = "crypto_bo" if asset_type == "crypto" else "bo"
                desc = (
                    f"Exit optimization: {asset_type} {tier} — {fast_stops}/{len(stops)} "
                    f"alerts hit stop within 1h. Consider widening {prefix}_stop_atr_mult."
                )
                save_insight(db, user_id, desc, confidence=0.55,
                             wins=len(winners), losses=len(losers))
                adjustments += 1

        # Optimal exit vs actual outcome
        opt_exits = [a.optimal_exit_pct for a in alerts if a.optimal_exit_pct is not None]
        actual_gains = [a.max_gain_pct for a in winners if a.max_gain_pct is not None]
        if opt_exits and actual_gains:
            avg_opt = sum(opt_exits) / len(opt_exits)
            avg_actual = sum(actual_gains) / len(actual_gains)
            if avg_opt > avg_actual * 0.8 and avg_opt > 1.0:
                desc = (
                    f"Exit optimization: {asset_type} {tier} — trailing stop would "
                    f"capture avg {avg_opt:.1f}% vs actual avg peak {avg_actual:.1f}%. "
                    f"Trailing stop strategy is recommended."
                )
                save_insight(db, user_id, desc, confidence=0.65,
                             wins=len(winners), losses=len(alerts) - len(winners))
                adjustments += 1

    if adjustments:
        log_learning_event(
            db, user_id, "exit_optimization",
            f"Generated {adjustments} exit optimization insights from {len(resolved)} alerts",
        )

    return {"adjustments": adjustments, "alerts_analyzed": len(resolved)}


# ── Fakeout Pattern Mining ────────────────────────────────────────────

def mine_fakeout_patterns(db: Session, user_id: int | None) -> dict[str, Any]:
    """Mine indicator states that commonly precede fakeout outcomes and
    create insights so the brain learns to penalize them."""
    import json as _json
    from ...models.trading import BreakoutAlert

    try:
        fakeouts = db.query(BreakoutAlert).filter(BreakoutAlert.outcome == "fakeout").all()
        winners = db.query(BreakoutAlert).filter(BreakoutAlert.outcome == "winner").all()
    except Exception:
        return {"patterns_found": 0}

    if len(fakeouts) < 3 or len(winners) < 3:
        return {"patterns_found": 0, "note": "insufficient fakeout/winner data"}

    def _parse_indicators(alerts):
        parsed = []
        for a in alerts:
            try:
                ind = _json.loads(a.indicator_snapshot) if a.indicator_snapshot else {}
                parsed.append(ind)
            except Exception:
                pass
        return parsed

    fakeout_inds = _parse_indicators(fakeouts)
    winner_inds = _parse_indicators(winners)

    patterns_found = 0

    def _check_condition(inds, condition_fn):
        return sum(1 for i in inds if condition_fn(i)) / max(len(inds), 1) * 100

    conditions = [
        ("RSI > 65 at alert", lambda i: (i.get("rsi") or 50) > 65, "overbought squeeze fakeout"),
        ("RVOL < 1.0", lambda i: (i.get("rvol") or 1.0) < 1.0, "low volume fakeout"),
        ("ADX > 30", lambda i: (i.get("adx") or 0) > 30, "trending squeeze fakeout"),
        ("BB width narrow (<0.02)", lambda i: (i.get("bb_width") or 1.0) < 0.02, "extremely narrow range fakeout"),
    ]

    for label, cond, keyword in conditions:
        fakeout_pct = _check_condition(fakeout_inds, cond)
        winner_pct = _check_condition(winner_inds, cond)

        if fakeout_pct > winner_pct + 15 and fakeout_pct > 30:
            desc = (
                f"Fakeout pattern: {label} occurs in {fakeout_pct:.0f}% of fakeouts "
                f"vs {winner_pct:.0f}% of winners — {keyword}"
            )
            save_insight(db, user_id, desc, confidence=0.55,
                         wins=len(winner_inds), losses=len(fakeout_inds))
            patterns_found += 1

    # Signal combination analysis
    from collections import Counter
    fakeout_sig_combos: Counter = Counter()
    winner_sig_combos: Counter = Counter()

    for a in fakeouts:
        try:
            sigs = _json.loads(a.signals_snapshot) if a.signals_snapshot else []
            for i in range(len(sigs)):
                for j in range(i + 1, min(i + 3, len(sigs))):
                    combo = tuple(sorted([sigs[i][:30], sigs[j][:30]]))
                    fakeout_sig_combos[combo] += 1
        except Exception:
            pass

    for a in winners:
        try:
            sigs = _json.loads(a.signals_snapshot) if a.signals_snapshot else []
            for i in range(len(sigs)):
                for j in range(i + 1, min(i + 3, len(sigs))):
                    combo = tuple(sorted([sigs[i][:30], sigs[j][:30]]))
                    winner_sig_combos[combo] += 1
        except Exception:
            pass

    for combo, count in fakeout_sig_combos.most_common(5):
        if count < 3:
            continue
        fakeout_rate = count / max(len(fakeouts), 1) * 100
        winner_rate = winner_sig_combos.get(combo, 0) / max(len(winners), 1) * 100
        if fakeout_rate > winner_rate * 1.5 and fakeout_rate > 20:
            desc = (
                f"Fakeout combo: '{combo[0]}' + '{combo[1]}' — "
                f"{fakeout_rate:.0f}% fakeout rate vs {winner_rate:.0f}% winner rate"
            )
            save_insight(db, user_id, desc, confidence=0.5,
                         losses=count)
            patterns_found += 1

    if patterns_found:
        log_learning_event(
            db, user_id, "fakeout_mining",
            f"Discovered {patterns_found} fakeout patterns from {len(fakeouts)} fakeouts",
        )

    return {"patterns_found": patterns_found, "fakeouts_analyzed": len(fakeouts)}


# ── Position Sizing Feedback Loop ─────────────────────────────────────

def tune_position_sizing(db: Session, user_id: int | None) -> dict[str, Any]:
    """Link breakout outcome stats to position sizing adaptive weights."""
    from ...models.trading import BreakoutAlert
    from collections import defaultdict

    try:
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
        ).all()
    except Exception:
        return {"adjustments": 0}

    if len(resolved) < 10:
        return {"adjustments": 0, "note": "insufficient data"}

    groups: dict[str, list] = defaultdict(list)
    for a in resolved:
        regime = a.regime_at_alert or "unknown"
        groups[f"{a.asset_type}|{regime}"].append(a)

    adjustments = 0
    for key, alerts in groups.items():
        if len(alerts) < 5:
            continue
        asset_type, regime = key.split("|", 1)
        winners = [a for a in alerts if a.outcome == "winner"]
        fakeouts = [a for a in alerts if a.outcome == "fakeout"]
        losers = [a for a in alerts if a.outcome == "loser"]
        win_rate = len(winners) / len(alerts) * 100
        fakeout_rate = len(fakeouts) / len(alerts) * 100

        if asset_type == "crypto" and regime == "risk_off" and fakeout_rate > 60:
            desc = (
                f"Position sizing: crypto in risk_off has {fakeout_rate:.0f}% fakeout rate "
                f"({len(alerts)} alerts) — reduce pos_speculative_mult"
            )
            save_insight(db, user_id, desc, confidence=0.6,
                         wins=len(winners), losses=len(losers) + len(fakeouts))
            adjustments += 1

        if regime == "risk_on" and win_rate > 70:
            desc = (
                f"Position sizing: {asset_type} in risk_on has {win_rate:.0f}% win rate "
                f"({len(alerts)} alerts) — can increase pos_regime_risk_off_mult towards 1.0"
            )
            save_insight(db, user_id, desc, confidence=0.6,
                         wins=len(winners), losses=len(losers) + len(fakeouts))
            adjustments += 1

        # Profit factor per tier
        avg_winner_gain = sum(a.max_gain_pct or 0 for a in winners) / max(len(winners), 1)
        avg_loser_loss = abs(sum(a.max_drawdown_pct or 0 for a in losers) / max(len(losers), 1))
        if avg_loser_loss > 0:
            profit_factor = avg_winner_gain / avg_loser_loss
            if profit_factor > 2.0 and len(alerts) >= 10:
                desc = (
                    f"Position sizing: {asset_type} {regime} has profit factor "
                    f"{profit_factor:.1f}x — consider larger pos_pct_hard_cap for this regime"
                )
                save_insight(db, user_id, desc, confidence=0.65,
                             wins=len(winners), losses=len(losers))
                adjustments += 1

    if adjustments:
        log_learning_event(
            db, user_id, "position_sizing_feedback",
            f"Generated {adjustments} sizing adjustments from {len(resolved)} alerts",
        )

    return {"adjustments": adjustments}


# ── Inter-Alert Learning ──────────────────────────────────────────────

def learn_inter_alert_patterns(db: Session, user_id: int | None) -> dict[str, Any]:
    """Correlate co-fired alerts (same scan_cycle_id) to learn about
    alert volume and sector concentration effects."""
    from ...models.trading import BreakoutAlert
    from collections import defaultdict

    try:
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.scan_cycle_id.isnot(None),
        ).all()
    except Exception:
        return {"insights": 0}

    if len(resolved) < 10:
        return {"insights": 0, "note": "insufficient data"}

    cycles: dict[str, list] = defaultdict(list)
    for a in resolved:
        cycles[a.scan_cycle_id].append(a)

    insights_created = 0
    multi_alert_cycles = {k: v for k, v in cycles.items() if len(v) >= 3}

    if len(multi_alert_cycles) >= 3:
        high_vol_wins = []
        low_vol_wins = []
        for cid, alerts in cycles.items():
            winners = sum(1 for a in alerts if a.outcome == "winner")
            wr = winners / len(alerts) * 100 if alerts else 0
            if len(alerts) >= 4:
                high_vol_wins.append(wr)
            elif len(alerts) <= 2:
                low_vol_wins.append(wr)

        if high_vol_wins and low_vol_wins:
            avg_high = sum(high_vol_wins) / len(high_vol_wins)
            avg_low = sum(low_vol_wins) / len(low_vol_wins)
            if avg_high < avg_low - 10:
                desc = (
                    f"Inter-alert: high-volume cycles (4+ alerts) have {avg_high:.0f}% win rate "
                    f"vs low-volume (1-2) at {avg_low:.0f}% — reduce crypto_alert_max_per_cycle"
                )
                save_insight(db, user_id, desc, confidence=0.55)
                insights_created += 1

    # Sector concentration analysis
    for cid, alerts in multi_alert_cycles.items():
        sectors = set(a.sector or "unknown" for a in alerts)
        winners = sum(1 for a in alerts if a.outcome == "winner")
        wr = winners / len(alerts) * 100

        if len(sectors) == 1 and wr > 60 and len(alerts) >= 3:
            desc = (
                f"Inter-alert: single-sector cycle ({list(sectors)[0]}, {len(alerts)} alerts) "
                f"achieved {wr:.0f}% win rate — sector momentum confirmed"
            )
            save_insight(db, user_id, desc, confidence=0.55)
            insights_created += 1
            break  # one insight per cycle is enough

    if insights_created:
        log_learning_event(
            db, user_id, "inter_alert_learning",
            f"Generated {insights_created} inter-alert insights from {len(cycles)} cycles",
        )

    return {"insights": insights_created, "cycles_analyzed": len(cycles)}


# ── Adaptive Timeframe Learning ───────────────────────────────────────

def learn_timeframe_performance(db: Session, user_id: int | None) -> dict[str, Any]:
    """Learn which scanner timeframes produce the best outcomes."""
    from ...models.trading import BreakoutAlert
    from collections import defaultdict

    try:
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.timeframe.isnot(None),
        ).all()
    except Exception:
        return {"insights": 0}

    if len(resolved) < 10:
        return {"insights": 0, "note": "insufficient data"}

    groups: dict[str, list] = defaultdict(list)
    for a in resolved:
        groups[f"{a.asset_type}|{a.timeframe}"].append(a)

    insights_created = 0
    tf_stats: list[tuple[str, float, int]] = []

    for key, alerts in groups.items():
        if len(alerts) < 5:
            continue
        asset_type, tf = key.split("|", 1)
        winners = sum(1 for a in alerts if a.outcome == "winner")
        wr = winners / len(alerts) * 100
        avg_gain = sum(a.max_gain_pct or 0 for a in alerts) / len(alerts)
        tf_stats.append((key, wr, len(alerts)))

        if wr > 65 and len(alerts) >= 8:
            desc = (
                f"Timeframe performance: {asset_type} {tf} achieves {wr:.0f}% win rate "
                f"(avg gain {avg_gain:+.1f}%, n={len(alerts)}) — boost {tf} pattern weights"
            )
            save_insight(db, user_id, desc, confidence=min(0.8, wr / 100),
                         wins=winners, losses=len(alerts) - winners)
            insights_created += 1

    if tf_stats and len(tf_stats) >= 2:
        best = max(tf_stats, key=lambda x: x[1])
        worst = min(tf_stats, key=lambda x: x[1])
        if best[1] - worst[1] > 20:
            desc = (
                f"Timeframe comparison: best {best[0]} at {best[1]:.0f}%wr (n={best[2]}) "
                f"vs worst {worst[0]} at {worst[1]:.0f}%wr (n={worst[2]})"
            )
            save_insight(db, user_id, desc, confidence=0.55)
            insights_created += 1

    if insights_created:
        log_learning_event(
            db, user_id, "timeframe_learning",
            f"Generated {insights_created} timeframe insights from {len(resolved)} alerts",
        )

    return {"insights": insights_created}


# ── Confidence Decay ──────────────────────────────────────────────────

def decay_stale_insights(db: Session, user_id: int | None) -> dict[str, Any]:
    """Decay confidence of insights not refreshed recently. Prune dead ones."""
    now = datetime.utcnow()
    q = db.query(TradingInsight).filter(TradingInsight.active.is_(True))
    if user_id is not None:
        q = q.filter(TradingInsight.user_id == user_id)
    insights = q.all()

    decayed = 0
    pruned = 0
    for ins in insights:
        if ins.last_seen is None:
            continue
        age_days = (now - ins.last_seen).days

        if age_days > 90 and ins.confidence < 0.3:
            ins.active = False
            pruned += 1
            log_learning_event(
                db, user_id, "insight_pruned",
                f"Pruned stale insight (>{age_days}d, conf {ins.confidence:.0%}): "
                f"{ins.pattern_description[:60]}",
                confidence_before=ins.confidence,
                confidence_after=0,
                related_insight_id=ins.id,
            )
        elif age_days > 60:
            old_conf = ins.confidence
            ins.confidence = round(ins.confidence * 0.8, 3)
            decayed += 1
        elif age_days > 30:
            old_conf = ins.confidence
            ins.confidence = round(ins.confidence * 0.9, 3)
            decayed += 1

    if decayed or pruned:
        db.commit()
        log_learning_event(
            db, user_id, "confidence_decay",
            f"Decayed {decayed} stale insights, pruned {pruned} dead insights",
        )

    return {"decayed": decayed, "pruned": pruned}


# ── Signal Synergy Mining ─────────────────────────────────────────────

def mine_signal_synergies(db: Session, user_id: int | None) -> dict[str, Any]:
    """Find which signal combinations are most powerful (win more than
    individual signals predict) and create synergy insights."""
    import json as _json
    from ...models.trading import BreakoutAlert
    from collections import Counter, defaultdict

    try:
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.signals_snapshot.isnot(None),
        ).all()
    except Exception:
        return {"synergies_found": 0}

    if len(resolved) < 10:
        return {"synergies_found": 0, "note": "insufficient data"}

    combo_outcomes: dict[tuple, list[str]] = defaultdict(list)

    for a in resolved:
        try:
            sigs = _json.loads(a.signals_snapshot) if a.signals_snapshot else []
            short_sigs = [s[:35] for s in sigs[:8]]
            for i in range(len(short_sigs)):
                for j in range(i + 1, len(short_sigs)):
                    combo = tuple(sorted([short_sigs[i], short_sigs[j]]))
                    combo_outcomes[combo].append(a.outcome)
        except Exception:
            pass

    synergies_found = 0
    overall_wr = sum(1 for a in resolved if a.outcome == "winner") / len(resolved) * 100

    for combo, outcomes in combo_outcomes.items():
        if len(outcomes) < 5:
            continue
        wr = sum(1 for o in outcomes if o == "winner") / len(outcomes) * 100

        if wr > overall_wr + 15 and wr > 65:
            desc = (
                f"Signal synergy: '{combo[0]}' + '{combo[1]}' — "
                f"{wr:.0f}% win rate (n={len(outcomes)}) vs baseline {overall_wr:.0f}% "
                f"— pattern combo synergy bonus"
            )
            w = sum(1 for o in outcomes if o == "winner")
            save_insight(db, user_id, desc, confidence=min(0.85, wr / 100),
                         wins=w, losses=len(outcomes) - w)
            synergies_found += 1

        if synergies_found >= 5:
            break

    if synergies_found:
        log_learning_event(
            db, user_id, "synergy_mining",
            f"Discovered {synergies_found} signal synergies from {len(resolved)} alerts",
        )

    return {"synergies_found": synergies_found}


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

    mine_tickers = list(ALL_SCAN_TICKERS)[:300]
    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=_workers) as pool:
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
                best_wins = 0

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
                        best_wins = wins

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
                        wins=best_wins, losses=best_n - best_wins,
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

    # Dynamic hypothesis pool status
    from ...models.trading import TradingHypothesis
    hypothesis_pool_text = "No hypotheses yet."
    try:
        recent_hyps = db.query(TradingHypothesis).order_by(
            TradingHypothesis.last_tested_at.desc().nullslast()
        ).limit(15).all()
        if recent_hyps:
            lines = []
            for h in recent_hyps:
                rate = (h.times_confirmed or 0) / max(1, h.times_tested or 1)
                result_json = json.loads(h.last_result_json) if h.last_result_json else {}
                a_avg = result_json.get("group_a_avg", "?")
                b_avg = result_json.get("group_b_avg", "?")
                lines.append(
                    f"  - [{h.status}] {h.description} "
                    f"(tested {h.times_tested}x, confirm rate {rate:.0%}, "
                    f"last A={a_avg}, B={b_avg}, origin={h.origin})"
                )
            hypothesis_pool_text = "\n".join(lines)
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

## HYPOTHESIS POOL STATUS
{hypothesis_pool_text}

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

### Active Hypothesis Results
(Review the hypothesis pool. Which hypotheses are being confirmed? Which are failing?
 Note any surprises — especially hypotheses you expected to confirm but didn't.)

### Hypotheses to Test
IMPORTANT: After your report, output a JSON block with concrete testable hypotheses.
These will be automatically added to the hypothesis pool and tested against real data.
Use EXACTLY this format (valid JSON):

```json
{{{{
  "hypotheses": [
    {{{{
      "description": "human-readable description of what to test",
      "condition_a": "indicator condition for group A (e.g. 'rsi > 65 and ema_stack == true')",
      "condition_b": "indicator condition for group B (e.g. 'rsi > 65 and ema_stack == false')",
      "expected_winner": "a",
      "related_weight": "optional_weight_name_to_influence"
    }}}}
  ]
}}}}
```

Available indicator fields: rsi, macd, macd_sig, macd_hist, adx, stoch_k, bb_pct,
bb_squeeze (bool), vol_ratio, ema_stack (bool), ema_20, ema_50, ema_100, gap_pct,
resistance_retests (int), narrow_range (string like NR7), vcp_count (int),
above_sma20 (bool), regime (string), spy_mom_5d (float).

Generate 3-5 NEW hypotheses based on:
1. Gaps in your knowledge from the hypothesis pool above
2. Challenged hypotheses that need follow-up investigation
3. Interesting indicator combinations not yet tested
4. Breakout-specific tests (resistance retests, VCP, narrow range patterns)

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
        related_w = hyp.get("related_weight")
        if desc and cond_a and cond_b:
            existing = db.query(TradingHypothesis).filter_by(description=desc).first()
            if not existing:
                new_hyp = TradingHypothesis(
                    description=desc,
                    condition_a=cond_a,
                    condition_b=cond_b,
                    expected_winner=expected,
                    origin="llm_generated",
                    status="pending",
                    related_weight=related_w if related_w else None,
                )
                db.add(new_hyp)
                hypotheses_saved += 1
    if hypotheses_saved:
        db.commit()

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


def _indicator_data_to_flat_snapshot(
    ind_data: dict[str, Any], price: float | None,
) -> dict[str, Any]:
    """Convert nested ``get_indicator_snapshot()`` output to flat dict for ``evaluate_patterns()``."""
    snap: dict[str, Any] = {}
    if price is not None:
        snap["price"] = price

    rsi_val = (ind_data.get("rsi") or {}).get("value")
    if rsi_val is not None:
        snap["rsi_14"] = rsi_val

    for ema_key in ("ema_20", "ema_50", "ema_100"):
        v = (ind_data.get(ema_key) or {}).get("value")
        if v is not None:
            snap[ema_key] = v

    sma20 = (ind_data.get("sma_20") or {}).get("value")
    if sma20 is not None:
        snap["sma_20"] = sma20

    macd_hist = (ind_data.get("macd") or {}).get("histogram")
    if macd_hist is not None:
        snap["macd_hist"] = macd_hist

    adx_val = (ind_data.get("adx") or {}).get("adx")
    if adx_val is not None:
        snap["adx"] = adx_val

    atr_val = (ind_data.get("atr") or {}).get("value")
    if atr_val is not None:
        snap["atr"] = atr_val

    bb = ind_data.get("bbands") or {}
    bb_upper = bb.get("upper")
    bb_lower = bb.get("lower")
    if bb_upper and bb_lower and bb_upper > bb_lower:
        bandwidth = (bb_upper - bb_lower) / ((bb_upper + bb_lower) / 2)
        snap["bb_squeeze"] = bandwidth < 0.04

    obv = (ind_data.get("obv") or {}).get("value")
    if obv is not None:
        snap["obv"] = obv

    stoch_k = (ind_data.get("stoch") or {}).get("k")
    if stoch_k is not None:
        snap["stoch_k"] = stoch_k

    return snap


def _predict_single_ticker(
    ticker: str,
    quotes_map: dict[str, dict],
    vix: float | None,
    vol_regime: dict,
    meta_learner_ready: bool,
    meta_predict_fn,
    active_patterns: list | None = None,
) -> dict | None:
    """Predict a single ticker using the pattern-driven ML brain.

    Scoring tiers (graceful degradation):
      1. Meta-learner trained  -> probability from pattern feature model
      2. Patterns exist, no ML -> weighted soft-match fallback
      3. No patterns            -> neutral (score=0)
    """
    from .pattern_engine import evaluate_patterns_with_strength
    from .pattern_ml import extract_pattern_features

    try:
        snapshot = get_indicator_snapshot(ticker)
        if not snapshot or len(snapshot) < 3:
            return None
        ind_data = {k: v for k, v in snapshot.items() if k not in ("ticker", "interval")}

        quote = quotes_map.get(ticker)
        if not quote:
            quote = fetch_quote(ticker)
        price = quote["price"] if quote else None

        if not active_patterns:
            return None

        flat_snap = _indicator_data_to_flat_snapshot(ind_data, price) if price else {}
        if not flat_snap:
            return None

        # Evaluate patterns with strength for explainability
        matches = evaluate_patterns_with_strength(flat_snap, active_patterns)
        matched_patterns: list[dict] = []
        for m in matches:
            raw_wr = m.get("win_rate")
            wr_pct = round(raw_wr) if raw_wr is not None and raw_wr > 1 else (round(raw_wr * 100) if raw_wr else None)
            matched_patterns.append({
                "name": m["name"],
                "win_rate": wr_pct,
                "pattern_id": m.get("pattern_id"),
                "match_quality": m.get("match_quality"),
                "conditions_met": m.get("conditions_met"),
                "conditions_total": m.get("conditions_total"),
                "avg_strength": m.get("avg_strength"),
            })

        # --- Tier 1: meta-learner ---
        meta_prob = None
        if meta_learner_ready:
            pat_features = extract_pattern_features(active_patterns, flat_snap)
            meta_prob = meta_predict_fn(pat_features)

        if meta_prob is not None:
            blended_score = round((meta_prob - 0.5) * 20, 2)
        elif matched_patterns:
            # --- Tier 2: soft-match fallback ---
            pattern_score = 0.0
            for m in matches:
                raw_wr = m.get("win_rate") or 0.5
                wr = raw_wr / 100.0 if raw_wr > 1 else raw_wr
                quality = m.get("match_quality", 1.0)
                strength = m.get("avg_strength", 0.5)
                contrib = m.get("score_boost", 1.0) * max(0.5, wr) * quality * max(0.3, strength)
                pattern_score += contrib
            blended_score = max(-10.0, min(10.0, round(pattern_score, 2)))
        else:
            # --- Tier 3: neutral ---
            blended_score = 0.0

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
            "meta_ml_probability": round(meta_prob, 4) if meta_prob is not None else None,
            "direction": direction,
            "confidence": confidence,
            "signals": _explain_prediction(matched_patterns, blended_score),
            "matched_patterns": matched_patterns or [],
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
    """Core prediction logic (no cache).  Pattern-driven ML pipeline."""
    from .pattern_engine import get_active_patterns
    from .pattern_ml import get_meta_learner

    tickers = _build_prediction_tickers(db, tickers)
    ticker_batch = tickers[:150]

    vix = get_vix()
    vol_regime = get_volatility_regime(vix)

    meta = get_meta_learner()
    meta_ready = meta.is_ready()

    try:
        _active_patterns = get_active_patterns(db)
    except Exception:
        _active_patterns = []

    if not _active_patterns:
        return []

    quotes_map = fetch_quotes_batch(ticker_batch)

    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    results = []
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futures = {
            pool.submit(
                _predict_single_ticker,
                t, quotes_map, vix, vol_regime,
                meta_ready, meta.predict,
                _active_patterns,
            ): t
            for t in ticker_batch
        }
        for fut in as_completed(futures):
            entry = fut.result()
            if entry is not None:
                results.append(entry)

    results.sort(key=lambda x: abs(x["score"]), reverse=True)
    return results


def _explain_prediction(
    matched_patterns: list[dict] | None,
    score: float,
) -> list[str]:
    """Generate human-readable explanations from matched patterns."""
    reasons: list[str] = []

    if matched_patterns:
        for mp in matched_patterns:
            wr = mp.get("win_rate")
            met = mp.get("conditions_met")
            total = mp.get("conditions_total")
            strength = mp.get("avg_strength")
            label = mp["name"]
            parts: list[str] = []
            if wr is not None:
                parts.append(f"{wr}% WR")
            if met is not None and total is not None and met < total:
                parts.append(f"{met}/{total} conditions")
            if strength is not None and strength < 1.0:
                parts.append(f"{round(strength * 100)}% strength")
            if parts:
                label += f" ({', '.join(parts)})"
            reasons.append(f"Pattern: {label}")

    if not reasons:
        if abs(score) < 0.5:
            reasons.append("No active patterns matched — neutral")
        else:
            reasons.append("Weak pattern signals")

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

    # Early accuracy using 3-day returns (available sooner than 5-day)
    early_accuracy = 0
    early_predictions = 0
    if total_predictions == 0:
        early_snaps = db.query(MarketSnapshot).filter(
            MarketSnapshot.future_return_3d.isnot(None),
            MarketSnapshot.predicted_score.isnot(None),
            MarketSnapshot.future_return_5d.is_(None),
        ).order_by(MarketSnapshot.snapshot_date.desc()).limit(2000).all()
        e_correct = 0
        for snap in early_snaps:
            try:
                pred_score = snap.predicted_score
                if abs(pred_score) < 0.1:
                    continue
                predicted_up = pred_score > 0
                actual_up = (snap.future_return_3d or 0) > 0
                if predicted_up == actual_up:
                    e_correct += 1
                early_predictions += 1
            except Exception:
                continue
        early_accuracy = round(e_correct / early_predictions * 100, 1) if early_predictions > 0 else 0

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

    from .pattern_ml import get_meta_learner
    _meta = get_meta_learner()
    ml_stats = _meta.get_stats()

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
        "early_accuracy": early_accuracy,
        "early_predictions": early_predictions,
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
        "ml_ready": _meta.is_ready(),
        "ml_accuracy": ml_stats.get("cv_accuracy", 0),
        "ml_samples": ml_stats.get("samples", 0),
        "ml_trained_at": ml_stats.get("trained_at"),
        "ml_feature_importances": ml_stats.get("feature_importances"),
        "ml_active_patterns": ml_stats.get("active_patterns", 0),
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
    "total_steps": 14,
    "patterns_found": 0,
    "tickers_processed": 0,
    "step_timings": {},
    "data_provider": None,
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


def _run_pattern_engine_cycle(db: Session, user_id: int | None) -> dict[str, Any]:
    """Run pattern discovery, testing, and evolution in one cycle."""
    result: dict[str, Any] = {
        "hypotheses_generated": 0,
        "patterns_tested": 0,
        "patterns_evolved": 0,
        "web_patterns_created": 0,
    }

    try:
        from .pattern_engine import get_active_patterns, update_pattern
        from ..backtest_service import backtest_pattern

        # Step A: LLM-based hypothesis discovery from internal data
        hypotheses = discover_pattern_hypotheses(db, user_id)
        result["hypotheses_generated"] = len(hypotheses)

        # Step B: Web research — browse online for new patterns
        try:
            from .web_pattern_researcher import run_web_pattern_research
            web_report = run_web_pattern_research(db)
            result["web_patterns_created"] = web_report.get("patterns_created", 0)
            result["web_searches"] = web_report.get("searches", 0)
        except Exception as e:
            logger.warning(f"[learning] Web pattern research failed: {e}")

        # Step C: Test all active patterns
        patterns = get_active_patterns(db)
        tested = 0
        for p in patterns[:10]:
            try:
                bt_result = test_pattern_hypothesis(db, p, user_id)
                if bt_result:
                    tested += 1
            except Exception:
                continue
        result["patterns_tested"] = tested

        # Step D: Evolve — prune weak, promote strong
        evolved = evolve_patterns(db)
        result["patterns_evolved"] = evolved

    except Exception as e:
        logger.warning(f"[learning] Pattern engine cycle error: {e}")

    return result


def discover_pattern_hypotheses(
    db: Session,
    user_id: int | None,
    max_hypotheses: int = 3,
) -> list[dict[str, Any]]:
    """Use LLM + recent trading data to propose new ScanPattern hypotheses.

    Analyzes recent high-scoring breakouts and missed opportunities to
    generate new pattern rules that the current patterns don't capture.
    """
    from .pattern_engine import create_pattern, list_patterns, get_active_patterns
    from ...models.trading import TradingInsight, ScanPattern

    existing = list_patterns(db)
    existing_names = {p["name"].lower() for p in existing}

    insights = (
        db.query(TradingInsight)
        .filter(TradingInsight.active.is_(True))
        .filter(TradingInsight.confidence >= 0.6)
        .order_by(TradingInsight.confidence.desc())
        .limit(20)
        .all()
    )

    if len(insights) < 3:
        return []

    insight_summaries = []
    for ins in insights[:15]:
        insight_summaries.append(
            f"- {ins.pattern_description} (confidence={ins.confidence:.2f}, seen={ins.evidence_count}x)"
        )

    existing_summaries = []
    for p in existing[:10]:
        existing_summaries.append(f"- {p['name']}: {p.get('description', '')}")

    prompt = (
        "You are a quantitative trading pattern researcher. Based on these high-confidence "
        "learned trading insights, propose 1-3 NEW composable breakout patterns that are NOT "
        "already covered by the existing patterns.\n\n"
        "## Learned Insights:\n" + "\n".join(insight_summaries) + "\n\n"
        "## Existing Patterns:\n" + ("\n".join(existing_summaries) if existing_summaries else "(none)") + "\n\n"
        "For each proposed pattern, respond with JSON array. Each element:\n"
        '{"name": "...", "description": "...", "conditions": [{"indicator": "...", "op": "...", "value": ...}], '
        '"score_boost": 1.5, "min_base_score": 4.0}\n\n'
        "Available indicators: rsi_14, ema_9, ema_20, ema_50, ema_100, price, bb_squeeze, "
        "bb_squeeze_firing, adx, rel_vol, macd_hist, resistance_retests, "
        "dist_to_resistance_pct, narrow_range, vcp_count, vwap_reclaim, "
        "daily_change_pct, gap_pct, "
        "bullish_engulfing, bearish_engulfing, hammer, inverted_hammer, "
        "morning_star, doji.\n"
        "Available ops: >, >=, <, <=, ==, between, any_of.\n"
        "Respond ONLY with the JSON array, no other text."
    )

    try:
        from ..llm_caller import call_llm
        response = call_llm(prompt, max_tokens=1500)
        if not response:
            return []

        import json as _json
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        proposals = _json.loads(text)
        if not isinstance(proposals, list):
            proposals = [proposals]

        created = []
        for prop in proposals[:max_hypotheses]:
            name = prop.get("name", "").strip()
            if not name or name.lower() in existing_names:
                continue

            pattern_data = {
                "name": name,
                "description": prop.get("description", ""),
                "rules_json": _json.dumps({"conditions": prop.get("conditions", [])}),
                "origin": "brain_discovered",
                "score_boost": prop.get("score_boost", 1.0),
                "min_base_score": prop.get("min_base_score", 4.0),
                "confidence": 0.3,
                "active": True,
            }
            p = create_pattern(db, pattern_data)

            user_ids_q = [
                r[0] for r in db.query(TradingInsight.user_id)
                .filter(TradingInsight.active.is_(True))
                .distinct()
                .all()
            ]
            if not user_ids_q:
                user_ids_q = [user_id]
            for uid in user_ids_q:
                db.add(TradingInsight(
                    user_id=uid,
                    scan_pattern_id=p.id,
                    pattern_description=f"{p.name} — {p.description or ''}",
                    confidence=p.confidence or 0.3,
                    evidence_count=0,
                    win_count=0,
                    loss_count=0,
                    active=True,
                ))
            db.commit()

            created.append({"id": p.id, "name": p.name})
            existing_names.add(name.lower())
            logger.info(f"[learning] Discovered new pattern hypothesis: {name}")

        return created

    except Exception as e:
        logger.warning(f"[learning] Pattern hypothesis discovery failed: {e}")
        return []


def test_pattern_hypothesis(
    db: Session,
    pattern,
    user_id: int | None,
    tickers: list[str] | None = None,
) -> dict[str, Any] | None:
    """Backtest a ScanPattern on a sample of tickers and update its confidence."""
    from ..backtest_service import backtest_pattern
    from .pattern_engine import update_pattern
    from .market_data import DEFAULT_SCAN_TICKERS

    if tickers is None:
        tickers = list(DEFAULT_SCAN_TICKERS[:10])

    wins = 0
    total = 0
    returns = []

    for ticker in tickers[:5]:
        try:
            result = backtest_pattern(
                ticker=ticker,
                pattern_name=pattern.name,
                rules_json=pattern.rules_json,
                interval="1d",
                period="1y",
            )
            if not result.get("ok"):
                continue
            total += 1
            wr = result.get("win_rate", 0)
            ret = result.get("return_pct", 0)
            if wr > 50:
                wins += 1
            returns.append(ret)
        except Exception:
            continue

    if total == 0:
        return None

    avg_return = sum(returns) / len(returns) if returns else 0
    overall_wr = (wins / total) * 100 if total > 0 else 0

    new_confidence = max(0.1, min(0.95,
        pattern.confidence * 0.7 + (overall_wr / 100) * 0.3
    ))

    update_pattern(db, pattern.id, {
        "confidence": round(new_confidence, 3),
        "win_rate": round(overall_wr, 1),
        "avg_return_pct": round(avg_return, 2),
        "backtest_count": (pattern.backtest_count or 0) + total,
        "evidence_count": (pattern.evidence_count or 0) + total,
    })

    logger.info(
        f"[learning] Tested pattern '{pattern.name}': "
        f"WR={overall_wr:.0f}%, avg_ret={avg_return:.1f}%, conf={new_confidence:.2f}"
    )

    return {
        "pattern_id": pattern.id,
        "name": pattern.name,
        "tickers_tested": total,
        "win_rate": overall_wr,
        "avg_return": avg_return,
        "new_confidence": new_confidence,
    }


def evolve_patterns(db: Session, min_evidence: int = 5, min_confidence: float = 0.2) -> int:
    """Prune low-confidence patterns and promote high-confidence ones.

    Returns the number of patterns modified.
    """
    from ...models.trading import ScanPattern

    _PROTECTED_ORIGINS = {
        "builtin", "user_seeded", "seed", "user",
        "exit_variant", "entry_variant", "combo_variant",
    }

    patterns = db.query(ScanPattern).filter_by(active=True).all()
    modified = 0

    for p in patterns:
        if p.origin in _PROTECTED_ORIGINS:
            if (p.confidence or 0) >= 0.7 and (p.score_boost or 0) < 2.0:
                p.score_boost = min((p.score_boost or 0) + 0.5, 3.0)
                modified += 1
                logger.info(f"[learning] Promoted pattern: {p.name} boost→{p.score_boost:.1f}")
            continue

        if p.parent_id is not None:
            continue

        if (p.evidence_count or 0) >= min_evidence and (p.confidence or 0) < min_confidence:
            if (p.win_rate or 0) >= 0.4:
                continue
            p.active = False
            modified += 1
            logger.info(f"[learning] Deactivated low-confidence pattern: {p.name} (conf={p.confidence:.2f})")
            continue

        if (p.confidence or 0) >= 0.7 and (p.score_boost or 0) < 2.0:
            p.score_boost = min((p.score_boost or 0) + 0.5, 3.0)
            modified += 1
            logger.info(f"[learning] Promoted pattern: {p.name} boost→{p.score_boost:.1f}")

    if modified:
        db.commit()
    return modified


# ── Entry condition mutation operators ─────────────────────────────

_COMPLEMENTARY_POOL: list[dict[str, Any]] = [
    {"indicator": "rsi_14", "op": ">", "value": 50},
    {"indicator": "rsi_14", "op": "<", "value": 40},
    {"indicator": "adx", "op": ">", "value": 25},
    {"indicator": "macd_hist", "op": ">", "value": 0},
    {"indicator": "rel_vol", "op": ">=", "value": 2.0},
    {"indicator": "price", "op": ">", "ref": "ema_20"},
    {"indicator": "price", "op": ">", "ref": "ema_50"},
    {"indicator": "price", "op": ">", "ref": "sma_50"},
    {"indicator": "bb_squeeze", "op": "==", "value": True},
    {"indicator": "daily_change_pct", "op": ">=", "value": 3.0},
    {"indicator": "gap_pct", "op": ">", "value": 2.0},
    {"indicator": "resistance_retests", "op": ">=", "value": 2,
     "params": {"tolerance_pct": 1.5, "lookback": 20}},
]


def _tweak_threshold(cond: dict[str, Any]) -> dict[str, Any] | None:
    """Shift a numeric threshold by +/-10-20%.  Returns None for non-numeric."""
    import random
    mutated = dict(cond)
    val = mutated.get("value")
    if mutated.get("ref"):
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        direction = random.choice([-1, 1])
        pct = random.uniform(0.10, 0.20) * direction
        new_val = round(val * (1 + pct), 2) if val != 0 else round(random.uniform(-5, 5), 2)
        mutated["value"] = new_val
        return mutated
    if isinstance(val, list) and len(val) == 2:
        idx = random.randint(0, 1)
        direction = random.choice([-1, 1])
        pct = random.uniform(0.10, 0.20) * direction
        new_list = list(val)
        old = float(new_list[idx])
        new_list[idx] = round(old * (1 + pct), 2) if old != 0 else round(random.uniform(-5, 5), 2)
        if new_list[0] > new_list[1]:
            new_list[0], new_list[1] = new_list[1], new_list[0]
        mutated["value"] = new_list
        return mutated
    return None


_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "tolerance_pct": (0.5, 5.0),
    "lookback": (5, 60),
}


def _tweak_params(cond: dict[str, Any]) -> dict[str, Any] | None:
    """Shift a random numeric param (e.g. tolerance_pct, lookback) by +/-10-25%.

    Returns None when the condition has no tweakable params.
    """
    import random
    params = cond.get("params")
    if not params or not isinstance(params, dict):
        return None
    numeric_keys = [k for k, v in params.items() if isinstance(v, (int, float))]
    if not numeric_keys:
        return None
    key = random.choice(numeric_keys)
    old_val = params[key]
    direction = random.choice([-1, 1])
    pct = random.uniform(0.10, 0.25) * direction
    new_val = round(old_val * (1 + pct), 2) if old_val != 0 else round(random.uniform(1, 10), 2)
    lo, hi = _PARAM_BOUNDS.get(key, (None, None))
    if lo is not None:
        new_val = max(lo, new_val)
    if hi is not None:
        new_val = min(hi, new_val)
    if isinstance(old_val, int):
        new_val = int(round(new_val))
    mutated = dict(cond)
    mutated["params"] = {**params, key: new_val}
    return mutated


def _find_weakest_condition(
    conditions: list[dict[str, Any]],
    loss_report: dict[str, Any] | None,
) -> int | None:
    """Return index of the condition most likely causing losses.

    Uses ``condition_pass_rates`` from the loss report: a condition that
    passes on nearly ALL losing tickers is not filtering losers out, so it
    is the weakest link.  Conversely a condition that rarely passes is too
    strict (but at least it keeps losers out).
    """
    if not loss_report or not conditions:
        return None
    pass_rates = loss_report.get("condition_pass_rates", {})
    if not pass_rates:
        return None

    worst_idx, worst_rate = None, -1.0
    for i, cond in enumerate(conditions):
        key = f"{cond.get('indicator', '')}{cond.get('op', '')}{cond.get('value', '')}"
        rate = pass_rates.get(key, 0.5)
        if rate > worst_rate:
            worst_rate = rate
            worst_idx = i
    return worst_idx


def _pick_complementary_condition(
    existing: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select a random indicator condition not already present."""
    import random
    existing_inds = {c.get("indicator") for c in existing}
    candidates = [c for c in _COMPLEMENTARY_POOL if c["indicator"] not in existing_inds]
    if not candidates:
        return None
    import copy
    picked = copy.deepcopy(random.choice(candidates))
    return picked


def _cross_breed_condition(
    db: "Session",
    existing: list[dict[str, Any]],
    exclude_pattern_id: int | None = None,
) -> dict[str, Any] | None:
    """Grab a condition from a high-performing unrelated pattern."""
    import random
    from ...models.trading import ScanPattern

    top_patterns = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.rules_json.isnot(None),
            ScanPattern.win_rate > 0.5,
        )
        .order_by(ScanPattern.win_rate.desc())
        .limit(20)
        .all()
    )
    existing_inds = {c.get("indicator") for c in existing}
    for _ in range(10):
        if not top_patterns:
            break
        donor = random.choice(top_patterns)
        if donor.id == exclude_pattern_id:
            continue
        try:
            rules = json.loads(donor.rules_json)
            donor_conds = rules.get("conditions", [])
        except (json.JSONDecodeError, TypeError):
            continue
        novel = [c for c in donor_conds if c.get("indicator") not in existing_inds]
        if novel:
            return dict(random.choice(novel))
    return None


def _mutate_entry_conditions(
    conditions: list[dict[str, Any]],
    loss_report: dict[str, Any] | None = None,
    db: "Session | None" = None,
    pattern_id: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Produce a mutated copy of entry conditions, guided by loss analysis.

    Returns ``(new_conditions, mutation_label)`` describing what changed.
    """
    import random
    if not conditions:
        return list(conditions), "no-op"

    conds = [dict(c) for c in conditions]

    weakest = _find_weakest_condition(conds, loss_report)

    has_params = any(c.get("params") for c in conds)
    ops = ["tweak_threshold", "remove_weakest", "add_complementary"]
    if has_params:
        ops.append("tweak_params")
    if db:
        ops.append("cross_breed")
    if weakest is None:
        ops = [o for o in ops if o != "remove_weakest"]
    if len(conds) <= 1:
        ops = [o for o in ops if o != "remove_weakest"]

    op = random.choice(ops)

    if op == "tweak_threshold":
        numeric_idxs = [
            i for i, c in enumerate(conds)
            if isinstance(c.get("value"), (int, float, list)) and not isinstance(c.get("value"), bool) and not c.get("ref")
        ]
        if numeric_idxs:
            idx = random.choice(numeric_idxs)
            tweaked = _tweak_threshold(conds[idx])
            if tweaked:
                old_val = conds[idx].get("value")
                conds[idx] = tweaked
                ind = tweaked.get("indicator", "?")
                return conds, f"tweak-{ind}-{old_val}->{tweaked['value']}"
        op = "add_complementary"

    if op == "tweak_params":
        param_idxs = [i for i, c in enumerate(conds) if c.get("params")]
        if param_idxs:
            idx = random.choice(param_idxs)
            tweaked = _tweak_params(conds[idx])
            if tweaked:
                old_params = conds[idx].get("params", {})
                conds[idx] = tweaked
                ind = tweaked.get("indicator", "?")
                diff = {k: tweaked["params"][k] for k in tweaked["params"]
                        if tweaked["params"][k] != old_params.get(k)}
                diff_str = ",".join(f"{k}={v}" for k, v in diff.items())
                return conds, f"tweak-params-{ind}-{diff_str}"
        op = "tweak_threshold"

    if op == "remove_weakest" and weakest is not None and len(conds) > 1:
        removed = conds.pop(weakest)
        return conds, f"drop-{removed.get('indicator', '?')}"

    if op == "cross_breed" and db:
        new_cond = _cross_breed_condition(db, conds, exclude_pattern_id=pattern_id)
        if new_cond:
            conds.append(new_cond)
            return conds, f"cross-{new_cond.get('indicator', '?')}"

    if op == "add_complementary" or True:
        new_cond = _pick_complementary_condition(conds)
        if new_cond:
            conds.append(new_cond)
            return conds, f"add-{new_cond.get('indicator', '?')}"

    return conds, "no-op"


# ── Pattern evolution ──────────────────────────────────────────────

_EXIT_VARIANTS: list[dict[str, Any]] = [
    {
        "label": "No-BOS-breakout",
        "config": {
            "use_bos": False, "atr_mult": 3.0, "max_bars": 50,
            "bos_buffer_pct": 0, "bos_grace_bars": 0,
        },
    },
    {
        "label": "BOS-tight",
        "config": {
            "use_bos": True, "atr_mult": 2.0, "max_bars": 25,
            "bos_buffer_pct": 0.003, "bos_grace_bars": 3,
        },
    },
    {
        "label": "BOS-moderate",
        "config": {
            "use_bos": True, "atr_mult": 2.5, "max_bars": 35,
            "bos_buffer_pct": 0.008, "bos_grace_bars": 4,
        },
    },
    {
        "label": "BOS-wide",
        "config": {
            "use_bos": True, "atr_mult": 3.0, "max_bars": 50,
            "bos_buffer_pct": 0.015, "bos_grace_bars": 6,
        },
    },
]

_MAX_ACTIVE_VARIANTS = 8


def _create_variant_child(
    db: "Session",
    parent: "ScanPattern",
    *,
    origin: str,
    variant_label: str,
    rules_json: str | None = None,
    exit_config_json: str | None = None,
) -> "ScanPattern":
    """Low-level helper: create a child ScanPattern + linked TradingInsight."""
    from ...models.trading import ScanPattern, TradingInsight

    child_name = f"{parent.name} [{variant_label}]"
    child = ScanPattern(
        name=child_name,
        description=parent.description,
        rules_json=rules_json or parent.rules_json,
        origin=origin,
        asset_class=parent.asset_class,
        confidence=parent.confidence,
        evidence_count=0,
        backtest_count=0,
        score_boost=0.0,
        min_base_score=parent.min_base_score,
        active=True,
        parent_id=parent.id,
        exit_config=exit_config_json or parent.exit_config,
        variant_label=variant_label,
        generation=(parent.generation or 0) + 1,
    )
    db.add(child)
    db.flush()

    user_ids = [
        r[0] for r in db.query(TradingInsight.user_id)
        .filter(
            TradingInsight.pattern_description.like(f"{parent.name}%"),
            TradingInsight.active.is_(True),
        )
        .distinct()
        .all()
    ]
    if not user_ids:
        user_ids = [None]
    for uid in user_ids:
        db.add(TradingInsight(
            user_id=uid,
            scan_pattern_id=child.id,
            pattern_description=f"{child_name} — {parent.description or ''}",
            confidence=parent.confidence,
            evidence_count=0,
            win_count=0,
            loss_count=0,
            active=True,
        ))
    return child


def fork_exit_variants(db: Session, pattern_id: int) -> list[int]:
    """Clone *pattern_id* into exit-strategy variant children."""
    from ...models.trading import ScanPattern

    parent = db.query(ScanPattern).get(pattern_id)
    if not parent or not parent.active:
        return []

    existing_children = (
        db.query(ScanPattern)
        .filter(ScanPattern.parent_id == pattern_id, ScanPattern.active.is_(True))
        .count()
    )
    if existing_children >= _MAX_ACTIVE_VARIANTS:
        return []

    slots = _MAX_ACTIVE_VARIANTS - existing_children
    existing_labels = set(
        r[0] for r in db.query(ScanPattern.variant_label)
        .filter(ScanPattern.parent_id == pattern_id)
        .all() if r[0]
    )

    created_ids: list[int] = []
    for variant in _EXIT_VARIANTS:
        if len(created_ids) >= slots:
            break
        if variant["label"] in existing_labels:
            continue
        child = _create_variant_child(
            db, parent,
            origin="exit_variant",
            variant_label=variant["label"],
            exit_config_json=json.dumps(variant["config"]),
        )
        created_ids.append(child.id)
        logger.info(
            "[learning] Forked exit variant: %s (parent=%d, gen=%d)",
            child.name, parent.id, child.generation,
        )

    if created_ids:
        db.commit()
    return created_ids


def fork_entry_variants(
    db: Session,
    pattern_id: int,
    loss_report: dict[str, Any] | None = None,
    max_variants: int = 3,
) -> list[int]:
    """Create entry-condition variants by mutating indicator thresholds."""
    from ...models.trading import ScanPattern

    parent = db.query(ScanPattern).get(pattern_id)
    if not parent or not parent.active or not parent.rules_json:
        return []

    existing_children = (
        db.query(ScanPattern)
        .filter(ScanPattern.parent_id == pattern_id, ScanPattern.active.is_(True))
        .count()
    )
    if existing_children >= _MAX_ACTIVE_VARIANTS:
        return []

    try:
        rules = json.loads(parent.rules_json)
        conditions = rules.get("conditions", [])
    except (json.JSONDecodeError, TypeError):
        return []
    if not conditions:
        return []

    slots = min(max_variants, _MAX_ACTIVE_VARIANTS - existing_children)
    existing_labels = set(
        r[0] for r in db.query(ScanPattern.variant_label)
        .filter(ScanPattern.parent_id == pattern_id)
        .all() if r[0]
    )

    created_ids: list[int] = []
    attempts = 0
    while len(created_ids) < slots and attempts < slots * 3:
        attempts += 1
        new_conds, label = _mutate_entry_conditions(
            conditions, loss_report=loss_report, db=db, pattern_id=pattern_id,
        )
        if label == "no-op":
            continue
        short_label = f"entry-{label}"[:40]
        if short_label in existing_labels:
            continue

        new_rules = json.dumps({"conditions": new_conds})
        child = _create_variant_child(
            db, parent,
            origin="entry_variant",
            variant_label=short_label,
            rules_json=new_rules,
        )
        existing_labels.add(short_label)
        created_ids.append(child.id)
        logger.info(
            "[learning] Forked entry variant: %s (parent=%d, gen=%d)",
            child.name, parent.id, child.generation,
        )

    if created_ids:
        db.commit()
    return created_ids


def fork_combo_variants(db: Session, pattern_id: int) -> list[int]:
    """Create a cross-bred variant by grafting a condition from a top pattern."""
    from ...models.trading import ScanPattern

    parent = db.query(ScanPattern).get(pattern_id)
    if not parent or not parent.active or not parent.rules_json:
        return []

    existing_children = (
        db.query(ScanPattern)
        .filter(ScanPattern.parent_id == pattern_id, ScanPattern.active.is_(True))
        .count()
    )
    if existing_children >= _MAX_ACTIVE_VARIANTS:
        return []

    try:
        rules = json.loads(parent.rules_json)
        conditions = rules.get("conditions", [])
    except (json.JSONDecodeError, TypeError):
        return []

    donor_cond = _cross_breed_condition(db, conditions, exclude_pattern_id=pattern_id)
    if not donor_cond:
        return []

    label = f"cross-{donor_cond.get('indicator', '?')}"[:40]
    existing_labels = set(
        r[0] for r in db.query(ScanPattern.variant_label)
        .filter(ScanPattern.parent_id == pattern_id)
        .all() if r[0]
    )
    if label in existing_labels:
        return []

    new_conds = [dict(c) for c in conditions] + [donor_cond]
    new_rules = json.dumps({"conditions": new_conds})
    child = _create_variant_child(
        db, parent,
        origin="combo_variant",
        variant_label=label,
        rules_json=new_rules,
    )
    db.commit()
    logger.info(
        "[learning] Forked combo variant: %s (parent=%d, gen=%d)",
        child.name, parent.id, child.generation,
    )
    return [child.id]


def fork_pattern_variants(
    db: Session,
    pattern_id: int,
    loss_report: dict[str, Any] | None = None,
) -> list[int]:
    """Fork entry, exit, AND combo variants for a pattern.

    Orchestrates all three axes of evolution.
    """
    created: list[int] = []
    created.extend(fork_exit_variants(db, pattern_id))
    created.extend(fork_entry_variants(db, pattern_id, loss_report=loss_report))
    created.extend(fork_combo_variants(db, pattern_id))
    return created


def _mutate_exit_config(config: dict[str, Any]) -> dict[str, Any]:
    """Create a small random mutation of an exit config."""
    import random
    mutated = dict(config)

    atr = mutated.get("atr_mult", 2.0)
    mutated["atr_mult"] = round(atr * random.uniform(0.85, 1.15), 2)
    mutated["atr_mult"] = max(1.0, min(5.0, mutated["atr_mult"]))

    max_bars = mutated.get("max_bars", 25)
    mutated["max_bars"] = max(10, min(80, max_bars + random.randint(-5, 5)))

    if mutated.get("use_bos"):
        buf = mutated.get("bos_buffer_pct", 0.003)
        mutated["bos_buffer_pct"] = round(
            max(0.001, min(0.03, buf * random.uniform(0.8, 1.2))), 4
        )
        grace = mutated.get("bos_grace_bars", 3)
        mutated["bos_grace_bars"] = max(2, min(10, grace + random.randint(-1, 1)))

    return mutated


def _analyze_variant_losses(
    db: Session,
    pattern_id: int,
) -> dict[str, Any] | None:
    """Analyze losing BacktestResult records for a pattern to identify failure patterns.

    Returns a ``loss_report`` dict with:
    - ``losing_tickers``: list of tickers where the pattern lost money
    - ``condition_pass_rates``: for each condition, the fraction of losing
      tickers where it was met (high = not filtering losers = weak)
    - ``sector_losses``: count of losses by sector
    - ``avg_losing_return``: average return among losing backtests
    """
    from ...models.trading import ScanPattern, TradingInsight, BacktestResult

    pattern = db.query(ScanPattern).get(pattern_id)
    if not pattern or not pattern.rules_json:
        return None

    insight_ids = [
        r[0] for r in db.query(TradingInsight.id)
        .filter(TradingInsight.scan_pattern_id == pattern_id)
        .all()
    ]
    if not insight_ids:
        return None

    losing_bts = (
        db.query(BacktestResult.ticker, BacktestResult.return_pct)
        .filter(
            BacktestResult.related_insight_id.in_(insight_ids),
            BacktestResult.trade_count > 0,
            BacktestResult.return_pct < 0,
        )
        .all()
    )
    if len(losing_bts) < 3:
        return None

    losing_tickers = list({bt[0] for bt in losing_bts})
    avg_losing_return = round(
        sum(bt[1] for bt in losing_bts) / len(losing_bts), 2
    )

    from .backtest_engine import SECTOR_TICKERS
    ticker_to_sector: dict[str, str] = {}
    for sector, tickers in SECTOR_TICKERS.items():
        for t in tickers:
            ticker_to_sector[t] = sector

    sector_losses: dict[str, int] = {}
    for t in losing_tickers:
        sec = ticker_to_sector.get(t, "other")
        sector_losses[sec] = sector_losses.get(sec, 0) + 1

    try:
        rules = json.loads(pattern.rules_json)
        conditions = rules.get("conditions", [])
    except (json.JSONDecodeError, TypeError):
        conditions = []

    condition_pass_rates: dict[str, float] = {}
    if conditions:
        for cond in conditions:
            key = f"{cond.get('indicator', '')}{cond.get('op', '')}{cond.get('value', '')}"
            condition_pass_rates[key] = 0.5

    report: dict[str, Any] = {
        "losing_tickers": losing_tickers[:30],
        "condition_pass_rates": condition_pass_rates,
        "sector_losses": sector_losses,
        "avg_losing_return": avg_losing_return,
        "total_losses": len(losing_bts),
    }

    try:
        log_learning_event(
            db, None, "loss_analysis",
            f"Loss analysis for '{pattern.name}': {len(losing_bts)} losing backtests, "
            f"avg return {avg_losing_return}%, sectors={sector_losses}",
        )
    except Exception:
        pass

    return report


def evolve_exit_strategies(db: Session) -> dict[str, Any]:
    """Legacy alias — delegates to the full pattern evolution engine."""
    return evolve_pattern_strategies(db)


def evolve_pattern_strategies(db: Session) -> dict[str, Any]:
    """Full evolutionary loop: entry, exit, AND combo variants.

    1. **Fork** — root patterns without children get initial variants
       across all three axes (exit, entry, combo).
    2. **Compare** — variants with enough backtests are ranked; the best
       is promoted and underperformers are deactivated.
    3. **Loss analysis** — inspect why the worst variants lost.
    4. **Guided mutate** — use loss reports to guide next-gen mutations
       instead of pure randomness.
    5. **Journal** — log rich LearningEvents summarising what was learned.

    Returns summary stats for logging.
    """
    from ...models.trading import (
        ScanPattern, TradingInsight, BacktestResult, LearningEvent,
    )

    stats: dict[str, Any] = {
        "forked_exit": 0, "forked_entry": 0, "forked_combo": 0,
        "promoted": 0, "deactivated": 0,
        "mutated_exit": 0, "mutated_entry": 0,
        "loss_reports": 0,
    }

    # ── 1. Fork phase ────────────────────────────────────────────────
    root_patterns = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.parent_id.is_(None),
            ScanPattern.origin.notin_(["exit_variant", "entry_variant", "combo_variant"]),
        )
        .all()
    )

    for rp in root_patterns:
        child_count = (
            db.query(func.count(ScanPattern.id))
            .filter(ScanPattern.parent_id == rp.id, ScanPattern.active.is_(True))
            .scalar()
        )
        if child_count == 0:
            exit_ids = fork_exit_variants(db, rp.id)
            stats["forked_exit"] += len(exit_ids)
            entry_ids = fork_entry_variants(db, rp.id, max_variants=2)
            stats["forked_entry"] += len(entry_ids)
            combo_ids = fork_combo_variants(db, rp.id)
            stats["forked_combo"] += len(combo_ids)

    # ── 2. Compare phase ─────────────────────────────────────────────
    parents_with_children = (
        db.query(ScanPattern.parent_id)
        .filter(ScanPattern.parent_id.isnot(None), ScanPattern.active.is_(True))
        .distinct()
        .all()
    )

    min_bt_per_variant = 5
    loss_reports_by_parent: dict[int, dict[str, Any]] = {}

    for (parent_id,) in parents_with_children:
        parent = db.query(ScanPattern).get(parent_id)
        if not parent:
            continue

        siblings = (
            db.query(ScanPattern)
            .filter(ScanPattern.parent_id == parent_id, ScanPattern.active.is_(True))
            .all()
        )
        if len(siblings) < 2:
            continue

        variant_scores: list[tuple] = []

        for sib in siblings:
            bts = (
                db.query(BacktestResult)
                .join(
                    TradingInsight,
                    TradingInsight.id == BacktestResult.related_insight_id,
                )
                .filter(
                    TradingInsight.scan_pattern_id == sib.id,
                    BacktestResult.trade_count > 0,
                )
                .all()
            )
            if not bts:
                bts = (
                    db.query(BacktestResult)
                    .join(
                        TradingInsight,
                        TradingInsight.id == BacktestResult.related_insight_id,
                    )
                    .filter(
                        TradingInsight.pattern_description.like(f"{sib.name}%"),
                        BacktestResult.trade_count > 0,
                    )
                    .all()
                )
            if len(bts) < min_bt_per_variant:
                variant_scores = []
                break

            sharpes = [bt.sharpe for bt in bts if bt.sharpe is not None]
            avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0
            wins = sum(1 for bt in bts if (bt.return_pct or 0) > 0)
            wr = wins / len(bts) if bts else 0
            variant_scores.append((sib, avg_sharpe, wr, len(bts)))

        if not variant_scores or len(variant_scores) < 2:
            continue

        variant_scores.sort(key=lambda x: (x[1], x[2]), reverse=True)
        winner = variant_scores[0][0]
        winner_sharpe = variant_scores[0][1]
        winner_wr = variant_scores[0][2]

        if winner.origin == "exit_variant" and winner.exit_config:
            parent.exit_config = winner.exit_config
            parent.updated_at = datetime.utcnow()
            stats["promoted"] += 1
        elif winner.origin in ("entry_variant", "combo_variant") and winner.rules_json:
            parent.rules_json = winner.rules_json
            parent.updated_at = datetime.utcnow()
            stats["promoted"] += 1

        logger.info(
            "[learning] Promoted variant '%s' (%s) → parent '%s' "
            "(sharpe=%.2f, wr=%.0f%%)",
            winner.variant_label, winner.origin, parent.name,
            winner_sharpe, winner_wr * 100,
        )

        # ── 3. Loss analysis for worst variant ───────────────────────
        worst = variant_scores[-1][0]
        worst_sharpe = variant_scores[-1][1]
        worst_wr = variant_scores[-1][2]
        loss_report = None
        if worst_wr < 0.5:
            loss_report = _analyze_variant_losses(db, worst.id)
            if loss_report:
                loss_reports_by_parent[parent_id] = loss_report
                stats["loss_reports"] += 1

        for loser, l_sharpe, l_wr, l_bt_count in variant_scores[1:]:
            if l_bt_count < 8:
                continue
            sharpe_gap = winner_sharpe - l_sharpe
            wr_gap = winner_wr - l_wr
            if sharpe_gap > 0.5 or wr_gap > 0.3:
                loser.active = False
                stats["deactivated"] += 1
                logger.info(
                    "[learning] Deactivated underperformer: '%s' (%s, sharpe=%.2f vs %.2f, wr=%.0f%% vs %.0f%%)",
                    loser.variant_label, loser.origin, l_sharpe, winner_sharpe,
                    l_wr * 100, winner_wr * 100,
                )

        try:
            evt = LearningEvent(
                user_id=None,
                event_type="pattern_evolution",
                description=(
                    f"Evolution for '{parent.name}': winner={winner.variant_label} "
                    f"({winner.origin}, sharpe={winner_sharpe:.2f}, wr={winner_wr:.0%}). "
                    f"Compared {len(variant_scores)} variants across exit/entry/combo axes."
                    + (f" Loss report: avg_losing_return={loss_report['avg_losing_return']}%, "
                       f"sectors={loss_report.get('sector_losses', {})}"
                       if loss_report else "")
                ),
                confidence_before=parent.confidence,
                confidence_after=parent.confidence,
            )
            db.add(evt)
        except Exception:
            pass

        # ── 4. Guided mutate phase ───────────────────────────────────
        active_children = (
            db.query(func.count(ScanPattern.id))
            .filter(ScanPattern.parent_id == parent_id, ScanPattern.active.is_(True))
            .scalar()
        )
        mutations_to_spawn = min(2, _MAX_ACTIVE_VARIANTS - active_children)

        if mutations_to_spawn > 0:
            parent_loss = loss_reports_by_parent.get(parent_id)

            # Exit mutation (if winner was exit-type or parent has exit_config)
            if winner.exit_config:
                try:
                    base_config = json.loads(winner.exit_config)
                except (json.JSONDecodeError, TypeError):
                    base_config = None
                if base_config:
                    mutated = _mutate_exit_config(base_config)
                    mut_label = (
                        f"mut-g{(winner.generation or 0) + 1}-"
                        f"atr{mutated['atr_mult']:.1f}"
                    )
                    existing = db.query(ScanPattern).filter(
                        ScanPattern.parent_id == parent_id,
                        ScanPattern.variant_label == mut_label,
                    ).first()
                    if not existing:
                        _create_variant_child(
                            db, parent,
                            origin="exit_variant",
                            variant_label=mut_label,
                            exit_config_json=json.dumps(mutated),
                        )
                        stats["mutated_exit"] += 1
                        mutations_to_spawn -= 1

            # Entry mutation guided by loss report
            if mutations_to_spawn > 0 and winner.rules_json:
                try:
                    w_rules = json.loads(winner.rules_json)
                    w_conds = w_rules.get("conditions", [])
                except (json.JSONDecodeError, TypeError):
                    w_conds = []
                if w_conds:
                    new_conds, label = _mutate_entry_conditions(
                        w_conds, loss_report=parent_loss, db=db,
                        pattern_id=parent_id,
                    )
                    if label != "no-op":
                        ent_label = f"guided-{label}"[:40]
                        existing = db.query(ScanPattern).filter(
                            ScanPattern.parent_id == parent_id,
                            ScanPattern.variant_label == ent_label,
                        ).first()
                        if not existing:
                            _create_variant_child(
                                db, parent,
                                origin="entry_variant",
                                variant_label=ent_label,
                                rules_json=json.dumps({"conditions": new_conds}),
                            )
                            stats["mutated_entry"] += 1
                            logger.info(
                                "[learning] Guided entry mutation: '%s' for '%s' "
                                "(loss_report=%s)",
                                ent_label, parent.name,
                                "yes" if parent_loss else "random",
                            )

    db.commit()
    logger.info("[learning] Pattern evolution complete: %s", stats)
    return stats


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
    _learning_status["total_steps"] = 23
    _learning_status["patterns_found"] = 0
    _learning_status["tickers_processed"] = 0
    _learning_status["started_at"] = datetime.utcnow().isoformat()
    _learning_status["step_timings"] = {}
    start = time.time()
    report: dict[str, Any] = {}

    _provider = (
        "Massive" if _use_massive() else
        "Polygon" if _use_polygon() else
        "yfinance"
    )
    _learning_status["data_provider"] = _provider
    logger.info(f"[learning] Starting learning cycle — primary data provider: {_provider}")

    def _step_time(name: str, t0: float, extra: str = "") -> None:
        elapsed = round(time.time() - t0, 1)
        _learning_status["step_timings"][name] = elapsed
        suffix = f" | {extra}" if extra else ""
        logger.info(f"[learning] Step '{name}' took {elapsed}s{suffix}")

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
        _step_time("pre-filter", step_start, f"{len(candidates)} candidates")

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
        _step_time("scan", step_start, f"{len(scan_results)} scored via {_provider}")

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
        _step_time("snapshots", step_start,
                    f"{snap_count}/{len(top_tickers)} tickers via {_provider}")

        # Step 4: Backfill future returns + predicted scores
        step_start = time.time()
        _learning_status["current_step"] = "Backfilling future returns"
        _learning_status["phase"] = "backfilling"
        filled = backfill_future_returns(db)
        scores_filled = backfill_predicted_scores(db, limit=1000)
        report["returns_backfilled"] = filled
        report["scores_backfilled"] = scores_filled
        _learning_status["steps_completed"] = 4
        _step_time("backfill", step_start,
                    f"{filled} returns + {scores_filled} scores via {_provider}")

        # Step 4b: Confidence decay (prune stale insights early)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Decaying stale insights"
        _learning_status["phase"] = "confidence_decay"
        decay_result = decay_stale_insights(db, user_id)
        report["insights_decayed"] = decay_result.get("decayed", 0)
        report["insights_pruned"] = decay_result.get("pruned", 0)
        _learning_status["steps_completed"] = 5
        _step_time("confidence_decay", step_start,
                    f"{decay_result.get('decayed', 0)} decayed, {decay_result.get('pruned', 0)} pruned")

        # Step 6: Mine patterns
        _learning_status["current_step"] = "Mining patterns"
        _learning_status["phase"] = "mining"
        step_start = time.time()
        discoveries = mine_patterns(db, user_id)
        report["patterns_discovered"] = len(discoveries)
        _learning_status["patterns_found"] = len(discoveries)
        _learning_status["steps_completed"] = 6
        _step_time("mine", step_start,
                    f"{len(discoveries)} patterns from OHLCV via {_provider}")

        # Step 5b: Active pattern seeking (boost under-sampled patterns)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Active pattern seeking"
        _learning_status["phase"] = "active_seeking"
        seek_result = seek_pattern_data(db, user_id)
        report["patterns_boosted"] = seek_result.get("sought", 0)
        _learning_status["steps_completed"] = 7
        _step_time("active_seek", step_start,
                    f"{seek_result.get('sought', 0)} boosted")

        # Step 6: Backtest discovered patterns (expanded)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Backtesting patterns"
        _learning_status["phase"] = "backtesting"
        bt_count = _auto_backtest_patterns(db, user_id)
        report["backtests_run"] = bt_count
        _learning_status["steps_completed"] = 8
        _step_time("backtest", step_start, f"{bt_count} backtests via {_provider}")

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
        _learning_status["steps_completed"] = 9
        _step_time("evolve", step_start,
                    f"{evolve_result.get('hypotheses_tested', 0)} hypotheses, "
                    f"{evolve_result.get('weights_evolved', 0)} weights evolved")

        # Step 8b: Breakout outcome learning
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start_bo = time.time()
        _learning_status["current_step"] = "Learning from breakout outcomes"
        _learning_status["phase"] = "breakout_learning"
        bo_result = learn_from_breakout_outcomes(db, user_id)
        report["breakout_patterns_learned"] = bo_result.get("patterns_learned", 0)
        _learning_status["steps_completed"] = 10
        _step_time("breakout_outcomes", step_start_bo,
                    f"{bo_result.get('patterns_learned', 0)} patterns from "
                    f"{bo_result.get('total_resolved', 0)} resolved alerts")

        # Step 8c: Intraday breakout pattern mining (15m data)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start_id = time.time()
        _learning_status["current_step"] = "Mining intraday breakout patterns"
        _learning_status["phase"] = "intraday_mining"
        intra_result = mine_intraday_patterns(db, user_id)
        report["intraday_discoveries"] = intra_result.get("discoveries", 0)
        _learning_status["steps_completed"] = 11
        _step_time("intraday_mining", step_start_id,
                    f"{intra_result.get('discoveries', 0)} discoveries from "
                    f"{intra_result.get('rows_mined', 0)} bars")

        # Step 8d: Refine patterns (parameter sweeping)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Refining patterns"
        _learning_status["phase"] = "refining"
        refine_result = refine_patterns(db, user_id)
        report["patterns_refined"] = refine_result.get("refined", 0)
        _learning_status["steps_completed"] = 12
        _step_time("refine", step_start,
                    f"{refine_result.get('refined', 0)} patterns refined")

        # Step 8e: Exit optimization learning
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Learning exit optimization"
        _learning_status["phase"] = "exit_optimization"
        exit_result = learn_exit_optimization(db, user_id)
        report["exit_adjustments"] = exit_result.get("adjustments", 0)
        _learning_status["steps_completed"] = 13
        _step_time("exit_optimization", step_start,
                    f"{exit_result.get('adjustments', 0)} adjustments")

        # Step 8f: Fakeout pattern mining
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Mining fakeout patterns"
        _learning_status["phase"] = "fakeout_mining"
        fakeout_result = mine_fakeout_patterns(db, user_id)
        report["fakeout_patterns"] = fakeout_result.get("patterns_found", 0)
        _learning_status["steps_completed"] = 14
        _step_time("fakeout_mining", step_start,
                    f"{fakeout_result.get('patterns_found', 0)} fakeout patterns")

        # Step 8g: Position sizing feedback
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Tuning position sizing"
        _learning_status["phase"] = "position_sizing"
        sizing_result = tune_position_sizing(db, user_id)
        report["sizing_adjustments"] = sizing_result.get("adjustments", 0)
        _learning_status["steps_completed"] = 15
        _step_time("position_sizing", step_start,
                    f"{sizing_result.get('adjustments', 0)} sizing adjustments")

        # Step 8h: Inter-alert learning
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Learning inter-alert patterns"
        _learning_status["phase"] = "inter_alert"
        inter_result = learn_inter_alert_patterns(db, user_id)
        report["inter_alert_insights"] = inter_result.get("insights", 0)
        _learning_status["steps_completed"] = 16
        _step_time("inter_alert", step_start,
                    f"{inter_result.get('insights', 0)} inter-alert insights")

        # Step 8i: Timeframe performance learning
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Learning timeframe performance"
        _learning_status["phase"] = "timeframe_learning"
        tf_result = learn_timeframe_performance(db, user_id)
        report["timeframe_insights"] = tf_result.get("insights", 0)
        _learning_status["steps_completed"] = 17
        _step_time("timeframe_learning", step_start,
                    f"{tf_result.get('insights', 0)} timeframe insights")

        # Step 8j: Signal synergy mining
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Mining signal synergies"
        _learning_status["phase"] = "synergy_mining"
        synergy_result = mine_signal_synergies(db, user_id)
        report["synergies_found"] = synergy_result.get("synergies_found", 0)
        _learning_status["steps_completed"] = 18
        _step_time("synergy_mining", step_start,
                    f"{synergy_result.get('synergies_found', 0)} synergies found")

        # Step 19: Market journal
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Writing market journal"
        _learning_status["phase"] = "journaling"
        journal = daily_market_journal(db, user_id)
        report["journal_written"] = journal is not None
        _learning_status["steps_completed"] = 19
        _step_time("journal", step_start)

        # Step 10: Signal events
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Checking signal events"
        _learning_status["phase"] = "signals"
        events = check_signal_events(db, user_id)
        report["signal_events"] = len(events)
        _learning_status["steps_completed"] = 20
        _step_time("signals", step_start, f"{len(events)} events")

        # Step 11: Train pattern meta-learner
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Training pattern meta-learner"
        _learning_status["phase"] = "ml_training"
        from .pattern_ml import get_meta_learner, apply_ml_feedback
        _meta = get_meta_learner()
        ml_result = _meta.train(db)
        report["ml_trained"] = ml_result.get("ok", False)
        report["ml_accuracy"] = ml_result.get("cv_accuracy", 0)
        if ml_result.get("ok"):
            _fb = apply_ml_feedback(db, _meta.get_pattern_importances())
            report["ml_feedback_boosted"] = _fb.get("boosted", 0)
            report["ml_feedback_penalised"] = _fb.get("penalised", 0)
            log_learning_event(
                db, user_id, "ml_feedback",
                f"Pattern meta-learner trained: CV acc={ml_result.get('cv_accuracy',0)}%, "
                f"{ml_result.get('active_patterns',0)} patterns, "
                f"{_fb.get('boosted',0)} boosted, {_fb.get('penalised',0)} penalised",
            )
        _learning_status["steps_completed"] = 21
        _step_time("ml_train", step_start,
                    f"acc={ml_result.get('cv_accuracy', 0):.3f}"
                    if ml_result.get("ok") else "skipped")

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
        _learning_status["steps_completed"] = 22
        _step_time("proposals", step_start,
                    f"{report.get('proposals_generated', 0)} generated")

        # Step 13b: Pattern engine — discover, test, evolve
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        _learning_status["current_step"] = "Pattern discovery & evolution"
        _learning_status["phase"] = "pattern_engine"
        try:
            pe_result = _run_pattern_engine_cycle(db, user_id)
            report["patterns_discovered_engine"] = pe_result.get("hypotheses_generated", 0)
            report["patterns_tested"] = pe_result.get("patterns_tested", 0)
            report["patterns_evolved"] = pe_result.get("patterns_evolved", 0)
        except Exception as e:
            logger.warning(f"[trading] Pattern engine cycle failed: {e}")
        _step_time("pattern_engine", step_start, "done")

        # Step 14: Finalize + log
        _learning_status["current_step"] = "Finalizing"
        _learning_status["phase"] = "finalizing"
        elapsed = time.time() - start
        report["data_provider"] = _provider
        log_learning_event(
            db, user_id, "scan",
            f"Learning cycle ({_provider}): "
            f"{report.get('prescreen_candidates', 0)} pre-screened, "
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
        _learning_status["steps_completed"] = 23

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

    logger.info(
        f"[learning] Learning cycle finished in {elapsed:.0f}s "
        f"(provider={report.get('data_provider', 'unknown')}): {report}"
    )
    return {"ok": True, **report}


def should_run_learning() -> bool:
    if _learning_status["running"]:
        return False
    last = _learning_status.get("last_run")
    if last is None:
        return True
    try:
        from ...config import settings
        cooldown = max(1, settings.learning_interval_hours)
        last_dt = datetime.fromisoformat(last)
        return datetime.utcnow() - last_dt > timedelta(hours=cooldown)
    except Exception:
        return True
