"""Batch-pin the surviving high-value replay windows into the golden archive.

The 30d bridge pruner (iqfeed_trade_ticks) and the 6h scheduler NBBO prune age the
live tape out daily; the 2026-07-25 census found 219 un-archived high-value windows
(78 gold / 141 silver) with the oldest (06-26) pruning within a day. This tool pins
ALL of them in urgency order (oldest day first, gold before silver, biggest first)
using the same idempotent per-window copy as ``scripts/pin_replay_window.py`` (whose
SQL constants it imports), verifies each window against the census counts, writes a
final per-window census markdown + size report, and refreshes the disaster-recovery
pg_dump ONCE at the end (write-to-temp, verify with ``pg_restore --list``, swap).

    python scripts/harvest_golden_windows.py \
        --inventory scripts/data/golden_harvest_inventory.json \
        --out-dir D:/CHILI-Docker/chili-data/replay_batch \
        --dump-dir E:/chili-backups

READ-ONLY on the live tables (indexed (symbol, observed_at) range probes only);
writes only to ``replay_golden_ticks`` / ``replay_golden_nbbo`` + report files +
the dump. Strictly sequential — one transaction per window, so a crash loses at
most one window and never holds a multi-million-row insert open.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pin_replay_window import COPY_NBBO, COPY_TICKS, COUNTS, DDL  # noqa: E402

DB_URL = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")

SIZE_SQL = """
SELECT pg_total_relation_size('replay_golden_ticks'),
       pg_total_relation_size('replay_golden_nbbo'),
       current_setting('data_directory')
"""

CENSUS_SQL = """
SELECT t.symbol, t.day::text, t.ticks, coalesce(n.nbbo, 0) AS nbbo,
       t.first_tick::text, t.last_tick::text
FROM (SELECT symbol, observed_at::date AS day, count(*) AS ticks,
             min(observed_at) AS first_tick, max(observed_at) AS last_tick
      FROM replay_golden_ticks GROUP BY 1, 2) t
LEFT JOIN (SELECT symbol, observed_at::date AS day, count(*) AS nbbo
           FROM replay_golden_nbbo GROUP BY 1, 2) n USING (symbol, day)
