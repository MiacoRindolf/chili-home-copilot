# Follow-up probe after recovery confirmed postgres healthy + intents being
# created. Goals:
#   A. Show bracket_intent rows for the 9 open Coinbase trades (correct schema)
#   B. Show actual columns on trading_bracket_intents (so future probes use
#      the right names)
#   C. BRAIN_LIVE_BRACKETS_MODE in autotrader-worker (single-quote-safe sh)
#   D. Recent bracket_intent_writer events (last 10 min)
#   E. Recent bracket_writer_g2 place_missing_stop events for Coinbase
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-bracket-intent-followup-out.txt"
"# dispatch-bracket-intent-followup $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"## A. bracket_intent rows for the 9 open Coinbase trades" | Add-Content $out
$tmp = [System.IO.Path]::GetTempFileName()
@"
SELECT t.id AS trade_id, t.ticker, t.stop_loss,
       bi.id AS intent_id, bi.intent_state, bi.broker_stop_order_id,
       bi.created_at, bi.last_observed_at
  FROM trading_trades t
  LEFT JOIN trading_bracket_intents bi ON bi.trade_id = t.id
 WHERE t.status = 'open' AND t.broker_source = 'coinbase'
 ORDER BY t.entry_date DESC;
"@ | Out-File $tmp -Encoding ascii
& docker cp $tmp chili-home-copilot-postgres-1:/tmp/qa.sql 2>&1 | Out-Null
& docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -f /tmp/qa.sql 2>&1 | Out-String | Add-Content $out
Remove-Item $tmp -ErrorAction SilentlyContinue
"" | Add-Content $out

"## B. trading_bracket_intents schema (\\d)" | Add-Content $out
& docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -c "\d trading_bracket_intents" 2>&1 | Out-String | Add-Content $out
"" | Add-Content $out

"## C. BRAIN_LIVE_BRACKETS_MODE in autotrader-worker" | Add-Content $out
& docker exec chili-home-copilot-autotrader-worker-1 sh -c "env | grep BRAIN_LIVE_BRACKETS" 2>&1 | Out-String | Add-Content $out
& docker exec chili-home-copilot-autotrader-worker-1 sh -c "env | grep BRACKET" 2>&1 | Out-String | Add-Content $out
"" | Add-Content $out

"## D. last 15 bracket_intent_writer events (broker-sync-worker, 10m)" | Add-Content $out
$logs = & docker logs --since 10m chili-home-copilot-broker-sync-worker-1 2>&1
$intents = $logs | Select-String -Pattern "bracket_intent_ops.*event=intent_write" -CaseSensitive:$false
if ($intents) {
    "match count: $($intents.Count)" | Add-Content $out
    $intents | Select-Object -Last 15 | Out-String | Add-Content $out
} else {
    "no intent_write events in last 10 min" | Add-Content $out
}
"" | Add-Content $out

"## E. bracket_writer_g2 place_missing_stop events for Coinbase trades, 10m" | Add-Content $out
$g2 = $logs | Select-String -Pattern "bracket_writer_g2.*place_missing_stop" -CaseSensitive:$false
if ($g2) {
    "match count: $($g2.Count) (last 10)" | Add-Content $out
    $g2 | Select-Object -Last 10 | Out-String | Add-Content $out
} else {
    "no place_missing_stop logs in last 10 min" | Add-Content $out
}
"" | Add-Content $out

"## F. autotrader-worker stop_engine logs, 10m" | Add-Content $out
$logs2 = & docker logs --since 10m chili-home-copilot-autotrader-worker-1 2>&1
$se = $logs2 | Select-String -Pattern "stop_engine|bracket_intent|brain_live_brackets" -CaseSensitive:$false
if ($se) {
    "match count: $($se.Count) (last 10)" | Add-Content $out
    $se | Select-Object -Last 10 | Out-String | Add-Content $out
} else {
    "no stop_engine activity in autotrader-worker last 10 min" | Add-Content $out
}

"# end" | Add-Content $out
Write-Host "done -- $out"
