$out = "scripts/dispatch-auth-failure-wipeout-trace-output.txt"
"# verify auth-failure -> position-wipeout hypothesis $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "1. simultaneous closes - timestamps in detail" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, exit_date, exit_reason FROM trading_trades WHERE ticker LIKE '%-USD' AND status='closed' AND exit_date BETWEEN '2026-04-30 15:55:00' AND '2026-04-30 15:57:00' ORDER BY exit_date;"
}

S "2. broker-sync-worker logs around 15:56 UTC (close window)" {
    docker compose logs --since 12h broker-sync-worker 2>&1 | Select-String "15:55|15:56|invalid_grant|refresh_token|sync_positions|stale|reconcile_position_gone|crypto.*position" | Select-Object -First 30
}

S "3. read sync_positions_to_db: does it have empty-positions guard?" {
    docker compose exec -T chili python -c "import inspect; from app.services.broker_service import sync_positions_to_db; src = inspect.getsource(sync_positions_to_db); has_empty_guard = 'no positions' in src.lower() or 'positions == []' in src or 'len(positions) == 0' in src or 'not positions' in src; lines = src.split(chr(10)); print('total lines:', len(lines)); print('has empty-positions guard:', has_empty_guard); print('handles auth failure:', 'is_connected' in src)"
}

S "4. read get_crypto_positions: failure modes" {
    docker compose exec -T chili sh -c 'grep -n -A 5 "def get_crypto_positions" /app/app/services/broker_service.py | head -40'
}

S "5. read get_positions: failure modes" {
    docker compose exec -T chili sh -c 'grep -n -A 5 "def get_positions" /app/app/services/broker_service.py | head -40'
}

S "6. how does sync_positions_to_db build the 'stale' list?" {
    docker compose exec -T chili sh -c 'grep -n -B 2 -A 10 "stale = \[" /app/app/services/broker_service.py | head -25'
}

Write-Host "auth-failure trace done -- see $out"
