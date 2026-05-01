$out = "scripts/dispatch-r30-verify-output.txt"
"# r30 verification (post-cleanup) $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "wait 90s for at least one crypto_stop_monitor cycle (every 2min)" {
    Start-Sleep -Seconds 90
    "ok"
}

S "crypto_stop_monitor brain_batch_jobs since R30 deploy" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*), MIN(started_at), MAX(started_at) FROM brain_batch_jobs WHERE job_type='crypto_stop_monitor' AND started_at > NOW() - INTERVAL '5 minutes';"
}

S "stop_engine source confirmation: function call removed (look for the call, not the comment)" {
    docker compose exec -T broker-sync-worker python -c "import inspect; from app.services.trading.stop_engine import dispatch_stop_alerts; src = inspect.getsource(dispatch_stop_alerts); has_call = '_try_auto_execute_stop(' in src and 'REMOVED' not in src.split('_try_auto_execute_stop(')[0].split('\n')[-1]; print('still has live call (bad if True):', has_call); print('has REMOVED comment (good if True):', 'R30 cleanup' in src and 'REMOVED' in src)"
}

S "scheduler source confirmation: renamed function present" {
    docker compose exec -T broker-sync-worker python -c "import inspect; from app.services import trading_scheduler as ts; new_fn = getattr(ts, '_run_stop_alert_dispatch_job', None); old_fn = getattr(ts, '_run_crypto_stop_monitor_job', None); print('new fn present:', new_fn is not None); print('old fn present:', old_fn is not None); print('new fn doc first line:', (new_fn.__doc__ or '').strip().split(chr(10))[0] if new_fn else None)"
}

S "broker-sync-worker logs for stop_alert_dispatch (last 3 min)" {
    docker compose logs --since 3m broker-sync-worker 2>&1 | Select-String -Pattern "stop_alert_dispatch|Crypto stop|crypto_stop_monitor" | Select-Object -Last 10
}

Write-Host "r30 verify done -- see $out"
