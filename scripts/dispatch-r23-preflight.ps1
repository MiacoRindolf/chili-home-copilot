$out = "scripts/dispatch-r23-preflight-output.txt"
"# r23 PRE-FLIGHT (verify stop semantics before flipping) $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# The writer uses trading_bracket_intents.stop_price (not trading_trades.stop_loss).
# Verify the intent stop_price is BELOW current bid for each open Robinhood trade --
# that is the precondition for a SELL stop-loss order. If stop_price > current bid,
# the broker will reject with invalid_stop_price.

S "trade + bracket intent join (the data the writer actually sees)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT t.id AS trade_id, t.ticker, t.direction, ROUND(t.entry_price::numeric,4) AS entry, ROUND(t.stop_loss::numeric,4) AS trade_stop_loss, ROUND(t.take_profit::numeric,4) AS trade_take_profit, bi.id AS intent_id, bi.intent_state, ROUND(bi.stop_price::numeric,4) AS intent_stop, ROUND(bi.target_price::numeric,4) AS intent_target FROM trading_trades t LEFT JOIN trading_bracket_intents bi ON bi.trade_id = t.id WHERE t.status='open' AND t.broker_source='robinhood' ORDER BY t.id;"
}

S "current best bid/ask for each ticker (broker call)" {
    docker compose exec -T chili python -c "from app.services import broker_service; tickers = ['ABEV','WDCX','ADT']; import json; out = {}; [out.__setitem__(t, broker_service.fetch_quote(t)) for t in tickers]; print(json.dumps({k: {kk: vv for kk,vv in (v or {}).items() if kk in ('bid','ask','mark_price','last_trade_price')} for k,v in out.items()}, default=str, indent=2))" 2>&1
}

S "stop semantics check (intent.stop_price vs current bid)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT t.ticker, ROUND(t.entry_price::numeric,4) AS entry, ROUND(bi.stop_price::numeric,4) AS intent_stop, CASE WHEN bi.stop_price < t.entry_price THEN 'below_entry_OK' WHEN bi.stop_price > t.entry_price THEN 'ABOVE_ENTRY_BAD' ELSE 'equal' END AS sanity FROM trading_trades t JOIN trading_bracket_intents bi ON bi.trade_id = t.id WHERE t.status='open' AND t.broker_source='robinhood';"
}

S "verify writer module sees the right stop_price (dry-run query in chili container)" {
    docker compose exec -T chili python -c @"
from app.db import SessionLocal
from app.services.trading.bracket_reconciliation_service import _load_local_view
db = SessionLocal()
try:
    rows = _load_local_view(db)
    rh = [r for r in rows if (r.get('broker_source') or '').lower() == 'robinhood']
    for r in rh:
        print(f'trade={r[\"trade_id\"]} ticker={r[\"ticker\"]} qty={r[\"quantity\"]} intent_stop={r[\"stop_price\"]} intent_target={r[\"target_price\"]} intent_state={r[\"intent_state\"]}')
finally:
    db.close()
"@
}

Write-Host "preflight complete -- see $out"
