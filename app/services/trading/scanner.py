"""Scanner: ticker scoring, custom screener, batch scanning, smart pick."""
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

from ..yf_session import get_history as _yf_history, get_fundamentals, batch_download
from .market_data import (
    fetch_quote, smart_round, DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS, ALL_SCAN_TICKERS,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights

logger = logging.getLogger(__name__)

_shutting_down = threading.Event()
_MAX_SCAN_WORKERS = 8


def signal_shutdown():
    _shutting_down.set()


# ── Single ticker scoring ─────────────────────────────────────────────

def _score_ticker(ticker: str, *, skip_fundamentals: bool = False) -> dict[str, Any] | None:
    """Score a single ticker using multi-signal confluence (1-10).

    When *skip_fundamentals* is True the expensive ``get_fundamentals()``
    call is skipped — used during bulk scans where FinViz already
    pre-filtered for fundamental quality.
    """
    try:
        from ta.momentum import RSIIndicator, StochasticOscillator
        from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator
        from ta.volatility import BollingerBands, AverageTrueRange

        df = _yf_history(ticker, period="6mo", interval="1d")
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

        # EMA Stacking
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

        if pd.notna(macd_val) and pd.notna(macd_sig):
            if macd_val > macd_sig:
                score += 1.0
                signals.append("MACD bullish crossover")
            else:
                score -= 0.5

        if pd.notna(sma_20) and pd.notna(sma_50):
            if price > sma_20 > sma_50:
                score += 0.5
                signals.append("Uptrend (price > SMA20 > SMA50)")
            elif price < sma_20 < sma_50:
                score -= 0.5
                signals.append("Downtrend")

        if pd.notna(bb_lower) and pd.notna(bb_upper):
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                bb_pct = (price - bb_lower) / bb_range
                if bb_pct < 0.15:
                    score += 1.0
                    signals.append("Near lower Bollinger Band")
                elif bb_pct > 0.85:
                    score -= 0.5

        if pd.notna(adx_val) and adx_val > 25:
            score += 0.5
            signals.append(f"Strong trend (ADX {adx_val:.0f})")

        if vol_avg > 0 and vol_latest > vol_avg * 1.5:
            score += 0.5
            signals.append("Volume surge")

        is_crypto_ticker = ticker.upper().endswith("-USD")
        if not is_crypto_ticker and not skip_fundamentals:
            try:
                fund = get_fundamentals(ticker)
                if fund:
                    fund_bonus = 0.0
                    if fund.get("profit_margins") is not None and fund["profit_margins"] > 0.10:
                        if fund.get("debt_to_equity") is not None and fund["debt_to_equity"] < 100:
                            fund_bonus += 0.5
                            signals.append("Strong margins + low debt")
                    if fund.get("revenue_growth") is not None and fund["revenue_growth"] > 0:
                        fund_bonus += 0.5
                        signals.append(f"Revenue growth +{fund['revenue_growth']:.0%}")
                    if fund.get("pe_trailing") is not None:
                        pe = fund["pe_trailing"]
                        if 5 < pe < 25:
                            fund_bonus += 0.5
                            signals.append(f"Reasonable P/E ({pe:.1f})")
                        elif pe > 60:
                            fund_bonus -= 0.5
                            signals.append(f"Expensive P/E ({pe:.1f})")
                    score += fund_bonus
            except Exception:
                pass

        score = max(1.0, min(10.0, score))

        if score >= 7:
            signal = "buy"
        elif score <= 3.5:
            signal = "sell"
        else:
            signal = "hold"

        atr_f = float(atr_val) if pd.notna(atr_val) else price * 0.02
        stop_loss = smart_round(price - 2 * atr_f)
        take_profit = smart_round(price + 3 * atr_f)

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
            "price": smart_round(price),
            "entry_price": smart_round(price),
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
                "ema_20": smart_round(float(ema_20)) if pd.notna(ema_20) else None,
                "ema_50": smart_round(float(ema_50)) if pd.notna(ema_50) else None,
                "ema_100": smart_round(float(ema_100)) if ema_100 is not None and pd.notna(ema_100) else None,
                "ema_200": smart_round(float(ema_200)) if ema_200 is not None and pd.notna(ema_200) else None,
                "stoch_k": round(float(stoch_k), 1) if pd.notna(stoch_k) else None,
                "bb_pct": round((price - float(bb_lower)) / (float(bb_upper) - float(bb_lower)) * 100, 1)
                    if pd.notna(bb_lower) and pd.notna(bb_upper) and float(bb_upper) > float(bb_lower) else None,
                "vol_ratio": round(vol_latest / vol_avg, 2) if vol_avg > 0 else None,
            },
        }
    except Exception:
        return None


