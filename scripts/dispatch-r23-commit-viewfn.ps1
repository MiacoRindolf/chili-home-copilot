$out = "scripts/dispatch-r23-commit-viewfn-output.txt"
"# r23 commit view-fn fix $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale .git/index.lock if present" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git status -s (filtered)" {
    git status -s | Where-Object { $_ -match 'bracket_reconciliation_service|dispatch-r23-(deactivate|reactivate|preflight|commit-viewfn)' }
}

S "py-compile (final guardrail)" {
    conda run -n chili-env python -m py_compile app/services/trading/bracket_reconciliation_service.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git add" {
    git add `
        app/services/trading/bracket_reconciliation_service.py `
        scripts/dispatch-r23-deactivate-and-diag.ps1 `
        scripts/dispatch-r23-deactivate-and-diag-output.txt `
        scripts/dispatch-r23-reactivate-with-viewfn-fix.ps1 `
        scripts/dispatch-r23-reactivate-simple.ps1 `
        scripts/dispatch-r23-reactivate-simple-output.txt `
        scripts/dispatch-r23-commit-viewfn.ps1 `
        .env
    "git add complete"
}

S "git status (post-add)" { git status -s }

S "git commit" {
    git commit -m "fix(r23): broker_manager_view_fn surfaces resting SELL stop orders so classifier sees them"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -3" { git log --oneline -3 }

S "live: latest sweep summary lines" {
    docker compose logs --since 2m broker-sync-worker 2>&1 | Select-String "sweep_summary|writer_action" | Select-Object -Last 5
}

S "live: open RH trade + its stop" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status FROM trading_trades WHERE id = 1694;"
    docker compose exec -T chili python -c "from app.services import broker_service; o = broker_service.get_order_by_id('69f3947a-61cf-4e11-99c4-1f45879749e0'); print('stop state=', (o or {}).get('state'), 'stop_price=', (o or {}).get('stop_price'), 'qty=', (o or {}).get('quantity'))"
}

S "git push origin main" { git push origin main }

Write-Host "commit + push done -- see $out"
