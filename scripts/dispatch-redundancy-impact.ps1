$out = "scripts/dispatch-redundancy-impact-output.txt"
"# impact analysis: redundant crypto exit paths $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "1. is chili_auto_execute_stops enabled? (the gate for race-prone auto-execute path)" {
    docker compose exec -T autotrader-worker python -c "from app.config import settings; print('auto_execute_stops:', getattr(settings, 'chili_auto_execute_stops', False)); print('crypto_exit_monitor_enabled:', settings.chili_autotrader_crypto_exit_monitor_enabled); print('autotrader_enabled:', settings.chili_autotrader_enabled)"
}

S "2. dispatch_stop_alerts last 24h: how many alerts went out by event?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT alert_type, COUNT(*) FROM trading_alerts WHERE created_at > NOW() - INTERVAL '24 hours' AND alert_type IN ('stop_hit','target_hit','stop_approaching') GROUP BY alert_type ORDER BY count DESC;" 2>&1
}

S "3. neural mesh nm_stop_eval activity (consumer of dispatch_stop_alerts)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS rows, MAX(created_at) AS most_recent FROM neural_mesh_sensor_events WHERE sensor_id = 'nm_stop_eval' AND created_at > NOW() - INTERVAL '24 hours';" 2>&1
}

S "4. WHERE else does run_crypto_exit_pass call submit_robinhood_trade_exit / place_crypto_sell_order?" {
    docker compose exec -T chili sh -c 'grep -rn "place_crypto_sell_order\|run_crypto_exit_pass\|tick_auto_trader_monitor" /app/app/services/ 2>/dev/null | grep -v __pycache__ | head -25'
}

S "5. ALL callers of place_crypto_sell_order (the execution primitive)" {
    docker compose exec -T chili sh -c 'grep -rn "place_crypto_sell_order\|broker_service.place_crypto_sell" /app/app/ 2>/dev/null | grep -v __pycache__ | head -15'
}

S "6. _run_crypto_stop_monitor_job activity in last 24h (writing brain_batch_jobs?)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS runs, MIN(started_at) AS first, MAX(started_at) AS last FROM brain_batch_jobs WHERE job_type='crypto_stop_monitor' AND started_at > NOW() - INTERVAL '24 hours';"
}

S "7. summary of overlap: do the two paths produce overlapping decisions on same trade?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT 'this is a code-review item not a query' AS note;"
}

S "8. log lines showing both paths firing on same trade window" {
    docker compose logs --since 24h autotrader-worker broker-sync-worker 2>&1 | Select-String -Pattern "stop_hit|target_hit|crypto_exit_pass.*closed|AUTO-EXECUTING" | Select-Object -Last 30
}

Write-Host "redundancy impact done -- see $out"
