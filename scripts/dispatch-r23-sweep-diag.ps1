$out = "scripts/dispatch-r23-sweep-diag-output.txt"
"# r23 sweep diagnostic $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "now() and broker-sync-worker uptime" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT NOW();"
    docker ps --filter "name=chili-home-copilot-broker-sync-worker-1" --format "{{.Names}} | {{.Status}}"
}

S "FULL broker-sync-worker logs since restart (look for crashes / errors)" {
    docker compose logs broker-sync-worker 2>&1 | Select-Object -Last 200
}

S "broker-sync-worker logs filtered: ERROR / Traceback / authoritative / bracket" {
    docker compose logs broker-sync-worker 2>&1 | Select-String -Pattern "ERROR|Traceback|authoritative|bracket|writer|sweep" | Select-Object -Last 80
}

S "scheduler jobs registered (in-process)" {
    docker compose exec -T broker-sync-worker python -c @"
import os, sys
os.chdir('/app')
sys.path.insert(0, '/app')
from app.services import trading_scheduler as ts
sched = getattr(ts, '_scheduler', None) or getattr(ts, 'scheduler', None)
if sched is None:
    print('NO SCHEDULER GLOBAL')
else:
    jobs = sched.get_jobs()
    print(f'jobs registered: {len(jobs)}')
    for j in jobs:
        print(f'  id={j.id} name={j.name!r} next_run={j.next_run_time} trigger={j.trigger}')
"@
}

S "test calling run_reconciliation_sweep directly inside container" {
    docker compose exec -T broker-sync-worker python -c @"
from app.db import SessionLocal
from app.services.trading.bracket_reconciliation_service import run_reconciliation_sweep, _effective_mode
print('effective mode:', _effective_mode())
db = SessionLocal()
try:
    summary = run_reconciliation_sweep(db)
    print('sweep ok:', summary.to_dict())
except Exception as e:
    import traceback
    print('SWEEP RAISED:', e)
    traceback.print_exc()
finally:
    db.close()
"@
}

Write-Host "diag complete -- see $out"
