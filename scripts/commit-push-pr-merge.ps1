# scripts/commit-push-pr-merge.ps1
# One-shot: stage everything, commit, push the branch, open a PR against main, then merge.
# Requires: git in PATH and `gh` (GitHub CLI) authenticated with repo write.
# Usage:    .\scripts\commit-push-pr-merge.ps1

param(
    [string]$BaseBranch = "main",
    [string]$CommitMessage = $null,
    [switch]$NoMerge
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path "$PSScriptRoot\..\"
Set-Location $RepoRoot

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

# 1. Branch sanity ----------------------------------------------------------
$current = (& git rev-parse --abbrev-ref HEAD).Trim()
if ([string]::IsNullOrEmpty($current) -or $current -eq "HEAD") {
    throw "Detached HEAD; check out a branch first."
}
if ($current -eq $BaseBranch) {
    throw "On '$BaseBranch'; create a feature branch before running this."
}
Section "Branch state"
Write-Host "Current branch: $current"
Write-Host "Base branch:    $BaseBranch"

# 2. Drop scratch files (leftover dispatch test artifacts) ------------------
Section "Cleaning scratch files"
$Scratch = @(
    "app/services/code_dispatch/test2.py",
    "app/services/code_dispatch/test3.py",
    "app/services/code_dispatch/test_write.txt"
)
foreach ($f in $Scratch) {
    if (Test-Path $f) {
        Remove-Item $f -Force
        Write-Host "  removed $f"
    }
}

# 3. Stage everything (gitignore filters daemon artifacts + log dumps) -----
Section "git add -A"
git add -A
git status --short | Select-Object -First 30 | Out-Host

# 4. Anything to commit? ----------------------------------------------------
$staged = (& git diff --cached --name-only) -join "`n"
if ([string]::IsNullOrWhiteSpace($staged)) {
    Write-Host "Nothing staged — branch already clean." -ForegroundColor Yellow
} else {
    if (-not $CommitMessage) {
        $CommitMessage = @"
Phase F.4-F.14: universal LLM gateway, learning loop, project-domain agents

- F.10  Universal LLM Gateway (per-purpose routing: passthrough/augmented/tree)
        + tree-of-context: Ollama decompose/chunks/cross-exam/compile -> premium synth
        + 36 purpose policies seeded across chat, code dispatch, trading,
          project-brain agents, planner, memory, personality, wellness,
          desktop, reasoning, web search
        + migration 174 (gateway/decomp/chunk/policy tables)

- F.4-F.6 Gateway learning loop
        + migration 175 (gateway_pattern, policy_change_proposal,
          gateway_learning_run; enriched context_brain_outcome)
        + outcome_tracker (chat followup heuristic, thumbs, dispatch, trade)
        + distiller (strategy/chunks/model vs outcome correlations)
        + policy_evolver (low-stakes auto-apply + high-stakes proposals)
        + APScheduler entries: distiller every 15min, evolver hourly
        + 7 visibility endpoints + Brain hub UI section
          (patterns, proposals, outcomes, learning runs, manual trigger)

- F.7  Chat thumbs feedback
        + gateway_log_id propagated through chat_service -> /api/chat
        + thumbs UI on bot replies, POST -> /api/brain/context/gateway/thumbs

- F.14 Project-domain agents -> gateway
        + call_llm autodetects purpose from caller frame
          (project_brain/agents/*.py + playwright_runner + web_research)
        + 12 new project_* policies; zero agent-file edits

- Cross-examination: pulled llama3.2:1b alongside qwen2.5-coder for
  dual-model agreement on tree-routed purposes.

Earlier in this branch (background context):
- Phase D Chili Dispatch (autonomous coder loop)
- Phase E reactive Code Brain (event bus, decision router, trigger watchers)
- Phase F.1-F.3 Context Brain (intent router, retrievers, scorer, budget, composer)
- Migration 171-173 (dispatch / code brain / context brain tables)
- Bridge daemon for autonomous Claude execution
- Trading brain neural mesh, momentum neural pipeline, etc.
"@
    }
    Section "Committing"
    git commit -m $CommitMessage
}

# 5. Push -------------------------------------------------------------------
Section "Pushing $current"
git push -u origin $current

# 6. PR ---------------------------------------------------------------------
Section "Creating PR -> $BaseBranch"
$prTitle = "Phase F: Universal LLM gateway, learning loop, project-domain agents"
$prBody = @"
End-to-end shipment of the universal LLM gateway and the F.4-F.6 learning loop,
plus chat thumbs feedback and project-domain auto-routing.

See the commit message on the head commit for the per-phase summary.

**Migrations:** 174, 175 (idempotent, applied at app startup)
**Policies seeded:** 36 across chat / code dispatch / trading / project-brain / etc.
**New endpoints:**
- GET  /api/brain/context/gateway/log
- GET  /api/brain/context/gateway/summary
- GET  /api/brain/context/gateway/tree
- GET  /api/brain/context/gateway/policies
- GET  /api/brain/context/gateway/outcomes
- GET  /api/brain/context/gateway/patterns
- GET  /api/brain/context/gateway/proposals
- POST /api/brain/context/gateway/proposals/{id}/decide
- POST /api/brain/context/gateway/thumbs
- POST /api/brain/context/gateway/learn/run
- GET  /api/brain/context/gateway/learn/runs

**Operator controls:** Brain hub -> Context tab -> Gateway Learning Loop
(pending proposals approve/reject, manual run-pass-now, live patterns +
outcomes + run history).

Smoke-tested locally: distiller and evolver runs persisted, all visibility
endpoints return JSON, chat path produces gateway_log_id, thumbs button writes
to context_brain_outcome.
"@

# Check if a PR already exists for this branch
$existing = & gh pr list --head $current --base $BaseBranch --json number,url 2>$null | ConvertFrom-Json
if ($existing -and $existing.Count -gt 0) {
    $prNum = $existing[0].number
    Write-Host "PR #$prNum already open for $current -> $BaseBranch"
    Write-Host "URL: $($existing[0].url)"
} else {
    $prUrl = & gh pr create --base $BaseBranch --head $current --title $prTitle --body $prBody
    Write-Host $prUrl
    if ($prUrl -match "/pull/(\d+)") { $prNum = $Matches[1] } else { $prNum = $null }
}

# 7. Merge ------------------------------------------------------------------
if ($NoMerge) {
    Write-Host "`n--no-merge supplied; stopping after PR creation." -ForegroundColor Yellow
    exit 0
}
if ($prNum) {
    Section "Merging PR #$prNum"
    # --merge keeps the branch history (instead of squash/rebase) so the per-phase
    # commits stay legible. --delete-branch cleans up the feature branch.
    gh pr merge $prNum --merge --delete-branch --admin
    Write-Host "`nMerged. Local main may be stale; run: git checkout $BaseBranch && git pull"
}
