"""Crypto clock analysis from cached Coinbase 1m candles.

Reads scripts/_cx_cache/<PRODUCT>_<YYYY-MM-DD>.json (written by _cx_clock_fetch.py).

Definitions
- burst: 1m close-to-close return >= +1.5% on CONSECUTIVE minute bars
  (ts gap exactly 60s — a +1.5% print across a 30-min trade gap is not a burst).
- follow-through: from the burst bar close, within the next 120 minutes the
  tape prints high >= +2.0% before low <= -1.0% (vs burst close).
  Both-in-one-bar counts as FAIL (conservative). Unresolved (ran out of data,
  neither level hit) excluded from the rate but counted.
- liquidity proxies per hour bucket: $vol/min (sum vol*close / 60 incl. missing
  minutes as zero-trade minutes), traded-minute share (bars present / 60),
  median bar range (hi-lo)/close among traded bars.

Buckets: UTC hour-of-day (0-23), ISO dow (1=Mon..7=Sun), weekend = Sat/Sun.
"""
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "_cx_cache"
MAJORS = {"BTC-USD", "SOL-USD", "DOGE-USD"}

BURST_UP = 0.015
FT_TARGET = 0.02
FT_STOP = -0.01
FT_HORIZON_MIN = 120


def load_series():
    """{product: [(ts, low, high, open, close, vol), ...] sorted}"""
    series = defaultdict(dict)
    for f in CACHE.glob("*_2026-*.json"):
        name = f.name
        prod = name.rsplit("_", 1)[0]
        try:
            rows = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        for r in rows:
            series[prod][int(r[0])] = r
    return {p: [v[k] for k in sorted(v)] for p, v in series.items()}


