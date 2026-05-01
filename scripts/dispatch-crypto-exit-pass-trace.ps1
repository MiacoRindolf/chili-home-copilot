$out = "scripts/dispatch-crypto-exit-pass-trace-output.txt"
"# trace whether run_crypto_exit_pass actually fires + why it produces 0 closes $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "1. autotrader-worker uptime + role" {
    docker ps --filter "name=chili-home-copilot-autotrader-worker-1" --format "{{.Names}} | {{.Status}}"
    docker compose exec -T autotrader-worker sh -c 'echo "ROLE=$CHILI_SCHEDULER_ROLE"'
}

S "2. is auto_trader_monitor job registered?" {
    docker compose exec -T autotrader-worker python -c "from app.services import trading_scheduler as ts; sched = getattr(ts, '_scheduler', None); print('jobs:'); [print(f'  {j.id} next={j.next_run_time}') for j in (sched.get_jobs() if sched else [])]" 2>&1 | head -40
}

S "3. recent auto_trader_monitor brain_batch_jobs runs (last 24h)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT job_type, status, started_at, ended_at FROM brain_batch_jobs WHERE job_type = 'auto_trader_monitor' AND started_at > NOW() - INTERVAL '24 hours' ORDER BY started_at DESC LIMIT 10;"
}

S "4. autotrader-worker logs: any 'crypto_exit_pass' or 'run_crypto_exit_pass' lines (last 6h)" {
    docker compose logs --since 6h autotrader-worker 2>&1 | Select-String -Pattern "crypto_exit_pass|run_crypto_exit|crypto.*closed|crypto_exit_monitor" | Select-Object -Last 30
}

S "5. configured cadence for auto_trader_monitor + tick" {
    docker compose exec -T autotrader-worker python -c "from app.config import settings; print('monitor_interval_s:', settings.chili_autotrader_monitor_interval_seconds); print('tick_interval_s:', settings.chili_autotrader_tick_interval_seconds); print('crypto_exit_monitor_enabled:', settings.chili_autotrader_crypto_exit_monitor_enabled); print('autotrader_enabled:', settings.chili_autotrader_enabled)"
}

S "6. simulate run_crypto_exit_pass right now (no open RH crypto, but check the candidate pool logic)" {
    docker compose exec -T autotrader-worker python -c @"
from app.db import SessionLocal
from app.services.trading.crypto.exit_monitor import run_crypto_exit_pass
db = SessionLocal()
try:
    result = run_crypto_exit_pass(db)
    print('result:', result)
finally:
    db.close()
"@
}

S "7. retroactively look at HISTORIC crypto trades: did any have stop_loss/take_profit set?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, ROUND(entry_price::numeric,4) AS entry, ROUND(exit_price::numeric,4) AS exit, ROUND(stop_loss::numeric,4) AS stop, ROUND(take_profit::numeric,4) AS target, exit_reason, entry_date::date FROM trading_trades WHERE ticker LIKE '%-USD' AND status='closed' AND entry_date > NOW() - INTERVAL '30 days' ORDER BY entry_date DESC LIMIT 20;"
}

S "8. count of crypto trades in 30d that EVER had a stop_loss set vs not" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE stop_loss IS NOT NULL AND stop_loss > 0) AS with_stop, COUNT(*) FILTER (WHERE take_profit IS NOT NULL AND take_profit > 0) AS with_target FROM trading_trades WHERE ticker LIKE '%-USD' AND entry_date > NOW() - INTERVAL '30 days';"
}

S "9. WHO is closing crypto trades? trace the close path" {
    docker compose exec -T chili sh -c 'grep -rn "broker_reconcile_position_gone\|trade.exit_reason\s*=\s*.broker_reconcile" /app/app/services/ 2>/dev/null | grep -v __pycache__ | head -10'
}

Write-Host "crypto exit pass trace done -- see $out"
