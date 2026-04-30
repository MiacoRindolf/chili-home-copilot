$out = "scripts/dispatch-r23-fix-trigger-and-investigate-output.txt"
"# r23 fix trigger kwargs + investigate position closes $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- Investigation: what happened to ADT/WDCX/ABEV ----------

S "current state of trades 1694, 1759, 1781" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, broker_order_id, ROUND(entry_price::numeric,4) AS entry, ROUND(exit_price::numeric,4) AS exit, ROUND(pnl::numeric,2) AS pnl, exit_reason, exit_date FROM trading_trades WHERE id IN (1694, 1759, 1781) ORDER BY id;"
}

S "any close events in trading_execution_events for those tickers in last 10 min" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, status, ticker, order_id, recorded_at FROM trading_execution_events WHERE ticker IN ('ADT','WDCX','ABEV') AND recorded_at > NOW() - INTERVAL '10 minutes' ORDER BY id DESC LIMIT 30;"
}

S "any new sell orders on those tickers in last 10 min" {
    docker compose exec -T chili python -c @"
from app.services import broker_service
import json
orders = broker_service.get_recent_orders() or []
relevant = [o for o in orders if o.get('symbol') in ('ADT','WDCX','ABEV') or (o.get('instrument_url') and any(t in str(o.get('instrument_url','')) for t in ('ADT','WDCX','ABEV')))]
print(f'recent orders for ADT/WDCX/ABEV: {len(relevant)}')
for o in relevant[:10]:
    print(json.dumps({k: v for k, v in o.items() if k in ('id','symbol','side','type','state','price','stop_price','quantity','cumulative_quantity','last_transaction_at','created_at')}, default=str, indent=2))
"@
}

S "current Robinhood positions (ground truth)" {
    docker compose exec -T chili python -c @"
from app.services import broker_service
positions = broker_service.get_positions() or []
relevant = [p for p in positions if p.get('ticker') in ('ADT','WDCX','ABEV')]
print(f'positions still held: {len(relevant)}')
for p in relevant:
    print(p)
print('--- all current positions ---')
for p in positions:
    print(f'{p.get(\"ticker\"):>10}  qty={p.get(\"quantity\")}  avg=${p.get(\"avg_price\")}')
"@
}

S "autotrader-worker logs for those tickers in last 10 min" {
    docker compose logs --since 10m autotrader-worker 2>&1 | Select-String -Pattern "ADT|WDCX|ABEV" | Select-Object -Last 30
}

# ---------- Apply the trigger-kwarg fix ----------

S "py-compile broker_service" {
    conda run -n chili-env python -m py_compile app/services/broker_service.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git diff broker_service.py (the fix)" {
    git diff app/services/broker_service.py | Select-Object -First 80
}

S "force-recreate broker-sync-worker" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 90s for at least one bracket sweep" { Start-Sleep -Seconds 90; "ok" }

S "g2_ events post-fix" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, status, ticker, order_id, payload_json->>'error' AS error, recorded_at FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '5 minutes' ORDER BY id DESC LIMIT 20;"
}

S "broker-sync-worker logs: bracket_writer_g2 lines post-fix" {
    docker compose logs --since 3m broker-sync-worker 2>&1 | Select-String -Pattern "bracket_writer_g2|writer_action|missing_stop|order\(\) got" | Select-Object -Last 40
}

S "any new SELL stop orders on broker post-fix" {
    docker compose exec -T chili python -c @"
from app.services import broker_service
import json
orders = broker_service.get_recent_orders() or []
stops = [o for o in orders if o.get('side') == 'sell' and (o.get('trigger') == 'stop' or o.get('stop_price'))]
print(f'sell-stop orders: {len(stops)}')
for o in stops[:10]:
    print(json.dumps({k: v for k, v in o.items() if k in ('id','symbol','side','type','trigger','state','price','stop_price','quantity','last_transaction_at','created_at')}, default=str, indent=2))
"@
}

Write-Host "fix-trigger investigation complete -- see $out"
