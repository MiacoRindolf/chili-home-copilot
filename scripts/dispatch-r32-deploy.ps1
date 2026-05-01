$out = "scripts/dispatch-r32-deploy-output.txt"
"# r32 deploy (file already edited via Edit tool) $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "py-compile broker_service.py" {
    conda run -n chili-env python -m py_compile app/services/broker_service.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git diff stat" {
    git diff --stat app/services/broker_service.py
}

S "git diff (R32 region only)" {
    git diff -U2 app/services/broker_service.py | Select-String -Pattern "R32 GUARD|open_local_count|empty_broker_positions" -Context 1,1 | Select-Object -First 30
}

S "force-recreate broker-sync-worker" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 12s + container health" {
    Start-Sleep -Seconds 12
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}" | Select-String "broker-sync"
}

S "startup error scan" {
    docker compose logs --since 60s broker-sync-worker 2>&1 | Select-String "ERROR|Traceback|ImportError|SyntaxError" | Select-Object -Last 10
}

S "verify R32 GUARD marker loaded in running container" {
    docker compose exec -T broker-sync-worker python -c "import inspect; from app.services.broker_service import sync_positions_to_db; src = inspect.getsource(sync_positions_to_db); print('R32 GUARD marker:', 'R32 GUARD' in src); print('skipped_reason marker:', 'empty_broker_positions_with_open_local_trades' in src); print('open_local_count marker:', 'open_local_count' in src)"
}

S "remove stale .git/index.lock" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/services/broker_service.py `
        scripts/_r32_apply_empty_positions_guard.py `
        scripts/dispatch-r32-apply.ps1 `
        scripts/dispatch-r32-deploy.ps1 `
        scripts/dispatch-fix-packed-refs.ps1 `
        scripts/dispatch-auth-failure-wipeout-trace.ps1 `
        scripts/dispatch-auth-failure-wipeout-trace-output.txt
    "git add complete"
}

S "git commit" {
    git commit -m "fix(r32): empty-positions guard in sync_positions_to_db (refuse mass-close when broker returns 0 positions while local trades remain open -- prevents auth-flap from manufacturing phantom broker_reconcile_position_gone losses; root cause of 2026-04-30 15:56:02 cascade)"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "git push origin main" { git push origin main }

S "watch broker-sync logs for any guard firing in next minute" {
    Start-Sleep -Seconds 30
    docker compose logs --since 90s broker-sync-worker 2>&1 | Select-String "R32 GUARD|broker_sync|sync_positions" | Select-Object -Last 15
}

Write-Host "r32 deploy done -- see $out"
