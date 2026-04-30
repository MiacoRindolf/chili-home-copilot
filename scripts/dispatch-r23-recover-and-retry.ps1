$out = "scripts/dispatch-r23-recover-and-retry-output.txt"
"# r23 recover trades + retry activation $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- Step 1: Recover the 3 corrupted trade rows ----------

S "before-recovery snapshot" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, ROUND(entry_price::numeric,4) AS entry, ROUND(exit_price::numeric,4) AS exit, pnl, exit_reason, exit_date FROM trading_trades WHERE id IN (1694, 1759, 1781) ORDER BY id;"
}

S "recover trade 1694 ADT (no exit info, broker holds 42 sh -> status=open)" {
    docker compose exec -T postgres psql -U chili -d chili -c "BEGIN; UPDATE trading_trades SET status='open', broker_status='filled', notes = COALESCE(notes,'') || E'\n[r23-recover] status reverted from rejected->open after writer audit-row contamination 2026-04-30; broker still holds 42 sh' WHERE id = 1694 AND status='rejected'; SELECT id, status, broker_status FROM trading_trades WHERE id=1694; COMMIT;"
}

S "recover trade 1759 WDCX (had real exit + pnl -> status=closed)" {
    docker compose exec -T postgres psql -U chili -d chili -c "BEGIN; UPDATE trading_trades SET status='closed', broker_status='filled', notes = COALESCE(notes,'') || E'\n[r23-recover] status reverted from rejected->closed after writer audit-row contamination 2026-04-30; real exit at 64.50 +6.70 stop on 08:02' WHERE id = 1759 AND status='rejected'; SELECT id, status, broker_status FROM trading_trades WHERE id=1759; COMMIT;"
}

S "recover trade 1781 ABEV (had real exit + pnl -> status=closed)" {
    docker compose exec -T postgres psql -U chili -d chili -c "BEGIN; UPDATE trading_trades SET status='closed', broker_status='filled', notes = COALESCE(notes,'') || E'\n[r23-recover] status reverted from rejected->closed after writer audit-row contamination 2026-04-30; real exit at 2.8602 +0.55 stop on 13:04' WHERE id = 1781 AND status='rejected'; SELECT id, status, broker_status FROM trading_trades WHERE id=1781; COMMIT;"
}

S "audit row to record the recovery" {
    docker compose exec -T postgres psql -U chili -d chili -c "INSERT INTO trading_learning_events (user_id, event_type, description, created_at) VALUES (NULL, 'r23_recover_trades', 'Recovered trades 1694/1759/1781 from status=rejected after R23 writer first run set status via apply_execution_event_to_trade (now fixed in code: writer passes trade=None to record_execution_event)', CURRENT_TIMESTAMP) RETURNING id;"
}

S "after-recovery snapshot" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, ROUND(entry_price::numeric,4) AS entry, ROUND(exit_price::numeric,4) AS exit, pnl FROM trading_trades WHERE id IN (1694, 1759, 1781) ORDER BY id;"
}

# ---------- Step 2: Validate the writer code fix ----------

S "py-compile bracket_writer_g2 + broker_service" {
    conda run -n chili-env python -m py_compile app/services/trading/bracket_writer_g2.py app/services/broker_service.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git diff bracket_writer_g2 (the trade=None fix)" {
    git diff app/services/trading/bracket_writer_g2.py | Select-Object -First 80
}

# ---------- Step 3: Restart broker-sync-worker to pick up the fix ----------

S "force-recreate broker-sync-worker" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 90s for one bracket sweep" { Start-Sleep -Seconds 90; "ok" }

# ---------- Step 4: Verify writer behavior post-fix ----------

S "trade 1694 status (should STAY open after sweep)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status FROM trading_trades WHERE id = 1694;"
}

S "g2_ events post-fix (will record the broker outcome but not contaminate trade)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, status, ticker, payload_json->>'error' AS error, recorded_at FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '5 minutes' ORDER BY id DESC LIMIT 20;"
}

S "broker-sync-worker logs since restart (look for actual stop placement)" {
    docker compose logs --since 3m broker-sync-worker 2>&1 | Select-String -Pattern "bracket_writer_g2|writer_action|missing_stop|order\(\) got|SELL_STOP|invalid_stop_price" | Select-Object -Last 40
}

S "any new sell-stop orders on broker" {
    docker compose exec -T chili python -c @"
from app.services import broker_service
import json
orders = broker_service.get_recent_orders() or []
adt_orders = [o for o in orders if o.get('symbol') == 'ADT']
print(f'recent ADT orders: {len(adt_orders)}')
for o in adt_orders[:10]:
    print(json.dumps({k: v for k, v in o.items() if k in ('id','symbol','side','type','trigger','state','price','stop_price','quantity','last_transaction_at','created_at')}, default=str, indent=2))
print()
stops = [o for o in orders if str(o.get('side','')).lower()=='sell' and (o.get('trigger') == 'stop' or (o.get('stop_price') and o.get('stop_price') != '0'))]
print(f'all sell-stop orders: {len(stops)}')
for o in stops[:5]:
    print(json.dumps({k: v for k, v in o.items() if k in ('id','symbol','side','type','trigger','state','stop_price','quantity','last_transaction_at')}, default=str, indent=2))
"@
}

Write-Host "recover + retry done -- see $out"
