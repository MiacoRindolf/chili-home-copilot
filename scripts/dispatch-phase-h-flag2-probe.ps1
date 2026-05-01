$out = "scripts/dispatch-phase-h-flag2-probe-output.txt"
"# Phase H + Flag 2 prerequisite probe $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- Phase H probe ----------

S "Phase H: position sizer table row count + sample" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT column_name FROM information_schema.columns WHERE table_name = 'trading_position_sizer_log' ORDER BY ordinal_position LIMIT 30;"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS total_rows, MIN(decided_at) AS oldest, MAX(decided_at) AS newest FROM trading_position_sizer_log;"
}

S "Phase H: callers of position_sizer in production code" {
    docker compose exec -T chili sh -c 'grep -rn "position_sizer\|run_position_sizer\|kelly_aware_size" /app/app/services/ 2>/dev/null | grep -v __pycache__ | head -15'
}

S "Phase H: scheduled job presence" {
    docker compose exec -T chili sh -c 'grep -n "_run_position_sizer\|position_sizer_log" /app/app/services/trading_scheduler.py 2>/dev/null | head -10'
}

S "Phase H: brain_position_sizer_mode allowed values + reader code" {
    docker compose exec -T chili sh -c 'grep -rn "brain_position_sizer_mode\|_ALLOWED_MODES" /app/app/services/trading/position_sizer*.py 2>/dev/null | grep -v __pycache__ | head -10'
}

# ---------- Flag 2 probe (perps_lane) ----------

S "Flag 2: hyperliquid network reachability from chili container" {
    docker compose exec -T chili python -c @"
import socket, urllib.request, ssl
hosts = [('api.hyperliquid.xyz', 443), ('robinhood.com', 443)]
for host, port in hosts:
    try:
        ip = socket.gethostbyname(host)
        s = socket.create_connection((host, port), timeout=5)
        s.close()
        print(f'{host}:{port} -> {ip} TCP_OK')
    except socket.gaierror as e:
        print(f'{host}:{port} DNS_FAIL: {e}')
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        print(f'{host}:{port} TCP_FAIL: {e}')
print()
# Try a tiny HTTPS GET to confirm not just TCP but TLS+HTTP works
try:
    ctx = ssl.create_default_context()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info', method='HEAD')
    with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
        print(f'hyperliquid HEAD status={r.status}')
except Exception as e:
    print(f'hyperliquid HEAD failed: {type(e).__name__}: {e}')
"@
}

S "Flag 2: perp_contracts seed (mig 184)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT venue, COUNT(*) FROM perp_contracts GROUP BY venue ORDER BY count DESC;"
}

S "Flag 2: prior ingest data (perp_quotes / perp_funding)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT 'perp_quotes' AS tbl, COUNT(*) FROM perp_quotes UNION ALL SELECT 'perp_funding', COUNT(*) FROM perp_funding UNION ALL SELECT 'perp_oi', COUNT(*) FROM perp_oi UNION ALL SELECT 'perp_basis', COUNT(*) FROM perp_basis;"
}

S "Flag 2: scheduled job presence" {
    docker compose exec -T chili sh -c 'grep -n "perps_ingestion\|_run_perps\|perps_lane" /app/app/services/trading_scheduler.py 2>/dev/null | head -10'
}

# ---------- General health ----------

S "container uptime + last R28 deploy state" {
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}"
}

S "Phase 1 Flag 1: pattern_survival_features still has fresh rows from prior runs" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS rows, MAX(snapshot_date) AS latest, COUNT(DISTINCT scan_pattern_id) AS distinct_patterns FROM pattern_survival_features WHERE snapshot_date > NOW() - INTERVAL '7 days';"
}

Write-Host "phase H + flag 2 probe done -- see $out"
