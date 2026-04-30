$out = "scripts/dispatch-cron-check-output.txt"
"# cron registration check $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "scheduler-worker startup logs (look for FIX C5 + EV demote registration)" {
    docker compose logs scheduler-worker 2>&1 | Select-String -Pattern "FIX C5|realized_ev_demote_pass|breaker_heartbeat|canonical-job" | Select-Object -Last 20
}

S "schedule env vars" {
    docker compose exec -T scheduler-worker env 2>&1 | Select-String -Pattern "REALIZED_EV|BREAKER_HEARTBEAT|chili_realized" | Sort-Object
}

S "settings via python" {
    docker compose exec -T scheduler-worker python -c "from app.config import settings; print('chili_realized_ev_demote_pass_enabled=', getattr(settings, 'chili_realized_ev_demote_pass_enabled', '<MISSING>')); print('chili_realized_ev_demote_settle_days=', getattr(settings, 'chili_realized_ev_demote_settle_days', '<MISSING>'))" 2>&1
}

S "live job list (introspect via python)" {
    docker compose exec -T scheduler-worker python -c "
import json, time, urllib.request
# We cannot easily see the running scheduler from a fresh process,
# so just verify the registration code path by re-registering.
from app.services.trading_scheduler import _scheduler
print('scheduler is None?', _scheduler is None)
if _scheduler is not None:
    jobs = [(j.id, j.name, str(j.next_run_time)) for j in _scheduler.get_jobs()]
    print('total jobs:', len(jobs))
    for j in jobs:
        if 'realized_ev' in j[0] or 'breaker' in j[0] or 'macro' in j[0]:
            print(j)
" 2>&1
}

Write-Host "done"
