# Final cleanup: cancel the 3 surviving phantoms, recreate broker-sync with new
# C2b code that resolves order_id on the revive path, then verify.
$out = "scripts/dispatch-final-cleanup-output.txt"
"# Final cleanup $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "cancel surviving phantom trades (force re-revive next pass)" {
    docker compose exec -T postgres psql -U chili -d chili -c "UPDATE trading_trades SET status='cancelled', exit_reason=COALESCE(exit_reason,'phantom_no_order_id_force'), exit_date=COALESCE(exit_date, CURRENT_TIMESTAMP), exit_price=COALESCE(exit_price, entry_price), notes=COALESCE(notes,'')||E'\n[force-cancel-phantom-2026-04-30] missing broker_order_id; broker-sync revive path will retry with order_id resolution' WHERE status='open' AND broker_order_id IS NULL RETURNING id, ticker;"
}

S "recreate broker-sync with new C2b code" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait for broker-sync to do a pass" {
    Start-Sleep -Seconds 75
}

S "verify phantom count" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM trading_trades WHERE status='open' AND broker_order_id IS NULL;"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_order_id, broker_status, last_broker_sync FROM trading_trades WHERE id IN (1773, 1784, 1794);"
}

S "broker-sync logs FIX C2b" {
    docker compose logs broker-sync-worker --tail 200 2>&1 | Select-String -Pattern "FIX C2|GGG revived|REFUSING"
}

S "container status" {
    docker ps --format "table {{.Names}}`t{{.Status}}"
}

"" | Add-Content $out
"===== Done =====" | Add-Content $out
Write-Host "done"
