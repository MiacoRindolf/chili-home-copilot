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
    get_market_regime,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights

logger = logging.getLogger(__name__)

_shutting_down = threading.Event()
_MAX_SCAN_WORKERS = 12

_top_picks_cache: dict[str, Any] = {"picks": [], "ts": 0.0}
_TOP_PICKS_TTL = 300  # 5 minutes — data doesn't change meaningfully faster
_TOP_PICKS_STALE_TTL = 600  # 10 minutes — serve stale while refreshing in background
_top_picks_refresh_lock = threading.Lock()


def signal_shutdown():
    _shutting_down.set()


# ── Adaptive Weight System ────────────────────────────────────────────
# Starting defaults are informed by common momentum strategies, but CHILI's
# brain continuously adjusts them via backtest validation and pattern mining.
# Every learning cycle, evolve_strategy_weights() recalibrates these based
# on what the data actually shows — no assumption is sacred.

_DEFAULT_WEIGHTS: dict[str, float] = {
    "macd_positive_bonus": 1.5,
    "macd_negative_penalty": -2.0,
    "float_micro_bonus": 2.0,
    "float_low_bonus": 1.0,
    "float_high_penalty": -0.5,
    "vol_surge_5x": 2.0,
    "vol_surge_3x": 1.0,
    "price_sweetspot_bonus": 0.5,
    "price_out_of_range_penalty": -1.0,
    "topping_tail_penalty": -1.5,
    "extended_pullback_penalty": -1.0,
    "volume_profile_bonus": 0.5,
    "pullback_clean_bonus": 1.5,
    "daily_gainer_10pct": 1.0,
    "daily_gainer_5pct": 0.5,
    "time_of_day_bonus": 0.5,
    "macd_negative_cap": 4.0,
    "immaculate_min_score": 8.0,
    "immaculate_min_vol": 3.0,
    "immaculate_min_rr": 2.0,
    "regime_risk_off_penalty": -0.5,
    "regime_risk_on_bonus": 0.3,
    "regime_vix_breakout_penalty": -0.8,
    "regime_spy_concordance": 0.5,
    "stoch_oversold_macd_bonus": 0.8,
    "stoch_overbought_macd_penalty": -0.8,
    "stoch_crossover_bonus": 0.5,
}

_adaptive_weights: dict[str, float] = dict(_DEFAULT_WEIGHTS)
_weights_lock = threading.Lock()


def get_adaptive_weight(key: str) -> float:
    """Read a scoring weight — returns the brain-adjusted value."""
    with _weights_lock:
        return _adaptive_weights.get(key, _DEFAULT_WEIGHTS.get(key, 0.0))


def _brain_meta() -> dict[str, Any]:
    """Lightweight brain metadata to attach to every scan response."""
    with _weights_lock:
        adjusted = {k for k, v in _adaptive_weights.items() if v != _DEFAULT_WEIGHTS.get(k)}
    return {
        "brain_adjusted_weights": len(adjusted),
        "immaculate_thresholds": {
            "min_score": get_adaptive_weight("immaculate_min_score"),
            "min_vol": get_adaptive_weight("immaculate_min_vol"),
            "min_rr": get_adaptive_weight("immaculate_min_rr"),
        },
    }


def get_all_weights() -> dict[str, float]:
    """Snapshot of current adaptive weights for diagnostics."""
    with _weights_lock:
        return dict(_adaptive_weights)


def _apply_learned_weights(overrides: dict[str, float]) -> None:
    """Merge brain-computed weight adjustments into the live weights."""
    with _weights_lock:
        for k, v in overrides.items():
            if k in _DEFAULT_WEIGHTS:
                default = _DEFAULT_WEIGHTS[k]
                floor = default * 0.2 if default > 0 else default * 3.0
                ceil = default * 3.0 if default > 0 else default * 0.2
                _adaptive_weights[k] = round(max(floor, min(ceil, v)), 3)


