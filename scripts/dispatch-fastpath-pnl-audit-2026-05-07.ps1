# Pull every shred of realized P&L data the fast-path has produced.
# Goal: ground the architectural audit in real numbers.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-fastpath-pnl-audit-2026-05-07-output.txt"
"# fastpath pnl audit $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$chili = "chili-home-copilot-chili-1"

"" | Add-Content $out
"## Schema discovery for fast-path tables" | Add-Content $out
$qs = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
for t in ["fast_alerts","fast_snapshots","fast_orderbook","fast_signal_decay","fast_path_status","fast_executions","fast_exits","fast_paper_trades"]:
    cur.execute("""
      SELECT column_name, data_type FROM information_schema.columns
      WHERE table_name = %s ORDER BY ordinal_position
    """, (t,))
    rows = cur.fetchall()
    print(f"=== {t} ({len(rows)} cols)")
    for r in rows: print("  ", r)
cur.close(); conn.close()
'@
$qs | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## All fast_* tables that exist" | Add-Content $out
$q0 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("""
  SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'fast%'
  ORDER BY 1
""")
for r in cur.fetchall(): print(r)
cur.close(); conn.close()
'@
$q0 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## fast_alerts: total volume + by signal type" | Add-Content $out
$q1 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("SELECT COUNT(*), MIN(fired_at), MAX(fired_at) FROM fast_alerts")
print("total alerts (lifetime):", cur.fetchone())
cur.execute("""
  SELECT alert_type, ticker, COUNT(*),
         AVG(score)::numeric(6,4) AS avg_score,
         MAX(fired_at) AS latest
  FROM fast_alerts
  GROUP BY alert_type, ticker
  ORDER BY 3 DESC
  LIMIT 60
""")
print()
print("--- alerts by (alert_type, ticker) ---")
for r in cur.fetchall(): print(r)
cur.close(); conn.close()
'@
$q1 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## fast_signal_decay: realized forward-return by signal/ticker/score bucket" | Add-Content $out
$q2 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("""
  SELECT column_name, data_type FROM information_schema.columns
  WHERE table_name='fast_signal_decay' ORDER BY ordinal_position
""")
print("fast_signal_decay cols:", cur.fetchall())
print()
cur.execute("SELECT COUNT(*) FROM fast_signal_decay")
print("rows:", cur.fetchone())
print()
# Sample top rows
cur.execute("""
  SELECT * FROM fast_signal_decay
  WHERE sample_count >= 5
  ORDER BY sample_count DESC LIMIT 30
""")
cols = [c.name for c in cur.description]
for r in cur.fetchall():
    d = dict(zip(cols, r))
    print(d)
cur.close(); conn.close()
'@
$q2 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## decay_miner observations: pending heap, finalized, validations" | Add-Content $out
$q3 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
# Whatever observation table exists
cur.execute("""
  SELECT tablename FROM pg_tables
  WHERE schemaname='public' AND (tablename LIKE '%observation%' OR tablename LIKE '%decay%')
  ORDER BY 1
""")
print("candidate tables:", cur.fetchall())
cur.close(); conn.close()
'@
$q3 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## paper trades + realized P&L (Trade table where management_scope LIKE 'fast%' OR similar)" | Add-Content $out
$q4 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("""
  SELECT DISTINCT management_scope FROM trades
  WHERE management_scope IS NOT NULL
  ORDER BY 1
""")
print("management_scopes:", cur.fetchall())
print()
# trades with fast_path scope
cur.execute("""
  SELECT id, ticker, status, asset_type, management_scope,
         entry_price, exit_price, pnl_pct, pnl_dollars, realized_pnl_pct,
         entry_time, exit_time
  FROM trades
  WHERE management_scope LIKE '%fast%' OR management_scope LIKE '%scalp%'
  ORDER BY id DESC LIMIT 30
""")
cols = [c.name for c in cur.description]
print("--- fast-path trades (last 30) ---")
for r in cur.fetchall():
    d = dict(zip(cols, r))
    print(d)
cur.close(); conn.close()
'@
$q4 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## current open paper position SOL-USD detail" | Add-Content $out
$q5 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("""
  SELECT id, ticker, status, entry_price, current_price, qty,
         entry_time, management_scope, asset_type, pnl_pct
  FROM trades
  WHERE ticker = 'SOL-USD' AND management_scope LIKE '%fast%'
  ORDER BY id DESC LIMIT 5
""")
cols = [c.name for c in cur.description]
for r in cur.fetchall():
    print(dict(zip(cols, r)))
cur.close(); conn.close()
'@
$q5 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## settings.py thresholds for context" | Add-Content $out
docker exec $chili sh -c 'grep -nE "^[A-Z_]+ *=" /app/app/services/trading/fast_path/settings.py 2>&1 | head -60' 2>&1 | Add-Content $out

"" | Add-Content $out
"---" | Add-Content $out
"# end" | Add-Content $out
