$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-phase2-review-and-phase3-queue-out.txt"
"# dispatch-phase2-review-and-phase3-queue $(Get-Date -Format o)" | Out-File $out -Encoding utf8

if (Test-Path ".git\index.lock") { Remove-Item ".git\index.lock" -ErrorAction SilentlyContinue }

# Validate Phase 3 .session JSON parses
try {
    $null = Get-Content "scripts/_claude_session_queue/200-promotion-rebalance-phase3.session" -Raw | ConvertFrom-Json
    "json OK Phase 3 session" | Add-Content $out
} catch {
    "JSON PARSE ERROR Phase 3 session: $_" | Add-Content $out
    "ABORT" | Add-Content $out
    "# end" | Add-Content $out
    exit 1
}

"---" | Add-Content $out

# Commit only the persistent docs + dispatch script. The session file in
# queue/ may get picked up by the daemon between `git add` and `git commit`,
# so we don't try to commit it -- session files are transient anyway.
git add `
    docs/STRATEGY/COWORK_REVIEWS/2026-05-09_f-promotion-pipeline-rebalance-phase2.md `
    scripts/dispatch-phase2-review-and-phase3-queue.ps1 `
    2>&1 | Add-Content $out

"# git status" | Add-Content $out
git status --short docs/STRATEGY/COWORK_REVIEWS/ scripts/dispatch-phase2-review-and-phase3-queue.ps1 2>&1 | Add-Content $out
"---" | Add-Content $out

$msg = @"
docs(strategy): COWORK_REVIEW for Phase 2 + Phase 3 queued with plan-gate

Phase 2 of f-promotion-pipeline-rebalance shipped clean (commit e480d9f,
32m13s session, 19/19 new tests pass, no regression). Headline finding:
pattern 585's directional WR is 73.3% on 30 alerts -- vindicating the
brief's hypothesis that gate-laundered realized WR was misleading us.

This commit adds:
  - Phase 2 review at docs/STRATEGY/COWORK_REVIEWS/
    Documents the verdict (GREEN), what was nailed, and answers to the
    3 open questions CC surfaced (hold-window, threshold, backfill).
    These answers feed into Phase 4's brief when I write it.

Phase 3 (shadow_promoted lifecycle) is queued at
scripts/_claude_session_queue/200-promotion-rebalance-phase3.session
with PLAN-GATE PROTOCOL active in the prompt. When the session daemon
picks it up, CC will:
  1. Read CLAUDE.md, PROTOCOL.md, COWORK_ADVISOR_BRIEF.md, NEXT_TASK,
     the brief, and Phase 2's CC_REPORT
  2. Write its implementation plan to plan.request.md
  3. Wait for plan.response.md (Cowork reviews and writes APPROVED/
     REVISE/ABORT)
  4. Implement only after APPROVED

Phase 3 is the highest-stakes change in the initiative because it
touches auto_trader.py and must preserve BYTE-IDENTICAL behavior for
non-shadow_promoted patterns (the RH parity HARD GATE). The plan-gate
exists for exactly this moment -- catches splice-point errors before
the irreversible cost of writing 1700-line file edits.

The session file itself is not tracked (transient state in queue/).
"@

$msg | Out-File ".cm.txt" -Encoding utf8
git commit -F ".cm.txt" 2>&1 | Add-Content $out
Remove-Item ".cm.txt" -ErrorAction SilentlyContinue

git push 2>&1 | Add-Content $out

"# end" | Add-Content $out
