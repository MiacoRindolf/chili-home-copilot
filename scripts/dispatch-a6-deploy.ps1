$out = "scripts/dispatch-a6-deploy-output.txt"
"# A-6 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add" {
    git add `
      app/services/trading/venue/idempotency_store.py `
      app/services/trading/robinhood_exit_execution.py `
      scripts/_commit_msg_a6_real.txt `
      scripts/dispatch-a6-deploy.ps1
    git status -s | Select-Object -First 10
}

S "git commit" {
    git commit -F scripts/_commit_msg_a6_real.txt
}

S "recreate autotrader-worker (the loop runs there)" {
    docker compose up -d --force-recreate autotrader-worker
}

S "wait + verify dup-coid loop broken" {
    Start-Sleep -Seconds 60
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT decision, reason, COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '5 minutes' AND (decision LIKE '%recovered%' OR reason LIKE '%dup_coid%') GROUP BY 1,2 ORDER BY 3 DESC;"
}

S "git push" {
    git push origin main
}

Write-Host "done"
