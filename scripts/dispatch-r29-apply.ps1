$out = "scripts/dispatch-r29-apply-output.txt"
"# r29 apply tca call + commit + push $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "apply edit (host git)" {
    conda run -n chili-env python scripts/_r29_apply_tca_call.py
}

S "py-compile robinhood_exit_execution + tca_service" {
    conda run -n chili-env python -m py_compile app/services/trading/robinhood_exit_execution.py app/services/trading/tca_service.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git diff stat" {
    git diff --stat app/services/trading/robinhood_exit_execution.py
}

S "force-recreate workers that call _finalize_filled_exit" {
    docker compose up -d --force-recreate broker-sync-worker autotrader-worker chili
}

S "wait 12s + container health" {
    Start-Sleep -Seconds 12
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}"
}

S "startup error scan" {
    docker compose logs --since 30s broker-sync-worker autotrader-worker chili 2>&1 | Select-String "ERROR|Traceback|ImportError|SyntaxError" | Select-Object -Last 15
}

S "verify call site is loaded in container" {
    docker compose exec -T autotrader-worker python -c "import inspect; from app.services.trading.robinhood_exit_execution import _finalize_filled_exit; src = inspect.getsource(_finalize_filled_exit); print('R29 wired:', 'apply_tca_on_trade_close' in src)"
}

S "remove stale .git/index.lock" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/services/trading/robinhood_exit_execution.py `
        scripts/_r29_apply_tca_call.py `
        scripts/dispatch-r29-apply.ps1 `
        scripts/dispatch-phase-h-flag2-probe.ps1 `
        scripts/dispatch-phase-h-flag2-probe-output.txt `
        scripts/dispatch-flag2-pulse.ps1 `
        scripts/dispatch-flag2-pulse-output.txt `
        docs/AUDITS/2026-04-30-third-party-response.md
    "git add complete"
}

S "git commit" {
    git commit -m "fix(r29): wire apply_tca_on_trade_close into _finalize_filled_exit so legitimate exits compute slippage from decision-time reference"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -3" { git log --oneline -3 }

S "git push origin main" { git push origin main }

Write-Host "r29 done -- see $out"
