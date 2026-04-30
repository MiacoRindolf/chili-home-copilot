$out = "scripts/dispatch-r25-bbj-diag-output.txt"
"# r25 brain_batch_jobs heartbeat diagnosis $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "scheduler-worker uptime + role" {
    docker ps --filter "name=chili-home-copilot-scheduler-worker-1" --format "{{.Names}} | {{.Status}}"
    docker compose exec -T scheduler-worker sh -c 'echo "CHILI_SCHEDULER_ROLE=$CHILI_SCHEDULER_ROLE"'
}

S "scheduler-worker logs: brain_batch_reconciler job activity last 1 hour" {
    docker compose logs --since 1h scheduler-worker 2>&1 | Select-String -Pattern "brain_batch_reconciler|reconcile_stale_batch_jobs|orphaned" | Select-Object -Last 30
}

S "stale running rows DETAILED (with heartbeat_at + age + worker_instance_id)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, job_type, status, started_at, heartbeat_at, worker_instance_id, EXTRACT(EPOCH FROM (NOW() - started_at))/60 AS age_min, CASE WHEN heartbeat_at IS NULL THEN 'no_hb' ELSE 'hb_at_' || ROUND(EXTRACT(EPOCH FROM (NOW() - heartbeat_at))/60.0)::int::text || 'min' END AS hb_status FROM brain_batch_jobs WHERE status = 'running' ORDER BY started_at LIMIT 30;"
}

S "is the brain_batch_reconciler job actually registered in scheduler-worker?" {
    docker compose exec -T scheduler-worker python -c @"
from app.services import trading_scheduler as ts
sched = getattr(ts, '_scheduler', None) or getattr(ts, 'scheduler', None)
if sched is None:
    print('NO SCHEDULER GLOBAL')
else:
    jobs = sched.get_jobs()
    print(f'jobs registered: {len(jobs)}')
    for j in jobs:
        if 'reconciler' in j.id or 'batch' in j.id.lower():
            print(f'  id={j.id} next_run={j.next_run_time}')
"@
}

S "what role flags would scheduler-worker compute? (debugging include_web_light)" {
    docker compose exec -T scheduler-worker python -c @"
import os
role = (os.environ.get('CHILI_SCHEDULER_ROLE') or '').lower()
print(f'role={role!r}')
print(f'include_web_light = role in (all, web, cron_only) -> {role in (\"all\", \"web\", \"cron_only\")}')
print(f'include_broker_sync = role in (all, web, worker, broker_sync_only) -> {role in (\"all\", \"web\", \"worker\", \"broker_sync_only\")}')
"@
}

S "reconciler called manually right now (catches the stale rows)" {
    docker compose exec -T scheduler-worker python -c @"
from app.db import SessionLocal
from app.services.trading.brain_batch_reconciler import reconcile_stale_batch_jobs
db = SessionLocal()
try:
    out = reconcile_stale_batch_jobs(db)
    print('reconciler output:', out)
finally:
    db.close()
"@
}

S "stale running rows AFTER manual reconcile" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT status, COUNT(*) FROM brain_batch_jobs WHERE started_at > NOW() - INTERVAL '24 hours' GROUP BY status ORDER BY count DESC;"
}

Write-Host "diag done -- see $out"
