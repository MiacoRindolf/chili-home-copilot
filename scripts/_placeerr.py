import os, json
from sqlalchemy import create_engine, text
c = create_engine(os.environ["DATABASE_URL"]).connect()
rows = c.execute(text(
    "select symbol, updated_at, "
    "risk_snapshot_json->'momentum_live_execution'->'entry_place_result' as epr "
    "from trading_automation_sessions where mode='live' and state='live_error' "
    "and risk_snapshot_json->'momentum_live_execution'->>'entry_submitted'='true' "
    "order by updated_at desc limit 6")).fetchall()
print(f"latest place-isError sessions: {len(rows)}")
for sym, ts, epr in rows:
    err = (epr or {}).get("error") if isinstance(epr, dict) else epr
    print(f"  {sym} @ {str(ts)[11:19]}: {err}")
# ff5cddc (#784) deployed ~14:55 UTC — anything after has the fix