# ── Intraday (Day-Trade) Scoring ──────────────────────────────────────

def _score_ticker_intraday(ticker: str) -> dict[str, Any] | None:
    """Score a ticker for day-trade suitability using 15-minute intraday data.

    Evaluates momentum, VWAP positioning, volume surge, and ATR-based
    risk on 5 days of 15m bars.
    """
    try:
        from ta.momentum import RSIIndicator, StochasticOscillator
        from ta.trend import MACD, EMAIndicator
        from ta.volatility import AverageTrueRange

        df = _yf_history(ticker, period="5d", interval="15m")
        if df.empty or len(df) < 40:
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
        ema_9 = EMAIndicator(close=close, window=9).ema_indicator().iloc[-1]
        ema_21 = EMAIndicator(close=close, window=21).ema_indicator().iloc[-1]
        atr_val = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range().iloc[-1]
        stoch = StochasticOscillator(high=high, low=low, close=close)
        stoch_k = stoch.stoch().iloc[-1]

        price = float(close.iloc[-1])
        if price <= 0:
            return None

        # VWAP (cumulative from market open of today)
        today_mask = df.index.date == df.index.date[-1]
        today_df = df[today_mask]
        vwap = None
        vwap_pct = 0.0
        if len(today_df) > 1:
            typical = (today_df["High"] + today_df["Low"] + today_df["Close"]) / 3
            cum_vol = today_df["Volume"].cumsum()
            cum_tp_vol = (typical * today_df["Volume"]).cumsum()
            if float(cum_vol.iloc[-1]) > 0:
                vwap = float(cum_tp_vol.iloc[-1] / cum_vol.iloc[-1])
                vwap_pct = round((price - vwap) / vwap * 100, 2)

        # Volume analysis
        vol_avg = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else float(volume.mean())
        vol_latest = float(volume.iloc[-1])
        vol_ratio = round(vol_latest / vol_avg, 2) if vol_avg > 0 else 1.0

        # Gap from previous close
        if len(df) > 26:
            prev_day_close = float(close.iloc[-27]) if len(df) >= 27 else price
            gap_pct = round((float(df["Open"].iloc[-1]) - prev_day_close) / prev_day_close * 100, 2) if prev_day_close > 0 else 0
        else:
            gap_pct = 0.0

        # Scoring
        score = 5.0
        signals: list[str] = []

        # Momentum: RSI sweet spot for day trades (40-65 for longs, 35-60 for shorts)
        if pd.notna(rsi_val):
            if 40 <= rsi_val <= 65:
                score += 1.0
                signals.append(f"RSI in momentum zone ({rsi_val:.0f})")
            elif rsi_val < 25:
                score += 0.5
                signals.append(f"RSI deeply oversold ({rsi_val:.0f}) — potential bounce")
            elif rsi_val > 75:
                score -= 0.5
                signals.append(f"RSI overextended ({rsi_val:.0f})")

        # MACD momentum
        if pd.notna(macd_val) and pd.notna(macd_sig):
            if macd_val > macd_sig and pd.notna(macd_hist) and float(macd_hist) > 0:
                score += 1.0
                signals.append("MACD bullish + positive histogram")
            elif macd_val < macd_sig and pd.notna(macd_hist) and float(macd_hist) < 0:
                score -= 0.5

        # EMA trend alignment
        if pd.notna(ema_9) and pd.notna(ema_21):
            if price > float(ema_9) > float(ema_21):
                score += 1.0
                signals.append("Price > EMA9 > EMA21 (bullish intraday)")
            elif price < float(ema_9) < float(ema_21):
                score -= 0.5
                signals.append("Bearish EMA alignment")

        # VWAP positioning
        if vwap is not None:
            if price > vwap:
                score += 0.8
                signals.append(f"Above VWAP ({vwap_pct:+.1f}%)")
            else:
                score -= 0.3
                signals.append(f"Below VWAP ({vwap_pct:+.1f}%)")

        # Volume surge
        if vol_ratio >= 3.0:
            score += 1.5
            signals.append(f"Volume explosion ({vol_ratio:.1f}x avg)")
        elif vol_ratio >= 2.0:
            score += 1.0
            signals.append(f"Strong volume surge ({vol_ratio:.1f}x avg)")
        elif vol_ratio >= 1.5:
            score += 0.5
            signals.append(f"Above-average volume ({vol_ratio:.1f}x)")

        # Gap play
        if abs(gap_pct) > 2:
            score += 0.5
            signals.append(f"Gap {'up' if gap_pct > 0 else 'down'} {gap_pct:+.1f}%")

        score = max(1.0, min(10.0, score))

        if score >= 7:
            signal = "long"
        elif score <= 3.5:
            signal = "short"
        else:
            signal = "wait"

        atr_f = float(atr_val) if pd.notna(atr_val) else price * 0.01
        scalp_stop = smart_round(price - 1.5 * atr_f)
        scalp_target = smart_round(price + 2.0 * atr_f)
        risk_reward = round(2.0 * atr_f / (1.5 * atr_f), 2) if atr_f > 0 else 1.33

        return {
            "ticker": ticker.upper(),
            "score": round(score, 1),
            "signal": signal,
            "price": smart_round(price),
            "entry_price": smart_round(price),
            "stop_loss": scalp_stop,
            "take_profit": scalp_target,
            "risk_reward": risk_reward,
            "risk_level": "high" if atr_f / price > 0.02 else "medium",
            "signals": signals,
            "vwap": smart_round(vwap) if vwap else None,
            "vwap_pct": vwap_pct,
            "vol_ratio": vol_ratio,
            "gap_pct": gap_pct,
            "indicators": {
                "rsi": round(float(rsi_val), 1) if pd.notna(rsi_val) else None,
                "macd_hist": round(float(macd_hist), 4) if pd.notna(macd_hist) else None,
                "ema_9": smart_round(float(ema_9)) if pd.notna(ema_9) else None,
                "ema_21": smart_round(float(ema_21)) if pd.notna(ema_21) else None,
                "atr": round(atr_f, 4),
                "stoch_k": round(float(stoch_k), 1) if pd.notna(stoch_k) else None,
                "vol_ratio": vol_ratio,
            },
        }
    except Exception:
        return None


