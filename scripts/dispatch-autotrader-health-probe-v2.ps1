# Autotrader/exit-monitor health probe v2 — fixes column name drift from v1.
# Schema-correct (verified against app/models/trading.py):
#   trading_trades:           direction (NOT side), quantity (NOT qty),
#                             entry_date (NOT open_at), exit_date (NOT exit_at),
#                             broker_source (NOT broker), exit_reason
#   trading_autotrader_runs:  created_at (NOT occurred_at),
#                             breakout_alert_id (NOT alert_id)
#   trading_bracket_intents:  intent_state (one column, NOT kind+status),
#                             last_diff_reason (NOT reason_no_op),
#                             last_observed_at (NOT last_status_at)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-autotrader-health-out.txt"

$ts = Get-Date -Format o
"" | Add-Content $out
"================================================================" | Add-Content $out
"# AUTOTRADER HEALTH v2 $ts" | Add-Content $out
"================================================================" | Add-Content $out

function PSQL {
    param([string]$Label, [string]$Query)
    "## $Label" | Add-Content $out
    $tmp = [System.IO.Path]::GetTempFileName()
    $Query | Out-File $tmp -Encoding ascii
    try {
        & docker cp $tmp chili-home-copilot-postgres-1:/tmp/qhealth.sql 2>&1 | Out-Null
        $r = & docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -f /tmp/qhealth.sql 2>&1
        $r | Out-String | Add-Content $out
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
    }
    "" | Add-Content $out
}

function GREP-LOGS {
    param([string]$Label, [string]$Container, [string]$Pattern, [int]$Tail = 20, [string]$Since = "15m")
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

# 1. Currently-open trades — operator's headline overnight concern
PSQL "1. ALL currently-open trades" @"
SELECT id, ticker, direction, quantity, entry_price, broker_source,
       entry_date,
       extract(epoch from (now() - entry_date))/3600.0 AS age_hours,
       scan_pattern_id, last_fill_at,
       LEFT(coalesce(notes, ''), 60) AS notes_excerpt
  FROM trading_trades
 WHERE status = 'open'
 ORDER BY entry_date DESC
 LIMIT 30;
"@

# 2. Stale-open trades — exit_monitor likely missing them
PSQL "2. Stale-open trades (>24h with status=open)" @"
SELECT id, ticker, broker_source, direction, quantity, entry_price,
       extract(epoch from (now() - entry_date))/3600.0 AS age_hours,
       last_fill_at, status
  FROM trading_trades
 WHERE status = 'open'
   AND entry_date < now() - interval '24 hours'
 ORDER BY entry_date ASC
 LIMIT 20;
"@

# 3. Recent closes (last 60 min) — exit_monitor working?
PSQL "3. Recent closes (last 60 min)" @"
SELECT id, ticker, broker_source, direction, exit_price, exit_date,
       coalesce(exit_reason, '?') AS exit_reason,
       extract(epoch from (now() - exit_date))/60.0 AS minutes_ago,
       pnl
  FROM trading_trades
 WHERE exit_date IS NOT NULL
   AND exit_date > now() - interval '60 minutes'
 ORDER BY exit_date DESC
 LIMIT 20;
"@

# 4. Crypto exit_monitor cycles in last 15 min
GREP-LOGS "4. crypto exit_monitor cycles (autotrader-worker, 15m)" `
    "chili-home-copilot-autotrader-worker-1" `
    "crypto_exit|crypto.exit_monitor" `
    20 "15m"

# 5. Equity exit_monitor cycles
GREP-LOGS "5. equity exit_monitor (autotrader-worker, 15m)" `
    "chili-home-copilot-autotrader-worker-1" `
    "equity_exit|exit_monitor.*equity" `
    15 "15m"

# 6. Errors/warnings in autotrader-worker
GREP-LOGS "6. autotrader-worker WARN/ERROR (15m)" `
    "chili-home-copilot-autotrader-worker-1" `
    "WARNING|ERROR|CRITICAL|Traceback" `
    25 "15m"

# 7. Errors/warnings in broker-sync-worker
GREP-LOGS "7. broker-sync-worker WARN/ERROR (15m)" `
    "chili-home-copilot-broker-sync-worker-1" `
    "WARNING|ERROR|CRITICAL|Traceback" `
    25 "15m"

# 8. Recent autotrader decisions
PSQL "8. Recent autotrader_runs decisions (last 30 min)" @"
SELECT id, ticker, decision, reason, created_at,
       breakout_alert_id, trade_id
  FROM trading_autotrader_runs
 WHERE created_at > now() - interval '30 minutes'
 ORDER BY created_at DESC
 LIMIT 20;
"@

# 9. Bracket intents for currently-open trades — UNPROTECTED check
PSQL "9. Bracket intents for currently-open trades (UNPROTECTED check)" @"
SELECT t.id AS trade_id, t.ticker, t.broker_source,
       bi.id AS bi_id, bi.intent_state, bi.shadow_mode,
       bi.target_price, bi.stop_price,
       bi.broker_stop_order_id, bi.broker_target_order_id,
       bi.last_observed_at,
       LEFT(coalesce(bi.last_diff_reason, ''), 60) AS last_diff_reason,
       extract(epoch from (now() - t.entry_date))/60.0 AS trade_age_min
  FROM trading_trades t
  LEFT JOIN trading_bracket_intents bi ON bi.trade_id = t.id
 WHERE t.status = 'open'
 ORDER BY t.entry_date DESC
 LIMIT 25;
"@

# 10. Crypto position-gone events (the wipeout-detection family)
PSQL "10. broker_reconcile_position_gone trades (last 60 min)" @"
SELECT id, ticker, broker_source, status, exit_reason, exit_price, exit_date
  FROM trading_trades
 WHERE exit_reason ILIKE '%reconcile_position_gone%'
   AND exit_date > now() - interval '60 minutes'
 ORDER BY exit_date DESC
 LIMIT 10;
"@

"================================================================" | Add-Content $out
"# autotrader-health v2 complete $(Get-Date -Format o)" | Add-Content $out
"================================================================" | Add-Content $out
