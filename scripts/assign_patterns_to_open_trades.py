"""Best-effort pattern assignment for open trades that have no scan_pattern_id.

Run inside the chili container:
    docker exec chili-home-copilot-chili-1 python -m scripts.assign_patterns_to_open_trades

For each user-13 open trade without a scan_pattern_id:
  1. Fetch recent OHLC (daily, 3mo).
  2. Compute the full indicator bundle used by the scanner.
  3. Build a `build_indicator_snapshot`-style snapshot identical to the one
     the scanner uses for pattern evaluation (scanner.py:1034–1057).
  4. Call `evaluate_patterns()` for hard matches; fall back to
     `evaluate_patterns_soft(min_eval_ratio=0.6)` if nothing matched.
  5. Pick the match with the highest `score_boost` (tie-break by confidence)
     and write it to `trading_trades.scan_pattern_id`.

Idempotent: WHERE scan_pattern_id IS NULL, so re-runs won't overwrite any
pattern link (manual, Telegram-recovered, or prior assignment).
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import pandas as pd

from app.db import SessionLocal
from app.models.trading import Trade, ScanPattern
from app.services.trading.pattern_engine import (
    evaluate_patterns,
    evaluate_patterns_soft,
    get_active_patterns,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("assign_patterns")

USER_ID = 13


def _build_snapshot(ticker: str) -> dict[str, Any] | None:
    """Replicate the indicator bundle the scanner feeds into evaluate_patterns."""
    from app.services.trading.market_data import fetch_ohlcv_df
    from app.services.trading.indicator_core import compute_all_from_df
    from app.services.trading.pattern_engine import build_indicator_snapshot

    df = fetch_ohlcv_df(ticker, period="3mo", interval="1d")
    if df is None or len(df) < 25:
        log.warning("%s: insufficient OHLC (%s rows)", ticker, 0 if df is None else len(df))
        return None

    needed = {
        "rsi_14", "macd_histogram", "macd", "macd_signal", "adx",
        "ema_9", "ema_20", "ema_50", "ema_100", "ema_200",
        "sma_20", "sma_50", "sma_200",
        "bb_upper", "bb_middle", "bb_lower", "bb_pct",
        "atr", "stochastic_k", "stochastic_d", "obv",
        "volume_ratio", "rel_vol", "price",
        "dist_to_resistance_pct", "dist_from_ema_20_pct",
        "vcp_count",
    }
    bundle = compute_all_from_df(df, needed=needed)

    def last(key: str) -> float | None:
        arr = bundle.get(key)
        if not arr:
            return None
        try:
            v = arr[-1]
            if pd.isna(v):
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    price = last("price") or float(df["Close"].iloc[-1])
    ema_20 = last("ema_20")
    ema_50 = last("ema_50")
    ema_100 = last("ema_100")
    rsi = last("rsi_14")
    macd_h = last("macd_histogram")
    adx = last("adx")
    bb_p = last("bb_pct")
    stoch_k = last("stochastic_k")
    vol_r = last("volume_ratio") or last("rel_vol")
    dist_res = last("dist_to_resistance_pct")
    dist_ema20 = last("dist_from_ema_20_pct")
    if dist_ema20 is None and ema_20 and price:
        dist_ema20 = abs(price - ema_20) / ema_20 * 100.0
    vcp = last("vcp_count")

    ema_stack_bullish = bool(
        ema_20 and ema_50 and ema_100
        and price > ema_20 > ema_50 > ema_100
    )
    ema_stack_bearish = bool(
        ema_20 and ema_50 and ema_100
        and price < ema_20 < ema_50 < ema_100
    )

    resistance = price * (1 + (dist_res / 100.0)) if dist_res is not None else price * 1.02
    snap = build_indicator_snapshot(
        price=price,
        resistance=resistance,
        indicators={
            "rsi_14": rsi,
            "macd_hist": macd_h,
            "macd_histogram": macd_h,
            "adx": adx,
            "bb_pct": bb_p,
            "rel_vol": vol_r,
            "volume_ratio": vol_r,
            "ema_stack_bullish": ema_stack_bullish,
            "ema_stack_bearish": ema_stack_bearish,
            "ema_20": ema_20,
            "ema_50": ema_50,
            "ema_100": ema_100,
            "sma_20": last("sma_20"),
            "sma_50": last("sma_50"),
            "dist_to_resistance_pct": dist_res,
            "dist_from_ema_20_pct": dist_ema20,
            "vcp_count": int(vcp) if vcp is not None else None,
        },
        extra={
            "stoch_k": stoch_k,
            "stochastic_k": stoch_k,
            "macd_histogram": macd_h,
        },
    )
    return snap


def _best_match(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not matches:
        return None
    return max(
        matches,
        key=lambda m: (
            float(m.get("score_boost") or 0),
            float(m.get("confidence") or 0),
            float(m.get("win_rate") or 0),
        ),
    )


def main() -> int:
    db = SessionLocal()
    updated = 0
    considered = 0
    try:
        trades: list[Trade] = (
            db.query(Trade)
            .filter(
                Trade.user_id == USER_ID,
                Trade.status == "open",
                Trade.scan_pattern_id.is_(None),
            )
            .all()
        )
        log.info("Found %d open trades without scan_pattern_id for user %d", len(trades), USER_ID)
        if not trades:
            return 0

        patterns = get_active_patterns(db, asset_class="all")
        log.info("Evaluating against %d active patterns", len(patterns))

        for trade in trades:
            considered += 1
            ticker = trade.ticker.upper()
            snap = _build_snapshot(ticker)
            if snap is None:
                log.info("%s trade %s: no indicator snapshot — skipping", ticker, trade.id)
                continue

            hard = evaluate_patterns(snap, patterns)
            pick = _best_match(hard)
            mode = "hard"
            if pick is None:
                soft = evaluate_patterns_soft(snap, patterns, min_eval_ratio=0.6)
                pick = _best_match(soft)
                mode = "soft"
            if pick is None:
                log.info("%s trade %s: no pattern match (hard or soft)", ticker, trade.id)
                continue

            trade.scan_pattern_id = int(pick["pattern_id"])
            notes = (trade.notes or "").rstrip()
            tag = f"[auto-assigned pattern #{pick['pattern_id']} via {mode} match on {pd.Timestamp.now(tz='UTC'):%Y-%m-%d}]"
            trade.notes = f"{notes}\n{tag}".strip() if notes else tag
            log.info(
                "%s trade %s -> pattern #%s %s (score_boost=%s, mode=%s)",
                ticker, trade.id, pick["pattern_id"], pick["name"],
                pick.get("score_boost"), mode,
            )
            updated += 1

        if updated:
            db.commit()
            log.info("Committed %d pattern assignments (out of %d considered)", updated, considered)
        else:
            log.info("No assignments made (considered %d trades)", considered)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
