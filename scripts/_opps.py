import os, json
from sqlalchemy import create_engine, text
e = create_engine(os.environ["DATABASE_URL"])
c = e.connect()
print("=== CHILI open sessions NOW (symbol/state) ===")
rows = c.execute(text(
    "select symbol, state from trading_automation_sessions where mode='live' and state in "
    "('watching_live','queued_live','live_entry_candidate','live_pending_entry','live_entered','live_trailing') "
    "order by state, symbol")).fetchall()
for s, st in rows:
    print(f"  {s}: {st}")

print("=== TOP live-eligible viability candidates (what CHILI ranks now) ===")
rows = c.execute(text(
    "select symbol, viability_score, live_eligible, updated_at "
    "from momentum_symbol_viability where live_eligible = true "
    "and updated_at > now() - interval '20 min' "
    "order by viability_score desc nulls last limit 18")).fetchall()
for s, v, le, ts in rows:
    print(f"  {s}: viab={round(float(v),3) if v is not None else None} fresh={str(ts)[11:19]}")

print("=== how many live-eligible fresh candidates total ===")
n = c.execute(text(
    "select count(*) from momentum_symbol_viability where live_eligible=true and updated_at > now() - interval '20 min'")).scalar()
print("  fresh_live_eligible =", n)
