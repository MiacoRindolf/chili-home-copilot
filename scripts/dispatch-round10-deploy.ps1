$out = "scripts/dispatch-round10-deploy-output.txt"
"# round-10 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add app/services/trading_scheduler.py scripts/_commit_msg_round10.txt scripts/dispatch-round10-deploy.ps1 scripts/_commit_msg_round9.txt scripts/dispatch-round9-deploy.ps1 scripts/dispatch-cron-check.ps1
    git commit -F scripts/_commit_msg_round10.txt
}

S "before count: distinct job_types in brain_batch_jobs (last 24h)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(DISTINCT job_type) AS distinct_jobs_24h FROM brain_batch_jobs WHERE started_at > NOW() - INTERVAL '24 hours';"
}

S "recreate scheduler-worker" {
    docker compose up -d --force-recreate scheduler-worker
}

S "wait for first cron tick to write a baseline row" {
    Start-Sleep -Seconds 75
}

S "after count: distinct job_types in brain_batch_jobs (last 5min)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(DISTINCT job_type) AS distinct_jobs_5min FROM brain_batch_jobs WHERE started_at > NOW() - INTERVAL '5 minutes';"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT job_type, COUNT(*), status FROM brain_batch_jobs WHERE started_at > NOW() - INTERVAL '5 minutes' GROUP BY 1,3 ORDER BY 1,3;"
}

S "git push" {
    git push origin main
}

Write-Host "done"
