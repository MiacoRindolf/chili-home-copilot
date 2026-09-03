"""Detect a price-SCALE mismatch between a corpus's levels and the hydrated tape.

THE DEFECT THIS EXISTS TO CATCH
-------------------------------
Provider **tick** endpoints (IQFeed lookup HTT, Polygon ``/v3/trades``) return
**as-traded** prices.  Provider **aggregate** endpoints (Polygon grouped daily
and per-ticker minute bars) return **split-adjusted** prices by default.

A study that derives its entry, stop and target levels from aggregates and then
evaluates them against a hydrated tick tape is comparing two different price
scales.  For a symbol with a reverse split between the trading day and today,
those scales differ by the split ratio -- measured here at up to **160x**.

Nothing about it looks wrong.  Both numbers are individually correct.  The stop
simply never trips, or trips instantly, and the counterfactual reports a
confident, meaningless answer.

HOW IT IS DETECTED, AND WHY THE TEST IS TRUSTWORTHY
---------------------------------------------------
**Dollar volume is invariant under split adjustment** -- adjusting multiplies
price by *k* and divides volume by *k*, so ``sum(price * size)`` is unchanged.
Price is not invariant.  So the two measures separate the two hypotheses
cleanly:

* dollar volume agrees **and** price agrees  -> same scale, all well
* dollar volume agrees **but** price does not -> **scale mismatch** (a split);
  the tape is complete and correct, it is simply denominated differently
* dollar volume disagrees -> a coverage or symbology problem, NOT a split

That second row is the whole point: it distinguishes "the tape is wrong" from
"the tape is right and your levels are on another scale", which are very
different problems with very different fixes.

Comparison uses the **median** traded price, not the high.  A single stray
print -- one share at a nonsense price -- moves the max by orders of magnitude
and produced three false positives when this was first run against the max.

    python scripts/hydration_price_scale_check.py --csv corpus.csv \
        --price-column move_high --dollar-volume-column dollar_vol
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from historical_tick_hydrator import (  # noqa: E402
    DEFAULT_HYDRATED_DB,
    TRADES_TABLE,
    connect,
    et_day_bounds_utc,
    naive_utc,
)

# A split ratio is never subtle. These bounds are deliberately wide so that
# ordinary disagreement between "sustained high" and "median print" never trips
# the alarm, and only an order-of-magnitude scale error does.
PRICE_LO, PRICE_HI = 0.5, 2.0
# Dollar volume should agree closely when the tape is complete; the band allows
# for the corpus measuring a session window slightly different from the ET day.
DV_LO, DV_HI = 0.7, 1.4


def tape_stats(conn, symbol: str, day: date) -> tuple[float, float, int] | None:
    """(median price, dollar volume, ticks) for one ET day of the hydrated tape."""
    lo, hi = et_day_bounds_utc(day)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY price), "
            "       sum(price * size), count(*) "
            f"FROM {TRADES_TABLE} "
            "WHERE symbol = %s AND observed_at >= %s AND observed_at < %s",
            (symbol, naive_utc(lo), naive_utc(hi)),
        )
        med, dv, n = cur.fetchone()
    if med is None or not n:
        return None
    return float(med), float(dv or 0.0), int(n)


def classify(price_ratio: float, dv_ratio: float | None) -> str:
    """Name the situation, so the caller is told what to DO, not just a number."""
    price_ok = PRICE_LO <= price_ratio <= PRICE_HI
    if dv_ratio is None:
        return "ok" if price_ok else "price_scale_mismatch_unconfirmed"
    dv_ok = DV_LO <= dv_ratio <= DV_HI
    if price_ok and dv_ok:
        return "ok"
    if dv_ok and not price_ok:
        # The decisive case: complete tape, different denomination.
        return "split_adjusted_levels"
    if not dv_ok and price_ok:
        return "volume_mismatch"
    return "scale_and_volume_mismatch"


def build(rows: list[dict], dbname: str, price_col: str, dv_col: str,
          low_col: str | None, env_path: str | None = None) -> dict[str, Any]:
    conn = connect(dbname, env_path)
    out: list[dict[str, Any]] = []
    try:
        for r in rows:
            sym = (r.get("symbol") or r.get("ticker") or "").strip().upper()
            raw = (r.get("date") or r.get("trading_day") or "").strip()
            if not sym or not raw:
                continue
            day = date.fromisoformat(raw[:10])
            stats = tape_stats(conn, sym, day)
            if stats is None:
                out.append({"symbol": sym, "date": raw[:10], "status": "no_tape"})
                continue
            med, dv, n = stats

            hi_v = r.get(price_col)
            lo_v = r.get(low_col) if low_col else None
            try:
                level = ((float(hi_v) + float(lo_v)) / 2.0) if lo_v else float(hi_v)
            except (TypeError, ValueError):
                out.append({"symbol": sym, "date": raw[:10], "status": "no_reference"})
                continue

            try:
                corpus_dv = float(r.get(dv_col))
            except (TypeError, ValueError):
                corpus_dv = None
            dv_ratio = (dv / corpus_dv) if corpus_dv else None
            price_ratio = level / med if med else float("inf")

            out.append({
                "symbol": sym, "date": raw[:10],
                "reference_level": round(level, 6),
                "tape_median_price": med,
                "price_ratio": round(price_ratio, 3),
                "corpus_dollar_volume": corpus_dv,
                "tape_dollar_volume": round(dv, 2),
                "dollar_volume_ratio": round(dv_ratio, 4) if dv_ratio else None,
                "ticks": n,
                "status": classify(price_ratio, dv_ratio),
            })
    finally:
        conn.close()

    by_status: dict[str, int] = {}
    for r in out:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    flagged = [r for r in out if r["status"] not in ("ok", "no_reference")]
    return {
        "database": dbname,
        "compared": len(out),
        "by_status": by_status,
        "affected_symbols": sorted({r["symbol"] for r in flagged}),
        "flagged": sorted(flagged, key=lambda r: -(r.get("price_ratio") or 0)),
        "rows": out,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="corpus CSV")
    ap.add_argument("--price-column", default="move_high",
                    help="corpus column holding a price level (default move_high)")
    ap.add_argument("--price-low-column", default="move_low",
                    help="optional second price column; the midpoint of the two is "
                         "used, which is steadier than either end")
    ap.add_argument("--dollar-volume-column", default="dollar_vol",
                    help="corpus column holding the day's dollar volume -- this is "
                         "what makes the diagnosis, so prefer a corpus that has it")
    ap.add_argument("--db-name", default=DEFAULT_HYDRATED_DB)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--json", help="write the full result here")
    args = ap.parse_args(argv)

    with open(args.csv, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    fields = set(rows[0].keys()) if rows else set()
    low_col = args.price_low_column if args.price_low_column in fields else None

    report = build(rows, args.db_name, args.price_column,
                   args.dollar_volume_column, low_col, args.env_file)

    if args.json:
        with open(args.json, "w", newline="", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)

    print(json.dumps({k: v for k, v in report.items() if k != "rows"},
                     indent=2, default=str))
    bad = sum(v for k, v in report["by_status"].items()
              if k not in ("ok", "no_reference"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
