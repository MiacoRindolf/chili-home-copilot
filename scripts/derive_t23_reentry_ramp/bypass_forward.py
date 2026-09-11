# [23] re-entry ramp derivation (PR #1412). READ-ONLY against the live DB: point
# DATABASE_URL at it privately; every connection sets default_transaction_read_only.
"""[23] remainder: forward outcome of BELOW-BAR, structural, tape-positive G4 instants.

READ-ONLY on chili (statement_timeout 20s; every tick read symbol-scoped + 15-min bounded).
Populations (level >= 1, structural trigger, price < required reclaim, tape positive):
  A  non-leader, REFUSED  (g4_reentry_escalation_blocked reason=reclaim_not_met, is_day_leader=false)
  B  leader, PASSED       (g4_reentry_pass_unproven decision_reason=leader_ignition_bypass)
Controls:
  C  reclaim PROVEN pass  (g4_reentry_reclaim_proven)
  D  leader, REFUSED, tape not positive (reclaim_not_met, is_day_leader=true)
Metric = the [7] metric (#1399) so the ratio is comparable to its 0.548 reference:
one sample per (symbol, 15-min bucket), forward 15-min MFE >= 2% from the last print at ts.
"""
import os
import psycopg2, psycopg2.extras
from collections import defaultdict

conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.set_session(readonly=True, autocommit=True)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SET statement_timeout='20s'")

cur.execute("""
SELECT e.ts, s.symbol, e.event_type, e.payload_json AS p
FROM trading_automation_events e JOIN trading_automation_sessions s ON s.id=e.session_id
WHERE e.ts >= now() - interval '30 days'
  AND ((e.event_type='g4_reentry_escalation_blocked' AND e.payload_json->>'structural_trigger'='true'
        AND e.payload_json->>'reason'='reclaim_not_met')
    OR (e.event_type='g4_reentry_pass_unproven' AND e.payload_json->>'decision_reason'='leader_ignition_bypass')
    OR e.event_type='g4_reentry_reclaim_proven')
ORDER BY e.ts
""")
rows = cur.fetchall()

def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def tape_pos(p):
    a, b, bb = f(p.get("tape_accel") or p.get("signed_tape_accel")), f(p.get("buy_share_delta")), f(p.get("tape_back_buy_share"))
    if a is None:
        return None
    if b is not None:
        return a > 0 and b > 0
    return a > 0 or (bb is not None and bb > 0.5)

samples = defaultdict(dict)
for r in rows:
    p = r["p"] or {}
    if r["event_type"] == "g4_reentry_pass_unproven":
        pop = "B"
    elif r["event_type"] == "g4_reentry_reclaim_proven":
        pop = "C"
    else:
        tp = tape_pos(p)
        ldr = p.get("is_day_leader")
        if ldr is False and tp:
            pop = "A"
        elif ldr is True:
            pop = "D"
        else:
            continue
    key = (r["symbol"], r["ts"].strftime("%Y%m%d"), r["ts"].hour * 4 + r["ts"].minute // 15)
    if key in samples[pop]:
        continue
    samples[pop][key] = r

def fwd(sym, ts):
    cur.execute("""SELECT price FROM iqfeed_trade_ticks WHERE symbol=%s AND observed_at<=%s
                   AND observed_at>=%s - interval '5 minutes' ORDER BY observed_at DESC LIMIT 1""", (sym, ts, ts))
    r0 = cur.fetchone()
    if not r0:
        return None
    p0 = r0["price"]
    cur.execute("""SELECT max(price) hi, min(price) lo, count(*) n FROM iqfeed_trade_ticks WHERE symbol=%s
                   AND observed_at>%s AND observed_at<=%s + interval '15 minutes'""", (sym, ts, ts))
    r1 = cur.fetchone()
    if not r1 or r1["hi"] is None:
        return None
    return p0, r1["hi"] / p0 - 1, r1["lo"] / p0 - 1, r1["n"]

for pop in ("A", "B", "C", "D"):
    hits = n = 0
    clusters = set()
    maes = []
    for key, r in sorted(samples[pop].items(), key=lambda kv: kv[1]["ts"]):
        out = fwd(r["symbol"], r["ts"])
        if out is None:
            print(pop, r["symbol"], r["ts"], "no tape")
            continue
        p0, mfe, mae, nf = out
        n += 1
        hits += mfe >= 0.02
        maes.append(mae)
        clusters.add((r["symbol"], key[1]))
        print(f"{pop} {r['ts']:%m-%d %H:%M:%S} {r['symbol']:5s} p0={p0:.4f} lvl={r['p'].get('escalation_level')} "
              f"req={r['p'].get('required_reclaim')} mfe={mfe*100:+.2f}% mae={mae*100:+.2f}% fwd_prints={nf}")
    if n:
        maes.sort()
        print(f"== {pop}: n={n} clusters={len(clusters)} hit(MFE>=2%)={hits}/{n}={hits/n:.3f} "
              f"ratio_vs_0.548={hits/n/0.548:.3f} MAE p50={maes[len(maes)//2]*100:.2f}% worst={maes[0]*100:.2f}%")
