$out = "scripts/dispatch-flag2-pulse-output.txt"
"# Flag 2 perps_lane current pulse $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "perp_quotes most recent rows by venue (is the job still firing?)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT venue, MAX(observed_at) AS most_recent, COUNT(*) FILTER (WHERE observed_at > NOW() - INTERVAL '24 hours') AS rows_24h, COUNT(*) FILTER (WHERE observed_at > NOW() - INTERVAL '1 hour') AS rows_1h FROM perp_quotes pq JOIN perp_contracts pc ON pq.contract_id = pc.id GROUP BY venue ORDER BY most_recent DESC;"
}

S "perp_funding most recent" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT MAX(observed_at) AS most_recent, COUNT(*) FILTER (WHERE observed_at > NOW() - INTERVAL '24 hours') AS rows_24h FROM perp_funding;"
}

S "current chili_perps_lane_enabled flag value" {
    docker compose exec -T chili python -c "from app.config import settings; print('perps_lane_enabled:', getattr(settings, 'chili_perps_lane_enabled', 'attr_missing'))"
}

S "search trading_scheduler.py for any 'perp' job (might use a different name than I expected)" {
    docker compose exec -T chili sh -c 'grep -n -i "perp" /app/app/services/trading_scheduler.py 2>/dev/null | head -20'
}

S "find the perps ingest module" {
    docker compose exec -T chili sh -c 'find /app/app -name "*perp*" 2>/dev/null | grep -v __pycache__ | head -10'
}

Write-Host "flag 2 pulse done -- see $out"
