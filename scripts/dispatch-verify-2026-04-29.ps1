# Verify whether the 2026-04-29 audit fixes actually deployed.
$out = "scripts/dispatch-verify-2026-04-29-output.txt"
"# Verify 2026-04-29 deploy $(Get-Date)" | Out-File $out -Encoding utf8

function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "container uptimes" {
    docker ps --format "table {{.Names}}`t{{.Status}}"
}

S "migrations 205-208 applied?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id, applied_at FROM schema_version WHERE version_id IN ('205_phantom_trade_30min_sweep','206_realized_ev_retroactive_demote','207_avg_return_pct_unit_fix','208_pattern_trades_dedupe_and_clamp') ORDER BY version_id;"
}

S "CHECK constraints + UNIQUE index" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT conname FROM pg_constraint WHERE conname IN ('scan_patterns_avg_return_pct_sane','pattern_trades_ret_sane');"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT indexname FROM pg_indexes WHERE indexname='trading_pattern_trades_natural_key_uniq';"
}

S "phantom open trades" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_order_id, exit_reason FROM trading_trades WHERE status='open' AND broker_order_id IS NULL;"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM trading_trades WHERE status='open';"
}

S "scheduler heartbeat after restart" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT job_type, status, started_at FROM brain_batch_jobs WHERE job_type='scheduler_worker_heartbeat' ORDER BY started_at DESC LIMIT 5;"
}

S "scheduler logs FIX C5 banner" {
    docker compose logs scheduler-worker --tail 200 2>&1 | Select-String -Pattern "FIX C5|canonical-job"
}

S "git status after deploy attempt" {
    git log -1 --oneline
    ""
    git status -s app/migrations.py app/services/trading/pdt_guard.py app/services/trading/dynamic_priors.py app/services/trading/realized_ev_demote_pass.py
}

"" | Add-Content $out
"===== Done $(Get-Date) =====" | Add-Content $out
Write-Host "done"
