#!/usr/bin/env python3
"""Upsert historical MarketSnapshot rows from OHLCV + mined indicator rows.

Uses the same bar keys as the learning cycle (ticker, bar_interval, bar_start_utc).
Default period comes from settings ``brain_snapshot_backfill_years`` (via env).

- ``--incremental``: only bars strictly after max(bar_start_at) already in DB for that ticker/interval.
- Without ``--incremental``: upsert all bars returned by internal mining (can be heavy).

Usage:
  conda activate chili-env
  python scripts/backfill_snapshot_bars.py --tickers BTC-USD,ETH-USD --intervals 1d --dry-run
  python scripts/backfill_snapshot_bars.py --tickers BTC-USD --intervals 15m --incremental
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from sqlalchemy import func

    from app.config import settings
    from app.db import SessionLocal
    from app.models.trading import MarketSnapshot
    from app.services.trading.learning import compute_prediction, mine_row_to_indicator_payload
    from app.services.trading.snapshot_bar_ops import normalize_bar_start_utc, upsert_market_snapshot

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tickers",
        default="",
        help="Comma-separated tickers (default: small crypto subset)",
    )
    ap.add_argument(
        "--intervals",
        default="1d",
        help="Comma-separated intervals, e.g. 1d,15m",
    )
    ap.add_argument(
        "--years",
        type=int,
        default=0,
        help="Override backfill years (0 = use brain_snapshot_backfill_years)",
    )
    ap.add_argument("--incremental", action="store_true", help="Only new bars vs DB max bar_start_at")
    ap.add_argument("--dry-run", action="store_true", help="Count only, no commit")
    args = ap.parse_args()

    years = args.years or int(settings.brain_snapshot_backfill_years)
    if args.tickers.strip():
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = ["BTC-USD", "ETH-USD", "SOL-USD"]

    intervals = [x.strip() for x in args.intervals.split(",") if x.strip()]

    db = SessionLocal()
    total_upserts = 0
    try:
        for iv in intervals:
            period = f"{years}y" if years > 0 else ("6mo" if iv == "1d" else "90d")
            for ticker in tickers:
                # Re-fetch with explicit period override by temporarily patching would need learning change;
                # _mine_from_history uses fixed period — for long backfill call fetch inside loop:
                rows = _mine_with_period(ticker, iv, period)
                if args.incremental:
                    mx = (
                        db.query(func.max(MarketSnapshot.bar_start_at))
                        .filter(
                            MarketSnapshot.ticker == ticker,
                            MarketSnapshot.bar_interval == iv,
                        )
                        .scalar()
                    )
                    if mx is not None:
                        mxn = normalize_bar_start_utc(mx)
                        rows = [r for r in rows if normalize_bar_start_utc(r["bar_start_utc"]) > mxn]

                for r in rows:
                    payload = mine_row_to_indicator_payload(r)
                    ind_json = json.dumps(payload)
                    pred = compute_prediction(payload)
                    if args.dry_run:
                        total_upserts += 1
                        continue
                    upsert_market_snapshot(
                        db,
                        ticker=r["ticker"],
                        bar_interval=r["bar_interval"],
                        bar_start_at=r["bar_start_utc"],
                        close_price=float(r["price"]),
                        indicator_data=ind_json,
                        predicted_score=pred,
                        vix_at_snapshot=None,
                        news_sentiment=None,
                        news_count=None,
                        pe_ratio=None,
                        market_cap_b=None,
                    )
                    total_upserts += 1
                if not args.dry_run and rows:
                    db.commit()
        if args.dry_run:
            print(f"Dry-run: would upsert {total_upserts} snapshot rows")
        else:
            print(f"Upserted {total_upserts} snapshot rows")
    finally:
        db.close()
    return 0


def _mine_with_period(ticker: str, bar_interval: str, period: str) -> list[dict]:
    """Like _mine_from_history but with an explicit yfinance period string."""
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange

    import pandas as pd

    from app.services.trading.learning import _get_historical_regime_map
    from app.services.trading.market_data import fetch_ohlcv_df
    from app.services.trading.scanner import _detect_narrow_range, _detect_resistance_retests, _detect_vcp
    from app.services.trading.snapshot_bar_ops import normalize_bar_start_utc

    min_len = 60 if bar_interval == "1d" else 120
    try:
        df = fetch_ohlcv_df(ticker, period=period, interval=bar_interval)
        if df.empty or len(df) < min_len:
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
    rows: list[dict] = []
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
        vol_ratio = (float(volume.iloc[i]) / float(vol_sma.iloc[i]) if pd.notna(vol_sma.iloc[i]) and float(vol_sma.iloc[i]) > 0 else 1.0)
        prev_close = float(close.iloc[i - 1]) if i > 0 else price
        gap_pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0
        stoch_bull_div = stoch_bear_div = False
        if i >= 5:
            prices_5 = [float(close.iloc[j]) for j in range(i - 4, i + 1)]
            stochs_5 = [float(stoch_k.iloc[j]) if pd.notna(stoch_k.iloc[j]) else 50 for j in range(i - 4, i + 1)]
            if prices_5[-1] < min(prices_5[:-1]) and stochs_5[-1] > min(stochs_5[:-1]):
                stoch_bull_div = True
            if prices_5[-1] > max(prices_5[:-1]) and stochs_5[-1] < max(stochs_5[:-1]):
                stoch_bear_div = True
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
        bar_start_utc = normalize_bar_start_utc(df.index[i])
        rows.append({
            "ticker": ticker,
            "bar_interval": bar_interval,
            "bar_start_utc": bar_start_utc,
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
            "ema_stack": (e20 is not None and e50 is not None and e100 is not None and price > e20 > e50 > e100),
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


if __name__ == "__main__":
    raise SystemExit(main())
