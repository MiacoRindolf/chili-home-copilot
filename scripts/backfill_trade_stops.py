"""
Backfill stop_loss / take_profit on all open trades from indicator_snapshot
or ATR-based computation.  Also fixes known data-quality issues:
  - close duplicate open rows (STLA/AMGN/ETH-USD older versions without snapshot)
  - fix PTEN entry_price=0

Usage:
    python scripts/backfill_trade_stops.py          # dry-run
    python scripts/backfill_trade_stops.py --apply   # commit changes
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def _is_crypto(ticker: str) -> bool:
    return ticker.upper().endswith("-USD")


def _fetch_atr(ticker: str) -> float | None:
    """Best-effort ATR(14) for a ticker via the market_data helpers."""
    try:
        from app.services.trading.market_data import fetch_ohlcv_df
        df = fetch_ohlcv_df(ticker, interval="1d", period="30d")
        if df is None or len(df) < 15:
            return None
        from ta.volatility import AverageTrueRange
        atr_series = AverageTrueRange(
            high=df["High"], low=df["Low"], close=df["Close"], window=14
        ).average_true_range()
        val = atr_series.iloc[-1]
        return float(val) if val and val > 0 else None
    except Exception as e:
        log.warning(f"  ATR fetch failed for {ticker}: {e}")
        return None


def _fetch_price(ticker: str) -> float | None:
    try:
        from app.services.trading.market_data import fetch_quote
        q = fetch_quote(ticker)
        p = q.get("price", 0) if q else 0
        return float(p) if p and p > 0 else None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Commit changes (default is dry-run)")
    args = parser.parse_args()

    engine = create_engine(settings.database_url)

    with engine.connect() as conn:
        # ── 1. Close duplicate open rows (older versions without snapshot) ──
        dupes = conn.execute(text("""
            SELECT ticker, array_agg(id ORDER BY id) as ids,
                   array_agg(indicator_snapshot IS NOT NULL ORDER BY id) as has_snap
            FROM trading_trades WHERE status = 'open'
            GROUP BY ticker HAVING COUNT(*) > 1
        """)).fetchall()

        for row in dupes:
            ticker, ids, snaps = row[0], list(row[1]), list(row[2])
            keep_id = None
            for i, (tid, has_s) in enumerate(zip(ids, snaps)):
                if has_s:
                    keep_id = tid
            if keep_id is None:
                keep_id = ids[-1]
            close_ids = [i for i in ids if i != keep_id]
            log.info(f"[dupe] {ticker}: keeping id={keep_id}, closing {close_ids}")
            if args.apply:
                for cid in close_ids:
                    conn.execute(text("""
                        UPDATE trading_trades SET status='closed',
                            notes=COALESCE(notes,'')||' [auto-closed: duplicate from backfill]',
                            exit_date=NOW()
                        WHERE id=:id
                    """), {"id": cid})

        # ── 2. Fix PTEN entry_price=0 ──
        zeros = conn.execute(text(
            "SELECT id, ticker FROM trading_trades WHERE status='open' AND (entry_price IS NULL OR entry_price=0)"
        )).fetchall()
        for row in zeros:
            tid, ticker = row[0], row[1]
            price = _fetch_price(ticker)
            if price:
                log.info(f"[fix] {ticker} id={tid}: entry_price 0 -> {price} (current price as fallback)")
                if args.apply:
                    conn.execute(text(
                        "UPDATE trading_trades SET entry_price=:p WHERE id=:id"
                    ), {"p": price, "id": tid})
            else:
                log.warning(f"[fix] {ticker} id={tid}: entry_price=0 and no quote available")

        # ── 3. Backfill stop_loss / take_profit from snapshot or ATR ──
        trades = conn.execute(text("""
            SELECT id, ticker, entry_price, indicator_snapshot, direction,
                   stop_loss, take_profit
            FROM trading_trades WHERE status='open'
        """)).fetchall()

        filled = 0
        atr_computed = 0
        for t in trades:
            tid, ticker, entry, snap_raw, direction = t[0], t[1], t[2], t[3], t[4]
            existing_sl, existing_tp = t[5], t[6]

            if existing_sl and existing_tp:
                continue

            sl = tp = None
            model = None

            # Try snapshot first
            if snap_raw:
                try:
                    snap = json.loads(snap_raw) if isinstance(snap_raw, str) else snap_raw
                    sl = snap.get("stop_loss")
                    tp = snap.get("take_profit")
                    if sl and tp:
                        model = "snapshot"
                except Exception:
                    pass

            # Fall back to ATR computation
            if not sl or not tp:
                atr = _fetch_atr(ticker)
                price = _fetch_price(ticker) or entry
                if atr and price and price > 0 and entry and entry > 0:
                    crypto = _is_crypto(ticker)
                    vol_pct = (atr / price) * 100
                    stop_mult = 2.5 if vol_pct > 3 else 2.0
                    if direction == "long":
                        sl = sl or round(entry - stop_mult * atr, 8 if crypto else 4)
                        tp = tp or round(entry + 3.0 * atr, 8 if crypto else 4)
                    else:
                        sl = sl or round(entry + stop_mult * atr, 8 if crypto else 4)
                        tp = tp or round(entry - 3.0 * atr, 8 if crypto else 4)
                    model = "atr_swing"
                    atr_computed += 1
                else:
                    # Last resort: percentage-based
                    if entry and entry > 0:
                        if direction == "long":
                            sl = sl or round(entry * 0.92, 4)
                            tp = tp or round(entry * 1.15, 4)
                        else:
                            sl = sl or round(entry * 1.08, 4)
                            tp = tp or round(entry * 0.85, 4)
                        model = "pct_fallback"
                    else:
                        log.warning(f"  [skip] {ticker} id={tid}: no entry price, no ATR")
                        continue

            # Set high_watermark to current price if available
            hwm = _fetch_price(ticker) or entry

            log.info(
                f"  [fill] {ticker} id={tid}: SL={sl} TP={tp} model={model} HWM={hwm}"
            )
            filled += 1

            if args.apply:
                conn.execute(text("""
                    UPDATE trading_trades
                    SET stop_loss=:sl, take_profit=:tp, stop_model=:model,
                        high_watermark=:hwm
                    WHERE id=:id
                """), {"sl": sl, "tp": tp, "model": model, "hwm": hwm, "id": tid})

        if args.apply:
            conn.commit()
            log.info(f"\nCommitted. Filled {filled} trades ({atr_computed} via ATR).")
        else:
            log.info(f"\nDry-run. Would fill {filled} trades ({atr_computed} via ATR). Re-run with --apply to commit.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
