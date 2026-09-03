"""Measure the MIXED-VENDOR QUOTE SEAM the canonical corpus actually has.

WHY THIS EXISTS
---------------
Canonicalization keeps ONE source per (symbol, ET day, table), and the
preference deliberately INVERTS between the two tables: IQFeed for trades
(Phase 3 measured it as the closer read of the feed our own bridge consumes),
Polygon for NBBO (IQFeed lookup exposes no historical quote STREAM, only the
quote attached to each print, so it cannot represent a quote that moves BETWEEN
trades -- which is exactly what spread floors and stale-BBO vetoes read).

That removes the vendor flicker WITHIN the NBBO table.  It does not remove it
ACROSS the two tables the replay reads together, and the replay reads both:

  * ``load_trade_tape`` selects ``x.bid, x.ask`` from ``iqfeed_trade_ticks`` and
    puts them on every ``ReplayTapeTick`` it returns -- those are IQFeed's
    native at-trade quotes;
  * ``load_nbbo_tape`` returns the Polygon quote stream.

So a spread computed off a trade tick and a spread read from the NBBO tape at
the same instant come from DIFFERENT VENDORS.  Phase 3 measured the two vendors'
NBBO tapes disagreeing at up to 50% of sampled instants on one name.  The
handover called the canonicalization fix "structural" and it is -- but the
hazard was RELOCATED, not eliminated, and because the preference inverts, the
relocation is universal across every symbol-day rather than incidental.

This tool measures that seam, so the disagreement is a number rather than an
inference.  It does not fix it: the decision (recorded in
docs/HISTORICAL_TICK_HYDRATOR.md) is to KEEP IQFeed's native at-trade quotes
rather than overwrite them with a Polygon as-of reconstruction, because a native
measurement is worth more than a merge that is itself unvalidated -- and to make
the split VISIBLE instead of implicit.

METHOD
------
For each symbol-day, sample trade rows evenly across the session and, for each
sampled trade, find the NBBO row at-or-before it (a LATERAL backward index scan
on ``(symbol, observed_at)`` -- the as-of read a replay would do).  Compare the
trade row's own bid/ask against that NBBO row's.

Deliberately: the sample is taken in SQL and only the sampled rows are shipped,
the as-of lookback is bounded by the SESSION START rather than an arbitrary
interval (a quote from before the tape begins is genuinely absent, not stale),
and a trade with no prior quote is counted in its own bucket instead of being
dropped -- dropping it would flatter the agreement rate.

Read-only.  Touches ``chili_hydrated`` only; never ``chili``.

    python scripts/hydration_quote_seam_check.py --csv corpus.csv --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from historical_tick_hydrator import (  # noqa: E402
    DEFAULT_HYDRATED_DB,
    NBBO_TABLE,
    TRADES_TABLE,
    connect,
    read_pairs_csv,
)

ET = ZoneInfo("America/New_York")

DEFAULT_SAMPLE = 2000
# Exact agreement is at cent resolution: both vendors quote in cents, and a
# sub-cent difference is a rounding artefact of the wire format, not a
# disagreement about the book.
EXACT_TOL = 5e-7


def _session_bounds(day: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(day, time(4, 0), tzinfo=ET).astimezone(timezone.utc)
    hi = datetime.combine(day, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
    return lo, hi


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return round(s[idx], 6)


def compare_symbol_day(conn, symbol: str, day: date, *,
                       sample: int = DEFAULT_SAMPLE) -> dict[str, Any]:
    lo, hi = _session_bounds(day)
    lo_n, hi_n = lo.replace(tzinfo=None), hi.replace(tzinfo=None)
    rec: dict[str, Any] = {"symbol": symbol, "date": day.isoformat()}

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*), count(DISTINCT source) FROM {TRADES_TABLE} "
            "WHERE symbol = %s AND observed_at >= %s AND observed_at < %s "
            "AND price > 0 AND bid > 0 AND ask > 0 AND ask >= bid",
            (symbol, lo_n, hi_n),
        )
        n_trades, n_trade_sources = cur.fetchone()
        cur.execute(
            f"SELECT count(*), min(source) FROM {NBBO_TABLE} "
            "WHERE symbol = %s AND observed_at >= %s AND observed_at < %s "
            "AND bid > 0 AND ask > 0 AND ask >= bid",
            (symbol, lo, hi),
        )
        n_quotes, nbbo_source = cur.fetchone()
        cur.execute(
            f"SELECT DISTINCT source FROM {TRADES_TABLE} "
            "WHERE symbol = %s AND observed_at >= %s AND observed_at < %s LIMIT 4",
            (symbol, lo_n, hi_n),
        )
        trade_sources = sorted(s for (s,) in cur.fetchall())

    rec.update({
        "trade_rows_with_quote": int(n_trades or 0),
        "nbbo_rows_valid": int(n_quotes or 0),
        "trade_sources": trade_sources,
        "nbbo_source": nbbo_source,
        "mixed_vendor": bool(
            trade_sources and nbbo_source
            and trade_sources[0].split("_")[0] != str(nbbo_source).split("_")[0]
        ),
    })
    if not n_trades or not n_quotes:
        rec["status"] = "no_overlap"
        return rec

    stride = max(1, int(n_trades) // max(1, sample))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH s AS (
              SELECT observed_at, bid, ask,
                     row_number() OVER (ORDER BY observed_at, id) AS rn
              FROM {TRADES_TABLE}
              WHERE symbol = %s AND observed_at >= %s AND observed_at < %s
                AND price > 0 AND bid > 0 AND ask > 0 AND ask >= bid
            )
            SELECT s.observed_at, s.bid, s.ask, q.bid, q.ask, q.observed_at
            FROM s
            LEFT JOIN LATERAL (
              SELECT observed_at, bid, ask FROM {NBBO_TABLE}
              WHERE symbol = %s
                AND observed_at <= (s.observed_at AT TIME ZONE 'UTC')
                AND observed_at >= %s
                AND bid > 0 AND ask > 0 AND ask >= bid
              ORDER BY observed_at DESC LIMIT 1
            ) q ON true
            WHERE s.rn %% %s = 0
            """,
            (symbol, lo_n, hi_n, symbol, lo, stride),
        )
        rows = cur.fetchall()

    dbid: list[float] = []
    dask: list[float] = []
    ages: list[float] = []
    exact = 0
    no_quote = 0
    for t_ts, t_bid, t_ask, q_bid, q_ask, q_ts in rows:
        if q_bid is None or q_ask is None:
            no_quote += 1
            continue
        db_ = abs(float(t_bid) - float(q_bid))
        da_ = abs(float(t_ask) - float(q_ask))
        dbid.append(db_)
        dask.append(da_)
        if db_ <= EXACT_TOL and da_ <= EXACT_TOL:
            exact += 1
        ages.append((t_ts.replace(tzinfo=timezone.utc) - q_ts).total_seconds())

    compared = len(dbid)
    rec.update({
        "status": "compared",
        "sampled": len(rows),
        "stride": stride,
        "compared": compared,
        "no_prior_quote": no_quote,
        "exact_agreements": exact,
        "exact_rate": round(exact / compared, 6) if compared else None,
        "bid_abs_p50": _pct(dbid, 0.50), "bid_abs_p90": _pct(dbid, 0.90),
        "bid_abs_p99": _pct(dbid, 0.99),
        "bid_abs_max": round(max(dbid), 6) if dbid else None,
        "ask_abs_p50": _pct(dask, 0.50), "ask_abs_p90": _pct(dask, 0.90),
        "ask_abs_p99": _pct(dask, 0.99),
        "ask_abs_max": round(max(dask), 6) if dask else None,
        # How old the as-of NBBO row was at the trade instant. A large median
        # here means the disagreement is mostly the two vendors sampling the
        # book at different moments, not disagreeing about its level.
        "quote_age_s_p50": _pct(ages, 0.50),
        "quote_age_s_p90": _pct(ages, 0.90),
    })
    return rec