def evolve_strategy_weights(db: Session) -> dict[str, Any]:
    """Called during each learning cycle to let the brain refine its own
    scoring weights based on actual pattern performance.

    Reads active TradingInsights, groups them by the scoring factor they
    relate to, and nudges each weight up or down based on whether the
    patterns that use that factor are being validated or invalidated by
    real data.

    This is how CHILI outgrows any single teacher's strategy — the data
    decides what works.
    """
    from .portfolio import get_insights

    insights = get_insights(db, user_id=None, limit=100)
    if not insights:
        return {"adjusted": 0, "note": "no insights yet"}

    FACTOR_KEYWORDS: dict[str, list[str]] = {
        "macd_positive_bonus": ["macd positive", "macd bullish", "macd+", "histogram rising"],
        "macd_negative_penalty": ["macd negative", "macd flipped", "momentum lost", "setup invalidated"],
        "float_micro_bonus": ["micro float", "low float"],
        "float_low_bonus": ["low float", "float"],
        "vol_surge_5x": ["volume surge 5x", "massive volume", "5x"],
        "vol_surge_3x": ["volume surge", "high volume", "relative volume", "3x"],
        "topping_tail_penalty": ["topping tail", "reversal warning", "volume top"],
        "extended_pullback_penalty": ["extended pullback", "7+ red", "setup dead", "consecutive red"],
        "pullback_clean_bonus": ["clean pullback", "first pullback", "bread-and-butter", "pullback"],
        "volume_profile_bonus": ["volume profile", "green vol", "buyers stronger"],
        "daily_gainer_10pct": ["gapper", "top gainer", "10%+"],
        "time_of_day_bonus": ["morning session", "prime trading", "time of day"],
        "regime_risk_off_penalty": ["risk-off", "risk off", "bearish regime"],
        "regime_risk_on_bonus": ["risk-on", "risk on", "bullish regime"],
        "regime_vix_breakout_penalty": ["high vix", "elevated vix", "false breakout"],
        "regime_spy_concordance": ["spy concordance", "spy direction", "market alignment"],
        "stoch_oversold_macd_bonus": ["stoch oversold", "stochastic oversold", "double bottom"],
        "stoch_overbought_macd_penalty": ["stoch overbought", "stochastic overbought"],
        "stoch_crossover_bonus": ["stoch crossover", "stochastic crossover", "bullish crossover from oversold"],
    }

    adjustments: dict[str, float] = {}
    details: list[str] = []

    for factor_key, keywords in FACTOR_KEYWORDS.items():
        related = [
            ins for ins in insights
            if any(kw in ins.pattern_description.lower() for kw in keywords)
        ]
        if not related:
            continue

        avg_conf = sum(ins.confidence for ins in related) / len(related)
        total_evidence = sum(ins.evidence_count for ins in related)

        if total_evidence < 5:
            continue

        default = _DEFAULT_WEIGHTS[factor_key]
        if avg_conf >= 0.7:
            new_val = default * (1.0 + (avg_conf - 0.5) * 0.5)
        elif avg_conf <= 0.3:
            new_val = default * max(0.3, avg_conf / 0.5)
        else:
            new_val = default

        if abs(new_val - default) > 0.05:
            adjustments[factor_key] = round(new_val, 3)
            direction = "up" if abs(new_val) > abs(default) else "down"
            details.append(
                f"{factor_key}: {default}->{new_val:.3f} ({direction}, "
                f"avg_conf={avg_conf:.0%}, evidence={total_evidence})"
            )

    if adjustments:
        _apply_learned_weights(adjustments)
        from .learning import log_learning_event
        log_learning_event(
            db, None, "weight_evolution",
            f"Brain evolved {len(adjustments)} scoring weights: {'; '.join(details[:5])}",
        )

    return {
        "adjusted": len(adjustments),
        "details": details,
        "current_weights": get_all_weights(),
    }


# ── Single ticker scoring ─────────────────────────────────────────────

_score_cache: dict[tuple, tuple[float, dict | None]] = {}
_score_cache_lock = threading.Lock()
_SCORE_CACHE_TTL = 180  # 3 min
_SCORE_CACHE_MAX = 300


def _score_ticker(ticker: str, *, skip_fundamentals: bool = False) -> dict[str, Any] | None:
    """Score a single ticker using multi-signal confluence (1-10).

    When *skip_fundamentals* is True the expensive ``get_fundamentals()``
    call is skipped — used during bulk scans where FinViz already
    pre-filtered for fundamental quality.

    Results are cached for 3 minutes keyed on (ticker, skip_fundamentals).
    """
    cache_key = (ticker.upper(), skip_fundamentals)
    now = time.time()
    with _score_cache_lock:
        entry = _score_cache.get(cache_key)
        if entry and now - entry[0] < _SCORE_CACHE_TTL:
            return entry[1]

    result = _score_ticker_impl(ticker, skip_fundamentals=skip_fundamentals)

    with _score_cache_lock:
        if len(_score_cache) >= _SCORE_CACHE_MAX:
            cutoff = now - _SCORE_CACHE_TTL
            stale = [k for k, v in _score_cache.items() if v[0] < cutoff]
            for k in stale:
                del _score_cache[k]
        _score_cache[cache_key] = (now, result)
    return result


