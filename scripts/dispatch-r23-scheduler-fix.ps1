$out = "scripts/dispatch-r23-scheduler-fix-output.txt"
"# r23 scheduler-gate fix + activate $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "py-compile scheduler" {
    conda run -n chili-env python -m py_compile app/services/trading_scheduler.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git status" {
    git status -s app/services/trading_scheduler.py
}

S "force-recreate broker-sync-worker (picks up scheduler patch via bind mount)" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 10s for container start" { Start-Sleep -Seconds 10; "ok" }

S "broker-sync-worker uptime" {
    docker ps --filter "name=chili-home-copilot-broker-sync-worker-1" --format "{{.Names}} | {{.Status}}"
}

S "scheduler startup logs (jobs registered)" {
    docker compose logs --tail=80 broker-sync-worker 2>&1 | Select-String "Adding job|Added job|jobs registered|broker_sync_only|bracket_reconciliation" | Select-Object -Last 30
}

S "wait 90s for at least one bracket sweep" { Start-Sleep -Seconds 90; "ok" }

S "fresh sweep rows (last 90s)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT observed_at, mode, kind, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '90 seconds' GROUP BY observed_at, mode, kind ORDER BY observed_at DESC;"
}

S "g2_ execution events created since restart" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, status, ticker, payload_json->>'reason' AS reason, payload_json->>'stop_price' AS stop_price, payload_json->>'qty' AS qty, recorded_at FROM trading_execution_events WHERE event_type LIKE 'g2_%' ORDER BY id DESC LIMIT 30;"
}

S "broker-sync-worker logs: bracket_writer_g2 + writer_action lines" {
    docker compose logs --since 3m broker-sync-worker 2>&1 | Select-String -Pattern "bracket_writer_g2|writer_action|missing_stop|authoritative" | Select-Object -Last 40
}

S "current open Robinhood trades" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, broker_order_id IS NOT NULL AS has_oid, ROUND(entry_price::numeric,4) AS entry, ROUND(stop_loss::numeric,4) AS stop FROM trading_trades WHERE status = 'open' AND broker_source = 'robinhood' ORDER BY entry_date DESC;"
}

S "open SELL stop orders on broker (recent_orders cache poll)" {
    docker compose exec -T chili python -c "from app.services import broker_service; orders = broker_service.get_recent_orders() or []; stops = [o for o in orders if str(o.get('side','')).lower()=='sell' and (o.get('trigger') == 'stop' or o.get('stop_price'))]; import json; print('stop-side sell orders found:', len(stops)); print(json.dumps(stops[:5], default=str, indent=2))"
}

Write-Host "scheduler-fix activate done -- see $out"
