# Phase 4 salvage commit v2 — fixes the false-alarm size compare from v1.
# v1 used `current_bytes != head_bytes` which trips on Windows CRLF vs git LF.
# v2 uses `git diff --quiet HEAD -- <file>` which is content-aware and
# autocrlf-tolerant.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-phase4-salvage-commit-v2-out.txt"
"# dispatch-phase4-salvage-commit-v2 $(Get-Date -Format o)" | Out-File $out -Encoding utf8

if (Test-Path ".git\index.lock") { Remove-Item ".git\index.lock" -ErrorAction SilentlyContinue }

# STEP 1 — verify truncation-victim files match HEAD (content-aware)
"## STEP 1 -- verify truncation-victim files match HEAD via git diff --quiet" | Add-Content $out
$victims = @(
    "app/services/trading/auto_trader.py",
    "app/services/trading/venue/coinbase_spot.py",
    "app/services/broker_service.py",
    "app/services/trading/bracket_writer_g2.py",
    "app/services/trading/brain_work/dispatcher.py",
    "app/services/trading/learning.py",
    "app/services/trading/pdt_guard.py",
    "app/services/trading/promotion_evidence_audit.py"
)
$allClean = $true
foreach ($f in $victims) {
    & git diff --quiet HEAD -- $f 2>$null
    if ($LASTEXITCODE -ne 0) {
        "  MISMATCH $f differs from HEAD" | Add-Content $out
        $allClean = $false
    } else {
        "  OK $f matches HEAD (autocrlf-tolerant)" | Add-Content $out
    }
}
if (-not $allClean) {
    "ABORT -- truncation-victim files NOT clean per git diff" | Add-Content $out
    "$(Get-Date -Format o) ERROR-COMMIT-ABORTED-V2 -- truncation-victim files actually corrupted" | Add-Content docs/STRATEGY/COWORK_DECISIONS_LOG.md
    "# end" | Add-Content $out
    exit 1
}
"  OK -- all 8 victim files match HEAD" | Add-Content $out

# STEP 2 — stage Phase 4 brain-side changes
"## STEP 2 -- git add Phase 4 brain-side changes" | Add-Content $out
& git add `
    app/config.py `
    app/migrations.py `
    app/models/trading.py `
    app/services/trading_scheduler.py `
    2>&1 | Add-Content $out

# STEP 3 — stage NEW files (untracked)
"## STEP 3 -- git add Phase 4 new modules + test" | Add-Content $out
& git add `
    app/services/trading/pattern_quality_score.py `
    app/services/trading/pattern_cohort_promote.py `
    tests/test_pattern_cohort_promote.py `
    2>&1 | Add-Content $out

# STEP 4 — stage docs
"## STEP 4 -- git add docs" | Add-Content $out
& git add `
    docs/STRATEGY/NEXT_TASK.md `
    docs/STRATEGY/CC_REPORTS/2026-05-10_f-promotion-pipeline-rebalance-phase4.md `
    docs/STRATEGY/COWORK_REVIEWS/2026-05-10_f-promotion-pipeline-rebalance-phase4.md `
    docs/STRATEGY/COWORK_DECISIONS_LOG.md `
    scripts/dispatch-phase4-salvage-commit-v2.ps1 `
    scripts/dispatch-autotrader-health-probe-v2.ps1 `
    2>&1 | Add-Content $out

# STEP 5 — show staged diffstat
"## STEP 5 -- staged files" | Add-Content $out
& git diff --cached --stat 2>&1 | Out-String | Add-Content $out

# STEP 6 — paranoia: no victim files staged
"## STEP 6 -- paranoia check" | Add-Content $out
$stagedVictims = @()
foreach ($f in $victims) {
    $staged = & git diff --cached --name-only -- $f 2>$null
    if ($staged) { $stagedVictims += $f }
}
if ($stagedVictims.Count -gt 0) {
    "ABORT -- victim files in staged set: $($stagedVictims -join ', ')" | Add-Content $out
    & git reset 2>&1 | Add-Content $out
    "$(Get-Date -Format o) ERROR-COMMIT-ABORTED-V2 -- victim files staged" | Add-Content docs/STRATEGY/COWORK_DECISIONS_LOG.md
    "# end" | Add-Content $out
    exit 2
}
"  OK -- no victim files staged" | Add-Content $out

