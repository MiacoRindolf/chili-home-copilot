"""Report what a hydration run actually loaded, what it missed, and what it cost.

The hydrator's own ``--status`` answers "how many jobs are done".  That is not
the question a study needs answered before it trusts a corpus.  The questions
that matter are:

  * Of the symbol-days I ASKED for, which ones can I actually replay?
    (a job marked ``done`` with zero rows is not coverage, it is a hole)
  * Which failed, and for what distinct reasons?
  * What did it cost in requests, bytes and wall clock?

So this reads the corpus manifest as the DENOMINATOR and the tape tables as the
NUMERATOR, rather than reporting the job ledger back to itself.  A symbol-day is
only "replayable" if rows exist in the table the replay reads.

Read-only.  Touches ``chili_hydrated`` only; never ``chili``.

    python scripts/hydration_coverage_report.py --csv corpus.csv --json out.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def et_session_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """04:00-20:00 ET as UTC -- the window CHILI actually trades.

    Via zoneinfo, never a fixed offset: the corpus can straddle a DST boundary
    and a hardcoded -4 would silently shift a whole session by an hour.
    """
    lo = datetime.combine(day, time(4, 0), tzinfo=ET).astimezone(timezone.utc)
    hi = datetime.combine(day, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
    return lo, hi

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from historical_tick_hydrator import (  # noqa: E402
    DEFAULT_HYDRATED_DB,
    NBBO_TABLE,
    SOURCE_IQFEED_NBBO,
    SOURCE_IQFEED_TRADES,
    SOURCE_POLYGON_NBBO,
    SOURCE_POLYGON_TRADES,
    TRADES_TABLE,
    connect,
    read_pairs_csv,
)

# (provider, dataset) -> (table, source value written by the hydrator).
# The source values are IMPORTED, never spelled out here: a hardcoded copy that
# drifted from the hydrator would query a tag nothing was written under and
# report a clean "0 rows" — a silent false negative that looks like a coverage
# hole rather than a bug in this file.
DATASETS: dict[tuple[str, str], tuple[str, str]] = {
    ("iqfeed", "trades"): (TRADES_TABLE, SOURCE_IQFEED_TRADES),
    ("iqfeed", "nbbo"): (NBBO_TABLE, SOURCE_IQFEED_NBBO),
    ("polygon", "trades"): (TRADES_TABLE, SOURCE_POLYGON_TRADES),
    ("polygon", "nbbo"): (NBBO_TABLE, SOURCE_POLYGON_NBBO),
}


def _row_counts(conn, table: str, source: str) -> dict[tuple[str, date], int]:
    """Rows per (symbol, ET trading day) for one hydrated source.

    Bucketed by America/New_York because a trading day is an ET concept and the
    tape spans 04:00-20:00 ET, which straddles midnight UTC.  ``observed_at`` is
    naive-UTC in the trade table and aware in the NBBO table, so the cast to
    ``timestamptz`` is what makes the two comparable.
    """
    # The NBBO tape is timestamptz, the trade tape is naive-UTC. The extra
    # "AT TIME ZONE 'UTC'" is what gives the naive column a zone before it is
    # converted; omitting it would silently bucket by server-local time.
    day_expr = (
        "(observed_at AT TIME ZONE 'America/New_York')::date"
        if table == NBBO_TABLE
        else "(observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date"
    )
    sql = (f"SELECT symbol, {day_expr} AS d, count(*) "
           f"FROM {table} WHERE source = %s GROUP BY 1, 2")
    with conn.cursor() as cur:
        cur.execute(sql, (source,))
        return {(sym, day): int(n) for sym, day, n in cur.fetchall()}


def build_report(pairs: list[tuple[str, date]], dbname: str,
                 env_path: str | None = None) -> dict[str, Any]:
    conn = connect(dbname, env_path)
    try:
        counts = {key: _row_counts(conn, tbl, src) for key, (tbl, src) in DATASETS.items()}

        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, trading_day, dataset, provider, status, rows_loaded, last_error "
                "FROM hydration_jobs"
            )
            jobs = {(s, d, ds, p): (st, int(rl or 0), err)
                    for s, d, ds, p, st, rl, err in cur.fetchall()}

            cur.execute(
                "SELECT provider, dataset, count(*), coalesce(sum(request_count),0), "
                "       coalesce(sum(bytes_received),0), coalesce(sum(rows_loaded),0), "
                "       coalesce(sum(rows_deleted),0), "
                "       coalesce(sum(extract(epoch FROM (completed_at - requested_at))),0), "
                "       min(requested_at), max(completed_at) "
                "FROM hydration_batches GROUP BY 1,2 ORDER BY 1,2"
            )
            cost = [{
                "provider": p, "dataset": ds, "batches": int(n),
                "requests": int(rq), "bytes": int(by), "rows_loaded": int(rl),
                "rows_replaced": int(rd), "provider_seconds": round(float(sec), 1),
                "first_request": fr.isoformat() if fr else None,
                "last_complete": lc.isoformat() if lc else None,
            } for p, ds, n, rq, by, rl, rd, sec, fr, lc in cur.fetchall()]

            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            db_size = cur.fetchone()[0]
    finally:
        conn.close()

    per_pair: list[dict[str, Any]] = []
    covered: dict[str, int] = Counter()
    failures: list[dict[str, Any]] = []
    reasons: dict[str, list[str]] = defaultdict(list)

    for sym, day in pairs:
        since, until = et_session_bounds_utc(day)
        rec: dict[str, Any] = {
            "symbol": sym, "date": day.isoformat(),
            # Explicit UTC bounds for the replay harness. Prefer these over its
            # --date flag: that flag hardcodes a UTC-7 window ("PDT in July"),
            # which happens to contain the ET session but is not derived from it
            # and is an hour off outside daylight time.
            "since_utc": since.isoformat(),
            "until_utc": until.isoformat(),
        }
        for (prov, ds) in DATASETS:
            n = counts[(prov, ds)].get((sym, day), 0)
            rec[f"{prov}_{ds}"] = n
            if n > 0:
                covered[f"{prov}_{ds}"] += 1
            status, _, err = jobs.get((sym, day, ds, prov), ("missing", 0, None))
            rec[f"{prov}_{ds}_status"] = status
            if status in ("failed", "no_data") or (status == "done" and n == 0):
                label = status if status != "done" else "done_but_empty"
                failures.append({"symbol": sym, "date": day.isoformat(),
                                 "provider": prov, "dataset": ds,
                                 "status": label, "error": err})
                reasons[f"{prov}/{ds}/{label}"].append(f"{sym} {day}")
        # A symbol-day is replayable when SOME provider gave it trades.
        rec["replayable_trades"] = rec["iqfeed_trades"] > 0 or rec["polygon_trades"] > 0
        rec["replayable_nbbo"] = rec["iqfeed_nbbo"] > 0 or rec["polygon_nbbo"] > 0
        per_pair.append(rec)

    return {
        "database": dbname,
        "database_size": db_size,
        "corpus_symbol_days": len(pairs),
        "coverage_by_source": dict(covered),
        "replayable_trades": sum(1 for r in per_pair if r["replayable_trades"]),
        "replayable_nbbo": sum(1 for r in per_pair if r["replayable_nbbo"]),
        "replayable_both": sum(1 for r in per_pair
                               if r["replayable_trades"] and r["replayable_nbbo"]),
        "not_replayable": [f"{r['symbol']} {r['date']}" for r in per_pair
                           if not r["replayable_trades"]],
        "cost": cost,
        "cost_totals": {
            "requests": sum(c["requests"] for c in cost),
            "bytes": sum(c["bytes"] for c in cost),
            "rows_loaded": sum(c["rows_loaded"] for c in cost),
            "provider_seconds": round(sum(c["provider_seconds"] for c in cost), 1),
        },
        "failure_count": len(failures),
        "failure_reasons": {k: {"n": len(v), "examples": v[:8]}
                            for k, v in sorted(reasons.items())},
        "failures": failures,
        "per_symbol_day": per_pair,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="corpus CSV (symbol,date)")
    ap.add_argument("--db-name", default=DEFAULT_HYDRATED_DB)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--csv-out", help="write the per-symbol-day table here")
    args = ap.parse_args(argv)

    pairs = read_pairs_csv(args.csv)
    report = build_report(pairs, args.db_name, args.env_file)

    if args.json:
        with open(args.json, "w", newline="", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
    if args.csv_out:
        rows = report["per_symbol_day"]
        with open(args.csv_out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    brief = {k: v for k, v in report.items()
             if k not in ("per_symbol_day", "failures")}
    print(json.dumps(brief, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
