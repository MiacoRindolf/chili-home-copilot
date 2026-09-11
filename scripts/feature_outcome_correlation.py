"""Is there a relationship at all between the tape features and the outcome?

WHY THIS AND NOT A REFUSAL COUNT. Counting how many winners and losers a
THRESHOLD would refuse measures one threshold on one sample. It answers "does 0.75
separate?" when the question is "does this feature carry any information about the
outcome, and if so, where does it change?" A threshold-free discrimination measure
answers that directly: AUC 0.5 means the feature knows nothing and NO threshold can
help; AUC far from 0.5 means it does, and the operating point can then be derived
instead of guessed.

WHY CLUSTERING MATTERS. Legs are not independent observations. VEEE ml1/ml2/ml3 are
three windows of ONE move on ONE day; legs inside a session share a symbol, a
setup and a recycle. Treating 70 legs as 70 samples overstates the evidence by the
cluster size. Every statistic here is therefore reported twice: once per leg, and
once per SYMBOL-DAY with the legs averaged inside each cluster first. The
symbol-day figure is the honest one.

Features are computed by the REAL production helper, `l2_as_of` pinned to the fill
instant. Receipt/publication filtering reconstructs recorded eligibility; the
marker is not exact commit visibility or proof of the consumer's input prefix.

THE UNIT MATTERS ([29], 2026-09-10). The earlier 0.717 / 0.671 AUC claim did not
replicate: the event-only 82-entry/35-day audit gave print 0.496 / 0.645 versus
seconds 0.545 / 0.607. Those historical readings do not validate this stricter
recorded-publication selector. This script reads the same print window as the new entry gates
(`window_prints`, default `chili_momentum_tape_window_prints`), so the number and
the decision are measured in the same unit. `--window-s` re-runs the old seconds
form for a side-by-side.

    python scripts/feature_outcome_correlation.py --since 2026-06-01

Read-only. No IQFeed.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text          # noqa: E402
from sqlalchemy.orm import sessionmaker             # noqa: E402

from app.services.trading.momentum_neural.entry_gates import (  # noqa: E402
    signed_tape_accel_features,
)

_SQL = """
SELECT e.ts, s.symbol, e.ts::date AS d,
       coalesce(o.broker_realized_pnl_usd, o.realized_pnl_usd) AS pnl
FROM trading_automation_events e
JOIN trading_automation_sessions s ON s.id = e.session_id
JOIN momentum_automation_outcomes o ON o.session_id = s.id
WHERE s.mode = 'live' AND e.event_type = 'live_entry_filled' AND o.mode = 'live'
  AND coalesce(o.broker_realized_pnl_usd, o.realized_pnl_usd) IS NOT NULL
  AND (:since IS NULL OR e.ts >= CAST(:since AS timestamp))
ORDER BY e.ts DESC
LIMIT :lim
"""

FEATURES = ("high_print_position", "buy_share_delta", "signed_tape_accel",
            "tick_rate", "prints_since_high", "n_ticks")


def auc(pos: list[float], neg: list[float]) -> float | None:
    """Mann-Whitney: P(a random winner scores above a random loser), ties at 0.5."""
    if not pos or not neg:
        return None
    wins = 0.0
    for a in pos:
        for b in neg:
            wins += 1.0 if a > b else (0.5 if a == b else 0.0)
    return wins / (len(pos) * len(neg))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--since", default=None)
    ap.add_argument("--limit", type=int, default=600)
    ap.add_argument("--prints", type=int, default=None,
                    help="tape window in PRINTS (default: "
                         "settings.chili_momentum_tape_window_prints, the live binding)")
    ap.add_argument("--window-s", type=float, default=None,
                    help="re-run the legacy SECONDS window instead (the broken unit; "
                         "kept for a side-by-side)")
    args = ap.parse_args()
    if not args.database_url:
        print("DATABASE_URL is required.", file=sys.stderr)
        return 2

    if args.window_s is not None:
        win = {"window_s": float(args.window_s)}
        print(f"tape window                 : {args.window_s} SECONDS (legacy unit)")
    else:
        from app.config import settings

        n_prints = int(args.prints or getattr(
            settings, "chili_momentum_tape_window_prints", 255) or 255)
        win = {"window_prints": n_prints}
        print(f"tape window                 : last {n_prints} PRINTS "
              f"(chili_momentum_tape_window_prints)")

    eng = create_engine(args.database_url, pool_pre_ping=True)
    Session = sessionmaker(bind=eng)
    with Session() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout='20s'"))
        rows = db.execute(text(_SQL),
                          {"since": args.since, "lim": args.limit}).fetchall()
    print(f"entry fills with an outcome : {len(rows)}")

    obs, unreadable = [], 0
    with Session() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout='20s'"))
        for ts, symbol, d, pnl in rows:
            f = None
            try:
                f = signed_tape_accel_features(symbol, db=db, as_of=ts, **win)
            except Exception:
                f = None
            if not f:
                unreadable += 1
                continue
            obs.append({"sym": symbol, "day": str(d), "won": float(pnl) > 0,
                        **{k: f.get(k) for k in FEATURES}})
    print(f"tape readable               : {len(obs)}   (unreadable {unreadable})")
    if not obs:
        print("\nNo readable tape. Tick retention bounds this; the bench is the "
              "instrument with a preserved corpus.")
        return 0

    clusters = defaultdict(list)
    for o in obs:
        clusters[(o["sym"], o["day"])].append(o)
    nW = sum(1 for o in obs if o["won"]); nL = len(obs) - nW
    cW = sum(1 for v in clusters.values() if any(x["won"] for x in v))
    print(f"legs                        : {nW} panalo / {nL} talo")
    print(f"symbol-days (ANG TUNAY NA n): {len(clusters)}  ({cW} may panalo)\n")

    print(f"{'feature':<22}{'AUC/leg':>9}{'AUC/araw':>10}   basa")
    print("-" * 62)
    for k in FEATURES:
        p = [o[k] for o in obs if o["won"] and o.get(k) is not None]
        n = [o[k] for o in obs if not o["won"] and o.get(k) is not None]
        a_leg = auc(p, n)
        cp, cn = [], []
        for v in clusters.values():
            vals = [x[k] for x in v if x.get(k) is not None]
            if not vals:
                continue
            (cp if any(x["won"] for x in v) else cn).append(sum(vals) / len(vals))
        a_day = auc(cp, cn)
        def fmt(a):
            return "  n/a  " if a is None else f"{a:6.3f}"
        note = ("walang alam" if a_leg is not None and abs(a_leg - 0.5) < 0.10
                else "may senyales" if a_leg is not None else "")
        print(f"{k:<22}{fmt(a_leg):>9}{fmt(a_day):>10}   {note}")

    print("\n  AUC 0.5 = ang feature ay WALANG alam tungkol sa kinalabasan; walang")
    print("  hangganan dito ang makakatulong. Malayo sa 0.5 = may impormasyon, at")
    print("  doon dapat i-derive ang operating point.")
    print("  Ang AUC/araw ang tapat na bilang: pinagsasama muna ang magkakaugnay na leg.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
