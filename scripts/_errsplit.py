import os
from sqlalchemy import create_engine, text
e = create_engine(os.environ["DATABASE_URL"])
c = e.connect()
# RTH opens 13:30 UTC (09:30 ET). Split today's live_error premarket vs RTH.
pre = c.execute(text(
    "select count(*) from trading_automation_sessions where mode='live' and state='live_error' "
    "and updated_at >= date_trunc('day', now()) "
    "and (updated_at AT TIME ZONE 'UTC')::time < time '13:30'")).scalar()
rth = c.execute(text(
    "select count(*) from trading_automation_sessions where mode='live' and state='live_error' "
    "and updated_at >= date_trunc('day', now()) "
    "and (updated_at AT TIME ZONE 'UTC')::time >= time '13:30'")).scalar()
print(f"live_error TODAY: premarket(<13:30 UTC)={pre}  RTH(>=13:30 UTC)={rth}")
# Of RTH errors, top symbols
print("=== RTH live_error by symbol (top 12) ===")
rows = c.execute(text(
    "select symbol, count(*) n from trading_automation_sessions where mode='live' and state='live_error' "
    "and updated_at >= date_trunc('day', now()) and (updated_at AT TIME ZONE 'UTC')::time >= time '13:30' "
    "group by symbol order by n desc limit 12")).fetchall()
for s, n in rows:
    print(f"  {s}: {n}")
