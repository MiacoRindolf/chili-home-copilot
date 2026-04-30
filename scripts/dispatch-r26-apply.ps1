$out = "scripts/dispatch-r26-apply-output.txt"
"# r26 apply exit-defer-on-reject $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "apply edit via Python script (host git works)" {
    conda run -n chili-env python scripts/_r26_apply_exit_defer.py
}

S "py-compile" {
    conda run -n chili-env python -m py_compile app/services/trading/robinhood_exit_execution.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git diff stat" {
    git diff --stat app/services/trading/robinhood_exit_execution.py
}

S "force-recreate autotrader-worker (where the exit submission runs)" {
    docker compose up -d --force-recreate autotrader-worker
}

S "wait 10s" { Start-Sleep -Seconds 10; "ok" }

S "autotrader-worker uptime" {
    docker ps --filter "name=chili-home-copilot-autotrader-worker-1" --format "{{.Names}} | {{.Status}}"
}

S "autotrader-worker startup errors?" {
    docker compose logs --since 30s autotrader-worker 2>&1 | Select-String "ERROR|Traceback|ImportError|SyntaxError" | Select-Object -Last 10
}

S "exit-rejected count BEFORE further sweeps (last 5 min)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT decision, reason, COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '5 minutes' AND decision LIKE 'monitor_exit%' GROUP BY decision, reason ORDER BY count DESC;"
}

Write-Host "r26 apply done -- see $out"
