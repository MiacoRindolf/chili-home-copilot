$out = "scripts/dispatch-round13-deploy-output.txt"
"# round-13 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add `
      app/services/trading/learning.py `
      app/services/trading/crypto/exit_monitor.py `
      scripts/_commit_msg_round13.txt `
      scripts/dispatch-round13-deploy.ps1
    git commit -F scripts/_commit_msg_round13.txt
}

S "recreate scheduler-worker (queue worker) + broker-sync-worker (crypto exit monitor)" {
    docker compose up -d --force-recreate scheduler-worker broker-sync-worker
}

S "wait + verify" {
    Start-Sleep -Seconds 25
    docker ps --format "table {{.Names}}`t{{.Status}}" | Select-String -Pattern "scheduler|broker-sync"
}

S "git push" {
    git push origin main
}

Write-Host "done"
