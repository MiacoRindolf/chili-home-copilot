"""READ-ONLY derivation probe 2 for [26]: does the window_s/2 = 7.5 s halt trim
actually trim a 255-print window TO NOTHING, and on what class of name?

For every disjoint 255-print window (premarket 08:00Z + RTH, two dates) we replay the
EXACT main-branch trim: keep the contiguous segment AFTER the LAST inter-print gap
> 7.5 s; < 3 prints left => _signed_tape_features returns None.

Bounded: symbol + [08:00Z, 20:00Z) per date, statement_timeout 20 s, read-only.
"""
import statistics as st
import sys

import psycopg2

SYMS = """ACCL AGPU AHMA AIOS ALAR ANY AOUT BJDX BNC BRNX BSEM CDTG CLIK CULP
DFNS DPU EHGO EHLD ETS FGL FRTT GAME GCDT GFRR GLMD GMEX GRAN GRNQ HCAI
HCWC HTOO HYPD ISPC JILL KPLT LABT LHSW LONA MIMI MOBX MSS NUR NWGL OCC ODD
PCLA PHGE PLSM PMN PSIG RIBB RML RTB SGLY SKYQ SLE SSL SST SWVL TANH
TPET TSSI VIOT WETO WYHG XHLD XRTX YQ""".split()
DATES = ["2026-09-09", "2026-09-10"]
N = 255
HALF = 7.5   # window_s / 2 with the deployed chili_momentum_l2_confirm_window_s = 15


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    i = int(round((len(xs) - 1) * p))
    return xs[max(0, min(len(xs) - 1, i))]


def main():
    conn = psycopg2.connect(
        host="localhost", port=5433, dbname="chili", user="chili", password="chili"
    )
    conn.set_session(readonly=True, autocommit=True)
    retained = []
    med_gaps = []
    none_windows = 0
    n_windows = 0
    slow_windows = 0        # windows whose MEDIAN gap is over the 7.5 s constant
    none_by_sym: dict[str, int] = {}
    tot_by_sym: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout='20s'")
        for sym in SYMS:
            for d in DATES:
                try:
                    cur.execute(
                        "SELECT EXTRACT(EPOCH FROM observed_at) FROM iqfeed_trade_ticks "
                        "WHERE symbol=%s AND observed_at >= %s AND observed_at < %s "
                        "ORDER BY observed_at ASC, id ASC",
                        (sym, f"{d} 08:00:00", f"{d} 20:00:00"),
                    )
                    ts = [float(r[0]) for r in cur.fetchall()]
                except Exception as exc:  # noqa: BLE001
                    print(f"  !! {sym} {d}: {exc}", file=sys.stderr)
                    continue
                if len(ts) < N:
                    continue
                for s in range(0, len(ts) - N + 1, N):
                    w = ts[s:s + N]
                    gaps = [b - a for a, b in zip(w, w[1:]) if b >= a]
                    if not gaps:
                        continue
                    n_windows += 1
                    tot_by_sym[sym] = tot_by_sym.get(sym, 0) + 1
                    med = st.median(gaps)
                    med_gaps.append(med)
                    if med > HALF:
                        slow_windows += 1
                    last = None
                    for i, g in enumerate(gaps):
                        if g > HALF:
                            last = i + 1        # index of the print AFTER the gap
                    keep = len(w) if last is None else len(w) - last
                    retained.append(keep)
                    if keep < 3:
                        none_windows += 1
                        none_by_sym[sym] = none_by_sym.get(sym, 0) + 1
    print(f"windows={n_windows}")
    print(f"windows whose MEDIAN gap > {HALF}s (the 'slow name' the lift is for): "
          f"{slow_windows} ({100.0*slow_windows/max(1,n_windows):.3f}%)")
    print(f"windows the 7.5 s trim reduces to <3 prints (=> None): "
          f"{none_windows} ({100.0*none_windows/max(1,n_windows):.3f}%)")
    print(f"retained prints after trim: p1={pct(retained,0.01)} p5={pct(retained,0.05)} "
          f"p10={pct(retained,0.10)} p25={pct(retained,0.25)} p50={pct(retained,0.50)} "
          f"p90={pct(retained,0.90)} max={max(retained)}")
    print(f"median gap (s): p50={pct(med_gaps,0.50):.6g} p90={pct(med_gaps,0.90):.6g} "
          f"p99={pct(med_gaps,0.99):.6g} p999={pct(med_gaps,0.999):.6g} "
          f"max={max(med_gaps):.6g}")
    worst = sorted(none_by_sym.items(), key=lambda kv: -kv[1])[:10]
    print("worst symbols by None-windows:",
          [(s, c, tot_by_sym.get(s)) for s, c in worst])


if __name__ == "__main__":
    main()
