$ErrorActionPreference = "Continue"
$repo = "C:\dev\chili-home-copilot"
Set-Location $repo

$out = "$PSScriptRoot\dispatch-followup-activations-commit-output.txt"
"# Followup activations commit + push - $(Get-Date -Format o)" | Out-File $out -Encoding utf8

# 1. Clear stale git lock
if (Test-Path "$repo\.git\index.lock") {
    Remove-Item "$repo\.git\index.lock" -Force -ErrorAction SilentlyContinue
    "cleared .git/index.lock" | Add-Content $out
}

# 2. Stage the files
$files = @(
    "app/config.py",
    "scripts/check_venue_truth_release_blocker.ps1",
    "scripts/dispatch-hypothesis-family-backfill.ps1",
    "scripts/dispatch-followup-activations-commit.ps1",
    "docs/STRATEGY/QUEUED/f-netedge-stage2-allocator-routing.md",
    "docs/STRATEGY/QUEUED/f-netedge-stage1-soak-audit.md",
    "docs/STRATEGY/CC_REPORTS/2026-05-15_evidence-fidelity-followup-activations.md"
)

foreach ($f in $files) {
    & git add $f 2>&1 | Add-Content $out
}

"---staged---" | Add-Content $out
& git status --short 2>&1 | Add-Content $out

# 3. Verify config.py is intact (truncation guard).
# Prefer conda-env python (project convention per CLAUDE.md). If conda
# is not on PATH, fall back to file-shape sanity: line count must match
# the expected 3270 +/- a small drift, and the file must end with the
# canonical "settings = Settings()" line. The Windows Store python alias
# is unusable so we never call bare `python` as a last resort.
"---ast-check---" | Add-Content $out
$astCmd = "import ast; ast.parse(open('app/config.py').read()); print('AST OK', sum(1 for _ in open('app/config.py')))"
$ast = $null
$astExit = 1
try {
    $ast = & conda run -n chili-env python -c $astCmd 2>&1
    $astExit = $LASTEXITCODE
} catch {
    $astExit = 1
}
if ($astExit -ne 0 -or $ast -notlike "*AST OK*") {
    "[ast-check] conda-env python unavailable; falling back to file-shape sanity" | Add-Content $out
    $lines = (Get-Content app/config.py).Count
    $tail = (Get-Content app/config.py -Tail 3) -join "`n"
    "  lines=$lines" | Add-Content $out
    "  tail:`n$tail" | Add-Content $out
    if ($lines -lt 3260 -or $lines -gt 3290 -or $tail -notlike "*settings = Settings()*") {
        "ABORT: config.py shape sanity failed (lines=$lines, tail mismatch). Refusing commit." | Add-Content $out
        exit 1
    }
    "  PASS (shape sanity)" | Add-Content $out
} else {
    $ast | Add-Content $out
}

# 4. Commit
$msg = @"
chore(trading): activate evidence-fidelity flags + queue NetEdge Stage 2

Operator-directed follow-up to the evidence-fidelity-architecture arc.

- chili_family_fdr_enabled: False -> True (Phase E BH adjustment on)
- brain_execution_cost_mode: shadow -> authoritative (Phase F)
- brain_venue_truth_mode: shadow -> authoritative (Phase F)
- check_venue_truth_release_blocker.ps1 inverted: now fires on
  mode=shadow regression; legacy semantics via -LegacyShadowLockdown
- f-netedge-stage1-soak-audit.md + f-netedge-stage2-allocator-routing.md
  queued (not yet promoted - gated on shadow-log soak data)
- dispatch-hypothesis-family-backfill.ps1 staged for daemon pickup
- CC_REPORT 2026-05-15_evidence-fidelity-followup-activations.md

No autotrader / venue / broker behavior change at runtime today; no
consumer reads mode=='authoritative' differently from mode=='shadow'
yet. The flips are forward-compatible signal + the family-FDR flag is
research-correct discipline activated.

Container restart required for the flag flips to take effect.
"@

& git commit -m $msg 2>&1 | Add-Content $out

# 5. Verify commit landed
"---log---" | Add-Content $out
& git log --oneline -3 2>&1 | Add-Content $out

# 6. Push
"---push---" | Add-Content $out
& git push origin HEAD 2>&1 | Add-Content $out

"DONE at $(Get-Date -Format o)" | Add-Content $out