# ── Breakout Detection Scoring ────────────────────────────────────────

def _score_breakout(ticker: str) -> dict[str, Any] | None:
    """Score a ticker for breakout readiness.

    Detects consolidation via Bollinger Band squeeze, low ADX, declining
    volume, and proximity to resistance.  Returns a "readiness" score
    and distance-to-breakout percentage.
    """
    try:
        from ta.momentum import RSIIndicator
        from ta.trend import MACD, EMAIndicator, ADXIndicator
        from ta.volatility import BollingerBands, AverageTrueRange

        df = _yf_history(ticker, period="6mo", interval="1d")
        if df.empty or len(df) < 60:
            return None

        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]
        price = float(close.iloc[-1])
        if price <= 0:
            return None

        rsi_val = RSIIndicator(close=close, window=14).rsi().iloc[-1]
        macd_obj = MACD(close=close)
        macd_hist = macd_obj.macd_diff().iloc[-1]
        ema_20 = EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
        ema_50 = EMAIndicator(close=close, window=50).ema_indicator().iloc[-1]
        adx_val = ADXIndicator(high=high, low=low, close=close).adx().iloc[-1]
        atr_val = AverageTrueRange(high=high, low=low, close=close).average_true_range().iloc[-1]
        bb = BollingerBands(close=close, window=20, window_dev=2)
        bb_upper = bb.bollinger_hband()
        bb_lower = bb.bollinger_lband()
        bb_width = bb.bollinger_wband()

        # Resistance: 20-day high
        resistance = float(high.rolling(20).max().iloc[-1])
        dist_to_breakout = round((resistance - price) / price * 100, 2)

        # Bollinger Band width squeeze detection
        bb_width_series = bb_width.dropna()
        if len(bb_width_series) < 20:
            return None
        current_bb_width = float(bb_width_series.iloc[-1])
        bb_width_pct_rank = float(
            (bb_width_series < current_bb_width).sum() / len(bb_width_series) * 100
        )
        is_squeeze = bb_width_pct_rank < 25

        # Volume trend (declining = compression before expansion)
        vol_recent = float(volume.iloc[-5:].mean())
        vol_prior = float(volume.iloc[-20:-5].mean()) if len(volume) >= 20 else vol_recent
        vol_declining = vol_recent < vol_prior * 0.8
        vol_trend_pct = round((vol_recent - vol_prior) / vol_prior * 100, 1) if vol_prior > 0 else 0

        # Consolidation days: how many recent bars stayed within a tight range
        recent_range = high.iloc[-20:].max() - low.iloc[-20:].min()
        daily_ranges = high.iloc[-20:] - low.iloc[-20:]
        tight_days = int((daily_ranges < recent_range * 0.3).sum())

        # Scoring
        score = 5.0
        signals: list[str] = []

        # BB squeeze = consolidation
        if is_squeeze:
            score += 2.0
            signals.append(f"Bollinger squeeze (width percentile: {bb_width_pct_rank:.0f}%)")

        # Near resistance
        if 0 <= dist_to_breakout <= 2.0:
            score += 1.5
            signals.append(f"Near resistance — {dist_to_breakout:.1f}% to breakout")
        elif 2.0 < dist_to_breakout <= 5.0:
            score += 0.5
            signals.append(f"{dist_to_breakout:.1f}% below resistance")

        # Already breaking out
        if dist_to_breakout <= 0:
            score += 2.0
            signals.append("BREAKING OUT — new 20-day high!")

        # Low ADX = consolidation, not trending
        if pd.notna(adx_val):
            if adx_val < 20:
                score += 1.0
                signals.append(f"Low ADX ({adx_val:.0f}) — consolidating")
            elif adx_val > 30:
                score -= 0.5

        # Declining volume = coiling
        if vol_declining:
            score += 0.8
            signals.append(f"Volume declining ({vol_trend_pct:+.0f}%) — coiling")

        # EMA support
        if pd.notna(ema_20) and pd.notna(ema_50):
            if price > float(ema_20) > float(ema_50):
                score += 0.5
                signals.append("Above rising EMAs — bullish base")

        # RSI neutral zone (not overbought for a pre-breakout)
        if pd.notna(rsi_val):
            if 45 <= rsi_val <= 65:
                score += 0.5
                signals.append(f"RSI neutral ({rsi_val:.0f}) — room to run")
            elif rsi_val > 70:
                score -= 1.0
                signals.append(f"RSI overbought ({rsi_val:.0f}) — may fade")

        # MACD building
        if pd.notna(macd_hist) and float(macd_hist) > 0:
            score += 0.5
            signals.append("MACD histogram positive — momentum building")

        score = max(1.0, min(10.0, score))

        if dist_to_breakout <= 0:
            status = "breaking_out"
        elif score >= 7:
            status = "ready"
        elif score >= 5:
            status = "watch"
        else:
            status = "wait"

        atr_f = float(atr_val) if pd.notna(atr_val) else price * 0.02

        return {
            "ticker": ticker.upper(),
            "score": round(score, 1),
            "signal": status,
            "status": status,
            "price": smart_round(price),
            "resistance": smart_round(resistance),
            "dist_to_breakout": dist_to_breakout,
            "bb_squeeze": is_squeeze,
            "bb_width_pctile": round(bb_width_pct_rank, 0),
            "adx": round(float(adx_val), 1) if pd.notna(adx_val) else None,
            "vol_trend_pct": vol_trend_pct,
            "tight_days": tight_days,
            "risk_level": "medium" if status == "watch" else "high",
            "signals": signals,
            "entry_price": smart_round(resistance),
            "stop_loss": smart_round(resistance - 2 * atr_f),
            "take_profit": smart_round(resistance + 3 * atr_f),
            "indicators": {
                "rsi": round(float(rsi_val), 1) if pd.notna(rsi_val) else None,
                "macd_hist": round(float(macd_hist), 4) if pd.notna(macd_hist) else None,
                "adx": round(float(adx_val), 1) if pd.notna(adx_val) else None,
                "atr": round(atr_f, 4),
                "ema_20": smart_round(float(ema_20)) if pd.notna(ema_20) else None,
                "ema_50": smart_round(float(ema_50)) if pd.notna(ema_50) else None,
                "bb_width_pctile": round(bb_width_pct_rank, 0),
            },
        }
    except Exception:
        return None


