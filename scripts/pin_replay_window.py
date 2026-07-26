"""Pin a replay window into the pruner-immune golden archive — the same-day habit tool.

The 30d bridge pruner (iqfeed_trade_ticks) and the 6h scheduler NBBO prune (5d for
universe-class rows) age live tape out; the canonical JEM 2026-07-09 window was lost
exactly this way, and CLRO 2026-07-02 was six days from deletion when the archive was
built (2026-07-25). THE HABIT: the day any window becomes canonical (a session worth
replaying forever), run this ONCE:

    python scripts/pin_replay_window.py CLRO 2026-07-02
    python scripts/pin_replay_window.py QTTB 2026-07-13 --dump E:/chili-backups

It copies the symbol-day's rows into ``replay_golden_ticks`` / ``replay_golden_nbbo``
(created on first use with LIKE...INCLUDING ALL — indexes preserved; original ``id``
values preserved so the replay driver's tie-stable ORDER BY (observed_at, id) reproduces
the live-table replay TO THE CENT — proven 2026-07-25), idempotently (NOT EXISTS on id).
``--dump`` refreshes a compressed pg_dump of both golden tables for disaster recovery.

The replay driver reads the archive with GOLDEN=1 (scripts/replay_ab_dark_flags.py).
READ-ONLY on the live tables; writes only to the golden tables (+ the dump file).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

DB_URL = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")

DDL = """
CREATE TABLE IF NOT EXISTS replay_golden_ticks (LIKE iqfeed_trade_ticks INCLUDING ALL);
CREATE TABLE IF NOT EXISTS replay_golden_nbbo (LIKE momentum_nbbo_spread_tape INCLUDING ALL);
"""

COPY_TICKS = """
INSERT INTO replay_golden_ticks
SELECT * FROM iqfeed_trade_ticks t
 WHERE t.symbol = %(sym)s AND t.observed_at >= %(day)s::date
   AND t.observed_at < (%(day)s::date + 1)
   AND NOT EXISTS (SELECT 1 FROM replay_golden_ticks g WHERE g.id = t.id)
"""

COPY_NBBO = """
INSERT INTO replay_golden_nbbo
SELECT * FROM momentum_nbbo_spread_tape n
 WHERE n.symbol = %(sym)s AND n.observed_at >= %(day)s::date
   AND n.observed_at < (%(day)s::date + 1)
   AND NOT EXISTS (SELECT 1 FROM replay_golden_nbbo g WHERE g.id = n.id)
"""

COUNTS = """
SELECT (SELECT count(*) FROM replay_golden_ticks WHERE symbol = %(sym)s
         AND observed_at >= %(day)s::date AND observed_at < (%(day)s::date + 1)),
       (SELECT count(*) FROM replay_golden_nbbo WHERE symbol = %(sym)s
         AND observed_at >= %(day)s::date AND observed_at < (%(day)s::date + 1))
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbol")
    ap.add_argument("day", help="YYYY-MM-DD (UTC day of the window)")
    ap.add_argument("--dump", metavar="DIR", default=None,
                    help="refresh a compressed pg_dump of BOTH golden tables into DIR")
    args = ap.parse_args()
    sym = args.symbol.strip().upper()

    import psycopg2

    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(COPY_TICKS, {"sym": sym, "day": args.day})
            new_ticks = cur.rowcount
            cur.execute(COPY_NBBO, {"sym": sym, "day": args.day})
            new_nbbo = cur.rowcount
            cur.execute(COUNTS, {"sym": sym, "day": args.day})
            total_ticks, total_nbbo = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    print(f"[pin] {sym} {args.day}: +{new_ticks} ticks / +{new_nbbo} nbbo "
          f"(archive totals for the window: {total_ticks} / {total_nbbo})")
    if total_ticks == 0:
        print("[pin] WARNING: zero ticks in the archive for this window — "
              "the live rows may already be pruned; nothing was preserved")

    if args.dump:
        os.makedirs(args.dump, exist_ok=True)
        out = os.path.join(args.dump, "golden_windows.dump")
        # pg_dump ONLY the golden tables, compressed; -h/-p/-U parsed from DB_URL is
        # overkill here — rely on the URL via the standard env (pg_dump honours it).
        env = dict(os.environ)
        cmd = ["pg_dump", "--dbname", DB_URL,
               "-t", "replay_golden_ticks", "-t", "replay_golden_nbbo",
               "-Fc", "-f", out]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[pin] dump FAILED: {r.stderr.strip()[:300]}", file=sys.stderr)
            return 1
        print(f"[pin] dump refreshed: {out} ({os.path.getsize(out):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