def _score_ticker_impl(ticker: str, *, skip_fundamentals: bool = False) -> dict[str, Any] | None:
    """Actual scoring logic (no cache)."""
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
            latest_close = float(df["Close"].iloc[-1])
            latest_open = float(df["Open"].iloc[-1])
            if latest_close >= latest_open:
                score += 0.5
                signals.append("Volume surge (accumulation)")
            else:
                score -= 0.5
                signals.append("Volume surge (distribution)")

        # ── Recent price trend — penalise falling knives ──
        if len(df) >= 6:
            _ret_5d = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-6]) - 1) * 100
            if _ret_5d < -15:
                score -= 2.0
                signals.append(f"Sharp decline ({_ret_5d:.1f}% in 5 days)")
            elif _ret_5d < -8:
                score -= 1.0
                signals.append(f"Falling ({_ret_5d:.1f}% in 5 days)")

        # ── Stochastic scoring (brain-adaptive) ──
        if pd.notna(stoch_k):
            stoch_d = stoch.stoch_signal().iloc[-1] if hasattr(stoch, 'stoch_signal') else None
            sk = float(stoch_k)

            if sk < 20 and pd.notna(macd_val) and pd.notna(macd_sig) and float(macd_val) > float(macd_sig):
                score += get_adaptive_weight("stoch_oversold_macd_bonus")
                signals.append(f"Stoch oversold ({sk:.0f}) + MACD bullish — double-bottom bounce")
            elif sk > 80 and pd.notna(macd_val) and pd.notna(macd_sig) and float(macd_val) < float(macd_sig):
                score += get_adaptive_weight("stoch_overbought_macd_penalty")
                signals.append(f"Stoch overbought ({sk:.0f}) + MACD bearish — sell signal")

            if stoch_d is not None and pd.notna(stoch_d):
                sd = float(stoch_d)
                if sk > sd and sk < 25:
                    score += get_adaptive_weight("stoch_crossover_bonus")
                    signals.append(f"Stoch bullish crossover from oversold ({sk:.0f}>{sd:.0f})")

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

        # ── Market regime modifier (brain-adaptive) ──
        try:
            _regime = get_market_regime()
            _regime_label = _regime.get("regime", "cautious")
            if _regime_label == "risk_off":
                score += get_adaptive_weight("regime_risk_off_penalty")
                if pd.notna(rsi_val) and rsi_val < 35:
                    score += 0.3
                    signals.append("Risk-off regime but oversold — contra play")
                else:
                    signals.append("Risk-off regime — penalised")
            elif _regime_label == "risk_on":
                if pd.notna(rsi_val) and rsi_val < 60:
                    score += get_adaptive_weight("regime_risk_on_bonus")
                    signals.append("Risk-on regime — momentum boost")
        except Exception:
            pass

        # ── Falling-knife gate: below major MAs = extra penalty ──
        _below_sma50 = pd.notna(sma_50) and price < float(sma_50)
        _below_ema50 = pd.notna(ema_50) and price < float(ema_50)
        if _below_sma50 and _below_ema50:
            score -= 1.0
            signals.append("Below SMA50 & EMA50 — falling-knife risk")

        score = max(1.0, min(10.0, score))

        if score >= 7:
            signal = "buy"
        elif score <= 3.5:
            signal = "sell"
        else:
            signal = "hold"

        atr_f = float(atr_val) if pd.notna(atr_val) else price * 0.02
        _cr = is_crypto_ticker

        volatility_pct = (atr_f / price * 100) if price > 0 else 5
        _stop_mult = 2.5 if volatility_pct > 3 else 2.0
        stop_loss = smart_round(price - _stop_mult * atr_f, crypto=_cr)
        take_profit = smart_round(price + 3 * atr_f, crypto=_cr)
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
            "price": smart_round(price, crypto=_cr),
            "entry_price": smart_round(price, crypto=_cr),
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
                "atr": round(atr_f, 6 if _cr else 4),
                "ema_20": smart_round(float(ema_20), crypto=_cr) if pd.notna(ema_20) else None,
                "ema_50": smart_round(float(ema_50), crypto=_cr) if pd.notna(ema_50) else None,
                "ema_100": smart_round(float(ema_100), crypto=_cr) if ema_100 is not None and pd.notna(ema_100) else None,
                "ema_200": smart_round(float(ema_200), crypto=_cr) if ema_200 is not None and pd.notna(ema_200) else None,
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

    Evaluates momentum, VWAP positioning, volume surge, MACD gating,
    pullback quality, float, time-of-day, and ATR-based risk on 5 days
    of 15m bars.
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

        # Gap from previous close (daily % change)
        if len(df) > 26:
            prev_day_close = float(close.iloc[-27]) if len(df) >= 27 else price
            gap_pct = round((float(df["Open"].iloc[-1]) - prev_day_close) / prev_day_close * 100, 2) if prev_day_close > 0 else 0
        else:
            gap_pct = 0.0

        # Daily change % (from today's open to current price)
        daily_change_pct = 0.0
        if len(today_df) > 0:
            today_open = float(today_df["Open"].iloc[0])
            if today_open > 0:
                daily_change_pct = round((price - today_open) / today_open * 100, 2)

        # ── MACD negative disqualification flag ──
        macd_negative = (
            pd.notna(macd_val) and pd.notna(macd_sig) and pd.notna(macd_hist)
            and float(macd_val) < float(macd_sig) and float(macd_hist) < 0
        )

        # Scoring
        score = 5.0
        signals: list[str] = []

        # Momentum: RSI sweet spot for day trades
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

        # MACD momentum (brain-adaptive gating)
        if pd.notna(macd_val) and pd.notna(macd_sig):
            if macd_val > macd_sig and pd.notna(macd_hist) and float(macd_hist) > 0:
                score += get_adaptive_weight("macd_positive_bonus")
                signals.append("MACD bullish + positive histogram — momentum confirmed")
            elif macd_negative:
                score += get_adaptive_weight("macd_negative_penalty")
                signals.append("MACD negative — momentum lost, avoid entry")

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

        # Volume surge (brain-adaptive tiers)
        if vol_ratio >= 5.0:
            score += get_adaptive_weight("vol_surge_5x")
            signals.append(f"Massive volume surge ({vol_ratio:.1f}x avg) — high conviction")
        elif vol_ratio >= 3.0:
            score += get_adaptive_weight("vol_surge_3x") + 0.5
            signals.append(f"Volume explosion ({vol_ratio:.1f}x avg)")
        elif vol_ratio >= 2.0:
            score += get_adaptive_weight("vol_surge_3x")
            signals.append(f"Strong volume surge ({vol_ratio:.1f}x avg)")
        elif vol_ratio >= 1.5:
            score += 0.5
            signals.append(f"Above-average volume ({vol_ratio:.1f}x)")

        # Gap play
        if abs(gap_pct) > 2:
            score += 0.5
            signals.append(f"Gap {'up' if gap_pct > 0 else 'down'} {gap_pct:+.1f}%")

        # ── Daily gainer check (brain-adaptive) ──
        if daily_change_pct >= 10.0:
            score += get_adaptive_weight("daily_gainer_10pct")
            signals.append(f"Top gainer today (+{daily_change_pct:.1f}%)")
        elif daily_change_pct >= 5.0:
            score += get_adaptive_weight("daily_gainer_5pct")
            signals.append(f"Strong gainer today (+{daily_change_pct:.1f}%)")

        # ── Float factor (brain-adaptive — learns its own float preferences) ──
        _cr = ticker.upper().endswith("-USD")
        if not _cr:
            try:
                fund = get_fundamentals(ticker)
                if fund and fund.get("market_cap") and price > 0:
                    shares = fund["market_cap"] / price
                    if shares < 5_000_000:
                        score += get_adaptive_weight("float_micro_bonus")
                        signals.append(f"Micro float ({shares/1e6:.1f}M) — explosive potential")
                    elif shares < 20_000_000:
                        score += get_adaptive_weight("float_low_bonus") * 0.8
                        signals.append(f"Low float ({shares/1e6:.1f}M)")
            except Exception:
                pass

        # ── Pullback detection (brain-adaptive — clean first pullback) ──
        if len(today_df) >= 5 and not macd_negative:
            today_high = float(today_df["High"].max())
            pullback_pct = (today_high - price) / today_high * 100 if today_high > 0 else 0
            if 2.0 <= pullback_pct <= 8.0:
                red_count = 0
                for i in range(len(today_df) - 1, max(0, len(today_df) - 8), -1):
                    if float(today_df["Close"].iloc[i]) < float(today_df["Open"].iloc[i]):
                        red_count += 1
                    else:
                        break
                if red_count <= 4:
                    score += get_adaptive_weight("pullback_clean_bonus")
                    signals.append(f"Clean pullback ({pullback_pct:.1f}% from high, {red_count} red bars) — entry zone")

        # ── Topping tail warning (brain-adaptive) ──
        last_open = float(df["Open"].iloc[-1])
        last_high = float(high.iloc[-1])
        body = abs(price - last_open)
        upper_wick = last_high - max(price, last_open)
        if body > 0 and upper_wick > 2 * body and vol_latest > vol_avg * 1.5:
            score += get_adaptive_weight("topping_tail_penalty") * 0.67
            signals.append("Topping tail on volume — reversal risk")

        # ── Time-of-day factor (brain-adaptive) ──
        try:
            from datetime import timezone, timedelta as _td
            last_ts = df.index[-1]
            if hasattr(last_ts, 'hour'):
                et_offset = last_ts.utcoffset() or _td(0)
                et_hour = last_ts.hour
                if 9 <= et_hour <= 10:
                    score += get_adaptive_weight("time_of_day_bonus")
                    signals.append("Prime trading window (morning session)")
        except Exception:
            pass

        # ── MACD negative cap (brain-adaptive) ──
        if macd_negative:
            score = min(score, get_adaptive_weight("macd_negative_cap"))

        # ── Market regime modifier (brain-adaptive) ──
        try:
            _regime = get_market_regime()
            _regime_label = _regime.get("regime", "cautious")
            _spy_dir = _regime.get("spy_direction", "flat")
            if _regime_label == "risk_off":
                score += get_adaptive_weight("regime_risk_off_penalty")
                signals.append("Risk-off regime — tighter filters")
            if _spy_dir in ("up",) and daily_change_pct > 0:
                score += get_adaptive_weight("regime_spy_concordance")
                signals.append("Trade direction aligns with SPY — concordance bonus")
            elif _spy_dir == "down" and daily_change_pct > 0:
                signals.append("Long against SPY trend — added caution")
        except Exception:
            pass

        score = max(1.0, min(10.0, score))

        if score >= 7:
            signal = "long"
        elif score <= 3.5:
            signal = "short"
        else:
            signal = "wait"

        atr_f = float(atr_val) if pd.notna(atr_val) else price * 0.01
        scalp_stop = smart_round(price - 1.5 * atr_f, crypto=_cr)
        scalp_target = smart_round(price + 2.5 * atr_f, crypto=_cr)
        risk_reward = round(2.5 * atr_f / (1.5 * atr_f), 2) if atr_f > 0 else 1.67

        return {
            "ticker": ticker.upper(),
            "score": round(score, 1),
            "signal": signal,
            "price": smart_round(price, crypto=_cr),
            "entry_price": smart_round(price, crypto=_cr),
            "stop_loss": scalp_stop,
            "take_profit": scalp_target,
            "risk_reward": risk_reward,
            "risk_level": "high" if atr_f / price > 0.02 else "medium",
            "signals": signals,
            "vwap": smart_round(vwap, crypto=_cr) if vwap else None,
            "vwap_pct": vwap_pct,
            "vol_ratio": vol_ratio,
            "gap_pct": gap_pct,
            "daily_change_pct": daily_change_pct,
            "macd_positive": not macd_negative,
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
    volume, and proximity to resistance.  Also evaluates momentum quality
    via MACD gating, float size, relative volume surge, pullback quality,
    and volume profile analysis.
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
        macd_line = macd_obj.macd().iloc[-1]
        macd_sig = macd_obj.macd_signal().iloc[-1]
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

        # Relative volume (latest bar vs 20-day average)
        vol_avg_20 = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else float(volume.mean())
        vol_latest = float(volume.iloc[-1])
        rel_vol = round(vol_latest / vol_avg_20, 2) if vol_avg_20 > 0 else 1.0

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

        # ── MACD gate (primary momentum filter — weight is brain-adaptive) ──
        if pd.notna(macd_hist) and pd.notna(macd_line) and pd.notna(macd_sig):
            if float(macd_line) > float(macd_sig) and float(macd_hist) > 0:
                score += get_adaptive_weight("macd_positive_bonus")
                signals.append("MACD positive + histogram rising — strong momentum")
            elif float(macd_hist) > 0:
                score += 0.5
                signals.append("MACD histogram positive — momentum building")
            elif float(macd_line) < float(macd_sig) and float(macd_hist) < 0:
                score += get_adaptive_weight("macd_negative_penalty")
                signals.append("MACD negative — momentum lost, avoid entry")

        # ── Relative volume surge (brain-adaptive thresholds) ──
        if rel_vol >= 5.0:
            score += get_adaptive_weight("vol_surge_5x")
            signals.append(f"Massive volume surge ({rel_vol:.1f}x avg) — high conviction")
        elif rel_vol >= 3.0:
            score += get_adaptive_weight("vol_surge_3x")
            signals.append(f"Strong relative volume ({rel_vol:.1f}x avg)")

        # ── Price range sweet spot ──
        _is_crypto = ticker.upper().endswith("-USD")
        if not _is_crypto:
            if 2.0 <= price <= 20.0:
                score += get_adaptive_weight("price_sweetspot_bonus")
                signals.append(f"Price ${price:.2f} in momentum sweet spot ($2-$20)")
            elif price < 1.0 or price > 50.0:
                score += get_adaptive_weight("price_out_of_range_penalty")

        # ── Float size (brain-adaptive — CHILI learns its own float preferences) ──
        if not _is_crypto:
            try:
                fund = get_fundamentals(ticker)
                if fund and fund.get("market_cap"):
                    shares = fund["market_cap"] / price if price > 0 else None
                    if shares:
                        if shares < 5_000_000:
                            score += get_adaptive_weight("float_micro_bonus")
                            signals.append(f"Micro float ({shares/1e6:.1f}M shares) — explosive potential")
                        elif shares < 20_000_000:
                            score += get_adaptive_weight("float_low_bonus")
                            signals.append(f"Low float ({shares/1e6:.1f}M shares)")
                        elif shares > 50_000_000:
                            score += get_adaptive_weight("float_high_penalty")
            except Exception:
                pass

        # ── Topping tail detection (brain-adaptive penalty) ──
        last_open = float(df["Open"].iloc[-1])
        last_high = float(high.iloc[-1])
        last_low = float(low.iloc[-1])
        last_close = float(close.iloc[-1])
        body = abs(last_close - last_open)
        upper_wick = last_high - max(last_close, last_open)
        if body > 0 and upper_wick > 2 * body and vol_latest > vol_avg_20 * 1.5:
            score += get_adaptive_weight("topping_tail_penalty")
            signals.append("Topping tail on high volume — reversal warning")

        # ── Pullback quality (consecutive red candles) ──
        consec_red = 0
        for i in range(len(close) - 1, max(0, len(close) - 15), -1):
            if float(close.iloc[i]) < float(df["Open"].iloc[i]):
                consec_red += 1
            else:
                break
        if consec_red >= 7:
            score += get_adaptive_weight("extended_pullback_penalty")
            signals.append(f"Extended pullback ({consec_red} red candles) — setup weakened")

        # ── Volume profile: green vs red candle volume ──
        last_10 = df.iloc[-10:]
        green_vol = float(last_10[last_10["Close"] >= last_10["Open"]]["Volume"].mean() or 0)
        red_vol = float(last_10[last_10["Close"] < last_10["Open"]]["Volume"].mean() or 0)
        if green_vol > 0 and red_vol > 0 and green_vol > red_vol * 1.3:
            score += get_adaptive_weight("volume_profile_bonus")
            signals.append("Buyers stronger than sellers (green vol > red vol)")

        # ── Market regime modifier for breakouts (brain-adaptive) ──
        try:
            _regime = get_market_regime()
            _vix_regime = _regime.get("vix_regime", "normal")
            if _vix_regime in ("elevated", "extreme"):
                score += get_adaptive_weight("regime_vix_breakout_penalty")
                signals.append(f"High VIX ({_vix_regime}) — false breakout risk elevated")
        except Exception:
            pass

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
        _cr = ticker.upper().endswith("-USD")

        return {
            "ticker": ticker.upper(),
            "score": round(score, 1),
            "signal": status,
            "status": status,
            "price": smart_round(price, crypto=_cr),
            "resistance": smart_round(resistance, crypto=_cr),
            "dist_to_breakout": dist_to_breakout,
            "bb_squeeze": is_squeeze,
            "bb_width_pctile": round(bb_width_pct_rank, 0),
            "adx": round(float(adx_val), 1) if pd.notna(adx_val) else None,
            "vol_trend_pct": vol_trend_pct,
            "tight_days": tight_days,
            "risk_level": "medium" if status == "watch" else "high",
            "signals": signals,
            "entry_price": smart_round(resistance, crypto=_cr),
            "stop_loss": smart_round(resistance - 2 * atr_f, crypto=_cr),
            "take_profit": smart_round(resistance + 3 * atr_f, crypto=_cr),
            "indicators": {
                "rsi": round(float(rsi_val), 1) if pd.notna(rsi_val) else None,
                "macd_hist": round(float(macd_hist), 4) if pd.notna(macd_hist) else None,
                "adx": round(float(adx_val), 1) if pd.notna(adx_val) else None,
                "atr": round(atr_f, 6 if _cr else 4),
                "ema_20": smart_round(float(ema_20), crypto=_cr) if pd.notna(ema_20) else None,
                "ema_50": smart_round(float(ema_50), crypto=_cr) if pd.notna(ema_50) else None,
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
    candidates, total_sourced = get_daytrade_candidates()
    logger.info(f"[trading] Day-trade scan: {len(candidates)}/{total_sourced} candidates")
    _prewarm_cache_intraday(candidates)

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
        "total_sourced": total_sourced,
        "matches": len(results),
        "elapsed_s": elapsed,
        "results": results,
        "brain": _brain_meta(),
    }


def run_breakout_scan(max_results: int = 30) -> dict[str, Any]:
    """Pre-filter with FinViz consolidation signals, then score for breakout readiness."""
    from .prescreener import get_breakout_candidates

    start = time.time()
    candidates, total_sourced = get_breakout_candidates()
    logger.info(f"[trading] Breakout scan: {len(candidates)}/{total_sourced} candidates")

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
        "total_sourced": total_sourced,
        "matches": len(results),
        "elapsed_s": elapsed,
        "results": results,
        "brain": _brain_meta(),
    }


# ── Momentum Scanner (active, finds "immaculate" trades) ──────────────

_momentum_cache: dict[str, Any] = {"results": [], "ts": 0.0}
_MOMENTUM_CACHE_TTL = 120  # 2 minutes

def run_momentum_scanner(max_results: int = 10) -> dict[str, Any]:
    """Active momentum scanner: finds the best intraday setups right now.

    Applies strict "immaculate trade" filters so only A+ setups surface:
    score >= 8.0, MACD positive, relative volume >= 3x, risk:reward >= 2:1.
    Returns the top few setups ranked by score.
    """
    global _momentum_cache
    now = time.time()
    if _momentum_cache["results"] and (now - _momentum_cache["ts"]) < _MOMENTUM_CACHE_TTL:
        return {
            "ok": True,
            "scan_type": "momentum",
            "cached": True,
            "candidates_scanned": _momentum_cache.get("total_sourced"),
            "matches": len(_momentum_cache["results"]),
            "results": _momentum_cache["results"],
            "brain": _brain_meta(),
        }

    from .prescreener import get_daytrade_candidates

    start = time.time()
    candidates, total_sourced = get_daytrade_candidates()
    logger.info(f"[trading] Momentum scanner: {len(candidates)}/{total_sourced} candidates")
    _prewarm_cache_intraday(candidates)

    scored: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=_MAX_SCAN_WORKERS) as executor:
        futures = {executor.submit(_score_ticker_intraday, t): t for t in candidates}
        for future in as_completed(futures):
            if _shutting_down.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                result = future.result()
                if result is not None:
                    scored.append(result)
            except Exception:
                pass

    imm_score = get_adaptive_weight("immaculate_min_score")
    imm_vol = get_adaptive_weight("immaculate_min_vol")
    imm_rr = get_adaptive_weight("immaculate_min_rr")

    immaculate: list[dict[str, Any]] = []
    for r in scored:
        if (
            r["score"] >= imm_score
            and r.get("macd_positive", False)
            and r.get("vol_ratio", 0) >= imm_vol
            and r.get("risk_reward", 0) >= imm_rr
        ):
            r["immaculate"] = True
            immaculate.append(r)

    immaculate.sort(key=lambda x: x["score"], reverse=True)
    immaculate = immaculate[:max_results]

    if not immaculate:
        good = [r for r in scored if r["score"] >= 7.0]
        good.sort(key=lambda x: x["score"], reverse=True)
        results = good[:max_results]
        for r in results:
            r["immaculate"] = False
    else:
        results = immaculate

    _momentum_cache = {"results": results, "ts": time.time(), "total_sourced": total_sourced}
    elapsed = round(time.time() - start, 1)

    return {
        "ok": True,
        "scan_type": "momentum",
        "cached": False,
        "candidates_scanned": len(candidates),
        "total_sourced": total_sourced,
        "immaculate_count": len(immaculate),
        "matches": len(results),
        "elapsed_s": elapsed,
        "results": results,
        "brain": _brain_meta(),
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
    _prewarm_cache(scan_list)
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
        "brain": _brain_meta(),
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


def _prewarm_cache_intraday(tickers: list[str]) -> None:
    """Pre-warm 5d/15m intraday cache for day-trade scoring."""
    BATCH_SIZE = 50
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i + BATCH_SIZE]
        try:
            batch_download(chunk, period="5d", interval="15m")
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


