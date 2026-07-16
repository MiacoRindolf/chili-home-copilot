import os, json
from sqlalchemy import create_engine, text
e = create_engine(os.environ["DATABASE_URL"])
c = e.connect()

# Today's live momentum sessions by state (the conversion funnel)
rows = c.execute(text(
    "select state, count(*) from trading_automation_sessions "
    "where mode='live' and updated_at >= date_trunc('day', now()) "
    "group by state order by 2 desc")).fetchall()
print("TODAY_sessions_by_state:", {r[0]: r[1] for r in rows})

# distinct symbols armed today + how many ever ENTERED (held a position)
armed = c.execute(text(
    "select count(distinct symbol) from trading_automation_sessions "
    "where mode='live' and updated_at >= date_trunc('day', now())")).scalar()
entered = c.execute(text(
    "select count(distinct symbol) from trading_automation_sessions where mode='live' "
    "and updated_at >= date_trunc('day', now()) "
    "and state in ('live_entered','live_trailing','live_scaling_out','live_bailout','live_exited')")).scalar()
print(f"TODAY distinct_symbols_armed={armed}  distinct_symbols_that_ENTERED={entered}")

# last-hour activity
for label, mins in (("last_60m", 60), ("last_15m", 15)):
    r2 = c.execute(text(
        "select state, count(*) from trading_automation_sessions where mode='live' "
        f"and updated_at > now() - interval '{mins} minutes' group by state order by 2 desc")).fetchall()
    print(f"{label}_by_state:", {x[0]: x[1] for x in r2})
