$out = "scripts/dispatch-round19-20-deploy-output.txt"
"# round-19-20 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "git add + commit" {
    git add docker-compose.yml app/services/trading/robinhood_exit_execution.py scripts/_commit_msg_round19_20.txt scripts/dispatch-round19-20-deploy.ps1
    git commit -F scripts/_commit_msg_round19_20.txt
}

S "before: UnboundLocalError count last 30min in autotrader-worker" {
    docker compose logs --since 30m autotrader-worker 2>$null | Select-String -Pattern "UnboundLocalError" | Measure-Object | Select-Object -ExpandProperty Count
}

S "before: monitor_exit_rejected:Sell.*PDT count last 30min" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '30 minutes' AND decision='monitor_exit_rejected' AND reason ILIKE '%PDT designation%';"
}

S "force-recreate all script-running workers (picks up new mount + R20 fix)" {
    docker compose up -d --force-recreate brain-worker scheduler-worker autotrader-worker broker-sync-worker
}

S "wait 15s for containers to settle" {
    Start-Sleep -Seconds 15
    "ok"
}

S "verify autotrader-worker has the R20 fix loaded" {
    docker compose exec -T autotrader-worker grep -c '__import__."logging".' /app/app/services/trading/robinhood_exit_execution.py
}

S "verify autotrader-worker has scripts mount working" {
    docker compose exec -T autotrader-worker wc -l /app/scripts/scheduler_worker.py
}

S "wait 90s for monitor cycles to fire (autotrader tick is 10s, monitor is 30s)" {
    Start-Sleep -Seconds 90
    "ok"
}

S "after: UnboundLocalError count last 2min" {
    docker compose logs --since 2m autotrader-worker 2>$null | Select-String -Pattern "UnboundLocalError" | Measure-Object | Select-Object -ExpandProperty Count
}

S "after: monitor_exit_rejected:Sell.*PDT count last 2min (should drop sharply)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '2 minutes' AND decision='monitor_exit_rejected' AND reason ILIKE '%PDT designation%';"
}

S "after: monitor_exit_filled count last 2min (real exits should be possible now)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS filled, decision, reason FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '2 minutes' AND decision LIKE 'monitor_exit_%' GROUP BY decision, reason ORDER BY filled DESC LIMIT 10;"
}

S "container line counts (should all show R19 mount working)" {
    "brain-worker:"; docker compose exec -T brain-worker test -f /app/scripts/brain_worker.py; if ($LASTEXITCODE -eq 0) { "  scripts mount OK" } else { "  scripts mount BROKEN" }
    "scheduler-worker:"; docker compose exec -T scheduler-worker test -f /app/scripts/scheduler_worker.py; if ($LASTEXITCODE -eq 0) { "  scripts mount OK" } else { "  scripts mount BROKEN" }
    "autotrader-worker:"; docker compose exec -T autotrader-worker test -f /app/scripts/scheduler_worker.py; if ($LASTEXITCODE -eq 0) { "  scripts mount OK" } else { "  scripts mount BROKEN" }
    "broker-sync-worker:"; docker compose exec -T broker-sync-worker test -f /app/scripts/scheduler_worker.py; if ($LASTEXITCODE -eq 0) { "  scripts mount OK" } else { "  scripts mount BROKEN" }
}

S "git push" {
    git push origin main
}

Write-Host "done"
