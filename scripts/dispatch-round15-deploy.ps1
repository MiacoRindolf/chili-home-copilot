$out = "scripts/dispatch-round15-deploy-output.txt"
"# round-15 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add app/services/trading/options/exit_monitor.py scripts/_commit_msg_round15.txt scripts/dispatch-round15-deploy.ps1
    git commit -F scripts/_commit_msg_round15.txt
}

S "recreate broker-sync-worker (where options_exit_monitor runs)" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait + verify" {
    Start-Sleep -Seconds 20
    docker ps --format "table {{.Names}}`t{{.Status}}" | Select-String -Pattern "broker-sync"
}

S "options_exit_monitor enabled?" {
    docker compose exec -T scheduler-worker python -c "from app.config import settings; print('options_exit_monitor_enabled=', getattr(settings, 'chili_autotrader_options_exit_monitor_enabled', '<MISSING>'))" 2>&1
}

S "git push" {
    git push origin main
}

Write-Host "done"
