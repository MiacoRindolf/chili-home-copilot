# Deploy F-1: pattern_regime_ledger decoupling + config.py defaults.
$out = "scripts/dispatch-f1-deploy-output.txt"
"# F-1 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock if any" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add F-1 files" {
    git add `
      app/services/trading/pattern_regime_ledger.py `
      app/config.py `
      scripts/_commit_msg_f1.txt `
      scripts/dispatch-f1-deploy.ps1 `
      scripts/dispatch-regime-mode-probe.ps1
    git status -s | Select-Object -First 15
}

S "git commit" {
    git commit -F scripts/_commit_msg_f1.txt
}

S "recreate containers (bind mount picks up new code on restart)" {
    docker compose up -d --force-recreate chili brain-worker scheduler-worker
}

S "wait then verify" {
    Start-Sleep -Seconds 30
    docker ps --format "table {{.Names}}`t{{.Status}}" | Select-String -Pattern "chili|brain|scheduler"
}

S "git push" {
    git push origin main
}

Write-Host "done"
