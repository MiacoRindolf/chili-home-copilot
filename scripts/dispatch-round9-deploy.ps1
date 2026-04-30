$out = "scripts/dispatch-round9-deploy-output.txt"
"# round-9 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add app/config.py app/services/trading_scheduler.py scripts/_commit_msg_round9.txt scripts/dispatch-round9-deploy.ps1 scripts/dispatch-cron-check.ps1 scripts/dispatch-pnl-unknown-deploy.ps1 scripts/_commit_msg_pnl_unknown.txt
    git commit -F scripts/_commit_msg_round9.txt
}

S "recreate scheduler-worker (the cron lives there)" {
    docker compose up -d --force-recreate scheduler-worker
}

S "wait + verify settings now visible" {
    Start-Sleep -Seconds 20
    docker compose exec -T scheduler-worker python -c "from app.config import settings; print('demote_pass_enabled=', settings.chili_realized_ev_demote_pass_enabled); print('demote_settle_days=', settings.chili_realized_ev_demote_settle_days)" 2>&1
}

S "git push" {
    git push origin main
}

Write-Host "done"