def _bg_refresh_top_picks(user_id: int | None) -> None:
    """Background thread: recompute top picks and update the cache."""
    from ...db import SessionLocal
    global _top_picks_cache
    if not _top_picks_refresh_lock.acquire(blocking=False):
        return
    try:
        s = SessionLocal()
        try:
            picks = _generate_top_picks_impl(s, user_id)
            _top_picks_cache = {"picks": picks, "ts": time.time()}
        finally:
            s.close()
    except Exception:
        logger.debug("Background top-picks refresh failed", exc_info=True)
    finally:
        _top_picks_refresh_lock.release()


def generate_top_picks(db: Session, user_id: int | None) -> list[dict[str, Any]]:
    """Generate proactive AI-driven top picks from scan results + Brain predictions.

    Uses a stale-while-revalidate cache: returns cached data immediately and
    triggers a background refresh when the cache is past its fresh TTL but
    still within the stale window.
    """
    global _top_picks_cache
    now = time.time()
    age = now - _top_picks_cache["ts"]

    if _top_picks_cache["picks"] and age < _TOP_PICKS_TTL:
        return _top_picks_cache["picks"]

    if _top_picks_cache["picks"] and age < _TOP_PICKS_STALE_TTL:
        threading.Thread(
            target=_bg_refresh_top_picks, args=(user_id,), daemon=True,
        ).start()
        return _top_picks_cache["picks"]

    picks = _generate_top_picks_impl(db, user_id)
    _top_picks_cache = {"picks": picks, "ts": time.time()}
    return picks


