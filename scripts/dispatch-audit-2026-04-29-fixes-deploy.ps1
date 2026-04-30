# Deploy 10 fixes from the 2026-04-29 third-pass audit.
#   6 CRITICAL: PDT gate, phantom guard, avg_return_pct unit, pattern_trades dedupe,
#               heartbeat re-registration, orphan reason age
#   4 HIGH:     daily realized_ev_demote_pass + mig 206, dynamic_priors module,
#               breaker liveness heartbeat, crypto pre-flight
# Usage: .\scripts\dispatch-audit-2026-04-29-fixes-deploy.ps1

$ErrorActionPreference = "Continue"
$start = Get-Date
$out = "scripts/dispatch-audit-2026-04-29-fixes-deploy-output.txt"
"# Deploy 2026-04-29 audit fixes $start" | Out-File $out -Encoding utf8

function Section {
    param([string]$Title, [scriptblock]$Body)
    "" | Add-Content $out
    "===== $Title =====" | Add-Content $out
    try { (& $Body 2>&1) | Out-String | Add-Content $out } catch { "ERROR: $_" | Add-Content $out }
}

# Use a here-string for the commit message to avoid PowerShell parser eating dashes.
$commitMsg = @'
fix(audit-2026-04-29): 10 fixes from third-pass audit

CRITICAL (C1-C6) + HIGH (B-1, E-1, G-1, A-3). Detailed per-fix
descriptions in docs/AUDITS/2026-04-29.md.

C1 PDT-aware entry gate: pdt_guard.py + auto_trader wire-up.
C2 Phantom-trade source plug: broker_service order-id resolution + mig 205.
C3 avg_return_pct unit fix: mig 207 recompute + CHECK constraint.
C4 pattern_trades dedupe: mig 208 DELETE dups + clamp + UNIQUE.
C5 scheduler_worker_heartbeat re-registration for cron_only role.
C6 brain_batch_reconciler: orphan reason now includes job age.
B1 realized_ev_demote_pass daily job + mig 206 retroactive sweep.
E1 dynamic_priors module replaces 8+ hardcoded "or 0.5" sites.
G1 daily breaker_heartbeat liveness snapshot to trading_risk_state.
A3 crypto pre-flight: refuses unsupported Robinhood symbols upstream.
'@

Section "git status changed files" {
    git status -s
}

Section "git add" {
    git add `
      app/migrations.py `
      app/services/broker_service.py `
      app/services/trading/auto_trader.py `
      app/services/trading/pdt_guard.py `
      app/services/trading/dynamic_priors.py `
      app/services/trading/realized_ev_demote_pass.py `
      app/services/trading/ai_context.py `
      app/services/trading/alpha_decay.py `
      app/services/trading/learning_predictions.py `
      app/services/trading/live_drift.py `
      app/services/trading/contracts/signal_emit.py `
      app/services/trading/backtest_queue_worker.py `
      app/services/trading/portfolio_risk.py `
      app/services/trading_scheduler.py `
      app/services/trading/brain_batch_reconciler.py `
      docs/AUDITS/2026-04-29.md `
      scripts/dispatch-audit-2026-04-29-fixes-deploy.ps1
    git status -s
}

Section "git commit" {
    git commit -m $commitMsg
}

Section "rebuild containers" {
    docker compose build chili scheduler-worker brain-worker autotrader-worker broker-sync-worker
}

Section "recreate containers" {
    docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker broker-sync-worker
}

Section "wait 30s for migrations to apply" {
    Start-Sleep -Seconds 30
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id FROM schema_version WHERE version_id LIKE '20%' ORDER BY version_id DESC LIMIT 12;"
}

Section "verify migrations 205-208 applied" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id FROM schema_version WHERE version_id IN ('205_phantom_trade_30min_sweep','206_realized_ev_retroactive_demote','207_avg_return_pct_unit_fix','208_pattern_trades_dedupe_and_clamp') ORDER BY version_id;"
}

Section "verify CHECK constraints + UNIQUE index" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT conname FROM pg_constraint WHERE conname IN ('scan_patterns_avg_return_pct_sane','pattern_trades_ret_sane');"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT indexname FROM pg_indexes WHERE indexname='trading_pattern_trades_natural_key_uniq';"
}

Section "phantom-trade count after deploy" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS phantom_open FROM trading_trades WHERE status='open' AND broker_order_id IS NULL;"
}

Section "scheduler heartbeat post-restart" {
    Start-Sleep -Seconds 10
    docker compose logs scheduler-worker --tail 200 2>&1 | Select-String -Pattern "FIX C5|scheduler_worker_heartbeat|canonical-job"
}

Section "git push" {
    git push origin main
}

$elapsed = ((Get-Date) - $start).TotalSeconds
"" | Add-Content $out
"===== Done in $([Math]::Round($elapsed,1))s =====" | Add-Content $out
Write-Host "done"
