# Check whether autotrader-worker has ticked since the 18:57 force-recreate
# and whether the LLM cascade is now returning viable responses (not parse_failed).

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-autotrader-tick-check-2026-05-07-output.txt"
"# autotrader tick check $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$chili = "chili-home-copilot-chili-1"

"" | Add-Content $out
"## Autotrader runs since 18:57 (post-recreate)" | Add-Content $out
$q1 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("""
  SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE result::text LIKE '%parse_failed%') AS parse_failed,
    COUNT(*) FILTER (WHERE result::text LIKE '%"viable":%') AS has_viable_key,
    COUNT(*) FILTER (WHERE result::text LIKE '%"viable": true%') AS viable_true,
    COUNT(*) FILTER (WHERE result::text LIKE '%"viable": false%') AS viable_false,
    MAX(created_at) AS latest,
    MIN(created_at) AS earliest
  FROM trading_autotrader_runs
  WHERE created_at >= '2026-05-07 18:57:00'
""")
print("post-recreate runs:", cur.fetchone())
cur.execute("""
  SELECT id, ticker, created_at, LEFT(result::text, 240)
  FROM trading_autotrader_runs
  WHERE created_at >= '2026-05-07 18:57:00'
  ORDER BY created_at DESC
  LIMIT 10
""")
print("\nlast 10 post-recreate runs:")
for r in cur.fetchall():
    print(r)
cur.close()
conn.close()
'@
$q1 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## Most recent autotrader_runs overall (any time)" | Add-Content $out
$q2 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("""
  SELECT id, ticker, created_at, LEFT(result::text, 200)
  FROM trading_autotrader_runs
  ORDER BY created_at DESC
  LIMIT 5
""")
for r in cur.fetchall():
    print(r)
cur.close()
conn.close()
'@
$q2 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## Recent stock pattern_imminent alerts (last 30min)" | Add-Content $out
$q3 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("""
  SELECT id, ticker, kind, fired_at
  FROM trading_alerts
  WHERE kind LIKE '%breakout%' OR kind LIKE '%pattern%'
  AND fired_at >= NOW() - interval '30 minutes'
  ORDER BY fired_at DESC
  LIMIT 15
""")
for r in cur.fetchall():
    print(r)
cur.close()
conn.close()
'@
$q3 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## autotrader-worker last 30 log lines" | Add-Content $out
docker logs --tail=30 chili-home-copilot-autotrader-worker-1 2>&1 | Add-Content $out

"" | Add-Content $out
"---" | Add-Content $out
"# end" | Add-Content $out