# ── Custom Screener ───────────────────────────────────────────────────

PRESET_SCREENS: dict[str, dict[str, Any]] = {
    "ema_stack_bullish": {
        "name": "EMA Stacking (Bullish)",
        "description": "Price > EMA20 > EMA50 > EMA100",
        "conditions": [{"field": "ema_stack_bullish", "op": "eq", "value": True}],
        "confirmations": [
            {"field": "adx", "op": "gte", "value": 20, "label": "ADX > 20 (trending)"},
            {"field": "rsi", "op": "between", "value": [40, 70], "label": "RSI 40-70 (not overbought)"},
            {"field": "macd_hist", "op": "gt", "value": 0, "label": "MACD histogram positive"},
        ],
    },
    "ema_stack_bearish": {
        "name": "EMA Stacking (Bearish)",
        "description": "Price < EMA20 < EMA50 < EMA100",
        "conditions": [{"field": "ema_stack_bearish", "op": "eq", "value": True}],
    },
    "oversold_bounce": {
        "name": "Oversold Bounce",
        "description": "RSI below 30 with MACD turning positive",
        "conditions": [
            {"field": "rsi", "op": "lt", "value": 30},
            {"field": "macd_hist", "op": "gt", "value": 0},
        ],
    },
    "golden_cross": {
        "name": "Golden Cross Setup",
        "description": "EMA20 crossed above EMA50 with price above both",
        "conditions": [
            {"field": "ema_20", "op": "gt_field", "value": "ema_50"},
            {"field": "price", "op": "gt_field", "value": "ema_20"},
            {"field": "adx", "op": "gte", "value": 20},
        ],
    },
    "vol_breakout": {
        "name": "Volume Breakout",
        "description": "Volume 2x above average with bullish EMA alignment",
        "conditions": [
            {"field": "vol_ratio", "op": "gte", "value": 2.0},
            {"field": "ema_20", "op": "gt_field", "value": "ema_50"},
            {"field": "rsi", "op": "between", "value": [45, 75]},
        ],
    },
    "bb_squeeze_bullish": {
        "name": "Bollinger Squeeze (Bullish)",
        "description": "Price near lower BB with RSI oversold",
        "conditions": [
            {"field": "bb_pct", "op": "lt", "value": 15},
            {"field": "rsi", "op": "lt", "value": 35},
        ],
    },
    "day_trade": {
        "name": "Day Trade Momentum",
        "description": "Intraday momentum with volume surge, VWAP support, and RSI in the sweet spot",
        "scan_type": "intraday",
        "conditions": [],
    },
    "breakout_watch": {
        "name": "Breakout Watchlist",
        "description": "Consolidating near resistance with Bollinger squeeze — wait for the breakout",
        "scan_type": "breakout",
        "conditions": [],
    },
}


