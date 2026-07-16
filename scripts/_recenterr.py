import os, json
from sqlalchemy import create_engine, text
e = create_engine(os.environ["DATABASE_URL"])
c = e.connect()
rows = c.execute(text(
    "select id, symbol, updated_at, risk_snapshot_json from trading_automation_sessions "
    "where mode='live' and state='live_error' and updated_at > now() - interval '20 min' "
    "order by updated_at desc limit 12")).fetchall()
print(f"recent live_error sessions: {len(rows)}")
for sid, sym, ts, snap in rows:
    snap = snap or {}
    mle = snap.get("momentum_live_execution", {}) if isinstance(snap, dict) else {}
    epr = mle.get("entry_place_result") or {}
    err = epr.get("error") if isinstance(epr, dict) else None
    submitted = mle.get("entry_submitted")
    qty = (mle.get("entry_notional_guard") or {}).get("quantity") if isinstance(mle.get("entry_notional_guard"), dict) else None
    lpx = mle.get("entry_limit_price")
    print(f"  {sym} (s{sid}, {str(ts)[11:19]}): submitted={submitted} qty={qty} px={lpx}")
    print(f"      ERROR: {err}")
