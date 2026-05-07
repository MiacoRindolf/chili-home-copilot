# Why have no pattern_breakout_imminent alerts fired since 18:43?
# brain-worker emits these. Check its liveness + recent activity.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-brain-cadence-2026-05-07-output.txt"
"# brain cadence $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"" | Add-Content $out
"## docker compose ps - everything" | Add-Content $out
docker compose ps --format 'table {{.Name}}\t{{.Service}}\t{{.Status}}' 2>&1 | Add-Content $out

"" | Add-Content $out
"## brain-worker last 60 log lines" | Add-Content $out
docker logs --tail=60 chili-home-copilot-brain-worker-1 2>&1 | Select-Object -Last 60 | Add-Content $out

"" | Add-Content $out
"## brain-worker pattern_breakout cadence (psql)" | Add-Content $out
$q1 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
print("--- pattern_breakout_imminent timeline (last 4h) ---")
cur.execute("""
  SELECT id, ticker, created_at, scan_pattern_id, content_signature
  FROM trading_alerts
  WHERE alert_type = 'pattern_breakout_imminent'
    AND created_at >= NOW() - interval '4 hours'
  ORDER BY created_at DESC
""")
for r in cur.fetchall(): print(r)

print()
print("--- last 5 brain reconcile / scan events from any *log* table? ---")
cur.execute("""
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='public' AND (table_name LIKE '%scan%' OR table_name LIKE '%brain%' OR table_name LIKE '%heart%')
  ORDER BY 1
""")
for r in cur.fetchall(): print(r)
cur.close()
conn.close()
'@
$q1 | docker exec -i chili-home-copilot-chili-1 python 2>&1 | Add-Content $out

"" | Add-Content $out
"## scheduler-worker last 30 log lines (mining/scan worker)" | Add-Content $out
docker logs --tail=30 chili-home-copilot-scheduler-worker-1 2>&1 | Select-Object -Last 30 | Add-Content $out

"" | Add-Content $out
"## chili-cron / chili-mining last 20 lines (if exist)" | Add-Content $out
docker compose ps --format '{{.Name}}' 2>&1 | Where-Object { $_ -match 'chili|cron|mining|scan' } | ForEach-Object {
    "" | Add-Content $out
    "### container: $_" | Add-Content $out
    docker logs --tail=20 $_ 2>&1 | Select-Object -Last 20 | Add-Content $out
}

"" | Add-Content $out
"---" | Add-Content $out
"# end" | Add-Content $out
