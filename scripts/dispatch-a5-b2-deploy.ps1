$out = "scripts/dispatch-a5-b2-deploy-output.txt"
"# A-5 + B-2 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add" {
    git add `
      app/migrations.py `
      app/services/trading/robinhood_exit_execution.py `
      scripts/_commit_msg_a5_b2.txt `
      scripts/dispatch-a5-b2-deploy.ps1 `
      scripts/dispatch-macro-regime-trigger.ps1 `
      scripts/dispatch-trigger-ledger.ps1
    git status -s | Select-Object -First 15
}

S "git commit" {
    git commit -F scripts/_commit_msg_a5_b2.txt
}

S "recreate chili (mig 210) + autotrader-worker + broker-sync-worker" {
    docker compose up -d --force-recreate chili autotrader-worker broker-sync-worker
}

S "wait + verify mig 210" {
    Start-Sleep -Seconds 25
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id, applied_at FROM schema_version WHERE version_id LIKE '210%';"
}

S "B-2 verify avg_return_pct null counts" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FILTER (WHERE avg_return_pct IS NULL) AS arp_null_after, COUNT(*) AS total FROM scan_patterns;"
}

S "A-5 deploy verify (look for FIX A-5 logs after restart)" {
    Start-Sleep -Seconds 15
    docker compose logs autotrader-worker --tail 200 2>&1 | Select-String -Pattern "FIX A-5|broker_qty"
}

S "git push" {
    git push origin main
}

Write-Host "done"
