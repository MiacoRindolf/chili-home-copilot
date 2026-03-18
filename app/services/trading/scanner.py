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
    fetch_quote, fetch_ohlcv_df, fetch_ohlcv_batch, fetch_quotes_batch,
    smart_round, DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS,
    ALL_SCAN_TICKERS, get_market_regime, _use_massive, _use_polygon,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights

logger = logging.getLogger(__name__)

import os as _os

from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, EMAIndicator, ADXIndicator
from ta.volatility import BollingerBands, AverageTrueRange

_shutting_down = threading.Event()
_CPU_COUNT = _os.cpu_count() or 4
_MAX_SCAN_WORKERS = min(64, max(16, _CPU_COUNT * 2))

_top_picks_cache: dict[str, Any] = {"picks": [], "ts": 0.0}
_TOP_PICKS_TTL = 300  # 5 minutes — data doesn't change meaningfully faster
_TOP_PICKS_STALE_TTL = 600  # 10 minutes — serve stale while refreshing in background
_top_picks_refresh_lock = threading.Lock()

# Smart Pick context cache — cache expensive scan/context work, not the LLM reply
_smart_pick_ctx_cache: dict[tuple[int | None, str], dict[str, Any]] = {}
_SMART_PICK_CTX_TTL = 300  # 5 minutes fresh
_SMART_PICK_CTX_STALE_TTL = 600  # 10 minutes stale-while-revalidate
_smart_pick_ctx_lock = threading.Lock()


def signal_shutdown():
    _shutting_down.set()


# ── Adaptive Weight System ────────────────────────────────────────────
# Starting defaults are informed by common momentum strategies, but CHILI's
# brain continuously adjusts them via backtest validation and pattern mining.
# Every learning cycle, evolve_strategy_weights() recalibrates these based
# on what the data actually shows — no assumption is sacred.

