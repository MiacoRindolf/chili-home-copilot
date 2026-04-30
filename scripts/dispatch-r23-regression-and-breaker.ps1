$out = "scripts/dispatch-r23-regression-and-breaker-output.txt"
"# R23 regression + drawdown breaker investigation $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- ADT stop investigation ----------

S "ADT trade current state" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, broker_order_id, ROUND(entry_price::numeric,4) AS entry, ROUND(exit_price::numeric,4) AS exit, pnl, pending_exit_status, pending_exit_reason FROM trading_trades WHERE id = 1694;"
}

S "ADT broker order 69f3947a state" {
    docker compose exec -T chili python -c "from app.services import broker_service; import json; o = broker_service.get_order_by_id('69f3947a-61cf-4e11-99c4-1f45879749e0'); print(json.dumps(o, default=str, indent=2) if o else 'not found')"
}

S "g2_ events for ADT in last 2 hours - what's the writer doing?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, status, ticker, order_id, payload_json->>'error' AS error, payload_json->>'new_stop_order_id' AS new_stop, recorded_at FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '2 hours' ORDER BY id DESC LIMIT 30;"
}

S "Robinhood positions: does broker still hold ADT?" {
    docker compose exec -T chili python -c "from app.services import broker_service; positions = broker_service.get_positions() or []; adt = [p for p in positions if (p.get('ticker') or '').upper() == 'ADT']; print('ADT held:', adt)"
}

S "open Robinhood orders (any new ADT stop the writer placed?)" {
    docker compose exec -T chili python -c "from app.services import broker_service; import json; orders = broker_service.get_recent_orders(limit=100) or []; adt = [o for o in orders if (o.get('symbol') or '') == 'ADT']; print(f'ADT recent orders: {len(adt)}'); [print(json.dumps({k:v for k,v in o.items() if k in ('id','side','type','trigger','state','stop_price','quantity','last_transaction_at')}, default=str)) for o in adt[:10]]"
}

# ---------- Drawdown breaker investigation ----------

S "drawdown breaker rows last 24h" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, snapshot_date, breaker_tripped, breaker_reason, total_heat_pct, capital, regime, created_at FROM trading_risk_state WHERE created_at > NOW() - INTERVAL '24 hours' ORDER BY created_at DESC LIMIT 10;"
}

S "what 5 consecutive losing trades?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, ROUND(entry_price::numeric,4) AS entry, ROUND(exit_price::numeric,4) AS exit, pnl, exit_reason, exit_date FROM trading_trades WHERE pnl IS NOT NULL AND status='closed' AND exit_date > NOW() - INTERVAL '24 hours' ORDER BY exit_date DESC LIMIT 15;"
}

S "kill switch + drawdown breaker code-side checks" {
    docker compose exec -T chili python -c @"
from app.services.trading.governance import get_kill_switch_status
print('kill_switch:', get_kill_switch_status())
try:
    from app.services.trading.governance import check_daily_loss_breach
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        r = check_daily_loss_breach(db, user_id=1)
        print('daily_loss_breach:', r)
    finally:
        db.close()
except Exception as e:
    print('daily_loss_breach probe failed:', e)
"@
}

# ---------- Writer behavior under breaker tripped ----------

S "is the writer respecting the breaker? (should it block during circuit_breaker regime?)" {
    docker compose exec -T scheduler-worker python -c @"
# Look at whether bracket_writer_g2 / _invoke_writer_for_decision considers breaker state
import inspect
from app.services.trading import bracket_writer_g2 as g2
src = inspect.getsource(g2.place_missing_stop)
print('place_missing_stop guards:')
for line in src.split('\\n')[:50]:
    print(' ', line)
"@
}

Write-Host "regression + breaker investigation done -- see $out"
