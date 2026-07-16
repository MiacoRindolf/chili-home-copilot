import os
from sqlalchemy import create_engine, text
e = create_engine(os.environ["DATABASE_URL"])
c = e.connect()
for sym in ("EHGO", "NXTS", "AVTX"):
    print(f"=== {sym} ===")
    rows = c.execute(text(
        "select state, count(*) n from trading_automation_sessions where mode='live' and symbol=:s "
        "and updated_at >= date_trunc('day', now()) group by state order by n desc"), {"s": sym}).fetchall()
    print("  today_states:", {r[0]: r[1] for r in rows} or "NEVER ARMED today")
    last = c.execute(text(
        "select state, updated_at from trading_automation_sessions where mode='live' and symbol=:s "
        "order by updated_at desc limit 1"), {"s": sym}).fetchone()
    print("  last_session:", (last[0], str(last[1])[11:19]) if last else "none")
    # viability now
    v = c.execute(text(
        "select viability_score, live_eligible, updated_at from momentum_symbol_viability where symbol=:s "
        "order by updated_at desc limit 1"), {"s": sym}).fetchone()
    print("  viability:", (round(float(v[0]),3) if v and v[0] is not None else None, "eligible=" + str(v[1]) if v else None))
