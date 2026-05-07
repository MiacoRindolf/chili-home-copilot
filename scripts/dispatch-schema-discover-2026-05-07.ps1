# Discover real schema of trading_autotrader_runs and trading_alerts.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-schema-discover-2026-05-07-output.txt"
"# schema discover $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$chili = "chili-home-copilot-chili-1"

"" | Add-Content $out
"## trading_autotrader_runs columns" | Add-Content $out
$q1 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("""
  SELECT column_name, data_type
  FROM information_schema.columns
  WHERE table_name='trading_autotrader_runs'
  ORDER BY ordinal_position
""")
for r in cur.fetchall():
    print(r)
print()
print("--- trading_alerts columns ---")
cur.execute("""
  SELECT column_name, data_type
  FROM information_schema.columns
  WHERE table_name='trading_alerts'
  ORDER BY ordinal_position
""")
for r in cur.fetchall():
    print(r)
cur.close()
conn.close()
'@
$q1 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## last 5 autotrader runs (using *)" | Add-Content $out
$q2 = @'
import psycopg2, json
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("SELECT * FROM trading_autotrader_runs ORDER BY id DESC LIMIT 5")
cols = [c.name for c in cur.description]
for r in cur.fetchall():
    d = dict(zip(cols, r))
    # Truncate any large blobs
    for k, v in list(d.items()):
        s = str(v)
        if len(s) > 220:
            d[k] = s[:220] + "...TRUNC"
    print(d)
cur.close()
conn.close()
'@
$q2 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"## last 10 alerts (any type) by fired_at" | Add-Content $out
$q3 = @'
import psycopg2
conn = psycopg2.connect(host="postgres", dbname="chili", user="chili", password="chili")
cur = conn.cursor()
cur.execute("SELECT * FROM trading_alerts ORDER BY fired_at DESC LIMIT 10")
cols = [c.name for c in cur.description]
for r in cur.fetchall():
    d = dict(zip(cols, r))
    for k, v in list(d.items()):
        s = str(v)
        if len(s) > 200:
            d[k] = s[:200] + "...TRUNC"
    print(d)
cur.close()
conn.close()
'@
$q3 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"---" | Add-Content $out
"# end" | Add-Content $out
