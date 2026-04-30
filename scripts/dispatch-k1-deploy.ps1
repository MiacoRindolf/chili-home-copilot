$out = "scripts/dispatch-k1-deploy-output.txt"
"# K-1 commit + final pulse $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add" {
    git add `
      scripts/_claude_daemon.ps1 `
      scripts/_commit_msg_k1.txt `
      scripts/dispatch-k1-deploy.ps1 `
      scripts/dispatch-a6-deploy.ps1 `
      scripts/dispatch-pause-and-index.ps1 `
      scripts/dispatch-mig208-manual.ps1 `
      scripts/dispatch-final-cleanup.ps1 `
      scripts/dispatch-regime-mode-probe.ps1 `
      scripts/dispatch-trigger-ledger.ps1 `
      scripts/dispatch-macro-regime-trigger.ps1 `
      scripts/dispatch-chili-mig-error.ps1 `
      scripts/dispatch-verify-2026-04-29.ps1 `
      scripts/_commit_msg_audit_fixes.txt `
      scripts/_commit_msg_f1.txt `
      scripts/_commit_msg_f3.txt `
      scripts/_commit_msg_a5_b2.txt `
      scripts/_commit_msg_a6_real.txt
    git status -s | Select-Object -First 10
}

S "git commit" {
    git commit -F scripts/_commit_msg_k1.txt
}

S "final pulse: containers" {
    docker ps --format "table {{.Names}}`t{{.Status}}"
}

S "final pulse: phantom + heartbeat + dup-coid" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT 'phantom_open_trades' AS metric, COUNT(*)::text AS val FROM trading_trades WHERE status='open' AND broker_order_id IS NULL UNION ALL SELECT 'heartbeat_last_5min', COUNT(*)::text FROM brain_batch_jobs WHERE job_type='scheduler_worker_heartbeat' AND started_at > NOW() - INTERVAL '6 minutes' UNION ALL SELECT 'dup_coid_recoveries_15min', COUNT(*)::text FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '15 minutes' AND reason LIKE '%dup_coid%' UNION ALL SELECT 'pdt_blocks_1hr', COUNT(*)::text FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '1 hour' AND reason LIKE 'pdt_guard%' UNION ALL SELECT 'wide_spread_defers_1hr', COUNT(*)::text FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '1 hour' AND reason='wide_spread' UNION ALL SELECT 'live_regime_ledger_rows', COUNT(*)::text FROM trading_pattern_regime_performance_daily WHERE mode='live';"
}

S "final pulse: migrations applied" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id FROM schema_version WHERE version_id LIKE '2_%' OR version_id LIKE '20%' OR version_id LIKE '21%' ORDER BY version_id DESC LIMIT 8;"
}

S "git push" {
    git push origin main
}

S "git log" {
    git log --oneline -7
}

Write-Host "done"
