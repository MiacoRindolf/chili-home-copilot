"""Was this loser a winner first? Ask IQFeed directly; store nothing.

WHY THIS AND NOT THE HYDRATOR
The hydrator refuses inside 04:00-20:00 ET, and its own words say why: the
hydrated database is a separate DATABASE but not a separate CLUSTER — one
postmaster, one WAL, one 4 GB shared_buffers, one bind mount, shared with the
live 222 GB `chili`. A multi-million-row COPY evicts the live lane's buffers
while it is trading.

But that is a POSTGRES constraint, not an IQFeed one. This probe answers a POINT
question — the highest print between one entry and one exit — so it needs no
store at all: it asks :9100, keeps the maximum, and forgets the ticks. Nothing is
written anywhere. It is therefore safe to run while the lane trades, which is
exactly when the question tends to get asked.

The lookup client carries its own interlock (`assert_lane_clients_present`): it
refuses to run unless the live bridges are holding the streaming ports, because
otherwise this client could become IQConnect's last one and take the lane's feed
down when it disconnects. That check is honoured here, not bypassed.

    python scripts/loser_mfe_probe.py --limit 40
    python scripts/loser_mfe_probe.py --since 2026-08-01 --json out.json

Read-only against Postgres; read-only against IQFeed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, text  # noqa: E402

from iqfeed_lookup_client import (  # noqa: E402
    IQFeedLookupClient,
    assert_lane_clients_present,
)

# IQFeed wants CCYYMMDD HHmmSS in EASTERN time; the book stores UTC.
_ET_OFFSET_HOURS = -4  # US/Eastern DST — the window this book covers

_SQL = """
SELECT o.id, o.symbol, o.exit_reason, o.hold_seconds, o.terminal_at,
       (s.risk_snapshot_json->'momentum_live_execution'->>'last_exit_entry_price')::numeric AS entry_px,
       (s.risk_snapshot_json->'momentum_live_execution'->>'structural_stop_price')::numeric AS stop_px,
       coalesce(o.broker_realized_pnl_usd, o.realized_pnl_usd) AS pnl
FROM momentum_automation_outcomes o
JOIN trading_automation_sessions s ON s.id = o.session_id
WHERE o.mode = 'live'
  AND o.exit_reason IN ('bailout', 'trail_stop', 'stop')
  AND o.hold_seconds BETWEEN 1 AND 7200
  AND (s.risk_snapshot_json->'momentum_live_execution'->>'last_exit_entry_price') IS NOT NULL
  AND (:since IS NULL OR o.terminal_at >= CAST(:since AS timestamp))
