$out = "scripts/dispatch-commit-retry-output.txt"
"# Commit retry $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") {
        Remove-Item -Force ".git/index.lock"
        "removed .git/index.lock"
    } else {
        "no lock to remove"
    }
}

S "git add" {
    git add `
      app/migrations.py `
      app/services/broker_service.py `
      app/services/trading/auto_trader.py `
      app/services/trading/pdt_guard.py `
      app/services/trading/dynamic_priors.py `
      app/services/trading/realized_ev_demote_pass.py `
      app/services/trading/ai_context.py `
      app/services/trading/alpha_decay.py `
      app/services/trading/learning_predictions.py `
      app/services/trading/live_drift.py `
      app/services/trading/contracts/signal_emit.py `
      app/services/trading/backtest_queue_worker.py `
      app/services/trading/portfolio_risk.py `
      app/services/trading_scheduler.py `
      app/services/trading/brain_batch_reconciler.py `
      docs/AUDITS/2026-04-29.md `
      scripts/_commit_msg_audit_fixes.txt `
      scripts/dispatch-audit-2026-04-29-fixes-deploy.ps1 `
      scripts/dispatch-audit-2026-04-29.ps1 `
      scripts/dispatch-mig208-manual.ps1 `
      scripts/dispatch-pause-and-index.ps1 `
      scripts/dispatch-final-cleanup.ps1 `
      scripts/dispatch-commit-audit-fixes.ps1 `
      scripts/dispatch-commit-retry.ps1 `
      scripts/dispatch-verify-2026-04-29.ps1
    git status -s | Select-Object -First 30
}

S "git commit" {
    git commit -F scripts/_commit_msg_audit_fixes.txt
}

S "git log" {
    git log -3 --oneline
}

S "git push" {
    git push origin main
}

Write-Host "done"
