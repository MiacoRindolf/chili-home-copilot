# Deploy the f-exit-parity-trail-atr-zero-divergence Option A fix.
#
# What it does:
#   1. Runs the parity test suite (existing + new ATR=0 regression test).
#   2. If green: git add the two changed files + commit + push.
#   3. If red:   write the failure and abort (no commit, no push).
#
# Files touched by the fix (not by this script):
#   - app/services/backtest_service.py        (legacy ATR=0 guard)
#   - tests/test_exit_evaluator_parity.py     (helper mirrors fix +
#                                              new test_atr_zero_holds_in_both_engines)
#
# Output: scripts/dispatch-atr-zero-fix-deploy-output.txt

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-atr-zero-fix-deploy-output.txt"
"# atr-zero fix deploy $(Get-Date -Format o)" | Out-File $out -Encoding utf8
"---" | Add-Content $out

# ---- Section 1: pytest ----------------------------------------------
"" | Add-Content $out
"## 1. pytest tests/test_exit_evaluator_parity.py" | Add-Content $out
"----" | Add-Content $out

$env:TEST_DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili_test"
$pytest = & conda run --no-capture-output -n chili-env python -m pytest `
    tests/test_exit_evaluator_parity.py `
    -v -p no:asyncio --tb=short 2>&1
$pytest_exit = $LASTEXITCODE
$pytest | Add-Content $out
"" | Add-Content $out
"pytest exit code: $pytest_exit" | Add-Content $out

if ($pytest_exit -ne 0) {
    "" | Add-Content $out
    "## ABORT: pytest failed, NOT committing or pushing." | Add-Content $out
    Write-Output "pytest failed (exit=$pytest_exit). See $out."
    exit 1
}

# ---- Section 2: git status (sanity) -------------------------------------
"" | Add-Content $out
"## 2. git status --short" | Add-Content $out
"----" | Add-Content $out
$status = git status --short 2>&1
$status | Add-Content $out

# ---- Section 3: git diff stat ------------------------------------------
"" | Add-Content $out
"## 3. git diff --stat (changed files)" | Add-Content $out
"----" | Add-Content $out
$diff = git diff --stat -- `
    app/services/backtest_service.py `
    tests/test_exit_evaluator_parity.py 2>&1
$diff | Add-Content $out

# ---- Section 4: stage + commit + push ----------------------------------
"" | Add-Content $out
"## 4. stage + commit + push" | Add-Content $out
"----" | Add-Content $out

$add = git add `
    app/services/backtest_service.py `
    tests/test_exit_evaluator_parity.py 2>&1
"git add: $add" | Add-Content $out

$msg = @"
fix(backtest): guard legacy trail-close when atr_val == 0

Pre-fix the legacy DynamicPatternStrategy.next() collapsed
``trailing_stop = highest_since_entry - exit_atr_mult * 0`` to
``highest_since_entry`` on degenerate-volatility bars, firing
``exit_trail`` on ANY pullback from the running peak -- a fixed
peak-stop, not a trailing-stop. This was the sole structural
divergence the exit-parity cutover-gate ever surfaced (39
``legacy_only_close`` rows in the 2026-05-09 24h window, all
with priority_winner='exit_trail').

The canonical ExitEvaluator already short-circuits trail updates
when ``atr is None or atr <= 0``. The legacy path now matches.

Net effect:
- 39-row cohort in backtest_service.py is removed at source.
- Existing parity tests stay green (helper mirrors the fix).
- New test_atr_zero_holds_in_both_engines pins the invariant.

Closes ``f-exit-parity-trail-atr-zero-divergence`` (Option A).
"@
$msg | Set-Content scripts\_atr_zero_commit_msg.txt -Encoding utf8

$commit = git commit -F scripts\_atr_zero_commit_msg.txt 2>&1
$commit_exit = $LASTEXITCODE
"git commit exit=$commit_exit" | Add-Content $out
$commit | Add-Content $out

Remove-Item scripts\_atr_zero_commit_msg.txt -ErrorAction SilentlyContinue

if ($commit_exit -ne 0) {
    "## ABORT: commit failed, NOT pushing." | Add-Content $out
    Write-Output "commit failed (exit=$commit_exit). See $out."
    exit 1
}

$push = git push origin HEAD 2>&1
$push_exit = $LASTEXITCODE
"git push exit=$push_exit" | Add-Content $out
$push | Add-Content $out

"" | Add-Content $out
"## DONE" | Add-Content $out
Write-Output "Wrote $out (exit=$push_exit)"
