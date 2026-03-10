"""Learning: pattern mining, deep study, learning cycles, brain stats."""
from __future__ import annotations

import json
import logging
import time
import threading
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
    fetch_quote, get_indicator_snapshot,
    DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights, save_insight

logger = logging.getLogger(__name__)

_shutting_down = threading.Event()


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

def take_market_snapshot(db: Session, ticker: str) -> None:
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
    unfilled = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.is_(None),
    ).limit(100).all()

    updated = 0
    for snap in unfilled:
        try:
            df = _yf_history(snap.ticker, start=snap.snapshot_date, period="15d", interval="1d")
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


# ── Pattern Mining ────────────────────────────────────────────────────

def _mine_from_history(ticker: str) -> list[dict]:
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange

    try:
        df = _yf_history(ticker, period="1y", interval="1d")
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

    mine_tickers = mine_tickers[:50]

    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as executor:
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
            })
        except Exception:
            continue

    if len(all_rows) < 10:
        return []

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
            pattern = f"{label} -> avg {ret_str} ({wr:.0f}% win, {len(filtered)} samples)"
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

    test_tickers = list(DEFAULT_SCAN_TICKERS[:10]) + list(DEFAULT_CRYPTO_TICKERS[:3])

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
        for ticker in test_tickers[:5]:
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


def deep_study(db: Session, user_id: int | None) -> dict[str, Any]:
    """Intensive AI-powered learning: mine patterns then ask LLM to reflect."""
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
            max_tokens=2048,
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


# ── Brain Dashboard Stats ────────────────────────────────────────────

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
    patterns_this_week = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.created_at >= week_ago,
    ).count()

    total_snapshots = db.query(MarketSnapshot).count()

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
    "total_steps": 8,
    "patterns_found": 0,
    "tickers_processed": 0,
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
    """Complete learning cycle: scan -> snapshot -> backfill -> mine -> journal -> signals."""
    from .scanner import run_full_market_scan, _scan_status
    from .journal import daily_market_journal, check_signal_events

    if _learning_status["running"]:
        return {"ok": False, "reason": "Learning cycle already in progress"}
    if _shutting_down.is_set():
        return {"ok": False, "reason": "Server is shutting down"}

    _learning_status["running"] = True
    _learning_status["phase"] = "starting"
    _learning_status["steps_completed"] = 0
    _learning_status["patterns_found"] = 0
    _learning_status["tickers_processed"] = 0
    _learning_status["started_at"] = datetime.utcnow().isoformat()
    start = time.time()
    report: dict[str, Any] = {}

    try:
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        _learning_status["current_step"] = "Full market scan"
        _learning_status["phase"] = "scanning"
        scan_results = run_full_market_scan(db, user_id, use_full_universe=full_universe)
        report["tickers_scanned"] = _scan_status["tickers_total"]
        report["tickers_scored"] = len(scan_results)
        _learning_status["tickers_processed"] = len(scan_results)
        _learning_status["steps_completed"] = 1

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

        _learning_status["current_step"] = "Backfilling future returns"
        _learning_status["phase"] = "backfilling"
        filled = backfill_future_returns(db)
        report["returns_backfilled"] = filled
        _learning_status["steps_completed"] = 3

        _learning_status["current_step"] = "Mining patterns"
        _learning_status["phase"] = "mining"
        discoveries = mine_patterns(db, user_id)
        report["patterns_discovered"] = len(discoveries)
        _learning_status["patterns_found"] = len(discoveries)
        _learning_status["steps_completed"] = 4

        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        _learning_status["current_step"] = "Backtesting patterns"
        _learning_status["phase"] = "backtesting"
        bt_count = _auto_backtest_patterns(db, user_id)
        report["backtests_run"] = bt_count
        _learning_status["steps_completed"] = 5

        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        _learning_status["current_step"] = "Writing market journal"
        _learning_status["phase"] = "journaling"
        journal = daily_market_journal(db, user_id)
        report["journal_written"] = journal is not None
        _learning_status["steps_completed"] = 6

        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        _learning_status["current_step"] = "Checking signal events"
        _learning_status["phase"] = "signals"
        events = check_signal_events(db, user_id)
        report["signal_events"] = len(events)
        _learning_status["steps_completed"] = 7

        _learning_status["current_step"] = "Finalizing"
        _learning_status["phase"] = "finalizing"
        elapsed = time.time() - start
        log_learning_event(
            db, user_id, "scan",
            f"Full learning cycle: scanned {report['tickers_scanned']} tickers, "
            f"scored {report['tickers_scored']}, {report['snapshots_taken']} snapshots, "
            f"{report['patterns_discovered']} patterns discovered, "
            f"{report.get('backtests_run', 0)} backtests, "
            f"{report['signal_events']} signal events — {elapsed:.0f}s",
        )
        _learning_status["steps_completed"] = 8

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