_DEFAULT_WEIGHTS: dict[str, float] = {
    # ── Shared / cross-scorer weights ──────────────────────────────────
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

    # ── _score_ticker (swing) ──────────────────────────────────────────
    "swing_ema_stack_full_bull": 1.5,
    "swing_ema_stack_partial_bull": 0.8,
    "swing_ema_stack_full_bear": -1.5,
    "swing_ema_stack_partial_bear": -0.8,
    "swing_rsi_oversold": 1.5,
    "swing_rsi_near_oversold": 0.5,
    "swing_rsi_overbought": -1.5,
    "swing_macd_bull": 1.0,
    "swing_macd_bear": -0.5,
    "swing_sma_uptrend": 0.5,
    "swing_sma_downtrend": -0.5,
    "swing_bb_near_lower": 1.0,
    "swing_bb_near_upper": -0.5,
    "swing_adx_strong": 0.5,
    "swing_vol_surge_accum": 0.5,
    "swing_vol_surge_distrib": -0.5,
    "swing_decline_sharp": -2.0,
    "swing_decline_moderate": -1.0,
    "swing_fund_margins_debt": 0.5,
    "swing_fund_revenue_growth": 0.5,
    "swing_fund_pe_reasonable": 0.5,
    "swing_fund_pe_expensive": -0.5,
    "swing_regime_oversold_contra": 0.3,
    "swing_below_major_mas": -1.0,
    "swing_signal_buy": 7.0,
    "swing_signal_sell": 3.5,
    "swing_stop_atr_mult_vol": 2.5,
    "swing_stop_atr_mult_normal": 2.0,
    "swing_target_atr_mult": 3.0,

    # ── _score_ticker_intraday ─────────────────────────────────────────
    "intra_rsi_momentum_zone": 1.0,
    "intra_rsi_oversold": 0.5,
    "intra_rsi_overextended": -0.5,
    "intra_ema_bull": 1.0,
    "intra_ema_bear": -0.5,
    "intra_vwap_above": 0.8,
    "intra_vwap_below": -0.3,
    "intra_vol_above_avg": 0.5,
    "intra_gap_play": 0.5,
    "intra_signal_long": 7.0,
    "intra_signal_short": 3.5,
    "intra_stop_atr_mult": 1.5,
    "intra_target_atr_mult": 2.5,

    # ── _score_crypto_breakout ─────────────────────────────────────────
    "crypto_bo_rvol_5x": 2.0,
    "crypto_bo_rvol_3x": 1.5,
    "crypto_bo_rvol_2x": 1.0,
    "crypto_bo_rvol_1_5x": 0.5,
    "crypto_bo_rvol_low": -1.5,
    "crypto_bo_squeeze_firing": 2.5,
    "crypto_bo_squeeze": 1.5,
    "crypto_bo_breakout_confirmed": 2.0,
    "crypto_bo_atr_expanding": 0.5,
    "crypto_bo_atr_compressed_squeeze": 1.0,
    "crypto_bo_ema_bull_stack": 2.0,
    "crypto_bo_ema_bull": 0.5,
    "crypto_bo_ema_bear_stack": -0.5,
    "crypto_bo_rsi_momentum": 0.5,
    "crypto_bo_rsi_oversold": 0.3,
    "crypto_bo_rsi_overextended": -0.5,
    "crypto_bo_macd_bull": 1.0,
    "crypto_bo_macd_bear": -0.3,
    "crypto_bo_vwap_above": 0.5,
    "crypto_bo_vwap_below": -0.3,
    "crypto_bo_adx_strong": 0.5,
    "crypto_bo_hot_mover": 0.5,
    "crypto_bo_gaining": 0.3,
    "crypto_bo_vol_awakening": 0.5,
    "crypto_bo_stoch_curl_squeeze": 0.5,
    "crypto_bo_higher_lows": 1.0,
    "crypto_bo_macd_neg_cap": 8.0,
    "crypto_bo_signal_long": 7.0,
    "crypto_bo_signal_short": 3.5,
    "crypto_bo_stop_atr_mult": 2.0,
    "crypto_bo_target_atr_mult": 5.0,

    # ── _score_breakout (stocks) ───────────────────────────────────────
    "bo_squeeze": 2.0,
    "bo_near_resistance_close": 1.5,
    "bo_near_resistance_mid": 0.5,
    "bo_breaking_out": 2.0,
    "bo_adx_consolidating": 1.0,
    "bo_adx_trending": -0.5,
    "bo_vol_declining": 0.8,
    "bo_ema_support": 0.5,
    "bo_rsi_neutral": 0.5,
    "bo_rsi_overbought": -1.0,
    "bo_macd_building": 0.5,
    "bo_signal_ready": 7.0,
    "bo_signal_watch": 5.0,
    "bo_stop_atr_mult": 2.5,
    "bo_target_atr_mult": 5.0,

    # ── Alert / proposal thresholds ────────────────────────────────────
    "alert_min_score_proposal": 7.5,
    "alert_min_rr_proposal": 1.5,
    "alert_min_rr_from_pick": 0.8,
    "alert_min_price": 1.0,
    "alert_breakout_min_score": 7.0,
    "alert_auto_proposal_min_score": 8.0,

    # ── Position sizing ────────────────────────────────────────────────
    "pos_max_risk_pct": 2.0,
    "pos_pct_hard_cap": 10.0,
    "pos_pct_risk_off_cap": 7.0,
    "pos_pct_speculative_cap": 5.0,
    "pos_regime_risk_off_mult": 0.50,
    "pos_regime_cautious_mult": 0.75,
    "pos_vix_elevated_mult": 0.85,
    "pos_vix_extreme_mult": 0.70,
    "pos_vol_stop_10_mult": 0.70,
    "pos_vol_stop_8_mult": 0.80,
    "pos_vol_stop_5_mult": 0.90,
    "pos_speculative_mult": 0.60,
    "pos_scanner_cap_mult": 1.25,

    # ── Crypto breakout alert thresholds ───────────────────────────────
    "crypto_alert_coiled_spring_min": 6.0,
    "crypto_alert_squeeze_firing_min": 5.5,
    "crypto_alert_building_min": 6.5,
    "crypto_alert_range_tight_min": 6.5,
    "crypto_alert_high_score_min": 7.5,
    "crypto_alert_rvol_building_min": 1.0,
    "crypto_alert_rvol_high_score_min": 1.5,
    "crypto_alert_cooldown_s": 3600.0,
    "crypto_alert_max_per_cycle": 5.0,

    # ── Stock breakout alert thresholds ─────────────────────────────────
    "stock_alert_coiled_spring_min": 6.5,
    "stock_alert_squeeze_firing_min": 6.0,
    "stock_alert_high_score_min": 7.5,
    "stock_alert_cooldown_s": 3600.0,
    "stock_alert_max_per_cycle": 5.0,

    # ── New breakout patterns (crypto + stock) ──────────────────────────
    # Volume Contraction Pattern (VCP)
    "crypto_bo_vcp_3plus": 1.5,
    "crypto_bo_vcp_2": 0.8,
    "bo_vcp_3plus": 1.5,
    "bo_vcp_2": 0.8,
    # Narrow Range (NR4/NR7)
    "crypto_bo_nr7": 1.0,
    "crypto_bo_nr4": 0.5,
    "bo_nr7": 1.0,
    "bo_nr4": 0.5,
    # Multi-Timeframe Confirmation
    "crypto_bo_mtf_confirm": 1.0,
    "bo_mtf_confirm": 1.0,
    # VWAP Reclaim
    "crypto_bo_vwap_reclaim": 1.0,
    "bo_vwap_reclaim": 1.0,
    # Opening Range Breakout (stocks only)
    "bo_orb_break_above": 1.5,
    "bo_orb_break_below": -0.5,
    # Accumulation/Distribution
    "crypto_bo_accumulation": 1.0,
    "bo_accumulation": 1.0,
    # Momentum Divergence (penalty — fakeout filter)
    "crypto_bo_rsi_divergence_penalty": -1.0,
    "crypto_bo_macd_divergence_penalty": -0.8,
    "bo_rsi_divergence_penalty": -1.0,
    "bo_macd_divergence_penalty": -0.8,
    # Candlestick patterns at key levels
    "crypto_bo_candle_engulfing": 0.8,
    "crypto_bo_candle_hammer": 0.6,
    "bo_candle_engulfing": 0.8,
    "bo_candle_hammer": 0.6,

    # ── BTC leading indicator (crypto only) ──────────────────────────
    "crypto_bo_btc_trend_aligned": 0.5,
    "crypto_bo_btc_trend_against": -0.5,
    "crypto_bo_btc_dumping_penalty": -1.0,

    # ── Fakeout-specific penalties ───────────────────────────────────
    "crypto_bo_fakeout_rsi_high_penalty": -0.5,
    "bo_fakeout_rsi_high_penalty": -0.5,
    "crypto_bo_fakeout_trending_adx_penalty": -0.3,
    "bo_fakeout_trending_adx_penalty": -0.3,

    # ── News sentiment ───────────────────────────────────────────────
    "crypto_bo_news_bullish_bonus": 0.5,
    "crypto_bo_news_bearish_penalty": -0.3,
    "bo_news_bullish_bonus": 0.5,
    "bo_news_bearish_penalty": -0.3,

    # ── Pattern synergy bonus ────────────────────────────────────────
    "crypto_bo_synergy_bonus": 0.5,
    "bo_synergy_bonus": 0.5,

    # ── Sector diversification ───────────────────────────────────────
    "alert_max_per_sector": 2.0,

    # ── Scheduler / scanner limits ─────────────────────────────────────
    "momentum_max_results": 3.0,

    # ── Momentum breakout / resistance-retest weights ─────────────────
    "bo_rsi_momentum_strong": 1.0,
    "bo_retest_pressure": 2.0,
    "bo_retest_consolidation": 1.5,
    "bo_mtf_retest_confirm": 1.0,
    "crypto_bo_rsi_momentum_strong": 1.0,
    "crypto_bo_retest_pressure": 2.0,
    "crypto_bo_retest_consolidation": 1.5,
    "crypto_bo_mtf_retest_confirm": 1.0,
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
        # ── Swing scorer ──
        "swing_ema_stack_full_bull": ["ema stacking bullish", "full ema stack", "ema stack bullish"],
        "swing_ema_stack_partial_bull": ["partial ema alignment", "ema20>ema50"],
        "swing_ema_stack_full_bear": ["ema stacking bearish", "full bearish stack"],
        "swing_ema_stack_partial_bear": ["bearish ema alignment", "bearish ema"],
        "swing_rsi_oversold": ["rsi oversold", "rsi < 30", "deeply oversold"],
        "swing_rsi_near_oversold": ["rsi near oversold", "rsi approaching oversold"],
        "swing_rsi_overbought": ["rsi overbought", "rsi > 70", "overextended rsi"],
        "swing_macd_bull": ["macd bullish crossover", "macd bullish"],
        "swing_macd_bear": ["macd bearish", "macd negative"],
        "swing_sma_uptrend": ["uptrend", "sma20 > sma50", "price above sma"],
        "swing_sma_downtrend": ["downtrend", "sma decline", "below sma"],
        "swing_bb_near_lower": ["near lower bollinger", "bollinger low", "bb low"],
        "swing_bb_near_upper": ["near upper bollinger", "bb high"],
        "swing_adx_strong": ["strong trend", "adx strong", "adx > 25"],
        "swing_vol_surge_accum": ["volume surge accumulation", "accumulation volume"],
        "swing_vol_surge_distrib": ["volume surge distribution", "distribution volume"],
        "swing_decline_sharp": ["sharp decline", "falling knife", "crash"],
        "swing_decline_moderate": ["falling", "moderate decline", "5-day decline"],
        "swing_fund_margins_debt": ["strong margins", "low debt", "fundamentals strong"],
        "swing_fund_revenue_growth": ["revenue growth", "growing revenue"],
        "swing_fund_pe_reasonable": ["reasonable p/e", "fair valuation"],
        "swing_fund_pe_expensive": ["expensive p/e", "overvalued", "high pe"],
        "swing_below_major_mas": ["below sma50", "below ema50", "falling-knife"],
        # ── Intraday scorer ──
        "intra_rsi_momentum_zone": ["rsi momentum zone", "rsi 40-65"],
        "intra_rsi_oversold": ["rsi deeply oversold", "intraday oversold"],
        "intra_rsi_overextended": ["rsi overextended", "intraday overbought"],
        "intra_ema_bull": ["ema9 > ema21", "bullish intraday ema"],
        "intra_ema_bear": ["bearish ema alignment", "intraday bearish ema"],
        "intra_vwap_above": ["above vwap", "vwap support"],
        "intra_vwap_below": ["below vwap", "vwap resistance"],
        "intra_vol_above_avg": ["above-average volume", "volume 1.5x"],
        "intra_gap_play": ["gap up", "gap down", "gap play"],
        # ── Crypto breakout scorer ──
        "crypto_bo_rvol_5x": ["volume explosion", "5x volume", "rvol 5x"],
        "crypto_bo_rvol_3x": ["massive volume surge", "3x volume", "rvol 3x"],
        "crypto_bo_rvol_2x": ["strong volume", "2x volume", "rvol 2x"],
        "crypto_bo_rvol_1_5x": ["above-avg volume crypto", "1.5x volume"],
        "crypto_bo_rvol_low": ["low volume fakeout", "thin volume"],
        "crypto_bo_squeeze_firing": ["squeeze firing", "bollinger squeeze firing"],
        "crypto_bo_squeeze": ["bollinger squeeze", "bb squeeze consolidation"],
        "crypto_bo_breakout_confirmed": ["confirmed breakout", "breakout on volume"],
        "crypto_bo_atr_expanding": ["atr expanding", "volatility increasing"],
        "crypto_bo_atr_compressed_squeeze": ["coiled spring", "atr compressed + squeeze"],
        "crypto_bo_ema_bull_stack": ["full bullish ema stack crypto", "crypto ema bullish"],
        "crypto_bo_ema_bull": ["ema 9 > ema 21 crypto", "short-term bullish crypto"],
        "crypto_bo_ema_bear_stack": ["bearish ema stack crypto"],
        "crypto_bo_rsi_momentum": ["rsi momentum zone crypto"],
        "crypto_bo_rsi_oversold": ["rsi deeply oversold crypto", "bounce candidate"],
        "crypto_bo_rsi_overextended": ["rsi overextended crypto", "crypto overbought"],
        "crypto_bo_macd_bull": ["macd bullish crossover crypto"],
        "crypto_bo_macd_bear": ["macd bearish crypto", "crypto momentum lost"],
        "crypto_bo_vwap_above": ["above vwap crypto"],
        "crypto_bo_vwap_below": ["below vwap crypto"],
        "crypto_bo_adx_strong": ["strong trend crypto", "crypto adx strong"],
        "crypto_bo_hot_mover": ["hot mover", "top crypto gainer"],
        "crypto_bo_gaining": ["gaining crypto", "crypto up 5%"],
        "crypto_bo_vol_awakening": ["volume awakening", "volume waking up in squeeze", "rvol picking up"],
        "crypto_bo_stoch_curl_squeeze": ["stochastic curl", "stoch crossover in squeeze", "momentum building in squeeze"],
        "crypto_bo_higher_lows": ["higher lows", "ascending triangle", "pressure building", "ascending lows into resistance"],
        # ── Stock breakout scorer ──
        "bo_squeeze": ["bollinger squeeze breakout", "bb squeeze stocks"],
        "bo_near_resistance_close": ["near resistance", "close to breakout"],
        "bo_near_resistance_mid": ["below resistance", "approaching resistance"],
        "bo_breaking_out": ["breaking out", "new high", "20-day high"],
        "bo_adx_consolidating": ["low adx", "adx consolidating", "adx < 20"],
        "bo_adx_trending": ["adx trending", "adx > 30", "already trending"],
        "bo_vol_declining": ["volume declining", "volume coiling"],
        "bo_ema_support": ["above rising emas", "ema support", "bullish base"],
        "bo_rsi_neutral": ["rsi neutral", "rsi room to run"],
        "bo_rsi_overbought": ["rsi overbought breakout", "rsi may fade"],
        "bo_macd_building": ["macd building", "histogram positive building"],
        # ── New breakout patterns ──
        "crypto_bo_vcp_3plus": ["volume contraction pattern", "vcp 3+", "successive contractions crypto"],
        "crypto_bo_vcp_2": ["vcp 2 contractions", "tightening pullbacks crypto"],
        "bo_vcp_3plus": ["volume contraction pattern stock", "vcp 3+ stocks", "minervini vcp"],
        "bo_vcp_2": ["vcp 2 contractions stock", "tightening pullbacks stock"],
        "crypto_bo_nr7": ["narrow range 7 crypto", "nr7 crypto", "tightest range 7 bars"],
        "crypto_bo_nr4": ["narrow range 4 crypto", "nr4 crypto"],
        "bo_nr7": ["narrow range 7", "nr7 stocks", "narrowest range 7 bars"],
        "bo_nr4": ["narrow range 4", "nr4 stocks"],
        "crypto_bo_mtf_confirm": ["multi timeframe crypto", "1h trend confirms 15m", "higher tf alignment"],
        "bo_mtf_confirm": ["multi timeframe stock", "daily trend confirms intraday", "higher tf bullish"],
        "crypto_bo_vwap_reclaim": ["vwap reclaim crypto", "reclaiming vwap", "vwap flip bullish"],
        "bo_vwap_reclaim": ["vwap reclaim stock", "reclaiming vwap intraday"],
        "bo_orb_break_above": ["opening range breakout", "orb above", "first 30min high"],
        "bo_orb_break_below": ["orb breakdown", "below opening range"],
        "crypto_bo_accumulation": ["accumulation crypto", "obv rising price flat", "institutional buying crypto"],
        "bo_accumulation": ["accumulation stock", "obv divergence bullish", "institutional buying"],
        "crypto_bo_rsi_divergence_penalty": ["rsi bearish divergence crypto", "rsi divergence crypto"],
        "crypto_bo_macd_divergence_penalty": ["macd divergence crypto", "macd bearish divergence crypto"],
        "bo_rsi_divergence_penalty": ["rsi bearish divergence stock", "rsi divergence fakeout"],
        "bo_macd_divergence_penalty": ["macd divergence stock", "macd bearish divergence stock"],
        "crypto_bo_candle_engulfing": ["bullish engulfing crypto", "engulfing candle crypto"],
        "crypto_bo_candle_hammer": ["hammer candle crypto", "hammer at support crypto"],
        "bo_candle_engulfing": ["bullish engulfing stock", "engulfing candle stocks"],
        "bo_candle_hammer": ["hammer candle stock", "hammer at support stock"],
        "stock_alert_coiled_spring_min": ["stock coiled spring", "stock squeeze + atr compressed"],
        "stock_alert_squeeze_firing_min": ["stock squeeze firing", "stock squeeze releasing"],
        "stock_alert_high_score_min": ["stock high score setup", "strong stock breakout"],
        # ── Alert thresholds ──
        "alert_min_score_proposal": ["proposal score", "proposal threshold"],
        "alert_auto_proposal_min_score": ["auto proposal", "high-confidence pick"],
        "alert_breakout_min_score": ["breakout alert score", "breakout threshold"],
        # ── Position sizing ──
        "pos_pct_hard_cap": ["position cap", "hard cap"],
        "pos_regime_risk_off_mult": ["risk-off position", "risk off sizing"],
        "pos_speculative_mult": ["speculative sizing", "speculative position"],
        # ── Crypto alert thresholds ──
        "crypto_alert_coiled_spring_min": ["coiled spring", "squeeze + atr compressed", "double compression"],
        "crypto_alert_squeeze_firing_min": ["squeeze firing", "squeeze releasing", "bb squeeze fire"],
        "crypto_alert_building_min": ["breakout building", "squeeze + ema bullish", "volume picking up"],
        "crypto_alert_range_tight_min": ["range tightening", "atr compressed", "atr compression"],
        "crypto_alert_high_score_min": ["high score setup", "crypto high score", "strong crypto"],
        # ── BTC leading indicator ──
        "crypto_bo_btc_trend_aligned": ["btc trend aligned", "btc supporting altcoin breakout"],
        "crypto_bo_btc_trend_against": ["btc trend against", "altcoin vs btc", "btc headwind"],
        "crypto_bo_btc_dumping_penalty": ["btc dumping", "btc crash", "btc down hard"],
        # ── Fakeout penalties ──
        "crypto_bo_fakeout_rsi_high_penalty": ["rsi high squeeze fakeout", "overbought squeeze crypto"],
        "bo_fakeout_rsi_high_penalty": ["rsi high squeeze fakeout stock", "overbought squeeze stock"],
        "crypto_bo_fakeout_trending_adx_penalty": ["trending squeeze fakeout crypto", "adx high squeeze crypto"],
        "bo_fakeout_trending_adx_penalty": ["trending squeeze fakeout stock", "adx high squeeze stock"],
        # ── News sentiment ──
        "crypto_bo_news_bullish_bonus": ["bullish news crypto breakout", "positive news catalyst crypto"],
        "crypto_bo_news_bearish_penalty": ["bearish news crypto", "negative news crypto"],
        "bo_news_bullish_bonus": ["bullish news stock breakout", "positive news catalyst stock"],
        "bo_news_bearish_penalty": ["bearish news stock", "negative news stock"],
        # ── Pattern synergy ──
        "crypto_bo_synergy_bonus": ["signal synergy crypto", "pattern combo crypto", "winning combination crypto"],
        "bo_synergy_bonus": ["signal synergy stock", "pattern combo stock", "winning combination stock"],
        # ── Sector diversification ──
        "alert_max_per_sector": ["sector concentration", "sector limit", "diversification cap"],
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
_SCORE_CACHE_TTL = 300  # 5 min (64 GB RAM)
_SCORE_CACHE_MAX = 1000


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
        from ta.trend import SMAIndicator

        df = fetch_ohlcv_df(ticker, period="6mo", interval="1d")
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
                score += get_adaptive_weight("swing_ema_stack_full_bull")
                signals.append(f"EMA stacking bullish (P>{e20:.0f}>{e50:.0f}>{e100:.0f})")
            elif price > e20 > e50:
                score += get_adaptive_weight("swing_ema_stack_partial_bull")
                signals.append("Partial EMA alignment (P>EMA20>EMA50)")
            elif e100 is not None and price < e20 < e50 < e100:
                ema_stack_bearish = True
                score += get_adaptive_weight("swing_ema_stack_full_bear")
                signals.append("EMA stacking bearish")
            elif price < e20 < e50:
                score += get_adaptive_weight("swing_ema_stack_partial_bear")
                signals.append("Bearish EMA alignment")

        if pd.notna(rsi_val):
            if rsi_val < 30:
                score += get_adaptive_weight("swing_rsi_oversold")
                signals.append(f"RSI oversold ({rsi_val:.0f})")
            elif rsi_val < 40:
                score += get_adaptive_weight("swing_rsi_near_oversold")
                signals.append(f"RSI near oversold ({rsi_val:.0f})")
            elif rsi_val > 70:
                score += get_adaptive_weight("swing_rsi_overbought")
                signals.append(f"RSI overbought ({rsi_val:.0f})")

        if pd.notna(macd_val) and pd.notna(macd_sig):
            if macd_val > macd_sig:
                score += get_adaptive_weight("swing_macd_bull")
                signals.append("MACD bullish crossover")
            else:
                score += get_adaptive_weight("swing_macd_bear")

        if pd.notna(sma_20) and pd.notna(sma_50):
            if price > sma_20 > sma_50:
                score += get_adaptive_weight("swing_sma_uptrend")
                signals.append("Uptrend (price > SMA20 > SMA50)")
            elif price < sma_20 < sma_50:
                score += get_adaptive_weight("swing_sma_downtrend")
                signals.append("Downtrend")

        if pd.notna(bb_lower) and pd.notna(bb_upper):
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                bb_pct = (price - bb_lower) / bb_range
                if bb_pct < 0.15:
                    score += get_adaptive_weight("swing_bb_near_lower")
                    signals.append("Near lower Bollinger Band")
                elif bb_pct > 0.85:
                    score += get_adaptive_weight("swing_bb_near_upper")

        if pd.notna(adx_val) and adx_val > 25:
            score += get_adaptive_weight("swing_adx_strong")
            signals.append(f"Strong trend (ADX {adx_val:.0f})")

        if vol_avg > 0 and vol_latest > vol_avg * 1.5:
            latest_close = float(df["Close"].iloc[-1])
            latest_open = float(df["Open"].iloc[-1])
            if latest_close >= latest_open:
                score += get_adaptive_weight("swing_vol_surge_accum")
                signals.append("Volume surge (accumulation)")
            else:
                score += get_adaptive_weight("swing_vol_surge_distrib")
                signals.append("Volume surge (distribution)")

        # ── Recent price trend — penalise falling knives ──
        if len(df) >= 6:
            _ret_5d = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-6]) - 1) * 100
            if _ret_5d < -15:
                score += get_adaptive_weight("swing_decline_sharp")
                signals.append(f"Sharp decline ({_ret_5d:.1f}% in 5 days)")
            elif _ret_5d < -8:
                score += get_adaptive_weight("swing_decline_moderate")
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
                            fund_bonus += get_adaptive_weight("swing_fund_margins_debt")
                            signals.append("Strong margins + low debt")
                    if fund.get("revenue_growth") is not None and fund["revenue_growth"] > 0:
                        fund_bonus += get_adaptive_weight("swing_fund_revenue_growth")
                        signals.append(f"Revenue growth +{fund['revenue_growth']:.0%}")
                    if fund.get("pe_trailing") is not None:
                        pe = fund["pe_trailing"]
                        if 5 < pe < 25:
                            fund_bonus += get_adaptive_weight("swing_fund_pe_reasonable")
                            signals.append(f"Reasonable P/E ({pe:.1f})")
                        elif pe > 60:
                            fund_bonus += get_adaptive_weight("swing_fund_pe_expensive")
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
                    score += get_adaptive_weight("swing_regime_oversold_contra")
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
            score += get_adaptive_weight("swing_below_major_mas")
            signals.append("Below SMA50 & EMA50 — falling-knife risk")

        score = max(1.0, min(10.0, score))

        _buy_thresh = get_adaptive_weight("swing_signal_buy")
        _sell_thresh = get_adaptive_weight("swing_signal_sell")
        if score >= _buy_thresh:
            signal = "buy"
        elif score <= _sell_thresh:
            signal = "sell"
        else:
            signal = "hold"

        atr_f = float(atr_val) if pd.notna(atr_val) else price * 0.02
        _cr = is_crypto_ticker

        volatility_pct = (atr_f / price * 100) if price > 0 else 5
        _stop_mult = get_adaptive_weight("swing_stop_atr_mult_vol") if volatility_pct > 3 else get_adaptive_weight("swing_stop_atr_mult_normal")
        stop_loss = smart_round(price - _stop_mult * atr_f, crypto=_cr)
        take_profit = smart_round(price + get_adaptive_weight("swing_target_atr_mult") * atr_f, crypto=_cr)
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
        df = fetch_ohlcv_df(ticker, period="5d", interval="15m")
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
                score += get_adaptive_weight("intra_rsi_momentum_zone")
                signals.append(f"RSI in momentum zone ({rsi_val:.0f})")
            elif rsi_val < 25:
                score += get_adaptive_weight("intra_rsi_oversold")
                signals.append(f"RSI deeply oversold ({rsi_val:.0f}) — potential bounce")
            elif rsi_val > 75:
                score += get_adaptive_weight("intra_rsi_overextended")
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
                score += get_adaptive_weight("intra_ema_bull")
                signals.append("Price > EMA9 > EMA21 (bullish intraday)")
            elif price < float(ema_9) < float(ema_21):
                score += get_adaptive_weight("intra_ema_bear")
                signals.append("Bearish EMA alignment")

        # VWAP positioning
        if vwap is not None:
            if price > vwap:
                score += get_adaptive_weight("intra_vwap_above")
                signals.append(f"Above VWAP ({vwap_pct:+.1f}%)")
            else:
                score += get_adaptive_weight("intra_vwap_below")
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
            score += get_adaptive_weight("intra_vol_above_avg")
            signals.append(f"Above-average volume ({vol_ratio:.1f}x)")

        # Gap play
        if abs(gap_pct) > 2:
            score += get_adaptive_weight("intra_gap_play")
            signals.append(f"Gap {'up' if gap_pct > 0 else 'down'} {gap_pct:+.1f}%")

        # ── Daily gainer check (brain-adaptive) ──
        if daily_change_pct >= 10.0:
            score += get_adaptive_weight("daily_gainer_10pct")
            signals.append(f"Top gainer today (+{daily_change_pct:.1f}%)")
        elif daily_change_pct >= 5.0:
            score += get_adaptive_weight("daily_gainer_5pct")
            signals.append(f"Strong gainer today (+{daily_change_pct:.1f}%)")

        # ── Float factor (only uses cached fundamentals — never blocks on API call) ──
        _cr = ticker.upper().endswith("-USD")
        if not _cr:
            try:
                from ..yf_session import _cache_get as _yf_cache_get
                _fund_cached = _yf_cache_get(f"fund:{ticker}")
                if _fund_cached and _fund_cached != "EMPTY" and isinstance(_fund_cached, dict) and _fund_cached.get("market_cap") and price > 0:
                    shares = _fund_cached["market_cap"] / price
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

        _long_thresh = get_adaptive_weight("intra_signal_long")
        _short_thresh = get_adaptive_weight("intra_signal_short")
        if score >= _long_thresh:
            signal = "long"
        elif score <= _short_thresh:
            signal = "short"
        else:
            signal = "wait"

        atr_f = float(atr_val) if pd.notna(atr_val) else price * 0.01
        _i_stop_m = get_adaptive_weight("intra_stop_atr_mult")
        _i_tgt_m = get_adaptive_weight("intra_target_atr_mult")
        scalp_stop = smart_round(price - _i_stop_m * atr_f, crypto=_cr)
        scalp_target = smart_round(price + _i_tgt_m * atr_f, crypto=_cr)
        risk_reward = round(_i_tgt_m * atr_f / (_i_stop_m * atr_f), 2) if atr_f > 0 else 1.67

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