def _generate_top_picks_impl(db: Session, user_id: int | None) -> list[dict[str, Any]]:
    """Core logic — scan DB + optionally merge Brain predictions."""
    from ...models.trading import ScanResult, BacktestResult
    from sqlalchemy import or_

    recent_cutoff = datetime.utcnow() - timedelta(hours=6)
    user_filter = or_(ScanResult.user_id == user_id, ScanResult.user_id.is_(None))

    scan_rows = db.query(ScanResult).filter(
        user_filter,
        ScanResult.scanned_at >= recent_cutoff,
        ScanResult.score >= 6.0,
        ScanResult.signal == "buy",
    ).order_by(ScanResult.score.desc()).limit(100).all()

    candidates: dict[str, dict] = {}
    for r in scan_rows:
        if r.ticker in candidates:
            continue
        _cr = r.ticker.endswith("-USD")
        candidates[r.ticker] = {
            "ticker": r.ticker,
            "score": r.score,
            "signal": r.signal,
            "price": r.entry_price,
            "entry_price": r.entry_price,
            "stop_loss": r.stop_loss,
            "take_profit": r.take_profit,
            "risk_level": r.risk_level,
            "signals": r.rationale.split("; ") if r.rationale else [],
            "indicators": json.loads(r.indicator_data) if r.indicator_data else {},
            "source": "scan",
            "is_crypto": _cr,
        }

    def _get_brain_predictions():
        from .learning import get_current_predictions
        return get_current_predictions(db, tickers=None)

    try:
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_get_brain_predictions)
        preds = future.result(timeout=30)
        pool.shutdown(wait=False)
        for p in preds:
            t = p["ticker"]
            if p.get("direction") != "bullish" or (p.get("confidence") or 0) < 50:
                continue
            if t in candidates:
                candidates[t]["brain_score"] = p["score"]
                candidates[t]["brain_confidence"] = p["confidence"]
                candidates[t]["brain_direction"] = p["direction"]
                candidates[t]["ml_probability"] = p.get("ml_probability")
                if p.get("suggested_stop"):
                    candidates[t]["brain_stop"] = p["suggested_stop"]
                if p.get("suggested_target"):
                    candidates[t]["brain_target"] = p["suggested_target"]
                if p.get("risk_reward"):
                    candidates[t]["risk_reward"] = p["risk_reward"]
            else:
                _cr = t.endswith("-USD")
                candidates[t] = {
                    "ticker": t,
                    "score": max(6.0, (p["score"] + 10) / 2),
                    "signal": "buy",
                    "price": p.get("price"),
                    "entry_price": p.get("price"),
                    "stop_loss": p.get("suggested_stop"),
                    "take_profit": p.get("suggested_target"),
                    "risk_level": "high" if (p.get("confidence") or 0) < 60 else "medium",
                    "signals": p.get("signals", []),
                    "indicators": {},
                    "source": "brain",
                    "is_crypto": _cr,
                    "brain_score": p["score"],
                    "brain_confidence": p["confidence"],
                    "brain_direction": p["direction"],
                    "ml_probability": p.get("ml_probability"),
                    "risk_reward": p.get("risk_reward"),
                }
    except Exception:
        logger.debug("Brain predictions skipped (timeout or error)")

    picks = list(candidates.values())

    # Bulk-fetch best backtest per ticker (single query instead of N)
    pick_tickers = [p["ticker"] for p in picks]
    bt_rows = db.query(BacktestResult).filter(
        BacktestResult.ticker.in_(pick_tickers),
    ).all() if pick_tickers else []
    bt_map: dict[str, BacktestResult] = {}
    for bt in bt_rows:
        prev = bt_map.get(bt.ticker)
        if prev is None or (bt.return_pct or 0) > (prev.return_pct or 0):
            bt_map[bt.ticker] = bt

    for pick in picks:
        combined = pick["score"]
        if pick.get("brain_confidence"):
            combined = combined * 0.5 + (pick["brain_confidence"] / 10) * 0.5
        pick["combined_score"] = round(combined, 2)

        price = pick.get("price") or 0
        target = pick.get("take_profit") or pick.get("brain_target")
        stop = pick.get("stop_loss") or pick.get("brain_stop")
        if price and target and price > 0:
            pick["projected_profit_pct"] = round((target - price) / price * 100, 2)
            pick["projected_profit_dollar"] = round(target - price, 6)
        if price and stop and target and price > 0:
            risk_amt = abs(price - stop)
            reward_amt = abs(target - price)
            pick["risk_reward"] = round(reward_amt / risk_amt, 2) if risk_amt > 0 else 0
            if risk_amt > 0:
                pick["position_size_pct"] = round(min(5.0, 1.0 / (risk_amt / price * 100)) * 100 / 100, 2)

        best_bt = bt_map.get(pick["ticker"])
        if best_bt:
            pick["best_strategy"] = best_bt.strategy_name
            pick["backtest_return"] = best_bt.return_pct
            pick["backtest_win_rate"] = best_bt.win_rate
        else:
            pick["best_strategy"] = None
            pick["backtest_return"] = None
            pick["backtest_win_rate"] = None

        # Build trade thesis
        thesis_parts = []
        if pick["signal"] == "buy":
            thesis_parts.append("Bullish setup identified by CHILI's AI.")
        for sig in (pick.get("signals") or [])[:3]:
            thesis_parts.append(sig)
        if pick.get("brain_confidence"):
            thesis_parts.append(f"AI Brain confidence: {pick['brain_confidence']:.0f}%")
        if pick.get("risk_reward"):
            thesis_parts.append(f"Risk:Reward ratio {pick['risk_reward']:.1f}:1")
        pick["thesis"] = " ".join(thesis_parts)

        # Timeframe suggestion
        if pick.get("indicators", {}).get("adx") and pick["indicators"]["adx"] > 25:
            pick["timeframe"] = "1-5 days (trending)"
        else:
            pick["timeframe"] = "3-10 days (swing)"

    picks.sort(key=lambda x: x.get("combined_score", 0), reverse=True)

    top = picks[:15]

    for i, pick in enumerate(top):
        pick["rank"] = i + 1

    return top


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
