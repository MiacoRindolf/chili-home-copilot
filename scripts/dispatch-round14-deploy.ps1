$out = "scripts/dispatch-round14-deploy-output.txt"
"# round-14 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add app/services/trading/stop_engine.py scripts/_commit_msg_round14.txt scripts/dispatch-round14-deploy.ps1
    git commit -F scripts/_commit_msg_round14.txt
}

S "recreate broker-sync-worker (where stop_engine evaluates) + autotrader-worker" {
    docker compose up -d --force-recreate broker-sync-worker autotrader-worker
}

S "wait + verify" {
    Start-Sleep -Seconds 20
    docker ps --format "table {{.Names}}`t{{.Status}}" | Select-String -Pattern "broker-sync|autotrader"
}

S "git push" {
    git push origin main
}

Write-Host "done"
