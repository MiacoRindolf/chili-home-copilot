$out = "scripts/dispatch-autotrader-monitor-live-output.txt"
"# is auto_trader_monitor actually firing? $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "1. is auto_trader_monitor in the live scheduler?" {
    docker compose exec -T autotrader-worker python -c "from app.services import trading_scheduler as ts; sched = getattr(ts, '_scheduler', None); jobs = sched.get_jobs() if sched else []; print('total jobs:', len(jobs)); [print(f'  {j.id} next={j.next_run_time}') for j in jobs]"
}

S "2. autotrader-worker logs: any auto_trader_monitor activity (last 30 min)" {
    docker compose logs --since 30m autotrader-worker 2>&1 | Select-String -Pattern "auto_trader_monitor|tick_auto_trader_monitor|crypto_exit_pass" | Select-Object -Last 20
}

S "3. autotrader-worker logs: ALL apscheduler activity last 5 min" {
    docker compose logs --since 5m autotrader-worker 2>&1 | Select-String -Pattern "apscheduler|Running job|executed successfully" | Select-Object -Last 30
}

S "4. all brain_batch_jobs by job_type last 24h" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT job_type, COUNT(*), MAX(started_at) AS most_recent FROM brain_batch_jobs WHERE started_at > NOW() - INTERVAL '24 hours' GROUP BY job_type ORDER BY count DESC LIMIT 20;"
}

S "5. autotrader-worker very recent log lines" {
    docker compose logs --tail=60 autotrader-worker 2>&1 | Select-Object -Last 60
}

S "6. is run_scheduler_job_guarded gating on the breaker?" {
    docker compose exec -T autotrader-worker sh -c 'grep -n -A 3 "def run_scheduler_job_guarded\|_breaker_kill\|_breaker_active\|drawdown_breaker" /app/app/services/trading_scheduler.py 2>/dev/null | head -40'
}

Write-Host "monitor live trace done -- see $out"
