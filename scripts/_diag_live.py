"""Live state check — is monitor actually running, and what does it see?"""
import os, json
from dotenv import load_dotenv
load_dotenv()
import psycopg2, psycopg2.extras

conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
def q(sql, *args):
    cur.execute(sql, args)
    return cur.fetchall()

print("=== server 'now' (DB) ===")
print(" ", q("SELECT now() AS now_utc")[0])

print("\n=== last 20 autotrader_runs of ANY kind (proves monitor is ticking) ===")
rows = q("""
    SELECT created_at, ticker, decision, reason, trade_id
    FROM trading_autotrader_runs
    ORDER BY created_at DESC LIMIT 20
""")
for r in rows:
    print(f"  {r['created_at']} {r['ticker']:8s} {r['decision']:28s} "
          f"{(r['reason'] or '')[:40]}  trade={r['trade_id']}")

print("\n=== last 20 stop_decisions of any kind (stop_engine ticks) ===")
rows = q("""
    SELECT as_of_ts, trade_id, state, trigger,
           substring(reason from 1 for 70) AS reason
    FROM trading_stop_decisions
    ORDER BY as_of_ts DESC LIMIT 20
""")
for r in rows:
    print(f"  {r['as_of_ts']} trade={r['trade_id']:4d} state={r['state']:10s} "
          f"trig={r['trigger']!s:14s} {r['reason']!s}")

print("\n=== latest stop_engine evaluation for deferred-stop trades ===")
deferred_ids = [296, 301, 369, 302, 300, 312, 291]
rows = q("""
    SELECT DISTINCT ON (sd.trade_id)
           sd.trade_id, sd.as_of_ts, sd.trigger, sd.state,
           sd.new_stop, sd.reason,
           t.ticker, t.stop_loss, t.entry_price
    FROM trading_stop_decisions sd
    JOIN trading_trades t ON t.id = sd.trade_id
    WHERE sd.trade_id = ANY(%s)
    ORDER BY sd.trade_id, sd.as_of_ts DESC
""", deferred_ids)
for r in rows:
    print(f"  trade={r['trade_id']} {r['ticker']:8s} "
          f"last_eval={r['as_of_ts']} trig={r['trigger']!s} state={r['state']} "
          f"stop_on_trade={r['stop_loss']} entry={r['entry_price']} "
          f"reason={(r['reason'] or '')[:80]}")

print("\n=== latest alert_history (trading_alerts) by ticker for deferred trades ===")
for tkr in ["PFSI", "AAON", "ABM", "GEO", "INTC", "JOB", "EKSO", "GENI", "DHC"]:
    rows = q("""
        SELECT created_at, alert_type
        FROM trading_alerts
        WHERE ticker=%s AND user_id=1
        ORDER BY created_at DESC LIMIT 1
    """, tkr)
    if rows:
        r = rows[0]
        print(f"  {tkr:6s} last_alert={r['created_at']} type={r['alert_type']}")

print("\n=== 24h autotrader_runs timeline (hourly bucket) ===")
rows = q("""
    SELECT date_trunc('hour', created_at) AS hr,
           COUNT(*) AS total,
           SUM(CASE WHEN decision LIKE 'monitor_exit%%' THEN 1 ELSE 0 END) AS monitor_exit_n,
           SUM(CASE WHEN decision='monitor_exit_deferred' THEN 1 ELSE 0 END) AS deferred_n,
           SUM(CASE WHEN decision='monitor_exit_filled' THEN 1 ELSE 0 END) AS filled_n,
           SUM(CASE WHEN decision='placed' THEN 1 ELSE 0 END) AS placed_n
    FROM trading_autotrader_runs
    WHERE created_at > now() - interval '24 hours'
    GROUP BY 1 ORDER BY 1 DESC
""")
for r in rows:
    print(f"  {r['hr']}  total={r['total']:4d}  exit_any={r['monitor_exit_n']:4d} "
          f"(deferred={r['deferred_n']:4d} filled={r['filled_n']:2d})  placed={r['placed_n']}")

print("\n=== brain_cycle_lease (is scheduler alive?) ===")
try:
    for r in q("""
        SELECT cycle_name, host, acquired_at, expires_at, released_at
        FROM brain_cycle_lease
        ORDER BY COALESCE(acquired_at, expires_at) DESC LIMIT 10
    """):
        print(" ", dict(r))
except Exception as e:
    print(" err:", e)

print("\n=== trading_automation_events -- autotrader monitor heartbeat? ===")
try:
    for r in q("""
        SELECT event_type, event_name, created_at, source, payload
        FROM trading_automation_events
        WHERE event_type LIKE '%%monitor%%' OR event_name LIKE '%%monitor%%'
           OR source LIKE '%%autotrader%%'
        ORDER BY created_at DESC LIMIT 5
    """):
        print(" ", dict(r))
except Exception as e:
    print(" err:", e)

print("\n=== Zombie trade RKLX #375 full row ===")
rows = q("SELECT * FROM trading_trades WHERE id=375")
if rows:
    r = dict(rows[0])
    for k in ("id","ticker","status","auto_trader_version","entry_price","exit_price",
             "quantity","stop_loss","take_profit","entry_date","exit_date","exit_reason",
             "pending_exit_status","pending_exit_reason","broker_order_id",
             "pending_exit_order_id","broker_status","filled_at","filled_quantity",
             "remaining_quantity","pnl"):
        print(f"  {k:24s} = {r.get(k)!r}")

print("\n=== ETH-USD #370 row (weird) ===")
rows = q("SELECT * FROM trading_trades WHERE id=370")
if rows:
    r = dict(rows[0])
    for k in ("id","ticker","status","auto_trader_version","entry_price",
             "quantity","stop_loss","take_profit","entry_date","broker_source",
             "broker_status","broker_order_id","filled_at"):
        print(f"  {k:24s} = {r.get(k)!r}")
