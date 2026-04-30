$out = "scripts/dispatch-r23-commit-followup-output.txt"
"# r23 commit follow-up fixes $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale .git/index.lock if present" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git status -s (R23 follow-up files)" { git status -s }

S "git add follow-up files" {
    git add `
        app/services/broker_service.py `
        app/services/trading/bracket_writer_g2.py `
        app/services/trading_scheduler.py `
        scripts/dispatch-r23-ping.ps1 `
        scripts/dispatch-r23-ping-output.txt `
        scripts/dispatch-r23-preflight.ps1 `
        scripts/dispatch-r23-preflight-output.txt `
        scripts/dispatch-r23-activate.ps1 `
        scripts/dispatch-r23-activate-output.txt `
        scripts/dispatch-r23-activate-followup.ps1 `
        scripts/dispatch-r23-activate-followup-output.txt `
        scripts/dispatch-r23-sweep-diag.ps1 `
        scripts/dispatch-r23-sweep-diag-output.txt `
        scripts/dispatch-r23-scheduler-fix.ps1 `
        scripts/dispatch-r23-scheduler-fix-output.txt `
        scripts/dispatch-r23-fix-trigger-and-investigate.ps1 `
        scripts/dispatch-r23-fix-trigger-and-investigate-output.txt `
        scripts/dispatch-r23-recover-and-retry.ps1 `
        scripts/dispatch-r23-recover-and-retry-output.txt `
        scripts/dispatch-r23-commit-followup.ps1 `
        .env
    "git add complete"
}

S "git status post-add" { git status -s }

S "git commit (single follow-up commit)" {
    git commit -m "fix(r23): rh.orders.order kwargs + trade=None in writer audit + scheduler authoritative gate"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "live: writer audit rows since fix" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, status, ticker, recorded_at FROM trading_execution_events WHERE event_type LIKE 'g2_%' ORDER BY id DESC LIMIT 10;"
}

S "live: open Robinhood trades + their stops" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status FROM trading_trades WHERE status='open' AND broker_source='robinhood' ORDER BY id;"
}

S "live: latest sweeps" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT observed_at, mode, kind, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '5 minutes' GROUP BY observed_at, mode, kind ORDER BY observed_at DESC LIMIT 10;"
}

S "git push origin main retry" { git push origin main }

Write-Host "commit followup done -- see $out"
