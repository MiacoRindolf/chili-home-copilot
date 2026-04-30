$out = "scripts/dispatch-round16-deploy-output.txt"
"# round-16 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add app/services/trading/prescreen_job.py scripts/_commit_msg_round16.txt scripts/dispatch-round16-deploy.ps1
    git commit -F scripts/_commit_msg_round16.txt
}

S "before: brain-worker idle-tx count" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM pg_stat_activity WHERE application_name='chili-brain-worker' AND state='idle in transaction';"
}

S "recreate brain-worker" {
    docker compose up -d --force-recreate brain-worker
}

S "wait + verify (90s for cycle to engage)" {
    Start-Sleep -Seconds 90
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS idle_tx_after, COUNT(*) FILTER (WHERE NOW()-state_change > INTERVAL '60 seconds') AS gt_60s FROM pg_stat_activity WHERE application_name='chili-brain-worker' AND state='idle in transaction';"
}

S "git push" {
    git push origin main
}

Write-Host "done"
