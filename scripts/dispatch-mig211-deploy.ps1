$out = "scripts/dispatch-mig211-deploy-output.txt"
"# mig 211 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add app/migrations.py scripts/_commit_msg_mig211.txt scripts/dispatch-mig211-deploy.ps1
    git commit -F scripts/_commit_msg_mig211.txt
}

S "before count" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FILTER (WHERE avg_return_pct IS NULL) AS arp_null FROM scan_patterns;"
}

S "recreate chili (runs mig 211 on startup)" {
    docker compose up -d --force-recreate chili
}

S "wait + verify mig 211 + backfill" {
    Start-Sleep -Seconds 25
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id FROM schema_version WHERE version_id LIKE '211%';"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FILTER (WHERE avg_return_pct IS NULL) AS arp_null_after FROM scan_patterns;"
}

S "git push" {
    git push origin main
}

Write-Host "done"
