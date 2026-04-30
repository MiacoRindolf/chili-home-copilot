$out = "scripts/dispatch-r23-reactivate-with-viewfn-fix-output.txt"
"# r23 reactivate with broker-view-fn fix $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "py-compile" {
    conda run -n chili-env python -m py_compile app/services/trading/bracket_reconciliation_service.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

# Pre-fix: confirm view-fn returns stop_order_id=None today
S "BEFORE-fix: broker_manager_view_fn output for ADT (current container, before flag flip)" {
    docker compose exec -T broker-sync-worker python -c @"
from app.services.trading.bracket_reconciliation_service import broker_manager_view_fn
views = broker_manager_view_fn([{'ticker': 'ADT', 'broker_source': 'robinhood'}])
for v in views:
    print(f'available={v.available} stop_order_id={v.stop_order_id} stop_state={v.stop_order_state} stop_price={v.stop_order_price}')
"@
}

# Restart broker-sync-worker so the new view-fn ships
S "force-recreate broker-sync-worker (ship the fix)" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 8s for container start" { Start-Sleep -Seconds 8; "ok" }

S "AFTER-fix: broker_manager_view_fn output for ADT (should now show stop_order_id=69f3947a)" {
    docker compose exec -T broker-sync-worker python -c @"
from app.services.trading.bracket_reconciliation_service import broker_manager_view_fn
views = broker_manager_view_fn([{'ticker': 'ADT', 'broker_source': 'robinhood'}])
for v in views:
    print(f'available={v.available} stop_order_id={v.stop_order_id} stop_state={v.stop_order_state} stop_price={v.stop_order_price}')
"@
}

# Now reactivate the writer
S "set CHILI_BRACKET_SWEEP_WRITER_ENABLED=1 in .env" {
    $envFile = ".env"
    $current = Get-Content $envFile
    $newLines = @()
    foreach ($line in $current) {
        if ($line -match "^CHILI_BRACKET_SWEEP_WRITER_ENABLED=") {
            $newLines += "CHILI_BRACKET_SWEEP_WRITER_ENABLED=1"
        } else {
            $newLines += $line
        }
    }
    $newLines | Set-Content $envFile -Encoding utf8
    Get-Content $envFile | Select-String "BRAIN_LIVE_BRACKETS_MODE|CHILI_BRACKET_SWEEP_WRITER_ENABLED" | ForEach-Object { $_.Line }
}

S "force-recreate broker-sync-worker (pick up new flag)" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 75s for at least one bracket sweep" { Start-Sleep -Seconds 75; "ok" }

S "verify mode is authoritative + sweep classifies as agree (not missing_stop)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT observed_at, mode, kind, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '90 seconds' GROUP BY observed_at, mode, kind ORDER BY observed_at DESC LIMIT 5;"
}

S "g2_ events count since reactivation" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS events_last_90s, MAX(recorded_at) AS most_recent FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '90 seconds';"
}

S "wait another 65s, recheck — confirm steady-state with NO duplicate writer fires" {
    Start-Sleep -Seconds 65
    "ok"
}

S "g2_ events count after second sweep cycle" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS events_last_3m FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '3 minutes';"
}

S "broker-sync-worker logs since restart" {
    docker compose logs --since 4m broker-sync-worker 2>&1 | Select-String -Pattern "ADT|missing_stop|writer_action|agree|SELL_STOP" | Select-Object -Last 40
}

S "trade 1694 still open + ADT stop still resting" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status FROM trading_trades WHERE id = 1694;"
    docker compose exec -T chili python -c "from app.services import broker_service; o = broker_service.get_order_by_id('69f3947a-61cf-4e11-99c4-1f45879749e0'); print(o)"
}

Write-Host "reactivate done -- see $out"
