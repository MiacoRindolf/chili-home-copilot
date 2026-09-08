"""What did the TAPE say at the moment of each entry? Ask IQFeed; store nothing.

The question this answers: when the lane entered the same name eight times in
thirty minutes and lost on every one, was the tape already saying no?

It pulls a bounded slice of trade prints ending at each entry timestamp and runs
the real `_signed_tape_features` over them — the same function the live gates
read — so the verdict is the production computation, not a re-implementation.

Bid/ask are taken from the lookup rows when present; when absent the parser falls
back to the tick rule exactly as it does live, and the aggressor sign is then
inferred from price direction. That is a genuine fidelity limit and it is printed
with the results rather than hidden.

    python scripts/tape_verdict_probe.py --symbol WYHG --date 2026-09-08 \
        --at 08:41:02 --at 08:43:20 --lookback-s 20

Read-only against IQFeed; touches no database. The lookup client carries its own
interlock and refuses to run unless the live bridges hold the streaming ports.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from iqfeed_lookup_client import (  # noqa: E402
    IQFeedLookupClient,
    assert_lane_clients_present,
)

from app.services.trading.momentum_neural.entry_gates import (  # noqa: E402
    _signed_tape_features,
)

_ET_OFFSET_HOURS = -4  # the book stores UTC; IQFeed wants US/Eastern


def _iq(dt: datetime) -> str:
    return (dt + timedelta(hours=_ET_OFFSET_HOURS)).strftime("%Y%m%d %H%M%S")


def _rows(lines: list[str]) -> list[tuple]:
    """LH,timestamp,price,size,total_volume,bid,ask,... -> the SQL row shape."""
    out = []
    for ln in lines:
        p = ln.split(",")
        if len(p) < 4 or p[0] != "LH":
            continue
        try:
            px = float(p[2])
            sz = float(p[3])
        except (TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0:
            continue
        bid = ask = None
        if len(p) > 6:
            try:
                b, a = float(p[5]), float(p[6])
                if a > b > 0:
                    bid, ask = b, a
            except (TypeError, ValueError):
                pass
        try:
            ts = datetime.strptime(p[1].strip(), "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            try:
                ts = datetime.strptime(p[1].strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        out.append((px, sz, bid, ask, ts.timestamp()))
    out.sort(key=lambda r: r[4])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--date", required=True, help="UTC date, YYYY-MM-DD")
    ap.add_argument("--at", action="append", required=True,
                    help="UTC entry time HH:MM:SS (repeatable)")
    ap.add_argument("--lookback-s", type=float, default=20.0,
                    help="seconds of tape ending AT the entry — the window the live "
                         "confirmer reads")
    ap.add_argument("--pctile", type=float, default=0.0,
                    help="tick_rate_floor percentile (live binding default is 0.0)")
    ap.add_argument("--max-datapoints", type=int, default=15000)
    args = ap.parse_args()

    lane = assert_lane_clients_present()
    print(f"lane interlock : OK ({lane})\n")

    hdr = (f"{'entry (UTC)':<13} {'n':>5} {'accel':>10} {'rate':>7} {'floor':>7} "
           f"{'fl_n':>5} {'sinceHi':>8} {'hiPos':>6} {'lowPrev':>8} {'lowNow':>8} "
           f"{'buySup':>8}  verdict")
    print(hdr)
    print("-" * len(hdr))

    quotes_seen = 0
    with IQFeedLookupClient() as client:
        for t in args.at:
            end = datetime.strptime(f"{args.date} {t}", "%Y-%m-%d %H:%M:%S")
            start = end - timedelta(seconds=args.lookback_s)
            res = client.htt(args.symbol, _iq(start), _iq(end),
                             max_datapoints=args.max_datapoints)
            if res.error or res.no_data:
                print(f"{t:<13} {'—':>5}  (walang tape)")
                continue
            rows = _rows(res.lines)
            quotes_seen += sum(1 for r in rows if r[2] is not None)
            f = _signed_tape_features(
                rows, window_s=args.lookback_s, tick_rate_floor_pctile=args.pctile)
            if f is None:
                print(f"{t:<13} {len(rows):>5}  (kulang ang tape: <3 print)")
                continue

            def g(k, d=0.0):
                v = f.get(k)
                return d if v is None else v

            # The live entry confirmer's own rule: DEFER only on clear no-tape.
            accel = g("signed_tape_accel")
            hi_pos = f.get("high_print_position")
            low_prev, low_now = f.get("swing_low_prev"), f.get("swing_low_now")
            marks = []
            if accel <= 0:
                marks.append("accel<=0")
            if hi_pos is not None and hi_pos >= 0.5:
                marks.append(f"high {hi_pos:.0%} back")
            if low_prev is not None and low_now is not None and low_now <= low_prev:
                marks.append("lower low")
            if g("tick_rate") < g("tick_rate_floor"):
                marks.append("below floor")
            verdict = "REFUSE: " + ", ".join(marks) if marks else "ok"

            def num(v: float | None, nd: int = 4) -> str:
                return "—" if v is None else f"{v:.{nd}f}"

            print(f"{t:<13} {f['n_ticks']:>5} {accel:>10.0f} {g('tick_rate'):>7.2f} "
                  f"{g('tick_rate_floor'):>7.2f} {f['tick_rate_floor_n']:>5} "
                  f"{str(f.get('prints_since_high')):>8} "
                  f"{num(hi_pos, 2):>6} {num(low_prev):>8} {num(low_now):>8} "
                  f"{num(f.get('buy_support_px')):>8}  {verdict}")

    note = ("aggressor sign taken from the book" if quotes_seen else
            "NO bid/ask in the lookup rows — the aggressor sign fell back to the "
            "tick rule, exactly as the live parser does when quotes are absent")
    print(f"\nquote-bearing prints: {quotes_seen}  ({note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
