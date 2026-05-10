$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-cowork-advisor-and-plan-gate-out.txt"
"# dispatch-cowork-advisor-and-plan-gate $(Get-Date -Format o)" | Out-File $out -Encoding utf8

if (Test-Path ".git\index.lock") { Remove-Item ".git\index.lock" -ErrorAction SilentlyContinue }

# Parse-check the daemon patch (the highest-risk file in this commit).
"# parse-check daemon" | Add-Content $out
$tokens = $null
$errors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path "scripts/_claude_session_daemon.ps1").Path,
    [ref]$tokens,
    [ref]$errors
)
if ($errors -and $errors.Count -gt 0) {
    "PARSE ERRORS: $($errors.Count)" | Add-Content $out
    foreach ($e in $errors) {
        "  line $($e.Extent.StartLineNumber):$($e.Extent.StartColumnNumber) -- $($e.Message)" | Add-Content $out
    }
    "ABORT: not committing while parse errors exist" | Add-Content $out
    "# end" | Add-Content $out
    exit 1
} else {
    "parse OK ($($tokens.Count) tokens)" | Add-Content $out
}

# Truncation check on the advisor brief (markdown — should end with the
# "explicit authorization" sentence).
$advisorPath = "docs/STRATEGY/COWORK_ADVISOR_BRIEF.md"
if (Test-Path $advisorPath) {
    $lastLine = (Get-Content $advisorPath -Tail 3 | Out-String).Trim()
    "advisor brief tail: $lastLine" | Add-Content $out
    if ($lastLine -notmatch 'explicit authorization') {
        "WARN: advisor brief might be truncated -- expected to end near 'explicit authorization'" | Add-Content $out
    }
} else {
    "ABORT: advisor brief missing" | Add-Content $out
    "# end" | Add-Content $out
    exit 1
}

"---" | Add-Content $out

git add `
    docs/STRATEGY/COWORK_ADVISOR_BRIEF.md `
    docs/STRATEGY/CLAUDE_SESSION_DAEMON.md `
    scripts/_claude_session_daemon.ps1 `
    scripts/_claude_session_consult/.gitkeep `
    scripts/dispatch-cowork-advisor-and-plan-gate.ps1 `
    2>&1 | Add-Content $out

"# git status" | Add-Content $out
git status --short docs/STRATEGY/COWORK_ADVISOR_BRIEF.md docs/STRATEGY/CLAUDE_SESSION_DAEMON.md scripts/_claude_session_daemon.ps1 scripts/_claude_session_consult/.gitkeep scripts/dispatch-cowork-advisor-and-plan-gate.ps1 2>&1 | Add-Content $out
"---" | Add-Content $out

$msg = @"
infra(claude): COWORK_ADVISOR_BRIEF + plan-gate consult protocol

Two improvements to the Cowork-CC collaboration based on operator
feedback that one-shot autonomous sessions miss the iteration that
produces high-quality work.

## 1. docs/STRATEGY/COWORK_ADVISOR_BRIEF.md (NEW)

Curated by Cowork for CC to read at session start, immediately after
CLAUDE.md and PROTOCOL.md. Captures what's NOT in the repo:

  - Operator preferences (action over narration, no apologies-fest)
  - Hard hazards (Edit truncation, PowerShell Out-File BOM, FIX 46 leak,
    test DB safety, migration ID rules, no-magic-fallbacks)
  - Three-lane brain architecture mapping (reconcile / work-ledger /
    scheduler-batch)
  - Currently-armed state (Coinbase Phase 6 LIVE soak through 2026-05-11,
    f-promotion-pipeline-rebalance Phase 1 done)
  - The plan-gate consultation protocol (§6)
  - Workflow checklist for every CC session
  - When to push back (operator values pushback over compliance)

This is the closest thing to a one-way knowledge transfer from Cowork
to CC. Future updates: append a hazard whenever a new failure pattern
is discovered. Living doc.

## 2. scripts/_claude_session_daemon.ps1 (PATCH) -- plan-gate

Adds a consult-watching loop to Run-Session:

  - Creates scripts/_claude_session_consult/<id>/ at session start
  - Sets \$env:CHILI_SESSION_ID = <id> so spawned CC can derive its
    consult dir
  - Polls every 5s for *.request.md files lacking matching *.response.md
  - When a pending request appears, status.json flips to
    state: "awaiting_review" with the request file paths
  - When all requests have responses, reverts to state: "running"

CC's session prompt opts in by referencing
\$env:CHILI_SESSION_ID and the consult-dir protocol (see
docs/STRATEGY/COWORK_ADVISOR_BRIEF.md §6). Pre-Phase-3 sessions whose
prompts don't reference the consult dir simply ignore it.

The plan-gate use case: before CC writes any code, it submits its
implementation plan to plan.request.md and waits for plan.response.md.
Cowork (operator-mediated) reviews and writes APPROVED / REVISE / ABORT.
Catches design errors at the highest-leverage moment.

Does NOT modify the currently-running retry4 session (Phase 2 is
mechanical and low-risk). Plan-gate kicks in starting Phase 3 of
f-promotion-pipeline-rebalance, where the byte-identical RH parity
HARD GATE makes pre-code review highest-value.

## 3. docs/STRATEGY/CLAUDE_SESSION_DAEMON.md (UPDATE)

Documents the plan-gate protocol: layout, prompt template, how Cowork
responds, recovery semantics, and the design trade-off (chain stalls
when neither operator nor Cowork is reachable -- by design).

## 4. scripts/_claude_session_consult/.gitkeep (NEW)

Persists the consult dir under git so a fresh checkout has it.

## Operator side

After this commit lands, the running daemon needs a restart to pick up
the patch. But ONLY AFTER retry4 (Phase 2) finishes. The patch is
fully backwards compatible -- a daemon restart mid-no-session changes
nothing for sessions whose prompts don't reference the consult dir.

When Phase 3 is queued, its prompt will include the plan-gate protocol
and CC will pause for Cowork review of its plan before writing code.
"@

$msg | Out-File ".cm.txt" -Encoding utf8
git commit -F ".cm.txt" 2>&1 | Add-Content $out
Remove-Item ".cm.txt" -ErrorAction SilentlyContinue

git push 2>&1 | Add-Content $out

"# end" | Add-Content $out
