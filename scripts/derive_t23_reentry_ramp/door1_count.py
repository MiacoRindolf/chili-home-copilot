# [23] re-entry ramp derivation (PR #1412). READ-ONLY against the live DB: point
# DATABASE_URL at it privately; every connection sets default_transaction_read_only.
"""[23] slice C side-check — the [7] no-reference door READS the tape's sign.

The scout's plan said the [7] door-1 multiplier (0.81) "does not read the tape split's sign".
The code says otherwise: ``reentry_escalation_decision`` step 1 passes the no-reference door
only when ``_tape_unreadable() or _tape_positive()`` — tape+ = accel>0 AND buy_share_delta>0.
So the G4 contract switch (legacy_time_split -> count_v1) moves WHICH class-A instants pass.

The 0.81 itself was derived (app/config.py) on the PRE-#1376 hold (accel>0 OR majority-buy,
15-s window): class-A tape+ 36/81 = 0.444 vs the 0.548 full-size reference; tape- 34/81 =
0.420. This re-measures the class-A tape+ hit rate under BOTH print contracts with the SAME
metric (one sample per (symbol, 15-min bucket, class); forward 15-min MFE >= 2% from the last
print at the instant) so the PR can say whether 0.81 survives the switch.

Population: level-1 no-reference G4 instants since 2026-09-08 —
  g4_reentry_escalation_blocked reason=non_structural_trigger, substitute_required null, and
  g4_reentry_pass_unproven decision_reason=non_structural_substitute_no_reference.
At most 4 instants read per (symbol, 15-min bucket). READ-ONLY, bounded, symbol-scoped.
"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict

os.environ["CHILI_PYTEST"] = "1"
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.services.trading.momentum_neural.entry_gates import signed_tape_accel_features as F  # noqa: E402

eng = create_engine(
    os.environ["DATABASE_URL"],
    connect_args={"options": "-c statement_timeout=20000 -c default_transaction_read_only=on"},
)
db = sessionmaker(bind=eng)()

rows = db.execute(text("""
SELECT e.ts, s.symbol
FROM trading_automation_events e JOIN trading_automation_sessions s ON s.id = e.session_id
WHERE e.ts >= '2026-09-08'
  AND (
    (e.event_type = 'g4_reentry_escalation_blocked'
     AND e.payload_json->>'reason' = 'non_structural_trigger'
     AND e.payload_json->>'escalation_level' = '1'
     AND e.payload_json->>'substitute_required' IS NULL)
    OR
    (e.event_type = 'g4_reentry_pass_unproven'
     AND e.payload_json->>'decision_reason' = 'non_structural_substitute_no_reference')
  )
ORDER BY e.ts
""")).fetchall()
print("class-A instants:", len(rows))


def pos(t):
    if not t or t.get("signed_tape_accel") is None or t.get("buy_share_delta") is None:
        return None
    return bool(float(t["signed_tape_accel"]) > 0 and float(t["buy_share_delta"]) > 0)


def hit(sym, ts):
    p0 = db.execute(text("""SELECT price FROM iqfeed_trade_ticks WHERE symbol=:s AND observed_at<=:t
        AND observed_at >= :t - interval '5 minutes' ORDER BY observed_at DESC LIMIT 1"""),
        {"s": sym, "t": ts}).scalar()
    if not p0:
        return None
    hi = db.execute(text("""SELECT max(price) FROM iqfeed_trade_ticks WHERE symbol=:s
        AND observed_at > :t AND observed_at <= :t + interval '15 minutes'"""),
        {"s": sym, "t": ts}).scalar()
    if hi is None:
        return None
    return bool(float(hi) / float(p0) - 1.0 >= 0.02)


per_b = Counter()
seen = {c: set() for c in ("legacy_time_split", "count_v1")}
res = {c: defaultdict(lambda: [0, 0]) for c in seen}
clusters = {c: defaultdict(set) for c in seen}
fwd_cache: dict = {}
for ts, sym in rows:
    b = (sym, ts.strftime("%m%d"), ts.hour * 4 + ts.minute // 15)
    per_b[b] += 1
    if per_b[b] > 4:
        continue
    for c in seen:
        try:
            k = pos(F(sym, db=db, window_prints=255, as_of=ts, feature_contract=c))
        except Exception:
            db.rollback()
            continue
        if k is None or (b, k) in seen[c]:
            continue
        seen[c].add((b, k))
        if (sym, ts) not in fwd_cache:
            fwd_cache[(sym, ts)] = hit(sym, ts)
        h = fwd_cache[(sym, ts)]
        if h is None:
            continue
        res[c][k][0] += int(h)
        res[c][k][1] += 1
        clusters[c][k].add((sym, ts.strftime("%m-%d")))

REF = 0.548
for c in seen:
    for k in (True, False):
        hN, n = res[c][k]
        if n:
            print(f"{c:18s} tape+={k!s:5s} hit {hN}/{n} = {hN / n:.3f} clusters={len(clusters[c][k])}"
                  f" ratio_vs_0.548 = {hN / n / REF:.3f}")
