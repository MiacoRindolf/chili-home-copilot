import os
from sqlalchemy import create_engine, text
e = create_engine(os.environ["DATABASE_URL"])
c = e.connect()
print("=== live_error sessions TODAY by symbol (top 20) ===")
rows = c.execute(text(
    "select symbol, count(*) n from trading_automation_sessions "
    "where mode='live' and state='live_error' and updated_at >= date_trunc('day', now()) "
    "group by symbol order by n desc limit 20")).fetchall()
for sym, n in rows:
    print(f"  {sym}: {n}")
print("=== distinct symbols that errored today ===")
d = c.execute(text(
    "select count(distinct symbol) from trading_automation_sessions "
    "where mode='live' and state='live_error' and updated_at >= date_trunc('day', now())")).scalar()
print("  distinct_errored_symbols =", d)
print("=== live_error in last 20m by symbol (current churn) ===")
r2 = c.execute(text(
    "select symbol, count(*) n from trading_automation_sessions "
    "where mode='live' and state='live_error' and updated_at > now() - interval '20 min' "
    "group by symbol order by n desc limit 12")).fetchall()
for sym, n in r2:
    print(f"  {sym}: {n}")
