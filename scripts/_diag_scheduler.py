import os, json
from dotenv import load_dotenv
load_dotenv()
import psycopg2, psycopg2.extras
conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
def q(sql, *args):
    cur.execute(sql, args); return cur.fetchall()

print("=== brain_cycle_lease recent ===")
for r in q("""
    SELECT scope_key, holder_id, acquired_at, expires_at,
           (now() - GREATEST(acquired_at, expires_at)) AS age
    FROM brain_cycle_lease
    ORDER BY GREATEST(acquired_at, expires_at) DESC NULLS LAST LIMIT 15
"""):
    print(" ", dict(r))

print("\n=== trading_automation_events last 30 ===")
for r in q("""
    SELECT ts, event_type, source_node_id, correlation_id,
           substring(payload_json::text from 1 for 80) AS payload_head
    FROM trading_automation_events
    ORDER BY ts DESC LIMIT 30
"""):
    print(f"  {r['ts']} {r['event_type']:30s} src={r['source_node_id']!s:30s} "
          f"payload={r['payload_head']}")

print("\n=== trading_automation_sessions ===")
try:
    for r in q("SELECT * FROM trading_automation_sessions ORDER BY id DESC LIMIT 5"):
        print(" ", dict(r))
except Exception as e:
    print(" err:", e)

print("\n=== when did trading_autotrader_runs LAST record anything (per decision)? ===")
for r in q("""
    SELECT decision, MAX(created_at) AS last_at, COUNT(*) AS total_24h
    FROM trading_autotrader_runs
    WHERE created_at > now() - interval '24 hours'
    GROUP BY decision
    ORDER BY last_at DESC
"""):
    print(" ", dict(r))

print("\n=== StopDecision rows in last 6h (how many) ===")
for r in q("""
    SELECT date_trunc('hour', as_of_ts) AS hr,
           COUNT(*) AS n,
           COUNT(DISTINCT trade_id) AS trades,
           string_agg(DISTINCT trigger, ',') AS triggers
    FROM trading_stop_decisions
    WHERE as_of_ts > now() - interval '6 hours'
    GROUP BY 1 ORDER BY 1 DESC
"""):
    print(" ", dict(r))
