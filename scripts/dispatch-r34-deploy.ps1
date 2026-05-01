$out = "scripts/dispatch-r34-deploy-output.txt"
"# r34 indicator-key bridge deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "py-compile" {
    conda run -n chili-env python -m py_compile app/services/trading/pattern_imminent_alerts.py
    if ($LASTEXITCODE -ne 0) { "imminent FAILED"; return }
    conda run -n chili-env python -m py_compile app/services/trading/scanner.py
    if ($LASTEXITCODE -ne 0) { "scanner FAILED"; return }
    "OK"
}

S "git diff stat" {
    git diff --stat app/services/trading/pattern_imminent_alerts.py app/services/trading/scanner.py
}

S "force-recreate workers" {
    docker compose up -d --force-recreate scheduler-worker autotrader-worker brain-worker chili
}

S "wait 15s + container health" {
    Start-Sleep -Seconds 15
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}" | Select-String "scheduler|autotrader|brain|chili-1"
}

S "startup error scan" {
    docker compose logs --since 60s scheduler-worker autotrader-worker brain-worker chili 2>&1 | Select-String "ERROR|Traceback|ImportError|SyntaxError" | Select-Object -Last 15
}

S "verify R34 markers in scheduler-worker" {
    docker compose exec -T scheduler-worker python -c "import inspect; from app.services.trading.pattern_imminent_alerts import flat_indicators_from_score; src = inspect.getsource(flat_indicators_from_score); print('R34 imminent markers:'); print('  volume_ratio alias:', 'flat[\"volume_ratio\"]' in src); print('  gap_pct fallback:', 'gap_pct' in src and 'score.get(\"gap_pct\")' in src)"
    docker compose exec -T scheduler-worker python -c "import inspect; from app.services.trading.scanner import _score_ticker_impl; src = inspect.getsource(_score_ticker_impl); print('R34 scanner markers:'); print('  volume_ratio in indicators:', '\"volume_ratio\":' in src); print('  gap_pct in indicators:', '\"gap_pct\":' in src)"
}

S "wait for next pattern_imminent_scanner job (runs every ~5min) and inspect skip_reasons" {
    Start-Sleep -Seconds 60
    docker compose logs --since 90s scheduler-worker 2>&1 | Select-String "pattern_imminent_scanner|pattern_imminent" | Select-Object -Last 30
}

S "remove stale .git/index.lock" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/services/trading/pattern_imminent_alerts.py `
        app/services/trading/scanner.py `
        scripts/dispatch-r34-deploy.ps1 `
        scripts/dispatch-net-probe.ps1 `
        scripts/dispatch-net-probe-output.txt `
        scripts/dispatch-post-egress-pulse.ps1 `
        scripts/dispatch-post-egress-pulse-output.txt
    "git add complete"
}

S "git commit" {
    git commit -m "fix(r34): wire volume_ratio + gap_pct into flat_indicators_from_score and daily _score_ticker_impl indicators dict (post-egress pulse showed every crypto candidate suppressed as readiness_unusable / missing_indicators=[volume_ratio,gap_pct] despite scanner producing the underlying numbers)"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "git push origin main" { git push origin main }

Write-Host "r34 deploy done -- see $out"
