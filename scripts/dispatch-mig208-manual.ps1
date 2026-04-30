# Stop chili to break the healthcheck restart loop, then apply mig 208 manually
# via psql (no app overhead), then restart chili.
$out = "scripts/dispatch-mig208-manual-output.txt"
"# Manual mig 208 $(Get-Date)" | Out-File $out -Encoding utf8

function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "stop chili" {
    docker compose stop chili
}

S "kill any blocking idle-in-tx" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='chili' AND state LIKE 'idle in transaction%' AND NOW()-state_change > INTERVAL '60 seconds';"
}

S "step 1 dedupe (idempotent, fast if already deduped)" {
    docker compose exec -T postgres psql -U chili -d chili -c "DELETE FROM trading_pattern_trades a USING trading_pattern_trades b WHERE a.id > b.id AND a.scan_pattern_id IS NOT DISTINCT FROM b.scan_pattern_id AND a.ticker = b.ticker AND a.as_of_ts = b.as_of_ts AND a.timeframe IS NOT DISTINCT FROM b.timeframe;"
}

S "step 2 clamp outliers" {
    docker compose exec -T postgres psql -U chili -d chili -c "UPDATE trading_pattern_trades SET outcome_return_pct = NULL WHERE outcome_return_pct IS NOT NULL AND ABS(outcome_return_pct) > 100.0;"
}

S "step 3 CHECK constraint (already applied; verify idempotent)" {
    docker compose exec -T postgres psql -U chili -d chili -c "ALTER TABLE trading_pattern_trades DROP CONSTRAINT IF EXISTS pattern_trades_ret_sane;"
    docker compose exec -T postgres psql -U chili -d chili -c "ALTER TABLE trading_pattern_trades ADD CONSTRAINT pattern_trades_ret_sane CHECK (outcome_return_pct IS NULL OR ABS(outcome_return_pct) <= 100.0);"
}

S "step 4 CREATE UNIQUE INDEX (the long-running one)" {
    docker compose exec -T postgres psql -U chili -d chili -c "CREATE UNIQUE INDEX IF NOT EXISTS trading_pattern_trades_natural_key_uniq ON trading_pattern_trades (scan_pattern_id, ticker, as_of_ts, timeframe) WHERE scan_pattern_id IS NOT NULL;"
}

S "mark mig 208 applied" {
    docker compose exec -T postgres psql -U chili -d chili -c "INSERT INTO schema_version (version_id, applied_at) VALUES ('208_pattern_trades_dedupe_and_clamp', CURRENT_TIMESTAMP) ON CONFLICT (version_id) DO NOTHING;"
}

S "verify" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id FROM schema_version WHERE version_id LIKE '20%' ORDER BY version_id DESC LIMIT 5;"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT indexname FROM pg_indexes WHERE indexname='trading_pattern_trades_natural_key_uniq';"
}

S "restart chili" {
    docker compose up -d chili
}

S "wait + chili health" {
    Start-Sleep -Seconds 20
    docker ps --format "table {{.Names}}`t{{.Status}}" | Select-String -Pattern "chili"
}

"" | Add-Content $out
"===== Done =====" | Add-Content $out
Write-Host "done"
