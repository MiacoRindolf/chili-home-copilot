$out = "scripts/dispatch-r34-verify-output.txt"
"# r34 verify - did readiness_unusable drop? $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "wait 60s for the next imminent run to complete" {
    Start-Sleep -Seconds 60
    "done sleeping"
}

S "skip_reasons from the most recent pattern_imminent run (look for readiness_unusable count)" {
    docker compose logs --since 8m scheduler-worker 2>&1 | Select-String "skip_reasons|candidates.: " | Select-Object -Last 10
}

S "autotrader candidate_pool last 2min (any change from 0?)" {
    docker compose logs --since 2m autotrader-worker 2>&1 | Select-String "candidate_pool|tick uid" | Select-Object -Last 5
}

S "any breakout_alert pattern_imminent rows since R34?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT alert_tier, COUNT(*) AS n, MAX(alerted_at) AS most_recent FROM trading_breakout_alerts WHERE alerted_at > NOW() - INTERVAL '15 minutes' GROUP BY alert_tier;"
}

Write-Host "r34 verify done -- see $out"