# ── Batch runners for day-trade / breakout scans ──────────────────────

def run_daytrade_scan(max_results: int = 30) -> dict[str, Any]:
    """Pre-filter with FinViz day-trade signals, then score intraday."""
    from .prescreener import get_daytrade_candidates

    start = time.time()
    candidates = get_daytrade_candidates()
    logger.info(f"[trading] Day-trade scan: {len(candidates)} candidates")

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=_MAX_SCAN_WORKERS) as executor:
        futures = {executor.submit(_score_ticker_intraday, t): t for t in candidates}
        for future in as_completed(futures):
            if _shutting_down.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                scored = future.result()
                if scored is not None:
                    results.append(scored)
            except Exception:
                pass

    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[:max_results]
    elapsed = round(time.time() - start, 1)

    return {
        "ok": True,
        "scan_type": "day_trade",
        "candidates_scanned": len(candidates),
        "matches": len(results),
        "elapsed_s": elapsed,
        "results": results,
    }


def run_breakout_scan(max_results: int = 30) -> dict[str, Any]:
    """Pre-filter with FinViz consolidation signals, then score for breakout readiness."""
    from .prescreener import get_breakout_candidates

    start = time.time()
    candidates = get_breakout_candidates()
    logger.info(f"[trading] Breakout scan: {len(candidates)} candidates")

    # Pre-warm the OHLCV cache for breakout scoring (uses 6mo daily)
    _prewarm_cache(candidates)

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=_MAX_SCAN_WORKERS) as executor:
        futures = {executor.submit(_score_breakout, t): t for t in candidates}
        for future in as_completed(futures):
            if _shutting_down.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                scored = future.result()
                if scored is not None:
                    results.append(scored)
            except Exception:
                pass

    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[:max_results]
    elapsed = round(time.time() - start, 1)

    return {
        "ok": True,
        "scan_type": "breakout",
        "candidates_scanned": len(candidates),
        "matches": len(results),
        "elapsed_s": elapsed,
        "results": results,
    }


