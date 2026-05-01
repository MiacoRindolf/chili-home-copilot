$out = "scripts/dispatch-r30-cleanup-output.txt"
"# r30 cleanup (rename misleading job + remove dead auto-execute) $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "apply edits (host git)" {
    conda run -n chili-env python scripts/_r30_cleanup_apply.py
}

S "py-compile both files" {
    conda run -n chili-env python -m py_compile app/services/trading/stop_engine.py app/services/trading_scheduler.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git diff stat" {
    git diff --stat app/services/trading/stop_engine.py app/services/trading_scheduler.py
}

S "force-recreate broker-sync-worker (where crypto_stop_monitor job runs)" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 12s + container health" {
    Start-Sleep -Seconds 12
    docker ps --filter "name=chili-home-copilot-broker-sync-worker-1" --format "{{.Names}} | {{.Status}}"
}

S "broker-sync-worker startup errors?" {
    docker compose logs --since 30s broker-sync-worker 2>&1 | Select-String "ERROR|Traceback|ImportError|SyntaxError" | Select-Object -Last 10
}

S "verify the renamed job is registered (job_id should still be crypto_stop_monitor)" {
    docker compose exec -T broker-sync-worker python -c "from app.services import trading_scheduler as ts; sched = getattr(ts, '_scheduler', None); jobs = sched.get_jobs() if sched else []; csm = [j for j in jobs if j.id == 'crypto_stop_monitor']; print('crypto_stop_monitor jobs found:', len(csm)); [print(f'  func={j.func.__name__} name={j.name!r}') for j in csm]"
}

S "verify the function rename took effect" {
    docker compose exec -T broker-sync-worker python -c "import inspect; from app.services import trading_scheduler as ts; print('_run_stop_alert_dispatch_job:', hasattr(ts, '_run_stop_alert_dispatch_job')); print('_run_crypto_stop_monitor_job (old):', hasattr(ts, '_run_crypto_stop_monitor_job'))"
}

S "verify _try_auto_execute_stop call removed from dispatch_stop_alerts" {
    docker compose exec -T broker-sync-worker python -c "import inspect; from app.services.trading.stop_engine import dispatch_stop_alerts; src = inspect.getsource(dispatch_stop_alerts); print('R30 cleanup applied:', '_try_auto_execute_stop' not in src)"
}

S "remove stale .git/index.lock" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/services/trading/stop_engine.py `
        app/services/trading_scheduler.py `
        scripts/_r30_cleanup_apply.py `
        scripts/dispatch-r30-cleanup.ps1 `
        scripts/dispatch-cadence-investigation.ps1 `
        scripts/dispatch-cadence-investigation-output.txt `
        scripts/dispatch-crypto-exit-pass-trace.ps1 `
        scripts/dispatch-crypto-exit-pass-trace-output.txt `
        scripts/dispatch-autotrader-monitor-live.ps1 `
        scripts/dispatch-autotrader-monitor-live-output.txt `
        scripts/dispatch-redundancy-impact.ps1 `
        scripts/dispatch-redundancy-impact-2.ps1 `
        scripts/dispatch-redundancy-impact-2-output.txt
    "git add complete"
}

S "git commit" {
    git commit -m "fix(r30): rename _run_crypto_stop_monitor_job -> _run_stop_alert_dispatch_job; remove dead _try_auto_execute_stop call (would have raced run_crypto_exit_pass if flag flipped)"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -3" { git log --oneline -3 }

S "git push origin main" { git push origin main }

Write-Host "r30 cleanup done -- see $out"
