"""Reproduce [29]'s historical p99/p90 scale; no claim of labeled halt accuracy.

Each SELECT is bounded to one symbol and at most one hour. PostgreSQL enforces
read-only mode and a 20s statement timeout. Output includes each population,
not only the chosen maximum. DATABASE_URL is read privately from the environment.
"""
import json
import math
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

SYMBOLS = ("AHMA", "DBGI", "PCLA", "PSIG", "SKYQ", "TPET", "VIOT")
PERIODS = (("13:30", "14:00"), ("14:00", "15:00"), ("15:00", "16:00"),
           ("16:00", "17:00"), ("17:00", "18:00"), ("18:00", "19:00"), ("19:00", "20:00"))
QUERY = """WITH t AS (
 SELECT observed_at-lag(observed_at) OVER(ORDER BY observed_at,id) d
 FROM iqfeed_trade_ticks WHERE symbol=%s AND observed_at >= %s AND observed_at < %s
), g AS (SELECT EXTRACT(EPOCH FROM d)::float8 gap FROM t WHERE d IS NOT NULL)
SELECT count(*) n, percentile_disc(.9) WITHIN GROUP(ORDER BY gap) p90,
 percentile_disc(.99) WITHIN GROUP(ORDER BY gap) p99, max(gap) max_gap,
 count(*) FILTER(WHERE gap>0) nonzero_n FROM g"""


def main():
    rows = []
    with psycopg2.connect(os.environ["DATABASE_URL"]) as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET statement_timeout='20s'")
            for symbol in SYMBOLS:
                for start, end in PERIODS:
                    cur.execute(QUERY, (symbol, "2026-09-10 " + start, "2026-09-10 " + end))
                    rows.append(dict(symbol=symbol, start=start, end=end, **cur.fetchone()))
    included = [row for row in rows if row["n"] >= 200 and row["p90"] and row["p90"] > 0]
    ratios = sorted(row["p99"] / row["p90"] for row in included)
    out = {"rows": rows, "query": QUERY, "n_symbol_periods": len(included),
           "ratio_max": max(ratios) if ratios else None,
           "ratio_p50": ratios[math.ceil(.5 * len(ratios)) - 1] if ratios else None,
           "periods_with_max_gap_above_7_82_p90": sum(row["max_gap"] > 7.82 * row["p90"] for row in included),
           "caveat": "Unlabeled event-time history, includes zero gaps; production uses nonzero p90. "
                     "p99 is not max and does not guarantee ordinary-gap coverage. "
                     "Neither this scale nor eventual rows prove causal consumer visibility."}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
