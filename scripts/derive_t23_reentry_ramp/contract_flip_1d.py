# [23] re-entry ramp derivation (PR #1412). READ-ONLY against the live DB: point
# DATABASE_URL at it privately; every connection sets default_transaction_read_only.
"""[23] remainder S2: the G4 re-entry bar's tape hold runs feature_contract=legacy_time_split
(255 prints selected by COUNT, halves split at the TIME midpoint, gap trim window_s_half).
How often does the print-split contract (count_v1) disagree on tape+ (accel>0 AND bsd>0)
at the bar's own decision instants? READ-ONLY on chili; one bounded symbol read per instant.
"""
import os, sys
from collections import Counter, defaultdict
os.environ["CHILI_PYTEST"] = "1"
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

eng = create_engine(os.environ["DATABASE_URL"],
                    connect_args={"options": "-c statement_timeout=20000 -c default_transaction_read_only=on"})
db = sessionmaker(bind=eng)()
from app.services.trading.momentum_neural.entry_gates import signed_tape_accel_features as F

DAYS = int(os.environ.get("DAYS", "1"))
rows = db.execute(text("""
SELECT e.ts, s.symbol, e.event_type, e.payload_json->>'reason' r, e.payload_json->>'decision_reason' dr,
       e.payload_json->>'escalation_level' lvl
FROM trading_automation_events e JOIN trading_automation_sessions s ON s.id=e.session_id
WHERE e.ts >= now() - make_interval(days => :d)
  AND e.event_type IN ('g4_reentry_escalation_blocked','g4_reentry_pass_unproven','g4_reentry_reclaim_proven')
ORDER BY e.ts"""), {"d": DAYS}).fetchall()

def pos(t):
    if not t:
        return None
    a, b = t.get("signed_tape_accel"), t.get("buy_share_delta")
    if a is None or b is None:
        return None
    return float(a) > 0 and float(b) > 0

tab = Counter()
by_reason = defaultdict(Counter)
clusters = defaultdict(set)
for ts, sym, et, r, dr, lvl in rows:
    try:
        L = F(sym, db=db, window_prints=255, as_of=ts, feature_contract="legacy_time_split")
        C = F(sym, db=db, window_prints=255, as_of=ts, feature_contract="count_v1")
    except Exception as e:
        db.rollback()
        tab["error"] += 1
        continue
    pl, pc = pos(L), pos(C)
    k = (pl, pc)
    tab[k] += 1
    key = (et, r or dr)
    by_reason[key][k] += 1
    if pl != pc:
        clusters[k].add((sym, ts.strftime("%m-%d")))

print("instants", len(rows))
for k, v in sorted(tab.items(), key=lambda kv: -kv[1]):
    print("legacy,count =", k, v)
for k, c in clusters.items():
    print("disagree", k, "clusters:", sorted(c))
for key, c in sorted(by_reason.items(), key=lambda kv: -sum(kv[1].values())):
    print(key, dict(c))
# [23] builder addition: readability per contract (None = unreadable tape => fail-open
# skip in the G4 hold, WAIT in the [46] gate, door 2 in the [7] substitute).
print("legacy None:", sum(v for k, v in tab.items() if isinstance(k, tuple) and k[0] is None),
      " count None:", sum(v for k, v in tab.items() if isinstance(k, tuple) and k[1] is None))
