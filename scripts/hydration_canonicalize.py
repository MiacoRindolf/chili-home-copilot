"""Enforce ONE tape source per (symbol, trading day, table) in the hydrated DB.

THE DEFECT THIS EXISTS TO PREVENT
---------------------------------
``counterfactual_replay.load_trade_tape`` / ``load_nbbo_tape`` filter, in
non-strict mode, on **symbol and time only**:

    WHERE symbol = :symbol AND observed_at >= :since AND observed_at < :until
      AND price > 0

There is no ``source`` predicate.  So if one symbol-day is hydrated from two
providers, the replay reads **both tapes concatenated** and every print appears
twice.  Measured on TMCR 2026-08-24, which Phase 2 loaded from both providers:
the table held 16,933 ``iqfeed_lookup_hist`` rows and 16,933
``polygon_v3_trades`` rows, and ``load_trade_tape`` returned **33,866** ticks --
double the prints, double the volume, every price repeated back to back.

That is silent.  The rows are individually valid, the timestamps are right, and
nothing in the tape looks malformed; only the counts are wrong.  A momentum
counterfactual run on it would see fabricated volume and a doubled tape speed at
exactly the moments it cares about most.

THE FIX IS STRUCTURAL, NOT DOCUMENTARY
--------------------------------------
Phase 2's design principle was that provenance is enforced by the query rather
than by convention, because a convention is a thing a future edit forgets.  The
same standard applies here: rather than write "do not hydrate both providers"
in a runbook, this makes the hydrated database hold exactly one source per
symbol-day per table, and gives ``--check`` so the invariant can be asserted
before a study trusts the corpus.

PREFERENCE ORDER (implements the Phase 3 verdict)
-------------------------------------------------
* **trades**: ``iqfeed_lookup_hist`` > ``polygon_v3_trades``.  Phase 3 measured
  IQFeed as the closer read of the feed our own bridge consumes -- where the two
  providers disagree, IQFeed is the one that also matches our recording.
* **NBBO**: ``polygon_v3_quotes`` > ``iqfeed_lookup_bbo``.  IQFeed lookup
  exposes no historical quote *stream*, only the quote attached to each print,
  so an IQFeed NBBO tape cannot represent a quote that moves between trades --
  which is precisely what spread floors and stale-BBO vetoes read.

The lower-preference source is a **fallback, not a duplicate**: it is kept when
the preferred source has no rows for that symbol-day, which is real (IQFeed
returned no top-of-book at all for the OTC names REEMF 2026-08-19 and NLST
2026-08-20, whose trades loaded fine).  Coverage is preserved; duplication is
not.

Dropping the non-preferred copy loses nothing durable -- the hydrator is
idempotent and a cross-check copy is one command away:

    python scripts/historical_tick_hydrator.py --db-name chili_hydrated_xcheck ...

USAGE
-----
    python scripts/hydration_canonicalize.py --check   # assert; exit 1 if violated
    python scripts/hydration_canonicalize.py --apply   # enforce it
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Any

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
    et_day_bounds_utc,
    naive_utc,
)

# table -> (ET-day expression, preference order, best first)
TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    TRADES_TABLE: (
        "(observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date",
        (SOURCE_IQFEED_TRADES, SOURCE_POLYGON_TRADES),
    ),
    NBBO_TABLE: (
        "(observed_at AT TIME ZONE 'America/New_York')::date",
        (SOURCE_POLYGON_NBBO, SOURCE_IQFEED_NBBO),
    ),
}


def survey(conn, table: str) -> list[tuple[str, Any, str, int]]:
    """(symbol, ET day, source, rows) for every hydrated source in one table."""
    day_expr, prefs = TABLES[table]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT symbol, {day_expr} AS d, source, count(*) "
            f"FROM {table} WHERE source = ANY(%s) GROUP BY 1, 2, 3",
            (list(prefs),),
        )
        return [(s, d, src, int(n)) for s, d, src, n in cur.fetchall()]


def plan(rows: list[tuple[str, Any, str, int]], prefs: tuple[str, ...]) -> list[dict]:
    """Which (symbol, day, source) slices lose to a higher-preference source."""
    by_day: dict[tuple[str, Any], dict[str, int]] = {}
    for sym, day, src, n in rows:
        by_day.setdefault((sym, day), {})[src] = n

    drops = []
    for (sym, day), sources in sorted(by_day.items(), key=lambda kv: (str(kv[0][1]), kv[0][0])):
        present = [s for s in prefs if sources.get(s, 0) > 0]
        if len(present) <= 1:
            continue  # already canonical, or a fallback standing alone
        keep = present[0]
        for loser in present[1:]:
            drops.append({"symbol": sym, "day": str(day), "keep": keep,
                          "drop": loser, "rows": sources[loser],
                          "kept_rows": sources[keep]})
    return drops


def apply_drops(conn, table: str, drops: list[dict]) -> int:
    """Delete the non-preferred slices, one symbol-day at a time.

    Per-slice rather than one giant statement so a failure leaves a consistent
    database and can be resumed, and so the lock stays short while a load may be
    running against the same table.

    The predicate is ``(source, symbol, observed_at range)`` -- deliberately NOT
    the ET-day expression this module groups by.  Both tables carry a
    ``(source, symbol, observed_at)`` btree; a delete keyed on
    ``(observed_at AT TIME ZONE ...)::date`` cannot use it and would sequentially
    scan a multi-million-row table once per slice.  ``et_day_bounds_utc`` gives
    exactly the same rows as that expression, by construction.
    """
    naive = table == TRADES_TABLE  # trade tape is naive-UTC; NBBO tape is tz-aware
    removed = 0
    for d in drops:
        lo, hi = et_day_bounds_utc(date.fromisoformat(d["day"]))
        if naive:
            lo, hi = naive_utc(lo), naive_utc(hi)
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {table} WHERE source = %s AND symbol = %s "
                "AND observed_at >= %s AND observed_at < %s",
                (d["drop"], d["symbol"], lo, hi),
            )
            removed += cur.rowcount
        conn.commit()
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-name", default=DEFAULT_HYDRATED_DB)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="delete the non-preferred slices (default is check-only)")
    ap.add_argument("--check", action="store_true",
                    help="explicitly assert the invariant; exit 1 if violated "
                         "(this is also the default when --apply is absent)")
    ap.add_argument("--json", help="write the full plan here")
    args = ap.parse_args(argv)

    if args.check and args.apply:
        ap.error("--check and --apply are mutually exclusive: --check asserts, "
                 "--apply mutates")

    conn = connect(args.db_name, args.env_file)
    report: dict[str, Any] = {"database": args.db_name, "applied": bool(args.apply),
                              "tables": {}}
    violations = 0
    try:
        for table, (_, prefs) in TABLES.items():
            rows = survey(conn, table)
            drops = plan(rows, prefs)
            violations += len(drops)
            entry: dict[str, Any] = {
                "preference": list(prefs),
                "symbol_days_with_multiple_sources": len(drops),
                "rows_that_would_be_dropped": sum(d["rows"] for d in drops),
                "drops": drops,
            }
            if args.apply and drops:
                entry["rows_deleted"] = apply_drops(conn, table, drops)
                after = plan(survey(conn, table), prefs)
                entry["violations_after"] = len(after)
            report["tables"][table] = entry
    finally:
        conn.close()

    report["violations"] = violations
    if args.json:
        with open(args.json, "w", newline="", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)

    brief = {
        "database": report["database"],
        "applied": report["applied"],
        "violations": violations,
        "tables": {t: {k: v for k, v in e.items() if k != "drops"}
                   for t, e in report["tables"].items()},
    }
    print(json.dumps(brief, indent=2, default=str))
    # Check mode fails loudly so this can gate a study.
    if violations and not args.apply:
        return 1
    if args.apply:
        return 1 if any(e.get("violations_after") for e in report["tables"].values()) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