def build_report(pairs: list[tuple[str, date]], dbname: str,
                 env_path: str | None = None,
                 sample: int = DEFAULT_SAMPLE) -> dict[str, Any]:
    conn = connect(dbname, env_path)
    try:
        rows = [compare_symbol_day(conn, sym, day, sample=sample)
                for sym, day in pairs]
    finally:
        conn.close()

    compared = [r for r in rows if r.get("status") == "compared"]
    rates = [r["exact_rate"] for r in compared if r["exact_rate"] is not None]
    maxes = [r["bid_abs_max"] for r in compared if r["bid_abs_max"] is not None]
    return {
        "database": dbname,
        "symbol_days": len(rows),
        "compared": len(compared),
        "no_overlap": sum(1 for r in rows if r.get("status") == "no_overlap"),
        "mixed_vendor_symbol_days": sum(1 for r in rows if r.get("mixed_vendor")),
        "exact_rate_min": round(min(rates), 6) if rates else None,
        "exact_rate_median": round(statistics.median(rates), 6) if rates else None,
        "exact_rate_max": round(max(rates), 6) if rates else None,
        "bid_abs_max_overall": round(max(maxes), 6) if maxes else None,
        "worst_10_by_exact_rate": [
            {k: r[k] for k in ("symbol", "date", "exact_rate", "compared",
                               "bid_abs_max", "ask_abs_max", "quote_age_s_p50")}
            for r in sorted(compared, key=lambda r: r["exact_rate"] or 0.0)[:10]
        ],
        "per_symbol_day": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="corpus CSV (symbol,date)")
    ap.add_argument("--symbol-day", action="append", default=[],
                    metavar="SYMBOL:YYYY-MM-DD")
    ap.add_argument("--db-name", default=DEFAULT_HYDRATED_DB)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    ap.add_argument("--json", help="write the full report here")
    args = ap.parse_args(argv)

    pairs: list[tuple[str, date]] = []
    for token in args.symbol_day:
        sym, _, d = token.partition(":")
        pairs.append((sym.upper().strip(), date.fromisoformat(d.strip())))
    if args.csv:
        pairs.extend(read_pairs_csv(args.csv))
    if not pairs:
        ap.error("nothing to do: pass --csv and/or --symbol-day")

    report = build_report(pairs, args.db_name, args.env_file, sample=args.sample)
    if args.json:
        with open(args.json, "w", newline="", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
    print(json.dumps({k: v for k, v in report.items() if k != "per_symbol_day"},
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
