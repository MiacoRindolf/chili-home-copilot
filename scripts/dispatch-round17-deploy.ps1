$out = "scripts/dispatch-round17-deploy-output.txt"
"# round-17 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add app/services/trading/pattern_trade_storage.py scripts/_commit_msg_round17.txt scripts/dispatch-round17-deploy.ps1
    git commit -F scripts/_commit_msg_round17.txt
}

S "before: pattern_trades constraint-violation count last 60min in brain-worker logs" {
    docker compose logs --since 60m brain-worker 2>$null | Select-String -Pattern "trading_pattern_trades_natural_key_uniq" -SimpleMatch | Measure-Object | Select-Object -ExpandProperty Count
}

S "before: pattern_trades total row count" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM trading_pattern_trades;"
}

S "recreate brain-worker (writer is in-process)" {
    docker compose up -d --force-recreate brain-worker
}

S "wait 90s for backtest queue to fire" {
    Start-Sleep -Seconds 90
    "ok"
}

S "after: pattern_trades constraint-violation count post-deploy (last 5min)" {
    docker compose logs --since 5m brain-worker 2>$null | Select-String -Pattern "trading_pattern_trades_natural_key_uniq" -SimpleMatch | Measure-Object | Select-Object -ExpandProperty Count
}

S "after: pattern_trades total row count" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM trading_pattern_trades;"
}

S "after: skipped_dup log lines (debug level so might be 0 unless debug enabled)" {
    docker compose logs --since 5m brain-worker 2>$null | Select-String -Pattern "skipped_dup" -SimpleMatch | Measure-Object | Select-Object -ExpandProperty Count
}

S "after: pattern_trade_storage commit failures (should be 0 going forward)" {
    docker compose logs --since 5m brain-worker 2>$null | Select-String -Pattern "pattern_trade_storage. (commit failed|insert failed)" | Measure-Object | Select-Object -ExpandProperty Count
}

S "git push" {
    git push origin main
}

Write-Host "done"
