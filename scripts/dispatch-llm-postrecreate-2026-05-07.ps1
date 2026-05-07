# Re-run with correct schema. Question: have any autotrader_runs landed
# in the post-recreate window (>=18:57), and what shape is llm_snapshot?

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-llm-postrecreate-2026-05-07-output.txt"
"# llm postrecreate $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$chili = "chili-home-copilot-chili-1"

"" | Add-Content $out
"## post-recreate autotrader_runs (>=18:57)" | Add-Content $out
$q1 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("""
  SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE llm_snapshot::text LIKE '%parse_failed%') AS parse_failed,
    COUNT(*) FILTER (WHERE llm_snapshot::text LIKE '%"viable":%') AS has_viable,
    COUNT(*) FILTER (WHERE llm_snapshot::text LIKE '%"viable": true%') AS viable_true,
    COUNT(*) FILTER (WHERE llm_snapshot::text LIKE '%"viable": false%') AS viable_false,
    COUNT(*) FILTER (WHERE reason = 'llm_not_viable') AS reason_llm_not_viable,
    COUNT(*) FILTER (WHERE reason = 'projected_profit_below_min') AS reason_below_min,
    COUNT(*) FILTER (WHERE decision = 'placed') AS placed
  FROM trading_autotrader_runs
  WHERE created_at >= '2026-05-07 18:57:00'
""")
print(cur.fetchone())
print()
print("--- last 10 post-recreate runs ---")
cur.execute("""
  SELECT id, ticker, decision, reason, llm_snapshot::text, created_at
  FROM trading_autotrader_runs
  WHERE created_at >= '2026-05-07 18:57:00'
  ORDER BY created_at DESC
  LIMIT 10
""")
for r in cur.fetchall():
    rid, t, d, why, snap, ts = r
    snap_short = (snap or '')[:200]
    print(f"id={rid} {t} dec={d} reason={why} snap={snap_short!r} created={ts}")
cur.close()
conn.close()
'@
$q1 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## alerts (post-recreate, >=18:57) - alert_type/ticker/created_at" | Add-Content $out
$q2 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("""
  SELECT id, alert_type, ticker, created_at, scan_pattern_id, content_signature
  FROM trading_alerts
  WHERE created_at >= '2026-05-07 18:57:00'
  ORDER BY created_at DESC
  LIMIT 20
""")
for r in cur.fetchall():
    print(r)
print()
cur.execute("""
  SELECT alert_type, COUNT(*) AS n,
    MIN(created_at) AS first_seen,
    MAX(created_at) AS last_seen
  FROM trading_alerts
  WHERE created_at >= '2026-05-07 18:00:00'
  GROUP BY alert_type
  ORDER BY n DESC
""")
print("--- alert_type histogram (last hour) ---")
for r in cur.fetchall():
    print(r)
cur.close()
conn.close()
'@
$q2 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## why candidate_pool=0? Look at autotrader's alert query (recent processed_at)" | Add-Content $out
$q3 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
# Check trading_alerts_processed table
cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name='trading_alerts_processed'
  ORDER BY ordinal_position
""")
print("trading_alerts_processed cols:", cur.fetchall())
print()
cur.execute("""
  SELECT MAX(created_at) FROM trading_alerts_processed
""")
print("latest processed:", cur.fetchone())
print()
# check breakout_alert_id mapping
cur.execute("""
  SELECT breakout_alert_id, MAX(created_at) FROM trading_autotrader_runs
  WHERE created_at >= '2026-05-07 18:00:00'
  GROUP BY breakout_alert_id ORDER BY 2 DESC LIMIT 10
""")
print("--- recent processed breakout_alert_ids ---")
for r in cur.fetchall(): print(r)
cur.close()
conn.close()
'@
$q3 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"---" | Add-Content $out
"# end" | Add-Content $out
