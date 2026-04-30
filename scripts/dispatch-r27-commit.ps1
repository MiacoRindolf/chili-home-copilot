$out = "scripts/dispatch-r27-commit-output.txt"
"# r27 commit + push $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale .git/index.lock if present" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/services/trading/execution_audit.py `
        .env `
        scripts/_r27_apply_terminal_guard.py `
        scripts/dispatch-r23-regression-and-breaker.ps1 `
        scripts/dispatch-r23-regression-and-breaker-output.txt `
        scripts/dispatch-r23-adt-reconcile.ps1 `
        scripts/dispatch-r23-adt-reconcile-output.txt `
        scripts/dispatch-phase0-phase1-flag1-activate.ps1 `
        scripts/dispatch-phase0-phase1-flag1-activate-output.txt `
        scripts/dispatch-r27-apply.ps1 `
        scripts/dispatch-r27-apply-output.txt `
        scripts/dispatch-r27-commit.ps1 `
        docs/AUDITS/2026-04-30-third-party-response.md
    "git add complete"
}

S "git status post-add" { git status -s | Select-Object -First 30 }

S "git commit" {
    git commit -m "fix(r27): apply_execution_event_to_trade respects terminal trade states + activate Phase 1 Flag 1 (pattern_survival_classifier) + audit response doc"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "git push origin main" { git push origin main }

Write-Host "r27 commit done -- see $out"
