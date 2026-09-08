"""Does the entry confirmer's predicate separate winners from losers, or just refuse?

A defer rule is only worth having if it refuses losers at a materially higher rate
than it refuses winners. Judged on eight entries from one symbol on one day, any
rule can look good or bad by accident — and this codebase has paid for that
mistake repeatedly. This sweeps every live entry in the book instead.

For each entry fill it runs the REAL `_l2_entry_confirm` with `l2_as_of` pinned to
the fill instant and the kill switch forced on, then buckets the verdict against
what that leg actually earned. The output is the only number that decides a
predicate:

    winners deferred   (the cost)   vs   losers deferred   (the benefit)

    python scripts/l2_predicate_sweep.py --since 2026-08-01
    python scripts/l2_predicate_sweep.py --since 2026-08-01 --late 0.6 --spent 0.85

Read-only. No IQFeed. Bounded reads against the live database.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings as _real_settings  # noqa: E402
from app.services.trading.momentum_neural.entry_gates import (  # noqa: E402
    _l2_entry_confirm,
)

# Entry fills joined to the P&L of the outcome that closed that session. The
# recycle means a session can hold several legs under one outcome row, so the P&L
# is attributed per session, not per leg — stated here rather than hidden, and the
# leg count per session is reported so the reader can see how much of the sample
# that affects.
_SQL = """
SELECT e.ts, s.symbol,
       coalesce(o.broker_realized_pnl_usd, o.realized_pnl_usd) AS pnl,
       coalesce((s.risk_snapshot_json->'momentum_live_execution'->>'trade_cycles')::int, 1)
FROM trading_automation_events e
JOIN trading_automation_sessions s ON s.id = e.session_id
JOIN momentum_automation_outcomes o ON o.session_id = s.id
WHERE s.mode = 'live'
  AND e.event_type = 'live_entry_filled'
  AND o.mode = 'live'
  AND coalesce(o.broker_realized_pnl_usd, o.realized_pnl_usd) IS NOT NULL
  AND (:since IS NULL OR e.ts >= CAST(:since AS timestamp))
ORDER BY e.ts DESC
LIMIT :lim
"""


class _Forced:
    def __init__(self, **o):
        self._o = o

    def __getattr__(self, name):
        if name in self._o:
            return self._o[name]
        return getattr(_real_settings, name)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--since", default=None, help="UTC date, YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--late", type=float, default=None,
                    help="override the late-arrival print position")
    ap.add_argument("--spent", type=float, default=None,
                    help="override the spent-move print position")
    args = ap.parse_args()
    if not args.database_url:
        print("DATABASE_URL is required.", file=sys.stderr)
        return 2

    over = {"chili_momentum_l2_confirm_enabled": True}
    if args.late is not None:
        over["chili_momentum_l2_confirm_late_arrival_position"] = args.late
    if args.spent is not None:
        over["chili_momentum_l2_confirm_spent_position"] = args.spent
    forced = _Forced(**over)
    late = args.late if args.late is not None else getattr(
        _real_settings, "chili_momentum_l2_confirm_late_arrival_position", 0.5)
    spent = args.spent if args.spent is not None else getattr(
        _real_settings, "chili_momentum_l2_confirm_spent_position", 0.75)
    print(f"late_arrival_position={late}  spent_position={spent}\n")

    eng = create_engine(args.database_url, pool_pre_ping=True)
    Session = sessionmaker(bind=eng)
    rows = []
    with Session() as db:
        db.execute(text("SET statement_timeout = '60s'"))
        rows = db.execute(text(_SQL),
                          {"since": args.since, "lim": args.limit}).fetchall()
    print(f"entry fills with a P&L: {len(rows)}")

    win_defer = win_ok = los_defer = los_ok = 0
    win_pnl_defer = los_pnl_defer = 0.0
    reasons_w: Counter = Counter()
    reasons_l: Counter = Counter()
    unreadable = 0

    with Session() as db:
        db.execute(text("SET statement_timeout = '30s'"))
        for ts, symbol, pnl, cycles in rows:
            try:
                decision, dbg = _l2_entry_confirm(
                    symbol, db=db, le={}, settings=forced, l2_as_of=ts,
                )
            except Exception:
                unreadable += 1
                continue
            reason = str(dbg.get("reason") or "")
            deferred = decision == "defer"
            if float(pnl) > 0:
                reasons_w[reason] += 1
                if deferred:
                    win_defer += 1
                    win_pnl_defer += float(pnl)
                else:
                    win_ok += 1
            else:
                reasons_l[reason] += 1
                if deferred:
                    los_defer += 1
                    los_pnl_defer += float(pnl)
                else:
                    los_ok += 1

    nw, nl = win_defer + win_ok, los_defer + los_ok
    print(f"unreadable: {unreadable}\n")
    print(f"{'':<12}{'legs':>7}{'deferred':>10}{'rate':>8}{'P&L deferred':>15}")
    print("-" * 52)
    if nw:
        print(f"{'winners':<12}{nw:>7}{win_defer:>10}{win_defer / nw:>8.1%}"
              f"{win_pnl_defer:>15,.2f}")
    if nl:
        print(f"{'losers':<12}{nl:>7}{los_defer:>10}{los_defer / nl:>8.1%}"
              f"{los_pnl_defer:>15,.2f}")
    if nw and nl:
        edge = (los_defer / nl) - (win_defer / nw)
        print(f"\nseparation (loser rate - winner rate): {edge:+.1%}")
        if edge <= 0:
            print("⇒ The rule refuses winners at least as often as losers. It is not a "
                  "filter, it is a tax. Do not ship it.")
        else:
            print(f"⇒ Refuses {edge:.1%} more of the losers than of the winners. "
                  f"Net P&L of what it would have skipped: "
                  f"{-(win_pnl_defer + los_pnl_defer):+,.2f} kept.")

    for name, c in (("winners", reasons_w), ("losers", reasons_l)):
        if not c:
            continue
        print(f"\nreasons on {name}:")
        for r, n in c.most_common(8):
            print(f"  {r:<36} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
