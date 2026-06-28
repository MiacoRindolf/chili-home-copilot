"""Replay v2 CLI — thin wrapper over the engine (see momentum_neural/replay_v2.py).

    PYTHONPATH=<tree> DATABASE_URL=postgres://...chili python scripts/_replay_v2.py 2026-06-10

The web UI runs the same engine at /trading/replay.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.trading.momentum_neural.replay_v2 import run_replay  # noqa: E402

if __name__ == "__main__":
    argv = [a for a in sys.argv[1:]]
    # --json: print the full run_replay result dict as JSON to stdout (machine-readable,
    # for the version-diff harness which captures + parses two ledgers from subprocesses).
    # Additive + parity-safe: the human print path below is untouched when --json is absent.
    json_mode = "--json" in argv
    if json_mode:
        argv = [a for a in argv if a != "--json"]
    # --armed-source=<asof|live|full_pipeline>: optional, defaults to the engine default.
    armed_source = "live"
    _rest = []
    for a in argv:
        if a.startswith("--armed-source="):
            armed_source = a.split("=", 1)[1] or armed_source
        else:
            _rest.append(a)
    argv = _rest
    date = argv[0] if argv else None
    if not date:
        print("usage: python scripts/_replay_v2.py YYYY-MM-DD [--json] [--armed-source=live]")
        raise SystemExit(2)
    if json_mode:
        # persist=False so a diff run never clobbers the shared /app/data/replays cache;
        # the harness pins the basis + flags via env, so both subprocess runs are isolated.
        r = run_replay(date, persist=False, armed_source=armed_source)
        sys.stdout.write(json.dumps(r, default=str))
        sys.stdout.flush()
        raise SystemExit(1 if r.get("error") else 0)
    r = run_replay(date, armed_source=armed_source)
    print(f"=== REPLAY v2 — {date} ===")
    print(f"tape: {r['tape_symbols']} symbols | halt windows: {r['halt_windows']} on {r['halted_symbols']} symbols | candidates: {r['candidates']}")
    if r.get("error"):
        print("ERROR:", r["error"]); raise SystemExit(1)
    print(f"TRADES ({len(r['trades'])}; {r['wins']}W/{r['losses']}L; halted={r['day_halted']}):")
    for t in r["trades"]:
        # RECORDED-FILLS CONSUMER: surface the SOURCE tag so the operator can see which trades
        # are EXACT recorded broker truth (recorded_live) vs MODELED from the tape (derived).
        # Recorded trades carry no run_r (no modeled bracket) → print '   —'. Default tag for a
        # plain (flag-off) run = 'derived' so the column is always meaningful.
        _src = t.get("source") or ("recorded_live" if t.get("fidelity") == "recorded" else "derived")
        _rr = t.get("run_r")
        _rr_s = ("%+.2f" % _rr) if isinstance(_rr, (int, float)) else "   —"
        print("  %s %-6s [%-13s] entry=%.3f exit=%.3f qty=%-7.0f spread=%3.0fbps runR=%s partial=%.2f %-32s $%+8.0f" % (
            t["t"], t["sym"], _src, t["entry"], t["exit"], t["qty"], (t.get("spread_bps") or 0.0),
            _rr_s, (t.get("partial") or 1.0), t["why"], t["usd"]))
    # RECORDED-FILLS CONSUMER summary (present only when the flag is on + armed_source=='live').
    _rfc = r.get("recorded_fills_consumer")
    if isinstance(_rfc, dict) and _rfc.get("enabled"):
        _rf = _rfc.get("recorded_filled", [])
        _dr = _rfc.get("live_cancelled_dropped", [])
        print(f"RECORDED-FILLS CONSUMER: {len(_rf)} live-armed names FILLED (emitted recorded) "
              f"| {len(_dr)} live-armed names CANCELLED (dropped) of {_rfc.get('live_armed')} live-armed")
        print(f"  recorded-live names: {_rf}")
    print(f"DAY TOTAL (v2, real-spread fidelity): ${r['total_usd']:+,.0f}")
    # FIDELITY-V2 confidence band (present only when chili_momentum_replay_fidelity_v2 is on).
    _band = r.get("day_pnl_band")
    if _band:
        _bm = r.get("day_pnl_band_meta", {})
        _ov, _ln = _bm.get("fill_set_overlap"), _bm.get("live_filled_names")
        _ov_str = (f" | fill-set overlap {_ov}/{_ln}" if _ov is not None and _ln else "")
        print(f"DAY $ BAND (fidelity-v2): ${r['total_usd']:+,.0f} over [${_band[0]:+,.0f}, ${_band[1]:+,.0f}]"
              f"{_ov_str} (brackets the irreducible rail-4xx tail; tape-ceiling misses = one-sided floor)")
    # run_R separator: DERIVED trades only (recorded_live carries no modeled bracket → run_r=None).
    _w = [t["run_r"] for t in r["trades"] if t.get("usd", 0) > 0 and isinstance(t.get("run_r"), (int, float))]
    _l = [t["run_r"] for t in r["trades"] if t.get("usd", 0) <= 0 and isinstance(t.get("run_r"), (int, float))]
    if _w or _l:
        import statistics as _st
        print("RUN-R SEPARATOR: winners median=%s (n=%d) | losers median=%s (n=%d)  [MESO: winners thrust, losers fade]" % (
            (round(_st.median(_w), 2) if _w else "—"), len(_w), (round(_st.median(_l), 2) if _l else "—"), len(_l)))
