$out = "scripts/dispatch-f3-deploy-output.txt"
"# F-3 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add F-3 files" {
    git add `
      app/migrations.py `
      app/services/trading_scheduler.py `
      scripts/_commit_msg_f3.txt `
      scripts/dispatch-f3-deploy.ps1 `
      scripts/dispatch-trigger-ledger.ps1 `
      scripts/dispatch-macro-regime-trigger.ps1
    git status -s | Select-Object -First 15
}

S "git commit" {
    git commit -F scripts/_commit_msg_f3.txt
}

S "recreate chili (runs migrations on startup) + scheduler-worker (re-registers cron)" {
    docker compose up -d --force-recreate chili scheduler-worker
}

S "wait + verify mig 209" {
    Start-Sleep -Seconds 30
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id, applied_at FROM schema_version WHERE version_id LIKE '209%';"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT column_name, character_maximum_length FROM information_schema.columns WHERE table_name='trading_macro_regime_snapshots' AND column_name IN ('credit_regime','rates_regime','usd_regime') ORDER BY column_name;"
}

S "trigger macro snapshot now" {
    docker compose exec -T chili python -c "from app.db import SessionLocal; from app.services.trading.macro_regime_service import compute_and_persist; db=SessionLocal(); r=compute_and_persist(db); print('regime_id=', r.regime_id if r else None, 'credit_regime=', getattr(r,'credit_regime',None), 'rates_regime=', getattr(r,'rates_regime',None)); db.close()" 2>&1
}

S "verify fresh snapshot" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT as_of_date, regime_id, credit_regime, rates_regime, usd_regime, mode, computed_at FROM trading_macro_regime_snapshots ORDER BY computed_at DESC LIMIT 3;"
}

S "git push" {
    git push origin main
}

Write-Host "done"
