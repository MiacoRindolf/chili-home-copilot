$out = "scripts/dispatch-r23-ping-output.txt"
"# r23 ping (post-crash daemon check) $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "host date" { Get-Date }

S "git log --oneline -3" { git log --oneline -3 }

S "docker ps (chili containers running?)" {
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}"
}

S "broker-sync-worker last 30 log lines" {
    docker compose logs --tail=30 broker-sync-worker 2>&1
}

S "current .env brackets flags" {
    Get-Content .env | Select-String "BRAIN_LIVE_BRACKETS_MODE|CHILI_BRACKET_SWEEP_WRITER_ENABLED|chili_bracket" | ForEach-Object { $_.Line }
}

S "any reconciliation sweep in last 10 min?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*), MAX(observed_at) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '10 minutes';"
}

S "current open Robinhood trades" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, broker_order_id IS NOT NULL AS has_oid, ROUND(entry_price::numeric,4) AS entry, ROUND(stop_loss::numeric,4) AS stop FROM trading_trades WHERE status = 'open' AND broker_source = 'robinhood' ORDER BY entry_date DESC;"
}

Write-Host "ping complete -- see $out"