ORDER BY t.day, t.symbol
"""


@dataclass
class WindowSpec:
    symbol: str
    day: str
    cls: str
    exp_ticks: int
    exp_nbbo: int
    canonical: bool = False


def load_inventory(path: str) -> list[WindowSpec]:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    specs = [
        WindowSpec(w["symbol"].strip().upper(), w["day"], w["class"],
                   int(w["exp_ticks"]), int(w["exp_nbbo"]), bool(w.get("canonical", False)))
        for w in doc["windows"]
    ]
    keys = [(s.symbol, s.day) for s in specs]
    assert len(keys) == len(set(keys)), "duplicate symbol+day in inventory"
    return specs


def urgency_order(specs: list[WindowSpec]) -> list[WindowSpec]:
    # oldest day first (closest to the pruner), gold before silver within the day,
    # biggest first within the class. Deterministic.
    return sorted(specs, key=lambda s: (s.day, 0 if s.cls == "gold" else 1, -s.exp_ticks))


def verify(spec: WindowSpec, tot_ticks: int, tot_nbbo: int) -> str:
    if tot_ticks == 0:
        return "lost"
    if tot_ticks < 0.90 * spec.exp_ticks:
        return "short_ticks"
    if tot_nbbo < 0.90 * spec.exp_nbbo:
        return "short_nbbo"  # expected on older windows — the 6h nbbo prune kept running post-census
    return "ok"


def refresh_dump(dump_dir: str) -> str | None:
    os.makedirs(dump_dir, exist_ok=True)
    final = os.path.join(dump_dir, "golden_windows_202607.dump")
    tmp = final + ".new"
    cmd = ["pg_dump", "--dbname", DB_URL,
           "-t", "replay_golden_ticks", "-t", "replay_golden_nbbo", "-Fc", "-f", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[harvest] dump FAILED: {r.stderr.strip()[:400]}", file=sys.stderr)
        return None
    chk = subprocess.run(["pg_restore", "--list", tmp], capture_output=True, text=True)
    if chk.returncode != 0:
        print(f"[harvest] dump verify FAILED: {chk.stderr.strip()[:400]}", file=sys.stderr)
        return None
    os.replace(tmp, final)  # existing dump stays intact until the new one verifies
    return final


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inventory", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "golden_harvest_inventory.json"))
    ap.add_argument("--out-dir", default="D:/CHILI-Docker/chili-data/replay_batch")
    ap.add_argument("--dump-dir", default=None,
                    help="refresh the compressed pg_dump of BOTH golden tables into DIR (once, at end)")
    ap.add_argument("--gold-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="pin at most N windows (testing)")
    ap.add_argument("--dry-run", action="store_true", help="print the order, touch nothing")
    args = ap.parse_args()

    specs = urgency_order(load_inventory(args.inventory))
    if args.gold_only:
        specs = [s for s in specs if s.cls == "gold"]
    if args.limit:
        specs = specs[: args.limit]
    print(f"[harvest] {len(specs)} windows queued "
          f"(gold={sum(1 for s in specs if s.cls == 'gold')}, "
          f"silver={sum(1 for s in specs if s.cls == 'silver')})")
    if args.dry_run:
        for s in specs:
            print(f"  {s.day} {s.symbol:6s} {s.cls:6s} exp {s.exp_ticks:>9,}t/{s.exp_nbbo:>9,}n")
        return 0

    import psycopg2

    os.makedirs(args.out_dir, exist_ok=True)
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    results: list[dict] = []
    t_all = time.time()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        for i, s in enumerate(specs, 1):
            t0 = time.time()
            params = {"sym": s.symbol, "day": s.day}
            with conn.cursor() as cur:
                cur.execute(COPY_TICKS, params)
                new_ticks = cur.rowcount
                cur.execute(COPY_NBBO, params)
                new_nbbo = cur.rowcount
                cur.execute(COUNTS, params)
                tot_ticks, tot_nbbo = cur.fetchone()
            conn.commit()  # one transaction per window
            verdict = verify(s, tot_ticks, tot_nbbo)
            dt = time.time() - t0
            results.append({"symbol": s.symbol, "day": s.day, "class": s.cls,
                            "exp_ticks": s.exp_ticks, "exp_nbbo": s.exp_nbbo,
                            "new_ticks": new_ticks, "new_nbbo": new_nbbo,
                            "tot_ticks": tot_ticks, "tot_nbbo": tot_nbbo,
                            "verdict": verdict, "elapsed_s": round(dt, 1)})
            print(f"[harvest] {i:3d}/{len(specs)} {s.day} {s.symbol:6s} {s.cls:6s} "
                  f"+{new_ticks:,}t/+{new_nbbo:,}n tot {tot_ticks:,}/{tot_nbbo:,} "
                  f"{verdict.upper()} ({dt:.1f}s)", flush=True)

        # final census + size report (golden tables only — cheap)
        with conn.cursor() as cur:
            cur.execute(CENSUS_SQL)
            census = cur.fetchall()
            cur.execute(SIZE_SQL)
            sz_ticks, sz_nbbo, data_dir = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    disk = shutil.disk_usage("E:\\")
    counts = {v: sum(1 for r in results if r["verdict"] == v)
              for v in ("ok", "short_ticks", "short_nbbo", "lost")}
    lines = ["# Golden archive census — harvest of 2026-07-26", "",
             f"- windows pinned this run: {len(results)} "
             f"(ok={counts['ok']}, short_ticks={counts['short_ticks']}, "
             f"short_nbbo={counts['short_nbbo']}, lost={counts['lost']})",
             f"- archive size: replay_golden_ticks={sz_ticks / 1e9:.2f}GB, "
             f"replay_golden_nbbo={sz_nbbo / 1e9:.2f}GB (postgres data_directory={data_dir})",
             f"- E: free after harvest: {disk.free / 1e9:.1f}GB of {disk.total / 1e9:.1f}GB", "",
             "| symbol | day | class | verdict | new t/n | archive t/n | census t/n | s |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['symbol']} | {r['day']} | {r['class']} | {r['verdict']} "
                     f"| +{r['new_ticks']:,}/+{r['new_nbbo']:,} "
                     f"| {r['tot_ticks']:,}/{r['tot_nbbo']:,} "
                     f"| {r['exp_ticks']:,}/{r['exp_nbbo']:,} | {r['elapsed_s']} |")
    lines += ["", f"## Full golden-table census ({len(census)} windows)", "",
              "| symbol | day | ticks | nbbo | first tick | last tick |", "|---|---|---|---|---|---|"]
    for sym, day, ticks, nbbo, first, last in census:
        lines.append(f"| {sym} | {day} | {ticks:,} | {nbbo:,} | {first} | {last} |")
    md_path = os.path.join(args.out_dir, "golden_census_2026-07-26.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    json_path = os.path.join(args.out_dir, "harvest_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    print(f"[harvest] census -> {md_path}")
    print(f"[harvest] TOTAL {time.time() - t_all:.0f}s; verdicts {counts}")

    if args.dump_dir:
        out = refresh_dump(args.dump_dir)
        if out is None:
            return 1
        print(f"[harvest] dump refreshed: {out} ({os.path.getsize(out):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
