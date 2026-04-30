# Stop ALL writers, finish mig 208 dedupe + unique index, then restart everything.
$out = "scripts/dispatch-pause-and-index-output.txt"
"# Pause writers + finish mig 208 $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "stop all writer containers" {
    docker compose stop chili scheduler-worker brain-worker autotrader-worker broker-sync-worker
}

S "kill any remaining idle-in-tx" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT pg_terminate_backend(pid), application_name FROM pg_stat_activity WHERE datname='chili' AND application_name LIKE 'chili%';"
}

S "verify quiet" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT pid, application_name, state FROM pg_stat_activity WHERE datname='chili' AND application_name LIKE 'chili%';"
}

S "final dedupe pass" {
    docker compose exec -T postgres psql -U chili -d chili -c "DELETE FROM trading_pattern_trades a USING trading_pattern_trades b WHERE a.id > b.id AND a.scan_pattern_id IS NOT DISTINCT FROM b.scan_pattern_id AND a.ticker = b.ticker AND a.as_of_ts = b.as_of_ts AND a.timeframe IS NOT DISTINCT FROM b.timeframe;"
}

S "create unique index" {
    docker compose exec -T postgres psql -U chili -d chili -c "CREATE UNIQUE INDEX IF NOT EXISTS trading_pattern_trades_natural_key_uniq ON trading_pattern_trades (scan_pattern_id, ticker, as_of_ts, timeframe) WHERE scan_pattern_id IS NOT NULL;"
}

S "verify" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT indexname FROM pg_indexes WHERE indexname='trading_pattern_trades_natural_key_uniq';"
}

S "restart all containers" {
    docker compose up -d chili scheduler-worker brain-worker autotrader-worker broker-sync-worker
}

S "container status" {
    Start-Sleep -Seconds 15
    docker ps --format "table {{.Names}}`t{{.Status}}"
}

"" | Add-Content $out
"===== Done =====" | Add-Content $out
Write-Host "done"
