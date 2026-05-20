# Re-runnable crypto autotrading pulse monitor.
# Cowork dispatches periodically to surface anomalies in the Coinbase
# live-soak lane. Output APPENDED to scripts/dispatch-crypto-pulse-out.txt.
#
# Table names are prefixed `trading_` per SQLAlchemy __tablename__:
#   trading_trades, trading_bracket_intents, trading_execution_events,
#   trading_autotrader_runs, trading_alerts, trading_venue_truth_log.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-crypto-pulse-out.txt"

$ts = Get-Date -Format o
"" | Add-Content $out
"================================================================" | Add-Content $out
"# CRYPTO PULSE $ts" | Add-Content $out
"================================================================" | Add-Content $out

function PSQL {
    param([string]$Label, [string]$Query)
    "## $Label" | Add-Content $out
    $tmp = [System.IO.Path]::GetTempFileName()
    $Query | Out-File $tmp -Encoding ascii
    try {
        & docker cp $tmp chili-home-copilot-postgres-1:/tmp/qpulse.sql 2>&1 | Out-Null
        $r = & docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -f /tmp/qpulse.sql 2>&1
        $r | Out-String | Add-Content $out
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
    }
    "" | Add-Content $out
}

function GREP-LOGS {
    param([string]$Label, [string]$Container, [string]$Pattern, [int]$Tail = 30, [string]$Since = "15m")
    "## $Label" | Add-Content $out
    $logs = & docker logs --since $Since $Container 2>&1
    $matches = $logs | Select-String -Pattern $Pattern
    if ($matches) {
        $count = ($matches | Measure-Object).Count
        "match count: $count (last $Tail shown)" | Add-Content $out
        $matches | Select-Object -Last $Tail | Out-String | Add-Content $out
    } else {
        "no matches" | Add-Content $out
    }
    "" | Add-Content $out
}

# 1. get_crypto_positions empty / auth-failure events
GREP-LOGS "1. get_crypto_positions empty/auth failures (autotrader-worker, 15m)" `
    "chili-home-copilot-autotrader-worker-1" `
    "get_crypto_positions|crypto_positions.*empty|coinbase.*auth|coinbase.*401|coinbase.*403" `
    25 "15m"

GREP-LOGS "1b. broker-sync-worker get_crypto_positions (15m)" `
    "chili-home-copilot-broker-sync-worker-1" `
    "get_crypto_positions|crypto_positions.*empty|coinbase.*auth|coinbase.*401|coinbase.*403" `
    25 "15m"

# 2. crypto_exit deferral warnings
GREP-LOGS "2. crypto_exit cannot resolve broker qty (autotrader-worker, 15m)" `
    "chili-home-copilot-autotrader-worker-1" `
    "cannot resolve broker qty|deferring sell" `
    25 "15m"

# 3. bracket_reconciliation missing_stop
GREP-LOGS "3. bracket_reconciliation missing_stop (broker-sync, 15m)" `
    "chili-home-copilot-broker-sync-worker-1" `
    "kind=missing_stop" `
    25 "15m"

# 4. broker_reconcile_position_gone trades (last 60 min)
PSQL "4. recent reconcile-position-gone trades (last 60 min)" @"
SELECT t.id, t.ticker, t.status, t.exit_reason, t.exit_price, t.exit_at,
       t.open_at, t.qty, t.entry_price
  FROM trading_trades t
 WHERE t.exit_reason ILIKE '%reconcile_position_gone%'
   AND t.exit_at > now() - interval '60 minutes'
 ORDER BY t.exit_at DESC
 LIMIT 10;
"@

# 5. Recent Coinbase trades in last 30 min
PSQL "5. Recent Coinbase trades (last 30 min, entry/exit)" @"
SELECT t.id, t.ticker, t.status, t.side, t.qty, t.entry_price, t.exit_price,
       t.open_at, t.exit_at, t.exit_reason, t.scan_pattern_id
  FROM trading_trades t
 WHERE t.broker = 'coinbase'
   AND (t.open_at > now() - interval '30 minutes'
        OR t.exit_at > now() - interval '30 minutes')
 ORDER BY t.open_at DESC
 LIMIT 20;
"@

# 6. Implausible-quote events
GREP-LOGS "6. Implausible quote events (15m)" `
    "chili-home-copilot-autotrader-worker-1" `
    "implausible_quote|quote_implausible|stale_quote" `
    20 "15m"

# 7. idle-in-tx by application_name
PSQL "7. idle-in-tx by application_name" @"
SELECT application_name,
       count(*) AS conns,
       count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_tx
  FROM pg_stat_activity
 WHERE datname = 'chili'
 GROUP BY application_name
 ORDER BY idle_in_tx DESC, conns DESC;
"@

# 8. Currently open crypto trades + bracket intent state
PSQL "8. Currently open crypto trades + bracket intent state" @"
SELECT t.id AS trade_id, t.ticker, t.qty, t.entry_price, t.open_at,
       bi.id AS bi_id, bi.kind AS bi_kind, bi.status AS bi_status,
       bi.target_price, bi.stop_price,
       bi.broker_order_id, bi.broker_stop_order_id,
       bi.last_status_at,
       LEFT(coalesce(bi.reason_no_op, ''), 80) AS reason_no_op
  FROM trading_trades t
  LEFT JOIN trading_bracket_intents bi ON bi.trade_id = t.id
 WHERE t.broker = 'coinbase'
   AND t.status = 'open'
 ORDER BY t.open_at DESC
 LIMIT 15;
"@

# 9. Recent autotrader decisions for crypto in last 30 min
PSQL "9. Recent autotrader decisions on crypto (last 30 min)" @"
SELECT id, ticker, decision, reason, occurred_at, alert_id, trade_id
  FROM trading_autotrader_runs
 WHERE ticker LIKE '%-USD'
   AND occurred_at > now() - interval '30 minutes'
 ORDER BY occurred_at DESC
 LIMIT 20;
"@

# 10. Phase 4 session daemon status
"## 10. Phase 4 session daemon status" | Add-Content $out
if (Test-Path "scripts/_claude_session_status.json") {
    Get-Content "scripts/_claude_session_status.json" -Raw | Add-Content $out
} else {
    "no status.json" | Add-Content $out
}
"" | Add-Content $out

# 11. Plan-gate consult dir for current Phase
"## 11. Phase 4 plan-gate consult dir" | Add-Content $out
$consultDir = "scripts/_claude_session_consult/promotion-rebalance-phase4-2026-05-10"
if (Test-Path $consultDir) {
    Get-ChildItem $consultDir | Select-Object Name, Length, LastWriteTime | Format-Table | Out-String | Add-Content $out
} else {
    "(no consult dir yet -- session may not have created it)" | Add-Content $out
}
"" | Add-Content $out

"================================================================" | Add-Content $out
"# pulse complete $(Get-Date -Format o)" | Add-Content $out
"================================================================" | Add-Content $out
