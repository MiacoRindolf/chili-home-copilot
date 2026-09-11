# [23] re-entry ramp derivation (PR #1412). READ-ONLY against the live DB: point
# DATABASE_URL at it privately; every connection sets default_transaction_read_only.
"""S2 forward: first-touch +2% vs -2% ([7]'s 2% threshold, symmetric) over the next 15 min of
PRINTS after each G4 instant, split by (legacy tape+, count_v1 tape+). One sample per
(symbol, 15-min bucket, class). READ-ONLY, bounded."""
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
rows = db.execute(text("""
SELECT e.ts, s.symbol FROM trading_automation_events e JOIN trading_automation_sessions s ON s.id=e.session_id
WHERE e.ts >= now() - interval '8 days'
  AND e.event_type IN ('g4_reentry_escalation_blocked','g4_reentry_pass_unproven','g4_reentry_reclaim_proven')
ORDER BY e.ts""")).fetchall()
def pos(t):
    if not t or t.get("signed_tape_accel") is None or t.get("buy_share_delta") is None:
        return None
    return float(t["signed_tape_accel"]) > 0 and float(t["buy_share_delta"]) > 0
seen = set(); res = defaultdict(Counter); cl = defaultdict(set)
per_b = Counter()
for ts, sym in rows:
    _bb = (sym, ts.strftime("%m%d"), ts.hour * 4 + ts.minute // 15)
    per_b[_bb] += 1
    if per_b[_bb] > 4:
        continue
    b = (sym, ts.strftime("%m%d"), ts.hour * 4 + ts.minute // 15)
    try:
        L = pos(F(sym, db=db, window_prints=255, as_of=ts, feature_contract="legacy_time_split"))
        C = pos(F(sym, db=db, window_prints=255, as_of=ts, feature_contract="count_v1"))
    except Exception:
        db.rollback(); continue
    k = (L, C)
    if None in k or (b, k) in seen:
        continue
    seen.add((b, k))
    p0 = db.execute(text("SELECT price FROM iqfeed_trade_ticks WHERE symbol=:s AND observed_at<=:t AND observed_at>=:t - interval '5 minutes' ORDER BY observed_at DESC LIMIT 1"), {"s": sym, "t": ts}).scalar()
    if not p0:
        continue
    r = db.execute(text("""SELECT min(observed_at) FILTER (WHERE price >= :up) tu, min(observed_at) FILTER (WHERE price <= :dn) td
        FROM iqfeed_trade_ticks WHERE symbol=:s AND observed_at>:t AND observed_at<=:t + interval '15 minutes'"""),
        {"s": sym, "t": ts, "up": p0 * 1.02, "dn": p0 * 0.98}).fetchone()
    tu, td = r
    o = "up" if tu and (not td or tu < td) else ("down" if td else "neither")
    res[k][o] += 1; cl[k].add((sym, ts.strftime("%m-%d")))
for k, c in sorted(res.items(), key=lambda kv: str(kv[0])):
    n = sum(c.values()); u, d = c["up"], c["down"]
    print(f"legacy={k[0]!s:5} count={k[1]!s:5} n={n:3d} clusters={len(cl[k])} up_first={u} down_first={d} neither={c['neither']} up_share_of_resolved={u/max(1,u+d):.3f}")
