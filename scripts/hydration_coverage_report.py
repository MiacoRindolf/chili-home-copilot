"""Report what a hydration run actually loaded, what it missed, and what it cost.

The hydrator's own ``--status`` answers "how many jobs are done".  That is not
the question a study needs answered before it trusts a corpus.  The questions
that matter are:

  * Of the symbol-days I ASKED for, which ones can I actually replay?
    (a job marked ``done`` with zero rows is not coverage, it is a hole)
  * Which failed, and for what distinct reasons?
  * What did it cost in requests, bytes and wall clock?

So this reads the corpus manifest as the DENOMINATOR and the tape tables as the
NUMERATOR, rather than reporting the job ledger back to itself.

WHAT "REPLAYABLE" MEANS -- AND WHY IT IS NOT "HAS TRADES"
--------------------------------------------------------
An earlier version of this file called a symbol-day replayable when SOME
provider had given it trades.  That is not the consumer's definition and it
overstated coverage by six symbol-days.  ``counterfactual_replay._confidence``
returns ``("no_tape", ["no_nbbo_tape"])`` the moment the NBBO tape is empty, and
bar-candidate generation is guarded on the NBBO ticks -- not the trade ticks.
Under the default ``live_admission_mode=True`` the two tick-driven families that
could otherwise have used trade prints are deliberately skipped.  So trades
alone generate ZERO candidates: the run is not a thin replay, it is no replay.

Replayable therefore means **trades AND NBBO**, and NBBO is counted with the
loader's OWN validity predicate (``bid > 0 AND ask > 0 AND ask >= bid``) rather
than a bare row count, so a tape full of crossed or zero quotes cannot pass.  A
symbol-day with trades but no usable book is reported as
``tape_only_not_replayable`` -- a real, named tier, not a silent pass.

THE MIXED-VENDOR QUOTE SEAM
---------------------------
After canonicalization the corpus is IQFeed trades + Polygon quotes, and
``load_trade_tape`` puts ``iqfeed_trade_ticks.bid/ask`` on every tick it
returns.  So the FSM sees IQFeed's at-trade quotes on the trade tape and
Polygon's quote stream on the NBBO tape -- two vendors, same symbol-day, same
run.  That is a deliberate choice (see ``scripts/hydration_quote_seam_check.py``
for the measured disagreement), but it must be VISIBLE, so every row names both:
``trade_quote_vendor`` / ``trade_quote_derivation`` and ``nbbo_vendor``.

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


# How the bid/ask on a TRADE row came to be, per trade source. IQFeed 6.2 L1
# ships the last trade and top-of-book in one record, so its at-trade quote is a
# native measurement. Polygon splits them, so the hydrator reconstructs the
# trade-row quote by as-of merge from /v3/quotes -- same columns, different
# epistemic status, and a consumer computing a spread off a trade tick deserves
# to know which one it is holding.
TRADE_QUOTE_DERIVATION = {
    SOURCE_IQFEED_TRADES: "native_at_trade_bid_ask",
    SOURCE_POLYGON_TRADES: "as_of_merge_from_v3_quotes",
}

# The loader's own NBBO validity predicate, quoted rather than paraphrased:
# counterfactual_replay.load_nbbo_tape filters on exactly this, so a row that
# fails it is invisible to the replay no matter how many of them exist.
NBBO_VALID_PREDICATE = "bid > 0 AND ask > 0 AND ask >= bid"


def _row_counts(conn, table: str, source: str) -> dict[tuple[str, date], dict]:
    """Rows per (symbol, ET trading day) for one hydrated source.

    Bucketed by America/New_York because a trading day is an ET concept and the
    tape spans 04:00-20:00 ET, which straddles midnight UTC.  ``observed_at`` is
    naive-UTC in the trade table and aware in the NBBO table, so the cast to
    ``timestamptz`` is what makes the two comparable.

    Returns rows, the count that survives the loader's validity predicate, and
    the OBSERVED first/last tick.  The observed bounds matter because the
    per-symbol-day ``since_utc``/``until_utc`` are what was REQUESTED; only the
    observed first tick can tell you whether premarket was actually delivered.
    """
    # The NBBO tape is timestamptz, the trade tape is naive-UTC. The extra
    # "AT TIME ZONE 'UTC'" is what gives the naive column a zone before it is
    # converted; omitting it would silently bucket by server-local time.
    is_nbbo = table == NBBO_TABLE
    day_expr = (
        "(observed_at AT TIME ZONE 'America/New_York')::date"
        if is_nbbo
        else "(observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date"
    )
    valid = NBBO_VALID_PREDICATE if is_nbbo else "price > 0"
    sql = (f"SELECT symbol, {day_expr} AS d, count(*), "
           f"       count(*) FILTER (WHERE {valid}), "
           f"       min(observed_at), max(observed_at) "
           f"FROM {table} WHERE source = %s GROUP BY 1, 2")
    with conn.cursor() as cur:
        cur.execute(sql, (source,))
        out: dict[tuple[str, date], dict] = {}
        for sym, day, n, n_valid, lo, hi in cur.fetchall():
            out[(sym, day)] = {
                "rows": int(n),
                "valid": int(n_valid or 0),
                "first": _as_utc_iso(lo, aware=is_nbbo),
                "last": _as_utc_iso(hi, aware=is_nbbo),
            }
        return out


def _as_utc_iso(ts: datetime | None, *, aware: bool) -> str | None:
    """Render an observed_at as an unambiguous UTC instant.

    The trade tape's column is naive-UTC and the NBBO tape's is aware; printing
    the naive one straight through would produce a timestamp that LOOKS like a
    local time and reads four hours wrong to anyone who assumes ET.
    """
    if ts is None:
        return None
    if not aware and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


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
            got = counts[(prov, ds)].get((sym, day)) or {}
            n = int(got.get("rows", 0))
            n_valid = int(got.get("valid", 0))
            rec[f"{prov}_{ds}"] = n
            rec[f"{prov}_{ds}_usable"] = n_valid
            rec[f"{prov}_{ds}_first_utc"] = got.get("first")
            rec[f"{prov}_{ds}_last_utc"] = got.get("last")
            if n > 0:
                covered[f"{prov}_{ds}"] += 1
            status, _, err = jobs.get((sym, day, ds, prov), ("missing", 0, None))
            rec[f"{prov}_{ds}_status"] = status

        # Report holes per TABLE, not per provider-dataset. A source with zero
        # rows is only a hole if NOTHING covers that table for that symbol-day:
        # canonicalization deliberately deletes the non-preferred source, so a
        # job legitimately marked `done` whose rows were then dropped is the
        # design working, not a failure. Counting those as failures would raise
        # ~250 false alarms and bury the handful of real gaps.
        for ds, provs in (("trades", ("iqfeed", "polygon")),
                          ("nbbo", ("polygon", "iqfeed"))):
            # NBBO coverage is counted with the loader's validity predicate: a
            # tape of crossed or zero quotes is a hole, not coverage.
            key = "_usable" if ds == "nbbo" else ""
            if any(rec[f"{p}_{ds}{key}"] > 0 for p in provs):
                continue
            details = []
            for p in provs:
                st, _, err = jobs.get((sym, day, ds, p), ("missing", 0, None))
                details.append(f"{p}={st}" + (f" ({err})" if err else ""))
            label = ("no_data_from_any_provider"
                     if all(jobs.get((sym, day, ds, p), ("missing", 0, None))[0]
                            == "no_data" for p in provs)
                     else "uncovered")
            failures.append({"symbol": sym, "date": day.isoformat(),
                             "dataset": ds, "status": label,
                             "detail": "; ".join(details)})
            reasons[f"{ds}/{label}"].append(f"{sym} {day}")
        # Which vendor's quote rides on the TRADE tape, and how it got there.
        # Named per symbol-day because canonicalization already chose, the two
        # tables can legitimately land on different vendors, and a consumer
        # cannot tell from the tick rows alone.
        rec["has_trades"] = rec["iqfeed_trades"] > 0 or rec["polygon_trades"] > 0
        rec["has_nbbo"] = rec["iqfeed_nbbo_usable"] > 0 or rec["polygon_nbbo_usable"] > 0
        trade_src = (SOURCE_IQFEED_TRADES if rec["iqfeed_trades"] > 0
                     else SOURCE_POLYGON_TRADES if rec["polygon_trades"] > 0 else None)
        nbbo_src = (SOURCE_POLYGON_NBBO if rec["polygon_nbbo_usable"] > 0
                    else SOURCE_IQFEED_NBBO if rec["iqfeed_nbbo_usable"] > 0 else None)
        rec["trade_source"] = trade_src
        rec["trade_quote_vendor"] = ("iqfeed" if trade_src == SOURCE_IQFEED_TRADES
                                     else "polygon" if trade_src else None)
        rec["trade_quote_derivation"] = TRADE_QUOTE_DERIVATION.get(trade_src or "")
        rec["nbbo_source"] = nbbo_src
        rec["nbbo_vendor"] = ("polygon" if nbbo_src == SOURCE_POLYGON_NBBO
                              else "iqfeed" if nbbo_src else None)
        rec["mixed_vendor_quote_seam"] = bool(
            rec["trade_quote_vendor"] and rec["nbbo_vendor"]
            and rec["trade_quote_vendor"] != rec["nbbo_vendor"]
        )

        # THE definition. Trades alone are not replayable -- see the module
        # docstring: _confidence returns no_tape without an NBBO tape, and
        # bar-candidate generation is guarded on NBBO ticks.
        rec["replayable"] = rec["has_trades"] and rec["has_nbbo"]
        rec["tape_only_not_replayable"] = rec["has_trades"] and not rec["has_nbbo"]
        per_pair.append(rec)

    return {
        "database": dbname,
        "database_size": db_size,
        "corpus_symbol_days": len(pairs),
        "coverage_by_source": dict(covered),
        "replayable_definition":
            "trades AND NBBO, NBBO counted with the loader's own predicate "
            f"({NBBO_VALID_PREDICATE}). Trades alone yield zero candidates.",
        "replayable": sum(1 for r in per_pair if r["replayable"]),
        "not_replayable": sum(1 for r in per_pair if not r["replayable"]),
        "not_replayable_symbol_days": [f"{r['symbol']} {r['date']}" for r in per_pair
                                       if not r["replayable"]],
        "tape_only_not_replayable": [f"{r['symbol']} {r['date']}" for r in per_pair
                                     if r["tape_only_not_replayable"]],
        "has_trades": sum(1 for r in per_pair if r["has_trades"]),
        "has_nbbo": sum(1 for r in per_pair if r["has_nbbo"]),
        "mixed_vendor_quote_seam": sum(1 for r in per_pair
                                       if r["mixed_vendor_quote_seam"]),
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
