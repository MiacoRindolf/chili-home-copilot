"""Derive per-window replay bounds from the golden archive + build the run manifest.

The harvest inventory is symbol-DAY granularity, but the replay driver
(``scripts/replay_ab_dark_flags.py``) needs ``WIN_START/WIN_END`` (a 2h-class
intraday burst) + ``OHLCV_START`` (warmup lead). ``derive_window()`` finds the
activity burst deterministically from the GOLDEN per-minute tick histogram (never
the 37GB live table): the minimal contiguous span holding >=85%% of the day's
ticks, always extended to include the day-high minute, capped/floored, padded.

The three canonical windows (CLRO 07-02 / QTTB 07-13 / PLSM 07-13) keep their
hand-picked bounds verbatim — they are the tie-back to every banked A/B number —
and the derived bounds are printed beside them as the algorithm's sanity check.

    python scripts/derive_replay_windows.py --out-dir D:/CHILI-Docker/chili-data/replay_batch

Output: ``window_manifest.json`` (schema chili.replay-window-manifest.v1) with a
``tier`` per window: "baseline" (the curated ~20-25 run set: canonicals + Ross-
evidence cross-reference days + top gold by ticks, max 2 per calendar day) or
"library" (everything else — runnable later, same manifest). READ-ONLY on the DB.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

DB_URL = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")

HIST_SQL = """
SELECT date_trunc('minute', observed_at) AS m, count(*) AS n
FROM replay_golden_ticks
WHERE symbol = %(s)s AND observed_at >= %(day)s::date
  AND observed_at < (%(day)s::date + 1) AND price > 0
GROUP BY 1 ORDER BY 1
"""

HI_SQL = """
SELECT observed_at FROM replay_golden_ticks
WHERE symbol = %(s)s AND observed_at >= %(day)s::date
  AND observed_at < (%(day)s::date + 1) AND price > 0
ORDER BY price DESC, observed_at ASC LIMIT 1
"""

CENSUS_SQL = """
SELECT t.symbol, t.day::text, t.ticks, coalesce(n.nbbo, 0)
FROM (SELECT symbol, observed_at::date AS day, count(*) AS ticks
      FROM replay_golden_ticks GROUP BY 1, 2) t
LEFT JOIN (SELECT symbol, observed_at::date AS day, count(*) AS nbbo
           FROM replay_golden_nbbo GROUP BY 1, 2) n USING (symbol, day)
