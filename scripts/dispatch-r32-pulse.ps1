$out = "scripts/dispatch-r32-pulse-output.txt"
"# r32 + breaker pulse $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "broker-sync logs since deploy: any R32 GUARD firings?" {
    docker compose logs --since 60m broker-sync-worker 2>&1 | Select-String "R32 GUARD|empty_broker_positions|broker_reconcile_position_gone" | Select-Object -Last 20
}

S "broker-sync logs since deploy: any sync_positions errors?" {
    docker compose logs --since 60m broker-sync-worker 2>&1 | Select-String "ERROR|Traceback|sync_positions" | Select-Object -Last 15
}

S "broker auth health (refresh_token / 401 / invalid_grant)" {
    docker compose logs --since 60m broker-sync-worker 2>&1 | Select-String "refresh_token|401|invalid_grant|auth.*fail" | Select-Object -Last 10
}

S "trading_risk_state recent rows (R31 self-clear?)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, snapshot_date, breaker_tripped, breaker_reason, regime, created_at FROM trading_risk_state ORDER BY id DESC LIMIT 8;"
}

S "open trades right now (live count -- should be > 0)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS open_count, COUNT(*) FILTER (WHERE broker_source='robinhood') AS rh_count, COUNT(*) FILTER (WHERE broker_source='coinbase') AS cb_count FROM trades WHERE status='open';"
}

S "exits since R32 deploy (broker_reconcile_* should not appear)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, exit_reason, exit_date FROM trades WHERE status='closed' AND exit_date > '2026-04-30 21:08:00' ORDER BY exit_date DESC LIMIT 20;"
}

S "force a fresh breaker recheck (writes new row if state changed)" {
    docker compose exec -T autotrader-worker python -c "from app.db import SessionLocal; from app.services.trading.portfolio_risk import check_drawdown_breaker; db = SessionLocal(); r = check_drawdown_breaker(db, user_id=1, capital=25000.0); print('check result:', r); db.close()"
}

S "post-recheck trading_risk_state" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, snapshot_date, breaker_tripped, breaker_reason, regime, created_at FROM trading_risk_state ORDER BY id DESC LIMIT 3;"
}

S "in-process is_breaker_tripped (autotrader)" {
    docker compose exec -T autotrader-worker python -c "from app.services.trading.portfolio_risk import is_breaker_tripped, get_breaker_status; print('tripped:', is_breaker_tripped()); print('status:', get_breaker_status())"
}

S "autotrader candidate_pool latest tick (do we have candidates again?)" {
    docker compose logs --tail=30 autotrader-worker 2>&1 | Select-String -Pattern "tick uid|candidate_pool" | Select-Object -Last 5
}

Write-Host "r32 pulse done -- see $out"