def main():
    series = load_series()
    print(f"products loaded: {len(series)}")
    for p in sorted(series):
        bars = series[p]
        if bars:
            d0 = datetime.fromtimestamp(bars[0][0], tz=timezone.utc)
            d1 = datetime.fromtimestamp(bars[-1][0], tz=timezone.utc)
            print(f"  {p:14} {len(bars):>6} bars  {d0:%m-%d} .. {d1:%m-%d %H:%M}")

    # accumulators keyed by (group, hour) and (group, dow)
    res = {
        "bars": defaultdict(int),          # traded bars observed
        "minutes": defaultdict(int),       # total possible minutes (traded+missing) approximated per full hour-buckets seen
        "bursts": defaultdict(int),
        "bursts_abs": defaultdict(int),
        "ft_win": defaultdict(int),
        "ft_loss": defaultdict(int),
        "ft_unresolved": defaultdict(int),
        "dollar_vol": defaultdict(float),
        "ranges": defaultdict(list),
    }
    burst_log = []

    for prod, bars in series.items():
        grp = "major" if prod in MAJORS else "alt"
        idx = {b[0]: i for i, b in enumerate(bars)}
        # observed (prod, day, hour) buckets -> count 60 possible minutes each
        seen_hours = set()
        for b in bars:
            ts, lo, hi, op, cl, vol = b[0], b[1], b[2], b[3], b[4], b[5]
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            hr, dow = dt.hour, dt.isoweekday()
            wk = "weekend" if dow >= 6 else "weekday"
            seen_hours.add((prod, dt.date(), hr, dow))
            for key in ((grp, "hr", hr), (grp, "dow", dow), (grp, "wk", wk)):
                res["bars"][key] += 1
                res["dollar_vol"][key] += vol * cl
                if cl > 0:
                    res["ranges"][key].append((hi - lo) / cl)

        for (p_, date_, hr, dow) in seen_hours:
            wk = "weekend" if dow >= 6 else "weekday"
            for key in ((grp, "hr", hr), (grp, "dow", dow), (grp, "wk", wk)):
                res["minutes"][key] += 60

        # bursts on consecutive bars
        for i in range(1, len(bars)):
            prev, cur = bars[i - 1], bars[i]
            if cur[0] - prev[0] != 60 or prev[4] <= 0:
                continue
            ret = cur[4] / prev[4] - 1.0
            dt = datetime.fromtimestamp(cur[0], tz=timezone.utc)
            hr, dow = dt.hour, dt.isoweekday()
            wk = "weekend" if dow >= 6 else "weekday"
            keys = ((grp, "hr", hr), (grp, "dow", dow), (grp, "wk", wk))
            if abs(ret) >= BURST_UP:
                for k in keys:
                    res["bursts_abs"][k] += 1
            if ret < BURST_UP:
                continue
            for k in keys:
                res["bursts"][k] += 1
            # follow-through scan
            base = cur[4]
            tgt, stp = base * (1 + FT_TARGET), base * (1 + FT_STOP)
            outcome = "unresolved"
            t_end = cur[0] + FT_HORIZON_MIN * 60
            j = i + 1
            while j < len(bars) and bars[j][0] <= t_end:
                b2 = bars[j]
                hit_t = b2[2] >= tgt
                hit_s = b2[1] <= stp
                if hit_t and hit_s:
                    outcome = "loss"  # ambiguous -> conservative
                    break
                if hit_s:
                    outcome = "loss"; break
                if hit_t:
                    outcome = "win"; break
                j += 1
            for k in keys:
                res["ft_" + outcome][k] += 1
            burst_log.append({"prod": prod, "ts": cur[0], "hr": hr, "dow": dow,
                              "ret": round(ret, 5), "outcome": outcome})

    def fmt_bucket(grp, dim, labels):
        print(f"\n=== {grp} / by {dim} ===")
        print(f"{'bucket':>8} {'tradedbars':>10} {'mins':>9} {'bursts':>7} {'b/1k-min':>9} "
              f"{'ftW':>5} {'ftL':>5} {'ftU':>4} {'ft%':>6} {'$vol/min':>10} {'medrange':>9}")
        for lb in labels:
            k = (grp, dim, lb)
            mins = res["minutes"][k]
            if mins == 0:
                continue
            b = res["bursts"][k]
            w, l, u = res["ft_win"][k], res["ft_loss"][k], res["ft_unresolved"][k]
            ftp = 100 * w / (w + l) if (w + l) else float("nan")
            dvm = res["dollar_vol"][k] / mins
            mr = 100 * statistics.median(res["ranges"][k]) if res["ranges"][k] else float("nan")
            print(f"{str(lb):>8} {res['bars'][k]:>10} {mins:>9} {b:>7} {1000*b/mins:>9.3f} "
                  f"{w:>5} {l:>5} {u:>4} {ftp:>5.1f}% {dvm:>10.0f} {mr:>8.3f}%")

    for grp in ("alt", "major"):
        fmt_bucket(grp, "hr", list(range(24)))
        fmt_bucket(grp, "dow", [1, 2, 3, 4, 5, 6, 7])
        fmt_bucket(grp, "wk", ["weekday", "weekend"])

    # weekend-vs-weekday x hour for alts (the schedule question)
    # recompute on the fly from burst_log for ft; and from per-(hour,wk) minute counts
    print("\n=== alt / hour x weekend split (bursts per 1k min | ft%) ===")
    hr_wk_min = defaultdict(int)
    for prod, bars in series.items():
        if prod in MAJORS:
            continue
        seen = set()
        for b in bars:
            dt = datetime.fromtimestamp(b[0], tz=timezone.utc)
            seen.add((prod, dt.date(), dt.hour, dt.isoweekday()))
        for (_, _, hr, dow) in seen:
            hr_wk_min[(hr, "weekend" if dow >= 6 else "weekday")] += 60
    hr_wk_burst = defaultdict(int)
    hr_wk_ft = defaultdict(lambda: [0, 0])
    for e in burst_log:
        prod = e["prod"]
        if prod in MAJORS:
            continue
        wk = "weekend" if e["dow"] >= 6 else "weekday"
        hr_wk_burst[(e["hr"], wk)] += 1
        if e["outcome"] == "win":
            hr_wk_ft[(e["hr"], wk)][0] += 1
        elif e["outcome"] == "loss":
            hr_wk_ft[(e["hr"], wk)][1] += 1
    print(f"{'hr':>3} | {'wd b/1k':>8} {'wd ft% (n)':>13} | {'we b/1k':>8} {'we ft% (n)':>13}")
    for hr in range(24):
        cells = []
        for wk in ("weekday", "weekend"):
            mins = hr_wk_min[(hr, wk)]
            b = hr_wk_burst[(hr, wk)]
            w, l = hr_wk_ft[(hr, wk)]
            rate = 1000 * b / mins if mins else float("nan")
            ftp = f"{100*w/(w+l):5.1f}% ({w+l:>3})" if (w + l) else "   -  (  0)"
            cells.append(f"{rate:>8.3f} {ftp:>13}")
        print(f"{hr:>3} | {cells[0]} | {cells[1]}")

    (CACHE / "cx_clock_burst_log.json").write_text(json.dumps(burst_log))
    print(f"\nburst log saved: {len(burst_log)} up-bursts")


if __name__ == "__main__":
    main()
