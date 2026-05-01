$out = "scripts/dispatch-r28-apply-output.txt"
"# r28 apply TCA-cleanup + commit + push $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "apply edit (host git)" {
    conda run -n chili-env python scripts/_r28_apply_tca_cleanup.py
}

S "py-compile" {
    conda run -n chili-env python -m py_compile app/services/broker_service.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git diff stat" {
    git diff --stat app/services/broker_service.py
}

S "force-recreate broker-sync-worker (only worker that runs sync_positions_to_db / cleanup_manual_trades)" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "wait 10s + container health" {
    Start-Sleep -Seconds 10
    docker ps --filter "name=chili-home-copilot-broker-sync-worker-1" --format "{{.Names}} | {{.Status}}"
}

S "broker-sync-worker startup errors?" {
    docker compose logs --since 30s broker-sync-worker 2>&1 | Select-String "ERROR|Traceback|ImportError|SyntaxError" | Select-Object -Last 10
}

S "remove stale .git/index.lock if present" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        app/services/broker_service.py `
        scripts/_r28_apply_tca_cleanup.py `
        scripts/dispatch-r28-apply.ps1 `
        scripts/dispatch-phase2-prep-probe.ps1 `
        scripts/dispatch-phase2-prep-probe-output.txt `
        scripts/dispatch-phase2a-tca-probe.ps1 `
        scripts/dispatch-phase2a-tca-probe-output.txt `
        docs/AUDITS/2026-04-30-third-party-response.md
    "git add complete"
}

S "git commit" {
    git commit -m "fix(r28): remove TCA corruption from synthetic-close paths (corrupt zero slippage); leave column NULL where ref-price was synthetic"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -3" { git log --oneline -3 }

S "git push origin main" { git push origin main }

Write-Host "r28 apply done -- see $out"
