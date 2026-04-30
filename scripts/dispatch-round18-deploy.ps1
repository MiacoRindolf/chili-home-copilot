$out = "scripts/dispatch-round18-deploy-output.txt"
"# round-18 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add scripts/brain_worker.py scripts/_commit_msg_round18.txt scripts/dispatch-round18-deploy.ps1
    git commit -F scripts/_commit_msg_round18.txt
}

S "before: learning_cycle row count in brain_batch_jobs (last 24h)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM brain_batch_jobs WHERE job_type='learning_cycle' AND started_at > NOW() - INTERVAL '24 hours';"
}

S "recreate brain-worker (script change requires container restart)" {
    docker compose up -d --force-recreate brain-worker
}

S "wait 90s for first cycle to begin" {
    Start-Sleep -Seconds 90
    "ok"
}

S "after: learning_cycle row count in brain_batch_jobs (last 5min)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM brain_batch_jobs WHERE job_type='learning_cycle' AND started_at > NOW() - INTERVAL '5 minutes';"
}

S "after: latest learning_cycle row" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, status, started_at, ended_at, COALESCE(error_message, '') AS err, meta_json FROM brain_batch_jobs WHERE job_type='learning_cycle' ORDER BY started_at DESC LIMIT 1;"
}

S "verify no audit-helper failures in brain-worker logs" {
    docker compose logs --since 5m brain-worker 2>$null | Select-String -Pattern "learning_cycle audit (begin|finish) failed" | Measure-Object | Select-Object -ExpandProperty Count
}

S "git push" {
    git push origin main
}

Write-Host "done"