# ── Crypto Intraday Breakout Scoring ──────────────────────────────────

_crypto_breakout_cache: dict[str, Any] = {"results": [], "ts": 0.0}
_CRYPTO_BREAKOUT_TTL = 900  # 15 minutes
_crypto_scan_running = False


# ── Vectorised indicator computation ──────────────────────────────────

def _compute_indicators(df: pd.DataFrame, *, crypto: bool = False) -> dict[str, Any] | None:
    """Compute all indicators needed for breakout scoring in a single pass.

    Returns a flat dict of scalar values or None if the DataFrame is too short.
    """
    if df.empty or len(df) < 60:
        return None

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]
    price = float(close.iloc[-1])
    if price <= 0:
        return None

    rsi = RSIIndicator(close=close, window=14).rsi()
    macd_obj = MACD(close=close)
    bb = BollingerBands(close=close, window=20, window_dev=2)
    atr_series = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()
    adx_series = ADXIndicator(high=high, low=low, close=close, window=14).adx()

    result: dict[str, Any] = {
        "df": df,
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "price": price,
        "rsi": float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None,
        "macd_line": float(macd_obj.macd().iloc[-1]) if pd.notna(macd_obj.macd().iloc[-1]) else None,
        "macd_signal": float(macd_obj.macd_signal().iloc[-1]) if pd.notna(macd_obj.macd_signal().iloc[-1]) else None,
        "macd_hist": float(macd_obj.macd_diff().iloc[-1]) if pd.notna(macd_obj.macd_diff().iloc[-1]) else None,
        "adx": float(adx_series.iloc[-1]) if pd.notna(adx_series.iloc[-1]) else None,
        "atr": float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else price * 0.01,
        "atr_series": atr_series,
        "bb_upper_series": bb.bollinger_hband(),
        "bb_lower_series": bb.bollinger_lband(),
        "bb_width_series": bb.bollinger_wband(),
        "bb_upper": float(bb.bollinger_hband().iloc[-1]) if pd.notna(bb.bollinger_hband().iloc[-1]) else None,
        "bb_lower": float(bb.bollinger_lband().iloc[-1]) if pd.notna(bb.bollinger_lband().iloc[-1]) else None,
        "bb_mid": float(bb.bollinger_mavg().iloc[-1]) if pd.notna(bb.bollinger_mavg().iloc[-1]) else None,
    }

    ema20 = EMAIndicator(close=close, window=20).ema_indicator()
    ema50 = EMAIndicator(close=close, window=50).ema_indicator()
    ema100 = EMAIndicator(close=close, window=100).ema_indicator()
    result["ema_20"] = float(ema20.iloc[-1]) if pd.notna(ema20.iloc[-1]) else None
    result["ema_50"] = float(ema50.iloc[-1]) if pd.notna(ema50.iloc[-1]) else None
    result["ema_100"] = float(ema100.iloc[-1]) if pd.notna(ema100.iloc[-1]) else None

    if crypto:
        ema9 = EMAIndicator(close=close, window=9).ema_indicator()
        ema21 = EMAIndicator(close=close, window=21).ema_indicator()
        result["ema_9"] = float(ema9.iloc[-1]) if pd.notna(ema9.iloc[-1]) else None
        result["ema_21"] = float(ema21.iloc[-1]) if pd.notna(ema21.iloc[-1]) else None
        stoch = StochasticOscillator(high=high, low=low, close=close)
        stoch_k = stoch.stoch()
        stoch_d = stoch.stoch_signal()
        result["stoch_k"] = float(stoch_k.iloc[-1]) if pd.notna(stoch_k.iloc[-1]) else None
        result["stoch_d"] = float(stoch_d.iloc[-1]) if pd.notna(stoch_d.iloc[-1]) else None
        result["stoch_k_prev"] = float(stoch_k.iloc[-2]) if len(stoch_k) >= 2 and pd.notna(stoch_k.iloc[-2]) else None
        result["stoch_d_prev"] = float(stoch_d.iloc[-2]) if len(stoch_d) >= 2 and pd.notna(stoch_d.iloc[-2]) else None

    bb_width_val = result["bb_width_series"].iloc[-1]
    result["bb_width"] = float(bb_width_val) if pd.notna(bb_width_val) else 0

    bb_width_clean = result["bb_width_series"].dropna()
    result["bb_squeeze"] = False
    result["bb_squeeze_firing"] = False
    if len(bb_width_clean) >= 50:
        bw50 = bb_width_clean.iloc[-50:]
        pct_20 = float(bw50.quantile(0.20))
        prev_w = float(bb_width_clean.iloc[-2]) if len(bb_width_clean) >= 2 else result["bb_width"]
        if result["bb_width"] <= pct_20:
            result["bb_squeeze"] = True
        if prev_w <= pct_20 and result["bb_width"] > pct_20:
            result["bb_squeeze_firing"] = True
    elif len(bb_width_clean) >= 20:
        pct_rank = float((bb_width_clean < result["bb_width"]).sum() / len(bb_width_clean) * 100)
        result["bb_width_pct_rank"] = pct_rank
        if pct_rank < 25:
            result["bb_squeeze"] = True

    vol_avg_20 = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else float(volume.mean())
    vol_latest = float(volume.iloc[-1])
    result["vol_avg_20"] = vol_avg_20
    result["vol_latest"] = vol_latest
    result["rel_vol"] = round(vol_latest / vol_avg_20, 2) if vol_avg_20 > 0 else 1.0

    atr_clean = atr_series.dropna()
    result["atr_state"] = "normal"
    if len(atr_clean) >= 50:
        atr50 = atr_clean.iloc[-50:]
        if result["atr"] <= float(atr50.quantile(0.25)):
            result["atr_state"] = "compressed"
        elif result["atr"] >= float(atr50.quantile(0.75)):
            result["atr_state"] = "expanding"

    return result


# ── Shared breakout pattern helpers ──────────────────────────────────

_sector_cache: dict[str, tuple[str, float]] = {}
_SECTOR_CACHE_TTL = 7200  # 2 hours — sectors rarely change


_CRYPTO_CATEGORIES: dict[str, str] = {
    "BTC": "crypto_store_of_value", "ETH": "crypto_smart_contract",
    "SOL": "crypto_smart_contract", "ADA": "crypto_smart_contract",
    "AVAX": "crypto_smart_contract", "DOT": "crypto_smart_contract",
    "NEAR": "crypto_smart_contract", "ATOM": "crypto_smart_contract",
    "LINK": "crypto_oracle", "AAVE": "crypto_defi", "UNI": "crypto_defi",
    "MKR": "crypto_defi", "CRV": "crypto_defi", "SUSHI": "crypto_defi",
    "DOGE": "crypto_meme", "SHIB": "crypto_meme", "PEPE": "crypto_meme",
    "BONK": "crypto_meme", "FLOKI": "crypto_meme", "WIF": "crypto_meme",
    "XRP": "crypto_payments", "LTC": "crypto_payments", "XLM": "crypto_payments",
    "MATIC": "crypto_l2", "ARB": "crypto_l2", "OP": "crypto_l2",
    "FIL": "crypto_infra", "RENDER": "crypto_infra", "AR": "crypto_infra",
    "BNB": "crypto_exchange", "FTT": "crypto_exchange", "CRO": "crypto_exchange",
}


def _get_sector_for_ticker(ticker: str) -> str:
    """Lookup sector for a ticker (cached 1h). Returns sub-category for crypto."""
    import time as _t

    if ticker.upper().endswith("-USD"):
        base = ticker.upper().replace("-USD", "")
        return _CRYPTO_CATEGORIES.get(base, "crypto_other")

    now = _t.time()
    cached = _sector_cache.get(ticker)
    if cached and now - cached[1] < _SECTOR_CACHE_TTL:
        return cached[0]

    try:
        from ..yf_session import _cache_get as _yf_cache_get
        _fund_cached = _yf_cache_get(f"fund:{ticker}")
        if _fund_cached and _fund_cached != "EMPTY" and isinstance(_fund_cached, dict):
            sector = _fund_cached.get("sector", "unknown")
        else:
            sector = "unknown"
    except Exception:
        sector = "unknown"

    _sector_cache[ticker] = (sector, now)
    return sector


_TIME_PER_BAR: dict[str, float] = {
    "1m": 1 / 60, "5m": 5 / 60, "15m": 0.25, "30m": 0.5,
    "1h": 1.0, "4h": 4.0, "1d": 6.5, "1wk": 32.5,
}


def _estimate_hold_duration(
    entry: float, target: float, atr: float,
    timeframe: str = "1d", adx: float | None = None,
) -> dict[str, Any]:
    """Estimate how long a position should be held to reach the target.

    Uses ATR-based distance, chart timeframe, and ADX trend strength
    to produce a human-readable hold duration range.
    """
    if atr <= 0 or entry <= 0 or target <= entry:
        return {"hours_low": 0, "hours_high": 0, "label": "n/a"}

    atr_multiples = (target - entry) / atr
    hours_per_bar = _TIME_PER_BAR.get(timeframe, 6.5)
    raw_hours = atr_multiples * hours_per_bar

    if adx is not None and adx > 30:
        adx_factor = 0.7
    elif adx is not None and adx > 20:
        adx_factor = 0.85
    else:
        adx_factor = 1.2

    center = raw_hours * adx_factor
    hours_low = max(0.25, center * 0.6)
    hours_high = center * 1.5

    if hours_high < 1:
        label = f"~{max(1, int(hours_low * 60))}-{int(hours_high * 60)} min"
    elif hours_high < 24:
        label = f"~{max(1, round(hours_low))}-{round(hours_high)} hours"
    else:
        days_low = max(1, round(hours_low / 24))
        days_high = max(days_low, round(hours_high / 24))
        if days_low == days_high:
            label = f"~{days_low} day{'s' if days_low > 1 else ''}"
        else:
            label = f"~{days_low}-{days_high} days"

    return {
        "hours_low": round(hours_low, 2),
        "hours_high": round(hours_high, 2),
        "label": label,
    }


def classify_trade_type(
    signals: list[str],
    hold_estimate: dict[str, Any] | None = None,
    indicators: dict[str, Any] | None = None,
    is_crypto: bool = False,
) -> dict[str, str]:
    """Classify a trade into a type with a human-readable label.

    Returns {"type": "swing", "label": "Swing Trade", "duration": "~3-5 days"}
    """
    indicators = indicators or {}
    signals_text = " ".join(s.lower() for s in (signals or []))

    hours_high = (hold_estimate or {}).get("hours_high", 0)
    hours_low = (hold_estimate or {}).get("hours_low", 0)
    duration_label = (hold_estimate or {}).get("label", "")

    adx = indicators.get("adx")

    if "breakout" in signals_text or "gap_up" in signals_text or "vcp" in signals_text:
        trade_type = "breakout"
        label = "Breakout"
    elif "reversal" in signals_text or "divergence" in signals_text or "rsi_oversold" in signals_text:
        trade_type = "reversal"
        label = "Reversal"
    elif "momentum" in signals_text or "squeeze" in signals_text:
        trade_type = "momentum"
        label = "Momentum"
    elif hours_high > 0 and hours_high < 4:
        trade_type = "scalp"
        label = "Scalp"
    elif hours_high > 0 and hours_high < 8:
        trade_type = "daytrade"
        label = "Day Trade"
    elif hours_high > 0 and hours_high <= 5 * 24:
        trade_type = "swing"
        label = "Swing Trade"
    elif hours_high > 5 * 24:
        trade_type = "position"
        label = "Position Trade"
    elif adx and adx > 30:
        trade_type = "trend_follow"
        label = "Trend Follow"
    else:
        trade_type = "swing"
        label = "Swing Trade"

    if not duration_label:
        if hours_high > 0:
            if hours_high < 1:
                duration_label = f"~{max(1, int(hours_low * 60))}-{int(hours_high * 60)} min"
            elif hours_high < 24:
                duration_label = f"~{max(1, round(hours_low))}-{round(hours_high)} hours"
            else:
                days_low = max(1, round(hours_low / 24))
                days_high = max(days_low, round(hours_high / 24))
                duration_label = f"~{days_low}-{days_high} days"

    return {
        "type": trade_type,
        "label": label,
        "duration": duration_label,
    }


def _detect_vcp(high: pd.Series, low: pd.Series, volume: pd.Series,
                lookback: int = 40) -> int:
    """Detect Volume Contraction Pattern (Minervini).

    Returns the number of successive contractions found (0, 1, 2, 3+).
    Each contraction must have a tighter range AND lower volume than the
    previous one.
    """
    if len(high) < lookback:
        return 0
    h = high.iloc[-lookback:]
    l = low.iloc[-lookback:]
    v = volume.iloc[-lookback:]

    swing_ranges: list[tuple[float, float]] = []
    window = lookback // 4
    if window < 3:
        window = 3
    for start in range(0, lookback - window + 1, window):
        end = min(start + window, lookback)
        seg_range = float(h.iloc[start:end].max() - l.iloc[start:end].min())
        seg_vol = float(v.iloc[start:end].mean())
        if pd.isna(seg_range) or pd.isna(seg_vol):
            return 0
        swing_ranges.append((seg_range, seg_vol))

    contractions = 0
    for i in range(1, len(swing_ranges)):
        r_prev, v_prev = swing_ranges[i - 1]
        r_curr, v_curr = swing_ranges[i]
        if r_prev > 0 and r_curr < r_prev * 0.85 and v_curr < v_prev:
            contractions += 1
        else:
            contractions = 0
    return contractions


def _detect_narrow_range(high: pd.Series, low: pd.Series) -> str | None:
    """Return 'NR7', 'NR4', or None based on current bar range vs recent."""
    if len(high) < 7:
        return None
    current_range = float(high.iloc[-1]) - float(low.iloc[-1])
    if pd.isna(current_range) or current_range <= 0:
        return None
    ranges_7 = [float(high.iloc[-i]) - float(low.iloc[-i]) for i in range(1, 8)]
    if any(pd.isna(r) for r in ranges_7):
        return None
    if current_range <= min(ranges_7[1:]):
        return "NR7"
    ranges_4 = ranges_7[:4]
    if current_range <= min(ranges_4[1:]):
        return "NR4"
    return None


def _detect_accumulation(close: pd.Series, volume: pd.Series,
                         bb_squeeze: bool, lookback: int = 20) -> bool:
    """Detect OBV rising while price is flat (accumulation before breakout)."""
    if len(close) < lookback or not bb_squeeze:
        return False
    seg_c = close.iloc[-lookback:]
    seg_v = volume.iloc[-lookback:]
    denom = float(seg_c.iloc[0])
    if denom == 0 or pd.isna(denom):
        return False
    price_change = (float(seg_c.iloc[-1]) - denom) / denom * 100
    if pd.isna(price_change) or abs(price_change) > 3.0:
        return False
    obv = 0.0
    obv_start = 0.0
    for i in range(1, len(seg_c)):
        if float(seg_c.iloc[i]) > float(seg_c.iloc[i - 1]):
            obv += float(seg_v.iloc[i])
        elif float(seg_c.iloc[i]) < float(seg_c.iloc[i - 1]):
            obv -= float(seg_v.iloc[i])
        if i == 1:
            obv_start = obv
    return obv > obv_start * 1.3 if obv_start > 0 else obv > 0


def _detect_divergence(close: pd.Series, indicator: pd.Series,
                       lookback: int = 14) -> str | None:
    """Detect bearish divergence: price higher high but indicator lower high.

    Returns 'bearish' or None.
    """
    if len(close) < lookback or len(indicator) < lookback:
        return None
    c = close.iloc[-lookback:]
    ind = indicator.iloc[-lookback:].dropna()
    if len(ind) < lookback // 2:
        return None
    mid = lookback // 2
    c_first_half_max = float(c.iloc[:mid].max())
    c_second_half_max = float(c.iloc[mid:].max())
    ind_first = ind.iloc[:min(mid, len(ind))]
    ind_second = ind.iloc[min(mid, len(ind)):]
    if len(ind_first) == 0 or len(ind_second) == 0:
        return None
    i_first_half_max = float(ind_first.max())
    i_second_half_max = float(ind_second.max())
    if c_second_half_max > c_first_half_max and i_second_half_max < i_first_half_max:
        return "bearish"
    return None


