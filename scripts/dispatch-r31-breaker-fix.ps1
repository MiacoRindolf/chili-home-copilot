$out = "scripts/dispatch-r31-breaker-fix-output.txt"
"# r31 breaker fix (consecutive-loss rule) $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "apply edit (host git)" {
    conda run -n chili-env python scripts/_r31_apply_breaker_fix.py
}

S "py-compile" {
    conda run -n chili-env python -m py_compile app/services/trading/portfolio_risk.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git diff stat" {
    git diff --stat app/services/trading/portfolio_risk.py
}

S "force-recreate workers that read the breaker" {
    docker compose up -d --force-recreate broker-sync-worker autotrader-worker chili
}

S "wait 12s + container health" {
    Start-Sleep -Seconds 12
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}"
}

S "startup error scan" {
    docker compose logs --since 30s broker-sync-worker autotrader-worker chili 2>&1 | Select-String "ERROR|Traceback|ImportError|SyntaxError" | Select-Object -Last 10
}

S "verify breaker source loads with new logic" {
    docker compose exec -T autotrader-worker python -c "import inspect; from app.services.trading.portfolio_risk import check_drawdown_breaker; src = inspect.getsource(check_drawdown_breaker); print('R31 markers present:'); print('  SYNTHETIC_EXIT_REASONS:', 'SYNTHETIC_EXIT_REASONS' in src); print('  min_streak_loss_pct:', 'brain_risk_min_streak_loss_pct' in src); print('  magnitude floor logic:', 'min_loss_dollars' in src)"
}

S "trigger a recheck of the breaker (dry-run; persists state if it changes)" {
    docker compose exec -T autotrader-worker python -c "from app.db import SessionLocal; from app.services.trading.portfolio_risk import check_drawdown_breaker; db = SessionLocal(); print(check_drawdown_breaker(db, user_id=1, capital=25000.0)); db.close()"
}

S "current breaker state" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, snapshot_date, breaker_tripped, breaker_reason, regime, created_at FROM trading_risk_state ORDER BY id DESC LIMIT 5;"
}

S "remove stale .git/index.lock" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/services/trading/portfolio_risk.py `
        scripts/_r31_apply_breaker_fix.py `
        scripts/dispatch-r31-breaker-fix.ps1 `
        scripts/dispatch-r30-cleanup.ps1 `
        scripts/dispatch-r30-cleanup-output.txt `
        scripts/dispatch-r30-verify.ps1 `
        scripts/dispatch-r30-verify-output.txt `
        scripts/_r30_cleanup_apply.py `
        scripts/dispatch-redundancy-impact-2.ps1 `
        scripts/dispatch-redundancy-impact-2-output.txt
    "git add complete"
}

S "git commit" {
    git commit -m "fix(r31): breaker consecutive-loss rule excludes synthetic reconcile exits + adds 1pct-of-capital magnitude floor (false trip on 5 micro-losses no longer fires)"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "git push origin main" { git push origin main }

Write-Host "r31 done -- see $out"