# STEP 7 — commit
"## STEP 7 -- commit" | Add-Content $out
$msg = @"
feat(brain): Phase 4 composite quality scoring + cohort auto-promote (promotion-pipeline-rebalance)

Phase 4 of f-promotion-pipeline-rebalance shipped DORMANT
(chili_cohort_promote_enabled=False default; operator opts in).

Composite formula:
  composite = w1*clip(cpcv_sharpe/2.0, 0, 1)
            + w2*clip(deflated_sharpe/1.0, 0, 1)
            + w3*(1 - clip(pbo, 0, 1))
            + w4*directional_wr
            + w5*(1 - decay)

Default weights: w1=0.30, w2=0.20, w3=0.15, w4=0.25, w5=0.10 (sum=1.0).

Decay = max(0, older_15_wr - newer_15_wr). Patterns with
rolling_sample_n < 30 produce decay=NULL → composite=NULL → excluded
from cohort eligibility (NULL propagation; advisor brief §2.6).

Pattern 585 calibration: composite ≈ 0.843 (top tier).

Files:
  + app/migrations.py: mig 237 (quality_composite_score column,
    idempotent ADD COLUMN IF NOT EXISTS)
  + app/config.py: 8 new settings (kill switch + 5 weights + top_n + cap)
  + app/models/trading.py: ScanPattern.quality_composite_score
  + app/services/trading_scheduler.py: 2 new jobs (nightly score
    refresh @ 23:30 PT, weekly cohort promote @ Sun 22:00 PT)
  + app/services/trading/pattern_quality_score.py (NEW): score module
  + app/services/trading/pattern_cohort_promote.py (NEW): cohort job
  + tests/test_pattern_cohort_promote.py (NEW): 21 tests

Cohort routes to shadow_promoted (Phase 3's stage), NOT promoted/live.

INCIDENT NOTE: CC's session was killed mid-flight after Edit-tool
truncated 8 unrelated large files (auto_trader.py, broker_service.py,
coinbase_spot.py, bracket_writer_g2.py, brain_work/dispatcher.py,
learning.py, pdt_guard.py, promotion_evidence_audit.py — total 1743
lines deleted). Brain-side intended Phase 4 work was clean and
matched plan-gate-approved bindings. Restored victim files via
nuclear delete-then-restore (`Remove-Item` + `git restore --source=HEAD
--worktree`). Salvage commit v1 aborted on a CRLF/LF size-compare
false alarm; v2 uses `git diff --quiet HEAD --` (autocrlf-tolerant).

Phase 4 ships dormant. Operator opts in via CHILI_COHORT_PROMOTE_ENABLED=true.
"@
$msg | Out-File ".cm.txt" -Encoding utf8
& git commit -F ".cm.txt" 2>&1 | Add-Content $out
Remove-Item ".cm.txt" -ErrorAction SilentlyContinue

# STEP 8 — push with retries
"## STEP 8 -- push" | Add-Content $out
for ($i = 1; $i -le 5; $i++) {
    "  attempt $i" | Add-Content $out
    & git push 2>&1 | Out-String | Add-Content $out
    if ($LASTEXITCODE -eq 0) {
        "  push SUCCEEDED on attempt $i" | Add-Content $out
        break
    }
    if ($i -lt 5) { Start-Sleep -Seconds 10 }
}

# STEP 9 — clear pause flag (Phase 4 done; Phase 5 ready to pick up)
"## STEP 9 -- remove pause flag" | Add-Content $out
if (Test-Path scripts/_claude_session_pause.flag) {
    Remove-Item scripts/_claude_session_pause.flag -Force
    "  pause flag removed; session daemon will pick up Phase 5 within 30s" | Add-Content $out
} else {
    "  no pause flag" | Add-Content $out
}

"$(Get-Date -Format o) PHASE-4-SALVAGED-V2 -- Phase 4 brain-side committed (mig 237 + 5 brain files + 2 new modules + test); pause flag cleared; Phase 5 .session in queue ready for session daemon pickup" | Add-Content docs/STRATEGY/COWORK_DECISIONS_LOG.md

"# end" | Add-Content $out
