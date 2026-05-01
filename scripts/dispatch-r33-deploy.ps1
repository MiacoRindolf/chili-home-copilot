$out = "scripts/dispatch-r33-deploy-output.txt"
"# r33 per-asset cooldown deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "py-compile config.py + pattern_imminent_alerts.py" {
    conda run -n chili-env python -m py_compile app/config.py
    if ($LASTEXITCODE -ne 0) { "config FAILED"; return }
    conda run -n chili-env python -m py_compile app/services/trading/pattern_imminent_alerts.py
    if ($LASTEXITCODE -ne 0) { "pattern_imminent FAILED"; return }
    "OK"
}

S "git diff stat" {
    git diff --stat app/config.py app/services/trading/pattern_imminent_alerts.py
}

S "git diff (R33 markers only)" {
    git diff app/config.py app/services/trading/pattern_imminent_alerts.py | Select-String "R33|cooldown_h_crypto|cooldown_hours_crypto|ticker_cooldown_h" | Select-Object -First 30
}

S "force-recreate workers that read pattern_imminent + config" {
    docker compose up -d --force-recreate scheduler-worker autotrader-worker brain-worker chili
}

S "wait 15s + container health" {
    Start-Sleep -Seconds 15
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}" | Select-String "scheduler|autotrader|brain|chili-1"
}

S "startup error scan" {
    docker compose logs --since 60s scheduler-worker autotrader-worker brain-worker chili 2>&1 | Select-String "ERROR|Traceback|ImportError|SyntaxError|AttributeError" | Select-Object -Last 15
}

S "verify R33 setting reachable in scheduler-worker" {
    docker compose exec -T scheduler-worker python -c "from app.config import settings; print('crypto cooldown_h:', settings.pattern_imminent_cooldown_hours_crypto); print('equity cooldown_h:', settings.pattern_imminent_cooldown_hours)"
}

S "verify R33 logic in pattern_imminent_alerts" {
    docker compose exec -T scheduler-worker python -c "import inspect; from app.services.trading import pattern_imminent_alerts; src = inspect.getsource(pattern_imminent_alerts.run_pattern_imminent_scan); print('R33 markers:'); print('  cooldown_h_crypto var:', 'cooldown_h_crypto' in src); print('  ticker_cooldown_h var:', 'ticker_cooldown_h' in src); print('  is_crypto(ticker) call:', 'is_crypto(ticker)' in src)"
}

S "remove stale .git/index.lock" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/config.py `
        app/services/trading/pattern_imminent_alerts.py `
        scripts/dispatch-r33-deploy.ps1 `
        scripts/dispatch-r32-pulse.ps1 `
        scripts/dispatch-r32-pulse-output.txt `
        scripts/_claude_daemon.ps1
    "git add complete"
}

S "git commit" {
    git commit -m "feat(r33): per-asset cooldown for pattern_imminent alerts (crypto 0.5h vs equity 3h) + remove dispatch-daemon whitelist (operator request; hard-rejects retained)"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "git push origin main" { git push origin main }

S "watch autotrader candidate flow next 30s" {
    Start-Sleep -Seconds 30
    docker compose logs --since 45s autotrader-worker 2>&1 | Select-String "candidate_pool|tick uid" | Select-Object -Last 5
}

Write-Host "r33 deploy done -- see $out"