ORDER BY o.terminal_at DESC
LIMIT :lim
"""


def _iq_ts(dt) -> str:
    return (dt + timedelta(hours=_ET_OFFSET_HOURS)).strftime("%Y%m%d %H%M%S")


def _peak_from(lines: list[str]) -> float | None:
    """Highest trade print in the response. Line shape: LH,ts,price,size,..."""
    peak = None
    for line in lines:
        parts = line.split(",")
        if len(parts) < 4 or parts[0] != "LH":
            continue
        try:
            px = float(parts[2])
        except (TypeError, ValueError):
            continue
        if px > 0 and (peak is None or px > peak):
            peak = px
    return peak


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--since", default=None)
    ap.add_argument("--max-datapoints", type=int, default=15000)
    ap.add_argument("--json", default=None, help="write the per-trade rows here")
    ap.add_argument(
        "--after-minutes", type=float, default=0.0,
        help="also measure the peak in [exit, exit+N min] — what the move did "
             "AFTER we got out. This is the 'left on the table' number.")
    args = ap.parse_args()
    if not args.database_url:
        print("DATABASE_URL is required.", file=sys.stderr)
        return 2

    # The lane must own the streaming ports before we touch IQConnect at all.
    lane = assert_lane_clients_present()
    print(f"lane interlock  : OK ({lane})")

    eng = create_engine(args.database_url, pool_pre_ping=True)
    with eng.connect() as cx:
        cx.execute(text("SET statement_timeout = '60s'"))
        rows = cx.execute(text(_SQL), {"since": args.since, "lim": args.limit}).fetchall()
    print(f"losing trades   : {len(rows)}")

    out, no_data, errors = [], 0, 0
    with IQFeedLookupClient() as client:
        for oid, symbol, reason, hold, exit_at, entry_px, stop_px, pnl in rows:
            entry_at = exit_at - timedelta(seconds=float(hold))
            try:
                res = client.htt(symbol, _iq_ts(entry_at), _iq_ts(exit_at),
                                 max_datapoints=args.max_datapoints)
            except Exception as exc:                       # noqa: BLE001
                errors += 1
                print(f"  {symbol:<6} ERROR {type(exc).__name__}: {exc}")
                continue
            if res.error or res.no_data:
                no_data += 1
                continue
            peak = _peak_from(res.lines)
            if peak is None:
                no_data += 1
                continue
            e = float(entry_px)
            r = (e - float(stop_px)) if stop_px is not None else None
            row = {
                "outcome_id": int(oid), "symbol": symbol, "exit_reason": reason,
                "entry_px": e, "peak_px": peak, "stop_px": (float(stop_px) if stop_px else None),
                "peak_pct": round((peak / e - 1.0) * 100.0, 3),
                "peak_r": (round((peak - e) / r, 2) if r and r > 0 else None),
                "pnl": (float(pnl) if pnl is not None else None),
                "ticks": res.n_records,
            }
            # What the move did AFTER we got out: the spike we were not in for.
            if args.after_minutes > 0:
                after_end = exit_at + timedelta(minutes=args.after_minutes)
                try:
                    res2 = client.htt(symbol, _iq_ts(exit_at), _iq_ts(after_end),
                                      max_datapoints=args.max_datapoints)
                    post = None if (res2.error or res2.no_data) else _peak_from(res2.lines)
                except Exception:                          # noqa: BLE001
                    post = None
                if post is not None:
                    row["post_peak_px"] = post
                    row["post_peak_pct_from_entry"] = round((post / e - 1.0) * 100.0, 3)
                    row["post_peak_r"] = (round((post - e) / r, 2) if r and r > 0 else None)
            out.append(row)

    green = [x for x in out if x["peak_pct"] > 0]
    two_pct = [x for x in out if x["peak_pct"] >= 2.0]
    with_r = [x for x in out if x["peak_r"] is not None]
    at_1r = [x for x in with_r if x["peak_r"] >= 1.0]
    at_target = [x for x in with_r if x["peak_r"] >= 2.5]

    print(f"\nmeasured        : {len(out)}   no data: {no_data}   errors: {errors}")
    if out:
        print(f"  ever green    : {len(green)}/{len(out)}")
        print(f"  peak >= +2%   : {len(two_pct)}/{len(out)}")
    if with_r:
        print(f"  reached 1R    : {len(at_1r)}/{len(with_r)}")
        print(f"  reached 2.5R  : {len(at_target)}/{len(with_r)}   <- winners given back")
        print(f"  best R seen   : {max(x['peak_r'] for x in with_r)}")
    post = [x for x in out if x.get("post_peak_pct_from_entry") is not None]
    if post:
        ran_on = [x for x in post
                  if x["post_peak_pct_from_entry"] > x["peak_pct"] + 0.25]
        big = [x for x in post if x["post_peak_pct_from_entry"] >= 5.0]
        print(f"\nAFTER THE EXIT (+{args.after_minutes:g} min), n={len(post)}")
        print(f"  kept running  : {len(ran_on)}/{len(post)}   "
              f"(post-exit peak beat the in-trade peak by >0.25pt)")
        print(f"  reached +5%   : {len(big)}/{len(post)} measured from OUR entry")
        for x in sorted(post, key=lambda z: -z["post_peak_pct_from_entry"])[:10]:
            print(f"  {x['symbol']:<6} {x['exit_reason']:<11} entry={x['entry_px']:<9} "
                  f"in-trade {x['peak_pct']:>6.2f}%  ->  AFTER {x['post_peak_pct_from_entry']:>7.2f}%"
                  f"   pnl={x['pnl']}")
    for x in sorted(out, key=lambda z: -(z["peak_r"] or 0))[:12]:
        print(f"  {x['symbol']:<6} {x['exit_reason']:<11} entry={x['entry_px']:<8} "
              f"peak={x['peak_px']:<8} +{x['peak_pct']:>6.2f}%  "
              f"R={x['peak_r']}  pnl={x['pnl']}  ({x['ticks']} ticks)")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
