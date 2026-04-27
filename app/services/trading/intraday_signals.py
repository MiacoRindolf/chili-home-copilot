"""Intraday signal pipeline for the trading brain.

Scans for real-time setups across crypto and stocks:
- Pre-market gap detection
- Opening Range Breakout (ORB)
- Crypto breakout signals
- Momentum continuation after pullback

Signals are scored, filtered, and optionally routed to paper trading
or alert dispatch.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def scan_premarket_gaps(
    tickers: list[str] | None = None,
    min_gap_pct: float = 3.0,
) -> list[dict[str, Any]]:
    """Detect pre-market gaps by comparing yesterday's close to current price."""
    from .market_data import fetch_quote, DEFAULT_SCAN_TICKERS

    if tickers is None:
        tickers = list(DEFAULT_SCAN_TICKERS)[:50]

    gaps = []
    for ticker in tickers:
        try:
            q = fetch_quote(ticker)
            if not q:
                continue
            price = q.get("price", 0)
            prev_close = q.get("previous_close") or q.get("regularMarketPreviousClose", 0)
            if not price or not prev_close or prev_close <= 0:
                continue
            gap_pct = (price - prev_close) / prev_close * 100
            if abs(gap_pct) >= min_gap_pct:
                gaps.append({
                    "ticker": ticker,
                    "price": round(price, 2),
                    "prev_close": round(prev_close, 2),
                    "gap_pct": round(gap_pct, 2),
                    "direction": "up" if gap_pct > 0 else "down",
                    "signal_type": "premarket_gap",
                })
        except Exception:
            continue

    gaps.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)
    return gaps[:15]


def scan_opening_range_breakout(
    tickers: list[str] | None = None,
    orb_minutes: int = 30,
) -> list[dict[str, Any]]:
    """Detect Opening Range Breakouts — price breaking above/below the first N minutes' range."""
    from .market_data import fetch_ohlcv_df, DEFAULT_SCAN_TICKERS

    if tickers is None:
        tickers = list(DEFAULT_SCAN_TICKERS)[:30]

    signals = []
    for ticker in tickers:
        try:
            df = fetch_ohlcv_df(ticker, period="2d", interval="5m")
            if df.empty or len(df) < 12:
                continue

            today = df.index[-1].date() if hasattr(df.index[-1], "date") else None
            if today is None:
                continue

            today_bars = df[df.index.date == today] if hasattr(df.index, "date") else df.tail(78)
            if len(today_bars) < 6:
                continue

            orb_bars = orb_minutes // 5
            opening_range = today_bars.head(orb_bars)
            orb_high = float(opening_range["High"].max())
            orb_low = float(opening_range["Low"].min())
            current = float(today_bars["Close"].iloc[-1])
            volume = float(today_bars["Volume"].iloc[-1])

            if current > orb_high:
                signals.append({
                    "ticker": ticker,
                    "price": round(current, 2),
                    "orb_high": round(orb_high, 2),
                    "orb_low": round(orb_low, 2),
                    "direction": "long",
                    "breakout_pct": round((current - orb_high) / orb_high * 100, 2),
                    "signal_type": "orb_breakout",
                    "stop_price": round(orb_low, 2),
                    "target_price": round(current + (orb_high - orb_low), 2),
                })
            elif current < orb_low:
                signals.append({
                    "ticker": ticker,
                    "price": round(current, 2),
                    "orb_high": round(orb_high, 2),
                    "orb_low": round(orb_low, 2),
                    "direction": "short",
                    "breakout_pct": round((orb_low - current) / orb_low * 100, 2),
                    "signal_type": "orb_breakdown",
                    "stop_price": round(orb_high, 2),
                    "target_price": round(current - (orb_high - orb_low), 2),
                })
        except Exception:
            continue

    signals.sort(key=lambda x: abs(x.get("breakout_pct", 0)), reverse=True)
    return signals[:10]


def _resolve_momentum_rvol_min(db: Optional[Session]) -> float:
    """Q2 Task J — adaptive rvol_min for momentum_continuation gate.

    Default 0.8 (current behavior). Bounds [0.3, 5.0] keep the learner
    from pushing the gate into nonsense territory: below 0.3 we're
    accepting setups with no relative-volume confirmation; above 5.0 we
    need a five-bagger volume spike to trade, which would silence the
    scanner entirely.
    """
    default = 0.8
    if db is None:
        return default
    try:
        from .strategy_parameter import (
            ParameterSpec, get_parameter, register_parameter,
        )
        register_parameter(
            db,
            ParameterSpec(
                strategy_family="momentum_continuation",
                parameter_key="rvol_min",
                initial_value=default,
                min_value=0.3,
                max_value=5.0,
                description=(
                    "Minimum relative volume to allow a momentum_continuation "
                    "signal. Pull-back-to-EMA setups without a volume "
                    "confirmation tend to fade; the learner adapts this "
                    "from realized 15m signal outcomes."
                ),
            ),
        )
        v = get_parameter(
            db, "momentum_continuation", "rvol_min", default=default,
        )
        if v is None:
            return default
        return float(max(0.3, min(5.0, v)))
    except Exception:
        return default


