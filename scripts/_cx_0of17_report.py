"""0/17 forensics — final aggregates for the report."""
import json
import pathlib
import statistics as st
from datetime import datetime, timezone

CACHE = pathlib.Path(__file__).resolve().parent / "_cx_cache"
rows = json.loads((CACHE / "cx_0of17_attrib.json").read_text())
DB = json.loads((CACHE / "cx_0of17_db.json").read_text())

# exit-submit-fail windows vs actual exit time (ghost retries after broker zero?)
print("exit_submit_fail windows (sid, n, first->last, actual_exit):")
for n in DB["noise_counts"]:
    if n["event_type"] == "live_exit_submit_failed" and int(n["n"]) > 10:
        sid = n["session_id"]
        r = next((x for x in rows if x["sid"] == sid), None)
        print(f"  sid={sid} n={n['n']} {n['first_ts'][5:19]} -> {n['last_ts'][5:19]} "
              f"exit_t={(r or {}).get('exit_t', '')[5:19]}")

tot_notional = sum(r["notional"] for r in rows)
print(f"\ntotal entry notional=${tot_notional:,.2f}")
print("per-day:")
for day in ("2026-06-06", "2026-06-07", "2026-06-08"):
    rr = [r for r in rows if r["entry_t"][:10] == day]
    if rr:
        print(f"  {day}: n={len(rr)} net_real={sum(r['net_real_usd'] for r in rr):+.2f} "
              f"rec_be={sum(1 for r in rr if r['recovered_be_2h'])} "
              f"rec_2r={sum(1 for r in rr if r['recovered_2r_2h'])}")

pre = [r["pre90m_trend_pct"] for r in rows if r["pre90m_trend_pct"] is not None]
print(f"\npre-entry 90m trend: median={st.median(pre):+.1f}% range=[{min(pre):+.1f},{max(pre):+.1f}] n={len(pre)}")
mfe = [r["mfe_r"] for r in rows if r["mfe_r"] is not None]
print(f"MFE(R) within hold: median={st.median(mfe):.2f} >=1R: {sum(1 for m in mfe if m >= 1)}/{len(mfe)} "
      f">=2R: {sum(1 for m in mfe if m >= 2)}/{len(mfe)}")
print(f"sim reached partial(2R) before stop: {sum(1 for r in rows if r['sim_partial'])}/21")
holds = [r["hold_min"] for r in rows if r["hold_min"]]
print(f"actual hold minutes: median={st.median(holds):.1f}")
fees_bps = [10000 * r["fees_real_usd"] / (2 * r["notional"]) for r in rows]
print(f"real fee per side: median={st.median(fees_bps):.0f}bps")
# variant family split
fam = {}
for r in rows:
    o = next((o for o in DB["outcomes"] if o["session_id"] == r["sid"]), {})
    fam.setdefault(o.get("variant_key") or "?", []).append(r["net_real_usd"])
print("\nby variant family:")
for k, v in sorted(fam.items(), key=lambda kv: sum(kv[1])):
    print(f"  {k}: n={len(v)} net={sum(v):+.2f}")
# hour-of-day
print("\nby UTC hour bucket:")
for lo, hi in ((0, 6), (6, 12), (12, 18), (18, 24)):
    rr = [r for r in rows if lo <= datetime.fromisoformat(r["entry_t"]).hour < hi]
    if rr:
        print(f"  {lo:02d}-{hi:02d}: n={len(rr)} net={sum(r['net_real_usd'] for r in rr):+.2f}")
