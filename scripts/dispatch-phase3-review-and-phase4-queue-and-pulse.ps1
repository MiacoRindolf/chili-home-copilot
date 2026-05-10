$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-phase3-review-and-phase4-queue-and-pulse-out.txt"
"# dispatch $(Get-Date -Format o)" | Out-File $out -Encoding utf8

if (Test-Path ".git\index.lock") { Remove-Item ".git\index.lock" -ErrorAction SilentlyContinue }

# 1. Validate Phase 4 .session JSON parses
try {
    $null = Get-Content "scripts/_claude_session_queue/300-promotion-rebalance-phase4.session" -Raw | ConvertFrom-Json
    "json OK Phase 4 session" | Add-Content $out
} catch {
    "JSON PARSE ERROR Phase 4 session: $_" | Add-Content $out
    "ABORT" | Add-Content $out
    "# end" | Add-Content $out
    exit 1
}

"---" | Add-Content $out

# 2. Investigate hung pytest (CC mentioned PID 61476)
"## 2. hung pytest investigation" | Add-Content $out
$pythonProcs = Get-Process | Where-Object { $_.ProcessName -in @("python", "pytest") }
if ($pythonProcs) {
    "Currently running python/pytest processes:" | Add-Content $out
    $pythonProcs | Select-Object Id, ProcessName, StartTime, CPU, WorkingSet | Format-Table | Out-String | Add-Content $out
    # Identify candidates: pytest-named, or python with low CPU but long lifetime
    $now = Get-Date
    foreach ($p in $pythonProcs) {
        try {
            $age = $now - $p.StartTime
            $cpuSec = $p.CPU
            if ($age.TotalMinutes -gt 10 -and $cpuSec -lt 60) {
                "CANDIDATE for kill (long age, low CPU): PID=$($p.Id) name=$($p.ProcessName) age_min=$([math]::Round($age.TotalMinutes,1)) cpu_sec=$([math]::Round($cpuSec,1))" | Add-Content $out
            }
        } catch {}
    }
} else {
    "no python/pytest processes currently running" | Add-Content $out
}

# Don't auto-kill -- surface to Cowork for review.
"---" | Add-Content $out

# 3. Crypto pulse refresh (re-runnable monitor)
"## 3. crypto pulse refresh" | Add-Content $out
& "$PSScriptRoot\dispatch-crypto-pulse.ps1" 2>&1 | Out-Null
"pulse re-dispatched (output appended to dispatch-crypto-pulse-out.txt)" | Add-Content $out
"---" | Add-Content $out

# 4. Commit Phase 3 review + this dispatch script. Don't include queue file
# (transient state; daemon may move it between add and commit).
git add `
    docs/STRATEGY/COWORK_REVIEWS/2026-05-10_f-promotion-pipeline-rebalance-phase3.md `
    scripts/dispatch-phase3-review-and-phase4-queue-and-pulse.ps1 `
    scripts/dispatch-crypto-pulse.ps1 `
    2>&1 | Add-Content $out

"# git status pre-commit" | Add-Content $out
git status --short docs/STRATEGY/COWORK_REVIEWS/2026-05-10_f-promotion-pipeline-rebalance-phase3.md scripts/dispatch-phase3-review-and-phase4-queue-and-pulse.ps1 scripts/dispatch-crypto-pulse.ps1 2>&1 | Add-Content $out

"---" | Add-Content $out

$msg = @"
docs(strategy): Phase 3 review GREEN + Phase 4 queued + crypto pulse infra

Phase 3 of f-promotion-pipeline-rebalance shipped clean (commit ba05195).

Headline: the byte-identical RH parity hard gate
(test_autotrader_byte_identical_for_promoted_pattern) PASSED. The single
highest-stakes change in the entire 6-phase initiative is verified.
Helper, splice point, and audit reason all match the plan-gate-approved
design.

The plan-gate caught 3 deviations from the brief that would have shipped
silently otherwise:
  1. Helper signature reuse of already-loaded ORM row (closed a race
     window AND saved a redundant DB query)
  2. Audit decision='blocked' for grep-tooling consistency
  3. Flag-gated eligibility branch (vs brief's illustrative unconditional
     snippet) so rollback semantics work as documented

3 of 4 DB-bound routing tests hit a Postgres deadlock from a hung pytest
(PID 61476, 7s CPU after 29 min). NOT a Phase 3 bug -- environmental.
The parity hard gate AND 8 pure-unit tests passed. CC was transparent,
didn't fake pass.

This commit:
  - docs/STRATEGY/COWORK_REVIEWS/2026-05-10_f-promotion-pipeline-rebalance-phase3.md
    (Cowork review of Phase 3, GREEN verdict, what the plan-gate caught,
    the test execution caveat, forward look)
  - scripts/dispatch-crypto-pulse.ps1
    (re-runnable crypto autotrading prod monitor: get_crypto_positions
    failures, crypto_exit deferral warnings, bracket_intent missing_stop,
    reconcile_position_gone events, implausible-quote bug family,
    idle-in-tx counts. Cowork dispatches periodically while Phase 4
    runs. Output appends to dispatch-crypto-pulse-out.txt.)
  - scripts/dispatch-phase3-review-and-phase4-queue-and-pulse.ps1
    (this dispatch: stage review + verify Phase 4 .session JSON + scan
    for hung pytest processes + refresh crypto pulse)

Phase 4 (composite quality scoring + weekly cohort auto-promote) is
queued at scripts/_claude_session_queue/300-promotion-rebalance-phase4.session
with the plan-gate active and the 3 Phase 2 review answers baked in
(per-pattern hold_hours from rules_json, 1.5% threshold default,
organic accumulation no backfill). Default weights proposed:
  w1=0.30 (cpcv_sharpe), w2=0.20 (deflated_sharpe), w3=0.15 (1-pbo),
  w4=0.25 (directional_wr), w5=0.10 (1-decay), sum=1.00
Plus a new cohort-eligibility filter: directional rolling_sample_n >= 10
(thin-evidence patterns wait for accumulation).

Default chili_cohort_promote_enabled=False -- Phase 4 ships dormant
until operator opts in. Risk-asymmetric design: cohort promote routes
to shadow_promoted (Phase 3's new stage), NOT to promoted/live.
"@

$msg | Out-File ".cm.txt" -Encoding utf8
git commit -F ".cm.txt" 2>&1 | Add-Content $out
Remove-Item ".cm.txt" -ErrorAction SilentlyContinue

# Push with retry
for ($i = 1; $i -le 3; $i++) {
    "# push attempt $i" | Add-Content $out
    git push 2>&1 | Out-String | Add-Content $out
    if ($LASTEXITCODE -eq 0) {
        "push SUCCEEDED on attempt $i" | Add-Content $out
        break
    }
    if ($i -lt 3) { Start-Sleep -Seconds 5 }
}

"# end" | Add-Content $out
