$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-pattern-roster-probe-out.txt"
"# d-pattern-roster-probe $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# Promoted-pattern roster: scan_patterns by lifecycle_stage + promotion_status" | Add-Content $out
docker exec chili-home-copilot-chili-1 python -c @"
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
try:
    rows = db.execute(text('''
        SELECT lifecycle_stage, promotion_status, count(*) AS c, count(*) FILTER (WHERE active) AS active_c
        FROM scan_patterns
        GROUP BY lifecycle_stage, promotion_status
        ORDER BY c DESC
        LIMIT 40
    ''')).fetchall()
    db.rollback()
    print('scan_patterns by lifecycle_stage + promotion_status (top 40):')
    for r in rows:
        ls = r[0] or '<null>'
        ps = r[1] or '<null>'
        print(f'  lifecycle={ls:15s} promo={ps:15s} count={r[2]:>5} active={r[3]:>5}')

    # Eligible for pattern_imminent: lifecycle_stage IN ('promoted','live') OR promotion_status='promoted'
    rows = db.execute(text('''
        SELECT count(*) FROM scan_patterns
        WHERE LOWER(COALESCE(lifecycle_stage,'')) IN ('promoted','live')
           OR LOWER(COALESCE(promotion_status,'')) = 'promoted'
    ''')).fetchall()
    db.rollback()
    print()
    print(f'pattern_imminent-ELIGIBLE patterns: {rows[0][0]}')

    rows = db.execute(text('''
        SELECT count(*) FROM scan_patterns
        WHERE active = true
          AND (LOWER(COALESCE(lifecycle_stage,'')) IN ('promoted','live')
               OR LOWER(COALESCE(promotion_status,'')) = 'promoted')
    ''')).fetchall()
    db.rollback()
    print(f'pattern_imminent-ELIGIBLE AND active: {rows[0][0]}')

    rows = db.execute(text('''
        SELECT id, ticker, lifecycle_stage, promotion_status, active, last_validated_at
        FROM scan_patterns
        WHERE LOWER(COALESCE(lifecycle_stage,'')) IN ('promoted','live')
           OR LOWER(COALESCE(promotion_status,'')) = 'promoted'
        ORDER BY id DESC LIMIT 10
    ''')).fetchall()
    db.rollback()
    print()
    print('eligible patterns (top 10 by id):')
    for r in rows:
        ls = r[2] or 'null'
        ps = r[3] or 'null'
        print('  id=%-5d %-8s life=%-12s promo=%-12s active=%s last_val=%s' % (r[0], r[1] or '?', ls, ps, r[4], r[5]))

    # Total counts for sanity
    rows = db.execute(text('SELECT count(*) FROM scan_patterns')).fetchall()
    db.rollback()
    print()
    print(f'scan_patterns total rows: {rows[0][0]}')
    rows = db.execute(text('SELECT count(*) FROM scan_patterns WHERE active = true')).fetchall()
    db.rollback()
    print(f'scan_patterns active=true: {rows[0][0]}')
finally:
    db.close()
"@ 2>&1 | Add-Content $out

"# end" | Add-Content $out
