$out = "scripts/dispatch-r23-reactivate-simple-output.txt"
"# r23 reactivate (simple, no heredocs) $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "ship view-fn fix: recreate broker-sync-worker" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 12s" { Start-Sleep -Seconds 12; "ok" }

S "broker-sync-worker uptime" {
    docker ps --filter "name=chili-home-copilot-broker-sync-worker-1" --format "{{.Names}} | {{.Status}}"
}

S "ANY startup errors in broker-sync-worker?" {
    docker compose logs --since 30s broker-sync-worker 2>&1 | Select-String "ERROR|Traceback|ImportError|SyntaxError" | Select-Object -Last 20
}

S "set CHILI_BRACKET_SWEEP_WRITER_ENABLED=1 in .env" {
    $envFile = ".env"
    $current = Get-Content $envFile
    $newLines = @()
    foreach ($line in $current) {
        if ($line -match "^CHILI_BRACKET_SWEEP_WRITER_ENABLED=") {
            $newLines += "CHILI_BRACKET_SWEEP_WRITER_ENABLED=1"
        } else { $newLines += $line }
    }
    $newLines | Set-Content $envFile -Encoding utf8
    Get-Content $envFile | Select-String "BRAIN_LIVE_BRACKETS_MODE|CHILI_BRACKET_SWEEP_WRITER_ENABLED" | ForEach-Object { $_.Line }
}

S "recreate broker-sync-worker (pick up flag)" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 80s for at least one bracket sweep" { Start-Sleep -Seconds 80; "ok" }

S "fresh sweeps (last 90s)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT observed_at, mode, kind, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '90 seconds' GROUP BY observed_at, mode, kind ORDER BY observed_at DESC LIMIT 5;"
}

S "g2_ events count last 2 min" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS recent_events, MAX(recorded_at) FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '2 minutes';"
}

S "broker-sync-worker logs since restart (key lines)" {
    docker compose logs --since 3m broker-sync-worker 2>&1 | Select-String -Pattern "ADT|missing_stop|writer_action|agree|SELL_STOP|stop_order_id" | Select-Object -Last 25
}

S "trade 1694 ADT status (should still be open)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status FROM trading_trades WHERE id = 1694;"
}

Write-Host "simple reactivate done -- see $out"
