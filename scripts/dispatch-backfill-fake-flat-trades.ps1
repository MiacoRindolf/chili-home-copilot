$out = "scripts/dispatch-backfill-fake-flat-trades-output.txt"
"# backfill 5 fake-flat trades $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "before" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, entry_price, exit_price, pnl, exit_reason, exit_date::date FROM trading_trades WHERE id IN (393, 440, 585, 610, 611) ORDER BY id;"
}

S "run backfill via chili container (uses _resolve_close_exit_price)" {
    docker compose exec -T chili python -c "
from app.db import SessionLocal
from app.models.trading import Trade
from app.services.broker_service import _resolve_close_exit_price
import json

db = SessionLocal()
try:
    trade_ids = [393, 440, 585, 610, 611]
    out = []
    for tid in trade_ids:
        t = db.query(Trade).filter(Trade.id == tid).first()
        if t is None:
            out.append({'id': tid, 'status': 'not_found'})
            continue
        if t.entry_price != t.exit_price or (t.pnl or 0) != 0:
            out.append({'id': tid, 'status': 'already_corrected', 'entry': float(t.entry_price), 'exit': float(t.exit_price), 'pnl': float(t.pnl or 0)})
            continue
        resolved = _resolve_close_exit_price(t.ticker)
        if resolved is None or resolved <= 0:
            t.exit_price = None
            t.pnl = None
            old_reason = t.exit_reason or ''
            t.exit_reason = 'broker_reconcile_no_exit_price'
            t.notes = (t.notes or '') + f'\nBackfilled 2026-04-30: original close had fake PnL=0 because _get_exit_price fell back to entry_price; no recoverable real exit price from order history (4-day window expired). Marking pnl=NULL per no-hardcoded-fallback rule. Old exit_reason: {old_reason}'
            db.commit()
            out.append({'id': tid, 'ticker': t.ticker, 'status': 'cleared_pnl_to_null', 'old_pnl': 0.0})
        else:
            entry = float(t.entry_price or 0)
            qty = float(t.quantity or 0)
            new_pnl = round((resolved - entry) * qty, 2)
            t.exit_price = float(resolved)
            t.pnl = new_pnl
            t.notes = (t.notes or '') + f'\nBackfilled 2026-04-30: original close had fake PnL=0 because _get_exit_price fell back to entry_price; resolved real exit ${resolved:.4f} from order history.'
            db.commit()
            out.append({'id': tid, 'ticker': t.ticker, 'status': 'fixed', 'old_exit': entry, 'new_exit': float(resolved), 'new_pnl': new_pnl})
    print(json.dumps(out, indent=2))
finally:
    db.close()
" 2>&1
}

S "after" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, entry_price, exit_price, pnl, exit_reason FROM trading_trades WHERE id IN (393, 440, 585, 610, 611) ORDER BY id;"
}

Write-Host "done"
