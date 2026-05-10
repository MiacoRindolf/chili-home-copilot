$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-alert-types-probe-out.txt"
"# d-alert-types-probe $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# alert_type breakdown (last 24h, last 1h)" | Add-Content $out
docker exec chili-home-copilot-chili-1 python -c @"
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
try:
    # Find columns first
    rows = db.execute(text('''
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'trading_alerts'
        ORDER BY ordinal_position
    ''')).fetchall()
    db.rollback()
    print('columns:', [r[0] for r in rows])

    # Try alert_type breakdown
    rows = db.execute(text('''
        SELECT alert_type, count(*) FROM trading_alerts
        WHERE created_at >= now() - interval '24 hours'
        GROUP BY alert_type
        ORDER BY 2 DESC
    ''')).fetchall()
    db.rollback()
    print()
    print('last 24h by alert_type:')
    for r in rows:
        print(f'  {r[0]:30s} count={r[1]}')

    rows = db.execute(text('''
        SELECT alert_type, count(*) FROM trading_alerts
        WHERE created_at >= now() - interval '1 hour'
        GROUP BY alert_type
        ORDER BY 2 DESC
    ''')).fetchall()
    db.rollback()
    print()
    print('last 1h by alert_type:')
    for r in rows:
        print(f'  {r[0]:30s} count={r[1]}')

    # Most recent 5 alerts
    rows = db.execute(text('''
        SELECT id, ticker, alert_type, created_at FROM trading_alerts
        ORDER BY created_at DESC LIMIT 5
    ''')).fetchall()
    db.rollback()
    print()
    print('most recent 5 alerts:')
    for r in rows:
        print(f'  id={r[0]} ticker={r[1]} type={r[2]} created={r[3]}')
finally:
    db.close()
"@ 2>&1 | Add-Content $out

"# autotrader-worker most recent 30 lines" | Add-Content $out
docker logs --tail 30 chili-home-copilot-autotrader-worker-1 2>&1 | Add-Content $out

"# end" | Add-Content $out