def _eval_condition(cond: dict, scored: dict) -> bool:
    """Evaluate a single screening condition against a scored ticker result."""
    field = cond["field"]
    op = cond["op"]
    value = cond["value"]

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
    """Run a preset or custom screen against the pre-filtered candidate pool."""
    from .prescreener import get_prescreened_candidates

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

    scan_list = tickers or get_prescreened_candidates()
    scored_all = batch_score_tickers(
        scan_list, max_workers=_MAX_SCAN_WORKERS, skip_fundamentals=True,
    )

    matches = []
    for scored in scored_all:
        if all(_eval_condition(c, scored) for c in conds):
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


_last_scan_cache: dict[str, Any] = {"results": [], "timestamp": None, "tickers_key": ""}


def run_scan(
    db: Session, user_id: int | None,
    tickers: list[str] | None = None,
    use_full_universe: bool = False,
) -> list[dict[str, Any]]:
    """Scan a list of tickers, score them, store results, return sorted.

    Uses cached results if the same scan ran within the last 2 hours.
    Pre-warms the OHLCV cache with a batch download before scoring.
    When *use_full_universe* is True, uses pre-screened candidates instead
    of the raw 5000+ ticker universe for dramatically faster scans.
    """
    from ...models.trading import ScanResult
    from .prescreener import get_prescreened_candidates

    if tickers:
        scan_list = tickers
    elif use_full_universe:
        scan_list = get_prescreened_candidates()
    else:
        scan_list = list(ALL_SCAN_TICKERS)

    cache_key = ",".join(sorted(scan_list[:100]))
    if (_last_scan_cache["timestamp"]
            and _last_scan_cache["tickers_key"] == cache_key
            and datetime.utcnow() - _last_scan_cache["timestamp"] < timedelta(hours=2)
            and _last_scan_cache["results"]):
        logger.info(f"[trading] Returning cached scan ({len(_last_scan_cache['results'])} results)")
        return _last_scan_cache["results"]

    skip_fund = use_full_universe or len(scan_list) > 100
    _prewarm_cache(scan_list)

    if len(scan_list) >= 10:
        results = batch_score_tickers(
            scan_list, max_workers=_MAX_SCAN_WORKERS,
            skip_fundamentals=skip_fund,
        )
    else:
        results = []
        for ticker in scan_list:
            scored = _score_ticker(ticker, skip_fundamentals=skip_fund)
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

    _last_scan_cache["results"] = results
    _last_scan_cache["timestamp"] = datetime.utcnow()
    _last_scan_cache["tickers_key"] = cache_key

    return results


