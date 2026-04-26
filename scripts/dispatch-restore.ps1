# One-shot recovery: pull the entire CHILI Dispatch source tree back from
# commit c1be332 ("Phase D.2.6 + async LLM logging: dispatch loop end-to-end
# working"). The packages live on a different branch (flip-revert-experiment)
# and were absent from the current branch's HEAD, leaving only stale
# __pycache__/*.pyc bytecode behind.
#
# After this finishes successfully, run .\scripts\dispatch-go.ps1 to rebuild
# the chili-app:local image and force-recreate scheduler-worker.
#
# Usage: .\scripts\dispatch-restore.ps1

$start = Get-Date
$commit = "c1be332"
$paths = @(
    "app/services/code_dispatch/",
    "app/services/distillation/",
    "app/services/llm_router/",
    "tests/test_code_dispatch_sandboxed.py",
    "tests/test_code_dispatch_shadow.py",
    "tests/test_dispatch_status_endpoint.py",
    "tests/test_validation_audit.py",
    "docs/CHILI_DISPATCH_AUTONOMOUS_DEV_PLAN.md",
    "docs/CHILI_DISPATCH_PRE_FLIGHT_BLOCKERS.md",
    "docs/CHILI_DISPATCH_RUNBOOK.md"
)

Write-Host "=== CHILI Dispatch source recovery from $commit ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Verifying commit is reachable..." -ForegroundColor Yellow
$commitInfo = git show --no-patch --oneline $commit 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: commit $commit is not reachable. Aborting." -ForegroundColor Red
    Write-Host $commitInfo
    exit 1
}
Write-Host "  $commitInfo"
Write-Host ""

Write-Host "Checking out files into working tree (HEAD branch will not change)..." -ForegroundColor Yellow
foreach ($p in $paths) {
    Write-Host "  $p" -NoNewline
    git checkout $commit -- $p 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK" -ForegroundColor Green
    } else {
        Write-Host "  FAILED (exit $LASTEXITCODE)" -ForegroundColor Red
    }
}
Write-Host ""

Write-Host "Verifying restored files..." -ForegroundColor Yellow
$expected = @(
    "app/services/code_dispatch/__init__.py",
    "app/services/code_dispatch/cycle.py",
    "app/services/code_dispatch/governance.py",
    "app/services/code_dispatch/runner.py",
    "app/services/code_dispatch/miner.py",
    "app/services/code_dispatch/scorer.py",
    "app/services/code_dispatch/rule_gate.py",
    "app/services/code_dispatch/notifier.py",
    "app/services/code_dispatch/audit.py",
    "app/services/code_dispatch/frozen_scope.py",
    "app/services/code_dispatch/synthetic_repo.py",
    "app/services/code_dispatch/validation_audit.py",
    "app/services/distillation/__init__.py",
    "app/services/distillation/evaluator.py",
    "app/services/distillation/exporter.py",
    "app/services/distillation/promotion_gate.py",
    "app/services/distillation/trainer.py",
    "app/services/llm_router/__init__.py",
    "app/services/llm_router/router.py",
    "app/services/llm_router/log.py",
    "app/services/llm_router/ollama_client.py"
)

$missing = 0
foreach ($f in $expected) {
    if (-not (Test-Path $f)) {
        Write-Host "  MISSING: $f" -ForegroundColor Red
        $missing++
    }
}

Write-Host ""
$elapsed = ((Get-Date) - $start).TotalSeconds
if ($missing -eq 0) {
    Write-Host "All $($expected.Count) files restored in $([Math]::Round($elapsed,1))s." -ForegroundColor Green
    Write-Host "Now run: .\scripts\dispatch-go.ps1" -ForegroundColor Cyan
} else {
    Write-Host "$missing file(s) missing after restore. Investigate before running dispatch-go." -ForegroundColor Red
    exit 1
}
