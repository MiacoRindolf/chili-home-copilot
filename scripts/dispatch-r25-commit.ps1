$out = "scripts/dispatch-r25-commit-output.txt"
"# r25 commit reconciler fix $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale .git/index.lock if present" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/services/trading/brain_batch_reconciler.py `
        scripts/dispatch-r23-pulse-and-survey.ps1 `
        scripts/dispatch-r23-pulse-and-survey-output.txt `
        scripts/dispatch-r25-bbj-diag.ps1 `
        scripts/dispatch-r25-bbj-diag-output.txt `
        scripts/dispatch-r25-reconciler-fix.ps1 `
        scripts/dispatch-r25-reconciler-fix-output.txt `
        scripts/dispatch-r25-commit.ps1
    "git add complete"
}

S "git commit" {
    git commit -m "fix(r25): brain_batch_reconciler colon-in-literal SQLAlchemy bind-param parser bug"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "live: brain_batch_jobs status distribution last 24h (post-fix)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT status, COUNT(*) FROM brain_batch_jobs WHERE started_at > NOW() - INTERVAL '24 hours' GROUP BY status ORDER BY count DESC;"
}

S "live: scheduler-worker brain_batch_reconciler activity in last 5 min" {
    docker compose logs --since 5m scheduler-worker 2>&1 | Select-String -Pattern "brain_batch_reconciler" | Select-Object -Last 10
}

S "git push origin main" { git push origin main }

Write-Host "r25 commit done -- see $out"
