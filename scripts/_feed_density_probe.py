"""Measure Massive WS true per-symbol delivery rate: is 4.6s a Massive aggregation
limit or just quiet names? Find the densest-trading symbol per source + its
median inter-tick gap. If the densest name's median gap << 4.6s, Massive delivers
fast and 'quiet names' explain the 4.6s; if even the densest ~4-5s, Massive aggregates."""
import os
from sqlalchemy import create_engine, text

e = create_engine(os.environ["DATABASE_URL"])
with e.connect() as c:
    rows = c.execute(text(
        "select source, count(*) n, count(distinct symbol) syms "
        "from momentum_nbbo_spread_tape "
        "where created_at > now() - interval '6 hours' and source like 'massive' || chr(37) "
        "group by source order by n desc"
    )).fetchall()
    print("=== massive tape sources (6h) ===")
    for r in rows:
        print(f"  {r[0]:24s} rows={r[1]:6d} syms={r[2]}")

    for src in ["massive_ws_universe", "massive_ws", "massive_snapshot"]:
        top = c.execute(text(
            "select symbol, count(*) n from momentum_nbbo_spread_tape "
            "where created_at > now() - interval '6 hours' and source=:s "
            "group by symbol order by n desc limit 1"
        ), {"s": src}).fetchone()
        if not top:
            continue
        sym = top[0]
        gaps = c.execute(text(
            "select extract(epoch from (created_at - lag(created_at) over (order by created_at))) g "
            "from momentum_nbbo_spread_tape "
            "where source=:s and symbol=:sym and created_at > now() - interval '6 hours'"
        ), {"s": src, "sym": sym}).fetchall()
        g = sorted([float(x[0]) for x in gaps if x[0] is not None])
        if g:
            med = g[len(g) // 2]
            p10 = g[len(g) // 10]
            print(f"  [{src}] densest={sym} ticks={top[1]} | min={g[0]:.2f}s p10={p10:.2f}s MEDIAN={med:.2f}s max={g[-1]:.1f}s")