def _prewarm_cache(tickers: list[str]) -> None:
    """Pre-warm OHLCV cache with a batch download (single HTTP request per ~50 tickers)."""
    BATCH_SIZE = 50
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i + BATCH_SIZE]
        try:
            batch_download(chunk, period="6mo", interval="1d")
        except Exception:
            pass


def get_latest_scan(db: Session, user_id: int | None, limit: int = 20) -> list[dict]:
    from ...models.trading import ScanResult

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


# ── Signal Generation ─────────────────────────────────────────────────

def generate_signals(db: Session, user_id: int | None) -> list[dict[str, Any]]:
    """Generate buy/hold/sell signals for all watchlist tickers."""
    from ...models.trading import BacktestResult

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

        best_bt = db.query(BacktestResult).filter(
            BacktestResult.ticker == w.ticker,
        ).order_by(BacktestResult.return_pct.desc()).first()

        bt_confidence = 0
        if best_bt and best_bt.win_rate > 50:
            bt_confidence = min(30, best_bt.win_rate - 50)

        base_confidence = (scored["score"] / 10) * 70
        confidence = min(95, base_confidence + bt_confidence)

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


# ── Batch Concurrent Scanner ──────────────────────────────────────────

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
    return dict(_scan_status)


def batch_score_tickers(
    tickers: list[str],
    max_workers: int = _MAX_SCAN_WORKERS,
    progress_callback: Any = None,
    skip_fundamentals: bool = False,
) -> list[dict[str, Any]]:
    """Score many tickers concurrently using a thread pool."""
    results: list[dict[str, Any]] = []
    total = len(tickers)
    completed = 0
    errors = 0

    def _score_one(ticker: str) -> dict[str, Any] | None:
        try:
            return _score_ticker(ticker, skip_fundamentals=skip_fundamentals)
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
    """Scan the market using pre-screened candidates, store results, return sorted.

    Uses the prescreener (FinViz + yfinance server-side screens) to narrow
    the universe to ~200-400 interesting candidates before deep-scoring.
    """
    from ...models.trading import ScanResult
    from .prescreener import get_prescreened_candidates

    _scan_status["running"] = True
    _scan_status["phase"] = "pre-filtering"
    _scan_status["errors"] = 0

    if use_full_universe:
        scan_list = get_prescreened_candidates()
    else:
        scan_list = list(ALL_SCAN_TICKERS)

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

    logger.info(f"[trading] Full market scan starting: {len(scan_list)} pre-screened candidates")

    _scan_status["phase"] = "pre-warming cache"
    _prewarm_cache(scan_list)
    _scan_status["phase"] = "scoring"

    results = batch_score_tickers(
        scan_list, progress_callback=_progress, skip_fundamentals=True,
    )

    _scan_status["tickers_scanned"] = len(scan_list)
    _scan_status["tickers_scored"] = len(results)
    _scan_status["progress_pct"] = 100
    _scan_status["phase"] = "storing"

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


