$out = "scripts/dispatch-r26-commit-output.txt"
"# r26 commit exit-defer-on-reject $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale .git/index.lock if present" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/services/trading/robinhood_exit_execution.py `
        scripts/_r26_apply_exit_defer.py `
        scripts/dispatch-r26-apply.ps1 `
        scripts/dispatch-r26-apply-output.txt `
        scripts/dispatch-r26-commit.ps1
    "git add complete"
}

S "git commit" {
    git commit -m "fix(r26): defer exit on retryable broker rejection (PDT/wide_spread/etc) to engage 5min cooldown"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "live: exit decisions last 5 min" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT decision, COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '5 minutes' AND decision LIKE 'monitor_exit%' GROUP BY decision ORDER BY count DESC;"
}

S "live: open Robinhood trades" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, pending_exit_status, pending_exit_reason FROM trading_trades WHERE status='open' AND broker_source='robinhood' ORDER BY id;"
}

S "git push origin main" { git push origin main }

Write-Host "r26 commit done -- see $out"
