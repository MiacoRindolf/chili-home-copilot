"""Backfill trading_macro_regime_snapshots with N years of historical data.

The daily writer (``macro_regime_service.compute_and_persist``) only fills
forward — it has no historical mode. Without backfill the regime
classifier cannot train (HMM needs >=200 daily features, and macro
snapshots are joined into the feature build).

This module covers the cold-start. Pulls:
  * VIX close from yfinance ``^VIX``
  * DGS10 + DGS2 from FRED public CSV (already used by
    ``fred_yield_curve.run_weekly_fred_yield_ingestion``)

For each business day in the requested range we INSERT one row into
``trading_macro_regime_snapshots`` with the minimum columns needed for
``regime_classifier.build_regime_features`` to consume it:

  ``as_of_date, vix, yield_curve_slope_proxy, dgs10_real, dgs2_real,
   yield_slope_source, mode='backfill', symbols_sampled=0,
   coverage_score=0``

The other macro features (rates_regime, credit_regime, etc.) stay NULL
on backfill rows. This is intentional — the HMM only consumes
yield_slope + vix from this table; the rich ETF basket features live in
the daily writer's "shadow" mode and are not part of the HMM
observation set.

Idempotent on ``as_of_date`` — re-running the backfill skips dates that
already have a row.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _fetch_fred_series_full(series: str) -> Optional[dict[date, float]]:
    """Pull the full FRED series CSV (decades of data; ~5kB compressed)."""
    from .fred_yield_curve import _fetch_fred_series_csv
    rows = _fetch_fred_series_csv(series)
    if rows is None:
        return None
    return {d: v for d, v in rows}


def _fetch_vix_history() -> Optional[dict[date, float]]:
    """Pull historical VIX daily closes.

    Primary: FRED ``VIXCLS`` (CBOE Volatility Index, daily) — same CSV
    pipe used for DGS10/DGS2, decades of history, no API key needed.

    Fallback: yfinance ``^VIX`` / ``VIX`` (often blocked or rate-limited
    in the chili container, but try anyway).
    """
    fred = _fetch_fred_series_full("VIXCLS")
    if fred:
        return fred

    logger.warning(
        "[macro_backfill] FRED VIXCLS unavailable, falling back to yfinance"
    )
    from .market_data import fetch_ohlcv_df
    df = fetch_ohlcv_df("^VIX", period="5y", interval="1d")
    if df is None or df.empty or "Close" not in df.columns:
        df = fetch_ohlcv_df("VIX", period="5y", interval="1d")
    if df is None or df.empty or "Close" not in df.columns:
        return None
    out: dict[date, float] = {}
    for ts, row in df.iterrows():
        ts = pd.Timestamp(ts)
        d = ts.tz_localize(None).date() if ts.tzinfo else ts.date()
        try:
            v = float(row["Close"])
            if v > 0:
                out[d] = v
        except (TypeError, ValueError):
            continue
    return out


def backfill_macro_regime_snapshots(
    db: Session,
    *,
    years: int = 5,
    end_date: Optional[date] = None,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Backfill macro snapshots for the last ``years`` years.

    Returns counts dict: rows_inserted, rows_skipped (already present),
    days_with_full_data, days_with_partial_data.

    Per-day data is "full" when both VIX and yield slope land. Partial
    rows still get inserted so the table doesn't have gaps; the
    regime_classifier feature build will drop them when it filters on
    NaN.
    """
    end = end_date or date.today()
    start = end - timedelta(days=years * 366 + 30)  # ~5y with leap padding

    logger.info("[macro_backfill] fetching FRED DGS10 ...")
    dgs10 = _fetch_fred_series_full("DGS10")
    logger.info("[macro_backfill] fetching FRED DGS2 ...")
    dgs2 = _fetch_fred_series_full("DGS2")
    logger.info("[macro_backfill] fetching VIX history ...")
    vix = _fetch_vix_history()

    if not dgs10 or not dgs2:
        return {
            "ok": False,
            "reason": "fred_fetch_failed",
            "dgs10_rows": len(dgs10) if dgs10 else 0,
            "dgs2_rows": len(dgs2) if dgs2 else 0,
        }
    if not vix:
        return {
            "ok": False,
            "reason": "yfinance_vix_fetch_failed",
        }

    # Existing dates
    existing: set[date] = set()
    if skip_existing:
        rows = db.execute(text(
            "SELECT as_of_date FROM trading_macro_regime_snapshots "
            "WHERE as_of_date >= :s AND as_of_date <= :e"
        ), {"s": start, "e": end}).fetchall()
        existing = {r[0] for r in rows if r[0] is not None}

    inserted = 0
    skipped = 0
    full = 0
    partial = 0

    cur = start
    while cur <= end:
        # Business days only (Mon-Fri)
        if cur.weekday() < 5:
            if cur in existing:
                skipped += 1
            else:
                d10 = dgs10.get(cur)
                d2 = dgs2.get(cur)
                vx = vix.get(cur)
                yld_slope = (d10 - d2) if (d10 is not None and d2 is not None) else None
                yld_source = "fred_dgs10_dgs2" if yld_slope is not None else None

                if yld_slope is not None and vx is not None:
                    full += 1
                else:
                    partial += 1

                # Insert even on partial — the HMM build will filter.
                # mode='backfill' so it's distinguishable from live rows.
                # regime_id is required NOT NULL — synthesize a stable
                # value derived from the date so re-runs are idempotent.
                try:
                    db.execute(text(
                        """
                        INSERT INTO trading_macro_regime_snapshots
                            (regime_id, as_of_date, vix,
                             yield_curve_slope_proxy,
                             dgs10_real, dgs2_real, yield_slope_source,
                             macro_numeric, macro_label,
                             mode, symbols_sampled, symbols_missing,
                             coverage_score, payload_json,
                             computed_at, observed_at)
                        VALUES
                            (:rid, :d, :vix, :yld, :d10, :d2, :ys,
                             0, 'unknown',
                             'backfill', 0, 0, 0.0,
                             CAST('{}' AS jsonb),
                             NOW(), CAST(:d AS timestamp))
                        """
                    ), {
                        "rid": f"backfill_{cur.isoformat()}",
                        "d": cur, "vix": vx, "yld": yld_slope,
                        "d10": d10, "d2": d2, "ys": yld_source,
                    })
                    inserted += 1
                    if inserted % 200 == 0:
                        db.commit()
                except Exception as e:
                    logger.warning(
                        "[macro_backfill] insert %s failed: %s", cur, e
                    )
        cur += timedelta(days=1)

    db.commit()
    summary = {
        "ok": True,
        "range": [start.isoformat(), end.isoformat()],
        "rows_inserted": inserted,
        "rows_skipped_existing": skipped,
        "rows_with_full_data": full,
        "rows_with_partial_data": partial,
    }
    logger.info("[macro_backfill] done: %s", summary)
    return summary