# ── Smart Pick ────────────────────────────────────────────────────────

def smart_pick(
    db: Session, user_id: int | None,
    message: str | None = None,
    budget: float | None = None,
    risk_tolerance: str = "medium",
) -> dict[str, Any]:
    """Scan the market, score all candidates, deep-analyze the top picks."""
    from ...models.trading import ScanResult, BacktestResult
    from sqlalchemy import or_
    from ..ticker_universe import get_full_ticker_universe, get_ticker_count

    recent_cutoff = datetime.utcnow() - timedelta(hours=2)
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
        universe = get_full_ticker_universe()
        total_scanned = len(universe)
        all_scored = batch_score_tickers(universe, max_workers=_MAX_SCAN_WORKERS)
        scored_results = [s for s in all_scored if s["signal"] == "buy" and s["score"] >= 5.5]

    watchlist = get_watchlist(db, user_id)
    existing_tickers = {s["ticker"] for s in scored_results}
    for w in watchlist:
        if w.ticker not in existing_tickers:
            scored = _score_ticker(w.ticker)
            if scored and scored["score"] >= 4.0:
                scored_results.append(scored)

    scored_results.sort(key=lambda r: r["score"], reverse=True)

    if risk_tolerance == "low":
        scored_results = [s for s in scored_results if s["risk_level"] in ("low", "medium")]

    top_picks = scored_results[:8]

    if not top_picks:
        return {
            "ok": True,
            "reply": f"I scanned {total_scanned:,} stocks and crypto and none have a strong enough setup right now. "
                     "The best trade is sometimes no trade. I'll keep watching and flag opportunities as they appear.",
            "picks": [],
        }

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

        best_bt = db.query(BacktestResult).filter(
            BacktestResult.ticker == p["ticker"],
        ).order_by(BacktestResult.return_pct.desc()).first()
        if best_bt:
            detail += (
                f"\n  Best backtest: {best_bt.strategy_name} → "
                f"{best_bt.return_pct:+.1f}% return, {best_bt.win_rate:.0f}% win rate"
            )

        pick_details.append(detail)

    context_parts = [
        f"## MARKET SCAN RESULTS — Top {len(top_picks)} candidates from {total_scanned:,} stocks & crypto scanned",
        "\n\n".join(pick_details),
    ]

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

    insights = get_insights(db, user_id, limit=10)
    if insights:
        lines = ["## LEARNED PATTERNS (your edge)"]
        for ins in insights:
            lines.append(f"- [{ins.confidence:.0%}] {ins.pattern_description}")
        context_parts.append("\n".join(lines))

    if budget:
        context_parts.append(f"## BUDGET\nUser has ${budget:,.2f} available to invest.")

    context_parts.append(f"## RISK TOLERANCE: {risk_tolerance.upper()}")

    try:
        from .. import broker_service
        portfolio_ctx = broker_service.build_portfolio_context()
        if portfolio_ctx:
            context_parts.insert(0, portfolio_ctx)
    except Exception:
        pass

    full_context = "\n\n".join(context_parts)

    user_msg = message or "Based on this scan, what are your top 3 stock picks I should buy RIGHT NOW? For each one, give me the exact buy-in price, sell target, stop-loss, expected hold duration, position size, and your confidence level. Rank them by conviction."

    from ...prompts import load_prompt
    system_prompt = load_prompt("trading_analyst")

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
        from ... import openai_client
        from ...logger import new_trace_id
        trace_id = new_trace_id()

        result = openai_client.chat(
            messages=[{"role": "user", "content": user_msg}],
            system_prompt=f"{system_prompt}\n{smart_pick_addendum}\n\n---\n\n{full_context}",
            trace_id=trace_id,
            user_message=user_msg,
            max_tokens=2048,
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
