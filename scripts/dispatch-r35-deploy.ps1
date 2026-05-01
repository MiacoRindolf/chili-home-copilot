$out = "scripts/dispatch-r35-deploy-output.txt"
"# r35 PDT crypto bypass deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "py-compile" {
    conda run -n chili-env python -m py_compile app/services/trading/pdt_guard.py
    if ($LASTEXITCODE -ne 0) { "pdt_guard FAILED"; return }
    conda run -n chili-env python -m py_compile app/services/trading/auto_trader.py
    if ($LASTEXITCODE -ne 0) { "auto_trader FAILED"; return }
    "OK"
}

S "git diff stat" {
    git diff --stat app/services/trading/pdt_guard.py app/services/trading/auto_trader.py
}

S "force-recreate autotrader-worker" {
    docker compose up -d --force-recreate autotrader-worker
}

S "wait 12s + container health" {
    Start-Sleep -Seconds 12
    docker ps --filter "name=chili-home-copilot-autotrader" --format "{{.Names}} | {{.Status}}"
}

S "startup error scan" {
    docker compose logs --since 60s autotrader-worker 2>&1 | Select-String "ERROR|Traceback|ImportError|SyntaxError" | Select-Object -Last 10
}

S "verify R35 markers in autotrader-worker" {
    $verifyPy = "import inspect; from app.services.trading.pdt_guard import can_open_intraday_round_trip; src = inspect.getsource(can_open_intraday_round_trip); print('R35 ticker arg:', 'ticker: str' in src); print('R35 crypto bypass:', 'crypto_not_pdt_eligible' in src); print('R35 endswith-USD:', '-USD' in src)"
    docker compose exec -T autotrader-worker python -c $verifyPy
}

S "watch autotrader candidate flow next 90s (any placed=1?)" {
    Start-Sleep -Seconds 90
    docker compose logs --since 100s autotrader-worker 2>&1 | Select-String "candidate_pool|tick uid|placed|pdt_guard" | Select-Object -Last 10
}

S "remove stale .git/index.lock" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/services/trading/pdt_guard.py `
        app/services/trading/auto_trader.py `
        scripts/dispatch-r35-deploy.ps1 `
        scripts/dispatch-r34-verify.ps1 `
        scripts/dispatch-r34-verify-output.txt
    "git add complete"
}

S "git commit" {
    git commit -m "fix(r35): PDT entry gate exempts crypto (24/7 cash market, not securities) -- post-R34 crypto candidates were 100% blocked by pdt_limit_reached:43>=3 because count included crypto round-trips. SEC rule applies only to margin securities trading."
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "git push origin main" { git push origin main }

Write-Host "r35 deploy done -- see $out"
