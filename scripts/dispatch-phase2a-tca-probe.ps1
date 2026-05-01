$out = "scripts/dispatch-phase2a-tca-probe-output.txt"
"# Phase 2a TCA prerequisite probe $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "TCA column coverage on closed trades last 30 days" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT broker_source, COUNT(*) AS total_closed, COUNT(*) FILTER (WHERE tca_entry_slippage_bps IS NOT NULL) AS has_entry_slip, COUNT(*) FILTER (WHERE tca_exit_slippage_bps IS NOT NULL) AS has_exit_slip, COUNT(*) FILTER (WHERE tca_entry_slippage_bps IS NOT NULL AND tca_exit_slippage_bps IS NOT NULL) AS both FROM trading_trades WHERE status='closed' AND entry_date > NOW() - INTERVAL '30 days' GROUP BY broker_source ORDER BY total_closed DESC;"
}

S "TCA-populated examples (most recent 10)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, broker_source, ROUND(tca_entry_slippage_bps::numeric,2) AS entry_slip, ROUND(tca_exit_slippage_bps::numeric,2) AS exit_slip, ROUND(entry_price::numeric,4) AS entry, ROUND(exit_price::numeric,4) AS exit, exit_date FROM trading_trades WHERE status='closed' AND (tca_entry_slippage_bps IS NOT NULL OR tca_exit_slippage_bps IS NOT NULL) ORDER BY exit_date DESC LIMIT 10;"
}

S "is the TCA writer actually firing? Check apply_tca_on_trade_close call sites" {
    docker compose exec -T chili sh -c 'grep -rn "apply_tca_on_trade_close\|tca_service" /app/app/services/ 2>/dev/null | grep -v __pycache__ | head -10'
}

S "candidate ticker universe for rebuild_all (closed trades last 60 days)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(DISTINCT ticker) AS uniq_tickers, COUNT(*) AS total_rows FROM trading_trades WHERE status='closed' AND entry_date > NOW() - INTERVAL '60 days';"
}

S "venue_truth.record_fill_observation call sites (any?)" {
    docker compose exec -T chili sh -c 'grep -rn "record_fill_observation\|venue_truth.record" /app/app/ 2>/dev/null | grep -v __pycache__ | head -10'
}

S "execution_cost_builder.rebuild_all call sites (any?)" {
    docker compose exec -T chili sh -c 'grep -rn "rebuild_all\|execution_cost_builder.rebuild" /app/app/ 2>/dev/null | grep -v __pycache__ | head -10'
}

S "scheduler-worker role + sample of registered jobs (where would I add the new job?)" {
    docker compose exec -T chili sh -c 'grep -n "_run_execution_cost\|_run_venue_truth\|_run_brain_batch_reconciler\|_run_promotion_evidence" /app/app/services/trading_scheduler.py 2>/dev/null | head -10'
}

Write-Host "TCA probe done -- see $out"
