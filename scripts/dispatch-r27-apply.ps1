$out = "scripts/dispatch-r27-apply-output.txt"
"# r27 apply terminal-state guard $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "apply edit via Python (host git works)" {
    conda run -n chili-env python scripts/_r27_apply_terminal_guard.py
}

S "py-compile" {
    conda run -n chili-env python -m py_compile app/services/trading/execution_audit.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git diff stat" {
    git diff --stat app/services/trading/execution_audit.py
}

S "force-recreate workers that handle order events" {
    docker compose up -d --force-recreate broker-sync-worker autotrader-worker chili
}

S "wait 15s" { Start-Sleep -Seconds 15; "ok" }

S "container health" {
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}"
}

S "audit row" {
    docker compose exec -T postgres psql -U chili -d chili -c "INSERT INTO trading_learning_events (user_id, event_type, description, created_at) VALUES (NULL, 'r27_apply_execution_event_terminal_guard', 'apply_execution_event_to_trade now respects terminal trade states (closed/cancelled with exit_date) -- no longer flips trade.status from closed back to open on SELL fills. Closes the bug that caused ADT/WDCX/ABEV to look open after exit.', CURRENT_TIMESTAMP) RETURNING id;"
}

S "verify open RH trades is still 0 (no spurious resurrection)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status FROM trading_trades WHERE status='open' AND broker_source='robinhood';"
}

S "verify recent g2_ events are quiet (no more writer loop)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*), MAX(recorded_at) FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '5 minutes';"
}

Write-Host "r27 apply done -- see $out"