def _detect_candle_pattern(open_s: pd.Series, high: pd.Series,
                           low: pd.Series, close: pd.Series) -> str | None:
    """Detect bullish engulfing or hammer on the last 2 candles."""
    if len(close) < 2:
        return None
    o1, h1, l1, c1 = float(open_s.iloc[-2]), float(high.iloc[-2]), float(low.iloc[-2]), float(close.iloc[-2])
    o2, h2, l2, c2 = float(open_s.iloc[-1]), float(high.iloc[-1]), float(low.iloc[-1]), float(close.iloc[-1])
    if any(pd.isna(v) for v in (o1, h1, l1, c1, o2, h2, l2, c2)):
        return None
    body1 = c1 - o1
    body2 = c2 - o2
    if body2 > 0 and body1 < 0 and c2 > o1 and o2 < c1:
        return "engulfing"
    body_abs = abs(body2)
    lower_wick = min(o2, c2) - l2
    upper_wick = h2 - max(o2, c2)
    if body_abs > 0 and lower_wick >= 2 * body_abs and upper_wick < body_abs and body2 > 0:
        return "hammer"
    return None


def _detect_vwap_reclaim(close: pd.Series, vwap_val: float | None,
                         rvol: float) -> bool:
    """Detect price reclaiming VWAP from below with above-avg volume."""
    if vwap_val is None or len(close) < 2:
        return False
    prev_close = float(close.iloc[-2])
    curr_close = float(close.iloc[-1])
    return prev_close < vwap_val and curr_close > vwap_val and rvol >= 1.2


