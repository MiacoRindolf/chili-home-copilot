$out = "scripts/dispatch-redundancy-impact-2-output.txt"
"# impact analysis (tight scope) $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "1. is chili_auto_execute_stops on?" {
    docker compose exec -T autotrader-worker python -c "from app.config import settings; print('auto_execute_stops:', getattr(settings, 'chili_auto_execute_stops', False))"
}

S "2. trading_alerts last 24h" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT alert_type, COUNT(*) FROM trading_alerts WHERE created_at > NOW() - INTERVAL '24 hours' GROUP BY alert_type ORDER BY count DESC LIMIT 10;"
}

S "3. crypto_stop_monitor brain_batch_jobs runs (last 24h)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM brain_batch_jobs WHERE job_type='crypto_stop_monitor' AND started_at > NOW() - INTERVAL '24 hours';"
}

S "4. all callers of place_crypto_sell_order" {
    docker compose exec -T chili sh -c "grep -rn 'place_crypto_sell_order' /app/app 2>/dev/null | grep -v __pycache__ | head -10"
}

S "5. neural mesh sensor table presence" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT to_regclass('public.neural_mesh_sensor_events') AS t;"
}

S "6. broker auth state right now (post-reauth)" {
    docker compose logs --tail=20 autotrader-worker 2>&1 | Select-String -Pattern "refresh_token|invalid_grant|broker session|connected" | Select-Object -Last 10
}

S "7. recent autotrader tick output" {
    docker compose logs --tail=10 autotrader-worker 2>&1 | Select-String -Pattern "tick uid|candidate_pool|placed=" | Select-Object -Last 5
}

Write-Host "tight redundancy probe done -- see $out"
