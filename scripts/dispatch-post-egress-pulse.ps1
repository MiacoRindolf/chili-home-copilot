$out = "scripts/dispatch-post-egress-pulse-output.txt"
"# post-egress brain pulse $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "scheduler-worker errors last 10min (now that egress is up, what's failing?)" {
    docker compose logs --since 10m scheduler-worker 2>&1 | Select-String "ERROR|Traceback" | Select-Object -Last 20
}

S "brain-worker activity last 10min" {
    docker compose logs --since 10m brain-worker 2>&1 | Select-String "work ledger|claimed=|processed=|backtest|mine" | Select-Object -Last 15
}

S "autotrader candidate flow last 5min (any rise from 0?)" {
    docker compose logs --since 5m autotrader-worker 2>&1 | Select-String "candidate_pool|tick uid" | Select-Object -Last 5
}

S "pattern_imminent recent run? (look for run_pattern_imminent_scan log lines)" {
    docker compose logs --since 30m scheduler-worker brain-worker 2>&1 | Select-String "pattern_imminent|imminent_scan|imminent_alert" | Select-Object -Last 15
}

S "brain_batch_jobs heartbeats" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT job_name, last_run_at, status, last_run_seconds, last_message FROM brain_batch_jobs ORDER BY last_run_at DESC LIMIT 12;"
}

S "breakout_alerts last 30min (pattern_imminent dispatches)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT alert_tier, COUNT(*) AS n, MAX(alerted_at) AS most_recent FROM breakout_alert WHERE alerted_at > NOW() - INTERVAL '30 minutes' GROUP BY alert_tier ORDER BY 2 DESC;"
}

S "trading_market_snapshots ingestion (recent rows = mining is alive)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT date_trunc('minute', created_at) AS bucket, COUNT(*) FROM market_snapshot WHERE created_at > NOW() - INTERVAL '30 minutes' GROUP BY 1 ORDER BY 1 DESC LIMIT 10;"
}

S "scan_pattern lifecycle counts (still 1 promoted? should be growing)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT lifecycle, COUNT(*) FROM scan_pattern GROUP BY lifecycle ORDER BY 2 DESC;"
}

S "live coinbase OHLCV fetch test (does the brain actually get data now?)" {
    docker compose exec -T scheduler-worker python -c "
from app.services.trading.coinbase_ohlcv import fetch_candles_coinbase
import time
t0 = time.time()
df = fetch_candles_coinbase('BTC-USD', '1h', limit=24)
print(f'BTC-USD 1h x24: rows={len(df) if df is not None else None} elapsed={int((time.time()-t0)*1000)}ms')
print(df.head(3).to_string() if df is not None and len(df) > 0 else '(no data)')
"
}

S "scheduler heartbeat" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT * FROM scheduler_worker_heartbeat ORDER BY id DESC LIMIT 3;"
}

Write-Host "post-egress pulse done -- see $out"