def _detect_resistance_retests(
    high: pd.Series,
    close: pd.Series,
    resistance: float,
    tolerance_pct: float = 1.5,
    lookback: int = 20,
) -> dict[str, Any]:
    """Count how many bars touched/came within tolerance of resistance.

    Returns dict with ``retest_count``, ``bars_since_last_retest``,
    ``range_tightening`` (bool — True when recent range is contracting
    towards resistance), and ``avg_wick_ratio`` (how far wicks reached
    into the resistance zone).
    """
    result: dict[str, Any] = {
        "retest_count": 0,
        "bars_since_last_retest": lookback,
        "range_tightening": False,
        "avg_wick_ratio": 0.0,
    }
    if len(high) < lookback or resistance <= 0:
        return result

    h = high.iloc[-lookback:]
    c = close.iloc[-lookback:]
    threshold = resistance * (tolerance_pct / 100.0)
    lower_band = resistance - threshold

    touches = 0
    last_touch_idx = -1
    wick_ratios: list[float] = []

    for i in range(len(h)):
        bar_high = float(h.iloc[i])
        if pd.isna(bar_high):
            continue
        if bar_high >= lower_band:
            touches += 1
            last_touch_idx = i
            penetration = min(bar_high, resistance) - lower_band
            wick_ratios.append(penetration / threshold if threshold > 0 else 0)

    result["retest_count"] = touches
    if last_touch_idx >= 0:
        result["bars_since_last_retest"] = len(h) - 1 - last_touch_idx

    if wick_ratios:
        result["avg_wick_ratio"] = sum(wick_ratios) / len(wick_ratios)

    if len(h) >= 10:
        first_half_range = float(h.iloc[:len(h)//2].max() - h.iloc[:len(h)//2].min())
        second_half_range = float(h.iloc[len(h)//2:].max() - h.iloc[len(h)//2:].min())
        if first_half_range > 0 and second_half_range < first_half_range * 0.75:
            result["range_tightening"] = True

    return result


_news_sentiment_cache: dict[str, tuple[float, float]] = {}
_NEWS_SENTIMENT_TTL = 1800  # 30 min — keep sentiment cached longer


def _get_cached_news_sentiment(ticker: str) -> float | None:
    """Get VADER-aggregated news sentiment for a ticker (cached 15min).

    Returns float in [-1, 1] or None if unavailable.
    """
    import time as _t

    now = _t.time()
    cached = _news_sentiment_cache.get(ticker)
    if cached and now - cached[1] < _NEWS_SENTIMENT_TTL:
        return cached[0]

    try:
        from ..yf_session import get_ticker_news
        from .sentiment import aggregate_sentiment

        news = get_ticker_news(ticker, limit=5)
        if not news:
            _news_sentiment_cache[ticker] = (0.0, now)
            return 0.0

        titles = [n.get("title", "") for n in news if n.get("title")]
        if not titles:
            _news_sentiment_cache[ticker] = (0.0, now)
            return 0.0

        agg = aggregate_sentiment(titles)
        score = agg.get("avg_score", 0.0)
        _news_sentiment_cache[ticker] = (score, now)
        return score
    except Exception:
        return None


_cbo_score_cache: dict[str, tuple[float, dict | None]] = {}
_cbo_score_lock = threading.Lock()
_CBO_SCORE_TTL = 600  # 10 min for crypto (64 GB RAM — keep scores longer)


def _score_crypto_breakout(ticker: str) -> dict[str, Any] | None:
    """Score a crypto pair for intraday breakout potential on 15m candles."""
    key = ticker.upper()
    now = time.time()
    with _cbo_score_lock:
        entry = _cbo_score_cache.get(key)
        if entry and now - entry[0] < _CBO_SCORE_TTL:
            return entry[1]
    try:
        df = fetch_ohlcv_df(ticker, period="5d", interval="15m")
        ind = _compute_indicators(df, crypto=True)
        if ind is None:
            with _cbo_score_lock:
                _cbo_score_cache[key] = (time.time(), None)
            return None

        close = ind["close"]
        high = ind["high"]
        low = ind["low"]
        volume = ind["volume"]
        price = ind["price"]
        rsi_val = ind["rsi"]
        macd_line = ind["macd_line"]
        macd_sig = ind["macd_signal"]
        macd_hist = ind["macd_hist"]
        ema_9 = ind["ema_9"]
        ema_21 = ind["ema_21"]
        ema_50 = ind["ema_50"]
        ema_20 = ind["ema_20"]
        ema_100 = ind["ema_100"]
        bb_upper = ind["bb_upper"]
        bb_lower = ind["bb_lower"]
        bb_mid = ind["bb_mid"]
        bb_width = ind["bb_width"]
        atr_val = ind["atr"]
        adx_val = ind["adx"]
        stoch_k = ind["stoch_k"]
        stoch_d = ind["stoch_d"]
        stoch_k_prev = ind["stoch_k_prev"]
        stoch_d_prev = ind["stoch_d_prev"]
        rvol = ind["rel_vol"]
        vol_avg_20 = ind["vol_avg_20"]
        vol_latest = ind["vol_latest"]
        bb_squeeze = ind["bb_squeeze"]
        bb_squeeze_firing = ind["bb_squeeze_firing"]
        atr_state = ind["atr_state"]

        ema_alignment = "neutral"
        if ema_9 is not None and ema_21 is not None and ema_50 is not None:
            if price > ema_9 > ema_21 > ema_50:
                ema_alignment = "bullish_stack"
            elif price < ema_9 < ema_21 < ema_50:
                ema_alignment = "bearish_stack"
            elif ema_9 > ema_21:
                ema_alignment = "bullish"
            elif ema_9 < ema_21:
                ema_alignment = "bearish"

        breakout_confirmed = False
        breakout_dir = None
        if bb_upper is not None and bb_lower is not None:
            if price > float(bb_upper) and rvol >= 1.5:
                breakout_confirmed = True
                breakout_dir = "long"
            elif price < float(bb_lower) and rvol >= 1.5:
                breakout_confirmed = True
                breakout_dir = "short"

        # ── VWAP ──
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

        # ── 24h change ──
        change_24h = 0.0
        if len(df) >= 96:
            prev_price = float(close.iloc[-96])
            if prev_price > 0:
                change_24h = round((price - prev_price) / prev_price * 100, 2)
        elif len(df) > 1:
            prev_price = float(close.iloc[0])
            if prev_price > 0:
                change_24h = round((price - prev_price) / prev_price * 100, 2)

        # ── MACD gating ──
        macd_negative = (
            pd.notna(macd_line) and pd.notna(macd_sig) and pd.notna(macd_hist)
            and float(macd_line) < float(macd_sig) and float(macd_hist) < 0
        )
        macd_bullish = (
            pd.notna(macd_line) and pd.notna(macd_sig) and pd.notna(macd_hist)
            and float(macd_line) > float(macd_sig) and float(macd_hist) > 0
        )

        # ── Scoring ──
        score = 5.0
        signals: list[str] = []

        # RVOL
        if rvol >= 5.0:
            score += get_adaptive_weight("crypto_bo_rvol_5x")
            signals.append(f"Volume explosion ({rvol:.1f}x avg)")
        elif rvol >= 3.0:
            score += get_adaptive_weight("crypto_bo_rvol_3x")
            signals.append(f"Massive volume surge ({rvol:.1f}x avg)")
        elif rvol >= 2.0:
            score += get_adaptive_weight("crypto_bo_rvol_2x")
            signals.append(f"Strong volume ({rvol:.1f}x avg)")
        elif rvol >= 1.5:
            score += get_adaptive_weight("crypto_bo_rvol_1_5x")
            signals.append(f"Above-avg volume ({rvol:.1f}x)")
        elif rvol < 0.5:
            score += get_adaptive_weight("crypto_bo_rvol_low")
            signals.append(f"Low volume ({rvol:.1f}x) — fakeout risk")

        # Bollinger squeeze
        if bb_squeeze_firing:
            score += get_adaptive_weight("crypto_bo_squeeze_firing")
            signals.append("Bollinger squeeze FIRING — breakout imminent")
        elif bb_squeeze:
            score += get_adaptive_weight("crypto_bo_squeeze")
            signals.append("Bollinger squeeze — consolidation, big move coming")

        # Breakout confirmation
        if breakout_confirmed:
            score += get_adaptive_weight("crypto_bo_breakout_confirmed")
            dir_label = "above upper BB" if breakout_dir == "long" else "below lower BB"
            signals.append(f"CONFIRMED breakout {dir_label} on volume")

        # ATR state
        if atr_state == "expanding":
            score += get_adaptive_weight("crypto_bo_atr_expanding")
            signals.append("ATR expanding — volatility increasing")
        elif atr_state == "compressed":
            if bb_squeeze:
                score += get_adaptive_weight("crypto_bo_atr_compressed_squeeze")
                signals.append("ATR compressed + BB squeeze — coiled spring")
            else:
                signals.append("ATR compressed — range tightening")

        # EMA stack
        if ema_alignment == "bullish_stack":
            score += get_adaptive_weight("crypto_bo_ema_bull_stack")
            signals.append("Full bullish EMA stack (P > 9 > 21 > 50)")
        elif ema_alignment == "bullish":
            score += get_adaptive_weight("crypto_bo_ema_bull")
            signals.append("EMA 9 > EMA 21 — short-term bullish")
        elif ema_alignment == "bearish_stack":
            score += get_adaptive_weight("crypto_bo_ema_bear_stack")
            signals.append("Bearish EMA stack")

        # RSI
        crypto_ema_stack = ema_alignment == "bullish_stack"
        if pd.notna(rsi_val):
            if 40 <= rsi_val <= 65:
                score += get_adaptive_weight("crypto_bo_rsi_momentum")
                signals.append(f"RSI in momentum zone ({rsi_val:.0f})")
            elif rsi_val < 25:
                score += get_adaptive_weight("crypto_bo_rsi_oversold")
                signals.append(f"RSI deeply oversold ({rsi_val:.0f}) — bounce candidate")
            elif rsi_val > 80:
                if crypto_ema_stack:
                    score += get_adaptive_weight("crypto_bo_rsi_momentum_strong")
                    signals.append(f"RSI momentum ({rsi_val:.0f}) + EMA stack — strong continuation")
                else:
                    score += get_adaptive_weight("crypto_bo_rsi_overextended")
                    signals.append(f"RSI overextended ({rsi_val:.0f}) — caution")
            elif 65 < rsi_val <= 80 and crypto_ema_stack:
                score += get_adaptive_weight("crypto_bo_rsi_momentum_strong")
                signals.append(f"RSI momentum ({rsi_val:.0f}) + EMA stack — breakout continuation")

        # ── Resistance retest pressure (momentum breakout path) ──
        retest_info = _detect_resistance_retests(high, close, resistance, tolerance_pct=2.0, lookback=20)
        if retest_info["retest_count"] >= 3:
            score += get_adaptive_weight("crypto_bo_retest_pressure")
            signals.append(f"Resistance retested {retest_info['retest_count']}x — pressure building")
            if retest_info["range_tightening"]:
                nr = _detect_narrow_range(high, low)
                vcp_count = _detect_vcp(high, low, volume, lookback=20)
                if nr or vcp_count >= 2:
                    score += get_adaptive_weight("crypto_bo_retest_consolidation")
                    tag = nr or f"VCP({vcp_count})"
                    signals.append(f"Post-retest consolidation ({tag}) — coiled for breakout")

        # ── Multi-timeframe retest confirmation (1h) ──
        try:
            df_1h = fetch_ohlcv_df(ticker, interval="1h", period="5d")
            if not df_1h.empty and len(df_1h) >= 20:
                h_1h = df_1h["High"]
                c_1h = df_1h["Close"]
                res_1h = float(h_1h.rolling(20).max().iloc[-1])
                mtf_retest = _detect_resistance_retests(h_1h, c_1h, res_1h, tolerance_pct=1.0, lookback=30)
                if mtf_retest["retest_count"] >= 3:
                    score += get_adaptive_weight("crypto_bo_mtf_retest_confirm")
                    signals.append(f"1h: {mtf_retest['retest_count']}x retest — multi-TF pressure confirmed")
        except Exception:
            pass

        # MACD
        if macd_bullish:
            score += get_adaptive_weight("crypto_bo_macd_bull")
            signals.append("MACD bullish crossover confirmed")
        elif macd_negative:
            score += get_adaptive_weight("crypto_bo_macd_bear")
            signals.append("MACD bearish — momentum lost")

        # VWAP
        if vwap is not None:
            if price > vwap:
                score += get_adaptive_weight("crypto_bo_vwap_above")
                signals.append(f"Above VWAP ({vwap_pct:+.1f}%)")
            else:
                score += get_adaptive_weight("crypto_bo_vwap_below")
                signals.append(f"Below VWAP ({vwap_pct:+.1f}%)")

        # ADX trend strength
        if pd.notna(adx_val):
            if adx_val > 25:
                score += get_adaptive_weight("crypto_bo_adx_strong")
                signals.append(f"Strong trend (ADX {adx_val:.0f})")
            elif adx_val < 15:
                signals.append(f"Weak trend (ADX {adx_val:.0f}) — range-bound")

        # 24h momentum
        if change_24h >= 10:
            score += get_adaptive_weight("crypto_bo_hot_mover")
            signals.append(f"Hot mover +{change_24h:.1f}% (24h)")
        elif change_24h >= 5:
            score += get_adaptive_weight("crypto_bo_gaining")
            signals.append(f"Gaining +{change_24h:.1f}% (24h)")

        # Volume awakening: RVOL picking up during a squeeze (early breakout sign)
        if bb_squeeze and 0.8 <= rvol <= 1.5:
            score += get_adaptive_weight("crypto_bo_vol_awakening")
            signals.append(f"Volume awakening in squeeze ({rvol:.1f}x)")

        # Stochastic curl in squeeze: %K crossing above %D inside a BB squeeze
        if bb_squeeze and pd.notna(stoch_k) and pd.notna(stoch_d):
            if pd.notna(stoch_k_prev) and pd.notna(stoch_d_prev):
                if stoch_k_prev <= stoch_d_prev and stoch_k > stoch_d and stoch_k < 80:
                    score += get_adaptive_weight("crypto_bo_stoch_curl_squeeze")
                    signals.append(f"Stochastic curl in squeeze (%K {stoch_k:.0f} > %D {stoch_d:.0f})")

        # Higher lows into flat resistance (ascending triangle / pressure building)
        if len(low) >= 12 and pd.notna(bb_upper):
            recent_lows = low.iloc[-12:]
            swing_lows = []
            for i in range(1, len(recent_lows) - 1):
                if recent_lows.iloc[i] <= recent_lows.iloc[i - 1] and recent_lows.iloc[i] <= recent_lows.iloc[i + 1]:
                    swing_lows.append(float(recent_lows.iloc[i]))
            if len(swing_lows) >= 3:
                ascending = all(swing_lows[j] < swing_lows[j + 1] for j in range(len(swing_lows) - 1))
                near_resistance = abs(price - float(bb_upper)) / price < 0.015
                if ascending and near_resistance:
                    score += get_adaptive_weight("crypto_bo_higher_lows")
                    signals.append("Higher lows into resistance — pressure building")

        # ── Volume Contraction Pattern (VCP) ──
        vcp_count = _detect_vcp(high, low, volume)
        if vcp_count >= 3:
            score += get_adaptive_weight("crypto_bo_vcp_3plus")
            signals.append(f"VCP: {vcp_count} successive contractions — high-probability setup")
        elif vcp_count == 2:
            score += get_adaptive_weight("crypto_bo_vcp_2")
            signals.append("VCP: 2 contractions — tightening")

        # ── Narrow Range (NR4/NR7) ──
        nr_type = _detect_narrow_range(high, low)
        if nr_type == "NR7":
            score += get_adaptive_weight("crypto_bo_nr7")
            signals.append("NR7 — tightest range in 7 bars, expansion imminent")
        elif nr_type == "NR4":
            score += get_adaptive_weight("crypto_bo_nr4")
            signals.append("NR4 — tightest range in 4 bars")

        # ── Multi-Timeframe Confirmation (1h trend vs 15m setup) ──
        try:
            df_1h = fetch_ohlcv_df(ticker, period="5d", interval="1h")
            if len(df_1h) >= 50:
                ema9_1h = EMAIndicator(close=df_1h["Close"], window=9).ema_indicator().iloc[-1]
                ema21_1h = EMAIndicator(close=df_1h["Close"], window=21).ema_indicator().iloc[-1]
                if pd.notna(ema9_1h) and pd.notna(ema21_1h):
                    if float(ema9_1h) > float(ema21_1h) and ema_alignment in ("bullish_stack", "bullish"):
                        score += get_adaptive_weight("crypto_bo_mtf_confirm")
                        signals.append("Multi-TF confirmed: 1h trend bullish + 15m setup aligned")
        except Exception:
            pass

        # ── VWAP Reclaim ──
        if _detect_vwap_reclaim(close, vwap, rvol):
            score += get_adaptive_weight("crypto_bo_vwap_reclaim")
            signals.append("VWAP reclaimed from below on volume — institutional buying")

        # ── Accumulation/Distribution ──
        if _detect_accumulation(close, volume, bb_squeeze):
            score += get_adaptive_weight("crypto_bo_accumulation")
            signals.append("OBV rising while price flat — accumulation detected")

        # ── Momentum Divergence Filter ──
        rsi_series = RSIIndicator(close=close, window=14).rsi()
        _macd_obj = MACD(close=close)
        macd_hist_series = _macd_obj.macd_diff()
        rsi_div = _detect_divergence(close, rsi_series)
        if rsi_div == "bearish":
            score += get_adaptive_weight("crypto_bo_rsi_divergence_penalty")
            signals.append("RSI bearish divergence — fakeout risk")
        macd_div = _detect_divergence(close, macd_hist_series)
        if macd_div == "bearish":
            score += get_adaptive_weight("crypto_bo_macd_divergence_penalty")
            signals.append("MACD bearish divergence — momentum weakening")

        # ── Candlestick Patterns at Key Levels ──
        candle = _detect_candle_pattern(df["Open"], high, low, close)
        if candle == "engulfing" and pd.notna(bb_upper) and abs(price - float(bb_upper)) / price < 0.02:
            score += get_adaptive_weight("crypto_bo_candle_engulfing")
            signals.append("Bullish engulfing candle near resistance")
        elif candle == "hammer":
            score += get_adaptive_weight("crypto_bo_candle_hammer")
            signals.append("Hammer candle — buyers rejecting lower prices")

        # ── BTC Leading Indicator ──
        if ticker.upper() != "BTC-USD":
            try:
                from .market_data import get_btc_state
                _btc = get_btc_state()
                _btc_trend = _btc.get("btc_trend", "flat")
                _btc_4h = _btc.get("btc_4h_momentum", 0)
                if _btc_trend == "up":
                    score += get_adaptive_weight("crypto_bo_btc_trend_aligned")
                    signals.append("BTC trending up — tailwind for altcoin breakouts")
                elif _btc_trend == "down":
                    score += get_adaptive_weight("crypto_bo_btc_trend_against")
                    signals.append("BTC trending down — headwind for altcoin breakouts")
                if _btc_4h < -3:
                    score += get_adaptive_weight("crypto_bo_btc_dumping_penalty")
                    signals.append("BTC dumping hard — altcoin breakouts rarely survive this")
            except Exception:
                pass

        # ── Fakeout Penalties (learned from outcome data) ──
        _rsi_raw = float(rsi_series.iloc[-1]) if pd.notna(rsi_series.iloc[-1]) else 50
        if bb_squeeze and _rsi_raw > 65:
            score += get_adaptive_weight("crypto_bo_fakeout_rsi_high_penalty")
            signals.append("RSI high during squeeze — historically higher fakeout rate")
        if bb_squeeze and adx_val is not None and adx_val > 30:
            score += get_adaptive_weight("crypto_bo_fakeout_trending_adx_penalty")
            signals.append("ADX trending during squeeze — squeeze may not resolve cleanly")

        # ── News Sentiment ──
        _news_score = _get_cached_news_sentiment(ticker)
        if _news_score is not None and abs(_news_score) > 0.1:
            if _news_score > 0.15:
                score += get_adaptive_weight("crypto_bo_news_bullish_bonus")
                signals.append("Recent news sentiment is bullish — catalyst could accelerate breakout")
            elif _news_score < -0.15:
                score += get_adaptive_weight("crypto_bo_news_bearish_penalty")
                signals.append("Recent news sentiment is bearish — breakout may face headwinds")

        # Cap + signal
        if macd_negative and not breakout_confirmed:
            score = min(score, get_adaptive_weight("crypto_bo_macd_neg_cap"))

        # ── Composable Pattern Engine evaluation ──
        try:
            from .pattern_engine import evaluate_patterns, build_indicator_snapshot, get_active_patterns
            from ...db import SessionLocal as _SL
            _pe_db = _SL()
            try:
                _pe_patterns = get_active_patterns(_pe_db, asset_class="crypto")
                if _pe_patterns:
                    _pe_snap = build_indicator_snapshot(
                        price=price,
                        indicators={
                            "rsi_14": float(rsi_val) if pd.notna(rsi_val) else None,
                            "ema_20": float(ema_20) if pd.notna(ema_20) else None,
                            "ema_50": float(ema_50) if pd.notna(ema_50) else None,
                            "ema_100": float(ema_100) if pd.notna(ema_100) else None,
                            "bb_squeeze": bb_squeeze,
                            "adx": float(adx_val) if pd.notna(adx_val) else None,
                            "rel_vol": rvol,
                            "macd_hist": float(macd_hist) if pd.notna(macd_hist) else None,
                            "narrow_range": _detect_narrow_range(high, low),
                            "vcp_count": _detect_vcp(high, low, volume, lookback=20),
                        },
                        resistance=resistance,
                        retest_info=retest_info,
                    )
                    _pe_matches = evaluate_patterns(_pe_snap, _pe_patterns)
                    for m in _pe_matches:
                        if score >= m.get("min_base_score", 0):
                            score += m["score_boost"]
                            signals.append(f"Pattern match: {m['name']} (+{m['score_boost']:.1f})")
            finally:
                _pe_db.close()
        except Exception:
            pass

        score = max(1.0, min(10.0, score))

        _c_long = get_adaptive_weight("crypto_bo_signal_long")
        _c_short = get_adaptive_weight("crypto_bo_signal_short")
        if breakout_confirmed and breakout_dir == "long":
            signal = "long"
        elif breakout_confirmed and breakout_dir == "short":
            signal = "short"
        elif score >= _c_long:
            signal = "long"
        elif score <= _c_short:
            signal = "short"
        else:
            signal = "watch"

        # Resistance & distance (use 20-bar rolling high on 15m candles)
        resistance = float(high.rolling(20).max().iloc[-1])
        dist_to_breakout = round((resistance - price) / price * 100, 2) if resistance and price else 0.0

        if dist_to_breakout <= 0:
            status = "breaking_out"
        elif score >= get_adaptive_weight("bo_signal_ready"):
            status = "ready"
        elif score >= get_adaptive_weight("bo_signal_watch"):
            status = "watch"
        else:
            status = "wait"

        # Entry / stop / target
        _c_stop_m = get_adaptive_weight("crypto_bo_stop_atr_mult")
        _c_tgt_m = get_adaptive_weight("crypto_bo_target_atr_mult")
        entry = smart_round(price, crypto=True)
        stop = smart_round(price - _c_stop_m * atr_val, crypto=True)
        target = smart_round(price + _c_tgt_m * atr_val, crypto=True)
        rr = round(_c_tgt_m / _c_stop_m, 2)

        bb_width_pct_rank = ind.get("bb_width_pct_rank", 0.0 if bb_squeeze else 50.0)

        result = {
            "ticker": ticker.upper(),
            "score": round(score, 1),
            "signal": signal,
            "status": status,
            "price": entry,
            "resistance": smart_round(resistance, crypto=True),
            "dist_to_breakout": dist_to_breakout,
            "bb_width_pctile": round(bb_width_pct_rank, 0),
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit": target,
            "risk_reward": rr,
            "risk_level": "high" if atr_val / price > 0.02 else "medium",
            "signals": signals,
            "rvol": rvol,
            "bb_squeeze": bb_squeeze,
            "bb_squeeze_firing": bb_squeeze_firing,
            "breakout_confirmed": breakout_confirmed,
            "breakout_dir": breakout_dir,
            "atr_state": atr_state,
            "ema_alignment": ema_alignment,
            "change_24h": change_24h,
            "vwap": smart_round(vwap, crypto=True) if vwap else None,
            "vwap_pct": vwap_pct,
            "indicators": {
                "rsi": round(float(rsi_val), 1) if pd.notna(rsi_val) else None,
                "macd_hist": round(float(macd_hist), 4) if pd.notna(macd_hist) else None,
                "adx": round(float(adx_val), 1) if pd.notna(adx_val) else None,
                "atr": round(atr_val, 6),
                "bb_width": round(bb_width, 4),
                "ema_9": smart_round(float(ema_9), crypto=True) if pd.notna(ema_9) else None,
                "ema_20": smart_round(float(ema_20), crypto=True) if pd.notna(ema_20) else None,
                "ema_21": smart_round(float(ema_21), crypto=True) if pd.notna(ema_21) else None,
                "ema_50": smart_round(float(ema_50), crypto=True) if pd.notna(ema_50) else None,
                "ema_100": smart_round(float(ema_100), crypto=True) if pd.notna(ema_100) else None,
                "stoch_k": round(float(stoch_k), 1) if pd.notna(stoch_k) else None,
                "stoch_d": round(float(stoch_d), 1) if pd.notna(stoch_d) else None,
                "rvol": rvol,
            },
            "news_sentiment": _news_score,
            "sector": "crypto",
            "hold_estimate": _estimate_hold_duration(
                entry, target, atr_val, "15m",
                float(adx_val) if pd.notna(adx_val) else None,
            ),
        }
        with _cbo_score_lock:
            _cbo_score_cache[key] = (time.time(), result)
        return result
    except Exception as e:
        logger.debug(f"[crypto_breakout] {ticker} failed: {e}")
        with _cbo_score_lock:
            _cbo_score_cache[key] = (time.time(), None)
        return None


def run_crypto_breakout_scan(max_results: int = 20) -> dict[str, Any]:
    """Scan 70+ crypto pairs for intraday breakout setups on 15m candles.

    Results are cached for 15 minutes. Runs all crypto candidates through
    _score_crypto_breakout() and returns the top setups sorted by score.
    """
    global _crypto_breakout_cache, _crypto_scan_running

    now = time.time()
    if _crypto_breakout_cache["ts"] > 0 and (now - _crypto_breakout_cache["ts"]) < _CRYPTO_BREAKOUT_TTL:
        return {
            "ok": True,
            "cached": True,
            "results": _crypto_breakout_cache["results"][:max_results],
            "total_scanned": _crypto_breakout_cache.get("total_scanned", 0),
            "scan_time": _crypto_breakout_cache.get("scan_time"),
            "age_seconds": int(now - _crypto_breakout_cache["ts"]),
        }

    if _crypto_scan_running:
        return {"ok": True, "warming_up": True, "results": [], "total_scanned": 0}
    _crypto_scan_running = True

    from .prescreener import get_trending_crypto, _crypto_top_movers

    try:
        tickers = set(DEFAULT_CRYPTO_TICKERS)

        # Pull the full crypto universe the brain knows about
        try:
            from ..ticker_universe import get_all_crypto_tickers
            tickers.update(get_all_crypto_tickers(n=200))
        except Exception:
            pass

        # Pull crypto tickers the brain has already scored (DB knowledge)
        try:
            from ...db import SessionLocal
            from ...models.trading import MarketSnapshot
            _db = SessionLocal()
            try:
                known = _db.query(MarketSnapshot.ticker).filter(
                    MarketSnapshot.ticker.like("%-USD")
                ).distinct().all()
                tickers.update(t[0] for t in known)
            finally:
                _db.close()
        except Exception:
            pass

        try:
            tickers.update(get_trending_crypto())
        except Exception:
            pass
        try:
            tickers.update(_crypto_top_movers())
        except Exception:
            pass

        # NOTE: We intentionally skip the Massive dead-ticker filter here.
        # Many crypto tickers 404 on Massive but resolve fine via yfinance.
        # The hard RSI/EMA filters will drop genuinely bad tickers anyway.

        ticker_list = sorted(tickers)
        total = len(ticker_list)
        logger.info(f"[crypto_breakout] Scanning {total} crypto pairs...")

        t_pw = time.time()
        try:
            fetch_ohlcv_batch(ticker_list, interval="15m", period="5d")
        except Exception as e:
            logger.warning(f"[crypto_breakout] Batch pre-warm failed (will fetch individually): {e}")
        logger.info(f"[crypto_breakout] Pre-warm {len(ticker_list)} tickers in {time.time()-t_pw:.1f}s")

        results = []
        errors = 0
        t0 = time.time()

        with ThreadPoolExecutor(max_workers=_MAX_SCAN_WORKERS) as pool:
            future_map = {
                pool.submit(_score_crypto_breakout, t): t
                for t in ticker_list
                if not _shutting_down.is_set()
            }
            for future in as_completed(future_map):
                if _shutting_down.is_set():
                    break
                try:
                    r = future.result(timeout=30)
                    if r is not None:
                        results.append(r)
                except Exception:
                    errors += 1

        results.sort(key=lambda x: x["score"], reverse=True)
        elapsed = round(time.time() - t0, 1)

        _crypto_breakout_cache.update(
            results=results, ts=time.time(),
            total_scanned=total,
            scan_time=datetime.utcnow().isoformat(),
            elapsed_s=elapsed, errors=errors,
        )

        above_6 = sum(1 for r in results if r["score"] >= 6)
        above_7 = sum(1 for r in results if r["score"] >= 7)
        squeezes = sum(1 for r in results if r.get("bb_squeeze"))
        logger.info(
            f"[crypto_breakout] Done: {len(results)}/{total} scored, "
            f"{errors} errors, {elapsed}s, "
            f"score>=6: {above_6}, score>=7: {above_7}, squeezes: {squeezes}, "
            f"top: "
            + (results[0]["ticker"] + f" ({results[0]['score']})" if results else "none")
        )

        return {
            "ok": True,
            "cached": False,
            "results": results[:max_results],
            "total_scanned": total,
            "scan_time": _crypto_breakout_cache["scan_time"],
            "elapsed_s": elapsed,
            "age_seconds": 0,
        }
    except Exception as exc:
        logger.exception(f"[crypto_breakout] Scan failed: {exc}")
        return {"ok": False, "error": str(exc), "results": [], "total_scanned": 0}
    finally:
        _crypto_scan_running = False


def get_crypto_breakout_cache() -> dict[str, Any]:
    """Return the current cached crypto breakout results (for brain context)."""
    now = time.time()
    age = int(now - _crypto_breakout_cache["ts"]) if _crypto_breakout_cache["ts"] > 0 else None
    return {
        "results": _crypto_breakout_cache.get("results", []),
        "scan_time": _crypto_breakout_cache.get("scan_time"),
        "age_seconds": age,
        "total_scanned": _crypto_breakout_cache.get("total_scanned", 0),
    }


# ── Breakout Detection Scoring ────────────────────────────────────────

_bo_score_cache: dict[str, tuple[float, dict | None]] = {}
_bo_score_lock = threading.Lock()
_BO_SCORE_TTL = 900  # 15 min — breakout setups don't change fast (64 GB RAM)


def _score_breakout(ticker: str) -> dict[str, Any] | None:
    """Score a ticker for breakout readiness."""
    key = ticker.upper()
    now = time.time()
    with _bo_score_lock:
        entry = _bo_score_cache.get(key)
        if entry and now - entry[0] < _BO_SCORE_TTL:
            return entry[1]
    try:
        df = fetch_ohlcv_df(ticker, period="6mo", interval="1d")
        ind = _compute_indicators(df, crypto=False)
        if ind is None:
            with _bo_score_lock:
                _bo_score_cache[key] = (time.time(), None)
            return None

        close = ind["close"]
        high = ind["high"]
        low = ind["low"]
        volume = ind["volume"]
        price = ind["price"]
        rsi_val = ind["rsi"]
        macd_line = ind["macd_line"]
        macd_sig = ind["macd_signal"]
        macd_hist = ind["macd_hist"]
        ema_20 = ind["ema_20"]
        ema_50 = ind["ema_50"]
        ema_100 = ind["ema_100"]
        adx_val = ind["adx"]
        atr_val = ind["atr"]
        rel_vol = ind["rel_vol"]
        vol_avg_20 = ind["vol_avg_20"]
        vol_latest = ind["vol_latest"]
        is_squeeze = ind["bb_squeeze"]
        bb_width_pct_rank = ind.get("bb_width_pct_rank", 0.0 if is_squeeze else 50.0)

        resistance = float(high.rolling(20).max().iloc[-1])
        dist_to_breakout = round((resistance - price) / price * 100, 2)

        vol_recent = float(volume.iloc[-5:].mean())
        vol_prior = float(volume.iloc[-20:-5].mean()) if len(volume) >= 20 else vol_recent
        vol_declining = vol_recent < vol_prior * 0.8
        vol_trend_pct = round((vol_recent - vol_prior) / vol_prior * 100, 1) if vol_prior > 0 else 0

        recent_range = high.iloc[-20:].max() - low.iloc[-20:].min()
        daily_ranges = high.iloc[-20:] - low.iloc[-20:]
        tight_days = int((daily_ranges < recent_range * 0.3).sum())

        score = 5.0
        signals: list[str] = []

        if is_squeeze:
            score += get_adaptive_weight("bo_squeeze")
            signals.append(f"Bollinger squeeze (width percentile: {bb_width_pct_rank:.0f}%)")

        if 0 <= dist_to_breakout <= 2.0:
            score += get_adaptive_weight("bo_near_resistance_close")
            signals.append(f"Near resistance — {dist_to_breakout:.1f}% to breakout")
        elif 2.0 < dist_to_breakout <= 5.0:
            score += get_adaptive_weight("bo_near_resistance_mid")
            signals.append(f"{dist_to_breakout:.1f}% below resistance")

        # Already breaking out
        if dist_to_breakout <= 0:
            score += get_adaptive_weight("bo_breaking_out")
            signals.append("BREAKING OUT — new 20-day high!")

        # Low ADX = consolidation, not trending
        if pd.notna(adx_val):
            if adx_val < 20:
                score += get_adaptive_weight("bo_adx_consolidating")
                signals.append(f"Low ADX ({adx_val:.0f}) — consolidating")
            elif adx_val > 30:
                score += get_adaptive_weight("bo_adx_trending")

        # Declining volume = coiling
        if vol_declining:
            score += get_adaptive_weight("bo_vol_declining")
            signals.append(f"Volume declining ({vol_trend_pct:+.0f}%) — coiling")

        # EMA support
        if pd.notna(ema_20) and pd.notna(ema_50):
            if price > float(ema_20) > float(ema_50):
                score += get_adaptive_weight("bo_ema_support")
                if pd.notna(ema_100) and float(ema_20) > float(ema_50) > float(ema_100):
                    signals.append("Above rising EMAs (20/50/100) — strong bullish base")
                else:
                    signals.append("Above rising EMAs — bullish base")

        # RSI neutral zone (not overbought for a pre-breakout)
        ema_stack_bullish = (pd.notna(ema_20) and pd.notna(ema_50) and pd.notna(ema_100)
                            and price > float(ema_20) > float(ema_50) > float(ema_100))
        if pd.notna(rsi_val):
            if 45 <= rsi_val <= 65:
                score += get_adaptive_weight("bo_rsi_neutral")
                signals.append(f"RSI neutral ({rsi_val:.0f}) — room to run")
            elif rsi_val > 70:
                if ema_stack_bullish:
                    score += get_adaptive_weight("bo_rsi_momentum_strong")
                    signals.append(f"RSI momentum ({rsi_val:.0f}) + EMA stack — strong continuation")
                else:
                    score += get_adaptive_weight("bo_rsi_overbought")
                    signals.append(f"RSI overbought ({rsi_val:.0f}) — may fade")
            elif 65 < rsi_val <= 80 and ema_stack_bullish:
                score += get_adaptive_weight("bo_rsi_momentum_strong")
                signals.append(f"RSI momentum ({rsi_val:.0f}) + EMA stack — breakout continuation setup")

        # ── Resistance retest pressure (momentum breakout path) ──
        retest_info = _detect_resistance_retests(high, close, resistance, tolerance_pct=1.5, lookback=20)
        if retest_info["retest_count"] >= 3:
            score += get_adaptive_weight("bo_retest_pressure")
            signals.append(f"Resistance retested {retest_info['retest_count']}x — pressure building")
            if retest_info["range_tightening"]:
                nr = _detect_narrow_range(high, low)
                vcp_count = _detect_vcp(high, low, volume, lookback=20)
                if nr or vcp_count >= 2:
                    score += get_adaptive_weight("bo_retest_consolidation")
                    tag = nr or f"VCP({vcp_count})"
                    signals.append(f"Post-retest consolidation ({tag}) — coiled for breakout")

        # ── Multi-timeframe retest confirmation (15m) ──
        try:
            df_15m = fetch_ohlcv_df(ticker, interval="15m", period="5d")
            if not df_15m.empty and len(df_15m) >= 20:
                h_15m = df_15m["High"]
                c_15m = df_15m["Close"]
                res_15m = float(h_15m.rolling(20).max().iloc[-1])
                mtf_retest = _detect_resistance_retests(h_15m, c_15m, res_15m, tolerance_pct=1.0, lookback=40)
                if mtf_retest["retest_count"] >= 3:
                    score += get_adaptive_weight("bo_mtf_retest_confirm")
                    signals.append(f"15m: {mtf_retest['retest_count']}x retest of ${smart_round(res_15m)} — intraday pressure confirmed")
        except Exception:
            pass

        # ── MACD gate (primary momentum filter — weight is brain-adaptive) ──
        if pd.notna(macd_hist) and pd.notna(macd_line) and pd.notna(macd_sig):
            if float(macd_line) > float(macd_sig) and float(macd_hist) > 0:
                score += get_adaptive_weight("macd_positive_bonus")
                signals.append("MACD positive + histogram rising — strong momentum")
            elif float(macd_hist) > 0:
                score += get_adaptive_weight("bo_macd_building")
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

        # ── Float size (only uses cached fundamentals — never blocks on API call) ──
        if not _is_crypto:
            try:
                from ..yf_session import _cache_get as _yf_cache_get
                _fund_cached = _yf_cache_get(f"fund:{ticker}")
                if _fund_cached and _fund_cached != "EMPTY" and isinstance(_fund_cached, dict) and _fund_cached.get("market_cap"):
                    shares = _fund_cached["market_cap"] / price if price > 0 else None
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

        # ── Volume Contraction Pattern (VCP) ──
        vcp_count = _detect_vcp(high, low, volume)
        if vcp_count >= 3:
            score += get_adaptive_weight("bo_vcp_3plus")
            signals.append(f"VCP: {vcp_count} successive contractions — high-probability setup")
        elif vcp_count == 2:
            score += get_adaptive_weight("bo_vcp_2")
            signals.append("VCP: 2 contractions — tightening")

        # ── Narrow Range (NR4/NR7) ──
        nr_type = _detect_narrow_range(high, low)
        if nr_type == "NR7":
            score += get_adaptive_weight("bo_nr7")
            signals.append("NR7 — tightest range in 7 bars, expansion imminent")
        elif nr_type == "NR4":
            score += get_adaptive_weight("bo_nr4")
            signals.append("NR4 — tightest range in 4 bars")

        # ── Multi-Timeframe Confirmation ──
        if pd.notna(ema_20) and pd.notna(ema_50) and price > float(ema_20) > float(ema_50):
            score += get_adaptive_weight("bo_mtf_confirm")
            signals.append("Daily EMA trend bullish — confirms breakout setup")

        # ── Accumulation/Distribution ──
        if _detect_accumulation(close, volume, is_squeeze):
            score += get_adaptive_weight("bo_accumulation")
            signals.append("OBV rising while price flat — accumulation detected")

        # ── Momentum Divergence Filter ──
        rsi_series = RSIIndicator(close=close, window=14).rsi()
        macd_hist_series = MACD(close=close).macd_diff()
        rsi_div = _detect_divergence(close, rsi_series)
        if rsi_div == "bearish":
            score += get_adaptive_weight("bo_rsi_divergence_penalty")
            signals.append("RSI bearish divergence — fakeout risk")
        macd_div = _detect_divergence(close, macd_hist_series)
        if macd_div == "bearish":
            score += get_adaptive_weight("bo_macd_divergence_penalty")
            signals.append("MACD bearish divergence — momentum weakening")

        # ── Candlestick Patterns at Key Levels ──
        candle = _detect_candle_pattern(df["Open"], high, low, close)
        bb_upper_last = ind["bb_upper"]
        if candle == "engulfing" and bb_upper_last and abs(price - bb_upper_last) / price < 0.02:
            score += get_adaptive_weight("bo_candle_engulfing")
            signals.append("Bullish engulfing candle near resistance")
        elif candle == "hammer":
            score += get_adaptive_weight("bo_candle_hammer")
            signals.append("Hammer candle — buyers rejecting lower prices")

        # ── Opening Range Breakout (market hours) ──
        if not _is_crypto:
            try:
                from datetime import time as _dt_time
                import pytz
                et = pytz.timezone("US/Eastern")
                now_et = datetime.now(et)
                if now_et.hour >= 10:
                    df_intra = fetch_ohlcv_df(ticker, period="1d", interval="5m")
                    if not df_intra.empty and len(df_intra) >= 6:
                        idx = df_intra.index
                        if hasattr(idx, 'tz_localize'):
                            try:
                                idx = idx.tz_localize(et)
                            except Exception:
                                pass
                        orb_mask = idx.indexer_between_time("09:30", "10:00")
                        if len(orb_mask) >= 2:
                            orb_high = float(df_intra.iloc[orb_mask]["High"].max())
                            orb_low = float(df_intra.iloc[orb_mask]["Low"].min())
                            if price > orb_high:
                                score += get_adaptive_weight("bo_orb_break_above")
                                signals.append(f"Opening Range Breakout — above ORB high ${orb_high:.2f}")
                            elif price < orb_low:
                                score += get_adaptive_weight("bo_orb_break_below")
                                signals.append(f"Below Opening Range low ${orb_low:.2f}")
            except Exception:
                pass

        # ── VWAP Reclaim (intraday) ──
        if not _is_crypto:
            try:
                df_intra = fetch_ohlcv_df(ticker, period="1d", interval="5m")
                if not df_intra.empty and len(df_intra) > 5:
                    typical = (df_intra["High"] + df_intra["Low"] + df_intra["Close"]) / 3
                    cum_vol = df_intra["Volume"].cumsum()
                    cum_tp_vol = (typical * df_intra["Volume"]).cumsum()
                    if float(cum_vol.iloc[-1]) > 0:
                        _vwap = float(cum_tp_vol.iloc[-1] / cum_vol.iloc[-1])
                        if _detect_vwap_reclaim(df_intra["Close"], _vwap, rel_vol):
                            score += get_adaptive_weight("bo_vwap_reclaim")
                            signals.append("VWAP reclaimed from below on volume — institutional buying")
            except Exception:
                pass

        # ── Weekly Multi-Timeframe Confirmation ──
        if not _is_crypto:
            try:
                from ta.trend import EMAIndicator as _EMA
                df_wk = fetch_ohlcv_df(ticker, period="6mo", interval="1wk")
                if df_wk is not None and len(df_wk) >= 12:
                    wk_close = df_wk["Close"]
                    wk_ema9 = _EMA(close=wk_close, window=9).ema_indicator()
                    wk_ema21 = _EMA(close=wk_close, window=21).ema_indicator()
                    _we9 = float(wk_ema9.iloc[-1]) if pd.notna(wk_ema9.iloc[-1]) else None
                    _we21 = float(wk_ema21.iloc[-1]) if pd.notna(wk_ema21.iloc[-1]) else None
                    if _we9 is not None and _we21 is not None:
                        if _we9 > _we21 and price > _we9:
                            score += get_adaptive_weight("bo_mtf_confirm")
                            signals.append("Weekly trend confirms daily setup — higher timeframe support")
                        elif _we9 < _we21:
                            score -= 0.3
                            signals.append("Weekly EMAs bearish — higher timeframe headwind")
            except Exception:
                pass

        # ── Market regime modifier for breakouts (brain-adaptive) ──
        try:
            _regime = get_market_regime()
            _vix_regime = _regime.get("vix_regime", "normal")
            if _vix_regime in ("elevated", "extreme"):
                score += get_adaptive_weight("regime_vix_breakout_penalty")
                signals.append(f"High VIX ({_vix_regime}) — false breakout risk elevated")
        except Exception:
            pass

        # ── Fakeout Penalties (learned from outcome data) ──
        if is_squeeze and rsi_val is not None and rsi_val > 65:
            score += get_adaptive_weight("bo_fakeout_rsi_high_penalty")
            signals.append("RSI high during squeeze — historically higher fakeout rate")
        if is_squeeze and adx_val is not None and adx_val > 30:
            score += get_adaptive_weight("bo_fakeout_trending_adx_penalty")
            signals.append("ADX trending during squeeze — squeeze may not resolve cleanly")

        # ── News Sentiment ──
        _news_score = _get_cached_news_sentiment(ticker)
        if _news_score is not None and abs(_news_score) > 0.1:
            if _news_score > 0.15:
                score += get_adaptive_weight("bo_news_bullish_bonus")
                signals.append("Recent news sentiment is bullish — catalyst could accelerate breakout")
            elif _news_score < -0.15:
                score += get_adaptive_weight("bo_news_bearish_penalty")
                signals.append("Recent news sentiment is bearish — breakout may face headwinds")

        # ── Composable Pattern Engine evaluation ──
        try:
            from .pattern_engine import evaluate_patterns, build_indicator_snapshot, get_active_patterns
            from ...models.trading import ScanPattern as _SP
            from ...db import SessionLocal as _SL
            _pe_db = _SL()
            try:
                _pe_patterns = get_active_patterns(_pe_db, asset_class="stocks")
                if _pe_patterns:
                    _pe_snap = build_indicator_snapshot(
                        price=price,
                        indicators={
                            "rsi_14": float(rsi_val) if pd.notna(rsi_val) else None,
                            "ema_20": float(ema_20) if pd.notna(ema_20) else None,
                            "ema_50": float(ema_50) if pd.notna(ema_50) else None,
                            "ema_100": float(ema_100) if pd.notna(ema_100) else None,
                            "bb_squeeze": is_squeeze,
                            "adx": float(adx_val) if pd.notna(adx_val) else None,
                            "rel_vol": rel_vol,
                            "macd_hist": float(macd_hist) if pd.notna(macd_hist) else None,
                            "narrow_range": _detect_narrow_range(high, low),
                            "vcp_count": _detect_vcp(high, low, volume, lookback=20),
                        },
                        resistance=resistance,
                        retest_info=retest_info,
                    )
                    _pe_matches = evaluate_patterns(_pe_snap, _pe_patterns)
                    for m in _pe_matches:
                        if score >= m.get("min_base_score", 0):
                            score += m["score_boost"]
                            signals.append(f"Pattern match: {m['name']} (+{m['score_boost']:.1f})")
            finally:
                _pe_db.close()
        except Exception:
            pass

        score = max(1.0, min(10.0, score))
        _bo_watch = get_adaptive_weight("bo_signal_watch")
        if dist_to_breakout <= 0:
            status = "breaking_out"
        elif score >= _bo_ready:
            status = "ready"
        elif score >= _bo_watch:
            status = "watch"
        else:
            status = "wait"

        atr_f = float(atr_val) if pd.notna(atr_val) else price * 0.02
        _cr = ticker.upper().endswith("-USD")
        _bo_stop_m = get_adaptive_weight("bo_stop_atr_mult")
        _bo_tgt_m = get_adaptive_weight("bo_target_atr_mult")

        result = {
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
            "stop_loss": smart_round(resistance - _bo_stop_m * atr_f, crypto=_cr),
            "take_profit": smart_round(resistance + _bo_tgt_m * atr_f, crypto=_cr),
            "indicators": {
                "rsi": round(float(rsi_val), 1) if pd.notna(rsi_val) else None,
                "macd_hist": round(float(macd_hist), 4) if pd.notna(macd_hist) else None,
                "adx": round(float(adx_val), 1) if pd.notna(adx_val) else None,
                "atr": round(atr_f, 6 if _cr else 4),
                "ema_20": smart_round(float(ema_20), crypto=_cr) if pd.notna(ema_20) else None,
                "ema_50": smart_round(float(ema_50), crypto=_cr) if pd.notna(ema_50) else None,
                "ema_100": smart_round(float(ema_100), crypto=_cr) if pd.notna(ema_100) else None,
                "bb_width_pctile": round(bb_width_pct_rank, 0),
            },
            "news_sentiment": _news_score,
            "sector": _get_sector_for_ticker(ticker),
            "hold_estimate": _estimate_hold_duration(
                resistance, smart_round(resistance + _bo_tgt_m * atr_f, crypto=_cr),
                atr_f, "1d",
                float(adx_val) if pd.notna(adx_val) else None,
            ),
        }
        with _bo_score_lock:
            _bo_score_cache[key] = (time.time(), result)
        return result
    except Exception as exc:
        logger.debug(f"[scanner] _score_breakout({ticker}) failed: {exc}")
        with _bo_score_lock:
            _bo_score_cache[key] = (time.time(), None)
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


# ── Quick first-pass filter using batch quotes ────────────────────────

_intraday_scan_progress: dict[str, Any] = {
    "running": False,
    "scan_type": "",
    "total_sourced": 0,
    "passed_filter": 0,
    "scored_so_far": 0,
    "started_at": 0.0,
}
_intraday_progress_lock = threading.Lock()

_MAX_HEAVY_CANDIDATES = 400


def get_intraday_scan_progress() -> dict[str, Any]:
    with _intraday_progress_lock:
        snap = dict(_intraday_scan_progress)
    if snap["started_at"]:
        snap["elapsed_s"] = round(time.time() - snap["started_at"], 1)
    return snap


def _quick_filter_candidates(
    tickers: list[str],
    *,
    min_price: float = 1.0,
    max_price: float = 500.0,
    min_abs_change_pct: float = 0.0,
    max_heavy: int = _MAX_HEAVY_CANDIDATES,
) -> list[str]:
    """Use Massive/Polygon batch quotes to cheaply rank and trim the full
    candidate universe before heavy intraday scoring.

    Returns at most *max_heavy* tickers, ranked by a simple activity
    score = volume * abs(change_pct).
    """
    quotes = fetch_quotes_batch(tickers)
    if not quotes:
        logger.warning("[scanner] Quick filter: no batch quotes returned, using all candidates")
        return tickers[:max_heavy]

    scored: list[tuple[str, float]] = []
    for t in tickers:
        q = quotes.get(t) or quotes.get(t.upper())
        if not q:
            continue
        price = q.get("price") or 0
        if price < min_price or price > max_price:
            continue
        change = abs(q.get("change_pct") or 0)
        if change < min_abs_change_pct:
            continue
        vol = q.get("volume") or q.get("avg_volume") or 0
        activity = vol * change if change else vol
        scored.append((t, activity))

    scored.sort(key=lambda x: x[1], reverse=True)
    filtered = [t for t, _ in scored[:max_heavy]]

    for t in tickers:
        if t not in set(filtered) and t not in quotes:
            filtered.append(t)
        if len(filtered) >= max_heavy:
            break

    logger.info(
        f"[scanner] Quick filter: {len(tickers)} -> {len(filtered)} "
        f"(quotes obtained for {len(quotes)})"
    )
    return filtered


# ── Batch runners for day-trade / breakout scans ──────────────────────

_daytrade_cache: dict[str, Any] = {"results": [], "ts": 0.0}
_DAYTRADE_CACHE_TTL = 600  # 10 min
_daytrade_scan_running = False

_breakout_cache: dict[str, Any] = {"results": [], "ts": 0.0}
_BREAKOUT_CACHE_TTL = 600  # 10 min
_breakout_scan_running = False


def get_daytrade_cache() -> dict[str, Any]:
    """Return cached daytrade scan results (for API layer)."""
    now = time.time()
    age = int(now - _daytrade_cache["ts"]) if _daytrade_cache["ts"] > 0 else None
    return {
        "results": _daytrade_cache.get("results", []),
        "scan_time": _daytrade_cache.get("scan_time"),
        "age_seconds": age,
        "total_scanned": _daytrade_cache.get("candidates_scanned", 0),
    }


def get_breakout_cache() -> dict[str, Any]:
    """Return merged stock + crypto breakout results (for API layer)."""
    now = time.time()
    stock_age = int(now - _breakout_cache["ts"]) if _breakout_cache["ts"] > 0 else None
    crypto_age = int(now - _crypto_breakout_cache["ts"]) if _crypto_breakout_cache["ts"] > 0 else None
    age = min(a for a in (stock_age, crypto_age) if a is not None) if (stock_age is not None or crypto_age is not None) else None

    merged = list(_breakout_cache.get("results", []))
    crypto_results = _crypto_breakout_cache.get("results", [])
    if crypto_results:
        existing_tickers = {r["ticker"] for r in merged}
        for cr in crypto_results:
            if cr["ticker"] not in existing_tickers:
                merged.append(cr)
        merged.sort(key=lambda r: r.get("score", 0), reverse=True)

    return {
        "results": merged,
        "scan_time": _breakout_cache.get("scan_time"),
        "age_seconds": age,
        "total_scanned": (
            _breakout_cache.get("candidates_scanned", 0)
            + _crypto_breakout_cache.get("total_scanned", 0)
        ),
    }


def run_daytrade_scan(max_results: int = 30) -> dict[str, Any]:
    """Full-universe day-trade scan: prescreen -> batch-quote filter -> intraday scoring."""
    global _daytrade_cache, _daytrade_scan_running

    now = time.time()
    if _daytrade_cache["ts"] > 0 and (now - _daytrade_cache["ts"]) < _DAYTRADE_CACHE_TTL:
        return {
            "ok": True,
            "scan_type": "day_trade",
            "cached": True,
            "candidates_scanned": _daytrade_cache.get("candidates_scanned", 0),
            "total_sourced": _daytrade_cache.get("total_sourced", 0),
            "matches": len(_daytrade_cache["results"]),
            "results": _daytrade_cache["results"][:max_results],
            "brain": _brain_meta(),
        }

    if _daytrade_scan_running:
        return {"ok": True, "scan_type": "day_trade", "warming_up": True, "matches": 0, "results": []}
    _daytrade_scan_running = True

    from .prescreener import get_daytrade_candidates

    start = time.time()
    with _intraday_progress_lock:
        _intraday_scan_progress.update(
            running=True, scan_type="day_trade", scored_so_far=0,
            total_sourced=0, passed_filter=0, started_at=start,
        )

    all_candidates, total_sourced = get_daytrade_candidates()
    with _intraday_progress_lock:
        _intraday_scan_progress["total_sourced"] = total_sourced

    candidates = _quick_filter_candidates(all_candidates, max_heavy=_MAX_HEAVY_CANDIDATES)
    with _intraday_progress_lock:
        _intraday_scan_progress["passed_filter"] = len(candidates)

    logger.info(f"[trading] Day-trade scan: {len(candidates)}/{total_sourced} candidates (after filter)")
    _prewarm_cache_intraday(candidates)

    results: list[dict[str, Any]] = []
    scored_count = 0
    with ThreadPoolExecutor(max_workers=_MAX_SCAN_WORKERS) as executor:
        futures = {executor.submit(_score_ticker_intraday, t): t for t in candidates}
        for future in as_completed(futures):
            if _shutting_down.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            scored_count += 1
            with _intraday_progress_lock:
                _intraday_scan_progress["scored_so_far"] = scored_count
            try:
                scored = future.result()
                if scored is not None:
                    results.append(scored)
            except Exception:
                pass

    results.sort(key=lambda r: r["score"], reverse=True)
    elapsed = round(time.time() - start, 1)

    _daytrade_cache.update(
        results=results, ts=time.time(),
        candidates_scanned=len(candidates),
        total_sourced=total_sourced,
        scan_time=elapsed,
    )
    _daytrade_scan_running = False
    logger.info(f"[trading] Day-trade cache filled: {len(results)} results in {elapsed}s")

    with _intraday_progress_lock:
        _intraday_scan_progress.update(running=False, scan_type="")

    return {
        "ok": True,
        "scan_type": "day_trade",
        "candidates_scanned": len(candidates),
        "total_sourced": total_sourced,
        "matches": len(results[:max_results]),
        "elapsed_s": elapsed,
        "results": results[:max_results],
        "brain": _brain_meta(),
    }


def run_breakout_scan(max_results: int = 30) -> dict[str, Any]:
    """Full-universe breakout scan: prescreen -> batch-quote filter -> breakout scoring."""
    global _breakout_cache, _breakout_scan_running

    now = time.time()
    if _breakout_cache["ts"] > 0 and (now - _breakout_cache["ts"]) < _BREAKOUT_CACHE_TTL:
        return {
            "ok": True,
            "scan_type": "breakout",
            "cached": True,
            "candidates_scanned": _breakout_cache.get("candidates_scanned", 0),
            "total_sourced": _breakout_cache.get("total_sourced", 0),
            "matches": len(_breakout_cache["results"][:max_results]),
            "results": _breakout_cache["results"][:max_results],
            "brain": _brain_meta(),
        }

    if _breakout_scan_running:
        return {"ok": True, "scan_type": "breakout", "warming_up": True, "matches": 0, "results": []}
    _breakout_scan_running = True

    from .prescreener import get_breakout_candidates

    start = time.time()
    with _intraday_progress_lock:
        _intraday_scan_progress.update(
            running=True, scan_type="breakout", scored_so_far=0,
            total_sourced=0, passed_filter=0, started_at=start,
        )

    try:
        all_candidates, total_sourced = get_breakout_candidates()

        stock_candidates = [t for t in all_candidates if not t.upper().endswith("-USD")]
        with _intraday_progress_lock:
            _intraday_scan_progress["total_sourced"] = len(stock_candidates)

        candidates = _quick_filter_candidates(
            stock_candidates, max_heavy=_MAX_HEAVY_CANDIDATES,
            min_abs_change_pct=0.0, max_price=1000.0,
        )
        with _intraday_progress_lock:
            _intraday_scan_progress["passed_filter"] = len(candidates)

        logger.info(f"[trading] Breakout scan: {len(candidates)}/{len(stock_candidates)} candidates (after filter)")
        _prewarm_cache(candidates)

        results: list[dict[str, Any]] = []
        scored_count = 0
        with ThreadPoolExecutor(max_workers=_MAX_SCAN_WORKERS) as executor:
            futures = {executor.submit(_score_breakout, t): t for t in candidates}
            for future in as_completed(futures):
                if _shutting_down.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                scored_count += 1
                with _intraday_progress_lock:
                    _intraday_scan_progress["scored_so_far"] = scored_count
                try:
                    scored = future.result()
                    if scored is not None:
                        results.append(scored)
                except Exception:
                    pass

        results.sort(key=lambda r: r["score"], reverse=True)
        elapsed = round(time.time() - start, 1)

        _breakout_cache.update(
            results=results, ts=time.time(),
            candidates_scanned=len(candidates),
            total_sourced=len(stock_candidates),
            scan_time=elapsed,
        )
        logger.info(f"[trading] Breakout cache filled: {len(results)} results in {elapsed}s")
    except Exception as exc:
        logger.exception(f"[trading] Breakout scan failed: {exc}")
    finally:
        _breakout_scan_running = False
        with _intraday_progress_lock:
            _intraday_scan_progress.update(running=False, scan_type="")

    return {
        "ok": True,
        "scan_type": "breakout",
        "candidates_scanned": len(candidates),
        "total_sourced": total_sourced,
        "matches": len(results[:max_results]),
        "elapsed_s": elapsed,
        "results": results[:max_results],
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
            "candidates_scanned": _momentum_cache.get("candidates_scanned", 0),
            "total_sourced": _momentum_cache.get("total_sourced", 0),
            "matches": len(_momentum_cache["results"]),
            "results": _momentum_cache["results"],
            "brain": _brain_meta(),
        }

    from .prescreener import get_daytrade_candidates

    start = time.time()
    with _intraday_progress_lock:
        _intraday_scan_progress.update(
            running=True, scan_type="momentum", scored_so_far=0,
            total_sourced=0, passed_filter=0, started_at=start,
        )

    all_candidates, total_sourced = get_daytrade_candidates()
    with _intraday_progress_lock:
        _intraday_scan_progress["total_sourced"] = total_sourced

    candidates = _quick_filter_candidates(all_candidates, max_heavy=_MAX_HEAVY_CANDIDATES)
    with _intraday_progress_lock:
        _intraday_scan_progress["passed_filter"] = len(candidates)

    logger.info(f"[trading] Momentum scanner: {len(candidates)}/{total_sourced} candidates (after filter)")
    _prewarm_cache_intraday(candidates)

    scored: list[dict[str, Any]] = []
    scored_count = 0
    with ThreadPoolExecutor(max_workers=_MAX_SCAN_WORKERS) as executor:
        futures = {executor.submit(_score_ticker_intraday, t): t for t in candidates}
        for future in as_completed(futures):
            if _shutting_down.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            scored_count += 1
            with _intraday_progress_lock:
                _intraday_scan_progress["scored_so_far"] = scored_count
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

    _momentum_cache = {"results": results, "ts": time.time(), "total_sourced": total_sourced, "candidates_scanned": len(candidates)}
    elapsed = round(time.time() - start, 1)

    with _intraday_progress_lock:
        _intraday_scan_progress.update(running=False, scan_type="")

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


def _try_load_cached_scores(db: "Session | None") -> list[dict[str, Any]]:
    """Attempt to load recent scored results from the DB or in-memory caches.

    Returns a list of scored dicts if fresh data is available, else empty list.
    """
    results: list[dict[str, Any]] = []

    if _breakout_cache["results"] and (time.time() - _breakout_cache["ts"]) < _BREAKOUT_CACHE_TTL:
        results.extend(_breakout_cache["results"])
    if _daytrade_cache["results"] and (time.time() - _daytrade_cache["ts"]) < _DAYTRADE_CACHE_TTL:
        results.extend(_daytrade_cache["results"])

    if db is not None:
        try:
            from ...models.trading import ScanResult
            cutoff = datetime.utcnow() - timedelta(hours=2)
            rows = db.query(ScanResult).filter(
                ScanResult.scanned_at >= cutoff,
            ).order_by(ScanResult.score.desc()).limit(500).all()
            seen = {r.get("ticker") for r in results}
            for r in rows:
                if r.ticker in seen:
                    continue
                seen.add(r.ticker)
                results.append({
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
                })
        except Exception as e:
            logger.debug(f"[trading] Could not load cached scores from DB: {e}")

    return results


def run_custom_screen(
    screen_id: str | None = None,
    conditions: list[dict] | None = None,
    tickers: list[str] | None = None,
    db: "Session | None" = None,
) -> dict[str, Any]:
    """Run a preset or custom screen against the pre-filtered candidate pool.

    When recent ScanResult rows exist in the DB (from the learning cycle),
    we evaluate conditions against those cached scores instead of re-scoring
    the entire universe from scratch.
    """
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

    scored_all = _try_load_cached_scores(db)
    if not scored_all:
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
    """Pre-warm OHLCV cache for all providers.

    Uses ``fetch_ohlcv_batch`` which dispatches through Massive's concurrent
    pool (saturating rate limit), Polygon, or yfinance batch_download.
    """
    if not tickers:
        return
    t0 = time.time()
    try:
        fetch_ohlcv_batch(tickers, interval="1d", period="6mo")
    except Exception as e:
        logger.warning(f"[scanner] prewarm_cache failed: {e}")
    elapsed = time.time() - t0
    logger.info(f"[scanner] Pre-warmed {len(tickers)} tickers in {elapsed:.1f}s")


def _prewarm_cache_intraday(tickers: list[str]) -> None:
    """Pre-warm 5d/15m intraday cache for all providers."""
    if not tickers:
        return
    t0 = time.time()
    try:
        fetch_ohlcv_batch(tickers, interval="15m", period="5d")
    except Exception as e:
        logger.warning(f"[scanner] prewarm_cache_intraday failed: {e}")
    elapsed = time.time() - t0
    logger.info(f"[scanner] Pre-warmed intraday {len(tickers)} tickers in {elapsed:.1f}s")


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


def get_top_picks_freshness(stale_threshold_seconds: float = 600) -> dict[str, Any]:
    """Return batch-level freshness metadata for the cached top picks.

    Used by the API to expose as_of, age_seconds, and is_stale.
    """
    global _top_picks_cache
    ts = _top_picks_cache.get("ts") or 0.0
    now = time.time()
    age = now - ts
    as_of_dt = datetime.utcfromtimestamp(ts) if ts else datetime.utcnow()
    return {
        "as_of": as_of_dt.isoformat() + "Z",
        "age_seconds": round(age),
        "is_stale": age > stale_threshold_seconds,
    }


def recheck_pick(
    ticker: str,
    entry_price: float,
    *,
    drift_ok_pct: float = 10.0,
    drift_invalidate_pct: float = 15.0,
) -> dict[str, Any]:
    """Re-validate a single pick with live price. Fast, no full rescan.

    Returns live price, drift_pct, and status: valid, moved_but_ok, or invalidated.
    """
    from .market_data import fetch_quote

    quote = fetch_quote(ticker)
    live_price = quote.get("price") if quote else None
    if not live_price or live_price <= 0:
        return {
            "ok": True,
            "ticker": ticker,
            "live_price": None,
            "entry_price": entry_price,
            "drift_pct": None,
            "status": "unavailable",
            "message": "Could not fetch current price.",
        }

    drift_pct = abs(live_price - entry_price) / entry_price * 100 if entry_price else 0

    if drift_pct <= drift_ok_pct:
        status = "valid"
    elif drift_pct <= drift_invalidate_pct:
        status = "moved_but_ok"
    else:
        status = "invalidated"

    return {
        "ok": True,
        "ticker": ticker,
        "live_price": live_price,
        "entry_price": entry_price,
        "drift_pct": round(drift_pct, 2),
        "status": status,
    }


from .thesis import (
    _SIGNAL_TRANSLATIONS,
    build_conversational_thesis as _build_conversational_thesis,
    make_plain_english as _make_plain_english,
    build_smart_pick_context_strings as _build_smart_pick_context_strings,
)


# _build_conversational_thesis -> imported from .thesis


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
            "scanned_at": r.scanned_at.isoformat() if r.scanned_at else None,
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
                candidates[t]["ml_probability"] = p.get("meta_ml_probability")
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
                    "scanned_at": None,  # ML-only pick; no scan timestamp
                    "is_crypto": _cr,
                    "brain_score": p["score"],
                    "brain_confidence": p["confidence"],
                    "brain_direction": p["direction"],
                    "ml_probability": p.get("meta_ml_probability"),
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

        pick["thesis"] = _build_conversational_thesis(pick)

        # Timeframe suggestion with hold duration and trade type classification
        _ind = pick.get("indicators", {})
        _p_entry = pick.get("entry_price") or pick.get("price", 0)
        _p_target = pick.get("take_profit") or pick.get("brain_target", 0)
        _p_atr = _ind.get("atr", 0)
        _is_crypto = pick.get("ticker", "").endswith("-USD")
        _he = {}
        if _p_entry and _p_target and _p_atr and _p_target > _p_entry:
            _he = _estimate_hold_duration(
                _p_entry, _p_target, _p_atr,
                "15m" if _is_crypto else "1d",
                _ind.get("adx"),
            )
            pick["hold_estimate"] = _he

        _tc = classify_trade_type(
            pick.get("signals", []), _he, _ind,
            is_crypto=_is_crypto,
        )
        pick["trade_type"] = _tc["type"]
        pick["trade_type_label"] = _tc["label"]
        pick["duration_estimate"] = _tc["duration"]
        if _tc["duration"]:
            pick["timeframe"] = f"{_tc['label']} ({_tc['duration']})"
        else:
            pick["timeframe"] = _tc["label"]

    # Filter out picks whose price has drifted >25% from the scored price
    # (stale scan results from DB where the stock has since crashed/spiked)
    _pick_tickers_for_check = [p["ticker"] for p in picks if p.get("price")]
    _live_quotes = {}
    try:
        _live_quotes = fetch_quotes_batch(_pick_tickers_for_check) if _pick_tickers_for_check else {}
    except Exception:
        pass

    validated = []
    for pick in picks:
        scored_price = pick.get("price") or 0
        if scored_price > 0 and _live_quotes:
            lq = _live_quotes.get(pick["ticker"])
            live_price = lq.get("price", 0) if lq else 0
            if live_price and live_price > 0:
                drift = abs(live_price - scored_price) / scored_price * 100
                if drift > 25:
                    logger.debug(
                        f"[scanner] Dropping stale pick {pick['ticker']}: "
                        f"scored at ${scored_price} but now ${live_price} ({drift:.0f}% drift)"
                    )
                    continue
                pick["price"] = live_price
                pick["entry_price"] = live_price
        validated.append(pick)

    validated.sort(key=lambda x: x.get("combined_score", 0), reverse=True)

    top = validated[:15]

    for i, pick in enumerate(top):
        pick["rank"] = i + 1

    return top


# _make_plain_english -> imported from .thesis


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

    # Pre-warm Smart Pick context for this user in the background so the
    # \"What Should I Buy?\" button feels instant after a full scan.
    try:
        threading.Thread(
            target=_bg_refresh_smart_pick_context,
            args=(user_id, None, "medium"),
            daemon=True,
        ).start()
    except Exception:
        logger.debug("[scanner] failed to start smart_pick_context pre-warm thread", exc_info=True)
    return results


# ── Smart Pick ────────────────────────────────────────────────────────


def _smart_pick_ctx_key(user_id: int | None, risk_tolerance: str) -> tuple[int | None, str]:
    return user_id, (risk_tolerance or "medium").lower()


def _bg_refresh_smart_pick_context(
    user_id: int | None, budget: float | None, risk_tolerance: str,
) -> None:
    """Background refresh for the Smart Pick context cache."""
    try:
        # Local import to avoid circulars at import time
        from ...db import SessionLocal

        db = SessionLocal()
        try:
            smart_pick_context(db, user_id, budget=budget, risk_tolerance=risk_tolerance, force_fresh=True)
        finally:
            db.close()
    except Exception:
        # Background refresh failures should never break foreground requests
        logger.exception("[scanner] smart_pick_context background refresh failed")


def smart_pick_context(
    db: Session,
    user_id: int | None,
    *,
    budget: float | None = None,
    risk_tolerance: str = "medium",
    force_fresh: bool = False,
) -> dict[str, Any]:
    """Build (and cache) the expensive Smart Pick scan/context data.

    This performs DB scans, optional full-universe scoring, watchlist scoring,
    and user/brain context building — but does *not* call the LLM.
    """
    from ...models.trading import ScanResult
    from sqlalchemy import or_
    from ..ticker_universe import get_full_ticker_universe, get_ticker_count

    key = _smart_pick_ctx_key(user_id, risk_tolerance)
    now = time.time()

    if not force_fresh:
        with _smart_pick_ctx_lock:
            cached = _smart_pick_ctx_cache.get(key)
        if cached:
            age = now - cached["ts"]
            if age < _SMART_PICK_CTX_TTL:
                return cached["ctx"]
            if age < _SMART_PICK_CTX_STALE_TTL:
                # Serve stale context but refresh in the background
                threading.Thread(
                    target=_bg_refresh_smart_pick_context,
                    args=(user_id, budget, risk_tolerance),
                    daemon=True,
                ).start()
                return cached["ctx"]

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

    top_picks = scored_results[:12]

    # User / learning context (safe to cache; prices will be refreshed later)
    stats = get_trade_stats(db, user_id)
    insights = get_insights(db, user_id, limit=10)

    portfolio_ctx: str | None = None
    try:
        from .. import broker_service

        portfolio_ctx = broker_service.build_portfolio_context()
    except Exception:
        portfolio_ctx = None

    ctx = {
        "top_picks": top_picks,
        "total_scanned": total_scanned,
        "picks_qualified": len(scored_results),
        "risk_tolerance": risk_tolerance,
        "budget": budget,
        "stats": stats,
        "insights": insights,
        "portfolio_ctx": portfolio_ctx,
    }

    with _smart_pick_ctx_lock:
        _smart_pick_ctx_cache[key] = {"ctx": ctx, "ts": time.time()}

    return ctx


# _build_smart_pick_context_strings -> imported from .thesis


def _validate_live_prices(
    picks: list[dict[str, Any]], *, drift_threshold_pct: float = 5.0
) -> list[dict[str, Any]]:
    """Refresh prices for picks and drop those whose price has drifted too far.

    This ensures Smart Pick recommendations use live prices while still
    benefiting from cached scan results and indicator data.
    """
    tickers = [p["ticker"] for p in picks if p.get("price")]
    if not tickers:
        return picks

    try:
        live_quotes = fetch_quotes_batch(tickers)
    except Exception:
        live_quotes = {}

    validated: list[dict[str, Any]] = []
    for pick in picks:
        scored_price = pick.get("price") or 0
        if scored_price > 0 and live_quotes:
            lq = live_quotes.get(pick["ticker"])
            live_price = lq.get("price", 0) if lq else 0
            if live_price and live_price > 0:
                drift = abs(live_price - scored_price) / scored_price * 100
                if drift > drift_threshold_pct:
                    logger.debug(
                        f"[scanner] Dropping stale smart-pick {pick['ticker']}: "
                        f"scored at ${scored_price} but now ${live_price} ({drift:.0f}% drift)"
                    )
                    continue
                # Use live price for both current and entry in the context
                pick["price"] = live_price
                pick["entry_price"] = live_price
        validated.append(pick)

    return validated


def smart_pick(
    db: Session, user_id: int | None,
    message: str | None = None,
    budget: float | None = None,
    risk_tolerance: str = "medium",
) -> dict[str, Any]:
    """Scan the market (using cached context where possible) and deep-analyze the top picks."""
    ctx = smart_pick_context(db, user_id, budget=budget, risk_tolerance=risk_tolerance)

    # Work on a shallow copy of the top picks so we don't mutate cached dicts in-place
    raw_top_picks: list[dict[str, Any]] = [dict(p) for p in ctx.get("top_picks", [])]
    total_scanned: int = ctx.get("total_scanned", 0)

    if not raw_top_picks:
        return {
            "ok": True,
            "reply": f"I scanned {total_scanned:,} stocks and crypto and none have a strong enough setup right now. "
                     "The best trade is sometimes no trade. I'll keep watching and flag opportunities as they appear.",
            "picks": [],
        }

    top_picks = _validate_live_prices(raw_top_picks, drift_threshold_pct=5.0)
    if not top_picks:
        return {
            "ok": True,
            "reply": f"I scanned {total_scanned:,} stocks and crypto and all previously-good setups have moved too far from their ideal entries. "
                     "Right now it's safer to wait for new clean setups. I'll keep scanning and surface fresh trades as they appear.",
            "picks": [],
        }

    ctx["top_picks"] = top_picks
    ctx["picks_qualified"] = len(top_picks)

    full_context = _build_smart_pick_context_strings(db, ctx)

    user_msg = message or (
        "Based on this scan, what are your top 10 stock picks I should buy RIGHT NOW? "
        "For each one, give me the exact buy-in price, sell target, stop-loss, expected hold duration, "
        "position size, and your confidence level. Rank them by conviction."
    )

    from ...prompts import load_prompt

    system_prompt = load_prompt("trading_analyst")

    ticker_names = ", ".join(p["ticker"] for p in top_picks)

    smart_pick_addendum = f"""

SPECIAL INSTRUCTION — SMART PICK MODE:
You scanned {total_scanned:,} stocks and crypto. The TOP candidates are: {ticker_names}
Their full indicator data and scores are in the MARKET SCAN RESULTS section below.

ABSOLUTE RULES (NEVER VIOLATE):
- You MUST list the top picks immediately. Do NOT ask the user to choose a universe, narrow down, or pick a letter. The scan is ALREADY DONE — your ONLY job is to rank and present the results.
- You MUST reference tickers BY NAME (e.g. "AAPL", "BTC-USD", "NVDA") — NEVER give a generic recommendation without naming specific tickers.
- Use the ACTUAL prices and indicator values from the data provided — do NOT make up numbers.
- If the user asked about crypto specifically, prioritize crypto tickers from the scan.
- If the user asked about stocks specifically, prioritize stock tickers.
- Do NOT refuse to list picks. If some candidates are weaker, still list them with appropriate caveats and lower confidence — the user wants a ranked list, not a refusal.

Your job: Rank and present UP TO 10 trades from this scan as a clear, specific action plan. If fewer than 10 candidates have viable setups, list only those that do — but you MUST list at least the top candidates provided.

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

End with portfolio allocation advice and any general market context warnings.
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
            max_tokens=4096,
        )
        reply = result.get("reply", "Could not generate recommendation.")
    except Exception as e:
        reply = f"Analysis unavailable: {e}"

    return {
        "ok": True,
        "reply": reply,
        "picks_scanned": total_scanned,
        "picks_qualified": ctx.get("picks_qualified", len(top_picks)),
        "top_picks": [
            {"ticker": p["ticker"], "score": p["score"], "signal": p["signal"], "price": p["price"]}
            for p in top_picks
        ],
    }

