# Recover from postgres-unhealthy + validate bracket fix in one shot.
# Run directly: .\scripts\dispatch-recover-and-validate.ps1
# Output: scripts\dispatch-recover-and-validate-out.txt
#
# Designed to be daemon-free since the dev daemon is currently hanging.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-recover-and-validate-out.txt"
"# dispatch-recover-and-validate $(Get-Date -Format o)" | Out-File $out -Encoding utf8

# ============================================================================
# STAGE 1 -- Postgres unhealthy diagnosis
# ============================================================================
"## STAGE 1 -- postgres-1 health diagnosis" | Add-Content $out

"### docker ps (filter postgres)" | Add-Content $out
& docker ps -a --filter "name=postgres" --format 'table {{.Names}}	{{.Status}}	{{.Health}}' 2>&1 |
    Out-String | Add-Content $out

"### postgres logs (last 80 lines)" | Add-Content $out
& docker logs --tail 80 chili-home-copilot-postgres-1 2>&1 | Out-String | Add-Content $out

"### healthcheck inspect" | Add-Content $out
& docker inspect --format '{{json .State.Health}}' chili-home-copilot-postgres-1 2>&1 |
    Out-String | Add-Content $out

# Decide if postgres is healthy enough to proceed
$pgHealth = & docker inspect --format '{{.State.Health.Status}}' chili-home-copilot-postgres-1 2>&1
"### resolved health: $pgHealth" | Add-Content $out
"" | Add-Content $out

# ============================================================================
# STAGE 2 -- If postgres is unhealthy, attempt safe restart (postgres only)
# ============================================================================
if ($pgHealth -notmatch 'healthy') {
    "## STAGE 2 -- postgres unhealthy, attempting safe restart" | Add-Content $out
    "### docker compose restart postgres" | Add-Content $out
    & docker compose restart postgres 2>&1 | Out-String | Add-Content $out

    # Wait up to 60s for healthy
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $h = & docker inspect --format '{{.State.Health.Status}}' chili-home-copilot-postgres-1 2>&1
        "  poll: $((Get-Date).ToString('HH:mm:ss')) -> $h" | Add-Content $out
        if ($h -match 'healthy') { break }
    }
    $pgHealth = & docker inspect --format '{{.State.Health.Status}}' chili-home-copilot-postgres-1 2>&1
    "### post-restart health: $pgHealth" | Add-Content $out
    "" | Add-Content $out
}

# ============================================================================
# STAGE 3 -- If healthy now, run bracket validation queries
# ============================================================================
if ($pgHealth -match 'healthy') {
    "## STAGE 3 -- bracket validation (postgres healthy, proceeding)" | Add-Content $out

    $tmp = [System.IO.Path]::GetTempFileName()
    @"
\echo === A. NAKED Coinbase trades (open + status) ===
SELECT id, ticker, stop_loss, broker_source, status, entry_date
  FROM trading_trades
 WHERE status = 'open' AND broker_source = 'coinbase'
 ORDER BY entry_date DESC;

\echo === B. bracket_intent rows for those trades ===
SELECT t.id AS trade_id, t.ticker, bi.intent_state, bi.broker_stop_order_id,
       bi.intent_mode, bi.last_observed_at, bi.created_at
  FROM trading_trades t
  LEFT JOIN trading_bracket_intents bi ON bi.trade_id = t.id
 WHERE t.status = 'open' AND t.broker_source = 'coinbase'
 ORDER BY t.entry_date DESC;

\echo === C. Recent bracket_intent activity (any source, last 30 min) ===
SELECT id, trade_id, ticker, intent_state, intent_mode, broker_stop_order_id, last_observed_at
  FROM trading_bracket_intents
 WHERE last_observed_at > NOW() - INTERVAL '30 minutes'
    OR created_at > NOW() - INTERVAL '30 minutes'
 ORDER BY COALESCE(last_observed_at, created_at) DESC
 LIMIT 30;

\echo === D. Count of bracket intents by state ===
SELECT intent_state, intent_mode, COUNT(*)
  FROM trading_bracket_intents
 GROUP BY intent_state, intent_mode
 ORDER BY COUNT(*) DESC;
"@ | Out-File $tmp -Encoding ascii

    & docker cp $tmp chili-home-copilot-postgres-1:/tmp/qbracket.sql 2>&1 | Out-Null
    & docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -f /tmp/qbracket.sql 2>&1 |
        Out-String | Add-Content $out
    Remove-Item $tmp -ErrorAction SilentlyContinue
    "" | Add-Content $out

    # ========================================================================
    # STAGE 4 -- Effective env in autotrader-worker (confirms fix propagated)
    # ========================================================================
    "## STAGE 4 -- BRAIN_LIVE_BRACKETS_MODE in autotrader-worker" | Add-Content $out
    & docker exec chili-home-copilot-autotrader-worker-1 sh -c 'env | grep -iE "BRAIN_LIVE_BRACKETS|BRACKET" | sort' 2>&1 |
        Out-String | Add-Content $out
    "" | Add-Content $out

    # ========================================================================
    # STAGE 5 -- bracket_reconciliation_service activity (last 30 min)
    # ========================================================================
    "## STAGE 5 -- broker-sync-worker bracket reconciliation logs (last 30m)" | Add-Content $out
    $logs = & docker logs --since 30m chili-home-copilot-broker-sync-worker-1 2>&1
    $matches = $logs | Select-String -Pattern "bracket_reconciliation|bracket_intent|missing_stop|new_intent" -CaseSensitive:$false
    if ($matches) {
        "match count: $($matches.Count) (last 25)" | Add-Content $out
        $matches | Select-Object -Last 25 | Out-String | Add-Content $out
    } else {
        "NO bracket reconciliation activity in last 30 min -- broker-sync-worker may not be running or env didn't propagate" | Add-Content $out
    }
    "" | Add-Content $out

    # ========================================================================
    # STAGE 6 -- stop_engine activity in autotrader-worker (last 30 min)
    # ========================================================================
    "## STAGE 6 -- autotrader-worker stop_engine + bracket emit logs (last 30m)" | Add-Content $out
    $logs2 = & docker logs --since 30m chili-home-copilot-autotrader-worker-1 2>&1
    $matches2 = $logs2 | Select-String -Pattern "stop_engine|bracket_intent|brain_live_brackets|upsert_bracket" -CaseSensitive:$false
    if ($matches2) {
        "match count: $($matches2.Count) (last 20)" | Add-Content $out
        $matches2 | Select-Object -Last 20 | Out-String | Add-Content $out
    } else {
        "NO stop_engine/bracket activity in autotrader-worker last 30 min" | Add-Content $out
    }
    "" | Add-Content $out
} else {
    "## STAGE 3-6 SKIPPED -- postgres still unhealthy ($pgHealth); operator must intervene" | Add-Content $out
    "" | Add-Content $out
}

