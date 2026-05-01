$out = "scripts/dispatch-end-of-night-pulse-output.txt"
"# end-of-night stability pulse $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "container health" {
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}"
}

S "errors across all workers in last 5min" {
    docker compose logs --since 5m scheduler-worker autotrader-worker brain-worker broker-sync-worker chili 2>&1 | Select-String "ERROR|Traceback|CRITICAL" | Select-Object -Last 25
}

S "broker auth health" {
    docker compose logs --since 30m broker-sync-worker chili 2>&1 | Select-String "refresh_token|invalid_grant|401 Unauthorized" | Select-Object -Last 5
}

S "open trade count (per asset class)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT broker_source, COUNT(*) AS n FROM trade WHERE status='open' GROUP BY broker_source ORDER BY n DESC;"
}

S "exits since R32 deploy (any synthetic reasons re-appeared?)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT exit_reason, COUNT(*) AS n FROM trade WHERE status='closed' AND exit_date > '2026-04-30 21:08:00' GROUP BY exit_reason ORDER BY n DESC;"
}

S "in-process breaker (autotrader)" {
    docker compose exec -T autotrader-worker python -c "from app.services.trading.portfolio_risk import is_breaker_tripped, get_breaker_status; print('tripped:', is_breaker_tripped()); print('status:', get_breaker_status())"
}

S "autotrader candidate_pool latest" {
    docker compose logs --tail=3 autotrader-worker 2>&1 | Select-String "tick uid"
}

S "verify R31/R32/R33 markers all live" {
    docker compose exec -T autotrader-worker python -c "import inspect; from app.services.trading.portfolio_risk import check_drawdown_breaker; r31 = 'SYNTHETIC_EXIT_REASONS' in inspect.getsource(check_drawdown_breaker); from app.services.broker_service import sync_positions_to_db; r32 = 'R32 GUARD' in inspect.getsource(sync_positions_to_db); from app.services.trading.pattern_imminent_alerts import run_pattern_imminent_scan; r33 = 'cooldown_h_crypto' in inspect.getsource(run_pattern_imminent_scan); print(f'R31 (breaker scope filter): {r31}'); print(f'R32 (empty positions guard): {r32}'); print(f'R33 (per-asset cooldown): {r33}')"
}

S "pg connections / idle-in-tx (FIX 46 health)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT state, COUNT(*) FROM pg_stat_activity WHERE datname='chili' GROUP BY state ORDER BY 2 DESC;"
}

S "remove stale .git/index.lock" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add ADR + dispatch artifacts" {
    git add `
        docs/adr/006-asset-class-segregated-lane-breakers.md `
        scripts/dispatch-end-of-night-pulse.ps1
    "git add complete"
}

S "git commit ADR-006" {
    git commit -m "docs(adr-006): draft asset-class-segregated lane breakers (per-lane equity/crypto budgets + global meta-safety floor; deploys post-egress + 1wk evidence window)"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "git push origin main" { git push origin main }

Write-Host "end-of-night pulse done -- see $out"
