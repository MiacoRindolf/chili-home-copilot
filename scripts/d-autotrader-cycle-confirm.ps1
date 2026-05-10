$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-autotrader-cycle-confirm-out.txt"
"# d-autotrader-cycle-confirm $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# autotrader-worker last 60s of logs (filter for autotrader activity)" | Add-Content $out
docker logs --since 60s chili-home-copilot-autotrader-worker-1 2>&1 | Select-String -Pattern '(\[autotrader\]|\[scheduler\] auto_trader|selector:|cost_gate:|coinbase_cap:|placed|skipped|blocked|imminent|alert)' | Select-Object -First 60 | ForEach-Object { $_.Line } | Add-Content $out

"---" | Add-Content $out
"# DB autotrader_runs in last 5 min" | Add-Content $out
docker exec chili-home-copilot-chili-1 python -c @"
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
try:
    rows = db.execute(text('''
        SELECT count(*) FROM trading_autotrader_runs
        WHERE created_at >= now() - interval '5 minutes'
    ''')).fetchall()
    print('autotrader_runs last 5min:', rows[0][0])
    db.rollback()
    rows = db.execute(text('''
        SELECT decision, reason, count(*) FROM trading_autotrader_runs
        WHERE created_at >= now() - interval '5 minutes'
        GROUP BY decision, reason
        ORDER BY 3 DESC
    ''')).fetchall()
    db.rollback()
    if rows:
        print('breakdown last 5min:')
        for r in rows:
            print(f'  decision={r[0]} reason={r[1]} count={r[2]}')
    else:
        print('breakdown last 5min: (empty)')
finally:
    db.close()
"@ 2>&1 | Add-Content $out

"# end" | Add-Content $out