def scan_momentum_continuation(
    tickers: list[str] | None = None,
    *,
    db: Optional[Session] = None,
) -> list[dict[str, Any]]:
    """Find stocks/crypto in strong intraday momentum pulling back to EMA support.

    Uses 15-minute bars over the last 5 days so the scan reflects actual
    intraday structure rather than daily candles.
    """
    from .market_data import fetch_ohlcv_df, DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS

    if tickers is None:
        tickers = list(DEFAULT_SCAN_TICKERS)[:30] + list(DEFAULT_CRYPTO_TICKERS)[:10]

    rvol_min = _resolve_momentum_rvol_min(db)

    signals = []
    for ticker in tickers:
        try:
            df = fetch_ohlcv_df(ticker, period="5d", interval="15m")
            if df.empty or len(df) < 40:
                continue

            close = df["Close"].astype(float)
            volume = df["Volume"].astype(float)
            ema9 = close.ewm(span=9, adjust=False).mean()
            ema21 = close.ewm(span=21, adjust=False).mean()

            last_close = float(close.iloc[-1])
            last_ema9 = float(ema9.iloc[-1])
            last_ema21 = float(ema21.iloc[-1])

            if last_ema9 > last_ema21:
                pullback_to_ema9 = abs(last_close - last_ema9) / last_close * 100
                if pullback_to_ema9 < 1.0 and last_close > last_ema21:
                    # Recent 20-bar momentum (5 hours on 15m)
                    bars_back = min(20, len(close) - 1)
                    mom_pct = (last_close - float(close.iloc[-bars_back])) / float(close.iloc[-bars_back]) * 100

                    # Volume confirmation: recent volume vs prior average
                    recent_vol = float(volume.iloc[-10:].mean()) if len(volume) >= 10 else 0
                    prior_vol = float(volume.iloc[-40:-10].mean()) if len(volume) >= 40 else recent_vol
                    rvol = round(recent_vol / prior_vol, 2) if prior_vol > 0 else 1.0

                    if mom_pct > 1.0 and rvol >= rvol_min:
                        signals.append({
                            "ticker": ticker,
                            "price": round(last_close, 4 if ticker.endswith("-USD") else 2),
                            "ema9": round(last_ema9, 4 if ticker.endswith("-USD") else 2),
                            "ema21": round(last_ema21, 4 if ticker.endswith("-USD") else 2),
                            "pullback_pct": round(pullback_to_ema9, 2),
                            "momentum_pct": round(mom_pct, 2),
                            "rvol": rvol,
                            "rvol_min_gate": rvol_min,
                            "direction": "long",
                            "signal_type": "momentum_continuation",
                            "timeframe": "15m",
                            "stop_price": round(last_ema21 * 0.995, 4 if ticker.endswith("-USD") else 2),
                            "target_price": round(last_close * 1.03, 4 if ticker.endswith("-USD") else 2),
                        })
        except Exception:
            continue

    signals.sort(key=lambda x: x.get("momentum_pct", 0), reverse=True)
    return signals[:10]


def run_intraday_signal_sweep(
    db: Session,
    user_id: int | None = None,
    *,
    auto_paper: bool = False,
) -> dict[str, Any]:
    """Run all intraday signal scanners and optionally paper-trade the best ones."""
    results: dict[str, Any] = {"timestamp": datetime.utcnow().isoformat() + "Z"}

    try:
        gaps = scan_premarket_gaps()
        results["premarket_gaps"] = gaps
    except Exception as e:
        logger.warning("[intraday] Gap scan failed: %s", e)
        results["premarket_gaps"] = []

    try:
        orbs = scan_opening_range_breakout()
        results["orb_signals"] = orbs
    except Exception as e:
        logger.warning("[intraday] ORB scan failed: %s", e)
        results["orb_signals"] = []

    try:
        momentum = scan_momentum_continuation(db=db)
        results["momentum_signals"] = momentum
    except Exception as e:
        logger.warning("[intraday] Momentum scan failed: %s", e)
        results["momentum_signals"] = []

    all_signals = results.get("premarket_gaps", []) + results.get("orb_signals", []) + results.get("momentum_signals", [])
    results["total_signals"] = len(all_signals)

    if auto_paper and all_signals:
        try:
            from .paper_trading import auto_enter_from_signals
            entered = auto_enter_from_signals(db, user_id, all_signals[:5])
            results["paper_entered"] = entered
        except Exception as e:
            logger.warning("[intraday] Paper entry failed: %s", e)

    results["ok"] = True
    return results
