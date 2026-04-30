$out = "scripts/dispatch-pnl-unknown-deploy-output.txt"
"# pnl-unknown deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add app/services/broker_service.py scripts/_commit_msg_pnl_unknown.txt scripts/dispatch-pnl-unknown-deploy.ps1 scripts/dispatch-mig211-deploy.ps1 scripts/_commit_msg_mig211.txt scripts/_commit_msg_k1.txt scripts/dispatch-k1-deploy.ps1
    git commit -F scripts/_commit_msg_pnl_unknown.txt
}

S "before count of fake-flat closes (entry == exit AND pnl=0)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS fake_flat_count FROM trading_trades WHERE status='closed' AND exit_reason='broker_reconcile_position_gone' AND entry_price = exit_price AND COALESCE(pnl, 0) = 0;"
}

S "recreate broker-sync-worker (the writer for this code path)" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait + verify container" {
    Start-Sleep -Seconds 15
    docker ps --format "table {{.Names}}`t{{.Status}}" | Select-String broker-sync
}

S "git push" {
    git push origin main
}

Write-Host "done"
