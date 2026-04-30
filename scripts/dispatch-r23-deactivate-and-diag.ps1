$out = "scripts/dispatch-r23-deactivate-and-diag-output.txt"
"# r23 temporary deactivate + diagnose duplicate-submission loop $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- Step 1: Stop the loop by flipping the SWEEP flag back ----------

S "set CHILI_BRACKET_SWEEP_WRITER_ENABLED=0 in .env (mode stays authoritative for now)" {
    $envFile = ".env"
    $current = Get-Content $envFile
    $newLines = @()
    foreach ($line in $current) {
        if ($line -match "^CHILI_BRACKET_SWEEP_WRITER_ENABLED=") {
            $newLines += "CHILI_BRACKET_SWEEP_WRITER_ENABLED=0"
        } else {
            $newLines += $line
        }
    }
    $newLines | Set-Content $envFile -Encoding utf8
    Get-Content $envFile | Select-String "BRAIN_LIVE_BRACKETS_MODE|CHILI_BRACKET_SWEEP_WRITER_ENABLED" | ForEach-Object { $_.Line }
}

S "force-recreate broker-sync-worker (writer flag flips back to OFF; mode stays authoritative -> shadow fallback)" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 65s for one fresh sweep" { Start-Sleep -Seconds 65; "ok" }

S "verify writer is OFF and falls back to shadow" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT observed_at, mode, kind, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '90 seconds' GROUP BY observed_at, mode, kind ORDER BY observed_at DESC LIMIT 5;"
}

S "g2_ events should be quiet now" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS events_last_2m FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '2 minutes';"
}

# ---------- Step 2: Diagnose why broker_view_fn didn't see the first stop ----------

S "the first stop's broker order details (69f3947a)" {
    docker compose exec -T chili python -c @"
from app.services import broker_service
import json
order = broker_service.get_order_by_id('69f3947a-61cf-4e11-99c4-1f45879749e0')
if order:
    print(json.dumps({k: v for k, v in order.items() if k in ('id','symbol','side','type','trigger','state','price','stop_price','quantity','cumulative_quantity','last_transaction_at','created_at')}, default=str, indent=2))
else:
    print('order not found via get_order_by_id')
"@
}

S "all open Robinhood orders for ADT" {
    docker compose exec -T chili python -c @"
from app.services import broker_service
import json
orders = broker_service.get_recent_orders(limit=200) or []
adt_orders = [o for o in orders if o.get('symbol') == 'ADT']
print(f'ADT orders found: {len(adt_orders)}')
for o in adt_orders[:20]:
    print(json.dumps({k: v for k, v in o.items() if k in ('id','symbol','side','type','trigger','state','price','stop_price','quantity','cumulative_quantity','last_transaction_at','created_at')}, default=str, indent=2))
"@
}

S "what does broker_manager_view_fn return for ADT?" {
    docker compose exec -T chili python -c @"
from app.services.trading.bracket_reconciliation_service import broker_manager_view_fn
views = broker_manager_view_fn([{'ticker': 'ADT', 'broker_source': 'robinhood'}])
for v in views:
    print(f'available={v.available} stop_order_id={v.stop_order_id} stop_state={v.stop_order_state} stop_price={v.stop_order_price} target_id={v.target_order_id}')
"@
}

S "broker-sync-worker logs for the last minute (showing the loop pattern)" {
    docker compose logs --since 5m broker-sync-worker 2>&1 | Select-String -Pattern "ADT|missing_stop|writer_action|SELL_STOP|invalid_stop|duplicate" | Select-Object -Last 40
}

S "audit row recording the temporary deactivation" {
    docker compose exec -T postgres psql -U chili -d chili -c "INSERT INTO trading_learning_events (user_id, event_type, description, created_at) VALUES (NULL, 'r23_temp_deactivate', 'R23 sweep writer flipped OFF temporarily after first activation produced 1 successful stop on ADT (order 69f3947a) but subsequent sweeps duplicate-submitted and broker rejected. broker_manager_view_fn likely not seeing the placed stop. Mode stays authoritative; sweep_writer flag back to 0 until view-fn fix.', CURRENT_TIMESTAMP) RETURNING id;"
}

Write-Host "deactivate + diag done -- see $out"
