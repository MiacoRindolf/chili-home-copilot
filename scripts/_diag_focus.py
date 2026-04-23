"""Focused follow-ups."""
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

print("=== Full open-trade snapshot for user 1 ===")
rows = q("""
    SELECT id, ticker, entry_price, stop_loss, take_profit, quantity, direction,
           broker_source, auto_trader_version, scan_pattern_id, related_alert_id,
           pending_exit_status, pending_exit_reason, pending_exit_requested_at,
           broker_order_id, pending_exit_order_id, entry_date, exit_date, exit_reason,
           management_scope, status
    FROM trading_trades
    WHERE user_id = 1 AND status = 'open'
    ORDER BY entry_date DESC
""")
print(f"total open: {len(rows)}")
for r in rows:
    print(f"  id={r['id']:4d} {r['ticker']:8s} entry={r['entry_price']} "
          f"stop={r['stop_loss']} tgt={r['take_profit']} "
          f"qty={r['quantity']} broker={r['broker_source']!s} v={r['auto_trader_version']!s} "
          f"pend={r['pending_exit_status']!s}/{r['pending_exit_reason']!s} "
          f"exit_reason={r['exit_reason']!s} exit_date={r['exit_date']!s}")

print("\n=== Count v1 opens ===")
for r in q("SELECT COUNT(*) AS n FROM trading_trades WHERE user_id=1 AND status='open' AND auto_trader_version='v1'"):
    print(" v1 open =", r['n'])

print("\n=== autotrader_runs 'placed' in last 7d ===")
for r in q("""
    SELECT created_at, ticker, trade_id, reason
    FROM trading_autotrader_runs
    WHERE decision='placed' AND created_at > now() - interval '7 days'
    ORDER BY created_at DESC
"""):
    print(" ", dict(r))

print("\n=== When did trades last CLOSE (last 7d) ===")
for r in q("""
    SELECT exit_date, ticker, exit_reason, pnl, auto_trader_version, broker_source, status
    FROM trading_trades
    WHERE user_id=1 AND exit_date > now() - interval '7 days'
    ORDER BY exit_date DESC LIMIT 30
"""):
    print(" ", dict(r))

print("\n=== Trades with 'deferred/stop' — how long deferred? ===")
for r in q("""
    SELECT id, ticker, stop_loss, quantity, pending_exit_reason,
           pending_exit_requested_at,
           (now() - pending_exit_requested_at) AS age
    FROM trading_trades
    WHERE user_id=1 AND status='open' AND pending_exit_status='deferred'
    ORDER BY pending_exit_requested_at ASC
"""):
    print(" ", dict(r))

print("\n=== Latest scan_pattern_ids on open trades ===")
for r in q("""
    SELECT t.id, t.ticker, t.scan_pattern_id, sp.name, sp.is_active, sp.lifecycle_stage
    FROM trading_trades t
    LEFT JOIN scan_patterns sp ON sp.id = t.scan_pattern_id
    WHERE t.user_id=1 AND t.status='open'
    ORDER BY t.id
"""):
    print(" ", dict(r))

print("\n=== recent autotrader_runs for user 1 (not system) -- scale_in decisions ===")
for r in q("""
    SELECT created_at, ticker, decision, reason
    FROM trading_autotrader_runs
    WHERE decision IN ('scaled_in','blocked','error')
      AND created_at > now() - interval '48 hours'
    ORDER BY created_at DESC LIMIT 30
"""):
    print(" ", dict(r))

print("\n=== brain risk dial value right now ===")
try:
    for r in q("""
        SELECT surface_name, payload_json, updated_at
        FROM trading_runtime_surface_state
        WHERE surface_name IN ('regime','risk_dial')
        ORDER BY updated_at DESC LIMIT 5
    """):
        print(" ", dict(r))
except Exception as e:
    print(" err:", e)

print("\n=== robinhood connection / autotrader desk payload ===")
for r in q("""
    SELECT slice_name, mode, payload_json, updated_at, updated_by, reason
    FROM trading_brain_runtime_modes
    WHERE slice_name = 'autotrader_v1_desk'
"""):
    print(" ", dict(r))

print("\n=== venue_health for robinhood (recent) ===")
try:
    for r in q("SELECT * FROM trading_venue_health ORDER BY ts DESC LIMIT 5"):
        print(" ", dict(r))
except Exception as e:
    print(" (no trading_venue_health:", e, ")")

print("\n=== Any stop_hit alerts -> matching monitor_exit audits ===")
rows = q("""
    SELECT ta.created_at AS alert_ts, ta.ticker, ta.user_id AS alert_uid,
           (
             SELECT decision || '|' || reason
             FROM trading_autotrader_runs ar
             WHERE ar.ticker = ta.ticker
               AND ar.created_at BETWEEN ta.created_at - interval '2 minutes' AND ta.created_at + interval '2 minutes'
             ORDER BY abs(extract(epoch FROM (ar.created_at - ta.created_at)))
             LIMIT 1
           ) AS nearest_run
    FROM trading_alerts ta
    WHERE ta.alert_type='stop_hit' AND ta.created_at > now() - interval '8 hours'
    ORDER BY ta.created_at DESC
""")
print(f"stop_hit alerts in last 8h: {len(rows)}")
for r in rows[:25]:
    print(f"  {r['alert_ts']} {r['ticker']:8s} uid={r['alert_uid']}  nearest_run={r['nearest_run']}")