ORDER BY t.day, t.symbol
"""

# Hand-picked canonical windows — the tie-back to every banked A/B number. NEVER derived.
CANONICAL = {
    ("CLRO", "2026-07-02"): {"win_start": "2026-07-02T14:00:00", "win_end": "2026-07-02T16:00:00",
                             "ohlcv_start": "2026-07-02T13:00:00", "prepend": False},
    ("QTTB", "2026-07-13"): {"win_start": "2026-07-13T13:00:00", "win_end": "2026-07-13T16:00:00",
                             "ohlcv_start": "2026-07-13T11:00:00", "prepend": False},
    ("PLSM", "2026-07-13"): {"win_start": "2026-07-13T13:00:00", "win_end": "2026-07-13T16:00:00",
                             "ohlcv_start": "2026-07-13T12:05:00", "prepend": True},
}

# Ross-evidence cross-reference symbol-days (frame-verified evidence exists) — always baseline.
ROSS_CROSSREF = {
    ("CLRO", "2026-07-07"), ("VRAX", "2026-07-09"), ("CETX", "2026-07-02"),
    ("BJDX", "2026-07-07"), ("SILO", "2026-07-07"), ("JEM", "2026-06-30"),
    ("UPC", "2026-06-29"), ("UPC", "2026-06-26"),
}

BASELINE_TARGET = 22          # canonicals + crossref + top-gold fill
MAX_PER_DAY = 2               # diversity: no single day monopolizes the baseline


def estimate_runtime_s(ticks: int) -> int:
    # CLRO 860k ~ 40-48 min, QTTB 378k ~ 25 min  =>  ~8 min fixed + ~2.8 ms/tick
    return 480 + int(ticks * 0.0028)


@dataclass
class DerivedWindow:
    win_start: datetime
    win_end: datetime
    ohlcv_start: datetime
    evidence: dict


def derive_window(minute_counts: list[tuple[datetime, int]], hi_at: datetime, *,
                  coverage: float = 0.85, cap_min: int = 165, hard_cap_min: int = 150,
                  min_min: int = 45, pad_pre_min: int = 5, pad_post_min: int = 10,
                  ohlcv_lead_min: int = 60) -> DerivedWindow:
    """Deterministic burst finder on a per-minute tick histogram (pure — unit-testable)."""
    assert minute_counts, "empty histogram"
    t0 = minute_counts[0][0]
    t1 = minute_counts[-1][0]
    n_min = int((t1 - t0).total_seconds() // 60) + 1
    dense = [0] * n_min
    for m, n in minute_counts:
        dense[int((m - t0).total_seconds() // 60)] = int(n)
    total = sum(dense)
    hi_idx = min(max(int((hi_at.replace(second=0, microsecond=0) - t0).total_seconds() // 60), 0),
                 n_min - 1)

    def minimal_span(cov: float) -> tuple[int, int]:
        # two-pointer minimal contiguous span with sum >= cov*total; earliest tie-break
        target = cov * total
        best = (0, n_min - 1)
        best_len = n_min
        s = 0
        lo = 0
        for hi in range(n_min):
            s += dense[hi]
            while s - dense[lo] >= target:
                s -= dense[lo]
                lo += 1
            if s >= target and (hi - lo) < best_len:
                best, best_len = (lo, hi), hi - lo
        return best

    cov = coverage
    lo, hi = minimal_span(cov)
    lo, hi = min(lo, hi_idx), max(hi, hi_idx)  # burst must contain the day high
    while (hi - lo + 1) > cap_min and cov > 0.55:
        cov = round(cov - 0.05, 2)
        lo, hi = minimal_span(cov)
        lo, hi = min(lo, hi_idx), max(hi, hi_idx)
    if (hi - lo + 1) > cap_min:
        # fixed-length max-sum span constrained to contain hi_idx (earliest tie-break)
        best_s, best_lo = -1, max(0, hi_idx - hard_cap_min + 1)
        for start in range(max(0, hi_idx - hard_cap_min + 1),
                           min(hi_idx, n_min - hard_cap_min) + 1):
            s = sum(dense[start:start + hard_cap_min])
            if s > best_s:
                best_s, best_lo = s, start
        lo, hi = best_lo, best_lo + hard_cap_min - 1
    if (hi - lo + 1) < min_min:  # symmetric extend, bounded by tape extent
        need = min_min - (hi - lo + 1)
        lo = max(0, lo - need // 2)
        hi = min(n_min - 1, lo + min_min - 1)
        lo = max(0, hi - min_min + 1)
    span_ticks = sum(dense[lo:hi + 1])
    win_start = t0 + timedelta(minutes=lo - pad_pre_min)
    win_end = t0 + timedelta(minutes=hi + 1 + pad_post_min)
    day0 = t0.replace(hour=0, minute=0, second=0, microsecond=0)
    win_start = max(win_start, day0)
    win_end = min(win_end, day0 + timedelta(days=1))
    ohlcv_start = max(win_start - timedelta(minutes=ohlcv_lead_min), day0)
    return DerivedWindow(win_start, win_end, ohlcv_start, {
        "day_ticks": total, "span_ticks": span_ticks, "coverage_used": cov,
        "span_min": hi - lo + 1, "hi_at": hi_at.isoformat(),
        "tape_first_min": t0.isoformat(), "tape_last_min": t1.isoformat(),
        "params": {"coverage": coverage, "cap_min": cap_min, "hard_cap_min": hard_cap_min,
                   "min_min": min_min, "pad_pre_min": pad_pre_min,
                   "pad_post_min": pad_post_min, "ohlcv_lead_min": ohlcv_lead_min},
    })


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", default="D:/CHILI-Docker/chili-data/replay_batch")
    ap.add_argument("--inventory", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "golden_harvest_inventory.json"))
    args = ap.parse_args()

    with open(args.inventory, "r", encoding="utf-8") as f:
        inv = {(w["symbol"], w["day"]): w for w in json.load(f)["windows"]}

    import psycopg2

    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    windows = []
    with conn.cursor() as cur:
        cur.execute(CENSUS_SQL)
        census = cur.fetchall()
        for sym, day, ticks, nbbo in census:
            if ticks == 0:
                continue
            cur.execute(HIST_SQL, {"s": sym, "day": day})
            hist = cur.fetchall()
            cur.execute(HI_SQL, {"s": sym, "day": day})
            (hi_at,) = cur.fetchone()
            d = derive_window([(m, int(n)) for m, n in hist], hi_at)
            spec = inv.get((sym, day), {})
            key = (sym, day)
            entry = {
                "symbol": sym, "day": day,
                "class": spec.get("class", "extra"),
                "ticks": int(ticks), "nbbo": int(nbbo),
                "win_start": d.win_start.isoformat(),
                "win_end": d.win_end.isoformat(),
                "ohlcv_start": d.ohlcv_start.isoformat(),
                "prepend": False,
                "window_source": "derived",
                "est_runtime_s": estimate_runtime_s(int(ticks)),
                "derivation": d.evidence,
            }
            if key in CANONICAL:
                c = CANONICAL[key]
                delta_start = (datetime.fromisoformat(c["win_start"]) - d.win_start)
                delta_end = (datetime.fromisoformat(c["win_end"]) - d.win_end)
                print(f"[derive] CANONICAL {sym} {day}: hand-picked {c['win_start']}..{c['win_end']}"
                      f" | derived {d.win_start.isoformat()}..{d.win_end.isoformat()}"
                      f" (delta start {delta_start}, end {delta_end}) — keeping hand-picked")
                entry.update(c)
                entry["window_source"] = "canonical"
            windows.append(entry)
    conn.close()

    # tier selection: canonicals + Ross-crossref always baseline; fill with top gold by ticks
    per_day: dict[str, int] = {}
    for w in windows:
        w["tier"] = "library"
    def promote(w) -> bool:
        if per_day.get(w["day"], 0) >= MAX_PER_DAY and w["window_source"] != "canonical" \
                and (w["symbol"], w["day"]) not in ROSS_CROSSREF:
            return False
        w["tier"] = "baseline"
        per_day[w["day"]] = per_day.get(w["day"], 0) + 1
        return True

    n_base = 0
    for w in windows:
        if w["window_source"] == "canonical" or (w["symbol"], w["day"]) in ROSS_CROSSREF:
            if promote(w):
                n_base += 1
    for w in sorted(windows, key=lambda x: -x["ticks"]):
        if n_base >= BASELINE_TARGET:
            break
        if w["tier"] == "baseline" or w["class"] != "gold":
            continue
        if promote(w):
            n_base += 1

    est_total = sum(w["est_runtime_s"] for w in windows if w["tier"] == "baseline")
    doc = {"schema": "chili.replay-window-manifest.v1",
           "generated_at": datetime.now().isoformat(timespec="seconds"),
           "baseline_count": n_base, "library_count": len(windows) - n_base,
           "baseline_est_runtime_s": est_total,
           "windows": sorted(windows, key=lambda w: (w["tier"] != "baseline", w["day"], w["symbol"]))}
    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, "window_manifest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"[derive] {len(windows)} windows -> {out}")
    print(f"[derive] baseline tier: {n_base} windows, est {est_total / 3600:.1f}h sequential")
    for w in doc["windows"]:
        if w["tier"] == "baseline":
            print(f"  BASELINE {w['day']} {w['symbol']:6s} {w['class']:6s} "
                  f"{w['ticks']:>9,}t {w['win_start'][11:16]}-{w['win_end'][11:16]} "
                  f"({w['window_source']}) est {w['est_runtime_s'] // 60}min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
