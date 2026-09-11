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
                    help="SEED seconds of tape to pull ending at the entry. The pull "
                         "doubles (up to --max-lookback-s) until it actually holds "
                         "--prints prints, because the window that DECIDES is counted "
                         "in prints and the probe must not report a print window while "
                         "measuring a seconds one")
    ap.add_argument("--max-lookback-s", type=float, default=3600.0,
                    help="ceiling on the widening pull (default 1 h)")
    ap.add_argument("--prints", type=int, default=None,
                    help="the window that DECIDES, in PRINTS (default: "
                         "settings.chili_momentum_tape_window_prints = the live "
                         "binding). The live gates are print-indexed since [29]; "
                         "reading the seconds form here would probe a unit the lane "
                         "no longer uses. Pass --window-s to force the legacy form.")
    ap.add_argument("--window-s", type=float, default=None,
                    help="legacy SECONDS window for a side-by-side (default: off)")
    ap.add_argument("--pctile", type=float, default=0.0,
                    help="tick_rate_floor percentile (live binding default is 0.0)")
    ap.add_argument("--max-datapoints", type=int, default=15000)
    args = ap.parse_args()

    lane = assert_lane_clients_present()
    from app.config import settings

    n_prints = int(args.prints or getattr(
        settings, "chili_momentum_tape_window_prints", 255) or 255)
    try:
        gap_floor = float(getattr(
            settings, "chili_momentum_g4_reentry_max_print_age_seconds", 14.69) or 14.69)
    except (TypeError, ValueError):
        gap_floor = 14.69
    from app.services.trading.momentum_neural.entry_gates import (
        _TAPE_GAP_DISCONTINUITY_P90_MULT,
    )
    try:
        gap_mult = float(getattr(
            settings, "chili_momentum_tape_gap_discontinuity_p90_mult",
            _TAPE_GAP_DISCONTINUITY_P90_MULT) or _TAPE_GAP_DISCONTINUITY_P90_MULT)
    except (TypeError, ValueError):
        gap_mult = float(_TAPE_GAP_DISCONTINUITY_P90_MULT)
    print(f"lane interlock : OK ({lane})")
    if args.window_s is not None:
        print(f"window         : {args.window_s} SECONDS (legacy unit)\n")
    else:
        # [29] review fix, 2026-09-11: this header used to read "last 255 PRINTS"
        # unconditionally while the code sliced rows[-255:] out of a 20-SECOND pull —
        # and by this PR's own arm-side measurement the p50 print count inside a 15-s
        # window is 3. The probe was therefore naming a print window and measuring a
        # clock one: the exact receipt-lies-about-the-window defect [29] exists to
        # remove, left inside the instrument used to derive [29]'s numbers. The n
        # actually used is now printed PER ROW ("n" is the window, "pull" is the span
        # it took), and a row that could not reach n_prints says so.
        print(f"window         : last {n_prints} PRINTS (count-split, discontinuity "
              f"trim = window gap p90 x {gap_mult:.2f}, age floor {gap_floor:.2f}s)")
        print(f"pull           : seeds at {args.lookback_s:g}s and doubles to at most "
              f"{args.max_lookback_s:g}s until {n_prints} prints are in hand\n")

    hdr = (f"{'entry (UTC)':<13} {'n':>5} {'pull_s':>7} {'accel':>10} {'rate':>7} "
           f"{'floor':>7} {'fl_n':>5} {'sinceHi':>8} {'hiPos':>6} {'lowPrev':>8} "
           f"{'lowNow':>8} {'buySup':>8}  verdict")
    print(hdr)
    print("-" * len(hdr))

    quotes_seen = 0
    with IQFeedLookupClient() as client:
        for t in args.at:
            end = datetime.strptime(f"{args.date} {t}", "%Y-%m-%d %H:%M:%S")
            # WIDEN THE PULL UNTIL THE PRINT WINDOW FITS INSIDE IT. The live gate takes
            # the last n_prints prints however long they took; a fixed-seconds pull can
            # only ever hand this probe whatever landed in those seconds.
            want = 1 if args.window_s is not None else n_prints
            pull_s = float(args.lookback_s)
            rows: list[tuple] = []
            res = None
            while True:
                start = end - timedelta(seconds=pull_s)
                res = client.htt(args.symbol, _iq(start), _iq(end),
                                 max_datapoints=args.max_datapoints)
                if res.error or res.no_data:
                    rows = []
                else:
                    rows = _rows(res.lines)
                if (len(rows) >= want or pull_s >= float(args.max_lookback_s)
                        or len(rows) >= int(args.max_datapoints)):
                    break
                pull_s = min(float(args.max_lookback_s), pull_s * 2.0)
            if not rows:
                print(f"{t:<13} {'—':>5}  (walang tape sa {pull_s:g}s)")
                continue
            quotes_seen += sum(1 for r in rows if r[2] is not None)
            # [29] Ang bintanang NAGPAPASYA ay bilang ng print, hindi segundo: ang
            # huling N print, count-split, at ang trim ng discontinuity ay ang
            # scale-free na hangganan — pareho ng binabasa ng live na gate. WALANG
            # window_s na ipinapasa sa anyong ito: kung maipapasa ito ay may orasan
            # pa rin sa loob (ang back_secs fallback), at iyon mismo ang inaalis.
            if args.window_s is not None:
                f = _signed_tape_features(
                    rows, window_s=args.window_s, tick_rate_floor_pctile=args.pctile)
                used = rows
            else:
                used = rows[-n_prints:]
                f = _signed_tape_features(
                    used,
                    tick_rate_floor_pctile=args.pctile,
                    split="count",
                    gap_trim_s=gap_floor,
                    gap_discontinuity_mult=gap_mult,
                )
            if f is None:
                print(f"{t:<13} {len(used):>5}  (kulang ang tape: <3 print)")
                continue
            short = (args.window_s is None and len(used) < n_prints)

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

            if short:
                verdict += (f"  [SHORT WINDOW: {len(used)} of {n_prints} prints in "
                            f"{pull_s:g}s — not the live window]")
            if f.get("gap_restricted"):
                verdict += (f"  [trimmed at {num(f.get('gap_trim_s'), 2)}s "
                            f"= p90 {num(f.get('gap_trim_window_p90_s'), 3)}s "
                            f"x {num(f.get('gap_trim_mult'), 2)}]")
            print(f"{t:<13} {f['n_ticks']:>5} {pull_s:>7.0f} {accel:>10.0f} "
                  f"{g('tick_rate'):>7.2f} "
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
