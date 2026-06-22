"""Replay v2 CLI — thin wrapper over the engine (see momentum_neural/replay_v2.py).

    PYTHONPATH=<tree> DATABASE_URL=postgres://...chili python scripts/_replay_v2.py 2026-06-10

The web UI runs the same engine at /trading/replay.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.trading.momentum_neural.replay_v2 import run_replay  # noqa: E402

if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    if not date:
        print("usage: python scripts/_replay_v2.py YYYY-MM-DD"); raise SystemExit(2)
    r = run_replay(date)
    print(f"=== REPLAY v2 — {date} ===")
    print(f"tape: {r['tape_symbols']} symbols | halt windows: {r['halt_windows']} on {r['halted_symbols']} symbols | candidates: {r['candidates']}")
    if r.get("error"):
        print("ERROR:", r["error"]); raise SystemExit(1)
    print(f"TRADES ({len(r['trades'])}; {r['wins']}W/{r['losses']}L; halted={r['day_halted']}):")
    for t in r["trades"]:
        print("  %s %-6s entry=%.3f exit=%.3f qty=%-7.0f spread=%3.0fbps runR=%+.2f partial=%.2f %-26s $%+8.0f" % (
            t["t"], t["sym"], t["entry"], t["exit"], t["qty"], t["spread_bps"], t.get("run_r", 0.0), t["partial"], t["why"], t["usd"]))
    print(f"DAY TOTAL (v2, real-spread fidelity): ${r['total_usd']:+,.0f}")
    _w = [t["run_r"] for t in r["trades"] if t.get("usd", 0) > 0 and "run_r" in t]
    _l = [t["run_r"] for t in r["trades"] if t.get("usd", 0) <= 0 and "run_r" in t]
    if _w or _l:
        import statistics as _st
        print("RUN-R SEPARATOR: winners median=%s (n=%d) | losers median=%s (n=%d)  [MESO: winners thrust, losers fade]" % (
            (round(_st.median(_w), 2) if _w else "—"), len(_w), (round(_st.median(_l), 2) if _l else "—"), len(_l)))
