"""Would the entry L2 confirmer have refused these entries, if it were switched on?

The confirmer binds OFF in production (`chili_momentum_l2_confirm_enabled` is
False and absent from the lane's .env), so every entry passes through a
short-circuit that returns "confirm" before reading anything. On 2026-09-08 the
lane entered WYHG eight times in thirty minutes for -$212.83, and a tape probe
showed seven of those entries carried `signed_tape_accel <= 0` at the instant of
entry.

That is NOT yet an argument for flipping the flag, because the confirmer defers
only on a CONJUNCTION -- `accel <= 0 AND ofi < 0` -- and OFI comes from the depth
book, not the trade tape. This script settles it by running the REAL
`_l2_entry_confirm` against the real stored data with `l2_as_of` pinned to each
entry instant, with the flag forced on and nothing else changed. The answer is
the production computation, not an estimate of it.

    python scripts/l2_confirm_counterfactual.py --symbol WYHG --date 2026-09-08 \
        --at 08:41:02 --at 08:43:20

Read-only. No IQFeed. Bounded reads against the live database through the same
helpers the lane uses.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings as _real_settings  # noqa: E402
from app.services.trading.momentum_neural.entry_gates import (  # noqa: E402
    _l2_entry_confirm,
    _l2_entry_veto,
)


class _Forced:
    """The real settings, with only the two kill switches flipped on.

    Nothing else is touched: every threshold, floor and window keeps its binding
    production value, so the verdict below is what the lane would have produced
    had the flags been on and nothing else been different.
    """

    def __init__(self, **overrides):
        self._o = overrides

    def __getattr__(self, name):
        if name in self._o:
            return self._o[name]
        return getattr(_real_settings, name)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--date", required=True, help="UTC date, YYYY-MM-DD")
    ap.add_argument("--at", action="append", required=True,
                    help="UTC entry time HH:MM:SS (repeatable)")
    args = ap.parse_args()
    if not args.database_url:
        print("DATABASE_URL is required.", file=sys.stderr)
        return 2

    forced = _Forced(
        chili_momentum_l2_confirm_enabled=True,
        chili_momentum_entry_l2_veto_enabled=True,
    )
    print(f"binding in production : l2_confirm="
          f"{bool(getattr(_real_settings, 'chili_momentum_l2_confirm_enabled', False))}"
          f"  l2_veto="
          f"{bool(getattr(_real_settings, 'chili_momentum_entry_l2_veto_enabled', False))}")
    print("forced for this probe : both True, every other setting untouched\n")

    eng = create_engine(args.database_url, pool_pre_ping=True)
    Session = sessionmaker(bind=eng)

    hdr = (f"{'entry (UTC)':<13} {'confirm':>8}  {'reason':<28} "
           f"{'accel':>10} {'ofi':>7} {'micro':>7} {'snaps':>6}  veto")
    print(hdr)
    print("-" * len(hdr))

    deferred = 0
    vetoed = 0
    with Session() as db:
        db.execute(text("SET statement_timeout = '20s'"))
        for t in args.at:
            as_of = datetime.strptime(f"{args.date} {t}", "%Y-%m-%d %H:%M:%S")
            decision, dbg = _l2_entry_confirm(
                args.symbol, db=db, le={}, settings=forced, l2_as_of=as_of,
            )
            veto = _l2_entry_veto(
                args.symbol, db=db, l2_as_of=as_of, is_ssr=None,
            )
            if decision == "defer":
                deferred += 1
            if veto is not None:
                vetoed += 1

            def num(k, nd=2):
                v = dbg.get(k)
                return "—" if v is None else f"{float(v):.{nd}f}"

            print(f"{t:<13} {decision:>8}  {str(dbg.get('reason'))[:28]:<28} "
                  f"{num('signed_tape_accel', 0):>10} {num('ofi', 3):>7} "
                  f"{num('micro_edge', 3):>7} {str(dbg.get('n_snaps', '—')):>6}  "
                  f"{'—' if veto is None else veto[0]}")

    n = len(args.at)
    print(f"\nconfirmer would have DEFERRED {deferred}/{n} of these entries")
    print(f"big/hidden-seller veto would have REFUSED {vetoed}/{n}")
    if deferred == 0:
        print("\n⇒ Flipping the flag alone would NOT have stopped these entries. The defer "
              "rule is a CONJUNCTION (accel <= 0 AND ofi < 0); whichever leg held here, the "
              "other did not. Read the ofi column before proposing the flip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
