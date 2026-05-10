$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-autotrader-worker-state-out.txt"
"# d-autotrader-worker-state $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# step 1: container state" | Add-Content $out
docker ps --filter "name=chili-home-copilot-autotrader-worker-1" --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}" 2>&1 | Add-Content $out

"# step 2: last 80 lines of autotrader-worker logs" | Add-Content $out
docker logs --tail 80 chili-home-copilot-autotrader-worker-1 2>&1 | Add-Content $out

"# step 3: process state inside container" | Add-Content $out
docker exec chili-home-copilot-autotrader-worker-1 bash -c "ps -ef | head -20" 2>&1 | Add-Content $out

"# step 4: filter for autotrader cycle activity in last 30 min" | Add-Content $out
docker logs --since 30m chili-home-copilot-autotrader-worker-1 2>&1 | Select-String -Pattern '(autotrader|cycle|tick|alert|imminent|selector|cost_gate)' | Select-Object -First 30 | ForEach-Object { $_.Line } | Add-Content $out

"# step 5: pgrep for any autotrader-related blocking" | Add-Content $out
docker exec chili-home-copilot-autotrader-worker-1 python -c @"
import psycopg2
import os
conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor()
cur.execute(""""""
SELECT application_name, state, query_start, wait_event_type, wait_event,
       LEFT(query, 80) AS q
FROM pg_stat_activity
WHERE application_name LIKE '%%autotrader%%' OR application_name LIKE '%%worker%%'
   OR query LIKE '%%autotrader%%' OR query LIKE '%%alert%%'
ORDER BY query_start DESC
LIMIT 20
"""""")
for r in cur.fetchall():
    print(' '.join(str(x) for x in r))
conn.close()
"@ 2>&1 | Add-Content $out

"# step 6: are alerts queued unprocessed?" | Add-Content $out
docker exec chili-home-copilot-autotrader-worker-1 python -c @"
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
try:
    rows = db.execute(text('''
        SELECT count(*) AS c
        FROM trading_alerts
        WHERE created_at >= now() - interval '24 hours'
    ''')).fetchall()
    print('alerts last 24h:', rows[0][0])
    db.rollback()
    rows = db.execute(text('''
        SELECT id, ticker, alert_type, created_at, processed_at, status
        FROM trading_alerts
        WHERE created_at >= now() - interval '6 hours'
        ORDER BY created_at DESC
        LIMIT 10
    ''')).fetchall()
    db.rollback()
    print('most recent 10 alerts (last 6h):')
    for r in rows:
        print(' ', dict(r._mapping))
finally:
    db.close()
"@ 2>&1 | Add-Content $out

"# end" | Add-Content $out