# ============================================================================
# STAGE 7 -- Container lineup (which containers are running/restarting)
# ============================================================================
"## STAGE 7 -- container status overview" | Add-Content $out
& docker ps --format 'table {{.Names}}	{{.Status}}' --filter "name=chili-home-copilot" 2>&1 |
    Out-String | Add-Content $out
"" | Add-Content $out

# ============================================================================
# STAGE 8 -- Daemon hang evidence
# ============================================================================
"## STAGE 8 -- dev daemon last activity" | Add-Content $out
if (Test-Path "scripts/_claude_daemon.log") {
    "### last 30 lines of _claude_daemon.log" | Add-Content $out
    Get-Content "scripts/_claude_daemon.log" -Tail 30 | Out-String | Add-Content $out
} else {
    "no _claude_daemon.log present" | Add-Content $out
}
"" | Add-Content $out

"### pending file state" | Add-Content $out
if (Test-Path "scripts/_claude_pending.txt") {
    $pendInfo = Get-Item "scripts/_claude_pending.txt"
    "  exists: yes" | Add-Content $out
    "  modified: $($pendInfo.LastWriteTime)" | Add-Content $out
    "  size_bytes: $($pendInfo.Length)" | Add-Content $out
    "  age_sec: $([int]((Get-Date) - $pendInfo.LastWriteTime).TotalSeconds)" | Add-Content $out
    "  content (first 500 chars):" | Add-Content $out
    (Get-Content "scripts/_claude_pending.txt" -Raw -ErrorAction SilentlyContinue).Substring(0,
        [Math]::Min(500, (Get-Item "scripts/_claude_pending.txt").Length)) | Add-Content $out
} else {
    "  no pending file" | Add-Content $out
}
"" | Add-Content $out

"### live claude_daemon process" | Add-Content $out
$daemon = Get-Process powershell -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -match '_claude_daemon' -or $_.CommandLine -match '_claude_daemon' }
# CommandLine isn't always available; fall back to listing all powershell procs
if (-not $daemon) {
    "  CommandLine match unavailable; listing all powershell.exe processes:" | Add-Content $out
    Get-Process powershell -ErrorAction SilentlyContinue |
        Select-Object Id, StartTime, CPU, WorkingSet64 |
        Format-Table -AutoSize | Out-String | Add-Content $out
} else {
    $daemon | Select-Object Id, StartTime, CPU, WorkingSet64 | Format-Table -AutoSize |
        Out-String | Add-Content $out
}

"# end" | Add-Content $out
Write-Host "done -- output at $out"
