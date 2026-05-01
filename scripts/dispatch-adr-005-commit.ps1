$out = "scripts/dispatch-adr-005-commit-output.txt"
"# adr-005 commit + push $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale .git/index.lock" {
    if (Test-Path .git/index.lock) { Remove-Item -Force .git/index.lock; "removed" } else { "no lock" }
}

S "git add" {
    git add `
        docs/adr/005-canonical-truth-layer.md `
        scripts/dispatch-adr-005-commit.ps1
    "git add complete"
}

S "git commit" {
    git commit -m "docs(adr-005): canonical feature/label/execution-truth layer (response to third-party audit's biggest recommendation)"
}

S "git rev-parse HEAD" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "git push origin main" { git push origin main }

Write-Host "adr-005 commit done -- see $out"
